#!/usr/bin/env python3
"""Regenerate the three event CSVs from the 60 km/h CAN demo baseline."""

from __future__ import annotations

import csv
import math
from datetime import datetime, timedelta
from pathlib import Path


DATA_DIR = Path(__file__).resolve().parents[1] / "data"
NORMAL_CSV = DATA_DIR / "can_normal_60kmh_15s.csv"
SAMPLE_RATE_HZ = 5
WHEELBASE_METERS = 2.7
TRACK_WIDTH_METERS = 1.7
MAX_SPEED_KPH = 120.0

# Sample indices: sleep starts, wake-up, recovery complete (5 Hz).
DROWSY_EPISODES = ((15, 25, 39), (40, 55, 64), (65, 80, 89))
DROWSY_LATERAL_METERS = -2.0
# Unlike drowsiness, this is one sustained off-centre input before a late return.
DISTRACTION_HOLD = (
    0.2, 0.5, *(0.8 for _ in range(8)),
    *(-1.0 for _ in range(6)), -0.7, -0.4,
)
CARDIAC_WOBBLE = (0.2, -0.3, 0.4, -0.4, 0.5)
CARDIAC_STEERING = (2.0, 2.8, 3.0, 2.8, 2.0, 1.0, 0.0)


def format_number(value: float, digits: int = 3) -> str:
    formatted = f"{value:.{digits}f}"
    return formatted if digits == 0 else formatted.rstrip("0").rstrip(".") or "0"


