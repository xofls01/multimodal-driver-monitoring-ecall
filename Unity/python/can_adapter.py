"""Replay simulated CAN controls without classifying the driver state."""

from __future__ import annotations

import math
from dataclasses import dataclass

from can_anomaly import CanAnomalyConfig, CanAnomalyDetector
from can_scenario import CanRow, CanScenario


@dataclass
class CanPlaybackConfig:
    max_steering_angle_deg: float = 30.0
    log_interval_s: float = 1.0
    caution_steering_limit: float = 0.08  # normalized Unity input; also used for recovery
    caution_throttle_limit: float = 0.35
    steering_command_rate_per_s: float = 0.5
    throttle_command_rate_per_s: float = 2.0
    brake_command_rate_per_s: float = 0.8
    road_heading_deg: float = 0.0  # straight Stage 1 road
    recovery_heading_gain_per_deg: float = 0.01
    recovery_lateral_gain_deg_per_m: float = 2.5
    recovery_max_heading_deg: float = 8.0
    recovery_heading_tolerance_deg: float = 1.0
    recovery_lateral_tolerance_m: float = 0.35


# Match the Unity Cruise controller while a CSV segment represents steady driving.
SPEED_DEADBAND_KPH = 1.5
THROTTLE_KP = 0.08
BRAKE_KP = 0.10
MAX_THROTTLE = 0.75
MAX_BRAKE = 0.65
PEDAL_OVERRIDE_DELTA_PCT = 20.0


