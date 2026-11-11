import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from driver_state_tools.handheld_objects import HandheldObjectDetector
from driver_state_tools.vision_features import FeatureTracker, RawFeatures
from driver_state_tools.decision_engine import Decision, DriverState
from driver_state_tools.dashboard import draw_overlay


class HandheldObjectsTest(unittest.TestCase):
    def setUp(self):
        self.frame = np.zeros((720, 1280, 3), np.uint8)
        self.config = json.loads((ROOT / "config/vision_baseline.json").read_text())
        self.config["handheld_object_classes"] = {"cup": "drink", "sandwich": "food"}
        self.model = SimpleNamespace(names={41: "cup", 48: "sandwich"})
        self.detector = HandheldObjectDetector(self.model, self.config)
        self.hands = {"landmarks": [[(float(300+i%5*5), float(350+i//5*5), 0.) for i in range(21)]]}
        self.cup = (280., 320., 360., 400., .8, 41)
        self.predictions = [self.cup]
        self.detector._predict = lambda images, size: [list(self.predictions) if size == 960 else [] for _ in images]

    def update(self, timestamp, hands=None, pose=None, face=None):
        return self.detector.update(self.frame, pose or {}, self.hands if hands is None else hands, face or {}, timestamp)

    def test_confirm_only_actual_matched_measurements(self):
        self.assertEqual(self.update(0), [])
        self.assertEqual(self.update(.1), [])
        items = self.update(.34)
        self.assertEqual([item["label"] for item in items], ["cup"])
        self.assertEqual(items[0]["mode"], "detected")

    def test_square_food_does_not_use_phone_shape_filter(self):
        self.predictions = [(280., 320., 360., 400., .8, 48)]
        self.update(0)
        self.assertEqual(self.update(.34)[0]["category"], "food")

    def test_background_object_not_attached_to_driver(self):
        self.predictions = [(800., 300., 860., 380., .95, 41)]
        self.assertEqual(self.update(0), [])
        self.assertEqual(self.update(.34), [])

    def test_pending_miss_breaks_confirmation(self):
        self.update(0)
        self.predictions = []
        self.update(.34)
        self.predictions = [self.cup]
        self.assertEqual(self.update(.68), [])
        self.assertEqual(len(self.update(1.02)), 1)

    def test_different_class_never_confirms_previous_object(self):
        self.update(0)
        self.predictions = [(*self.cup[:5], 48)]
        self.assertEqual(self.update(.34), [])

    def test_two_different_objects_do_not_share_confirmation(self):
        self.update(0)
        self.predictions = [self.cup, (400., 320., 460., 400., .8, 41)]
        self.hands["landmarks"].append([(float(420+i%5*5), float(350+i//5*5), 0.) for i in range(21)])
        items = self.update(.34)
        self.assertEqual(len(items), 1)
        self.assertLess(items[0]["box"][0], 400)

    def test_cache_expires_and_does_not_claim_current_mouth_relation(self):
        face = {"readable": True, "box": (200, 150, 400, 350), "landmarks": {13: (300, 330)}}
        self.update(0, face=face)
        self.assertTrue(self.update(.34, face=face)[0]["near_mouth"])
        cached = self.update(.4)[0]
        self.assertEqual(cached["mode"], "cached")
        self.assertIsNone(cached["near_mouth"])
        self.predictions = []
        self.assertEqual(self.update(.68)[0]["mode"], "tracked")
        self.assertEqual(self.update(1.01), [])

    def test_visible_wrist_fallback_only_without_hands(self):
        pose = {"keypoints": {9: (310., 390., .9)}}
        self.update(0, hands={}, pose=pose)
        self.assertEqual(len(self.update(.34, hands={}, pose=pose)), 1)
        self.detector.reset()
        self.assertEqual(self.update(0, hands={}, pose={}), [])

    def test_crop_coordinates_map_back_to_source(self):
        view = self.detector._views(self.frame, self.hands["landmarks"], {})[1]
        self.detector._predict = lambda images, size: [
            [] if size == 960 else [(self.cup[0]-view[0], self.cup[1]-view[1], self.cup[2]-view[0], self.cup[3]-view[1], .8, 41)]
            for _ in images]
        self.update(0)
        self.assertEqual(self.update(.34)[0]["box"], list(self.cup[:4]))

    def test_reset_does_not_carry_candidates_to_new_clip(self):
        self.update(0)
        self.update(.34)
        self.detector.reset()
        self.assertEqual(self.update(0), [])

    def test_unsupported_class_cannot_be_renamed_as_smoking(self):
        self.config["handheld_object_classes"]["cigarette"] = "smoking"
        with self.assertRaisesRegex(ValueError, "cigarette"):
            HandheldObjectDetector(self.model, self.config)

    def test_disabled_detector_does_not_infer(self):
        self.config["handheld_objects_enabled"] = False
        detector = HandheldObjectDetector(self.model, self.config)
        detector._predict = lambda *_: self.fail("Disabled detector performed inference")
        self.assertEqual(detector.update(self.frame, {}, {}, {}, 0), [])

    def test_real_class_ids_are_resolved_from_weight_names(self):
        detector = HandheldObjectDetector(SimpleNamespace(names={0: "sandwich", 1: "cup"}), self.config)
        self.assertEqual(detector.classes, {0: "food", 1: "drink"})

    def test_log_serialization_and_overlay_accept_additional_objects(self):
        self.update(0)
        items = self.update(.34)
        cfg = dict(self.config, calibration_seconds=.1, calibration_min_samples=2)
        tracker = FeatureTracker(cfg)
        for timestamp in (0., .1, .2):
            raw = RawFeatures(timestamp, True, True, True, ear_left=.3, ear_right=.3,
                              mar=.04, head_pitch=0, head_yaw=0, gaze_x=.5, gaze_y=.5,
                              torso_angle=0, head_drop_ratio=-.4, pose_head_x=0,
                              handheld_objects=items)
            derived = tracker.update(raw)
        output = draw_overlay(self.frame, raw, derived, Decision(.2, DriverState.NORMAL), ui_config=cfg)
        self.assertEqual(output.shape, (720, 1700, 3))
        spec = importlib.util.spec_from_file_location("object_log_runner", ROOT / "scripts/run_vision_baseline.py")
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        record = json.loads(json.dumps(runner.raw_record(raw)))
        self.assertEqual(record["handheld_objects"][0]["label"], "cup")
        self.assertFalse(derived.attention_away)


if __name__ == "__main__":
    unittest.main()
