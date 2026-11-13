import math
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from driver_state_tools.rppg import RPPGTracker
from driver_state_tools.vision_features import RawFeatures


class RPPGTrackerTest(unittest.TestCase):
    def config(self):
        return {
            "analysis_fps": 8.0,
            "rppg_window_seconds": 12.0,
            "rppg_min_seconds": 8.0,
            "rppg_min_coverage": 0.65,
            "rppg_min_bpm": 42.0,
            "rppg_max_bpm": 180.0,
            "rppg_peak_band_hz": 0.12,
            "rppg_min_snr_db": 6.0,
            "rppg_stability_seconds": 3.0,
            "rppg_max_bpm_mad": 8.0,
            "rppg_max_bpm_range": 12.0,
            "rppg_fusion_max_delta_bpm": 10.0,
        }

    @staticmethod
    def color(timestamp: float, bpm: float = 75.0):
        pulse = math.sin(2.0 * math.pi * bpm / 60.0 * timestamp)
        return (110.0 + 0.7 * pulse, 100.0 + 2.2 * pulse, 90.0 - 0.4 * pulse)

    @staticmethod
    def frame(timestamp: float, forehead=None, cheeks=None):
        return RawFeatures(
            timestamp=timestamp,
            face_readable=True,
            eyes_readable=True,
            pose_readable=True,
            rppg_forehead_rgb=forehead,
            rppg_cheeks_rgb=cheeks,
        )

    def test_forehead_is_preferred_when_signal_is_good(self):
        tracker = RPPGTracker(self.config())
        result = None
        for index in range(96):
            timestamp = index / 8.0
            result = tracker.update(self.frame(timestamp, self.color(timestamp), self.color(timestamp)))
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.source, "fusion")
        self.assertAlmostEqual(result.heart_rate_bpm, 75.0, delta=3.0)
        self.assertEqual(result.forehead_status, "ready")
        self.assertEqual(result.cheeks_status, "ready")

    def test_cheeks_are_used_when_forehead_is_missing(self):
        tracker = RPPGTracker(self.config())
        result = None
        for index in range(96):
            timestamp = index / 8.0
            result = tracker.update(self.frame(timestamp, None, self.color(timestamp)))
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.source, "cheeks")
        self.assertAlmostEqual(result.heart_rate_bpm, 75.0, delta=3.0)

    def test_missing_current_roi_does_not_reuse_previous_bpm(self):
        tracker = RPPGTracker(self.config())
        for index in range(96):
            timestamp = index / 8.0
            result = tracker.update(self.frame(timestamp, self.color(timestamp), None))
        self.assertEqual(result.status, "ready")
        result = tracker.update(self.frame(12.0, None, None))
        self.assertEqual(result.status, "unavailable")
        self.assertIsNone(result.heart_rate_bpm)


if __name__ == "__main__":
    unittest.main()
