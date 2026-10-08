"""판정 로직: 캘리브레이션 → 눈 감김 / 머리 숙임 / 하품 / 손 파지 → 경고 레벨.

입력은 매 프레임의 (시간, FaceObs|None, [HandObs]) 이고, 모델과 분리되어 있어
녹화 데이터나 합성 데이터로 테스트할 수 있다.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np

from .config import Config
from .wheel import WheelROI

# 숫자가 클수록 위험. 여러 조건이 겹치면 max() 로 가장 높은 레벨을 쓴다.
# CAUTION 은 화면 표시만 하고 경고음/테두리 점멸은 없다 (WARN 부터 경고음).
OK, CAUTION, WARN, DANGER = 0, 1, 2, 3
LEVEL_NAME = {OK: "OK", CAUTION: "CAUTION", WARN: "WARNING", DANGER: "DANGER"}


class Hold:
    """조건이 연속으로 참인 시간을 잰다."""

    def __init__(self):
        self.since: float | None = None

    def update(self, cond: bool, t: float) -> float:
        if cond:
            if self.since is None:
                self.since = t
            return t - self.since
        self.since = None
        return 0.0


class EventCounter:
    """최근 window 초 안의 이벤트 개수."""

    def __init__(self, window: float):
        self.window = window
        self.q: deque[float] = deque()

    def add(self, t: float):
        self.q.append(t)

    def count(self, t: float) -> int:
        while self.q and t - self.q[0] > self.window:
            self.q.popleft()
        return len(self.q)


@dataclass
class Baseline:
    ear: float = 0.30
    pitch: float = 0.0
    nose_ratio: float = 0.0
    done: bool = False


@dataclass
class State:
    level: int = OK
    reasons: list[str] = field(default_factory=list)
    danger_reasons: list[str] = field(default_factory=list)   # reasons 중 그 자체로 DANGER 인 것
    calibrating: bool = True
    calib_progress: float = 0.0
    face_found: bool = False
    # 눈
    ear: float = 0.0
    ear_thr: float = 0.0
    eyes_closed: bool = False
    eyes_closed_s: float = 0.0
    perclos: float = 0.0
    blinks_per_min: int = 0
    # 머리
    pitch_delta: float = 0.0
    head_down: bool = False
    head_down_s: float = 0.0
    nods: int = 0
    face_lost_s: float = 0.0
    # 입
    mar: float = 0.0
    yawning: bool = False
    yawns: int = 0
    # 손
    hands_detected: int = 0
    hands_on_wheel: int = -1        # -1 = 핸들 영역 미설정. 깜빡임 유예(hands_grace_s)를 거친 값
    hands_off_s: float = 0.0        # 두 손 모두 뗀 뒤 경과 시간 (마지막으로 잡고 있던 시점부터)
    one_hand_s: float = 0.0
    # 심박 (rPPG)
    hr: float = 0.0
    hr_snr: float = -99.0
    hr_valid: bool = False
    hr_progress: float = 0.0
    hr_msg: str = ""
    hr_mode: str = ""
    hr_base: float = 0.0            # 0 = 아직 기준 없음
    hr_delta_pct: float = 0.0
    hr_low_s: float = 0.0
    rmssd: float = float("nan")
    rppg_on: bool = False
    hr_roi: str = ""                # 지금 심박 계산에 쓰는 ROI ("forehead" | "forehead+cheeks")
    hr_roi_reason: str = ""         # 볼을 추가한 이유
    hr_fh_px: int = 0               # 이마 유효 픽셀 수


class DriverMonitor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.reset_calibration()
        self.h_eye = Hold()
        self.h_head = Hold()
        self.h_lost = Hold()
        self.h_yawn = Hold()
        self._reset_hands()
        self.perclos_q: deque[tuple[float, bool]] = deque()
        self.nod_counter = EventCounter(cfg.nod_window_s)
        self.yawn_counter = EventCounter(300.0)
        self.blink_counter = EventCounter(60.0)
        self._prev_closed = False
        self._closed_start = 0.0
        self._prev_head_down = False
        self._nod_counted = False
        self._yawn_counted = False
        self._ear_s = None
        self._pitch_s = None
        self.h_hrlow = Hold()
        self.state = State()

    # ---------------- 캘리브레이션 ----------------
    def reset_calibration(self):
        self.baseline = Baseline()
        self._cal_ear: list[float] = []
        self._cal_pitch: list[float] = []
        self._cal_nose: list[float] = []
        self._cal_t0: float | None = None
        self._hr_samples: list[float] = []
        self._hr_seq = -1
        if hasattr(self, "state"):
            self.state.hr_base = 0.0

    def _calibrate(self, t: float, face) -> bool:
        if face is None:
            return False
        if self._cal_t0 is None:
            self._cal_t0 = t
        self._cal_ear.append(face.ear)
        self._cal_pitch.append(face.pitch * self.cfg.head_pitch_sign)
        self._cal_nose.append(face.nose_ratio)
        el = t - self._cal_t0
        self.state.calib_progress = min(1.0, el / self.cfg.calib_seconds)
        if el >= self.cfg.calib_seconds and len(self._cal_ear) >= 10:
            # 깜빡임 프레임의 영향을 줄이기 위해 상위 70 퍼센타일 사용
            self.baseline = Baseline(
                ear=float(np.percentile(self._cal_ear, 70)),
                pitch=float(np.median(self._cal_pitch)),
                nose_ratio=float(np.median(self._cal_nose)),
                done=True,
            )
            print(f"[calib] EAR={self.baseline.ear:.3f}  pitch={self.baseline.pitch:.1f}deg")
            return True
        return False

    def ear_threshold(self) -> float:
        c = self.cfg
        return float(np.clip(self.baseline.ear * c.ear_ratio, c.ear_min_thr, c.ear_max_thr))

    # ---------------- 메인 업데이트 ----------------
    def update(self, t: float, face, hands, wheel: WheelROI | None = None, frame_wh=(1, 1),
               rppg=None) -> State:
        c, s = self.cfg, self.state
        s.reasons = []
        s.danger_reasons = []
        s.face_found = face is not None

        self._update_hr(t, rppg)

        if not self.baseline.done:
            s.calibrating = True
            self._calibrate(t, face)
            if not self.baseline.done:
                s.level = OK
                self._update_hands(t, hands, wheel, frame_wh, active=False)
                return s
        s.calibrating = False

        # ---------- 얼굴 ----------
        s.face_lost_s = self.h_lost.update(face is None, t)
        if face is not None:
            a = c.smooth_alpha
            p = face.pitch * c.head_pitch_sign
            self._ear_s = face.ear if self._ear_s is None else a * face.ear + (1 - a) * self._ear_s
            self._pitch_s = p if self._pitch_s is None else a * p + (1 - a) * self._pitch_s
            s.ear = self._ear_s
            s.ear_thr = self.ear_threshold()
            closed_ear = face.ear < s.ear_thr        # 감김은 반응이 빨라야 하므로 raw 값 사용
            closed_bs = (face.blink_l + face.blink_r) / 2 > c.blink_score_thr
            closed = {"ear": closed_ear, "blend": closed_bs}.get(c.eye_method, closed_ear or closed_bs)
            s.pitch_delta = self._pitch_s - self.baseline.pitch
            s.head_down = s.pitch_delta > c.head_down_deg
            s.mar = face.mar
        else:
            # 얼굴이 사라졌을 때: 직전에 고개가 숙여지던 중이었으면 계속 숙임으로 간주
            closed = s.eyes_closed if s.face_lost_s < 0.5 else False
            s.head_down = self._prev_head_down
            s.mar = 0.0

        # 눈 감김 지속 / 깜빡임
        s.eyes_closed = closed
        s.eyes_closed_s = self.h_eye.update(closed, t)
        if closed and not self._prev_closed:
            self._closed_start = t
        if not closed and self._prev_closed and 0.05 < t - self._closed_start < 0.5:
            self.blink_counter.add(t)
        self._prev_closed = closed
        s.blinks_per_min = self.blink_counter.count(t)

        # PERCLOS
        if face is not None:
            self.perclos_q.append((t, closed))
        while self.perclos_q and t - self.perclos_q[0][0] > c.perclos_window_s:
            self.perclos_q.popleft()
        if self.perclos_q:
            s.perclos = sum(v for _, v in self.perclos_q) / len(self.perclos_q)
        warm = bool(self.perclos_q) and (t - self.perclos_q[0][0]) > min(20.0, c.perclos_window_s * 0.5)

        # 머리 숙임 지속 / 끄덕임
        s.head_down_s = self.h_head.update(s.head_down, t)
        if s.head_down and s.head_down_s >= 0.3 and not self._nod_counted:
            self.nod_counter.add(t)
            self._nod_counted = True
        if not s.head_down:
            self._nod_counted = False
        self._prev_head_down = s.head_down
        s.nods = self.nod_counter.count(t)

        # 하품
        s.yawning = self.h_yawn.update(s.mar > c.mar_thr, t) >= c.yawn_min_s
        if s.yawning and not self._yawn_counted:
            self.yawn_counter.add(t)
            self._yawn_counted = True
        if s.mar <= c.mar_thr:
            self._yawn_counted = False
        s.yawns = self.yawn_counter.count(t)

        # ---------- 레벨 결정 ----------
        lvl = OK

        def raise_(level, reason):
            nonlocal lvl
            lvl = max(lvl, level)
            s.reasons.append(reason)
            if level == DANGER:
                s.danger_reasons.append(reason)

        if s.eyes_closed_s >= c.eye_closed_danger_s:
            raise_(DANGER, f"EYES CLOSED {s.eyes_closed_s:.1f}s")
        elif s.eyes_closed_s >= c.eye_closed_warn_s:
            raise_(WARN, f"EYES CLOSED {s.eyes_closed_s:.1f}s")
        if warm and s.perclos >= c.perclos_danger:
            raise_(DANGER, f"PERCLOS {s.perclos*100:.0f}%")
        elif warm and s.perclos >= c.perclos_warn:
            raise_(WARN, f"PERCLOS {s.perclos*100:.0f}%")
        if s.head_down_s >= c.head_down_danger_s:
            raise_(DANGER, f"HEAD DOWN {s.head_down_s:.1f}s")
        elif s.head_down_s >= c.head_down_warn_s:
            raise_(WARN, f"HEAD DOWN {s.head_down_s:.1f}s")
        if s.nods >= c.nod_warn_count:
            raise_(WARN, f"NODDING x{s.nods}")
        if s.face_lost_s >= c.face_lost_warn_s:
            raise_(DANGER if self._prev_head_down else WARN, f"FACE NOT VISIBLE {s.face_lost_s:.1f}s")
        if s.yawns >= 3:
            raise_(WARN, f"YAWNS x{s.yawns} (5min)")

        if c.rppg_alert and s.hr_low_s >= c.hr_low_s:
            raise_(WARN, f"HR LOW {s.hr_delta_pct:+.0f}%")

        lvl = max(lvl, self._update_hands(t, hands, wheel, frame_wh, active=True))
        s.level = lvl
        return s

    # ---------------- 심박 ----------------
    def _update_hr(self, t, r):
        c, s = self.cfg, self.state
        s.rppg_on = r is not None
        if r is None:
            return
        s.hr, s.hr_snr, s.hr_valid = r.bpm, r.snr, r.valid
        s.hr_progress, s.hr_msg, s.hr_mode, s.rmssd = r.progress, r.msg, r.mode, r.rmssd
        s.hr_roi, s.hr_roi_reason, s.hr_fh_px = r.roi, r.roi_reason, r.fh_px
        # 기준 심박: 캘리브레이션(눈/머리) 끝난 뒤 유효 추정 n개 모아 중앙값
        if self.baseline.done and r.seq != self._hr_seq:
            self._hr_seq = r.seq
            if r.valid and not s.hr_base:
                self._hr_samples.append(r.raw_bpm)
                if len(self._hr_samples) >= c.rppg_baseline_n:
                    s.hr_base = float(np.median(self._hr_samples))
                    print(f"[calib] HR baseline={s.hr_base:.1f} bpm")
        if s.hr_base and s.hr_valid and s.hr > 0:
            s.hr_delta_pct = (s.hr - s.hr_base) / s.hr_base * 100
        else:
            s.hr_delta_pct = 0.0
        s.hr_low_s = self.h_hrlow.update(bool(s.hr_base) and s.hr_valid and s.hr_delta_pct <= -c.hr_drop_pct, t)

    # ---------------- 손 ----------------
    def _reset_hands(self, t: float | None = None):
        # 마지막으로 '핸들 위 손 1개 이상' / '2개'가 보인 시각. 처음엔 시작 시각으로 둔다.
        self._last_any: float | None = t
        self._last_two: float | None = t
        self._prev_eff = 2
        self._one_since: float | None = None

    def _clear_hands(self, hands):
        s = self.state
        s.hands_on_wheel = -1
        s.hands_off_s = s.one_hand_s = 0.0
        for hd in hands:
            hd.on_wheel = False
        self._last_any = None           # 영역을 다시 지정하면 시간을 처음부터 잰다

    def _update_hands(self, t, hands, wheel, frame_wh, active: bool) -> int:
        c, s = self.cfg, self.state
        w, h = frame_wh
        s.hands_detected = len(hands)
        if not c.enable_hands or wheel is None or wheel.ax <= 0:
            self._clear_hands(hands)
            return OK
        if self._last_any is None:
            self._reset_hands(t)
        n = 0
        for hd in hands:
            hd.in_ratio = wheel.grip_ratio(hd.pts, w, h, c.wheel_ring_inner, c.wheel_margin)
            ok = hd.in_ratio >= c.wheel_point_ratio
            if c.require_curl:
                ok = ok and hd.curl < c.curl_thr
            hd.on_wheel = ok
            n += int(ok)
        # 손 검출 0개(화면 밖)도 n == 0 이라 '핸들 안 잡음'으로 처리된다.
        # 손 수가 줄어든 건 hands_grace_s 동안 이어져야 인정 (1~2프레임 검출 끊김 무시).
        # 늘어난 건 바로 인정. 경과 시간은 실제로 손을 뗀 시점(마지막으로 잡고 있던 프레임)부터 잰다.
        if n >= 1:
            self._last_any = t
        if n >= 2:
            self._last_two = t
        g = c.hands_grace_s
        if n == 0 and t - self._last_any >= g:
            eff = 0
        elif n >= 2 or t - self._last_two < g:
            eff = 2
        else:
            eff = 1
        s.hands_on_wheel = eff
        s.hands_off_s = t - self._last_any if eff == 0 else 0.0
        if eff == 1:
            if self._one_since is None:
                self._one_since = self._last_two if self._prev_eff == 2 else t
            s.one_hand_s = t - self._one_since
        else:
            self._one_since = None
            s.one_hand_s = 0.0
        self._prev_eff = eff

        if not active:
            return OK
        off = s.hands_off_s
        if off >= c.hands_off_danger_s:
            s.reasons.append(f"HANDS OFF WHEEL {off:.1f}s")
            s.danger_reasons.append(s.reasons[-1])
            return DANGER
        if off >= c.hands_off_warn_s:
            s.reasons.append(f"HANDS OFF WHEEL {off:.1f}s (DANGER in {c.hands_off_danger_s - off:.1f}s)")
            return WARN
        if s.one_hand_s >= c.one_hand_caution_s:
            s.reasons.append(f"ONE HAND {s.one_hand_s:.0f}s")
            return CAUTION
        return OK
