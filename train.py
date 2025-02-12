
import os
import json
import glob
import argparse
from typing import Optional
from torch.utils.data import DataLoader
from hifigan.light.hifigan import HifiGAN

from hifigan.data.collate import MelCollate

import lightning.pytorch as pl
from lightning.pytorch.callbacks import ModelCheckpoint
import lightning_fabric

from hifigan.hparams import HParams
from hifigan.data.dataset import MelDataset

def get_hparams(config_path: str) -> HParams:
    with open(config_path, "r") as f:
        data = f.read()
    config = json.loads(data)
    
    hparams = HParams(**config)
    return hparams

def last_checkpoint(path: str) -> Optional[str]:
    ckpt_path = None
    if os.path.exists(os.path.join(path, "lightning_logs")):
        versions = glob.glob(os.path.join(path, "lightning_logs", "version_*"))
        if len(list(versions)) > 0:
            last_ver = sorted(list(versions), key=lambda p: int(p.split("_")[-1]))[-1]
            last_ckpt = os.path.join(last_ver, "checkpoints/last.ckpt")
            if os.path.exists(last_ckpt):
                ckpt_path = last_ckpt
    return ckpt_path

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config', type=str, default="./configs/48k.json", help='JSON file for configuration')
    parser.add_argument('-a', '--accelerator', type=str, default="cpu", help='training device')
    parser.add_argument('-d', '--device', type=str, default="1", help='training device ids')
    parser.add_argument('-n', '--num-nodes', type=int, default=1, help='training node number')
    args = parser.parse_args()

    hparams = get_hparams(args.config)
    lightning_fabric.utilities.seed.seed_everything(hparams.train.seed)

    devices = [int(n.strip()) for n in args.device.split(",")]

    checkpoint_callback = ModelCheckpoint(
        dirpath=None, save_last=True, every_n_train_steps=2000, save_weights_only=False,
        monitor="valid/loss_mel_epoch", mode="min", save_top_k=5
    )

    trainer_params = {
        "accelerator": args.accelerator,
        "callbacks": [checkpoint_callback],
    }

    if args.accelerator != "cpu":
        trainer_params["devices"] = devices

    if len(devices) > 1:
        trainer_params["strategy"] = "ddp"

    trainer_params.update(hparams.trainer)

    if hparams.train.fp16_run:
        print("using fp16")
        trainer_params["precision"] = "16-mixed"
    elif hparams.train.bp16_run:
        print("using bf16")
        trainer_params["precision"] = "bf16-mixed"
    
    trainer_params["num_nodes"] = args.num_nodes

    # data
    train_dataset = MelDataset(hparams.data.training_files, hparams.data)
    valid_dataset = MelDataset(hparams.data.validation_files, hparams.data)

    collate_fn = MelCollate()

    if "strategy" in trainer_params and trainer_params["strategy"] == "ddp":
        batch_per_gpu = hparams.train.batch_size // len(devices)
    else:
        batch_per_gpu = hparams.train.batch_size
    train_loader = DataLoader(train_dataset, batch_size=batch_per_gpu, num_workers=8, shuffle=True, pin_memory=True,
                              collate_fn=collate_fn)
    valid_loader = DataLoader(valid_dataset, batch_size=batch_per_gpu, num_workers=8, shuffle=False, pin_memory=True,
                              collate_fn=collate_fn)

    # model
    model = HifiGAN(**hparams)
    print(sum(p.numel() for p in model.net_g.parameters() if p.requires_grad))
    trainer = pl.Trainer(**trainer_params)
    # resume training
    ckpt_path = last_checkpoint(hparams.trainer.default_root_dir)
    trainer.fit(model=model, train_dataloaders=train_loader, val_dataloaders=valid_loader, ckpt_path=ckpt_path)

if __name__ == "__main__":
  main()
