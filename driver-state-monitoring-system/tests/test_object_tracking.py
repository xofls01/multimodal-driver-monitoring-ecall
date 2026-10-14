import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from driver_state_tools.object_tracking import TemporalBoxTracker, box_iou


class TemporalBoxTrackerTest(unittest.TestCase):
    def test_iou(self):
        self.assertAlmostEqual(box_iou((0, 0, 10, 10, 0.9), (5, 5, 15, 15, 0.8)), 25 / 175)

    def test_short_miss_is_tracked_then_expires(self):
        tracker = TemporalBoxTracker(hold_seconds=0.8)
        result = tracker.update([(10, 20, 30, 50, 0.9)], 0.0)
        self.assertEqual(result.mode, "detected")
        result = tracker.update([], 0.4)
        self.assertEqual(result.mode, "tracked")
        self.assertEqual(len(result.boxes), 1)
        result = tracker.update([], 0.9)
        self.assertIsNone(result.mode)
        self.assertEqual(result.boxes, [])

    def test_confirmation_rejects_single_frame_false_positive(self):
        tracker = TemporalBoxTracker(hold_seconds=0.8, min_confirmations=2)
        first = tracker.update([(10, 20, 30, 50, 0.9)], 0.0)
        self.assertIsNone(first.mode)
        self.assertEqual(first.boxes, [])
        second = tracker.update([(11, 20, 31, 50, 0.8)], 0.125)
        self.assertEqual(second.mode, "detected")


if __name__ == "__main__":
    unittest.main()
