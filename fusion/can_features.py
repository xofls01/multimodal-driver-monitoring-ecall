"""조향·가속·제동 값에서 운전자의 조작 여부를 뽑는다.

값의 크기가 아니라 변화를 본다. 쓰러진 운전자의 발이 페달을 밟고 있어도 값은 크게 나온다.
크루즈처럼 차량이 만든 값이면 운전자 조작이 아니므로 driver_input_valid=False로 넣는다.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional, Tuple

from .config import FusionConfig


@dataclass
class CanFeatures:
    valid: bool = False             # 운전자 조작을 읽을 수 있는가
    no_input_s: Optional[float] = None
    stopped: bool = False
    abrupt_count: int = 0           # 최근 창 안의 급조작 횟수
    hard_brake_recent: bool = False


class CanTracker:
    def __init__(self, cfg: FusionConfig):
        self.cfg = cfg
        self._last_rx: Optional[float] = None
        self._valid = False
        self._reset_motion()

    def _reset_motion(self) -> None:
        self._anchor: Optional[Tuple[float, float, float]] = None
        self._last_event: Optional[float] = None
        self._gestures: Deque[float] = deque(maxlen=64)
        self._quiet_before = 0.0
        self._steer_counted = False
        self._brake_counted = False
        self._recent: Deque[Tuple[float, float, float]] = deque()
        self._abrupt: Deque[float] = deque()
        self._hard_brake_t: Optional[float] = None
        self._brake_since: Optional[float] = None
        self._speed: Optional[float] = None

    def push(self, now: float, steering: float, throttle: float, brake: float,
             driver_input_valid: bool = True, speed_kph: Optional[float] = None) -> None:
        if not all(math.isfinite(v) for v in (steering, throttle, brake)):
            return
        self._last_rx = now
        if not driver_input_valid:
            self._valid = False
            self._reset_motion()
            return
        self._valid = True
        self._speed = speed_kph
        if brake < self.cfg.stopped_brake:
            self._brake_since = None
        elif self._brake_since is None:
            self._brake_since = now
        if self._anchor is None:
            # 첫 샘플은 조작이 아니다. 무조작 시간은 여기서부터 잰다
            self._anchor, self._last_event = (steering, throttle, brake), now
        else:
            self._detect_input(now, steering, throttle, brake)
        self._detect_abrupt(now, steering, brake)

    def _detect_input(self, now: float, steering: float, throttle: float, brake: float) -> None:
        cfg = self.cfg
        s0, t0, b0 = self._anchor
        moved = (abs(steering - s0) >= cfg.steering_delta or abs(throttle - t0) >= cfg.pedal_delta
                 or abs(brake - b0) >= cfg.pedal_delta)
        if not moved:
            return
        gap = now - self._last_event
        if gap >= cfg.gesture_gap_s:
            self._gestures.append(now)
            self._quiet_before = gap
            self._steer_counted = self._brake_counted = False
        self._anchor, self._last_event = (steering, throttle, brake), now

    def _detect_abrupt(self, now: float, steering: float, brake: float) -> None:
        cfg = self.cfg
        self._recent.append((now, steering, brake))
        while self._recent[0][0] < now - cfg.abrupt_window_s:
            self._recent.popleft()
        _, s_old, b_old = self._recent[0]
        if (not self._steer_counted and self._quiet_before >= cfg.abrupt_quiet_before_s
                and abs(steering - s_old) >= cfg.abrupt_steer_delta):
            self._steer_counted = True
            self._abrupt.append(now)
        if not self._brake_counted and brake - b_old >= cfg.hard_brake_delta:
            self._brake_counted = True
            self._abrupt.append(now)
            self._hard_brake_t = now

    def gestures_since(self, t: float) -> int:
        return sum(g >= t for g in self._gestures)

    def features(self, now: float) -> CanFeatures:
        cfg = self.cfg
        if not self._valid or self._last_rx is None or now - self._last_rx > cfg.can_stale_s:
            return CanFeatures()
        while self._abrupt and self._abrupt[0] < now - cfg.abrupt_repeat_window_s:
            self._abrupt.popleft()
        if self._speed is not None:
            stopped = self._speed < cfg.stopped_speed_kph
        else:
            stopped = self._brake_since is not None and now - self._brake_since >= cfg.stopped_brake_hold_s
        return CanFeatures(
            valid=True,
            no_input_s=now - self._last_event,
            stopped=stopped,
            abrupt_count=len(self._abrupt),
            hard_brake_recent=(self._hard_brake_t is not None
                               and now - self._hard_brake_t <= cfg.hard_brake_note_s),
        )


def can_from_unity_state(state: dict) -> dict:
    """Unity vehicle_state를 push_can() 인자로 바꾼다.

    Manual일 때만 운전자 조작이다. Cruise에서는 조향이 0으로 고정되고 페달 값은 크루즈 제어기가 만든다.
    """
    return dict(steering=float(state["steering"]), throttle=float(state["throttle"]),
                brake=float(state["brake"]), speed_kph=state.get("speed_kph"),
                driver_input_valid=state.get("control_mode") == "Manual")
