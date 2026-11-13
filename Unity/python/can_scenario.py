"""Load and replay the demo CAN CSV without depending on Unity or ground truth."""

from __future__ import annotations

import csv
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


REQUIRED_COLUMNS = (
    "time_s", "timestamp_kst", "vehicle_speed_kph", "odometer_km",
    "wheel_speed_fl_kph", "wheel_speed_fr_kph", "wheel_speed_rl_kph",
    "wheel_speed_rr_kph", "steering_angle_deg", "steering_torque_nm",
    "accelerator_pedal_pct", "brake_pedal_pct", "brake_pressure_bar",
    "engine_rpm", "engine_torque_nm", "gear", "longitudinal_accel_g",
    "lateral_accel_g", "yaw_rate_dps", "left_indicator", "right_indicator",
    "hazard", "brake_lamp", "ecall", "ground_truth_state",
    "ground_truth_confidence_pct", "ground_truth_event",
)
TEXT_COLUMNS = {"timestamp_kst", "gear", "ground_truth_state", "ground_truth_event"}


@dataclass(frozen=True)
class CanSignals:
    time_s: float
    timestamp_kst: str
    vehicle_speed_kph: float
    odometer_km: float
    wheel_speed_fl_kph: float
    wheel_speed_fr_kph: float
    wheel_speed_rl_kph: float
    wheel_speed_rr_kph: float
    steering_angle_deg: float
    steering_torque_nm: float
    accelerator_pedal_pct: float
    brake_pedal_pct: float
    brake_pressure_bar: float
    engine_rpm: float
    engine_torque_nm: float
    gear: str
    longitudinal_accel_g: float
    lateral_accel_g: float
    yaw_rate_dps: float
    left_indicator: bool
    right_indicator: bool
    hazard: bool
    brake_lamp: bool
    ecall: bool


@dataclass(frozen=True)
class CanRow:
    signals: CanSignals
    ground_truth_state: str
    ground_truth_event: str


@dataclass(frozen=True)
class CanScenario:
    name: str
    path: Path
    rows: tuple[CanRow, ...]

    @property
    def duration_s(self) -> float:
        if len(self.rows) < 2:
            return 0.0
        return (self.rows[-1].signals.time_s - self.rows[0].signals.time_s
                + self.rows[-1].signals.time_s - self.rows[-2].signals.time_s)

    @property
    def ground_truth_danger_time_s(self) -> float | None:
        return next(
            (row.signals.time_s for row in self.rows
             if row.ground_truth_state.lower() == "danger"),
            None,
        )


def load_can_scenario(path: str | Path, name: str | None = None) -> CanScenario:
    source = Path(path)
    rows: list[CanRow] = []
    with source.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        missing_columns = set(REQUIRED_COLUMNS) - set(reader.fieldnames or ())
        if missing_columns:
            raise ValueError(f"CAN CSV missing columns: {', '.join(sorted(missing_columns))}")
        previous_time = -math.inf
        for line_number, raw in enumerate(reader, start=2):
            if None in raw or any(raw[column] is None or not raw[column].strip()
                                   for column in REQUIRED_COLUMNS):
                raise ValueError(f"CAN CSV line {line_number}: missing value")
            try:
                numeric = {
                    column: float(raw[column]) for column in REQUIRED_COLUMNS
                    if column not in TEXT_COLUMNS
                }
            except ValueError as error:
                raise ValueError(f"CAN CSV line {line_number}: invalid number") from error
            if not all(math.isfinite(value) for value in numeric.values()):
                raise ValueError(f"CAN CSV line {line_number}: non-finite number")
            sample_time = numeric["time_s"]
            if sample_time < 0 or sample_time <= previous_time:
                raise ValueError(f"CAN CSV line {line_number}: time_s must increase")
            previous_time = sample_time
            signals = CanSignals(
                time_s=sample_time,
                timestamp_kst=raw["timestamp_kst"],
                vehicle_speed_kph=numeric["vehicle_speed_kph"],
                odometer_km=numeric["odometer_km"],
                wheel_speed_fl_kph=numeric["wheel_speed_fl_kph"],
                wheel_speed_fr_kph=numeric["wheel_speed_fr_kph"],
                wheel_speed_rl_kph=numeric["wheel_speed_rl_kph"],
                wheel_speed_rr_kph=numeric["wheel_speed_rr_kph"],
                steering_angle_deg=numeric["steering_angle_deg"],
                steering_torque_nm=numeric["steering_torque_nm"],
                accelerator_pedal_pct=numeric["accelerator_pedal_pct"],
                brake_pedal_pct=numeric["brake_pedal_pct"],
                brake_pressure_bar=numeric["brake_pressure_bar"],
                engine_rpm=numeric["engine_rpm"],
                engine_torque_nm=numeric["engine_torque_nm"],
                gear=raw["gear"],
                longitudinal_accel_g=numeric["longitudinal_accel_g"],
                lateral_accel_g=numeric["lateral_accel_g"],
                yaw_rate_dps=numeric["yaw_rate_dps"],
                left_indicator=bool(numeric["left_indicator"]),
                right_indicator=bool(numeric["right_indicator"]),
                hazard=bool(numeric["hazard"]),
                brake_lamp=bool(numeric["brake_lamp"]),
                ecall=bool(numeric["ecall"]),
            )
            rows.append(CanRow(
                signals, raw["ground_truth_state"].strip(), raw["ground_truth_event"].strip(),
            ))
    if not rows:
        raise ValueError("CAN CSV has no data rows")
    return CanScenario(name or source.stem, source, tuple(rows))


