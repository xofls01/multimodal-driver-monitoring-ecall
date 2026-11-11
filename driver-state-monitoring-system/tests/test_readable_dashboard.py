import copy
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/"src"))
from driver_state_tools.dashboard import draw_overlay, _draw_baseline_overlay
from driver_state_tools.decision_engine import Decision, DriverState
from driver_state_tools.readable_dashboard import build_panel_content, replace_panel, draw_readable_panel
from driver_state_tools.vision_features import FeatureTracker, RawFeatures


class ReadableDashboardTests(unittest.TestCase):
    def setUp(self):
        self.rules = json.loads((ROOT/"config/decision_rules.json").read_text())
        self.cfg = json.loads((ROOT/"config/vision_baseline.json").read_text())
        self.cfg["decision_rules"] = self.rules
        self.raw = RawFeatures(1, False, False, False)
        self.derived = FeatureTracker(self.cfg).update(self.raw)
        self.decision = Decision(1, DriverState.UNAVAILABLE, ["required_observation_unavailable"],
                                 dict(eyes_closed=0, attention_away=0, attention_window_seconds=0),
                                 dict(mode="literature", feature_status=dict(eyes="needs_calibration",
                                 gaze="model_not_connected", nod="needs_calibration",
                                 mouth="mapping_and_calibration_required"), gaze="unknown"))

    def content(self, **kwargs):
        return build_panel_content(self.raw, self.derived, self.decision, self.rules, **kwargs)

    def test_unavailable_is_never_zero_or_open_or_no_nods(self):
        c = self.content()
        for key in ("eye_time", "away_time", "away_total", "nod_count"):
            self.assertEqual(c[key], "--")
        self.assertEqual(c["eye_state"], "기준 설정 필요")
        self.assertEqual(c["gaze_state"], "확인 불가")
        self.assertIsNone(c["eye_seconds"])
        self.assertIn("연결", c["gaze_note"])
        display = " ".join(str(c[k]) for k in ("eye_state", "gaze_note", "nod_state", "mouth_state", "reason"))
        self.assertNotIn("_", display)

    def test_observed_zero_differs_from_unknown_and_limits_are_from_rules(self):
        self.decision.metadata.update(feature_status=dict(eyes="ready", gaze="ready"), gaze="forward")
        self.derived.eyes_readable = True
        self.derived.eyes_closed = False
        self.rules.update(eyes_closed_caution_seconds=2.75, attention_away_caution_seconds=4.5,
                          attention_window_seconds=40, attention_cumulative_seconds=12)
        c = self.content()
        self.assertEqual(c["eye_time"], "0.00초")
        self.assertEqual(c["away_time"], "0.00초")
        self.assertIn("2.75초", c["eye_rule"])
        self.assertIn("4.5초", c["gaze_rule"])
        self.assertIn("40초 중 12초", c["gaze_rule"])

    def test_precision_does_not_show_1_47_as_reaching_1_50(self):
        self.decision.metadata["feature_status"]["eyes"] = "ready"
        self.derived.eyes_closed, self.derived.eyes_readable = True, True
        self.decision.durations["eyes_closed"] = 1.47
        self.assertEqual(self.content()["eye_time"], "1.47초")

    def test_stop_reason_has_priority_over_initial_caution_reason(self):
        self.decision.state = DriverState.DANGER
        self.decision.reasons = ["attention_away", "away_after_displayed_warning"]
        self.assertIn("경고가 표시된 뒤에도", self.content()["reason"])
        self.assertIn("정차 요청", self.content()["state_label"])

    def test_rppg_unready_does_not_show_stale_bpm(self):
        rppg = SimpleNamespace(status="unavailable", heart_rate_bpm=72)
        self.assertEqual(self.content(rppg=rppg)["rppg"], "신호 불안정")

    def test_nod_return_is_not_labeled_as_descent_or_absence(self):
        self.decision.metadata["feature_status"]["nod"] = "ready"
        self.decision.metadata["nod_event"] = dict(phase="recovering", suspected=True)
        self.assertEqual(self.content()["nod_state"], "고개 돌아오는 중")

    def test_ratio_unknown_bounds_not_converted_to_single_percentage(self):
        self.decision.metadata["eye_ratio"] = dict(status="unknown", lower_ratio=.1, upper_ratio=.8)
        self.assertEqual(self.content()["eye_ratio"], "10~80% (판단 보류)")

    def test_panel_only_preserves_every_source_pixel_and_does_not_mutate_inputs(self):
        scene = np.random.default_rng(3).integers(0,256,(720,1280,3),dtype=np.uint8)
        before = scene.copy()
        objects = copy.deepcopy((self.raw, self.derived, self.decision, self.rules))
        output = replace_panel(scene, self.raw, self.derived, self.decision, self.rules)
        self.assertEqual(output.shape, (720,1700,3))
        np.testing.assert_array_equal(scene, before)
        np.testing.assert_array_equal(output[:, :1280], before)
        self.assertEqual((self.raw, self.derived, self.decision, self.rules), objects)

    def test_active_renderer_changes_only_the_rail(self):
        scene = np.zeros((720,1280,3),dtype=np.uint8)
        expected = _draw_baseline_overlay(scene, self.raw, self.derived, self.decision, ui_config=self.cfg)
        output = draw_overlay(scene, self.raw, self.derived, self.decision, ui_config=self.cfg)
        np.testing.assert_array_equal(output[:, :1280], expected[:, :1280])
        self.assertFalse(np.array_equal(output[:, 1280:], expected[:, 1280:]))

    def test_absent_korean_font_has_readable_english_fallback(self):
        with patch("driver_state_tools.readable_dashboard.resolve_font", return_value=None):
            panel = draw_readable_panel(720,420,self.raw,self.derived,self.decision,self.rules)
        self.assertEqual(panel.shape, (720,420,3))
        c = self.content(language="en")
        self.assertIn("connecting", c["gaze_note"])
        self.assertNotIn("MODEL_NOT_CONNECTED", str(c))

    def test_saved_video_never_uses_future_observation(self):
        spec = importlib.util.spec_from_file_location("readable_rerender", ROOT/"scripts/rerender_readable_review.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.select_record_index([0,.1,.2], .19), 1)
        self.assertEqual(module.select_record_index([0,.1,.2], .2), 2)
        with self.assertRaises(ValueError):
            module.select_record_index([.1,.2], 0)


if __name__ == "__main__":
    unittest.main()