def extend_drowsy_baseline(baseline: list[dict[str, str]]) -> list[dict[str, str]]:
    extended = list(baseline)
    start_time = datetime.strptime(baseline[0]["timestamp_kst"],
                                   "%Y-%m-%d %H:%M:%S.%f KST")
    start_odometer = float(baseline[0]["odometer_km"])
    for index in range(len(baseline), 18 * SAMPLE_RATE_HZ):
        row = baseline[index - 15].copy()
        time_s = index / SAMPLE_RATE_HZ
        row["time_s"] = format_number(time_s, 1)
        row["timestamp_kst"] = (
            (start_time + timedelta(seconds=time_s)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            + " KST"
        )
        row["odometer_km"] = format_number(
            start_odometer + 60.0 * time_s / 3600.0, 3,
        )
        extended.append(row)
    return extended


def drowsy_target_lateral(index: int) -> float:
    for sleep_start, wake_up, recovery_end in DROWSY_EPISODES:
        if sleep_start <= index < wake_up:
            progress = (index - sleep_start) / (wake_up - sleep_start)
            return DROWSY_LATERAL_METERS * (10 * progress**3 - 15 * progress**4 + 6 * progress**5)
        if wake_up <= index < recovery_end:
            progress = (index - wake_up) / (recovery_end - wake_up)
            return DROWSY_LATERAL_METERS * (1 - (10 * progress**3 - 15 * progress**4 + 6 * progress**5))
    return 0.0


def drowsy_steering(index: int, previous_heading: float, speed_kph: float) -> tuple[float, float]:
    speed_mps = speed_kph / 3.6
    lateral_step = drowsy_target_lateral(index + 1) - drowsy_target_lateral(index)
    next_heading = math.asin(lateral_step * SAMPLE_RATE_HZ / speed_mps)
    yaw_rate = (next_heading - previous_heading) * SAMPLE_RATE_HZ
    road_wheel_angle = math.atan(yaw_rate * WHEELBASE_METERS / speed_mps)
    steering_limit = 1.0 - 0.55 * min(speed_kph / MAX_SPEED_KPH, 1.0)
    return math.degrees(road_wheel_angle) / steering_limit, next_heading


def write_scenario(name: str, baseline: list[dict[str, str]],
                   columns: list[str]) -> None:
    output = DATA_DIR / name
    odometer = float(baseline[0]["odometer_km"])
    previous_speed = float(baseline[0]["vehicle_speed_kph"])
    previous_drowsy_heading = 0.0
    with output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for index, source in enumerate(baseline):
            row = source.copy()
            time_s = index / SAMPLE_RATE_HZ
            steering = 0.0
            state = "Normal"
            event = "없음"
            if name == "can_caution_drowsy_drift_15s.csv":
                steering, previous_drowsy_heading = drowsy_steering(
                    index, previous_drowsy_heading, float(row["vehicle_speed_kph"]),
                )
                for sleep_start, wake_up, recovery_end in DROWSY_EPISODES:
                    if sleep_start <= index < wake_up:
                        state = "Caution"
                        event = "졸음 시연: 좌측으로 치우침"
                        break
                    if wake_up <= index <= recovery_end:
                        event = "각성 후 원래 주행 위치로 복귀"
                        break
            elif name == "can_distraction_held_steer_15s.csv":
                position = index - 25
                if 0 <= position < len(DISTRACTION_HOLD):
                    steering = DISTRACTION_HOLD[position]
                    state = "Caution"
                    event = ("전방주시 태만 시연: 조향 편향 유지" if position < 10
                             else "전방주시 태만 시연: 지연 복귀")
            elif name == "can_danger_steer_accel_15s.csv" and index >= 35:
                if index < 40:
                    steering = CARDIAC_WOBBLE[index - 35]
                    event = "심정지 시연: 초기 조향 흔들림"
                else:
                    position = index - 40
                    steering = CARDIAC_STEERING[position] if position < len(CARDIAC_STEERING) else 0.0
                    state = "Danger"
                    event = "심정지 시연: 급가속 및 급조향"
                    row["accelerator_pedal_pct"] = "75" if position == 0 else "95"
                    row["vehicle_speed_kph"] = format_number(min(82.0, 60.0 + 3.0 * (time_s - 8.0)), 2)

            speed = float(row["vehicle_speed_kph"])
            prior_speed = previous_speed
            if index:
                odometer += (prior_speed + speed) / 2 / 3600 / SAMPLE_RATE_HZ
            previous_speed = speed
            if name == "can_danger_steer_accel_15s.csv":
                row["odometer_km"] = format_number(odometer, 3)
                row["engine_rpm"] = format_number(
                    float(source["engine_rpm"]) + 25 * (speed - 60)
                    + (140 if state == "Danger" else 0), 0,
                )
                row["engine_torque_nm"] = format_number(
                    float(source["engine_torque_nm"])
                    + (float(row["accelerator_pedal_pct"]) - float(source["accelerator_pedal_pct"])) * 1.2,
                    1,
                )
                row["longitudinal_accel_g"] = format_number(
                    0.0 if index == 0 else (speed - prior_speed)
                    / (3.6 / SAMPLE_RATE_HZ * 9.81), 3,
                )

            if steering or (name == "can_danger_steer_accel_15s.csv" and state == "Danger"):
                # Match the speed-dependent steering limit in BasicWheelVehicleController.
                limit = 1.0 - 0.55 * min(speed / MAX_SPEED_KPH, 1.0)
                road_wheel_angle = steering * limit
                yaw_rps = speed / 3.6 * math.tan(math.radians(road_wheel_angle)) / WHEELBASE_METERS
                wheel_difference_kph = yaw_rps * TRACK_WIDTH_METERS * 3.6
                row["wheel_speed_fl_kph"] = format_number(speed + wheel_difference_kph / 2, 2)
                row["wheel_speed_rl_kph"] = row["wheel_speed_fl_kph"]
                row["wheel_speed_fr_kph"] = format_number(speed - wheel_difference_kph / 2, 2)
                row["wheel_speed_rr_kph"] = row["wheel_speed_fr_kph"]
                row["yaw_rate_dps"] = format_number(math.degrees(yaw_rps), 2)
                row["lateral_accel_g"] = format_number(speed / 3.6 * yaw_rps / 9.81, 3)
                row["steering_torque_nm"] = format_number(steering * 0.35, 2)
            row["steering_angle_deg"] = format_number(steering, 2)
            row["ground_truth_state"] = state
            row["ground_truth_confidence_pct"] = "95" if state == "Caution" else "98"
            row["ground_truth_event"] = event
            writer.writerow(row)


def main() -> None:
    with NORMAL_CSV.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        columns = list(reader.fieldnames or ())
        baseline = list(reader)
    if len(baseline) != 75 or any(float(row["time_s"]) != index / SAMPLE_RATE_HZ
                                    for index, row in enumerate(baseline)):
        raise ValueError("normal CAN baseline must contain 75 samples at 5 Hz")
    write_scenario("can_caution_drowsy_drift_15s.csv",
                   extend_drowsy_baseline(baseline), columns)
    write_scenario("can_danger_steer_accel_15s.csv", baseline, columns)
    write_scenario("can_distraction_held_steer_15s.csv", baseline, columns)


if __name__ == "__main__":
    main()
