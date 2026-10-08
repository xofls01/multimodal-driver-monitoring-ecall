"""핸들 영역(ROI) 정의 + 파지 판정.

핸들이 아직 없으면 책상 위 아무 물체(접시, 원형 판)나 가상 영역을 지정해서 테스트 가능.
- shape="ellipse": 핸들 림(ring). 안쪽 반경(inner) ~ 바깥(1+margin) 사이에 손이 있으면 파지
- shape="rect"   : 사각형 영역 안에 손이 있으면 파지 (핸들 없이 테스트할 때 편함)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np

from .geometry import HAND_KEYPOINTS


@dataclass
class WheelROI:
    shape: str = "ellipse"      # "ellipse" | "rect"
    cx: float = 0.0             # 정규화 좌표(0~1) - 해상도 바뀌어도 유지
    cy: float = 0.0
    ax: float = 0.0             # 반축(ellipse) 또는 반폭/반높이(rect)
    ay: float = 0.0

    # ---------- 저장/로드 ----------
    def save(self, path: str):
        Path(path).write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str) -> "WheelROI | None":
        p = Path(path)
        if not p.exists():
            return None
        return cls(**json.loads(p.read_text(encoding="utf-8")))

    @classmethod
    def from_drag(cls, p0, p1, w, h, shape="ellipse") -> "WheelROI":
        (x0, y0), (x1, y1) = p0, p1
        return cls(shape=shape,
                   cx=(x0 + x1) / 2 / w, cy=(y0 + y1) / 2 / h,
                   ax=abs(x1 - x0) / 2 / w, ay=abs(y1 - y0) / 2 / h)

    def px(self, w, h):
        return self.cx * w, self.cy * h, max(self.ax * w, 1.0), max(self.ay * h, 1.0)

    # ---------- 판정 ----------
    def point_mask(self, pts: np.ndarray, w: int, h: int, inner: float, margin: float) -> np.ndarray:
        cx, cy, ax, ay = self.px(w, h)
        dx = (pts[:, 0] - cx) / ax
        dy = (pts[:, 1] - cy) / ay
        if self.shape == "rect":
            lim = 1.0 + margin
            return (np.abs(dx) <= lim) & (np.abs(dy) <= lim)
        r = np.sqrt(dx * dx + dy * dy)
        return (r >= inner) & (r <= 1.0 + margin)

    def grip_ratio(self, hand_pts: np.ndarray, w: int, h: int, inner: float, margin: float) -> float:
        m = self.point_mask(hand_pts[HAND_KEYPOINTS], w, h, inner, margin)
        return float(m.mean())
