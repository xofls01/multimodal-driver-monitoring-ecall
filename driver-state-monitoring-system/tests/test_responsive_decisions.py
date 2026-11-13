import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from driver_state_tools.decision_engine import DEFAULT_RULES, DriverState, FeatureFrame, TemporalDecisionEngine
from driver_state_tools.vision_features import FeatureTracker, RawFeatures


def observed(t, **changes):
    data = dict(timestamp=t, face_readable=True, eyes_readable=True, pose_readable=True,
                eyes_closed=False, attention_away=False, head_abnormal=False, yawning=False,
                perclos_high=False, torso_lean=False, collapse_posture=False, distress_gesture=False,
                long_blinks_repeated=False, nods_repeated=False)
    data.update(changes)
    return FeatureFrame(**data)


class ResponsiveDecisionsTest(unittest.TestCase):
    def test_json_and_default_rules_agree(self):
        rules = json.loads((ROOT / 'config/legacy/decision_rules.json').read_text())
        self.assertEqual(rules, DEFAULT_RULES)

    def test_eye_closure_warns_at_one_second_but_a_blink_does_not(self):
        engine = TemporalDecisionEngine()
        engine.update(observed(0, eyes_closed=True))
        self.assertEqual(engine.update(observed(.2, eyes_closed=True)).state, DriverState.NORMAL)
        self.assertEqual(engine.update(observed(.21)).state, DriverState.NORMAL)
        engine.update(observed(1, eyes_closed=True))
        self.assertEqual(engine.update(observed(1.9, eyes_closed=True)).state, DriverState.NORMAL)
        self.assertEqual(engine.update(observed(2.01, eyes_closed=True)).state, DriverState.CAUTION)

    def test_closed_eyes_with_head_change_warns_earlier(self):
        engine = TemporalDecisionEngine()
        engine.update(observed(0, eyes_closed=True, head_abnormal=True))
        result = engine.update(observed(.41, eyes_closed=True, head_abnormal=True))
        self.assertEqual(result.state, DriverState.CAUTION)
        self.assertIn('eyes_closed_with_posture_change', result.reasons)

    def test_old_history_alone_does_not_warn(self):
        engine = TemporalDecisionEngine()
        for i in range(30):
            result = engine.update(observed(i * .1, perclos_high=True,
                                           long_blinks_repeated=True, nods_repeated=True))
            self.assertEqual(result.state, DriverState.NORMAL)

    def test_recovery_does_not_wait_for_old_eye_history_to_expire(self):
        engine = TemporalDecisionEngine()
        history = dict(perclos_high=True, long_blinks_repeated=True)
        engine.update(observed(0, eyes_closed=True, **history))
        self.assertEqual(engine.update(observed(1.1, eyes_closed=True, **history)).state, DriverState.CAUTION)
        self.assertEqual(engine.update(observed(1.2, **history)).state, DriverState.CAUTION)
        self.assertEqual(engine.update(observed(1.51, **history)).state, DriverState.NORMAL)
        self.assertEqual(engine.update(observed(2, **history)).state, DriverState.NORMAL)
        # A short normal blink must not re-trigger the retained PERCLOS/history.
        engine.update(observed(2.1, eyes_closed=True, **history))
        self.assertEqual(engine.update(observed(2.2, eyes_closed=True, **history)).state, DriverState.NORMAL)
        self.assertEqual(engine.update(observed(2.3, **history)).state, DriverState.NORMAL)

    def test_returning_forward_recovers_despite_glance_history(self):
        engine = TemporalDecisionEngine()
        for start in (0., 1., 2.):
            engine.update(observed(start, attention_away=True))
            result = engine.update(observed(start + .6, attention_away=True))
            if start < 2:
                engine.update(observed(start + .7))
        self.assertIn('attention_away_repeated', result.reasons)
        engine.update(observed(2.7))
        result = engine.update(observed(3.01))
        self.assertEqual(result.state, DriverState.NORMAL)
        self.assertEqual(result.durations['attention_episode_count'], 3)

    def test_unreadable_eyes_do_not_count_as_recovery(self):
        engine = TemporalDecisionEngine()
        engine.update(observed(0, eyes_closed=True))
        engine.update(observed(1.1, eyes_closed=True))
        for t in (1.2, 2., 3.):
            self.assertEqual(engine.update(observed(t, eyes_readable=False, eyes_closed=None)).state,
                             DriverState.UNAVAILABLE)
        self.assertEqual(engine.update(observed(3.1)).state, DriverState.CAUTION)
        self.assertEqual(engine.update(observed(3.41)).state, DriverState.NORMAL)

    def test_missing_pose_is_not_a_posture_recovery(self):
        engine = TemporalDecisionEngine()
        engine.update(observed(0, collapse_posture=True))
        engine.update(observed(.9, collapse_posture=True))
        self.assertEqual(engine.update(observed(1., pose_readable=False, collapse_posture=None)).state,
                         DriverState.UNAVAILABLE)

    def test_remaining_abnormal_signal_blocks_recovery(self):
        engine = TemporalDecisionEngine()
        engine.update(observed(0, eyes_closed=True, head_abnormal=True))
        engine.update(observed(.41, eyes_closed=True, head_abnormal=True))
        self.assertEqual(engine.update(observed(.5, head_abnormal=True)).state, DriverState.CAUTION)
        self.assertEqual(engine.update(observed(.9, head_abnormal=True)).state, DriverState.CAUTION)
        engine.update(observed(1.))
        self.assertEqual(engine.update(observed(1.31)).state, DriverState.NORMAL)

    def test_legacy_policy_requires_explicit_external_timeout(self):
        engine = TemporalDecisionEngine({"danger_policy": "external_response"})
        engine.update(observed(0, eyes_closed=True, warning_active=True))
        self.assertEqual(engine.update(observed(6, eyes_closed=True, warning_active=True)).state, DriverState.CAUTION)
        self.assertEqual(engine.update(observed(6.1, eyes_closed=True, warning_active=True,
                                               response_received=False)).state, DriverState.DANGER)

    def test_reset_clears_recovery_requirements(self):
        engine = TemporalDecisionEngine()
        engine.update(observed(0, collapse_posture=True))
        engine.update(observed(1., collapse_posture=True))
        engine.reset()
        self.assertEqual(engine.update(observed(0)).state, DriverState.NORMAL)


