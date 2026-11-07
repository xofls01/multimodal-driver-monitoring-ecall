"""Bridge CAN replay, anomaly evidence, and the existing TCP shared state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from can_anomaly import CanAnomalyConfig, CanAnomalyDetector, CanDecision
from can_scenario import CanRow, CanScenario


@dataclass
class CanPlaybackConfig:
    demo_mode: bool = False
    end_behavior: str = "normal"  # "normal" releases to cruise; "hold" keeps last command
    max_steering_angle_deg: float = 30.0
    log_interval_s: float = 1.0


class FinalStateRouter:
    """Demo-only final decision point; replace with real sensor fusion later."""

    def __init__(
        self, shared: object, publish_prediction: Callable[[object, str], None],
        config: CanPlaybackConfig,
    ) -> None:
        self.shared = shared
        self.publish_prediction = publish_prediction
        self.config = config
        self.danger_published = False

    def reset(self) -> None:
        self.danger_published = False

    def consider_can(self, decision: CanDecision) -> bool:
        if not (self.config.demo_mode and decision.can_anomaly) or self.danger_published:
            return False
        self.publish_prediction(self.shared, "Danger")
        self.danger_published = True
        return True


class CanAdapter:
    def __init__(
        self,
        shared: object,
        publish_prediction: Callable[[object, str], None],
        playback_config: CanPlaybackConfig | None = None,
        anomaly_config: CanAnomalyConfig | None = None,
    ) -> None:
        self.shared = shared
        self.publish_prediction = publish_prediction
        self.config = playback_config or CanPlaybackConfig()
        if self.config.max_steering_angle_deg <= 0:
            raise ValueError("max_steering_angle_deg must be positive")
        if self.config.end_behavior not in {"normal", "hold"}:
            raise ValueError("end_behavior must be normal or hold")
        self.detector = CanAnomalyDetector(anomaly_config)
        self.final_state_router = FinalStateRouter(shared, publish_prediction, self.config)
        self.scenario: CanScenario | None = None
        self.last_log_time_s = float("-inf")
        self.was_anomalous = False
        self.emergency_started_logged = False

    def on_start(self, scenario: CanScenario) -> None:
        self.shared.begin_can_playback(scenario.name)
        self.scenario = scenario
        self.detector.reset()
        self.last_log_time_s = float("-inf")
        self.was_anomalous = False
        self.final_state_router.reset()
        self.emergency_started_logged = False
        truth_time = scenario.ground_truth_danger_time_s
        print(f"[CAN] start {scenario.name}: {scenario.path} ({len(scenario.rows)} rows)")
        print(f"[CAN] ground truth Danger event: {truth_time if truth_time is not None else 'none'} s")

    def on_sample(self, row: CanRow) -> None:
        signals = row.signals  # Ground truth is deliberately not passed to the detector.
        decision = self.detector.update(signals)
        steering = max(-1.0, min(1.0,
            signals.steering_angle_deg / self.config.max_steering_angle_deg))
        throttle = max(0.0, min(1.0, signals.accelerator_pedal_pct / 100.0))
        brake = max(0.0, min(1.0, signals.brake_pedal_pct / 100.0))

        if decision.can_anomaly and not self.was_anomalous:
            self.was_anomalous = True
            truth_time = None if self.scenario is None else self.scenario.ground_truth_danger_time_s
            delay = None if truth_time is None else decision.decision_time_s - truth_time
            delay_text = "none" if delay is None else f"{delay:.1f}s"
            print(f"[CAN] anomaly confirmed at {decision.decision_time_s:.1f}s; "
                  f"truth={truth_time}; delay={delay_text}; conditions={decision.active_conditions}")
        if self.final_state_router.consider_can(decision):
            print("[CAN] final state Danger published; Unity emergency requested")

        applied = self.shared.apply_can_sample(signals, decision, steering, throttle, brake)
        _, unity_state = self.shared.snapshot()
        phase = None if unity_state is None else unity_state.get("emergency_phase")
        if phase not in (None, "Idle", "Unavailable") and not self.emergency_started_logged:
            self.emergency_started_logged = True
            print(f"[CAN] Unity emergency started: phase={phase} at CAN t={signals.time_s:.1f}s")
        if (signals.time_s - self.last_log_time_s >= self.config.log_interval_s
                or decision.can_anomaly and decision.decision_time_s == signals.time_s):
            self.last_log_time_s = signals.time_s
            actual_speed = None if unity_state is None else unity_state.get("speed_kph")
            error = None if actual_speed is None else float(actual_speed) - signals.vehicle_speed_kph
            print(
                f"[CAN] t={signals.time_s:.1f}s speed={signals.vehicle_speed_kph:.1f} "
                f"Unity={actual_speed} error={error} steer={signals.steering_angle_deg:.1f}deg "
                f"pedal={signals.accelerator_pedal_pct:.1f}% brake={signals.brake_pedal_pct:.1f}% "
                f"rpm={signals.engine_rpm:.0f} yaw={signals.yaw_rate_dps:.1f} "
                f"risk={decision.can_risk_score} conditions={decision.active_conditions} "
                f"applied={applied} emergency_phase={phase}"
            )

    def on_finish(self, stopped: bool) -> None:
        release = stopped or self.config.end_behavior == "normal"
        self.shared.finish_can_playback(release_control=release)
        if release and self.config.demo_mode and not self.shared.can_control_locked:
            self.publish_prediction(self.shared, "Normal")
        if self.shared.can_control_locked:
            outcome = "Danger latched; CAN controls blocked"
        elif release:
            outcome = "cruise restored"
        else:
            outcome = "last CAN command held"
        print(f"[CAN] {'stopped' if stopped else 'finished'} "
              f"{self.scenario.name if self.scenario else ''}; "
              f"{outcome}")
