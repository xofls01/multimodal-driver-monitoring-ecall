"""사전학습 PaPaGei-S로 PPG 특징을 뽑고, 그 위에 학습한 판정부로 등급을 낸다.

모델 구조(MyConv1dPadSame ~ ResNet1DMoE)는 Nokia Bell Labs의 PaPaGei 저장소
models/resnet.py에서 가져왔다. 라이선스는 third_party/PAPAGEI_LICENSE를 본다.
https://github.com/Nokia-Bell-Labs/papagei-foundation-model
가중치 출처: https://zenodo.org/records/13983110 (papagei_s.pt)
"""
# © 2024 Nokia
# Licensed under the BSD 3 Clause Clear License
# SPDX-License-Identifier: BSD-3-Clause-Clear
from __future__ import annotations

import numpy as np
import torch
import torch.nn.functional as F
from scipy import signal
from torch import nn

from .dsp import MODEL_FS, MODEL_SAMPLES, moving_average
from .hr_model import CLASSES, ModelOutput

EMBED_DIM = 512


def ppg_model_input(raw: np.ndarray, fs: float) -> np.ndarray:
    """PPG 원신호 10초 → PaPaGei 입력.

    공개 예제의 z-score → 0.5–12 Hz Chebyshev 필터 → 50 ms 평활 → 125 Hz 재샘플을 scipy로 옮겼다.
    원본은 pyPPG 패키지를 쓰므로 완전히 같다고 보장하지는 않는다.
    """
    x = np.asarray(raw, dtype=float)
    std = x.std()
    if std < 1e-9:
        return np.zeros(MODEL_SAMPLES, dtype=np.float32)
    x = (x - x.mean()) / std
    hi = min(12.0, 0.45 * fs)
    sos = signal.cheby2(4, 20, [0.5, hi], btype="bandpass", fs=fs, output="sos")
    y = signal.sosfiltfilt(sos, x, padlen=min(len(x) - 1, 200))
    y = moving_average(y, max(1, int(round(0.05 * fs))))
    t_out = np.arange(MODEL_SAMPLES) / MODEL_FS
    return np.interp(t_out, np.arange(len(y)) / fs, y).astype(np.float32)


