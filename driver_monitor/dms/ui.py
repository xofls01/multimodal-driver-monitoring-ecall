"""화면 오버레이(HUD). OpenCV putText 는 한글을 못 그려서 HUD 는 영어로 표기."""
from __future__ import annotations

import cv2
import numpy as np

from . import geometry as G
from .monitor import OK, CAUTION, WARN, DANGER, LEVEL_NAME, State
from .wheel import WheelROI

# BGR. CAUTION 은 초록과 주황 사이의 연한 노랑
COL = {OK: (80, 200, 80), CAUTION: (90, 220, 235), WARN: (0, 190, 255), DANGER: (40, 40, 240)}
WHITE, GRAY = (240, 240, 240), (160, 160, 160)
HAND_EDGES = [(0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8), (5, 9), (9, 10), (10, 11),
              (11, 12), (9, 13), (13, 14), (14, 15), (15, 16), (13, 17), (17, 18), (18, 19), (19, 20), (0, 17)]


def txt(img, s, org, scale=0.55, color=WHITE, th=1):
    cv2.putText(img, s, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), th + 2, cv2.LINE_AA)
    cv2.putText(img, s, org, cv2.FONT_HERSHEY_SIMPLEX, scale, color, th, cv2.LINE_AA)


def draw_wheel(img, wheel: WheelROI | None, inner: float, margin: float, color=(255, 200, 0)):
    if wheel is None or wheel.ax <= 0:
        return
    h, w = img.shape[:2]
    cx, cy, ax, ay = wheel.px(w, h)
    c = (int(cx), int(cy))
    if wheel.shape == "rect":
        cv2.rectangle(img, (int(cx - ax), int(cy - ay)), (int(cx + ax), int(cy + ay)), color, 2)
    else:
        cv2.ellipse(img, c, (int(ax), int(ay)), 0, 0, 360, color, 2)
        cv2.ellipse(img, c, (int(ax * inner), int(ay * inner)), 0, 0, 360, color, 1)


def draw_face(img, face):
    if face is None:
        return
    for idx in (G.LEFT_EYE, G.RIGHT_EYE):
        pts = face.pts[idx].astype(np.int32)
        cv2.polylines(img, [pts], True, (255, 255, 0), 1, cv2.LINE_AA)
    x0, y0, x1, y1 = face.bbox
    cv2.rectangle(img, (x0, y0), (x1, y1), (200, 200, 200), 1)


def draw_hands(img, hands):
    for hd in hands:
        color = (80, 220, 80) if hd.on_wheel else (60, 60, 230)
        p = hd.pts.astype(np.int32)
        for a, b in HAND_EDGES:
            cv2.line(img, tuple(p[a]), tuple(p[b]), color, 2, cv2.LINE_AA)
        txt(img, f"{hd.label} {'GRIP' if hd.on_wheel else 'free'} {hd.in_ratio:.0%}",
            (int(p[0][0]) - 30, int(p[0][1]) + 20), 0.45, color)


def hands_text(s: State, cfg) -> str:
    """핸들 파지 상태 + 경고까지 남은 시간 / 경과 시간."""
    base = f"on wheel {s.hands_on_wheel}/2  det {s.hands_detected}"
    if cfg is None:
        return base
    if s.hands_on_wheel == 0:
        off = s.hands_off_s
        if off < cfg.hands_off_warn_s:
            nxt = f"WARN in {cfg.hands_off_warn_s - off:.1f}s"
        elif off < cfg.hands_off_danger_s:
            nxt = f"DANGER in {cfg.hands_off_danger_s - off:.1f}s"
        else:
            nxt = "DANGER"
        return f"{base}  OFF {off:.1f}s -> {nxt}"
    if s.hands_on_wheel == 1:
        one = s.one_hand_s
        nxt = f"CAUTION in {cfg.one_hand_caution_s - one:.1f}s" if one < cfg.one_hand_caution_s else "CAUTION"
        return f"{base}  one hand {one:.1f}s -> {nxt}"
    return base


