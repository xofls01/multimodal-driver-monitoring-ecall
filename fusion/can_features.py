"""조향·가속·제동 값에서 운전자의 조작 여부를 뽑는다.

값의 크기가 아니라 변화를 본다. 쓰러진 운전자의 발이 페달을 밟고 있어도 값은 크게 나온다.
크루즈처럼 차량이 만든 값이면 운전자 조작이 아니므로 driver_input_valid=False로 넣는다.
조향 토크를 주면 토크 없이 바뀐 조향각은 차가 쏠린 것으로 보고 운전자 조작에서 뺀다.
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
    abrupt_count: int = 0           # 최근 창 안의 쏠림 후 복귀·급제동 횟수
    drift_confirmed: bool = False   # 토크로 확인한 쏠림 후 복귀가 방금 있었다
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
        self._steer_anchor: Optional[float] = None
        self._steer_moved_t: Optional[float] = None
        self._drift: Optional[dict] = None
        self._recent: Deque[Tuple[float, float]] = deque()
        self._brake_counted = False
        self._abrupt: Deque[float] = deque()
        self._drift_confirmed_t: Optional[float] = None
        self._hard_brake_t: Optional[float] = None
        self._brake_since: Optional[float] = None
        self._speed: Optional[float] = None

    def push(self, now: float, steering: float, throttle: float, brake: float,
             driver_input_valid: bool = True, speed_kph: Optional[float] = None,
             steering_torque: Optional[float] = None) -> None:
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
        hands_on = steering_torque is None or abs(steering_torque) >= self.cfg.hands_on_torque_nm
        if self._anchor is None:
            # 첫 샘플은 조작이 아니다. 무조작 시간은 여기서부터 잰다
            self._anchor, self._last_event = (steering, throttle, brake), now
            self._steer_anchor, self._steer_moved_t = steering, now
        else:
            self._detect_input(now, steering, throttle, brake, hands_on)
            self._detect_drift(now, steering, hands_on, steering_torque is not None)
        self._detect_hard_brake(now, brake)

    def _detect_input(self, now, steering, throttle, brake, hands_on) -> None:
        cfg = self.cfg
        s0, t0, b0 = self._anchor
        steer_moved = abs(steering - s0) >= cfg.steering_delta
        pedal_moved = abs(throttle - t0) >= cfg.pedal_delta or abs(brake - b0) >= cfg.pedal_delta
        if not (pedal_moved or (steer_moved and hands_on)):
            if steer_moved:
                self._anchor = (steering, t0, b0)  # 차가 쏠린 것. 운전자 조작이 아니다
            return
        if now - self._last_event >= cfg.gesture_gap_s:
            self._gestures.append(now)
            self._brake_counted = False
        self._anchor, self._last_event = (steering, throttle, brake), now

    def _detect_drift(self, now, steering, hands_on, has_torque) -> None:
        """조용하던 조향이 한쪽으로 움직였다가 곧 반대로 크게 돌아오는 패턴."""
        cfg = self.cfg
        drift = self._drift
        if drift is not None:
            if now - drift["t"] > cfg.correction_window_s:
                self._drift = drift = None
            else:
                d = drift["dir"]
                drift["peak"] = min(drift["peak"], steering) if d < 0 else max(drift["peak"], steering)
                if (steering - drift["peak"]) * -d >= cfg.correction_delta and hands_on:
                    self._abrupt.append(now)
                    if has_torque and drift["hands_off"]:
                        self._drift_confirmed_t = now
                    self._drift = None
                    self._steer_anchor, self._steer_moved_t = steering, now
                    return
        if abs(steering - self._steer_anchor) < cfg.steering_delta:
            return
        if drift is None and now - self._steer_moved_t >= cfg.drift_quiet_s:
            self._drift = {"t": now, "dir": 1 if steering > self._steer_anchor else -1,
                           "peak": steering, "hands_off": not hands_on}
        self._steer_anchor, self._steer_moved_t = steering, now

    def _detect_hard_brake(self, now: float, brake: float) -> None:
        cfg = self.cfg
        self._recent.append((now, brake))
        while self._recent[0][0] < now - cfg.abrupt_window_s:
            self._recent.popleft()
        if not self._brake_counted and brake - self._recent[0][1] >= cfg.hard_brake_delta:
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
        recent = lambda t: t is not None and now - t <= cfg.can_note_s
        return CanFeatures(
            valid=True,
            no_input_s=now - self._last_event,
            stopped=stopped,
            abrupt_count=len(self._abrupt),
            drift_confirmed=recent(self._drift_confirmed_t),
            hard_brake_recent=recent(self._hard_brake_t),
        )


def can_from_unity_state(state: dict) -> dict:
    """Unity vehicle_state를 push_can() 인자로 바꾼다.

    Manual일 때만 운전자 조작이다. Cruise에서는 조향이 0으로 고정되고 페달 값은 크루즈 제어기가 만든다.
    """
    return dict(steering=float(state["steering"]), throttle=float(state["throttle"]),
                brake=float(state["brake"]), speed_kph=state.get("speed_kph"),
                driver_input_valid=state.get("control_mode") == "Manual")


def can_from_csv_row(row: dict, steering_full_deg: float = 30.0) -> dict:
    """차량 CAN CSV 한 줄(steering_angle_deg, accelerator_pedal_pct 등)을 push_can() 인자로 바꾼다.

    steering_full_deg는 조향 -1~1에 대응하는 각도다. 기본 30도는 Unity의 최대 바퀴 조향각과 같다.
    핸들 각도를 기록한 로그라면 핸들 최대 각도(약 450~540도)를 넣는다.
    """
    def num(key):
        value = row.get(key)
        return None if value in (None, "") else float(value)
    return dict(steering=num("steering_angle_deg") / steering_full_deg,
                throttle=num("accelerator_pedal_pct") / 100.0,
                brake=num("brake_pedal_pct") / 100.0,
                speed_kph=num("vehicle_speed_kph"),
                steering_torque=num("steering_torque_nm"))
