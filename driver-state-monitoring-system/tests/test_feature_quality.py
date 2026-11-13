import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from driver_state_tools.vision_features import FeatureTracker, RawFeatures


class QualityTest(unittest.TestCase):
    def tracker(self):
        cfg = json.loads((ROOT/'config/legacy/vision_baseline.json').read_text())
        cfg.update(calibration_seconds=.5, calibration_min_samples=3, ema_alpha=1,
                   head_median_window_samples=1)
        return FeatureTracker(cfg)

    def raw(self,t,**changes):
        data = dict(timestamp=t, face_readable=True, eyes_readable=True, pose_readable=True,
                    ear_left=.3,ear_right=.3,mar=.04,head_pitch=0,head_yaw=0,
                    gaze_x=.5,gaze_y=.5,torso_angle=0,head_drop_ratio=-.4,pose_head_x=0)
        data.update(changes)
        return RawFeatures(**data)

    def test_eye_occlusion_preserves_pose_calibration_not_fake_eyes(self):
        tr = self.tracker()
        for i in range(11):
            out = tr.update(self.raw(i*.1, eyes_readable=False,ear_left=None,ear_right=None))
        self.assertEqual(out.calibration_status,'ready')
        self.assertIsNone(out.eyes_closed)
        self.assertIsNone(out.perclos)
        self.assertEqual(out.measurement_coverage,'limited')
        out = tr.update(self.raw(1.2,eyes_readable=False,ear_left=None,ear_right=None,head_drop_ratio=.2))
        self.assertTrue(out.collapse_posture)

    def test_phone_does_not_imply_not_looking_forward(self):
        tr = self.tracker()
        for i in range(20):
            out = tr.update(self.raw(i*.1,phone_confidence=.8))
        self.assertTrue(out.phone_visible)
        self.assertFalse(out.attention_away)

    def test_closed_eyes_are_not_iris_gaze_away(self):
        tr = self.tracker()
        for i in range(10):
            tr.update(self.raw(i*.1))
        out = tr.update(self.raw(1,ear_left=.1,ear_right=.1,gaze_x=.95,gaze_y=.95))
        self.assertTrue(out.eyes_closed)
        self.assertIsNone(out.gaze_away)

    def test_missing_eyes_not_counted_as_open_in_perclos(self):
        tr = self.tracker()
        for i in range(100):
            tr.update(self.raw(i*.1))
        before = tr._eye_observed_seconds
        for i in range(100,130):
            out = tr.update(self.raw(i*.1,eyes_readable=False))
        self.assertEqual(out.eye_observed_seconds,before)
        self.assertIsNone(out.perclos)

    def test_no_blink_duration_only_monitoring(self):
        tr = self.tracker()
        for i in range(240):
            out = tr.update(self.raw(i*.1))
        self.assertGreater(out.no_blink_seconds,20)
        self.assertFalse(out.long_blinks_repeated)
        self.assertFalse(out.perclos_high)
        self.assertEqual(out.blink_rate_per_minute,0)

    def test_losing_pose_does_not_complete_nod(self):
        tr = self.tracker()
        for i in range(10):
            tr.update(self.raw(i*.1))
        tr.update(self.raw(1,head_drop_ratio=.1))
        out = tr.update(self.raw(1.5,pose_readable=False,head_drop_ratio=None,torso_angle=None,pose_head_x=None))
        self.assertEqual(out.nod_count,0)
        out = tr.update(self.raw(1.6))
        self.assertEqual(out.nod_count,0)


if __name__ == '__main__':
    unittest.main()