def draw_hud(img, s: State, fps: float, show_help: bool, cfg=None):
    h, w = img.shape[:2]
    color = COL[s.level]
    # 상단 상태 바
    cv2.rectangle(img, (0, 0), (w, 34), color, -1)
    if s.calibrating:
        title = f"CALIBRATING... look ahead, eyes open  {s.calib_progress*100:.0f}%"
        if not s.face_found:
            title = "CALIBRATING... face not found"
    else:
        title = f"{LEVEL_NAME[s.level]} " + (" | ".join(s.reasons) if s.reasons else "attentive")
    txt(img, title[:90], (10, 23), 0.6, WHITE, 2)
    txt(img, f"{fps:.0f} fps", (w - 80, 23), 0.5)

    # 좌측 수치 패널
    lines = [
        ("EAR", f"{s.ear:.3f} / thr {s.ear_thr:.3f}", s.eyes_closed),
        ("Eyes", f"{'CLOSED' if s.eyes_closed else 'open'} {s.eyes_closed_s:.1f}s", s.eyes_closed),
        ("PERCLOS", f"{s.perclos*100:.0f}%", False),
        ("Blink/min", f"{s.blinks_per_min}", False),
        ("Pitch d", f"{s.pitch_delta:+.1f} deg", s.head_down),
        ("Head", f"{'DOWN' if s.head_down else 'up'} {s.head_down_s:.1f}s  nods {s.nods}", s.head_down),
        ("Yawn", f"MAR {s.mar:.2f}  x{s.yawns}", s.yawning),
    ]
    if s.hands_on_wheel < 0:
        lines.append(("Hands", f"det {s.hands_detected}  (wheel not set: press W)", False))
    else:
        lines.append(("Hands", hands_text(s, cfg), s.hands_on_wheel < 2))
    if s.rppg_on:
        if s.hr_progress < 1.0:
            lines.append(("HR", f"measuring... {s.hr_progress*100:.0f}%  (hold still)", False))
        elif s.hr > 0:
            q = "" if s.hr_valid else " ?"
            base = f"  base {s.hr_base:.0f} ({s.hr_delta_pct:+.0f}%)" if s.hr_base else ""
            lines.append(("HR", f"{s.hr:.0f} bpm{q}  SNR {s.hr_snr:+.1f}dB{base}", not s.hr_valid))
        else:
            lines.append(("HR", s.hr_msg or "no signal", True))
        if s.hr_roi:
            why = f"  ({s.hr_roi_reason})" if s.hr_roi_reason else ""
            lines.append(("ROI", f"{s.hr_roi}{why}  px {s.hr_fh_px}", bool(s.hr_roi_reason)))
        if s.rmssd == s.rmssd:  # not nan
            lines.append(("HRV*", f"RMSSD {s.rmssd:.0f} ms (experimental)", False))
    y = 60
    pw = max(400, max(cv2.getTextSize(f"{k:<9} {v}", cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)[0][0]
                      for k, v, _ in lines) + 20)
    overlay = img.copy()
    cv2.rectangle(overlay, (0, 40), (pw, 40 + 22 * len(lines) + 10), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.45, img, 0.55, 0, img)
    for k, v, alert in lines:
        txt(img, f"{k:<9} {v}", (8, y), 0.5, (80, 160, 255) if alert else WHITE)
        y += 22

    if show_help:
        helps = ["[Q] quit  [C] recalibrate  [W] draw wheel(ellipse)  [R] draw rect ROI",
                 "[X] clear ROI  [H] toggle help  [M] mirror  [S] snapshot"]
        for i, hs in enumerate(helps):
            txt(img, hs, (8, h - 30 + i * 18), 0.45, GRAY)
    # 위험 시 테두리 점멸 (CAUTION/WARN 은 없음)
    if s.level == DANGER:
        cv2.rectangle(img, (0, 0), (w - 1, h - 1), color, 8)


VEHICLE_COL = {"CRUISE": (80, 200, 80), "MANUAL": WHITE, "ROAD_END": (0, 190, 255),
               "ALERT": (0, 190, 255), "DECEL": (40, 40, 240), "STOPPED": (40, 40, 240)}


def draw_vehicle(img, v: dict | None):
    """Unity 차량 연동 상태 (--unity 일 때만). 좌하단, 도움말 위."""
    if v is None:
        return
    h, w = img.shape[:2]
    if v["client"] is None:
        lines = [(f"UNITY  waiting on port {v['port']} (press Play in Unity)", GRAY)]
    else:
        spd = f"{v['speed_kph']:.1f} km/h" if v["speed_kph"] is not None else "no state"
        z = f"  z {v['pos'].get('z', 0):.0f}m" if v["pos"] else ""
        phase = v["phase"]
        label = {"CRUISE": f"CRUISE {v['cruise_kph']:.0f} km/h", "MANUAL": "MANUAL (keyboard in Unity)",
                 "ALERT": "TAKEOVER: hazard on", "DECEL": "EMERGENCY: slowing, pulling over",
                 "STOPPED": "STOPPED: e-Call sent", "ROAD_END": "END OF ROAD: press R in Unity"}[phase]
        cmd = v["command"]
        lines = [(f"UNITY  {spd}{z}   {label}", VEHICLE_COL[phase]),
                 (f"       thr {cmd['throttle']:.2f} brk {cmd['brake']:.2f} steer {cmd['steering']:+.2f}"
                  f"{'  HAZARD' if cmd['hazard'] else ''}{'  ECALL' if cmd['ecall'] else ''}", GRAY)]
        if v["message"]:
            lines.append((f"       {v['message']}", GRAY))
    lines.append(("       [A] cruise/manual  [G] resolve emergency  [T] test takeover", GRAY))
    y = h - 50 - 20 * len(lines)
    for i, (s, col) in enumerate(lines):
        txt(img, s, (8, y + i * 20), 0.5, col)


def draw_rppg(img, r):
    """지금 쓰는 rPPG ROI 외곽선(이마, 필요할 때 볼) + 우하단 맥파 파형."""
    if r is None:
        return
    for hh in r.hulls:
        cv2.polylines(img, [hh], True, (200, 80, 200), 1, cv2.LINE_AA)
    if r.wave is None or r.progress < 1.0:
        return
    h, w = img.shape[:2]
    bw, bh = 260, 70
    x0, y0 = w - bw - 10, h - bh - 50
    ov = img.copy()
    cv2.rectangle(ov, (x0, y0), (x0 + bw, y0 + bh), (0, 0, 0), -1)
    cv2.addWeighted(ov, 0.5, img, 0.5, 0, img)
    wv = r.wave
    xs = np.linspace(x0 + 5, x0 + bw - 5, len(wv))
    ys = y0 + bh / 2 - wv * (bh / 2 - 8)
    pts = np.stack([xs, ys], 1).astype(np.int32)
    col = (120, 120, 255) if r.valid else (130, 130, 130)
    cv2.polylines(img, [pts], False, col, 2, cv2.LINE_AA)
    txt(img, f"pulse (last 5s) {r.mode}  ROI {r.roi}", (x0 + 5, y0 + 14), 0.4, GRAY)