class MyConv1dPadSame(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size, stride, groups=1):
        super().__init__()
        self.kernel_size = kernel_size
        self.stride = stride
        self.conv = nn.Conv1d(in_channels, out_channels, kernel_size, stride=stride, groups=groups)

    def forward(self, x):
        in_dim = x.shape[-1]
        out_dim = (in_dim + self.stride - 1) // self.stride
        p = max(0, (out_dim - 1) * self.stride + self.kernel_size - in_dim)
        return self.conv(F.pad(x, (p // 2, p - p // 2), "constant", 0))


class MyMaxPool1dPadSame(nn.Module):
    def __init__(self, kernel_size):
        super().__init__()
        self.kernel_size = kernel_size
        self.max_pool = nn.MaxPool1d(kernel_size=kernel_size)

    def forward(self, x):
        p = max(0, self.kernel_size - 1)
        return self.max_pool(F.pad(x, (p // 2, p - p // 2), "constant", 0))


class BasicBlock(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size, stride, groups, downsample, is_first_block=False):
        super().__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.downsample = downsample
        self.stride = stride if downsample else 1
        self.is_first_block = is_first_block
        self.bn1 = nn.BatchNorm1d(in_channels)
        self.relu1 = nn.ReLU()
        self.do1 = nn.Dropout(p=0.5)
        self.conv1 = MyConv1dPadSame(in_channels, out_channels, kernel_size, self.stride, groups)
        self.bn2 = nn.BatchNorm1d(out_channels)
        self.relu2 = nn.ReLU()
        self.do2 = nn.Dropout(p=0.5)
        self.conv2 = MyConv1dPadSame(out_channels, out_channels, kernel_size, 1, groups)
        self.max_pool = MyMaxPool1dPadSame(self.stride)

    def forward(self, x):
        identity = x
        out = x
        if not self.is_first_block:
            out = self.do1(self.relu1(self.bn1(out)))
        out = self.conv1(out)
        out = self.conv2(self.do2(self.relu2(self.bn2(out))))
        if self.downsample:
            identity = self.max_pool(identity)
        if self.out_channels != self.in_channels:
            ch1 = (self.out_channels - self.in_channels) // 2
            ch2 = self.out_channels - self.in_channels - ch1
            identity = F.pad(identity.transpose(-1, -2), (ch1, ch2), "constant", 0).transpose(-1, -2)
        return out + identity


class ResNet1DMoE(nn.Module):
    """PaPaGei-S. 가중치를 그대로 불러오기 위해 학습용 전문가 층도 남겨 두고, 추론은 특징만 쓴다."""

    def __init__(self, in_channels=1, base_filters=32, kernel_size=3, stride=2, groups=1, n_block=18,
                 n_classes=EMBED_DIM, n_experts=3, downsample_gap=2, increasefilter_gap=4):
        super().__init__()
        self.first_block_conv = MyConv1dPadSame(in_channels, base_filters, kernel_size, 1)
        self.first_block_bn = nn.BatchNorm1d(base_filters)
        self.first_block_relu = nn.ReLU()
        out_channels = base_filters
        self.basicblock_list = nn.ModuleList()
        for i in range(n_block):
            first = i == 0
            downsample = i % downsample_gap == 1
            c_in = base_filters if first else int(base_filters * 2 ** ((i - 1) // increasefilter_gap))
            out_channels = c_in * 2 if (i % increasefilter_gap == 0 and i != 0) else c_in
            self.basicblock_list.append(
                BasicBlock(c_in, out_channels, kernel_size, stride, groups, downsample, first))
        self.final_bn = nn.BatchNorm1d(out_channels)
        self.final_relu = nn.ReLU(inplace=True)
        self.dense = nn.Linear(out_channels, n_classes)
        self.expert_layers_1 = nn.ModuleList([
            nn.Sequential(nn.Linear(out_channels, out_channels // 2), nn.ReLU(),
                          nn.Linear(out_channels // 2, 1)) for _ in range(n_experts)])
        self.gating_network_1 = nn.Sequential(nn.Linear(out_channels, n_experts), nn.Softmax(dim=1))
        self.expert_layers_2 = nn.ModuleList([
            nn.Sequential(nn.Linear(out_channels, out_channels // 2), nn.ReLU(), nn.Dropout(0.3),
                          nn.Linear(out_channels // 2, 1)) for _ in range(n_experts)])
        self.gating_network_2 = nn.Sequential(nn.Linear(out_channels, n_experts), nn.Softmax(dim=1))

    def forward(self, x):
        out = self.first_block_relu(self.first_block_bn(self.first_block_conv(x)))
        for block in self.basicblock_list:
            out = block(out)
        out = self.final_relu(self.final_bn(out)).mean(-1)
        return self.dense(out)


def load_encoder(weights_path: str) -> ResNet1DMoE:
    """가중치만 불러온다(weights_only). 임의 코드를 실행할 수 있는 pickle 로드는 쓰지 않는다."""
    state = torch.load(weights_path, map_location="cpu", weights_only=True)
    state = {(k[7:] if k.startswith("module.") else k): v for k, v in state.items()}
    net = ResNet1DMoE()
    net.load_state_dict(state)
    net.eval()
    for p in net.parameters():
        p.requires_grad_(False)
    return net


def make_head(n_classes: int = len(CLASSES)) -> nn.Module:
    return nn.Sequential(nn.Linear(EMBED_DIM, 128), nn.ReLU(), nn.Dropout(0.3), nn.Linear(128, n_classes))


class PPGClassifier:
    def __init__(self, encoder_path: str, head_path: str):
        self.encoder = load_encoder(encoder_path)
        ckpt = torch.load(head_path, map_location="cpu", weights_only=True)
        self.head = make_head()
        self.head.load_state_dict(ckpt["state_dict"])
        self.head.eval()
        self.temperature = float(ckpt.get("temperature", 1.0))
        self.mean = ckpt["mean"]
        self.std = ckpt["std"]

    @torch.inference_mode()
    def predict(self, raw: np.ndarray, fs: float) -> ModelOutput:
        x = torch.from_numpy(ppg_model_input(raw, fs))[None, None]
        z = (self.encoder(x) - self.mean) / self.std
        probs = torch.softmax(self.head(z)[0] / self.temperature, dim=0).numpy()
        i = int(np.argmax(probs))
        return ModelOutput(CLASSES[i], float(probs[i]), None,
                           {c: float(p) for c, p in zip(CLASSES, probs)})
