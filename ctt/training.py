import os
from collections import defaultdict
from addict import Dict

import numpy as np
import torch
import torch.nn as nn
import torch.jit

from speedrun import (
    BaseExperiment,
    IOMixin,
    TensorboardMixin,
    register_default_dispatch,
)
from speedrun.logging.wandb import WandBSweepMixin, SweepRunner

import ctt.models.transformer as tr
from ctt.data_loading.loader import get_dataloader
from ctt.losses import WeightedSum
from ctt.utils import to_device, momentum_accumulator
from ctt.metrics import Metrics
from ctt import opts
from ctt.data_loading.transforms import get_transforms


class CTTTrainer(TensorboardMixin, WandBSweepMixin, IOMixin, BaseExperiment):
    WANDB_PROJECT = "ctt"

    def __init__(self):
        super(CTTTrainer, self).__init__()
        self.auto_setup()
        self._build()
        self._dummy_sample = None  # kept for repeated tracing only

    def _build(self):
        self._build_loaders()
        self._build_model()
        self._build_criteria_and_optim()
        self._build_scheduler()

    def _build_model(self):
        model_cls = getattr(tr, self.get("model/name", "ContactTracingTransformer"))
        self.model: nn.Module = to_device(
            model_cls(**self.get("model/kwargs", {})), self.device
        )

    def _build_train_loader(self):
        train_path = self.get("data/paths/train", ensure_exists=True)
        train_transforms = get_transforms(self.get("data/transforms/train", {}))
        self.train_loader = get_dataloader(
            path=train_path,
            transforms=train_transforms,
            rng=np.random.RandomState(self.epoch),
            **self.get("data/loader_kwargs", ensure_exists=True),
        )

    def _build_validate_loader(self):
        validate_path = self.get("data/paths/validate", ensure_exists=True)
        validate_transforms = get_transforms(self.get("data/transforms/validate", {}))
        self.validate_loader = get_dataloader(
            path=validate_path,
            transforms=validate_transforms,
            rng=np.random.RandomState(self.epoch),
            **self.get("data/loader_kwargs", ensure_exists=True),
        )

    def _build_loaders(self):
        self._build_train_loader()
        self._build_validate_loader()

    def _build_criteria_and_optim(self):
        # noinspection PyArgumentList
        self.loss = WeightedSum.from_config(self.get("losses", ensure_exists=True))
        optim_cls = getattr(opts, self.get("optim/name", "Adam"))
        self.optim = optim_cls(self.model.parameters(), **self.get("optim/kwargs"))
        self.metrics = Metrics(**self.get("metrics/kwargs", {}))

    def _build_scheduler(self):
        # Set up an epoch-wise scheduler here if you want to, but the
        # recommendation is to use the one defined in opts.
        self.scheduler = None

    def refresh_loader_if_required(self):
        # Refresh only if we need to
        if self.get("data/loader_kwargs/num_datasets_to_select", None) is not None:
            del self.train_loader
            self._build_train_loader()

    @property
    def device(self):
        return self.get("device", "cuda" if torch.cuda.is_available() else "cpu")

    @register_default_dispatch
    def train(self):
        if self.get("wandb/use", True):
            self.initialize_wandb()
        for epoch in self.progress(
            range(self.get("training/num_epochs", ensure_exists=True)), tag="epochs"
        ):
            self.train_epoch()
            validation_stats = self.validate_epoch()
            self.checkpoint()
            self.log_progress("epochs", **validation_stats)
            self.step_scheduler(epoch)
            self.refresh_loader_if_required()
            self.next_epoch()

    def train_epoch(self):
        self.clear_moving_averages()
        self.model.train()
        for model_input in self.progress(self.train_loader, tag="train"):
            # Evaluate model
            model_input = to_device(model_input, self.device)
            model_output = Dict(self.model(model_input))
            # Compute loss
            losses = self.loss(model_input, model_output)
            loss = losses.loss
            self.optim.zero_grad()
            loss.backward()
            self.optim.step()
            # Log to wandb (if required)
            self.log_training_losses(losses)
            self.log_learning_rates()
            # Log to pbar
            self.accumulate_in_cache(
                "moving_loss", loss.item(), momentum_accumulator(0.9)
            )
            self.log_progress(
                "train", loss=self.read_from_cache("moving_loss"),
            )
            self.next_step()

    def validate_epoch(self):
        all_losses_and_metrics = defaultdict(list)
        self.metrics.reset()
        self.model.eval()
        for model_input in self.progress(self.validate_loader, tag="validation"):
            with torch.no_grad():
                model_input = to_device(model_input, self.device)
                model_output = Dict(self.model(model_input))
                losses = self.loss(model_input, model_output)
                self.metrics.update(model_input, model_output)
                all_losses_and_metrics["loss"].append(losses.loss.item())
                for key in losses.unweighted_losses:
                    all_losses_and_metrics[key].append(
                        losses.unweighted_losses[key].item()
                    )
        # Compute mean for all losses
        all_losses_and_metrics = Dict(
            {key: np.mean(val) for key, val in all_losses_and_metrics.items()}
        )
        all_losses_and_metrics.update(Dict(self.metrics.evaluate()))
        self.log_validation_losses_and_metrics(all_losses_and_metrics)
        early_stopping_metric = all_losses_and_metrics[
            self.get("training/checkpoint/early_stopping_metric", "loss")
        ]
        # Store the validation loss in cache. This will be used for checkpointing.
        self.write_to_cache("current_validation_metrics", all_losses_and_metrics)
        self.write_to_cache("current_validation_loss", all_losses_and_metrics.loss)
        self.write_to_cache("current_early_stopping_metric", early_stopping_metric)
        return all_losses_and_metrics

    def log_training_losses(self, losses):
        if self.log_wandb_now and self.get("wandb/use", False):
            metrics = Dict({"training_loss": losses.loss})
            metrics.update(
                {f"training_{k}": v for k, v in losses.unweighted_losses.items()}
            )
            self.wandb_log(**metrics)
        if self.log_scalars_now:
            for key, value in losses.unweighted_losses.items():
                self.log_scalar(f"training/{key}", value)
        return self

    def checkpoint(self, force=False):
        # Checkpoint as required
        if force or self.epoch % self.get("training/checkpoint/every", 1) == 0:
            self._write_checkpoint(self.checkpoint_path)
        if self.get("training/checkpoint/if_best", True):
            # Save a checkpoint if the validation loss is better than best
            self.checkpoint_if_best()
        return self

    def checkpoint_if_best(self):
        current_early_stopping_metric = self.read_from_cache(
            "current_early_stopping_metric", float("inf")
        )
        best_early_stopping_metric = self.read_from_cache(
            "best_early_stopping_metric", float("inf")
        )
        if current_early_stopping_metric < best_early_stopping_metric:
            self.write_to_cache(
                "best_early_stopping_metric", current_early_stopping_metric
            )
            ckpt_path = os.path.join(self.checkpoint_directory, "best.ckpt")
            self._write_checkpoint(ckpt_path)
        return self

    def _write_checkpoint(self, ckpt_path: str):
        as_trace_too = self.get("training/checkpoint/save_trace", False)
        if as_trace_too:
            if self._dummy_sample is None:
                # getting a single minibatch from the data loader might be pretty slow, but
                # we don't have a choice if we want to trace the model...
                # self._dummy_sample = next(iter(self.train_loader))
                with open("/tmp/batch.pkl", "rb") as fd:
                    import pickle

                    self._dummy_sample = pickle.load(fd)
            self.model.eval()
            with self.model.output_as_tuple():
                test_output = self.model(self._dummy_sample)
                trace = torch.jit.trace(self.model, (self._dummy_sample,),)
            trace.save(ckpt_path + ".trace")
        info_dict = {
            "model": self.model.state_dict(),
            "optim": self.optim.state_dict(),
        }
        torch.save(info_dict, ckpt_path)

    def load(self, device=None):
        ckpt_path = os.path.join(self.checkpoint_directory, "best.ckpt")
        if not os.path.exists(ckpt_path):
            raise FileNotFoundError
        info_dict = torch.load(
            ckpt_path,
            map_location=torch.device((self.device if device is None else device)),
        )
        assert isinstance(
            info_dict, (dict, Dict)
        ), "checkpoint did not contain object map; are you trying to reload a trace?"
        self.model.load_state_dict(info_dict["model"])
        self.optim.load_state_dict(info_dict["optim"])
        return self

    def log_validation_losses_and_metrics(self, losses):
        if self.get("wandb/use", False):
            metrics = {f"validation_{k}": v for k, v in losses.items()}
            self.wandb_log(**metrics)
        for key, value in losses.items():
            self.log_scalar(f"validation/{key}", value)
        return self

    def clear_moving_averages(self):
        return self.clear_in_cache("moving_loss")

    def step_scheduler(self, epoch):
        if self.scheduler is not None:
            self.scheduler.step(epoch)
        return self

    def log_learning_rates(self):
        lrs = {
            f"lr_{i}": param_group["lr"]
            for i, param_group in enumerate(self.optim.param_groups)
        }
        if self.get("wandb/use", False):
            self.wandb_log(**lrs)
        for key, value in lrs.items():
            self.log_scalar(f"training/{key}", value)
        return self


if __name__ == "__main__":
    SweepRunner(CTTTrainer).run()
