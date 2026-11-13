import json
import sys
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from driver_state_tools.decision_engine import DriverState
from driver_state_tools.literature_engine import LiteratureDecisionEngine, LiteratureFrame, FixedClosureWindows


class LiteratureRulesTest(unittest.TestCase):
    def setUp(self):
        self.rules=json.loads((ROOT/"config/decision_rules.json").read_text())
        self.engine=LiteratureDecisionEngine(self.rules)

    def frame(self,t,**changes):
        values=dict(timestamp=t,session_id="fixture",eyes_closed=False,gaze="forward",gaze_zone="front",
                    feature_status=dict(eyes="ready",gaze="ready",nod="needs_calibration",mouth="needs_calibration"))
        values.update(changes)
        return LiteratureFrame(**values)

    def feed(self,start,end,**changes):
        count=round((end-start)*100)
        result=None
        for i in range(count+1):
            result=self.engine.update(self.frame(round(start+i*.01,8),**changes))
        return result

    def test_selected_defaults_are_separate_from_legacy(self):
        legacy=json.loads((ROOT/"config/legacy/decision_rules.json").read_text())
        self.assertEqual(self.rules["mode"],"literature")
        self.assertNotIn("caution_to_danger_seconds",self.rules)
        self.assertEqual(legacy["caution_to_danger_seconds"],3)

    def test_eye_boundaries_and_validation_gate(self):
        self.assertEqual(self.feed(0,1.49,eyes_closed=True).state,DriverState.UNAVAILABLE)
        decision=self.feed(1.5,1.5,eyes_closed=True)
        self.assertEqual(decision.state,DriverState.CAUTION)
        self.assertNotIn("prolonged_eye_closure",decision.reasons)
        self.assertNotIn("prolonged_eye_closure",self.feed(1.51,2.99,eyes_closed=True).reasons)
        self.assertIn("prolonged_eye_closure",self.feed(3,3,eyes_closed=True).reasons)
        self.assertEqual(self.feed(3.01,6,eyes_closed=True).state,DriverState.CAUTION)
        self.assertEqual(self.feed(6.01,6.01,eyes_closed=True,eye_danger_validated=True).state,DriverState.DANGER)

    def test_validated_eye_stop_exact_six_seconds(self):
        self.assertNotEqual(self.feed(0,5.99,eyes_closed=True,eye_danger_validated=True).state,DriverState.DANGER)
        self.assertEqual(self.feed(6,6,eyes_closed=True,eye_danger_validated=True).state,DriverState.DANGER)

    def test_eye_unknown_interrupts_continuity(self):
        self.feed(0,5.9,eyes_closed=True,eye_danger_validated=True)
        self.engine.update(self.frame(6,eyes_closed=None))
        decision=self.engine.update(self.frame(6.1,eyes_closed=True,eye_danger_validated=True))
        self.assertEqual(decision.durations["eyes_closed"],0)
        self.assertNotEqual(decision.state,DriverState.DANGER)

    def test_long_observation_gap_does_not_advance_eye_timer(self):
        self.feed(0,2,eyes_closed=True,eye_danger_validated=True)
        decision=self.engine.update(self.frame(8,eyes_closed=True,eye_danger_validated=True))
        self.assertEqual(decision.durations["eyes_closed"],0)
        self.assertTrue(decision.metadata["observation_gap"])

    def test_distraction_request_is_not_display_confirmation(self):
        decision=self.feed(0,2.99,gaze="away")
        self.assertEqual(decision.state,DriverState.NORMAL)
        decision=self.feed(3,3,gaze="away")
        self.assertEqual(decision.state,DriverState.CAUTION)
        self.assertTrue(decision.metadata["warning_request_id"])
        self.assertIsNone(decision.metadata["warning_issued_at"])
        self.assertNotEqual(self.feed(3.01,15,gaze="away").state,DriverState.DANGER)

    def test_three_seconds_after_actual_ack(self):
        decision=self.feed(0,3,gaze="away")
        request=decision.metadata["warning_request_id"]
        self.assertFalse(self.engine.confirm_warning("wrong",3,"fixture"))
        self.assertTrue(self.engine.confirm_warning(request,3,"simulated_display"))
        self.assertNotEqual(self.feed(3.01,5.99,gaze="away").state,DriverState.DANGER)
        decision=self.feed(6,6,gaze="away")
        self.assertEqual(decision.state,DriverState.DANGER)
        self.assertEqual(decision.metadata["warning_source"],"simulated_display")

    def test_unknown_or_forward_cancels_warning_stop_timer(self):
        for state in ("forward","unknown","eyes_closed"):
            self.setUp()
            d=self.feed(0,3,gaze="away")
            self.engine.confirm_warning(d.metadata["warning_request_id"],3,"fixture")
            self.engine.update(self.frame(3.1,gaze=state))
            d=self.feed(3.2,6.1,gaze="away")
            self.assertNotEqual(d.state,DriverState.DANGER)
            self.assertIsNone(d.metadata["warning_issued_at"])

    def test_closed_or_unreadable_eyes_not_added_to_away_time(self):
        for eye in (True,None):
            self.setUp()
            d=self.feed(0,6,gaze="away",eyes_closed=eye)
            self.assertEqual(d.durations["attention_window_seconds"],0)
            self.assertEqual(d.durations["attention_away"],0)

    def test_cumulative_warning_and_no_history_only_realert(self):
        # Four sub-three-second episodes, separated by observed forward returns.
        t=0.
        d=None
        for _ in range(4):
            d=self.feed(t,t+2.5,gaze="away")
            t=round(t+2.51,2)
            if _<3:
                self.feed(t,t+.19,gaze="forward")
                t=round(t+.2,2)
        self.assertIn("attention_away_cumulative",d.reasons)
        self.assertAlmostEqual(d.durations["attention_window_seconds"],10,places=6)
        d=self.feed(t,t+.4,gaze="forward")
        self.assertEqual(d.state,DriverState.NORMAL)
        d=self.feed(t+.41,t+.8,gaze="away")
        self.assertEqual(d.state,DriverState.NORMAL)
        self.assertIsNone(d.metadata["warning_request_id"])

    def test_mouth_or_nod_caution_never_becomes_stop_by_time(self):
        d=self.feed(0,10,mouth_open=True)
        self.assertEqual(d.state,DriverState.CAUTION)
        self.assertFalse(d.metadata["stop_requested"])
        d=self.feed(10.01,15,mouth_open=False,nod_suspected=True)
        self.assertEqual(d.state,DriverState.CAUTION)
        self.assertIsNone(d.metadata["warning_request_id"])

    def test_nod_caution_does_not_require_eye_duration_threshold(self):
        d=self.engine.update(self.frame(0,nod_suspected=True,eyes_closed=True))
        self.assertEqual(d.state,DriverState.CAUTION)
        self.assertEqual(d.durations["eyes_closed"],0)

    def test_short_file_collects_ratio_without_blocking_other_ready_features(self):
        d=self.feed(0,15)
        self.assertEqual(d.metadata["eye_ratio"]["status"],"collecting")
        self.assertEqual(d.state,DriverState.NORMAL)

    def test_partial_observation_keeps_valid_eye_warning(self):
        d=self.feed(0,2,eyes_closed=True,gaze="unknown",feature_status=dict(eyes="ready",gaze="model_not_connected"))
        self.assertEqual(d.state,DriverState.CAUTION)
        self.assertFalse(d.metadata["full_system_ready"])

    def test_normal_requires_both_required_observations(self):
        self.assertEqual(self.engine.update(self.frame(0,gaze="unknown")).state,DriverState.UNAVAILABLE)
        self.assertEqual(self.engine.update(self.frame(.1)).state,DriverState.NORMAL)

    def test_recovery_delay_is_not_stop_time(self):
        self.feed(0,2,eyes_closed=True)
        self.assertEqual(self.engine.update(self.frame(2.1)).state,DriverState.CAUTION)
        self.assertEqual(self.engine.update(self.frame(2.39)).state,DriverState.CAUTION)
        self.assertEqual(self.engine.update(self.frame(2.4)).state,DriverState.NORMAL)

    def test_new_session_or_time_reversal_resets_timers(self):
        self.feed(0,5,eyes_closed=True,eye_danger_validated=True)
        d=self.engine.update(self.frame(0,eyes_closed=True,eye_danger_validated=True))
        self.assertEqual(d.durations["eyes_closed"],0)
        self.assertEqual(d.metadata["observation_reset"],"timestamp_reversed")
        d=self.engine.update(self.frame(1,session_id="different",eyes_closed=True))
        self.assertEqual(d.durations["eyes_closed"],0)
        self.assertEqual(d.metadata["eye_ratio"]["status"],"collecting")

    def test_missing_window_cannot_create_fake_away_or_ratio_high(self):
        d=self.feed(0,65,eyes_closed=None,gaze="unknown")
        self.assertEqual(d.metadata["eye_ratio"]["status"],"unknown")
        self.assertEqual(d.state,DriverState.UNAVAILABLE)


