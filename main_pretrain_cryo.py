import argparse
import datetime
import numpy as np
import time
import json
import os
from pathlib import Path

import torch
import torch.backends.cudnn as cudnn
import torchvision.transforms as transforms
import torchvision.datasets as datasets

import zarr
from PIL import Image
from torch.nn.functional import interpolate
from torch.utils.data import Dataset, DataLoader

import timm

# assert timm.__version__ == "0.3.2"  # version check
import timm.optim.optim_factory as optim_factory

from engine_pretrain import train_one_epoch_cryo  # 改成cryo版
import models.fcmae as fcmae

import utils
from utils import NativeScalerWithGradNormCount as NativeScaler
from utils import str2bool


class Resize3D:
    def __init__(self, target_size):
        self.target_size = target_size  # 目标大小，tuple形式，如 (224, 224)

    def __call__(self, sample):
        # 假设sample是一个3D numpy数组，形状是 (200, 630, 630)
        depth, height, width = sample.shape
        target_depth, target_height, target_width = self.target_size

        # 对height和width进行resize
        sample_resized = np.zeros((depth, target_height, target_width), dtype=sample.dtype)

        for i in range(depth):
            # 对每个深度切片应用resize
            sample_resized[i] = interpolate(torch.tensor(sample[i]).unsqueeze(0).unsqueeze(0).float(), # (1, 1, height, width)
                                            size=(target_height, target_width),
                                            mode='bilinear',
                                            align_corners=False).squeeze(0).squeeze(0).numpy()
        return sample_resized

class ZarrDataset(Dataset):
    def __init__(self, root_dir, transform=None):
        self.root_dir = root_dir
        self.transform = transform
        self.folders = []  # 存放所有folders的路径

        # 遍历所有的folders，例如TS_0, TS_1, ...
        for subdir in os.listdir(root_dir):
            folder_path = os.path.join(root_dir, subdir)
            # 确保它是一个folders，但不能是Images（里面没有需要的zarr文件）
            if os.path.isdir(folder_path) and (subdir != "Images"):
                self.folders.append(folder_path)

    def __len__(self):
        # 返回数据集的大小（即folders的数量）
        return len(self.folders)

    def __getitem__(self, idx):
        folder_path = self.folders[idx]
        zarr_file_path = os.path.join(folder_path, 'Reconstructions', 'VoxelSpacing10.000', 'Tomograms', '100',
                                      f'{os.path.basename(folder_path)}.zarr')     
        zarr_file = zarr.open(zarr_file_path, mode='r')   # 构建zarr文件的路径并打开Zarr文件
        dataset = zarr_file['0'][:]    # 假设要访问数据集中的第一个数据集（例如"0"）
        if self.transform: 
            sample = self.transform(dataset)
        return sample

def get_args_parser():
    parser = argparse.ArgumentParser('FCMAE pre-training', add_help=False)
    parser.add_argument('--batch_size', default=64, type=int,
                        help='Per GPU batch size')
    parser.add_argument('--epochs', default=800, type=int)
    parser.add_argument('--warmup_epochs', type=int, default=40, metavar='N',
                        help='epochs to warmup LR')
    parser.add_argument('--update_freq', default=1, type=int,
                        help='gradient accumulation step')

    # Model parameters
    parser.add_argument('--model', default='convnextv2_base', type=str, metavar='MODEL',
                        help='Name of model to train')
    parser.add_argument('--input_size', default=224, type=int,
                        help='image input size')
    parser.add_argument('--mask_ratio', default=0.6, type=float,
                        help='Masking ratio (percentage of removed patches).')
    parser.add_argument('--norm_pix_loss', action='store_true',
                        help='Use (per-patch) normalized pixels as targets for computing loss')
    parser.set_defaults(norm_pix_loss=True)
    parser.add_argument('--decoder_depth', type=int, default=1)
    parser.add_argument('--decoder_embed_dim', type=int, default=512)

    # Optimizer parameters
    parser.add_argument('--weight_decay', type=float, default=0.05,
                        help='weight decay (default: 0.05)')
    parser.add_argument('--lr', type=float, default=None, metavar='LR',
                        help='learning rate (absolute lr)')
    parser.add_argument('--blr', type=float, default=1.5e-4, metavar='LR',
                        help='base learning rate: absolute_lr = base_lr * total_batch_size / 256')
    parser.add_argument('--min_lr', type=float, default=0., metavar='LR',
                        help='lower lr bound for cyclic schedulers that hit 0')

    # Dataset parameters
    parser.add_argument('--data_path', default='/datasets01/imagenet_full_size/061417/', type=str,
                        help='dataset path')
    parser.add_argument('--output_dir', default='',
                        help='path where to save, empty for no saving')
    parser.add_argument('--log_dir', default=None,
                        help='path where to tensorboard log')
    parser.add_argument('--device', default='cuda',
                        help='device to use for training / testing')
    parser.add_argument('--seed', default=0, type=int)
    parser.add_argument('--resume', default='',
                        help='resume from checkpoint')

    parser.add_argument('--auto_resume', type=str2bool, default=True)
    parser.add_argument('--save_ckpt', type=str2bool, default=True)
    parser.add_argument('--save_ckpt_freq', default=1, type=int)
    parser.add_argument('--save_ckpt_num', default=3, type=int)

    parser.add_argument('--start_epoch', default=0, type=int, metavar='N',
                        help='start epoch')
    parser.add_argument('--num_workers', default=10, type=int)
    parser.add_argument('--pin_mem', type=str2bool, default=True,
                        help='Pin CPU memory in DataLoader for more efficient (sometimes) transfer to GPU.')

    # Evaluation parameters
    parser.add_argument('--crop_pct', type=float, default=None)

    # distributed training parameters
    parser.add_argument('--world_size', default=1, type=int,
                        help='number of distributed processes')
    parser.add_argument('--local_rank', default=-1, type=int)
    parser.add_argument('--dist_on_itp', type=str2bool, default=False)
    parser.add_argument('--dist_url', default='env://',
                        help='url used to set up distributed training')
    return parser


