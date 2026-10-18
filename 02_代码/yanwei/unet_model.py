"""U-Net 病灶分割架构（支柱② 图像理解 · 训练与推理共用）。

架构源自候选仓库 kumawatvaibhav/diabetic_retinopathy_detection 的
`pipeline/correct_dataset.ipynb`（cell 6 / cell 22），逐层复刻以保证：
  - 能加载其现成 unet.pth 探路（state_dict key 完全一致）；
  - 也能用我们自己的数据自训（输出 5 通道病灶掩膜）。

输出 5 通道顺序（我们自训时显式按此组织，与推理 LESION_ORDER 对齐）：
  0 = MA（微动脉瘤）
  1 = HE（出血）
  2 = EX（硬性渗出）
  3 = SE（软性渗出）
  4 = OD（视盘）

注意：该架构依赖 PyTorch，仅在阶段 B 引入；上层通过懒加载保证未装 torch
时整条检索链路仍能降级到零依赖启发式。
"""

from __future__ import annotations

import torch
import torch.nn as nn


class DoubleConv(nn.Module):
    """两次 3×3 卷积 + BN + ReLU（U-Net 基本积木）。"""

    def __init__(self, in_ch, out_ch):
        super().__init__()
        self.conv = nn.Sequential(
            nn.Conv2d(in_ch, out_ch, 3, padding=1),
            nn.BatchNorm2d(out_ch),
            nn.ReLU(inplace=True),
            nn.Conv2d(out_ch, out_ch, 3, padding=1),
            nn.BatchNorm2d(out_ch),
            nn.ReLU(inplace=True),
        )

    def forward(self, x):
        return self.conv(x)


class UNet(nn.Module):
    """四层下采样 U-Net，输入 3 通道 RGB，输出 n_classes 通道 logits。

    与候选仓库完全一致：最大池下采样 + bilinear 上采样 + skip connection。
    """

    def __init__(self, n_classes=5):
        super().__init__()
        self.dconv_down1 = DoubleConv(3, 64)
        self.dconv_down2 = DoubleConv(64, 128)
        self.dconv_down3 = DoubleConv(128, 256)
        self.dconv_down4 = DoubleConv(256, 512)
        self.maxpool = nn.MaxPool2d(2)
        self.upsample = nn.Upsample(scale_factor=2, mode="bilinear", align_corners=True)
        self.dconv_up3 = DoubleConv(256 + 512, 256)
        self.dconv_up2 = DoubleConv(128 + 256, 128)
        self.dconv_up1 = DoubleConv(128 + 64, 64)
        self.conv_last = nn.Conv2d(64, n_classes, 1)

    def forward(self, x):
        conv1 = self.dconv_down1(x)
        x = self.maxpool(conv1)
        conv2 = self.dconv_down2(x)
        x = self.maxpool(conv2)
        conv3 = self.dconv_down3(x)
        x = self.maxpool(conv3)
        x = self.dconv_down4(x)
        x = self.upsample(x)
        x = torch.cat([x, conv3], dim=1)
        x = self.dconv_up3(x)
        x = self.upsample(x)
        x = torch.cat([x, conv2], dim=1)
        x = self.dconv_up2(x)
        x = self.upsample(x)
        x = torch.cat([x, conv1], dim=1)
        x = self.dconv_up1(x)
        return self.conv_last(x)


def build_unet(n_classes=5) -> UNet:
    """构建未加载权重的 U-Net。"""
    return UNet(n_classes=n_classes)


class WeightedBCEDiceLoss(nn.Module):
    """加权 BCE + Dice 损失（与候选仓库 cell 5 一致，bce_weight=0.7）。

    用 BCEWithLogitsLoss（内部自带 sigmoid），Dice 部分对 sigmoid 后逐类计算。
    """

    def __init__(self, bce_weight=0.7):
        super().__init__()
        self.bce = nn.BCEWithLogitsLoss()
        self.bce_weight = bce_weight

    def forward(self, preds, targets):
        bce = self.bce(preds, targets)

        smooth = 1e-6
        preds = torch.sigmoid(preds)
        intersection = (preds * targets).sum(dim=(2, 3))
        dice = (2.0 * intersection + smooth) / (
            preds.sum(dim=(2, 3)) + targets.sum(dim=(2, 3)) + smooth
        )
        dice_loss = 1 - dice.mean()

        return self.bce_weight * bce + (1 - self.bce_weight) * dice_loss