class CanScenarioPlayer:
    """Use CSV time_s as a monotonic-clock schedule; load only on start."""

    def __init__(
        self,
        on_start: Callable[[CanScenario], None],
        on_sample: Callable[[CanRow], None],
        on_finish: Callable[[bool], None],
    ) -> None:
        self.on_start = on_start
        self.on_sample = on_sample
        self.on_finish = on_finish
        self.scenario: CanScenario | None = None
        self.thread: threading.Thread | None = None
        self.stop_event: threading.Event | None = None
        self.started_at = 0.0
        self.elapsed_s = 0.0
        self.lock = threading.Lock()

    def start(self, path: str | Path, name: str) -> None:
        scenario = load_can_scenario(path, name)
        self._start_loaded(scenario)

    def restart(self) -> None:
        if self.scenario is None:
            raise ValueError("No CAN scenario to restart")
        self._start_loaded(self.scenario)

    def _start_loaded(self, scenario: CanScenario) -> None:
        self.stop()
        self.scenario = scenario
        self.elapsed_s = 0.0
        self.on_start(scenario)
        stop_event = threading.Event()
        self.stop_event = stop_event
        self.started_at = time.monotonic()
        self.thread = threading.Thread(
            target=self._run, args=(scenario, stop_event, self.started_at),
            daemon=True, name=f"CAN-{scenario.name}",
        )
        self.thread.start()

    def stop(self) -> bool:
        thread, stop_event = self.thread, self.stop_event
        if thread is None or stop_event is None or not thread.is_alive():
            return False
        stop_event.set()
        thread.join(timeout=1.5)
        if thread.is_alive():
            raise RuntimeError("CAN replay thread did not stop")
        return True

    def status(self) -> tuple[str | None, float, bool]:
        with self.lock:
            elapsed = self.elapsed_s
        return (
            None if self.scenario is None else self.scenario.name,
            elapsed,
            self.thread is not None and self.thread.is_alive(),
        )

    def _run(self, scenario: CanScenario, stop_event: threading.Event, started_at: float) -> None:
        first_time = scenario.rows[0].signals.time_s
        stopped = False
        try:
            for row in scenario.rows:
                due = started_at + row.signals.time_s - first_time
                if stop_event.wait(max(0.0, due - time.monotonic())):
                    stopped = True
                    break
                with self.lock:
                    self.elapsed_s = row.signals.time_s - first_time
                self.on_sample(row)
            if not stopped and len(scenario.rows) > 1:
                end_due = started_at + scenario.duration_s
                if stop_event.wait(max(0.0, end_due - time.monotonic())):
                    stopped = True
                else:
                    with self.lock:
                        self.elapsed_s = scenario.duration_s
        except Exception as error:
            stopped = True
            print(f"[CAN] replay error: {error}")
        finally:
            self.on_finish(stopped)
