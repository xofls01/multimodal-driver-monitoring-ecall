import sys
import unittest
import tempfile
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from driver_state_tools.decision_engine import DriverState, FeatureFrame, TemporalDecisionEngine
from driver_state_tools.video_common import capture_condition, clip_id, discover_videos, sample_timestamps, scenario_family, scenario_id
from driver_state_tools.vision_features import FeatureTracker, RawFeatures, eye_aspect_ratio, iris_gaze_ratio, mouth_aspect_ratio


def frame(t: float, **changes: object) -> FeatureFrame:
    values = dict(timestamp=t, face_readable=True, eyes_readable=True, pose_readable=True)
    values.update(changes)
    return FeatureFrame(**values)


class TemporalDecisionEngineTest(unittest.TestCase):
    def test_unreadable_is_not_normal(self) -> None:
        engine = TemporalDecisionEngine()
        decision = engine.update(frame(0.0, face_readable=False))
        self.assertEqual(decision.state, DriverState.UNAVAILABLE)

    def test_long_eye_closure_becomes_caution(self) -> None:
        engine = TemporalDecisionEngine()
        self.assertEqual(engine.update(frame(0.0, eyes_closed=True)).state, DriverState.NORMAL)
        self.assertEqual(engine.update(frame(3.1, eyes_closed=True)).state, DriverState.CAUTION)

    def test_sustained_chest_contact_is_caution_not_medical_diagnosis(self) -> None:
        engine = TemporalDecisionEngine({"distress_gesture_caution_seconds": 1.0})
        self.assertEqual(engine.update(frame(0.0, distress_gesture=True)).state, DriverState.NORMAL)
        decision = engine.update(frame(1.1, distress_gesture=True))
        self.assertEqual(decision.state, DriverState.CAUTION)
        self.assertIn("distress_gesture", decision.reasons)

    def test_video_cannot_infer_no_response(self) -> None:
        engine = TemporalDecisionEngine({"danger_policy": "external_response"})
        engine.update(frame(0.0, eyes_closed=True, warning_active=True))
        decision = engine.update(frame(6.0, eyes_closed=True, warning_active=True, response_received=None))
        self.assertEqual(decision.state, DriverState.CAUTION)

    def test_explicit_button_timeout_can_make_danger(self) -> None:
        engine = TemporalDecisionEngine({"danger_policy": "external_response"})
        engine.update(frame(0.0, eyes_closed=True, warning_active=True))
        decision = engine.update(frame(6.0, eyes_closed=True, warning_active=True, response_received=False))
        self.assertEqual(decision.state, DriverState.DANGER)

    def test_pose_failure_does_not_hide_readable_face(self) -> None:
        engine = TemporalDecisionEngine()
        decision = engine.update(frame(0.0, pose_readable=False))
        self.assertEqual(decision.state, DriverState.NORMAL)

    def test_readable_head_can_warn_when_eyes_are_unreadable(self) -> None:
        engine = TemporalDecisionEngine({"attention_away_caution_seconds": 1.0})
        self.assertEqual(engine.update(frame(0.0, eyes_readable=False, attention_away=True)).state, DriverState.UNAVAILABLE)
        self.assertEqual(engine.update(frame(1.1, eyes_readable=False, attention_away=True)).state, DriverState.CAUTION)

    def test_repeated_short_glances_can_warn(self) -> None:
        engine = TemporalDecisionEngine({"attention_repeat_count": 3, "attention_repeat_window_seconds": 5.0})
        engine.update(frame(0.0, attention_away=True))
        engine.update(frame(0.4, attention_away=True))
        engine.update(frame(0.5, attention_away=False))
        engine.update(frame(1.0, attention_away=True))
        engine.update(frame(1.4, attention_away=True))
        engine.update(frame(1.5, attention_away=False))
        engine.update(frame(2.0, attention_away=True))
        decision = engine.update(frame(2.6, attention_away=True))
        self.assertEqual(decision.state, DriverState.CAUTION)
        self.assertIn("attention_away_repeated", decision.reasons)

    def test_short_signal_gap_does_not_break_duration(self) -> None:
        engine = TemporalDecisionEngine({"eyes_closed_caution_seconds": 1.0, "signal_gap_tolerance_seconds": 0.3})
        engine.update(frame(0.0, eyes_closed=True))
        engine.update(frame(0.5, eyes_closed=False))
        decision = engine.update(frame(1.1, eyes_closed=True))
        # The 0.6 s gap exceeds tolerance, so this starts a new episode.
        self.assertEqual(decision.state, DriverState.NORMAL)
        engine.reset()
        engine.update(frame(0.0, eyes_closed=True))
        engine.update(frame(0.5, eyes_closed=True))
        engine.update(frame(0.7, eyes_closed=None))
        engine.update(frame(0.75, eyes_closed=True))
        decision = engine.update(frame(1.1, eyes_closed=True))
        self.assertEqual(decision.state, DriverState.CAUTION)

    def test_false_resets_but_missing_does_not_make_normal(self) -> None:
        engine = TemporalDecisionEngine({"eyes_closed_caution_seconds": 2.0})
        engine.update(frame(0, eyes_closed=True))
        engine.update(frame(1, eyes_closed=True))
        engine.update(frame(1.1, eyes_closed=False))
        self.assertEqual(engine.update(frame(1.5, eyes_closed=True)).state, DriverState.NORMAL)

    def test_posture_warning_survives_face_occlusion(self) -> None:
        engine = TemporalDecisionEngine()
        engine.update(frame(0, face_readable=False, eyes_readable=False, collapse_posture=True))
        result = engine.update(frame(1.6, face_readable=False, eyes_readable=False, collapse_posture=True))
        self.assertEqual(result.state, DriverState.CAUTION)

    def test_single_frame_glance_flickers_are_not_counted(self) -> None:
        engine = TemporalDecisionEngine()
        for i in range(30):
            result = engine.update(frame(i*.1,attention_away=i%2 == 0))
        self.assertEqual(result.state, DriverState.NORMAL)
        self.assertEqual(result.durations['attention_episode_count'],0)

    def test_cumulative_distraction_without_phone(self) -> None:
        engine = TemporalDecisionEngine({'attention_repeat_count':99})
        for i in range(70):
            result = engine.update(frame(i*.1, attention_away=i%20 < 15))
        self.assertEqual(result.state,DriverState.CAUTION)
        self.assertIn('attention_away_cumulative',result.reasons)


