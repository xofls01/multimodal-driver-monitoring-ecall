"""Offline CAN replay/anomaly tests; Unity is not required."""

import csv
import io
import json
import math
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from can_adapter import CanAdapter, CanPlaybackConfig
from can_anomaly import CanAnomalyDetector
from can_scenario import CanRow, CanScenario, CanScenarioPlayer, load_can_scenario
from vehicle_tcp_server import SharedData, main, parse_args, publish_prediction


DATA_DIR = Path(__file__).resolve().parents[1] / "data"
NORMAL_CSV = DATA_DIR / "can_normal_60kmh_15s.csv"
CAUTION_CSV = DATA_DIR / "can_caution_drowsy_drift_15s.csv"
DISTRACTION_CSV = DATA_DIR / "can_distraction_held_steer_15s.csv"
DANGER_CSV = DATA_DIR / "can_danger_steer_accel_15s.csv"


class CanIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        print_patch = patch("builtins.print")
        print_patch.start()
        self.addCleanup(print_patch.stop)

    def test_replay_respects_csv_time(self) -> None:
        source = NORMAL_CSV.read_text(encoding="utf-8").splitlines()
        rows = [line.split(",") for line in source[1:4]]
        for row, sample_time in zip(rows, ("0", "0.02", "0.04")):
            row[0] = sample_time
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "short.csv"
            path.write_text("\n".join([source[0], *(",".join(row) for row in rows)]) + "\n", encoding="utf-8")
            observed: list[float] = []
            finished = threading.Event()
            player = CanScenarioPlayer(
                lambda scenario: None,
                lambda row: observed.append(time.monotonic()),
                lambda stopped: finished.set(),
            )
            player.start(path, "short")
            self.assertTrue(finished.wait(1.0))
            self.assertEqual(len(observed), 3)
            self.assertGreaterEqual(observed[-1] - observed[0], 0.025)
            self.assertAlmostEqual(player.status()[1], 0.06)
            path.unlink()
            finished.clear()
            player.restart()  # The CSV is not read again.
            self.assertTrue(finished.wait(1.0))
            self.assertEqual(len(observed), 6)

    def test_csv_validation(self) -> None:
        self.assertEqual(
            {path.name for path in DATA_DIR.glob("can_*.csv")},
            {path.name for path in (NORMAL_CSV, CAUTION_CSV, DISTRACTION_CSV, DANGER_CSV)},
        )
        scenario = load_can_scenario(NORMAL_CSV)
        self.assertEqual(len(scenario.rows), 75)
        self.assertEqual(scenario.rows[0].signals.time_s, 0.0)
        self.assertAlmostEqual(scenario.duration_s, 15.0)

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "invalid.csv"
            source = NORMAL_CSV.read_text(encoding="utf-8")
            lines = source.splitlines()
            path.write_text("time_s,vehicle_speed_kph\n0,60\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing columns"):
                load_can_scenario(path)

            header = next(csv.reader([lines[0]]))
            rows = list(csv.reader(lines[1:]))
            rows[1][header.index("time_s")] = "0"
            self._write_rows(path, header, rows)
            with self.assertRaisesRegex(ValueError, "time_s must increase"):
                load_can_scenario(path)

            rows[1][header.index("time_s")] = "0.2"
            rows[1][header.index("yaw_rate_dps")] = ""
            self._write_rows(path, header, rows)
            with self.assertRaisesRegex(ValueError, "missing value"):
                load_can_scenario(path)

    def test_server_exposes_exactly_four_can_commands(self) -> None:
        with (patch("sys.argv", ["vehicle_tcp_server.py"]),
              patch("vehicle_tcp_server.server_loop"),
              patch("vehicle_tcp_server.console_loop") as console):
            main()
        can_paths = console.call_args.args[4]
        self.assertEqual(can_paths, {
            "normal": NORMAL_CSV,
            "caution": CAUTION_CSV,
            "distraction": DISTRACTION_CSV,
            "danger": DANGER_CSV,
        })

    def test_scenario_speed_and_wheel_signals_are_consistent(self) -> None:
        for path in (NORMAL_CSV, CAUTION_CSV, DISTRACTION_CSV, DANGER_CSV):
            scenario = load_can_scenario(path)
            self.assertEqual(len(scenario.rows), 90 if path == CAUTION_CSV else 75)
            previous_odometer = float("-inf")
            for row in scenario.rows:
                signals = row.signals
                mean_wheel_speed = sum((signals.wheel_speed_fl_kph,
                                        signals.wheel_speed_fr_kph,
                                        signals.wheel_speed_rl_kph,
                                        signals.wheel_speed_rr_kph)) / 4
                self.assertAlmostEqual(mean_wheel_speed, signals.vehicle_speed_kph,
                                       delta=0.04)
                self.assertGreaterEqual(signals.odometer_km, previous_odometer)
                self.assertGreater(signals.engine_rpm, 1000)
                previous_odometer = signals.odometer_km

    def test_straight_segments_do_not_command_steering(self) -> None:
        for path, onset in ((NORMAL_CSV, 15.0), (CAUTION_CSV, 3.0),
                            (DISTRACTION_CSV, 5.0), (DANGER_CSV, 7.0)):
            scenario = load_can_scenario(path)
            self.assertTrue(all(row.signals.steering_angle_deg == 0.0
                                for row in scenario.rows if row.signals.time_s < onset))

    @staticmethod
    def _estimated_lateral_poses(path: Path) -> dict[float, tuple[float, float]]:
        heading = 0.0
        lateral = 0.0
        poses = {}
        for row in load_can_scenario(path).rows:
            signals = row.signals
            speed_mps = signals.vehicle_speed_kph / 3.6
            steering_limit = 1.0 - 0.55 * min(signals.vehicle_speed_kph / 120.0, 1.0)
            wheel_angle = math.radians(signals.steering_angle_deg * steering_limit)
            heading += speed_mps * math.tan(wheel_angle) / 2.7 * 0.2
            lateral += speed_mps * math.sin(heading) * 0.2
            poses[signals.time_s] = (lateral, math.degrees(heading))
        return poses

    def test_drowsy_scenario_drifts_three_times_and_returns_to_lane(self) -> None:
        scenario = load_can_scenario(CAUTION_CSV)
        self.assertEqual(len(scenario.rows), 90)
        self.assertAlmostEqual(scenario.duration_s, 18.0)
        rows = {row.signals.time_s: row for row in scenario.rows}
        for sleep_start, wake_up, last_sleep_sample, last_recovery_sample in (
            (3.0, 5.0, 4.8, 7.8),
            (8.0, 11.0, 10.8, 12.8),
            (13.0, 16.0, 15.8, 17.8),
        ):
            self.assertEqual(rows[sleep_start].ground_truth_state, "Caution")
            self.assertEqual(rows[last_sleep_sample].ground_truth_state, "Caution")
            self.assertEqual(rows[wake_up].ground_truth_state, "Normal")
            self.assertNotEqual(rows[wake_up].signals.steering_angle_deg, 0.0)
            self.assertEqual(rows[last_recovery_sample].ground_truth_state, "Normal")
        self.assertLessEqual(max(abs(row.signals.steering_angle_deg)
                                 for row in scenario.rows), 2.7)
        poses = self._estimated_lateral_poses(CAUTION_CSV)
        for drift_end, recovery_end in ((4.8, 7.8), (10.8, 12.8), (15.8, 17.8)):
            self.assertLess(poses[drift_end][0], -1.75)
            self.assertAlmostEqual(poses[recovery_end][0], 0.0, delta=0.1)
            self.assertAlmostEqual(poses[recovery_end][1], 0.0, delta=0.2)

    def test_distraction_holds_steering_before_late_return(self) -> None:
        scenario = load_can_scenario(DISTRACTION_CSV)
        steering = {row.signals.time_s: row.signals.steering_angle_deg
                    for row in scenario.rows}
        self.assertTrue(all(steering[index / 5] == 0.8 for index in range(27, 35)))
        self.assertEqual(steering[5.0], 0.2)
        self.assertEqual(steering[7.0], -1.0)
        self.assertEqual(steering[8.6], 0.0)
        self.assertGreater(self._estimated_lateral_poses(DISTRACTION_CSV)[8.4][0], 1.75)
        self.assertTrue(all(row.ground_truth_state == "Caution" for row in scenario.rows
                            if 5.0 <= row.signals.time_s <= 8.4))
        with patch("sys.argv", ["vehicle_tcp_server.py"]):
            self.assertEqual(parse_args().can_distraction_csv, DISTRACTION_CSV)

    def test_cardiac_scenario_combines_steering_and_acceleration(self) -> None:
        scenario = load_can_scenario(DANGER_CSV)
        self.assertEqual(len(scenario.rows), 75)
        self.assertEqual(max(abs(row.signals.steering_angle_deg)
                             for row in scenario.rows), 3.0)
        rows = {row.signals.time_s: row for row in scenario.rows}
        self.assertEqual(scenario.ground_truth_danger_time_s, 8.0)
        self.assertEqual(rows[7.0].signals.steering_angle_deg, 0.2)
        self.assertLess(rows[7.2].signals.steering_angle_deg, 0.0)
        self.assertGreater(rows[7.4].signals.steering_angle_deg, 0.0)
        self.assertEqual(rows[7.8].ground_truth_state, "Normal")
        self.assertLess(rows[7.8].signals.accelerator_pedal_pct, 30.0)
        self.assertEqual(rows[8.0].signals.accelerator_pedal_pct, 75.0)
        self.assertGreater(rows[8.0].signals.steering_angle_deg, 1.0)
        detector = CanAnomalyDetector()
        decisions = {
            row.signals.time_s: detector.update(row.signals)
            for row in scenario.rows
        }
        self.assertFalse(any(decision.can_anomaly for time_s, decision in decisions.items()
                             if time_s < 8.0))
        self.assertGreater(decisions[8.2].pedal_rise_pct_s, 80)
        self.assertGreater(decisions[8.2].raw_longitudinal_accel_g, 0.05)
        self.assertFalse(decisions[8.2].can_anomaly)
        self.assertEqual(decisions[8.4].decision_time_s, 8.4)
        self.assertTrue(decisions[8.4].steer_anomaly)
        self.assertTrue(decisions[8.6].accel_anomaly)
        self.assertTrue(all(row.signals.steering_angle_deg == 0.0
                            for row in scenario.rows if row.signals.time_s >= 9.2))
        with patch("sys.argv", ["vehicle_tcp_server.py"]):
            self.assertEqual(parse_args().can_danger_csv, DANGER_CSV)

    def test_caution_csv_replays_without_publishing_final_state(self) -> None:
        with patch("sys.argv", ["vehicle_tcp_server.py"]):
            path = parse_args().can_caution_csv
        self.assertEqual(path, CAUTION_CSV)

        scenario = load_can_scenario(path, "caution")
        self.assertEqual(len(scenario.rows), 90)
        caution_times = [row.signals.time_s for row in scenario.rows
                         if row.ground_truth_state == "Caution"]
        self.assertEqual((min(caution_times), max(caution_times)), (3.0, 15.8))

        shared = SharedData()
        adapter = CanAdapter(shared)
        adapter.on_start(scenario)
        for row in scenario.rows:
            adapter.on_sample(row)
            if row.signals.time_s == 3.4:
                command, _ = shared.snapshot()
                self.assertEqual(command.can_scenario, "caution")
                self.assertLess(command.steering, 0.0)
                self.assertEqual(command.driver_state, "Normal")
        command, _ = shared.snapshot()
        self.assertEqual(command.driver_state, "Normal")
        self.assertFalse(command.steer_correction_active)
        self.assertFalse(command.accel_limit_active)
        self.assertFalse(shared.can_control_locked)
        adapter.on_finish(False)
        self.assertEqual(shared.snapshot()[0].control_mode, "cruise")

    @staticmethod
    def _write_rows(path: Path, header: list[str], rows: list[list[str]]) -> None:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(header)
        writer.writerows(rows)
        path.write_text(buffer.getvalue(), encoding="utf-8")

    def test_normal_and_danger_detection_without_ground_truth(self) -> None:
        for path, expected_any in ((NORMAL_CSV, False), (CAUTION_CSV, True),
                                   (DISTRACTION_CSV, False), (DANGER_CSV, True)):
            scenario = load_can_scenario(path)
            detector = CanAnomalyDetector()
            decisions = [detector.update(row.signals) for row in scenario.rows]
            self.assertEqual(any(decision.can_anomaly for decision in decisions), expected_any)
            if path == DANGER_CSV:
                self.assertTrue(all(not decision.can_anomaly for row, decision
                                    in zip(scenario.rows, decisions)
                                    if row.signals.time_s < 8.0))

        # The detector only receives CanSignals, which has no ground_truth_* fields.
        scenario = load_can_scenario(DANGER_CSV)
        changed_truth = [replace(row, ground_truth_state="Normal", ground_truth_event="none")
                         for row in scenario.rows]
        detector = CanAnomalyDetector()
        self.assertEqual(
            [detector.update(row.signals).can_anomaly for row in changed_truth][-1], True,
        )

    def test_can_danger_replays_without_classifying_driver(self) -> None:
        shared = SharedData()
        adapter = CanAdapter(shared)
        scenario = load_can_scenario(DANGER_CSV, "danger")
        adapter.on_start(scenario)
        for row in scenario.rows:
            adapter.on_sample(row)
            if row.signals.time_s == 8.6:
                command, _ = shared.snapshot()
                self.assertGreater(command.steering, 0.0)
                self.assertGreater(command.throttle, 0.9)
                self.assertTrue(command.steer_anomaly)
                self.assertTrue(command.accel_anomaly)
                self.assertFalse(command.steer_correction_active)
                self.assertFalse(command.accel_limit_active)
        command, _ = shared.snapshot()
        self.assertEqual(command.driver_state, "Normal")
        self.assertFalse(shared.can_control_locked)
        self.assertGreater(command.throttle, 0.9)
        packet = json.loads(shared.next_command_json())
        self.assertEqual(packet["driver_state"], "Normal")
        self.assertTrue(packet["can_anomaly"])
        self.assertTrue(packet["accel_anomaly"])
        adapter.on_finish(False)
        self.assertEqual(shared.snapshot()[0].driver_state, "Normal")

    def test_external_danger_still_locks_can_controls(self) -> None:
        shared = SharedData()
        adapter = CanAdapter(shared)
        scenario = load_can_scenario(DANGER_CSV, "danger")
        adapter.on_start(scenario)
        for row in scenario.rows:
            adapter.on_sample(row)
            if row.signals.time_s == 8.4:
                publish_prediction(shared, "Danger")
        command, _ = shared.snapshot()
        self.assertEqual(command.driver_state, "Danger")
        self.assertTrue(shared.can_control_locked)
        self.assertEqual((command.throttle, command.brake, command.steering),
                         (0.0, 0.0, 0.0))
        self.assertGreater(command.can_throttle, 0.9)
        adapter.on_finish(False)
        self.assertEqual(shared.snapshot()[0].driver_state, "Danger")

    def test_can_finish_does_not_reset_external_caution(self) -> None:
        shared = SharedData()
        publish_prediction(shared, "Caution")
        adapter = CanAdapter(shared)
        scenario = load_can_scenario(NORMAL_CSV, "normal")
        adapter.on_start(scenario)
        adapter.on_sample(scenario.rows[0])
        adapter.on_finish(False)
        self.assertEqual(shared.snapshot()[0].driver_state, "Caution")

    def test_all_can_scenarios_hold_actual_speed_during_steady_driving(self) -> None:
        for path in (NORMAL_CSV, CAUTION_CSV, DISTRACTION_CSV, DANGER_CSV):
            shared = SharedData()
            shared.set_state({"speed_kph": 60.0, "emergency_phase": "Idle"})
            adapter = CanAdapter(shared)
            scenario = load_can_scenario(path, path.stem)
            adapter.on_start(scenario)
            adapter.on_sample(scenario.rows[0])
            command, _ = shared.snapshot()
            self.assertEqual((command.throttle, command.brake), (0.0, 0.0))
            self.assertAlmostEqual(command.can_throttle, 0.22)
            self.assertEqual(command.control_mode, "remote")

            shared.set_state({"speed_kph": 55.0, "emergency_phase": "Idle"})
            adapter.on_sample(scenario.rows[1])
            command, _ = shared.snapshot()
            self.assertAlmostEqual(command.throttle, 0.4)
            self.assertEqual(command.brake, 0.0)

            shared.set_state({"speed_kph": 65.0, "emergency_phase": "Idle"})
            adapter.on_sample(scenario.rows[2])
            command, _ = shared.snapshot()
            self.assertEqual(command.throttle, 0.0)
            self.assertAlmostEqual(command.brake, 0.5)

    def test_danger_switches_from_speed_hold_to_csv_acceleration(self) -> None:
        shared = SharedData()
        shared.set_state({"speed_kph": 60.0, "emergency_phase": "Idle"})
        adapter = CanAdapter(shared)
        scenario = load_can_scenario(DANGER_CSV, "danger")
        adapter.on_start(scenario)
        for row in scenario.rows:
            if row.signals.time_s > 8.2:
                break
            adapter.on_sample(row)
            if row.signals.time_s == 7.8:
                command, _ = shared.snapshot()
                self.assertEqual((command.throttle, command.brake), (0.0, 0.0))
                self.assertNotEqual(command.steering, 0.0)
            if row.signals.time_s == 8.0:
                command, _ = shared.snapshot()
                self.assertEqual(command.throttle, 0.75)
                self.assertEqual(command.can_throttle, 0.75)
            if row.signals.time_s == 8.2:
                command, _ = shared.snapshot()
                self.assertEqual(command.throttle, 0.95)
                self.assertEqual(command.can_throttle, 0.95)

    def test_steering_events_keep_speed_feedback(self) -> None:
        for path, event_time in ((CAUTION_CSV, 5.0),
                                 (DISTRACTION_CSV, 6.0),
                                 (DANGER_CSV, 7.4)):
            shared = SharedData()
            shared.set_state({"speed_kph": 60.0, "emergency_phase": "Idle"})
            adapter = CanAdapter(shared)
            scenario = load_can_scenario(path, path.stem)
            adapter.on_start(scenario)
            event_row = next(row for row in scenario.rows
                             if row.signals.time_s == event_time)
            adapter.on_sample(event_row)
            command, _ = shared.snapshot()
            self.assertNotEqual(command.steering, 0.0)
            self.assertEqual((command.throttle, command.brake), (0.0, 0.0))

    def test_speed_hold_waits_for_vehicle_feedback(self) -> None:
        shared = SharedData()
        adapter = CanAdapter(shared)
        scenario = load_can_scenario(NORMAL_CSV, "normal")
        adapter.on_start(scenario)
        adapter.on_sample(scenario.rows[0])
        command, _ = shared.snapshot()
        self.assertEqual((command.throttle, command.brake), (0.0, 0.0))
        self.assertAlmostEqual(command.can_throttle, 0.22)

    def test_finish_releases_control_without_holding_stale_input(self) -> None:
        scenario = load_can_scenario(NORMAL_CSV, "normal")
        shared = SharedData()
        shared.set_state({"speed_kph": 55.0, "emergency_phase": "Idle"})
        adapter = CanAdapter(shared, CanPlaybackConfig())
        adapter.on_start(scenario)
        adapter.on_sample(scenario.rows[0])
        adapter.on_finish(False)
        command, _ = shared.snapshot()
        self.assertFalse(command.can_active)
        self.assertEqual(command.control_mode, "cruise")
        self.assertEqual(command.throttle, 0.0)
        self.assertAlmostEqual(command.can_throttle, 0.22)
        adapter.on_finish(True)
        command, _ = shared.snapshot()
        self.assertEqual(command.control_mode, "cruise")
        self.assertEqual(command.throttle, 0.0)

    def test_unity_emergency_must_reset_before_can_restart(self) -> None:
        shared = SharedData()
        shared.set_state({"emergency_phase": "Stopped"})
        with self.assertRaisesRegex(ValueError, "R 초기화"):
            shared.begin_can_playback("normal")
        shared.set_state({"emergency_phase": "Idle"})
        shared.begin_can_playback("normal")
        publish_prediction(shared, "Danger")
        self.assertTrue(shared.can_control_locked)
        with self.assertRaisesRegex(ValueError, "state normal"):
            shared.begin_can_playback("normal")
        shared.finish_can_playback(release_control=True)
        shared.set_state({"emergency_phase": "Stopped"})
        publish_prediction(shared, "Normal")
        self.assertTrue(shared.can_control_locked)
        self.assertEqual(shared.snapshot()[0].control_mode, "cruise")
        with self.assertRaisesRegex(ValueError, "R 초기화"):
            shared.begin_can_playback("normal")
        shared.set_state({"emergency_phase": "Idle"})
        shared.begin_can_playback("normal")
        self.assertFalse(shared.can_control_locked)

    @staticmethod
    def _signals(time_s: float, *, steer: float = 0.0, speed: float = 60.0,
                 pedal: float = 22.0, prior_speed: float | None = None):
        """Build kinematically consistent virtual CAN data without truth labels."""
        base = load_can_scenario(NORMAL_CSV).rows[0].signals
        factor = 1 - 0.55 * min(speed / 120, 1)
        yaw = speed / 3.6 * math.tan(math.radians(steer * factor)) / 2.7
        wheel_delta = yaw * 1.7 * 3.6
        accel_g = (0.0 if prior_speed is None else
                   (speed - prior_speed) / (3.6 * 0.2 * 9.81))
        return replace(base, time_s=time_s, steering_angle_deg=steer,
                       vehicle_speed_kph=speed, accelerator_pedal_pct=pedal,
                       longitudinal_accel_g=accel_g,
                       lateral_accel_g=speed / 3.6 * yaw / 9.81,
                       yaw_rate_dps=math.degrees(yaw),
                       wheel_speed_fl_kph=speed + wheel_delta / 2,
                       wheel_speed_rl_kph=speed + wheel_delta / 2,
                       wheel_speed_fr_kph=speed - wheel_delta / 2,
                       wheel_speed_rr_kph=speed - wheel_delta / 2)

    def test_consistent_normal_maneuvers_do_not_trigger(self) -> None:
        for maneuver in ("straight", "lane_change", "curve", "acceleration"):
            detector = CanAnomalyDetector()
            previous_speed = None
            for index in range(75):
                t = index * 0.2
                steer = (0.9 * math.sin(2 * math.pi * t / 5)
                         if maneuver == "lane_change" else
                         min(1.2, t * 0.3) if maneuver == "curve" else 0)
                speed = 60 + min(10, t * 2) if maneuver == "acceleration" else 60
                pedal = 22 + min(20, t * 4) if maneuver == "acceleration" else 22
                sample = self._signals(t, steer=steer, speed=speed, pedal=pedal,
                                       prior_speed=previous_speed)
                self.assertFalse(detector.update(sample).can_anomaly, (maneuver, t))
                previous_speed = speed

    def test_transient_steer_and_held_steer_with_hysteretic_release(self) -> None:
        detector = CanAnomalyDetector()
        decisions = {}
        for index in range(45):
            t = round(index * 0.2, 1)
            steer = (3.0 if t == 1.0 else
                     min(5.0, (t - 2.0) * 2.5) if 2.0 <= t < 5.0 else
                     5.0 if 5.0 <= t < 6.0 else 0.0)
            decisions[t] = detector.update(self._signals(t, steer=steer))
        self.assertFalse(decisions[1.0].steer_anomaly)
        self.assertFalse(decisions[1.2].steer_anomaly)
        self.assertTrue(decisions[5.0].steer_anomaly)
        self.assertEqual(decisions[5.0].steer_reason,
                         "sustained_high_lateral_accel")
        self.assertTrue(decisions[6.8].steer_anomaly)
        self.assertFalse(decisions[7.2].steer_anomaly)
        self.assertEqual(decisions[7.2].steer_release_reason,
                         "raw_steering_normal_for_release_seconds")

    def test_caution_corrects_each_channel_without_changing_driver_state(self) -> None:
        shared = SharedData()
        shared.set_state({"speed_kph": 60.0, "emergency_phase": "Idle"})
        publish_prediction(shared, "Caution")
        adapter = CanAdapter(shared)
        danger = load_can_scenario(DANGER_CSV)
        adapter.on_start(danger)
        for row in danger.rows:
            adapter.on_sample(row)
            if row.signals.time_s == 8.6:
                command, _ = shared.snapshot()
                self.assertEqual(command.driver_state, "Caution")
                self.assertTrue(command.steer_correction_active)
                self.assertTrue(command.accel_limit_active)
                self.assertLessEqual(abs(command.steering), 0.08)
                self.assertLess(command.throttle, command.can_throttle)
                self.assertLessEqual(command.throttle, 0.35)
                self.assertGreater(command.can_steering, command.steering)
                self.assertGreater(command.can_throttle, command.throttle)
            if row.signals.time_s == 8.8:
                command, _ = shared.snapshot()
                self.assertLessEqual(command.throttle, 0.35)
                publish_prediction(shared, "Normal")
            if row.signals.time_s == 9.0:
                command, _ = shared.snapshot()
                self.assertFalse(command.steer_correction_active)
                self.assertFalse(command.accel_limit_active)
                self.assertTrue(command.steer_anomaly and command.accel_anomaly)
                publish_prediction(shared, "Danger")
            if row.signals.time_s == 9.2:
                command, _ = shared.snapshot()
                self.assertEqual((command.throttle, command.brake, command.steering),
                                 (0.0, 0.0, 0.0))
                self.assertTrue(shared.can_control_locked)
                break

        steering_only = load_can_scenario(CAUTION_CSV)
        second_shared = SharedData()
        publish_prediction(second_shared, "Caution")
        second = CanAdapter(second_shared)
        second.on_start(steering_only)
        for row in steering_only.rows:
            second.on_sample(row)
            if row.signals.time_s == 3.6:
                command, _ = second_shared.snapshot()
                self.assertTrue(command.steer_correction_active)
                self.assertFalse(command.accel_limit_active)
                break

        accel_only = CanScenario("accel_only", DANGER_CSV, tuple(
            replace(row, signals=replace(row.signals, steering_angle_deg=0.0))
            for row in danger.rows))
        third_shared = SharedData()
        publish_prediction(third_shared, "Caution")
        third = CanAdapter(third_shared)
        third.on_start(accel_only)
        for row in accel_only.rows:
            third.on_sample(row)
            if row.signals.time_s == 8.6:
                command, _ = third_shared.snapshot()
                self.assertFalse(command.steer_correction_active)
                self.assertTrue(command.accel_limit_active)
                self.assertEqual(command.steering, 0.0)
                break

    def test_restart_and_disconnect_clear_applied_can_control(self) -> None:
        scenario = load_can_scenario(DANGER_CSV)
        shared = SharedData()
        publish_prediction(shared, "Caution")
        adapter = CanAdapter(shared)
        adapter.on_start(scenario)
        for row in scenario.rows:
            adapter.on_sample(row)
            if row.signals.time_s == 8.6:
                break
        self.assertTrue(shared.snapshot()[0].accel_limit_active)
        adapter.on_start(scenario)
        adapter.on_sample(scenario.rows[0])
        self.assertFalse(shared.snapshot()[0].can_anomaly)
        shared.on_unity_disconnect()
        command, state = shared.snapshot()
        self.assertIsNone(state)
        self.assertFalse(command.can_active)
        self.assertEqual(command.control_mode, "cruise")
        adapter.on_sample(scenario.rows[1])
        self.assertEqual(shared.snapshot()[0].control_mode, "cruise")

    def test_accel_held_and_normal_return_require_raw_release_time(self) -> None:
        detector = CanAnomalyDetector()
        decisions = {}
        previous_speed = None
        for index in range(26):
            t = round(index * 0.2, 1)
            pedal = (22 if t < 1.0 else 75 if t == 1.0 else
                     95 if t < 3.0 or t == 3.2 else 22)
            speed = 60 + min(3.0, max(0.0, t - 1.0) * 3.0)
            sample = self._signals(t, speed=speed, pedal=pedal,
                                   prior_speed=previous_speed)
            decisions[t] = detector.update(sample)
            previous_speed = speed
        self.assertFalse(decisions[1.2].accel_anomaly)
        self.assertTrue(decisions[1.6].accel_anomaly)
        self.assertTrue(decisions[2.8].accel_anomaly)  # pedal rate is zero
        self.assertTrue(decisions[3.0].accel_anomaly)
        self.assertTrue(decisions[4.2].accel_anomaly)  # 3.2 interruption reset timer
        self.assertFalse(decisions[4.4].accel_anomaly)
        self.assertTrue(decisions[4.4].accel_release_reason)

    def test_invalid_or_duplicate_raw_samples_cannot_trigger(self) -> None:
        detector = CanAnomalyDetector()
        first = self._signals(0.0)
        self.assertFalse(detector.update(first).can_anomaly)
        with self.assertRaisesRegex(ValueError, "increasing time_s"):
            detector.update(first)
        with self.assertRaisesRegex(ValueError, "Invalid raw CAN sample"):
            detector.update(replace(first, time_s=0.2, longitudinal_accel_g=float("nan")))
        self.assertFalse(detector.update(self._signals(0.2)).can_anomaly)

    def test_normal_detection_can_be_corrected_after_late_caution(self) -> None:
        shared = SharedData()
        shared.set_state({"speed_kph": 60.0, "emergency_phase": "Idle"})
        adapter = CanAdapter(shared)
        scenario = load_can_scenario(DANGER_CSV)
        adapter.on_start(scenario)
        for row in scenario.rows:
            adapter.on_sample(row)
            if row.signals.time_s == 8.6:
                before, _ = shared.snapshot()
                self.assertTrue(before.steer_anomaly and before.accel_anomaly)
                self.assertFalse(before.steer_correction_active or before.accel_limit_active)
                publish_prediction(shared, "Caution")
            if row.signals.time_s == 8.8:
                after, _ = shared.snapshot()
                self.assertTrue(after.steer_correction_active and after.accel_limit_active)
                self.assertLess(after.throttle, after.can_throttle)
                break

    def test_caution_uses_unity_pose_and_speed_to_recover_motion(self) -> None:
        shared = SharedData()
        shared.set_state({"speed_kph": 60.0, "heading_deg": 0.0,
                          "position": {"x": 0.8, "z": 0.0},
                          "emergency_phase": "Idle"})
        publish_prediction(shared, "Caution")
        adapter = CanAdapter(shared)
        adapter.on_start(load_can_scenario(DANGER_CSV))
        for row in adapter.scenario.rows:
            if row.signals.time_s == 10.6:
                shared.set_state({"speed_kph": 60.0, "heading_deg": 0.0,
                                  "position": {"x": 0.8, "z": 185.0},
                                  "emergency_phase": "Idle"})
            elif row.signals.time_s >= 8.4:
                shared.set_state({"speed_kph": 75.0, "heading_deg": 10.0,
                                  "position": {"x": 2.0, "z": 150.0},
                                  "emergency_phase": "Idle"})
            adapter.on_sample(row)
            if row.signals.time_s == 8.6:
                command, _ = shared.snapshot()
                self.assertGreater(command.can_steering, 0.0)
                self.assertLess(command.steering, 0.0)  # counter-steer
                self.assertEqual(command.throttle, 0.0)
                self.assertGreater(command.brake, 0.0)
            if row.signals.time_s == 10.4:
                command, _ = shared.snapshot()
                self.assertFalse(command.steer_anomaly)
                self.assertTrue(command.steer_correction_active)  # pose still offset
            if row.signals.time_s == 10.6:
                command, _ = shared.snapshot()
                self.assertFalse(command.steer_correction_active)
                break

    def test_geometry_aware_unity_guard_owns_steering_without_python_double_correction(self) -> None:
        shared = SharedData()
        shared.set_state({"speed_kph": 75.0, "heading_deg": 10.0,
                          "position": {"x": 2.0, "z": 150.0},
                          "lane_geometry_ready": True, "lane_assist_active": True,
                          "lane_applied_steering": -0.06, "emergency_phase": "Idle"})
        publish_prediction(shared, "Caution")
        adapter = CanAdapter(shared)
        scenario = load_can_scenario(DANGER_CSV)
        adapter.on_start(scenario)
        for row in scenario.rows:
            adapter.on_sample(row)
            if row.signals.time_s == 8.6:
                command, _ = shared.snapshot()
                self.assertTrue(command.steer_correction_active)
                self.assertTrue(command.steer_anomaly and command.accel_anomaly)
                self.assertEqual(command.steering, command.can_steering)
                self.assertGreater(command.can_steering, 0.0)
                self.assertEqual(command.throttle, 0.0)
                self.assertGreater(command.brake, 0.0)
                publish_prediction(shared, "Danger")
            if row.signals.time_s == 8.8:
                command, _ = shared.snapshot()
                self.assertFalse(command.steer_correction_active)
                self.assertEqual(command.steering, 0.0)
                self.assertEqual(command.driver_state, "Danger")
                break

    def test_recovery_verification_rejects_frozen_and_unrecovered_vehicles(self) -> None:
        from verify_can_lane import recovery_observed

        measured = {"peak_center_offset_m": 0.6, "final_center_offset_m": 0.03,
                    "final_heading_error_deg": 0.1, "applied_steering_range": [-0.07, 0.08]}
        self.assertTrue(recovery_observed(measured))
        self.assertFalse(recovery_observed({**measured, "peak_center_offset_m": 0.04}))
        self.assertFalse(recovery_observed({**measured, "final_center_offset_m": 0.4}))
        self.assertFalse(recovery_observed({**measured, "applied_steering_range": [0.0, 0.08]}))


if __name__ == "__main__":
    unittest.main()
