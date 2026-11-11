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

    def test_existing_phone_does_not_auto_confirm_new_false_candidate(self):
        tracker=TemporalBoxTracker(min_confirmations=2)
        a=(10,20,70,50,.8);b=(400,400,490,450,.2)
        tracker.update([a],0)
        self.assertEqual(len(tracker.update([a],.2).boxes),1)
        self.assertEqual(len(tracker.update([a,b],.4).boxes),1)
        self.assertEqual(len(tracker.update([a,b],.6).boxes),2)

    def test_matching_phone_cannot_confirm_other_pending_box(self):
        tracker=TemporalBoxTracker(min_confirmations=2)
        a=(10,20,70,50,.8);b=(400,400,490,450,.2)
        tracker.update([a,b],0)
        output=tracker.update([a],.2)
        self.assertEqual(output.boxes,[a])

    def test_unrelated_detections_do_not_extend_a_stale_track(self):
        tracker=TemporalBoxTracker(hold_seconds=.8,min_confirmations=2)
        a=(10,20,70,50,.8);b=(400,400,490,450,.2)
        tracker.update([a],0);tracker.update([a],.2)
        tracker.update([b],.8)
        output=tracker.update([b],1.1)
        self.assertEqual(output.boxes,[b])

    def test_expired_pending_hit_cannot_confirm_a_new_observation(self):
        tracker=TemporalBoxTracker(hold_seconds=.8,min_confirmations=2)
        a=(10,20,70,50,.8)
        tracker.update([a],0)
        self.assertEqual(tracker.peek(2).boxes,[])
        self.assertEqual(tracker.update([a],2.1).boxes,[])

    def test_two_predictions_in_one_frame_do_not_supply_two_confirmations(self):
        tracker=TemporalBoxTracker(min_confirmations=2)
        a=(10,20,70,50,.8)
        self.assertEqual(tracker.update([a,a],0).boxes,[])


if __name__ == "__main__":
    unittest.main()
