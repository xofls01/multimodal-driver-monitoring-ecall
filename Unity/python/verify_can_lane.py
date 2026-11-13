#!/usr/bin/env python3
"""Record real Unity lane clearance while replaying a bounded CAN window.

Start this server, then press Unity Play. Warm-up plus playback is capped at 14s;
stop Unity Play when RESULT is printed. Full 18s drowsiness is tested in windows.
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path

from can_adapter import CanAdapter
from can_scenario import CanScenario, load_can_scenario
from vehicle_tcp_server import SharedData, server_loop

FILES = {
    "normal": "can_normal_60kmh_15s.csv",
    "caution": "can_caution_drowsy_drift_15s.csv",
    "distraction": "can_distraction_held_steer_15s.csv",
    "danger": "can_danger_steer_accel_15s.csv",
}


def recovery_observed(result: dict) -> bool:
    """Reject a frozen vehicle even when lane clearance and final pose look good."""
    return (result["peak_center_offset_m"] >= 0.25
            and result["final_center_offset_m"] <= 0.15
            and abs(result["final_heading_error_deg"]) <= 1.0
            and result["applied_steering_range"][0] < -0.002
            and result["applied_steering_range"][1] > 0.002)


class Recorder(SharedData):
    def __init__(self) -> None:
        super().__init__()
        self.recording = False
        self.samples: list[dict] = []

    def set_state(self, state: dict) -> None:
        super().set_state(state)
        if self.recording and state.get("control_mode") == "Remote":
            command, _ = self.snapshot()
            self.samples.append({**state, "can_time_s": command.can_time_s,
                                 "raw_steering": command.can_steering,
                                 "requested_steering": command.steering,
                                 "accel_limit": command.accel_limit_active})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", choices=FILES)
    parser.add_argument("--start", type=float, default=0)
    parser.add_argument("--end", type=float, default=8)
    parser.add_argument("--state", choices=("Normal", "Caution", "Danger"), default="Caution")
    parser.add_argument("--min-clearance", type=float, default=0.15)
    parser.add_argument("--warmup-seconds", type=float, default=4)
    parser.add_argument("--min-interventions", type=int, default=0)
    parser.add_argument("--output", type=Path, default=Path("/private/tmp/can-lane-result.json"))
    args = parser.parse_args()
    if (args.warmup_seconds < 0 or args.end <= args.start
            or args.end - args.start + args.warmup_seconds > 13):
        parser.error("Warm-up plus playback must be at most 13 seconds to limit Unity heat")
    data = Path(__file__).resolve().parents[1] / "data"
    scenario = load_can_scenario(data / FILES[args.scenario], args.scenario)
    rows = tuple(row for row in scenario.rows if args.start <= row.signals.time_s < args.end)
    if not rows:
        parser.error("Empty CAN window")
    scenario = CanScenario(scenario.name, scenario.path, rows)
    shared = Recorder()
    shared.set_driver_state(args.state)
    server = threading.Thread(target=server_loop, args=("127.0.0.1", 5001, shared, 20), daemon=True)
    server.start()
    print("READY: press Unity Play", flush=True)
    try:
        while shared.latest_state is None:
            time.sleep(0.02)
        connected_at = time.monotonic()
        while time.monotonic() - connected_at < args.warmup_seconds:
            _, state = shared.snapshot()
            if state and state.get("speed_kph", 0) >= 58 and state.get("lane_geometry_ready"):
                break
            time.sleep(0.02)
        _, state = shared.snapshot()
        if not state or not state.get("lane_geometry_ready"):
            raise RuntimeError("Updated Unity lane geometry telemetry is unavailable")
        adapter = CanAdapter(shared)
        shared.recording = True
        adapter.on_start(scenario)
        started_at = time.monotonic()
        for row in rows:
            due = started_at + row.signals.time_s - rows[0].signals.time_s
            time.sleep(max(0, due - time.monotonic()))
            adapter.on_sample(row)
        # Keep the final neutral sample long enough to observe the control handoff
        # in the next telemetry packet (the editor may render at only 10 Hz).
        time.sleep(0.4)
        shared.recording = False
        adapter.on_finish(False)
        samples = shared.samples
        def center_offset(sample: dict) -> float:
            center = (sample["lane_left_boundary_m"] + sample["lane_right_boundary_m"]) / 2
            return abs(sample["position"]["x"] - center)
        first_assist = next((s for s in samples if s["lane_assist_active"]), None)
        activation_times = []
        release_times = []
        active = False
        for sample in samples:
            if sample["lane_assist_active"] and not active:
                activation_times.append(sample["can_time_s"])
            if active and not sample["lane_assist_active"]:
                release_times.append(sample["can_time_s"])
            active = sample["lane_assist_active"]
        result = {
            "scenario": args.scenario, "state": args.state,
            "window": [args.start, args.end], "samples": len(samples),
            "min_clearance_m": min(s["lane_clearance_m"] for s in samples),
            "physics_min_clearance_m": min((s["lane_min_clearance_m"] for s in samples
                if s.get("lane_assist_authorized")), default=min(s["lane_clearance_m"] for s in samples)),
            "max_speed_kph": max(s["speed_kph"] for s in samples),
            "assist_frames": sum(s["lane_assist_active"] for s in samples),
            "first_assist_can_time_s": next((s["can_time_s"] for s in samples
                                             if s["lane_assist_active"]), None),
            "x_range": [min(s["position"]["x"] for s in samples),
                        max(s["position"]["x"] for s in samples)],
            "peak_center_offset_m": max(center_offset(s) for s in samples),
            "first_assist_center_offset_m": None if first_assist is None else center_offset(first_assist),
            "final_center_offset_m": center_offset(samples[-1]),
            "final_heading_error_deg": (samples[-1]["heading_deg"] + 180) % 360 - 180,
            "applied_steering_range": [min(s["steering"] for s in samples),
                                       max(s["steering"] for s in samples)],
            "activation_can_times": activation_times,
            "release_can_times": release_times,
            "observed_can_time_range": [min(s["can_time_s"] for s in samples),
                                        max(s["can_time_s"] for s in samples)],
        }
        result["coverage_complete"] = (
            result["observed_can_time_range"][0] <= rows[0].signals.time_s + 0.25
            and result["observed_can_time_range"][1] >= rows[-1].signals.time_s - 0.25)
        result["passed"] = (result["physics_min_clearance_m"] > args.min_clearance
                            and result["min_clearance_m"] > args.min_clearance
                            and result["coverage_complete"]
                            and len(activation_times) >= args.min_interventions)
        if args.scenario != "normal" and args.state == "Caution":
            result["departure_and_recovery_verified"] = recovery_observed(result)
            result["passed"] = result["passed"] and result["departure_and_recovery_verified"]
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({"result": result, "telemetry": samples}, indent=2) + "\n")
        print("RESULT " + json.dumps(result), flush=True)
        if not result["passed"]:
            raise SystemExit(1)
    finally:
        shared.running = False
        server.join(timeout=1)


if __name__ == "__main__":
    main()
