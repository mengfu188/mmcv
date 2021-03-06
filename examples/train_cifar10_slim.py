import logging
import os
from argparse import ArgumentParser
from collections import OrderedDict

import examples_.resnet_cifar as resnet_cifar
import torch
import torch.distributed as dist
import torch.multiprocessing as mp
import torch.nn.functional as F
from torch.nn.parallel import DataParallel, DistributedDataParallel
from torch.utils.data import DataLoader
from torch.utils.data.distributed import DistributedSampler
from torchvision import datasets, transforms

from mmcv import Config
from mmcv.runner import DistSamplerSeedHook, Runner

def accuracy(output, target, topk=(1, )):
    with torch.no_grad():
        maxk = max(topk)
        batch_size = target.size(0)

        _, pred = output.topk(maxk, 1, True, True)
        pred = pred.t()
        correct = pred.eq(target.view(1, -1).expand_as(pred))

        res = []
        for k in topk:
            correct_k = correct[:k].view(-1).float().sum(0, keepdim=True)
            res.append(correct_k.mul_(100 / batch_size))
        return res

def batch_processor(model, data, train_mode):
    img, label = data
    label = label.cuda(non_blocking=True)
    pred = model(img)
    loss = F.cross_entropy(pred, label)
    acc_top1, acc_top5 = accuracy(pred, label, topk=(1, 5))
    log_vars = OrderedDict()
    log_vars['loss'] = loss.item()
    log_vars['acc_top1'] = acc_top1.item()
    log_vars['acc_top5'] = acc_top5.item()
    outputs = dict(loss=loss, log_vars=log_vars, num_samples = img.size(0))
    return outputs

def get_logger(log_level):
    from logzero import logger
    return logger

def parse_args():
    parser = ArgumentParser(description='Train CIFAR-10 classification')
    parser.add_argument('--config', help='train config file path',
                        default='examples_/config_cifar10.py')
    parser.add_argument(
        '--launcher',
        choices=['none', 'pytorch'],
        default='none',
        help='job launcher')
    parser.add_argument('--local_rank', type=int, default=0)
    return parser.parse_args()

def main():
    # 1. 解析配置信息
    args = parse_args()
    cfg = Config.fromfile(args.config)
    logger = get_logger(cfg.log_level)

    logger.info('Disabled distributed training.')

    # 1. 处理数据来源
    normalize = transforms.Normalize(mean=cfg.mean, std=cfg.std)
    train_dataset = datasets.CIFAR10(
        root=cfg.data_root,
        train=True,
        transform=transforms.Compose([
            transforms.RandomCrop(32, padding=4),
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            normalize,
        ]))
    val_dataset = datasets.CIFAR10(
        root=cfg.data_root,
        train=False,
        transform=transforms.Compose([
            transforms.ToTensor(),
            normalize,
        ]))

    num_workers = cfg.data_workers * len(cfg.gpus)
    batch_size = cfg.batch_size
    train_sampler = None
    val_sampler = None
    shuffle = True

    train_loader = DataLoader(
        train_dataset,
        batch_size=batch_size,
        shuffle=shuffle,
        sampler=train_sampler,
        num_workers=num_workers)
    val_loader = DataLoader(
        val_dataset,
        batch_size=batch_size,
        shuffle=False,
        sampler=val_sampler,
        num_workers=num_workers)

    # 3. 处理模型
    model = getattr(resnet_cifar, cfg.model)()
    model = DataParallel(model, device_ids=cfg.gpus).cuda()

    # 4. 训练配置,
    runner = Runner(
        model,
        batch_processor,
        cfg.optimizer,
        cfg.work_dir,
        logger=get_logger(cfg.log_level)
    )
    runner.register_training_hooks(
        lr_config=cfg.lr_config,
        optimizer_config=cfg.optimizer_config,
        checkpoint_config=cfg.checkpoint_config,
        log_config=cfg.log_config
    )

    # 5. load param (if necessary) and run
    if cfg.get('resume_from') is not None:
        runner.resume(cfg.resume_from)
    elif cfg.get('load_from') is not None:
        runner.load_checkpoint(cfg.load_from)

    runner.run([train_loader, val_loader], cfg.workflow, cfg.total_epochs)

if __name__ == '__main__':
    main()