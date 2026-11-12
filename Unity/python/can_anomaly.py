"""Raw CAN vehicle-control anomalies; independent of driver classification.

CSV steering_angle_deg is Unity's reference front-wheel angle at low speed, not
a physical steering-wheel angle. Unity reduces it with speed before applying it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from can_scenario import CanSignals


@dataclass(frozen=True)
class CanAnomalyConfig:
    wheelbase_m: float = 2.7
    steering_ratio: float = 1.0
    unity_max_speed_kph: float = 120.0
    unity_min_steering_factor: float = 0.45
    rapid_steer_rate_deg_s: float = 5.0
    moderate_lateral_accel_mps2: float = 2.0
    high_lateral_accel_mps2: float = 6.0
    steer_release_lateral_accel_mps2: float = 1.5
    steer_release_rate_deg_s: float = 3.0
    pedal_rise_pct_s: float = 80.0
    longitudinal_accel_g: float = 0.05
    accel_hold_pedal_delta_pct: float = 20.0
    accel_hold_longitudinal_g: float = 0.04
    accel_release_pedal_delta_pct: float = 5.0
    accel_release_longitudinal_g: float = 0.02
    confirm_seconds: float = 0.4
    release_seconds: float = 1.0

    def __post_init__(self) -> None:
        if (self.wheelbase_m <= 0 or self.steering_ratio <= 0
                or self.unity_max_speed_kph <= 0
                or not 0 < self.unity_min_steering_factor <= 1
                or self.confirm_seconds < 0 or self.release_seconds < 0):
            raise ValueError("Invalid CAN anomaly configuration")


@dataclass(frozen=True)
class CanDecision:
    steer_anomaly: bool
    accel_anomaly: bool
    steering_rate_deg_s: float
    predicted_lateral_accel_mps2: float
    pedal_rise_pct_s: float
    raw_longitudinal_accel_g: float
    steer_reason: str
    accel_reason: str
    steer_release_reason: str
    accel_release_reason: str
    decision_time_s: float | None

    @property
    def can_anomaly(self) -> bool:
        return self.steer_anomaly or self.accel_anomaly

    @property
    def can_risk_score(self) -> int:
        return int(self.steer_anomaly) + int(self.accel_anomaly)

    @property
    def active_conditions(self) -> tuple[str, ...]:
        return tuple(reason for reason, active in (
            (self.steer_reason, self.steer_anomaly),
            (self.accel_reason, self.accel_anomaly),
        ) if active)


class CanAnomalyDetector:
    def __init__(self, config: CanAnomalyConfig | None = None) -> None:
        self.config = config or CanAnomalyConfig()
        self.reset()

    def reset(self) -> None:
        self.previous: CanSignals | None = None
        self.baseline_pedal_pct: float | None = None
        self.steer_anomaly = False
        self.accel_anomaly = False
        self.rapid_since: float | None = None
        self.high_steer_since: float | None = None
        self.accel_since: float | None = None
        self.steer_clear_since: float | None = None
        self.accel_clear_since: float | None = None
        self.steer_reason = ""
        self.accel_reason = ""
        self.decision_time_s: float | None = None

    @property
    def can_anomaly(self) -> bool:
        return self.steer_anomaly or self.accel_anomaly

    def update(self, sample: CanSignals) -> CanDecision:
        c = self.config
        values = (sample.time_s, sample.vehicle_speed_kph,
                  sample.steering_angle_deg, sample.accelerator_pedal_pct,
                  sample.longitudinal_accel_g)
        if (not all(math.isfinite(value) for value in values)
                or sample.time_s < 0 or sample.vehicle_speed_kph < 0
                or not 0 <= sample.accelerator_pedal_pct <= 100):
            raise ValueError("Invalid raw CAN sample")
        previous = self.previous
        if previous is not None and sample.time_s <= previous.time_s:
            raise ValueError("CAN samples must have increasing time_s")
        dt = sample.time_s - previous.time_s if previous is not None else None
        steering_rate = (0.0 if dt is None else
                         (sample.steering_angle_deg - previous.steering_angle_deg) / dt)
        pedal_rise = (0.0 if dt is None else max(0.0,
            (sample.accelerator_pedal_pct - previous.accelerator_pedal_pct) / dt))
        speed_factor = 1.0 - (1.0 - c.unity_min_steering_factor) * min(
            sample.vehicle_speed_kph / c.unity_max_speed_kph, 1.0)
        front_wheel_angle = math.radians(
            sample.steering_angle_deg / c.steering_ratio * speed_factor)
        ay = (sample.vehicle_speed_kph / 3.6) ** 2 * abs(
            math.tan(front_wheel_angle)) / c.wheelbase_m
        if self.baseline_pedal_pct is None:
            self.baseline_pedal_pct = sample.accelerator_pedal_pct
        pedal_delta = sample.accelerator_pedal_pct - self.baseline_pedal_pct
        steer_release_reason = accel_release_reason = ""

        rapid = (dt is not None and abs(steering_rate) >= c.rapid_steer_rate_deg_s
                 and ay >= c.moderate_lateral_accel_mps2)
        if rapid and self.rapid_since is None:
            self.rapid_since = sample.time_s
        if ay >= c.high_lateral_accel_mps2:
            if self.high_steer_since is None:
                self.high_steer_since = sample.time_s
        else:
            self.high_steer_since = None
        if not self.steer_anomaly:
            rapid_confirmed = (self.rapid_since is not None
                and ay >= c.moderate_lateral_accel_mps2
                and sample.time_s - self.rapid_since + 1e-9 >= c.confirm_seconds)
            held_confirmed = (self.high_steer_since is not None
                and sample.time_s - self.high_steer_since + 1e-9 >= c.confirm_seconds)
            if rapid_confirmed or held_confirmed:
                self.steer_anomaly = True
                self.steer_reason = ("rapid_steering_with_lateral_accel" if rapid_confirmed
                                     else "sustained_high_lateral_accel")
                if self.decision_time_s is None:
                    self.decision_time_s = sample.time_s
        if ay < c.moderate_lateral_accel_mps2:
            self.rapid_since = None
        if self.steer_anomaly:
            clear = (ay <= c.steer_release_lateral_accel_mps2
                     and abs(steering_rate) <= c.steer_release_rate_deg_s)
            self.steer_clear_since = sample.time_s if clear and self.steer_clear_since is None else (
                self.steer_clear_since if clear else None)
            if (self.steer_clear_since is not None and
                    sample.time_s - self.steer_clear_since + 1e-9 >= c.release_seconds):
                self.steer_anomaly = False
                self.steer_reason = ""
                steer_release_reason = "raw_steering_normal_for_release_seconds"
                self.steer_clear_since = None

        accel_trigger = (dt is not None and pedal_rise >= c.pedal_rise_pct_s
                         and sample.longitudinal_accel_g >= c.longitudinal_accel_g)
        if accel_trigger:
            self.accel_since = sample.time_s
        if not self.accel_anomaly and self.accel_since is not None:
            hold = (pedal_delta >= c.accel_hold_pedal_delta_pct
                    and sample.longitudinal_accel_g >= c.accel_hold_longitudinal_g)
            if not hold:
                self.accel_since = None
            elif sample.time_s - self.accel_since + 1e-9 >= c.confirm_seconds:
                self.accel_anomaly = True
                self.accel_reason = "rapid_pedal_with_raw_longitudinal_accel"
                if self.decision_time_s is None:
                    self.decision_time_s = sample.time_s
        if self.accel_anomaly:
            clear = (pedal_delta <= c.accel_release_pedal_delta_pct
                     and sample.longitudinal_accel_g <= c.accel_release_longitudinal_g)
            self.accel_clear_since = sample.time_s if clear and self.accel_clear_since is None else (
                self.accel_clear_since if clear else None)
            if (self.accel_clear_since is not None and
                    sample.time_s - self.accel_clear_since + 1e-9 >= c.release_seconds):
                self.accel_anomaly = False
                self.accel_reason = ""
                accel_release_reason = "raw_pedal_and_acceleration_normal_for_release_seconds"
                self.accel_clear_since = None
        if not self.can_anomaly:
            self.decision_time_s = None
        self.previous = sample
        return CanDecision(
            self.steer_anomaly, self.accel_anomaly, steering_rate, ay, pedal_rise,
            sample.longitudinal_accel_g, self.steer_reason, self.accel_reason,
            steer_release_reason, accel_release_reason, self.decision_time_s,
        )