class VideoCommonTest(unittest.TestCase):
    def test_drive_copy_name_is_video_and_keeps_scenario(self) -> None:
        name = unicodedata.normalize('NFD', 'A1.mp4의 사본')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / name
            path.touch()
            self.assertEqual(discover_videos(Path(folder), ['.mp4']), [path])
            self.assertEqual(clip_id(path), 'A1')
            self.assertEqual(scenario_family(path), 'A')
            self.assertEqual(scenario_id(path), 'A1')
            self.assertEqual(path.name, name)

    def test_conditioned_scenario_name(self) -> None:
        path = Path("OA3.mp4")
        self.assertEqual(clip_id(path), "OA3")
        self.assertEqual(scenario_id(path), "A3")
        self.assertEqual(capture_condition(path), "O")
        self.assertEqual(scenario_family(path), "A")

    def test_sampling_stays_safely_before_eof(self) -> None:
        values = sample_timestamps(16.03, 3.0)
        self.assertEqual(values[:2], [0.0, 3.0])
        self.assertLessEqual(values[-1], 15.53)


class VisionFeaturesTest(unittest.TestCase):
    def test_eye_aspect_ratio(self) -> None:
        points = [(0.0, 0.0)] * 6
        points[0] = (0.0, 0.0)
        points[1] = (1.0, -1.0)
        points[2] = (3.0, -1.0)
        points[3] = (4.0, 0.0)
        points[4] = (3.0, 1.0)
        points[5] = (1.0, 1.0)
        self.assertAlmostEqual(eye_aspect_ratio(points, range(6)), 0.5)

    def test_mouth_aspect_ratio(self) -> None:
        points = [(0.0, 0.0)] * 309
        points[78] = (0.0, 0.0)
        points[308] = (10.0, 0.0)
        points[13] = (5.0, -2.0)
        points[14] = (5.0, 2.0)
        self.assertAlmostEqual(mouth_aspect_ratio(points), 0.4)

    def test_iris_gaze_ratio(self) -> None:
        points = [(0.0, 0.0, 0.0)] * 478
        points[33], points[133] = (0.0, 0.5, 0.0), (10.0, 0.5, 0.0)
        points[159], points[145], points[468] = (5.0, 0.0, 0.0), (5.0, 4.0, 0.0), (4.0, 2.0, 0.0)
        points[362], points[263] = (20.0, 0.5, 0.0), (30.0, 0.5, 0.0)
        points[386], points[374], points[473] = (25.0, 0.0, 0.0), (25.0, 4.0, 0.0), (24.0, 2.0, 0.0)
        self.assertEqual(iris_gaze_ratio(points), (0.4, 0.5))

    def test_tracker_is_unavailable_until_calibrated(self) -> None:
        config = {
            "calibration_seconds": 1.0,
            "calibration_max_seconds": 2.0,
            "calibration_min_samples": 2,
            "ema_alpha": 1.0,
            "eye_threshold_max": 0.28,
            "eye_threshold_min": 0.12,
            "eye_close_ratio": 0.70,
            "eye_hysteresis": 0.025,
            "perclos_window_seconds": 60.0,
            "perclos_min_observed_seconds": 2.0,
            "perclos_caution_ratio": 0.35,
            "mouth_open_min": 0.28,
            "mouth_open_delta": 0.16,
            "head_yaw_degrees": 24.0,
            "head_pitch_degrees": 18.0,
            "eye_pose_max_yaw_degrees": 32.0,
            "eye_pose_max_pitch_degrees": 25.0,
            "head_median_window_samples": 3,
            "torso_lean_degrees": 20.0,
            "collapse_lean_degrees": 34.0,
            "collapse_head_drop_delta": 0.30,
        }
        tracker = FeatureTracker(config)

        def raw(timestamp: float, ear: float = 0.30) -> RawFeatures:
            return RawFeatures(
                timestamp=timestamp,
                face_readable=True,
                eyes_readable=True,
                pose_readable=True,
                ear_left=ear,
                ear_right=ear,
                mar=0.08,
                head_pitch=2.0,
                head_yaw=3.0,
                torso_angle=1.0,
            )

        self.assertFalse(tracker.update(raw(0.0)).eyes_readable)
        tracker.update(raw(0.5))
        calibrated = tracker.update(raw(1.0))
        self.assertEqual(calibrated.calibration_status, "ready")
        self.assertFalse(calibrated.eyes_closed)
        self.assertTrue(tracker.update(raw(1.2, ear=0.15)).eyes_closed)


if __name__ == "__main__":
    unittest.main()

