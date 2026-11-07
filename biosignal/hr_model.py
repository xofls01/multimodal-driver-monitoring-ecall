"""ECG 10초 창에서 심박수와 등급을 함께 추론하는 1D-CNN.

YOLO가 이미지에서 '클래스 + 신뢰도'를 내듯, 이 모델은 ECG 파형에서
'등급(normal/caution/danger/unavailable) + 신뢰도 + 심박수'를 낸다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

import numpy as np
import torch
from torch import nn

from .dsp import MODEL_SAMPLES, model_input

CLASSES = ("normal", "caution", "danger", "unavailable")
HR_SCALE = 100.0


class _ResBlock(nn.Module):
    def __init__(self, c_in: int, c_out: int, stride: int = 2):
        super().__init__()
        self.body = nn.Sequential(
            nn.Conv1d(c_in, c_out, 9, stride=stride, padding=4, bias=False),
            nn.BatchNorm1d(c_out),
            nn.ReLU(inplace=True),
            nn.Conv1d(c_out, c_out, 9, padding=4, bias=False),
            nn.BatchNorm1d(c_out),
        )
        self.skip = nn.Sequential(
            nn.Conv1d(c_in, c_out, 1, stride=stride, bias=False),
            nn.BatchNorm1d(c_out),
        )
        self.act = nn.ReLU(inplace=True)

    def forward(self, x):
        return self.act(self.body(x) + self.skip(x))


class HeartNet(nn.Module):
    def __init__(self, n_classes: int = len(CLASSES)):
        super().__init__()
        self.features = nn.Sequential(
            nn.Conv1d(1, 16, 15, stride=2, padding=7, bias=False),
            nn.BatchNorm1d(16),
            nn.ReLU(inplace=True),
            _ResBlock(16, 24),
            _ResBlock(24, 32),
            _ResBlock(32, 48),
            _ResBlock(48, 64),
            _ResBlock(64, 64, stride=1),
        )
        self.dropout = nn.Dropout(0.2)
        self.cls_head = nn.Linear(128, n_classes)
        self.hr_head = nn.Linear(128, 1)

    def forward(self, x):
        z = self.features(x)
        z = torch.cat([z.mean(dim=-1), z.amax(dim=-1)], dim=1)
        z = self.dropout(z)
        return self.cls_head(z), self.hr_head(z).squeeze(-1)


@dataclass
class ModelOutput:
    label: str
    confidence: float
    hr: Optional[float]
    probs: Dict[str, float]


class HeartRateClassifier:
    """학습된 가중치를 불러와 실시간 추론한다. CPU 기준."""

    def __init__(self, weights_path: str):
        ckpt = torch.load(weights_path, map_location="cpu", weights_only=True)
        self.net = HeartNet()
        self.net.load_state_dict(ckpt["state_dict"])
        self.net.eval()
        self.temperature = float(ckpt.get("temperature", 1.0))

    @torch.inference_mode()
    def predict(self, raw: np.ndarray, fs: float, mains_hz: float = 60.0) -> ModelOutput:
        """raw: ECG 원신호 10초."""
        x = model_input(raw, fs, mains_hz)
        return self.predict_prepared(x)

    @torch.inference_mode()
    def predict_prepared(self, x: np.ndarray) -> ModelOutput:
        if x.shape != (MODEL_SAMPLES,):
            raise ValueError(f"입력 길이는 {MODEL_SAMPLES}이어야 합니다: {x.shape}")
        logits, hr = self.net(torch.from_numpy(x)[None, None])
        probs = torch.softmax(logits[0] / self.temperature, dim=0).numpy()
        i = int(np.argmax(probs))
        hr_value = float(hr[0]) * HR_SCALE
        return ModelOutput(
            label=CLASSES[i],
            confidence=float(probs[i]),
            hr=None if CLASSES[i] == "unavailable" else hr_value,
            probs={c: float(p) for c, p in zip(CLASSES, probs)},
        )
