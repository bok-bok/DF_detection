from typing import Dict, List, Optional, Sequence, Union

import numpy as np
import torch
import torch.nn as nn
import torchmetrics
from pytorch_lightning import LightningModule

# from lightning import LightningModule
from torch import Tensor
from torch.nn import BCEWithLogitsLoss, CrossEntropyLoss
from torch.optim import Adam
from torch.optim.lr_scheduler import ReduceLROnPlateau

import fairseq
import models.avhubert.hubert as hubert
import models.avhubert.hubert_pretraining as hubert_pretraining
import wandb
from eval_metrics import compute_eer
from fairseq.data.dictionary import Dictionary
from fairseq.modules import LayerNorm
from loss import ContrastLoss, OCSoftmax


def Average(lst):
    return sum(lst) / len(lst)


def Opposite(a):
    a = a + 1
    a[a > 1.5] = 0
    return a


class MSOC(LightningModule):

    def __init__(
        self,
        weight_decay=0.0001,
        learning_rate=0.0002,
        batch_size=32,
        sync=False,
        threshold=False,
        audio_threshold=0.5,
        video_threshold=0.5,
        final_threshold=0.5,
    ):
        super().__init__()
        self.model = hubert.AVHubertModel(
            cfg=hubert.AVHubertConfig,
            task_cfg=hubert_pretraining.AVHubertPretrainingConfig,
            dictionaries=hubert_pretraining.AVHubertPretrainingTask,
        )
        self.sync = sync

        # if true, use threshold not softmax
        self.threshold = threshold

        self.audio_threshold = audio_threshold
        self.video_threshold = video_threshold
        self.final_threshold = final_threshold

        if threshold:
            print("Using threshold")
            print(f"Audio: {audio_threshold}, Visual: {video_threshold}, Final: {final_threshold}")

        self.batch_size = batch_size
        self.embed = 768
        self.dropout = 0.1

        self.feature_extractor_audio_hubert = self.model.feature_extractor_audio
        self.feature_extractor_video_hubert = self.model.feature_extractor_video
        self.project_audio = nn.Sequential(
            LayerNorm(self.embed), nn.Linear(self.embed, self.embed), nn.Dropout(self.dropout)
        )

        self.project_video = nn.Sequential(
            LayerNorm(self.embed), nn.Linear(self.embed, self.embed), nn.Dropout(self.dropout)
        )

        self.project_hubert = nn.Sequential(
            self.model.layer_norm, self.model.post_extract_proj, self.model.dropout_input
        )

        self.fusion_encoder_hubert = self.model.encoder

        self.final_proj_audio = self.model.final_proj
        self.final_proj_video = self.model.final_proj
        self.final_proj_hubert = self.model.final_proj

        if not threshold:
            self.mm_classifier = nn.Sequential(
                nn.Linear(3, 2),
            )

        self.loss_audio = OCSoftmax(feat_dim=768, alpha=20).to("cuda")
        self.loss_visual = OCSoftmax(feat_dim=768, alpha=20).to("cuda")

        self.av_classifier = nn.Sequential(
            nn.Linear(self.embed, self.embed), nn.ReLU(inplace=True), nn.Linear(self.embed, 2)
        )

        self.mm_cls = CrossEntropyLoss()
        self.binary_cls = BCEWithLogitsLoss()

        # init loss computer

        self.weight_decay = weight_decay
        self.learning_rate = learning_rate

        self.acc = torchmetrics.classification.BinaryAccuracy()
        self.auroc = torchmetrics.classification.BinaryAUROC(thresholds=None)
        self.f1score = torchmetrics.classification.BinaryF1Score()
        self.recall = torchmetrics.classification.BinaryRecall()
        self.precisions = torchmetrics.classification.BinaryPrecision()

        self.best_loss = 1e9
        self.best_acc, self.best_auroc = 0.0, 0.0
        self.best_real_f1score, self.best_real_recall, self.best_real_precision = 0.0, 0.0, 0.0
        self.best_fake_f1score, self.best_fake_recall, self.best_fake_precision = 0.0, 0.0, 0.0

        self.softmax = nn.Softmax(dim=1)

    def forward(self, video: Tensor, audio: Tensor, mask: Tensor):
        # print(audio.shape, video.shape)
        a_features = self.feature_extractor_audio_hubert(audio).transpose(1, 2)
        v_features = self.feature_extractor_video_hubert(video).transpose(1, 2)
        # augmentation here
        # shift augmentation for audio

        # warp augmentation for audio

        av_features = torch.cat([a_features, v_features], dim=2)

        a_cross_embeds = a_features.mean(1)
        v_cross_embeds = v_features.mean(1)

        a_features = self.project_audio(a_features)
        v_features = self.project_video(v_features)
        av_features = self.project_hubert(av_features)

        a_embeds = a_features.mean(1)
        v_embeds = v_features.mean(1)

        av_features, _ = self.fusion_encoder_hubert(av_features, padding_mask=mask)

        # extract cls token
        av_features = av_features[:, 0, :]

        return av_features, v_cross_embeds, a_cross_embeds, v_embeds, a_embeds

    def loss_fn(
        self, av_feature, v_feats, a_feats, v_embeds, a_embeds, v_label, a_label, c_label, m_label, s_label
    ) -> Dict[str, Tensor]:

        v_loss, v_score = self.loss_visual(v_embeds, v_label)
        a_loss, a_score = self.loss_audio(a_embeds, a_label)
        # av_loss, av_score = self.loss_av(av_feature, m_label)
        av_logits = self.av_classifier(av_feature)

        if not self.threshold:

            av_score = av_logits[:, 1]
            all_scores = torch.stack([v_score, a_score, av_score], dim=1)

            logits = self.mm_classifier(all_scores)
            mm_loss = self.mm_cls(logits, m_label)

            scores = self.softmax(logits)
            preds = torch.argmax(scores, dim=1)
            scores = scores[:, 1].data.cpu().numpy()
        else:
            train_v_value = (v_score + 1) / 2
            train_a_value = (a_score + 1) / 2

            v_value = (v_score > self.video_threshold).float()
            a_value = (a_score > self.audio_threshold).float()
            av_value = av_logits[:, 1]

            train_final_value = ((train_v_value + train_a_value + av_value) / 3).float()
            mm_loss = self.binary_cls(train_final_value, m_label.float())

            final_value = (v_value + a_value + av_value) / 3
            preds = (final_value > self.final_threshold).float()
            scores = final_value.data.cpu().numpy()

        if self.sync:
            av_loss = self.mm_cls(av_logits, s_label)
            loss = v_loss + a_loss + mm_loss + av_loss
        else:
            loss = v_loss + a_loss + mm_loss

        result_dict = {
            "loss_dict": {"loss": loss, "mm_loss": mm_loss},
            "scores": scores,
            "preds": preds,
            # "logits": logits,
        }

        return result_dict

    def training_step(
        self,
        batch: Optional[Union[Tensor, Sequence[Tensor]]] = None,
        batch_idx: Optional[int] = None,
        optimizer_idx: Optional[int] = None,
        hiddens: Optional[Tensor] = None,
    ) -> Tensor:
        av_features, v_feats, a_feats, v_embeds, a_embeds = self(
            batch["video"], batch["audio"], batch["padding_mask"]
        )
        result_dict = self.loss_fn(
            av_features,
            v_feats,
            a_feats,
            v_embeds,
            a_embeds,
            batch["v_label"],
            batch["a_label"],
            batch["c_label"],
            batch["m_label"],
            batch["s_label"],
        )
        loss_dict = result_dict["loss_dict"]

        # scores = self.softmax(result_dict["logits"])
        # preds = torch.argmax(scores, dim=1)
        # scores = scores[:, 1].data.cpu().numpy()

        #
        self.log_dict(
            {f"train_{k}": v for k, v in loss_dict.items()},
            on_step=True,
            on_epoch=True,
            prog_bar=False,
            sync_dist=False,
            batch_size=self.batch_size,
        )

        return {
            "loss": loss_dict["loss"],
            "preds": result_dict["preds"],
            "targets": batch["m_label"].detach(),
        }

    def validation_step(
        self,
        batch: Optional[Union[Tensor, Sequence[Tensor]]] = None,
        batch_idx: Optional[int] = None,
        optimizer_idx: Optional[int] = None,
        hiddens: Optional[Tensor] = None,
    ) -> Tensor:

        av_features, v_feats, a_feats, v_embeds, a_embeds = self(
            batch["video"], batch["audio"], batch["padding_mask"]
        )
        result_dict = self.loss_fn(
            av_features,
            v_feats,
            a_feats,
            v_embeds,
            a_embeds,
            batch["v_label"],
            batch["a_label"],
            batch["c_label"],
            batch["m_label"],
            batch["s_label"],
        )

        loss_dict = result_dict["loss_dict"]

        # preds = torch.argmax(self.softmax(result_dict["logits"]), dim=1)

        # scores = self.softmax(result_dict["logits"])
        # preds = torch.argmax(scores, dim=1)
        # scores = scores[:, 1].data.cpu().numpy()

        self.log_dict(
            {f"val_{k}": v for k, v in loss_dict.items()},
            on_step=True,
            on_epoch=True,
            prog_bar=False,
            sync_dist=False,
            batch_size=self.batch_size,
        )

        return {
            "loss": loss_dict["loss"],
            "preds": result_dict["preds"],
            # "preds": preds.detach(),
            "targets": batch["m_label"].detach(),
            "scores": result_dict["scores"],
            # "scores": scores,
        }

    def test_step(
        self,
        batch: Optional[Union[Tensor, Sequence[Tensor]]] = None,
        batch_idx: Optional[int] = None,
        optimizer_idx: Optional[int] = None,
        hiddens: Optional[Tensor] = None,
    ) -> Tensor:

        av_features, v_feats, a_feats, v_embeds, a_embeds = self(
            batch["video"], batch["audio"], batch["padding_mask"]
        )
        result_dict = self.loss_fn(
            av_features,
            v_feats,
            a_feats,
            v_embeds,
            a_embeds,
            batch["v_label"],
            batch["a_label"],
            batch["c_label"],
            batch["m_label"],
            batch["s_label"],
        )
        loss_dict = result_dict["loss_dict"]
        # preds = torch.argmax(self.softmax(result_dict["logits"]), dim=1)
        # scores = self.softmax(result_dict["logits"])
        # preds = torch.argmax(scores, dim=1)
        # scores = scores[:, 1].data.cpu().numpy()

        return {
            "loss": loss_dict["loss"],
            "preds": result_dict["preds"],
            # "preds": preds.detach(),
            "targets": batch["m_label"].detach(),
            "scores": result_dict["scores"],
            # "scores": scores,
        }

    def training_step_end(self, training_step_outputs):
        # others: common, ensemble, multi-label
        train_acc = self.acc(training_step_outputs["preds"], training_step_outputs["targets"]).item()
        train_auroc = self.auroc(training_step_outputs["preds"], training_step_outputs["targets"]).item()

        self.log("train_acc", train_acc, prog_bar=True, batch_size=self.batch_size)
        self.log("train_auroc", train_auroc, prog_bar=True, batch_size=self.batch_size)

    def validation_step_end(self, validation_step_outputs):
        # others: common, ensemble, multi-label
        val_acc = self.acc(validation_step_outputs["preds"], validation_step_outputs["targets"]).item()
        val_auroc = self.auroc(validation_step_outputs["preds"], validation_step_outputs["targets"]).item()

        self.log("val_re", val_acc + val_auroc, prog_bar=True, batch_size=self.batch_size)
        self.log("val_acc", val_acc, prog_bar=True, batch_size=self.batch_size)
        self.log("val_auroc", val_auroc, prog_bar=True, batch_size=self.batch_size)

    def training_epoch_end(self, training_step_outputs):
        train_loss = Average([i["loss"] for i in training_step_outputs]).item()
        preds = [item for list in training_step_outputs for item in list["preds"]]
        targets = [item for list in training_step_outputs for item in list["targets"]]
        preds = torch.stack(preds, dim=0)
        targets = torch.stack(targets, dim=0)

        train_acc = self.acc(preds, targets).item()
        train_auroc = self.auroc(preds, targets).item()

        print("Train - loss:", train_loss, "acc: ", train_acc, "auroc: ", train_auroc)

    def validation_epoch_end(self, validation_step_outputs):
        valid_loss = Average([i["loss"] for i in validation_step_outputs]).item()
        preds = [item for list in validation_step_outputs for item in list["preds"]]
        targets = [item for list in validation_step_outputs for item in list["targets"]]
        scores = [item for list in validation_step_outputs for item in list["scores"]]

        preds = torch.stack(preds, dim=0)
        targets = torch.stack(targets, dim=0)

        scores = np.stack(scores, axis=0)
        numpy_targets = targets.cpu().numpy()

        val_eer = compute_eer(scores[numpy_targets == 1], scores[numpy_targets == 0])[0]

        self.log("val_eer", val_eer, prog_bar=True, batch_size=self.batch_size)

        # if valid_loss <= self.best_loss:
        self.best_acc = self.acc(preds, targets).item()
        self.best_auroc = self.auroc(preds, targets).item()
        self.best_real_f1score = self.f1score(preds, targets).item()
        self.best_real_recall = self.recall(preds, targets).item()
        self.best_real_precision = self.precisions(preds, targets).item()

        self.best_fake_f1score = self.f1score(Opposite(preds), Opposite(targets)).item()
        self.best_fake_recall = self.recall(Opposite(preds), Opposite(targets)).item()
        self.best_fake_precision = self.precisions(Opposite(preds), Opposite(targets)).item()

        self.best_loss = valid_loss
        print(
            "Valid loss: ",
            self.best_loss,
            "acc: ",
            self.best_acc,
            "auroc: ",
            self.best_auroc,
            "real_f1score:",
            self.best_real_f1score,
            "real_recall: ",
            self.best_real_recall,
            "real_precision: ",
            self.best_real_precision,
            "fake_f1score: ",
            self.best_fake_f1score,
            "fake_recall: ",
            self.best_fake_recall,
            "fake_precision: ",
            self.best_fake_precision,
        )

    def test_epoch_end(self, test_step_outputs):
        test_loss = Average([i["loss"] for i in test_step_outputs]).item()
        preds = [item for list in test_step_outputs for item in list["preds"]]
        targets = [item for list in test_step_outputs for item in list["targets"]]
        scores = [item for list in test_step_outputs for item in list["scores"]]

        preds = torch.stack(preds, dim=0)
        targets = torch.stack(targets, dim=0)

        scores = np.stack(scores, axis=0)
        numpy_targets = targets.cpu().numpy()

        test_eer = compute_eer(scores[numpy_targets == 1], scores[numpy_targets == 0])[0]

        test_acc = self.acc(preds, targets).item()
        test_auroc = self.auroc(preds, targets).item()
        test_real_f1score = self.f1score(preds, targets).item()
        test_real_recall = self.recall(preds, targets).item()
        test_real_precision = self.precisions(preds, targets).item()

        test_fake_f1score = self.f1score(Opposite(preds), Opposite(targets)).item()
        test_fake_recall = self.recall(Opposite(preds), Opposite(targets)).item()
        test_fake_precision = self.precisions(Opposite(preds), Opposite(targets)).item()

        self.log("test_eer", test_eer, batch_size=self.batch_size)
        self.log("test_acc", test_acc, batch_size=self.batch_size)
        self.log("test_auroc", test_auroc, batch_size=self.batch_size)
        self.log("test_real_f1score", test_real_f1score, batch_size=self.batch_size)
        self.log("test_real_recall", test_real_recall, batch_size=self.batch_size)
        self.log("test_real_precision", test_real_precision, batch_size=self.batch_size)
        self.log("test_fake_f1score", test_fake_f1score, batch_size=self.batch_size)
        self.log("test_fake_recall", test_fake_recall, batch_size=self.batch_size)
        self.log("test_fake_precision", test_fake_precision, batch_size=self.batch_size)
        return {
            "loss": test_loss,
            "test_acc": test_acc,
            "auroc": test_auroc,
            "real_f1score": test_real_f1score,
            "real_recall": test_real_recall,
            "real_precision": test_real_precision,
            "fake_f1score": test_fake_f1score,
            "fake_recall": test_fake_recall,
            "fake_precision": test_fake_precision,
        }

    def configure_optimizers(self):

        optimizer = Adam(
            self.parameters(), lr=self.learning_rate, betas=(0.5, 0.9), weight_decay=self.weight_decay
        )
        return {
            "optimizer": optimizer,
            "lr_scheduler": {
                "scheduler": ReduceLROnPlateau(optimizer, factor=0.5, patience=3, verbose=True, min_lr=1e-8),
                "monitor": "val_loss",
            },
        }
