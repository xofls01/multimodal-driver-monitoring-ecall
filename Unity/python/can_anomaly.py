"""CAN-only anomaly evidence, intentionally blind to CSV ground truth."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from can_scenario import CanSignals


@dataclass(frozen=True)
class CanAnomalyConfig:
    window_samples: int = 5
    window_seconds: float = 1.0
    minimum_vehicle_speed_kph: float = 40.0
    steering_rate_threshold_deg_s: float = 60.0
    accelerator_window_s: float = 0.5
    accelerator_delta_threshold_pct: float = 40.0
    yaw_rate_threshold_dps: float = 8.0
    speed_delta_threshold_kph: float = 5.0
    wheel_speed_difference_threshold_kph: float = 1.0
    brake_threshold_pct: float = 1.0
    minimum_risk_score: int = 2
    persistence_s: float = 0.4

    def __post_init__(self) -> None:
        if self.window_samples < 1 or self.window_seconds <= 0 or self.persistence_s < 0:
            raise ValueError("CAN anomaly window and persistence settings are invalid")


@dataclass(frozen=True)
class CanDecision:
    can_anomaly: bool
    can_risk_score: int
    active_conditions: tuple[str, ...]
    decision_time_s: float | None
    steering_rate_deg_s: float
    steering_delta_deg: float
    accelerator_delta_pct: float
    speed_delta_kph: float
    wheel_speed_difference_kph: float
    current_yaw_rate_dps: float


class CanAnomalyDetector:
    def __init__(self, config: CanAnomalyConfig | None = None) -> None:
        self.config = config or CanAnomalyConfig()
        self.reset()

    def reset(self) -> None:
        self.window: deque[CanSignals] = deque(maxlen=self.config.window_samples)
        self.high_risk_since: float | None = None
        self.decision_time_s: float | None = None

    def update(self, sample: CanSignals) -> CanDecision:
        previous = self.window[-1] if self.window else None
        if previous is not None and sample.time_s <= previous.time_s:
            raise ValueError("CAN samples must have increasing time_s")
        while self.window and sample.time_s - self.window[0].time_s > self.config.window_seconds:
            self.window.popleft()
        self.window.append(sample)
        current = tuple(self.window)
        steering_delta = 0.0 if previous is None else (
            sample.steering_angle_deg - previous.steering_angle_deg
        )
        steering_rate = 0.0 if previous is None else (
            steering_delta / (sample.time_s - previous.time_s)
        )
        accel_history = [item.accelerator_pedal_pct for item in current
                         if 0 < sample.time_s - item.time_s <= self.config.accelerator_window_s]
        accel_delta = max(0.0, sample.accelerator_pedal_pct - min(accel_history)) if accel_history else 0.0
        speed_delta = sample.vehicle_speed_kph - current[0].vehicle_speed_kph
        left = (sample.wheel_speed_fl_kph + sample.wheel_speed_rl_kph) / 2
        right = (sample.wheel_speed_fr_kph + sample.wheel_speed_rr_kph) / 2
        wheel_difference = abs(left - right)
        active: list[str] = []
        config = self.config
        if (sample.vehicle_speed_kph >= config.minimum_vehicle_speed_kph
                and abs(steering_rate) >= config.steering_rate_threshold_deg_s):
            active.append("rapid_steering")
        if accel_delta >= config.accelerator_delta_threshold_pct:
            active.append("rapid_acceleration_pedal")
        if abs(sample.yaw_rate_dps) >= config.yaw_rate_threshold_dps:
            active.append("high_yaw_rate")
        if (sample.brake_pedal_pct < config.brake_threshold_pct
                and speed_delta >= config.speed_delta_threshold_kph):
            active.append("unbraked_speed_increase")
        if wheel_difference >= config.wheel_speed_difference_threshold_kph:
            active.append("wheel_speed_imbalance")
        if len(active) >= config.minimum_risk_score:
            if self.high_risk_since is None:
                self.high_risk_since = sample.time_s
            if sample.time_s - self.high_risk_since + 1e-9 >= config.persistence_s:
                if self.decision_time_s is None:
                    self.decision_time_s = sample.time_s
        else:
            self.high_risk_since = None
        return CanDecision(
            can_anomaly=self.decision_time_s is not None,
            can_risk_score=len(active),
            active_conditions=tuple(active),
            decision_time_s=self.decision_time_s,
            steering_rate_deg_s=steering_rate,
            steering_delta_deg=steering_delta,
            accelerator_delta_pct=accel_delta,
            speed_delta_kph=speed_delta,
            wheel_speed_difference_kph=wheel_difference,
            current_yaw_rate_dps=sample.yaw_rate_dps,
        )