class CanAdapter:
    def __init__(
        self,
        shared: object,
        playback_config: CanPlaybackConfig | None = None,
        anomaly_config: CanAnomalyConfig | None = None,
    ) -> None:
        self.shared = shared
        self.config = playback_config or CanPlaybackConfig()
        if self.config.max_steering_angle_deg <= 0:
            raise ValueError("max_steering_angle_deg must be positive")
        if (not 0 < self.config.caution_steering_limit <= 1
                or not 0 < self.config.caution_throttle_limit <= 1
                or self.config.steering_command_rate_per_s <= 0
                or self.config.throttle_command_rate_per_s <= 0
                or self.config.brake_command_rate_per_s <= 0
                or self.config.recovery_heading_gain_per_deg <= 0
                or self.config.recovery_lateral_gain_deg_per_m <= 0):
            raise ValueError("Invalid Caution correction configuration")
        self.detector = CanAnomalyDetector(anomaly_config)
        self.scenario: CanScenario | None = None
        self.baseline_pedal_pct = 0.0
        self.last_log_time_s = float("-inf")
        self.emergency_started_logged = False
        self.last_steering: float | None = None
        self.last_throttle: float | None = None
        self.last_brake: float | None = None
        self.last_time_s: float | None = None
        self.last_corrected_steering = False
        self.last_limited_accel = False
        self.previous_anomalies = (False, False)
        self.steer_recovery_pending = False
        self.reference_position: tuple[float, float] | None = None
        self.nominal_speed_kph = 60.0

    def on_start(self, scenario: CanScenario) -> None:
        self.detector.reset()
        self.last_steering = self.last_throttle = self.last_brake = self.last_time_s = None
        self.last_corrected_steering = self.last_limited_accel = False
        self.previous_anomalies = (False, False)
        self.steer_recovery_pending = False
        self.reference_position = None
        self.shared.begin_can_playback(scenario.name)
        self.scenario = scenario
        self.baseline_pedal_pct = scenario.rows[0].signals.accelerator_pedal_pct
        self.nominal_speed_kph = scenario.rows[0].signals.vehicle_speed_kph
        _, unity_state = self.shared.snapshot()
        self.reference_position = self._position(unity_state)
        self.last_log_time_s = float("-inf")
        self.emergency_started_logged = False
        print(f"[CAN] start {scenario.name}: {scenario.path} ({len(scenario.rows)} rows)")
        print("[CAN] raw CAN anomaly monitoring active; driver classification unchanged")

    def on_sample(self, row: CanRow) -> None:
        signals = row.signals
        decision = self.detector.update(signals)
        anomalies = (decision.steer_anomaly, decision.accel_anomaly)
        if anomalies != self.previous_anomalies:
            print(f"[CAN] t={signals.time_s:.1f}s vehicle anomalies "
                  f"steer={anomalies[0]} accel={anomalies[1]} "
                  f"detect={decision.active_conditions} "
                  f"release=({decision.steer_release_reason},"
                  f"{decision.accel_release_reason})")
            self.previous_anomalies = anomalies
        steering = max(-1.0, min(1.0,
            signals.steering_angle_deg / self.config.max_steering_angle_deg))
        raw_throttle = max(0.0, min(1.0, signals.accelerator_pedal_pct / 100.0))
        raw_brake = max(0.0, min(1.0, signals.brake_pedal_pct / 100.0))
        command, unity_state = self.shared.snapshot()
        speed_value = None if unity_state is None else unity_state.get("speed_kph")
        actual_speed = (float(speed_value) if isinstance(speed_value, (int, float))
                        and math.isfinite(speed_value) else None)

        # A large pedal change is an intentional maneuver in the CSV, such as
        # the Danger acceleration. Ordinary 22% baseline pedal is not cruise control.
        pedal_override = (signals.accelerator_pedal_pct - self.baseline_pedal_pct
                          >= PEDAL_OVERRIDE_DELTA_PCT or raw_brake > 0.0)
        if pedal_override:
            throttle, brake = raw_throttle, raw_brake
            control_source = "csv_pedal"
        else:
            throttle, brake = self._speed_hold_control(actual_speed, signals.vehicle_speed_kph)
            control_source = "speed_hold" if actual_speed is not None else "waiting_for_speed"

        unrestricted_steering, unrestricted_throttle = steering, throttle
        # Updated Unity resolves the actual lane/vehicle geometry and performs
        # steering at FixedUpdate rate. Do not counter-steer a second time here.
        unity_lane_guard = unity_state is not None and unity_state.get("lane_geometry_ready") is True
        if command.driver_state != "Caution":
            self.steer_recovery_pending = False
        if command.driver_state == "Caution" and decision.steer_anomaly:
            self.steer_recovery_pending = True
        if self.reference_position is None:
            self.reference_position = self._position(unity_state)
        steer_correction = (command.driver_state == "Caution"
                            and self.steer_recovery_pending)
        accel_limit = command.driver_state == "Caution" and decision.accel_anomaly
        if unity_lane_guard:
            self.steer_recovery_pending = False
            steer_correction = (command.driver_state == "Caution"
                                and unity_state.get("lane_assist_active") is True)
        elif steer_correction:
            steering = self._stabilizing_steering(unity_state, steering)
            if (not decision.steer_anomaly and
                    (self._pose_error(unity_state) is None
                     or self._pose_recovered(unity_state))):
                self.steer_recovery_pending = False
                steer_correction = False
                steering = unrestricted_steering
        if accel_limit:
            if actual_speed is None:
                throttle, brake = 0.0, 0.0
            else:
                throttle, brake = self._speed_hold_control(
                    actual_speed, self.nominal_speed_kph)
                throttle = min(throttle, self.config.caution_throttle_limit)
        dt = None if self.last_time_s is None else signals.time_s - self.last_time_s
        if dt is not None and command.driver_state != "Danger":
            if not unity_lane_guard and (steer_correction or self.last_corrected_steering):
                steering = self._rate_limit(steering, self.last_steering,
                    self.config.steering_command_rate_per_s * dt)
            if accel_limit or self.last_limited_accel:
                if (brake > 0 or accel_limit and actual_speed is not None
                        and actual_speed >= self.nominal_speed_kph - SPEED_DEADBAND_KPH):
                    throttle = 0.0  # release power immediately near/above nominal speed
                else:
                    throttle = self._rate_limit(throttle, self.last_throttle,
                        self.config.throttle_command_rate_per_s * dt)
                brake = self._rate_limit(brake, self.last_brake,
                    self.config.brake_command_rate_per_s * dt)
        self.last_steering, self.last_throttle, self.last_brake, self.last_time_s = (
            steering, throttle, brake, signals.time_s)
        self.last_corrected_steering, self.last_limited_accel = steer_correction, accel_limit

        applied = self.shared.apply_can_sample(
            signals, decision, steering, throttle, brake,
            raw_throttle=raw_throttle, raw_brake=raw_brake,
            raw_steering=max(-1.0, min(1.0,
                signals.steering_angle_deg / self.config.max_steering_angle_deg)),
            steer_correction=steer_correction, accel_limit=accel_limit,
            unrestricted_steering=unrestricted_steering,
            unrestricted_throttle=unrestricted_throttle,
        )
        phase = None if unity_state is None else unity_state.get("emergency_phase")
        if phase not in (None, "Idle", "Unavailable") and not self.emergency_started_logged:
            self.emergency_started_logged = True
            print(f"[CAN] Unity emergency started: phase={phase} at CAN t={signals.time_s:.1f}s")
        if signals.time_s - self.last_log_time_s >= self.config.log_interval_s:
            self.last_log_time_s = signals.time_s
            error = None if actual_speed is None else float(actual_speed) - signals.vehicle_speed_kph
            print(
                f"[CAN] t={signals.time_s:.1f}s speed={signals.vehicle_speed_kph:.1f} "
                f"Unity={actual_speed} error={error} steer={signals.steering_angle_deg:.1f}deg "
                f"pedal={signals.accelerator_pedal_pct:.1f}% brake={signals.brake_pedal_pct:.1f}% "
                f"rpm={signals.engine_rpm:.0f} yaw={signals.yaw_rate_dps:.1f} "
                f"steer_anomaly={decision.steer_anomaly} accel_anomaly={decision.accel_anomaly} "
                f"steer_correction={steer_correction} accel_limit={accel_limit} "
                f"reason={decision.active_conditions} "
                f"release=({decision.steer_release_reason},{decision.accel_release_reason}) "
                f"control={control_source} throttle={throttle:.2f} brake={brake:.2f} "
                f"applied={applied} emergency_phase={phase}"
            )

    @staticmethod
    def _rate_limit(target: float, previous: float | None, max_change: float) -> float:
        if previous is None:
            return target
        return max(previous - max_change, min(previous + max_change, target))

    @staticmethod
    def _position(unity_state: dict | None) -> tuple[float, float] | None:
        position = None if unity_state is None else unity_state.get("position")
        if not isinstance(position, dict):
            return None
        x, z = position.get("x"), position.get("z")
        if not all(isinstance(value, (int, float)) and math.isfinite(value)
                   for value in (x, z)):
            return None
        return float(x), float(z)

    def _pose_error(self, unity_state: dict | None) -> tuple[float, float] | None:
        position = self._position(unity_state)
        heading = None if unity_state is None else unity_state.get("heading_deg")
        if (position is None or self.reference_position is None
                or not isinstance(heading, (int, float)) or not math.isfinite(heading)):
            return None
        road_heading = math.radians(self.config.road_heading_deg)
        dx = position[0] - self.reference_position[0]
        dz = position[1] - self.reference_position[1]
        lateral_error = dx * math.cos(road_heading) - dz * math.sin(road_heading)
        heading_error = (float(heading) - self.config.road_heading_deg + 180) % 360 - 180
        return lateral_error, heading_error

    def _stabilizing_steering(self, unity_state: dict | None, raw_steering: float) -> float:
        pose_error = self._pose_error(unity_state)
        if pose_error is None:
            return max(-self.config.caution_steering_limit,
                       min(self.config.caution_steering_limit, raw_steering))
        lateral_error, heading_error = pose_error
        desired_heading = max(-self.config.recovery_max_heading_deg,
            min(self.config.recovery_max_heading_deg,
                -self.config.recovery_lateral_gain_deg_per_m * lateral_error))
        target = (desired_heading - heading_error) * self.config.recovery_heading_gain_per_deg
        return max(-self.config.caution_steering_limit,
                   min(self.config.caution_steering_limit, target))

    def _pose_recovered(self, unity_state: dict | None) -> bool:
        error = self._pose_error(unity_state)
        return (error is not None and
                abs(error[0]) <= self.config.recovery_lateral_tolerance_m and
                abs(error[1]) <= self.config.recovery_heading_tolerance_deg)

    @staticmethod
    def _speed_hold_control(actual_speed: float | None, target_speed: float) -> tuple[float, float]:
        if actual_speed is None:
            return 0.0, 0.0
        error = target_speed - actual_speed
        if abs(error) <= SPEED_DEADBAND_KPH:
            return 0.0, 0.0
        if error > 0:
            return min(error * THROTTLE_KP, MAX_THROTTLE), 0.0
        return 0.0, min(-error * BRAKE_KP, MAX_BRAKE)

    def on_finish(self, stopped: bool) -> None:
        self.shared.finish_can_playback(release_control=True)
        if self.shared.can_control_locked:
            outcome = "Danger latched; CAN controls blocked"
        else:
            outcome = "cruise restored"
        print(f"[CAN] {'stopped' if stopped else 'finished'} "
              f"{self.scenario.name if self.scenario else ''}; "
              f"{outcome}")
