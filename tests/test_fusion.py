"""통합 판단 시나리오 테스트. 실행: python -m unittest tests.test_fusion -v"""
import csv
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from biosignal import BioSignalMonitor, Config
from biosignal import simulate as sim
from fusion import FusionConfig, FusionEngine, can_from_csv_row, can_from_unity_state


@dataclass
class Bio:
    level: str = "normal"
    reasons: List[str] = field(default_factory=list)


@dataclass
class Vision:
    state: str = "normal"
    reasons: List[str] = field(default_factory=list)
    durations: Dict[str, float] = field(default_factory=dict)


def eyes_closed(seconds_closed):
    """비전과 같게, 2초가 지나야 사유가 붙는다."""
    if seconds_closed < 2.0:
        return Vision()
    return Vision("caution", ["eyes_closed"], {"eyes_closed": seconds_closed})


COLLAPSE = Vision("caution", ["collapse_posture"])
UNREADABLE = Vision("unavailable", ["face_unreadable"])


def wobble(t):
    """깨어 있는 운전자의 작은 조향: 1초마다 좌우로 움직인다."""
    return 0.05 if int(t) % 2 else -0.05


class Drive:
    """0.1초 단위로 세 입력을 넣고 판단을 모은다. 함수는 t를 받아 그 시각의 입력을 돌려준다."""

    def __init__(self, cfg: Optional[FusionConfig] = None):
        self.engine = FusionEngine(cfg)
        self.t = 0.0
        self.rows = []

    def run(self, seconds, bio: Optional[Callable] = None, vision: Optional[Callable] = None,
            steering: Optional[Callable] = wobble, throttle=lambda t: 0.3, brake=lambda t: 0.0,
            manual=True, can=True):
        start = self.t
        for _ in range(round(seconds * 10)):
            self.t = round(self.t + 0.1, 1)
            local = self.t - start
            if bio is not None:
                self.engine.push_bio(self.t, bio(local))
            if vision is not None:
                self.engine.push_vision(self.t, vision(local))
            if can:
                self.engine.push_can(self.t, steering(local) if steering else 0.0, throttle(local), brake(local),
                                     driver_input_valid=manual)
            self.rows.append(self.engine.update(self.t))
        return self.rows[-1]

    def warmed_up(self):
        self.run(20, bio=lambda t: Bio(), vision=lambda t: Vision())
        return self

    def first(self, level, after=0.0):
        return next((r.t for r in self.rows if r.level == level and r.t > after), None)

    def levels(self, after=0.0):
        return {r.level for r in self.rows if r.t > after}


NORMAL_BIO = lambda t: Bio()
NORMAL_VISION = lambda t: Vision()
NO_STEER = None


class Normal(unittest.TestCase):
    def test_all_normal(self):
        last = Drive().warmed_up().rows[-1]
        self.assertEqual((last.level, last.driver_state), ("normal", "N"))
        self.assertEqual(last.coverage, {"bio": True, "vision": True, "can": True})

    def test_no_steering_while_watching_road_is_not_a_warning(self):
        d = Drive().warmed_up()
        last = d.run(40, NORMAL_BIO, NORMAL_VISION, steering=NO_STEER)
        self.assertEqual(last.level, "normal")
        self.assertIn("CAN:NO_INPUT", last.reasons)

    def test_hard_brake_alone_stays_normal(self):
        d = Drive().warmed_up()
        last = d.run(2, NORMAL_BIO, NORMAL_VISION, brake=lambda t: 0.9 if t > 1 else 0.0)
        self.assertEqual(last.level, "normal")
        self.assertIn("CAN:HARD_BRAKE", last.reasons)