class FastCalibrationTest(unittest.TestCase):
    def config(self):
        return json.loads((ROOT / 'config/legacy/vision_baseline.json').read_text())

    def raw(self, t, **changes):
        data = dict(timestamp=t, face_readable=True, eyes_readable=True, pose_readable=True,
                    ear_left=.3, ear_right=.3, mar=.05, head_pitch=0, head_yaw=0,
                    gaze_x=.5, gaze_y=.5, torso_angle=0, head_drop_ratio=-.4, pose_head_x=0)
        data.update(changes)
        return RawFeatures(**data)

    def test_valid_samples_finish_within_one_second(self):
        tracker = FeatureTracker(self.config())
        for i in range(13):
            out = tracker.update(self.raw(i / 15))
        self.assertEqual(out.calibration_status, 'ready')
        self.assertTrue(out.eyes_readable)
        self.assertFalse(out.eyes_closed)
        self.assertTrue(tracker.update(self.raw(.9, ear_left=.1, ear_right=.1)).eyes_closed)

    def test_time_alone_cannot_finish_calibration(self):
        tracker = FeatureTracker(self.config())
        for t in (0., .8, 1., 3.):
            out = tracker.update(self.raw(t, face_readable=False, eyes_readable=False, pose_readable=False,
                                         head_pitch=None, head_yaw=None, torso_angle=None, head_drop_ratio=None))
        self.assertEqual(out.calibration_status, 'insufficient')
        self.assertIsNone(out.eyes_closed)

    def test_late_face_fills_missing_eye_baseline_without_moving_pose_baseline(self):
        tracker = FeatureTracker(self.config())
        for i in range(13):
            out = tracker.update(self.raw(i / 15, face_readable=False, eyes_readable=False,
                                         ear_left=None, ear_right=None, head_pitch=None, head_yaw=None))
        self.assertEqual(out.calibration_status, 'ready')
        self.assertIsNone(out.eyes_closed)
        neutral_torso = tracker.profile.neutral_torso
        for i in range(13, 23):
            out = tracker.update(self.raw(i / 15, torso_angle=10))
        self.assertTrue(out.eyes_readable)
        self.assertFalse(out.eyes_closed)
        self.assertEqual(tracker.profile.neutral_torso, neutral_torso)


if __name__ == '__main__':
    unittest.main()
