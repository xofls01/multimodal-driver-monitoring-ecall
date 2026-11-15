"""실시간 화면: 카메라 + 비전 표시(원본 draw_overlay) + 생체신호는 BPM 숫자만.

원본 draw_multimodal 의 ECG/PPG 그래프, 채널 이름(ECG estimate 등), 신호 품질은 그리지 않는다.
BPM 값은 원본 select_display_bpm (ECG 우선, 없으면 PPG) 그대로 — 판정·MSD 와 같은 기준.
"""
from __future__ import annotations

import cv2
import numpy as np
from PIL import Image, ImageDraw

from driver_state_tools.bpm_display import bpm_range, select_display_bpm
from driver_state_tools.evidence_dashboard import PALETTE, font
from driver_state_tools.multimodal_dashboard import measurement_display

HEADER, FOOTER = 76, 96
WHITE, MUTED = (236, 243, 250), (151, 172, 193)
MEASURING_COLOR = (92, 199, 246)


def bpm_view(bio: dict) -> dict:
    """화면에 쓸 BPM 하나와 색. 측정 준비 중이면 그 안내."""
    display = measurement_display(bio)
    rate = select_display_bpm(bio)
    bpm = rate['bpm']
    if display['measuring']:
        return dict(text='--', color=MEASURING_COLOR, note=display['note'] or 'Measuring...', bpm=None)
    band = bpm_range(bpm)
    return dict(text=f'{bpm:.0f}' if bpm is not None else '--', color=PALETTE[band], note=None, bpm=bpm)


def draw_banner(frame: np.ndarray, title: str, sub: str = '') -> np.ndarray:
    """판단 전(워밍업) 화면: 카메라 영상 위에 안내만."""
    h, w = frame.shape[:2]
    im = Image.new('RGB', (w, h + HEADER), (9, 17, 26))
    im.paste(Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)), (0, HEADER))
    p = ImageDraw.Draw(im)
    p.text((20, 10), 'LIVE DRIVER MONITOR', font=font(12, True), fill=MUTED)
    p.text((20, 30), title, font=font(30, True), fill=MEASURING_COLOR)
    if sub:
        p.text((330, 38), sub, font=font(20, True), fill=MUTED)
    return cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)


def draw_live(vision_image: np.ndarray, combined, vision_decision, bio: dict, *, elapsed: float,
              fps: float | None = None, border: int = 8, notice: str | None = None) -> np.ndarray:
    scene = vision_image[border:-border, border:-border] if border else vision_image
    h, w = scene.shape[:2]
    im = Image.new('RGB', (w, h + HEADER + FOOTER), (9, 17, 26))
    im.paste(Image.fromarray(cv2.cvtColor(scene, cv2.COLOR_BGR2RGB)), (0, HEADER))
    p = ImageDraw.Draw(im)
    accent = PALETTE[combined.state.value]

    def text(s, x, y, size=14, color=WHITE, bold=False):
        p.text((x, y), str(s), font=font(size, bold), fill=color)

    display = measurement_display(bio)
    starting = (combined.state.value == 'unavailable' and display['measuring']
                and 'calibrating' in vision_decision.reasons)
    text('LIVE DRIVER MONITOR', 20, 10, 12, MUTED, True)
    text('PREPARING...' if starting else combined.state.value.upper(), 20, 30, 30,
         MEASURING_COLOR if starting else accent, True)
    vstate = vision_decision.state.value
    text('VISION', 330, 13, 11, MUTED)
    text('PREPARING...' if vstate == 'unavailable' and 'calibrating' in vision_decision.reasons else vstate.upper(),
         330, 34, 20, PALETTE[vstate], True)
    text('LIVE', w - 190, 14, 14, (103, 224, 177), True)
    text(f'{elapsed:6.1f}s' + (f'  {fps:4.1f} fps' if fps else ''), w - 190, 39, 14, MUTED)

    # 생체신호: BPM 숫자 하나
    y0 = HEADER + h
    p.line((0, y0, w, y0), fill=(48, 67, 84), width=2)
    view = bpm_view(bio)
    text('HEART RATE', 20, y0 + 12, 12, MUTED, True)
    text(view['text'], 20, y0 + 30, 48, view['color'], True)
    text('BPM', 150, y0 + 58, 16, MUTED)
    if view['note']:
        text(view['note'], 230, y0 + 52, 16, MEASURING_COLOR, True)
    if notice:
        text(notice, w // 2, y0 + 52, 16, (246, 189, 112), True)
    out = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)
    return cv2.copyMakeBorder(out, border, border, border, border, cv2.BORDER_CONSTANT, value=accent[::-1])