class EyesClosed(unittest.TestCase):
    def test_no_response_escalates_in_about_7_seconds(self):
        d = Drive().warmed_up()
        start = d.t
        d.run(5, NORMAL_BIO, NORMAL_VISION, steering=NO_STEER)  # 이미 조작이 끊긴 상태
        start = d.t
        last = d.run(15, NORMAL_BIO, lambda t: eyes_closed(t), steering=NO_STEER)
        self.assertEqual(last.level, "danger")
        self.assertEqual(last.driver_state, "E")
        self.assertAlmostEqual(d.first("caution", start) - start, 2.0, delta=0.2)
        self.assertAlmostEqual(d.first("danger", start) - start, 7.0, delta=0.2)

    def test_two_inputs_mean_driver_responded(self):
        d = Drive().warmed_up()
        last = d.run(9, NORMAL_BIO, lambda t: eyes_closed(t))
        self.assertEqual(last.level, "caution")
        self.assertNotIn("danger", d.levels())
        self.assertTrue(any("RESPONSE_CONFIRMED" in r.reasons for r in d.rows))
        self.assertEqual(last.driver_state, "D")

    def test_single_jerk_from_slumping_is_not_a_response(self):
        d = Drive().warmed_up()
        d.run(5, NORMAL_BIO, NORMAL_VISION, steering=NO_STEER)
        start = d.t
        last = d.run(15, NORMAL_BIO, lambda t: eyes_closed(t), steering=lambda t: 0.4 if t > 3 else 0.0)
        self.assertEqual(last.level, "danger")
        self.assertLess(d.first("danger", start) - start, 13.0)

    def test_face_lost_during_check_does_not_cancel_it(self):
        d = Drive().warmed_up()
        d.run(5, NORMAL_BIO, NORMAL_VISION, steering=NO_STEER)
        d.run(3, NORMAL_BIO, lambda t: eyes_closed(t + 2), steering=NO_STEER)
        last = d.run(12, NORMAL_BIO, lambda t: UNREADABLE, steering=NO_STEER)
        self.assertEqual(last.level, "danger")

    def test_eyes_reopening_cancels_check(self):
        d = Drive().warmed_up()
        d.run(5, NORMAL_BIO, NORMAL_VISION, steering=NO_STEER)
        d.run(3, NORMAL_BIO, lambda t: eyes_closed(t + 2), steering=NO_STEER)
        d.run(10, NORMAL_BIO, NORMAL_VISION, steering=NO_STEER)
        self.assertNotIn("danger", d.levels())
        self.assertEqual(d.rows[-1].level, "normal")

    def test_recovering_keeps_alert_category(self):
        d = Drive().warmed_up()
        d.run(3, NORMAL_BIO, lambda t: eyes_closed(t + 2))
        last = d.run(0.5, NORMAL_BIO, lambda t: Vision("caution", ["recovering"]))
        self.assertEqual((last.level, last.driver_state), ("caution", "D"))

    def test_cruise_without_input_channel_stays_within_euro_ncap_13s(self):
        d = Drive().warmed_up()
        d.run(2, NORMAL_BIO, NORMAL_VISION, manual=False)
        start = d.t
        last = d.run(15, NORMAL_BIO, lambda t: eyes_closed(t), manual=False)
        self.assertEqual(last.level, "danger")
        self.assertLessEqual(d.first("danger", start) - start, 13.0)

    def test_cruise_pedal_values_are_not_a_response(self):
        d = Drive().warmed_up()
        last = d.run(15, NORMAL_BIO, lambda t: eyes_closed(t), steering=lambda t: 0.0,
                     throttle=lambda t: 0.1 * (int(t) % 3), manual=False)
        self.assertEqual(last.level, "danger")

    def test_button_counts_as_response(self):
        d = Drive().warmed_up()
        d.run(3, NORMAL_BIO, lambda t: eyes_closed(t), manual=False)
        d.engine.push_button(d.t)
        d.run(1, NORMAL_BIO, lambda t: eyes_closed(t + 3), manual=False)
        self.assertTrue(any("RESPONSE_CONFIRMED" in r.reasons for r in d.rows))


