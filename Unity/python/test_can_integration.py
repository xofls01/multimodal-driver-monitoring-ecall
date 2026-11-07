"""Offline CAN replay/anomaly tests; Unity is not required."""

import csv
import io
import json
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from can_adapter import CanAdapter, CanPlaybackConfig
from can_anomaly import CanAnomalyDetector
from can_scenario import CanScenarioPlayer, load_can_scenario
from vehicle_tcp_server import SharedData, parse_args, publish_prediction


DATA_DIR = Path(__file__).resolve().parents[1] / "data"
NORMAL_CSV = DATA_DIR / "can_normal_60kmh_15s.csv"
DANGER_CSV = DATA_DIR / "can_danger_steer_accel_15s.csv"
PULSE_DANGER_CSV = DATA_DIR / "can_danger_steer_accel_pulse_15s.csv"


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

    def test_straight_segments_do_not_command_steering(self) -> None:
        normal = load_can_scenario(NORMAL_CSV)
        danger = load_can_scenario(DANGER_CSV)
        self.assertTrue(all(row.signals.steering_angle_deg == 0.0
                            for row in normal.rows))
        self.assertTrue(all(row.signals.steering_angle_deg == 0.0
                            for row in danger.rows if row.signals.time_s < 8.0))
        self.assertGreater(next(row.signals.steering_angle_deg
                                for row in danger.rows if row.signals.time_s == 8.2), 0.0)

    def test_danger_steering_is_small_and_returns_to_center(self) -> None:
        scenario = load_can_scenario(DANGER_CSV)
        steering_by_time = {
            row.signals.time_s: row.signals.steering_angle_deg
            for row in scenario.rows
        }
        self.assertLessEqual(max(abs(angle) for angle in steering_by_time.values()), 1.5)
        self.assertGreater(steering_by_time[8.4], 0.0)
        self.assertLess(steering_by_time[9.0], 0.0)
        self.assertTrue(all(angle == 0.0 for sample_time, angle in steering_by_time.items()
                            if sample_time >= 9.4))
        self.assertLessEqual(max(abs(angle / 30.0) for angle in steering_by_time.values()), 0.05)

    def test_pulse_danger_combines_rapid_steering_and_acceleration(self) -> None:
        scenario = load_can_scenario(PULSE_DANGER_CSV)
        self.assertEqual(len(scenario.rows), 75)
        self.assertEqual(max(abs(row.signals.steering_angle_deg)
                             for row in scenario.rows), 8.0)
        detector = CanAnomalyDetector()
        decisions = {
            row.signals.time_s: detector.update(row.signals)
            for row in scenario.rows
        }
        self.assertIn("rapid_steering", decisions[8.4].active_conditions)
        self.assertIn("rapid_acceleration_pedal", decisions[8.4].active_conditions)
        self.assertFalse(decisions[8.6].can_anomaly)
        self.assertEqual(decisions[8.8].decision_time_s, 8.8)
        self.assertTrue(all(row.signals.steering_angle_deg == 0.0
                            for row in scenario.rows if row.signals.time_s >= 9.4))
        with patch("sys.argv", ["vehicle_tcp_server.py"]):
            self.assertEqual(parse_args().can_danger_csv, PULSE_DANGER_CSV)

    @staticmethod
    def _write_rows(path: Path, header: list[str], rows: list[list[str]]) -> None:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(header)
        writer.writerows(rows)
        path.write_text(buffer.getvalue(), encoding="utf-8")

    def test_normal_and_danger_detection_without_ground_truth(self) -> None:
        for path, expected_time in ((NORMAL_CSV, None), (DANGER_CSV, 8.8),
                                    (PULSE_DANGER_CSV, 8.8)):
            scenario = load_can_scenario(path)
            detector = CanAnomalyDetector()
            decisions = [detector.update(row.signals) for row in scenario.rows]
            self.assertEqual(decisions[-1].decision_time_s, expected_time)
            self.assertEqual(decisions[-1].can_anomaly, expected_time is not None)
            if expected_time is None:
                self.assertLess(max(result.can_risk_score for result in decisions), 2)
            else:
                self.assertTrue(all(not decision.can_anomaly for row, decision
                                    in zip(scenario.rows, decisions)
                                    if row.signals.time_s < 8.8))

        # The detector only receives CanSignals, which has no ground_truth_* fields.
        scenario = load_can_scenario(DANGER_CSV)
        changed_truth = [replace(row, ground_truth_state="Normal", ground_truth_event="none")
                         for row in scenario.rows]
        detector = CanAnomalyDetector()
        self.assertEqual(
            [detector.update(row.signals).can_anomaly for row in changed_truth][-1], True,
        )

    def test_demo_danger_locks_can_controls(self) -> None:
        shared = SharedData()
        adapter = CanAdapter(shared, publish_prediction, CanPlaybackConfig(demo_mode=True))
        scenario = load_can_scenario(DANGER_CSV, "danger")
        adapter.on_start(scenario)
        for row in scenario.rows:
            adapter.on_sample(row)
        command, _ = shared.snapshot()
        self.assertEqual(command.driver_state, "Danger")
        self.assertTrue(shared.can_control_locked)
        self.assertEqual((command.throttle, command.brake, command.steering), (0.0, 0.0, 0.0))
        self.assertGreater(command.can_throttle, 0.9)  # Raw CAN telemetry is retained.
        packet = json.loads(shared.next_command_json())
        self.assertEqual((packet["throttle"], packet["brake"], packet["steering"]),
                         (0.0, 0.0, 0.0))
        self.assertGreater(packet["can_throttle"], 0.9)
        self.assertEqual(shared.can_decision.decision_time_s, 8.8)
        adapter.on_finish(False)
        self.assertEqual(shared.snapshot()[0].driver_state, "Danger")

    def test_general_mode_preserves_final_state(self) -> None:
        shared = SharedData()
        adapter = CanAdapter(shared, publish_prediction, CanPlaybackConfig(demo_mode=False))
        scenario = load_can_scenario(DANGER_CSV, "danger")
        adapter.on_start(scenario)
        for row in scenario.rows:
            adapter.on_sample(row)
        command, _ = shared.snapshot()
        self.assertTrue(command.can_anomaly)
        self.assertEqual(command.driver_state, "Normal")
        self.assertFalse(shared.can_control_locked)
        self.assertGreater(command.throttle, 0.9)
        self.assertEqual(command.steering, 0.0)

    def test_stop_releases_control_and_hold_keeps_last_input(self) -> None:
        scenario = load_can_scenario(NORMAL_CSV, "normal")
        shared = SharedData()
        adapter = CanAdapter(shared, publish_prediction,
                             CanPlaybackConfig(end_behavior="hold"))
        adapter.on_start(scenario)
        adapter.on_sample(scenario.rows[0])
        adapter.on_finish(False)
        command, _ = shared.snapshot()
        self.assertFalse(command.can_active)
        self.assertEqual(command.control_mode, "remote")
        self.assertAlmostEqual(command.throttle, 0.22)
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


if __name__ == "__main__":
    unittest.main()
