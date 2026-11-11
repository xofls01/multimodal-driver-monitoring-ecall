import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from driver_state_tools.decision_engine import DriverState, FeatureFrame, TemporalDecisionEngine


def sample(t, **changes):
    values = dict(timestamp=t, face_readable=True, eyes_readable=True, pose_readable=True,
                  eyes_closed=False, attention_away=False, head_abnormal=False, yawning=False,
                  perclos_high=False, torso_lean=False, collapse_posture=False, distress_gesture=False,
                  long_blinks_repeated=False, nods_repeated=False)
    values.update(changes)
    return FeatureFrame(**values)


class CautionEscalationTest(unittest.TestCase):
    def test_three_seconds_start_at_caution_not_at_eye_closure(self):
        engine = TemporalDecisionEngine()
        for i in range(40):
            out = engine.update(sample(i / 10, eyes_closed=True))
            self.assertNotEqual(out.state, DriverState.DANGER)
        out = engine.update(sample(4., eyes_closed=True))
        self.assertEqual(out.state, DriverState.DANGER)
        self.assertAlmostEqual(out.durations['caution_continuous'], 3.)
        self.assertIn('caution_persisted', out.reasons)
        self.assertNotIn('explicit response timeout after warning', out.reasons)
        self.assertEqual(engine.update(sample(4.1, eyes_closed=True)).state, DriverState.DANGER)

    def test_changing_warning_reason_does_not_reset_caution(self):
        engine = TemporalDecisionEngine()
        for i in range(41):
            out = engine.update(sample(i / 10, distress_gesture=True, eyes_closed=i < 25))
        self.assertEqual(out.state, DriverState.DANGER)
        self.assertAlmostEqual(out.durations['caution_continuous'], 3.)

    def test_normal_resets_escalation(self):
        engine = TemporalDecisionEngine()
        for i in range(25):
            out = engine.update(sample(i / 10, eyes_closed=i < 20))
        self.assertEqual(out.state, DriverState.NORMAL)
        for i in range(25, 60):
            out = engine.update(sample(i / 10, eyes_closed=True))
            self.assertNotEqual(out.state, DriverState.DANGER)

    def test_unavailable_breaks_caution_continuity(self):
        engine = TemporalDecisionEngine()
        for i in range(21):
            engine.update(sample(i / 10, eyes_closed=True))
        out = engine.update(sample(2.1, eyes_readable=False, eyes_closed=None))
        self.assertEqual(out.state, DriverState.UNAVAILABLE)
        self.assertEqual(out.durations['caution_continuous'], 0)
        for i in range(22, 52):
            out = engine.update(sample(i / 10, eyes_closed=True))
            self.assertNotEqual(out.state, DriverState.DANGER)
        self.assertEqual(engine.update(sample(5.2, eyes_closed=True)).state, DriverState.DANGER)

    def test_long_missing_observation_gap_is_not_three_seconds_of_evidence(self):
        engine = TemporalDecisionEngine()
        engine.update(sample(0, eyes_closed=True))
        engine.update(sample(1, eyes_closed=True))
        out = engine.update(sample(5, eyes_closed=True))
        self.assertEqual(out.state, DriverState.CAUTION)
        self.assertEqual(out.durations['caution_continuous'], 0)

    def test_reset_prevents_cross_video_escalation(self):
        engine = TemporalDecisionEngine()
        for i in range(41):
            out = engine.update(sample(i / 10, eyes_closed=True))
        self.assertEqual(out.state, DriverState.DANGER)
        engine.reset()
        self.assertEqual(engine.update(sample(0)).state, DriverState.NORMAL)

    def test_invalid_policy_rejected(self):
        with self.assertRaises(ValueError):
            TemporalDecisionEngine(dict(danger_policy='typo'))
        with self.assertRaises(ValueError):
            TemporalDecisionEngine(dict(caution_to_danger_seconds=0))


if __name__ == '__main__':
    unittest.main()