class Biosignal(unittest.TestCase):
    def test_danger_plus_collapse_is_immediate(self):
        d = Drive().warmed_up()
        d.run(6, NORMAL_BIO, NORMAL_VISION, steering=NO_STEER)
        start = d.t
        last = d.run(0.2, lambda t: Bio("danger", ["NO_PULSE_CONFIRMED"]), lambda t: COLLAPSE, steering=NO_STEER)
        self.assertEqual(last.level, "danger")
        self.assertLessEqual(d.first("danger", start) - start, 0.2)
        self.assertIn("MULTIMODAL_INCAPACITATION", last.reasons)

    def test_second_opinion_resolved_by_vision(self):
        d = Drive().warmed_up()
        d.run(6, NORMAL_BIO, NORMAL_VISION, steering=NO_STEER)
        last = d.run(0.2, lambda t: Bio("caution", ["NO_PULSE_UNCONFIRMED"]),
                     lambda t: eyes_closed(3.5), steering=NO_STEER)
        self.assertEqual(last.level, "danger")

    def test_sensor_conflict_with_drowsy_eyes_is_not_immediate(self):
        d = Drive().warmed_up()
        d.run(6, NORMAL_BIO, NORMAL_VISION, steering=NO_STEER)
        last = d.run(1, lambda t: Bio("caution", ["SENSOR_CONFLICT"]), lambda t: eyes_closed(3.5), steering=NO_STEER)
        self.assertEqual(last.level, "caution")

    def test_bio_danger_with_responding_driver_stays_caution(self):
        d = Drive().warmed_up()
        last = d.run(20, lambda t: Bio("danger", ["EXTREME_TACHY"]), NORMAL_VISION)
        self.assertEqual(last.level, "caution")
        self.assertEqual(last.alert_category, "health")
        self.assertNotIn("danger", d.levels())

    def test_bio_danger_without_response_escalates(self):
        d = Drive().warmed_up()
        d.run(6, NORMAL_BIO, NORMAL_VISION, steering=NO_STEER)
        start = d.t
        last = d.run(8, lambda t: Bio("danger", ["EXTREME_TACHY"]), NORMAL_VISION, steering=NO_STEER)
        self.assertEqual(last.level, "danger")
        self.assertAlmostEqual(d.first("danger", start) - start, 5.0, delta=0.2)

    def test_bio_danger_in_cruise_with_normal_vision_waits_longer(self):
        d = Drive().warmed_up()
        start = d.t
        d.run(35, lambda t: Bio("danger", ["EXTREME_TACHY"]), NORMAL_VISION, manual=False)
        self.assertAlmostEqual(d.first("danger", start) - start, 30.0, delta=0.5)

    def test_bio_danger_survives_sensor_loss(self):
        d = Drive().warmed_up()
        d.run(6, NORMAL_BIO, NORMAL_VISION, steering=NO_STEER)
        d.run(2, lambda t: Bio("danger", ["NO_PULSE_CONFIRMED"]), NORMAL_VISION, steering=NO_STEER)
        last = d.run(5, lambda t: Bio("unavailable", ["SENSOR_LOST"]), NORMAL_VISION, steering=NO_STEER)
        self.assertEqual(last.level, "danger")

    def test_mild_hr_is_caution_without_countdown(self):
        d = Drive().warmed_up()
        last = d.run(30, lambda t: Bio("caution", ["HR_HIGH"]), NORMAL_VISION, steering=NO_STEER)
        self.assertEqual(last.level, "caution")
        self.assertIsNone(last.response_remaining_s)
        self.assertEqual(last.alert_category, "health")

    def test_drowsy_hint_alone_stays_normal(self):
        d = Drive().warmed_up()
        last = d.run(5, lambda t: Bio("normal", ["DROWSY_HINT"]), NORMAL_VISION)
        self.assertEqual(last.level, "normal")

    def test_real_monitor_cardiac_arrest_with_collapse(self):
        s = sim.cardiac_arrest(onset=40.0)
        engine = FusionEngine()
        rows = []
        for status in sim.replay(BioSignalMonitor(Config()), s):
            t = status.t
            engine.push_bio(t, status)
            engine.push_vision(t, COLLAPSE if t >= s.onset + 3 else Vision())
            engine.push_can(t, wobble(t) if t < s.onset else 0.0, 0.3, 0.0)
            rows.append(engine.update(t))
        self.assertNotIn("danger", {r.level for r in rows if r.t < s.onset})
        danger_t = next(r.t for r in rows if r.level == "danger")
        self.assertLessEqual(danger_t - s.onset, 20.0)


class Attention(unittest.TestCase):
    def test_away_without_response_matches_euro_ncap_14s(self):
        d = Drive().warmed_up()
        start = d.t
        away = lambda t: Vision("caution", ["attention_away"]) if t >= 2 else Vision()
        d.run(16, NORMAL_BIO, away, manual=False)
        self.assertAlmostEqual(d.first("danger", start) - start, 14.0, delta=0.2)
        during = next(r for r in d.rows if r.t > start + 5)
        self.assertEqual(during.driver_state, "A")

    def test_yawning_alone_never_escalates(self):
        d = Drive().warmed_up()
        last = d.run(60, NORMAL_BIO, lambda t: Vision("caution", ["yawning"]), steering=NO_STEER)
        self.assertEqual((last.level, last.driver_state), ("caution", "D"))