class FixedWindowTest(unittest.TestCase):
    def test_full_window_not_observed_time_denominator(self):
        window=FixedClosureWindows(60,.2,.25,60)
        for i in range(601):
            t=i/10
            result=window.update(t,True if t<=15 else None)
        self.assertEqual(result["status"],"high")
        self.assertAlmostEqual(result["closed_seconds"],15)
        self.assertAlmostEqual(result["unknown_seconds"],45)
        self.assertAlmostEqual(result["lower_ratio"],.25)
        self.assertAlmostEqual(result["upper_ratio"],1)

    def test_missing_values_can_straddle_threshold(self):
        window=FixedClosureWindows(60,.2,.25,60)
        for i in range(601):
            t=i/10
            result=window.update(t,True if t<5 else None if t<20 else False)
        self.assertEqual(result["status"],"unknown")
        self.assertLess(result["lower_ratio"],.2)
        self.assertGreater(result["upper_ratio"],.2)

    def test_completed_result_replaced_at_next_fixed_boundary(self):
        window=FixedClosureWindows(60,.2,.25,60)
        for i in range(1201):
            t=i/10
            result=window.update(t,True if t<=60 else False)
            if i==600:
                self.assertEqual(result["status"],"high")
        self.assertEqual(result["status"],"low")
        self.assertEqual(result["window_end"],120)


if __name__=="__main__": unittest.main()