def main(args):
    utils.init_distributed_mode(args)

    print(args)
    device = torch.device(args.device)

    # fix the seed for reproducibility
    seed = args.seed + utils.get_rank()
    torch.manual_seed(seed)
    np.random.seed(seed)

    cudnn.benchmark = True

    # simple augmentation
    # transform_train = transforms.Compose([
    #     transforms.RandomResizedCrop(args.input_size, scale=(0.2, 1.0), interpolation=3),  # 3 is bicubic
    #     transforms.RandomHorizontalFlip(),
    #     transforms.ToTensor(),
    #     #transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
    #     ])
    # dataset_train = datasets.ImageFolder(os.path.join(args.data_path, 'train'), transform=transform_train)
    
    transform_train = transforms.Compose([
        Resize3D((200, args.input_size, args.input_size))  # 默认目标大小 (224, 224)
    ])

    dataset_train = ZarrDataset(root_dir=args.data_path, transform=transform_train)
    print(dataset_train)

    num_tasks = utils.get_world_size()
    global_rank = utils.get_rank()
    if num_tasks > 1: # 添加DistributedSampler的应用条件
        sampler_train = torch.utils.data.DistributedSampler(
            dataset_train, num_replicas=num_tasks, rank=global_rank, shuffle=True, seed=args.seed
        )
    else:  # 否则使用SequentialSampler
        sampler_train = torch.utils.data.SequentialSampler(dataset_train)

    print("Sampler_train = %s" % str(sampler_train))

    if global_rank == 0 and args.log_dir is not None:
        os.makedirs(args.log_dir, exist_ok=True)
        # log_writer = SummaryWriter(log_dir=args.log_dir)
        log_writer = utils.TensorboardLogger(log_dir=args.log_dir)
    else:
        log_writer = None

    data_loader_train = torch.utils.data.DataLoader(
        dataset_train, sampler=sampler_train,
        batch_size=args.batch_size,
        # num_workers=args.num_workers,
        # pin_memory=args.pin_mem,
        num_workers=0, # 节省空间
        pin_memory=False, 
        drop_last=False,  # 一开始是False，但是总共len就不多
    )
    print(f"DataLoader length: {len(data_loader_train)}")  # 检查dataloader长度
    
    # define the model
    model = fcmae.__dict__[args.model](
        in_chans=200,  # 添加默认200
        mask_ratio=args.mask_ratio,
        decoder_depth=args.decoder_depth,
        decoder_embed_dim=args.decoder_embed_dim,
        norm_pix_loss=args.norm_pix_loss
    )
    model.to(device)

    model_without_ddp = model
    n_parameters = sum(p.numel() for p in model.parameters() if p.requires_grad)

    print("Model = %s" % str(model_without_ddp))
    print('number of params:', n_parameters)

    eff_batch_size = args.batch_size * args.update_freq * utils.get_world_size()
    num_training_steps_per_epoch = len(dataset_train) // eff_batch_size

    if args.lr is None:
        args.lr = args.blr * eff_batch_size / 256

    print("base lr: %.2e" % (args.lr * 256 / eff_batch_size))
    print("actual lr: %.2e" % args.lr)

    print("accumulate grad iterations: %d" % args.update_freq)
    print("effective batch size: %d" % eff_batch_size)

    if args.distributed:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        model.to(device)
        if torch.cuda.is_available():
            model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu], find_unused_parameters=True)
        else: 
            model = torch.nn.parallel.DistributedDataParallel(model)
        model_without_ddp = model.module

    param_groups = optim_factory.param_groups_weight_decay(model_without_ddp, args.weight_decay)
    optimizer = torch.optim.AdamW(param_groups, lr=args.lr, betas=(0.9, 0.95))
    print(optimizer)
    loss_scaler = NativeScaler()

    utils.auto_load_model(
        args=args, model=model, model_without_ddp=model_without_ddp,
        optimizer=optimizer, loss_scaler=loss_scaler)

    print(f"Start training for {args.epochs} epochs")
    start_time = time.time()
    for epoch in range(args.start_epoch, args.epochs):
        if args.distributed and isinstance(data_loader_train.sampler, torch.utils.data.DistributedSampler):
            data_loader_train.sampler.set_epoch(epoch)
        if log_writer is not None:
            log_writer.set_step(epoch * num_training_steps_per_epoch * args.update_freq)
        train_stats = train_one_epoch_cryo(  # 改成cryo版
            model, data_loader_train,
            optimizer, device, epoch, loss_scaler,
            log_writer=log_writer,
            args=args
        )
        if args.output_dir and args.save_ckpt:
            if (epoch + 1) % args.save_ckpt_freq == 0 or epoch + 1 == args.epochs:
                utils.save_model(
                    args=args, model=model, model_without_ddp=model_without_ddp, optimizer=optimizer,
                    loss_scaler=loss_scaler, epoch=epoch)
        log_stats = {**{f'train_{k}': v for k, v in train_stats.items()},
                     'epoch': epoch,
                     'n_parameters': n_parameters}
        if args.output_dir and utils.is_main_process():
            if log_writer is not None:
                log_writer.flush()
            with open(os.path.join(args.output_dir, "log.txt"), mode="a", encoding="utf-8") as f:
                f.write(json.dumps(log_stats) + "\n")

    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    print('Training time {}'.format(total_time_str))


if __name__ == '__main__':
    args = get_args_parser()
    args = args.parse_args()
    if args.output_dir:
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    main(args)