class Can(unittest.TestCase):
    def test_no_input_without_camera_warns_then_checks(self):
        d = Drive().warmed_up()
        start = d.t
        last = d.run(30, NORMAL_BIO, None, steering=NO_STEER)
        self.assertEqual(last.level, "danger")
        self.assertAlmostEqual(d.first("caution", start) - start, 15.0, delta=2.5)

    def test_brake_held_means_stopped_not_unresponsive(self):
        d = Drive().warmed_up()
        last = d.run(40, NORMAL_BIO, None, steering=NO_STEER, throttle=lambda t: 0.0, brake=lambda t: 0.5)
        self.assertNotEqual(last.level, "danger")
        self.assertNotIn("CAN:NO_INPUT", last.reasons)

    def test_held_throttle_is_not_activity(self):
        d = Drive().warmed_up()
        d.run(16, NORMAL_BIO, None, steering=NO_STEER, throttle=lambda t: 0.8)
        self.assertIn("CAN:NO_INPUT", d.rows[-1].reasons)

    def test_repeated_freeze_then_jerk_is_drowsy_caution(self):
        def jerky(t):
            return 0.7 if (9 < t < 10) or (19 < t < 20) else 0.0
        d = Drive().warmed_up()
        last = d.run(21, NORMAL_BIO, NORMAL_VISION, steering=jerky)
        self.assertEqual((last.level, last.driver_state), ("caution", "D"))
        self.assertIn("CAN:ERRATIC_CONTROL", last.reasons)

    def drift_drive(self, vision, seconds=20.0, torque_on=0.35):
        """3초마다 토크 없이 왼쪽으로 쏠렸다가(0.4초) 토크를 주며 오른쪽으로 복귀하는 조향."""
        engine = FusionEngine()
        rows = []
        for i in range(int(seconds * 10)):
            t = i * 0.1
            phase = t % 5.0
            if 3.0 <= phase < 3.4:
                steer, torque = -1.0 / 30, 0.02
            elif 3.4 <= phase < 4.0:
                steer, torque = 1.2 / 30, torque_on
            else:
                steer, torque = 0.0, 0.02
            engine.push_bio(t, Bio())
            engine.push_vision(t, vision(t))
            engine.push_can(t, steer, 0.22, 0.0, True, 60.0, torque)
            rows.append(engine.update(t))
        return rows

    def test_hands_off_drift_then_correction_is_drowsy_caution(self):
        rows = self.drift_drive(lambda t: Vision())
        hit = next(r for r in rows if "CAN:DRIFT_CORRECTION" in r.reasons)
        self.assertAlmostEqual(hit.t, 3.4, delta=0.15)
        self.assertEqual((hit.level, hit.driver_state), ("caution", "D"))
        self.assertTrue(all(r.level == "normal" for r in rows if 1.0 < r.t < 3.3))

    def test_steering_without_torque_is_not_a_response(self):
        """손에 힘이 없는 채로 차가 비틀거리는 것은 운전자 응답이 아니다."""
        engine = FusionEngine()
        for i in range(400):
            t = i * 0.1
            closed = t >= 20
            engine.push_bio(t, Bio())
            engine.push_vision(t, eyes_closed(t - 20) if closed else Vision())
            steer = 0.05 * math.sin(t) if closed else 0.0
            engine.push_can(t, steer, 0.22, 0.0, True, 60.0, 0.02)
            last = engine.update(t)
        self.assertEqual(last.level, "danger")

    def test_csv_row_adapter(self):
        row = dict(steering_angle_deg="1.5", accelerator_pedal_pct="22", brake_pedal_pct="0",
                   vehicle_speed_kph="60", steering_torque_nm="0.35")
        kw = can_from_csv_row(row)
        self.assertAlmostEqual(kw["steering"], 0.05)
        self.assertAlmostEqual(kw["throttle"], 0.22)
        self.assertEqual(kw["steering_torque"], 0.35)
        self.assertIsNone(can_from_csv_row({**row, "steering_torque_nm": ""})["steering_torque"])

    def test_replay_tool_runs_on_files(self):
        with tempfile.TemporaryDirectory() as folder:
            can = os.path.join(folder, "can.csv")
            with open(can, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["time_s", "steering_angle_deg", "accelerator_pedal_pct", "brake_pedal_pct",
                            "vehicle_speed_kph", "steering_torque_nm", "ground_truth_state"])
                for i in range(75):
                    t = i * 0.2
                    steer, torque = (-1.0, 0.02) if 3.0 <= t < 3.4 else (1.2, 0.35) if 3.4 <= t < 4.0 else (0.0, 0.02)
                    w.writerow([t, steer, 22, 0, 60, torque, "Caution" if 3.0 <= t < 5.0 else "Normal"])
            out = os.path.join(folder, "out.jsonl")
            tool = os.path.join(os.path.dirname(__file__), "..", "tools", "replay_fusion.py")
            result = subprocess.run([sys.executable, tool, "--can", can, "--out", out],
                                    capture_output=True, text=True, encoding="utf-8",
                                    env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            self.assertEqual(result.returncode, 0, result.stderr)
            with open(out, encoding="utf-8") as f:
                levels = [json.loads(line)["level"] for line in f]
            self.assertIn("caution", levels)

    def test_unity_state_adapter(self):
        state = dict(steering=0.1, throttle=0.08, brake=0.0, speed_kph=59.4, control_mode="Cruise")
        self.assertFalse(can_from_unity_state(state)["driver_input_valid"])
        state["control_mode"] = "Manual"
        self.assertTrue(can_from_unity_state(state)["driver_input_valid"])

    def test_low_speed_suppresses_no_input(self):
        engine = FusionEngine()
        for i in range(300):
            t = i * 0.1
            engine.push_vision(t, UNREADABLE)
            engine.push_can(t, 0.0, 0.0, 0.0, True, speed_kph=0.0)
            last = engine.update(t)
        self.assertNotIn("CAN:NO_INPUT", last.reasons)


class Coverage(unittest.TestCase):
    def test_startup_is_not_called_normal(self):
        d = Drive()
        last = d.run(5, lambda t: Bio("unavailable", ["CALIBRATING"]), lambda t: UNREADABLE)
        self.assertEqual(last.level, "unavailable")

    def test_one_monitor_lost_keeps_working(self):
        d = Drive().warmed_up()
        last = d.run(10, NORMAL_BIO, lambda t: UNREADABLE)
        self.assertEqual(last.level, "normal")
        self.assertFalse(last.coverage["vision"])

    def test_both_monitors_lost_is_caution(self):
        d = Drive().warmed_up()
        last = d.run(10, lambda t: Bio("unavailable", ["SENSOR_LOST"]), lambda t: UNREADABLE)
        self.assertEqual(last.level, "caution")
        self.assertIn("DRIVER_MONITORING_LOST", last.reasons)
        self.assertEqual(last.alert_category, "system")

    def test_stale_input_counts_as_lost(self):
        d = Drive().warmed_up()
        last = d.run(5, None, None)
        self.assertFalse(last.coverage["bio"])
        self.assertFalse(last.coverage["vision"])
        self.assertEqual(last.level, "caution")


class Latching(unittest.TestCase):
    def danger(self):
        d = Drive().warmed_up()
        d.run(6, NORMAL_BIO, NORMAL_VISION, steering=NO_STEER)
        d.run(1, lambda t: Bio("danger", ["NO_PULSE_CONFIRMED"]), lambda t: COLLAPSE, steering=NO_STEER)
        return d

    def test_danger_holds_when_inputs_recover(self):
        d = self.danger()
        last = d.run(30, NORMAL_BIO, NORMAL_VISION)
        self.assertEqual((last.level, last.driver_state), ("danger", "E"))

    def test_reset_releases(self):
        d = self.danger()
        d.engine.reset()
        last = d.run(10, NORMAL_BIO, NORMAL_VISION)
        self.assertEqual(last.level, "normal")

    def test_caution_clears_after_hold(self):
        d = Drive().warmed_up()
        d.run(3, lambda t: Bio("caution", ["HR_HIGH"]), NORMAL_VISION)
        self.assertEqual(d.run(2, NORMAL_BIO, NORMAL_VISION).level, "caution")
        self.assertEqual(d.run(4, NORMAL_BIO, NORMAL_VISION).level, "normal")

    def test_vision_explicit_timeout_is_honored(self):
        d = Drive().warmed_up()
        last = d.run(0.1, NORMAL_BIO, lambda t: Vision("danger", ["eyes_closed"]))
        self.assertEqual(last.level, "danger")


if __name__ == "__main__":
    unittest.main()
