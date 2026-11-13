from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest

import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from driver_state_tools.prototype_engine import PrototypeFrame, PrototypeDecisionEngine
from driver_state_tools.decision_engine import DriverState
from driver_state_tools.revision_session import RevisionSession
from driver_state_tools.vision_features import RawFeatures
from driver_state_tools.evidence_dashboard import evidence_content, draw_evidence_overlay, PALETTE
from driver_state_tools.unity_bridge import vehicle_control_packet


class PrototypeEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.rules=json.loads((ROOT/"config/prototype/decision_rules.json").read_text())
        self.cfg=json.loads((ROOT/"config/prototype/vision_baseline.json").read_text())
        self.cfg["decision_rules"]=self.rules
        self.engine=PrototypeDecisionEngine(self.rules)
        self.normal=PrototypeFrame(0,True,True,False,False,False,False,False,False,False)

    def test_observed_neutral_is_normal_without_gaze_cnn(self):
        self.assertEqual(self.engine.update(self.normal).state,DriverState.NORMAL)

    def test_unknown_eyes_are_not_normal(self):
        self.assertEqual(self.engine.update(replace(self.normal,eyes_closed=None)).state,DriverState.UNAVAILABLE)

    def test_eye_warning_boundary_and_no_unvalidated_eye_stop(self):
        for i in range(61):
            d=self.engine.update(replace(self.normal,timestamp=i/10,eyes_closed=True))
            if i==14: self.assertEqual(d.state,DriverState.NORMAL)
            if i==15: self.assertEqual(d.state,DriverState.CAUTION)
        self.assertEqual(d.state,DriverState.CAUTION)
        self.assertFalse(d.metadata["eye_stop_enabled"])

    def test_collapse_warns_and_stops_without_face_or_eyes(self):
        for i in range(39):
            d=self.engine.update(replace(self.normal,timestamp=i/10,face_readable=False,eyes_closed=None,
                                         attention_away=None,collapse_posture=True))
            if i==7: self.assertEqual(d.state,DriverState.UNAVAILABLE)
            if i==8: self.assertEqual(d.state,DriverState.CAUTION)
            if i==37: self.assertNotEqual(d.state,DriverState.DANGER)
        self.assertEqual(d.state,DriverState.DANGER)
        self.assertEqual(d.metadata["stop_basis"],"simulator_sustained_posture")

    def test_unknown_or_long_gap_breaks_collapse_continuity(self):
        for i in range(35):
            self.engine.update(replace(self.normal,timestamp=i/10,collapse_posture=True))
        self.engine.update(replace(self.normal,timestamp=3.5,collapse_posture=None))
        d=self.engine.update(replace(self.normal,timestamp=3.6,collapse_posture=True))
        self.assertEqual(d.durations["collapse_posture"],0)
        d=self.engine.update(replace(self.normal,timestamp=8,collapse_posture=True))
        self.assertEqual(d.durations["collapse_after_caution"],0)
        self.assertNotEqual(d.state,DriverState.DANGER)

    def test_chest_mouth_and_recovery_do_not_accumulate_stop_time(self):
        for i in range(100):
            d=self.engine.update(replace(self.normal,timestamp=i/10,hand_near_chest=i<50,mouth_open=i>=50))
            self.assertNotEqual(d.state,DriverState.DANGER)
        self.assertEqual(d.durations["collapse_after_caution"],0)
        for i in range(100,135):
            d=self.engine.update(replace(self.normal,timestamp=i/10,collapse_posture=True))
            self.assertNotEqual(d.state,DriverState.DANGER)

    def test_cause_change_and_recovery_dont_keep_stop_timer(self):
        for i in range(40): d=self.engine.update(replace(self.normal,timestamp=i/10,collapse_posture=True))
        self.assertEqual(d.state,DriverState.DANGER)
        for i in range(40,45): d=self.engine.update(replace(self.normal,timestamp=i/10))
        self.assertEqual(d.state,DriverState.NORMAL)
        self.assertEqual(d.durations["collapse_after_caution"],0)

    def test_repeat_events_and_total_time_are_visible(self):
        for i in range(60):
            away=i%20<7
            d=self.engine.update(replace(self.normal,timestamp=i/10,attention_away=away))
        self.assertEqual(d.durations["attention_episode_count"],3)
        self.assertGreater(d.durations["attention_window_seconds"],1.7)
        self.assertNotEqual(d.state,DriverState.DANGER)

    def test_no_old_history_warning_when_currently_forward(self):
        for i in range(110): self.engine.update(replace(self.normal,timestamp=i/10,attention_away=True))
        for i in range(110,115): d=self.engine.update(replace(self.normal,timestamp=i/10))
        self.assertEqual(d.state,DriverState.NORMAL)

    def test_hips_missing_does_not_discard_visible_nose_and_shoulders(self):
        session=RevisionSession(self.cfg,self.rules,dict(neutral_window=[0,.8],provenance="synthetic fixture"))
        for i in range(50):
            low=i>=10
            raw=RawFeatures(i/10,not low,not low,not low,ear_left=.3 if not low else None,
                ear_right=.3 if not low else None,mar=.03,head_pitch=0 if not low else None,
                head_yaw=0 if not low else None,torso_angle=0 if not low else None,
                head_drop_ratio=.4 if low else -.4,pose_head_x=0)
            derived,d=session.update(raw)
        self.assertEqual(d.state,DriverState.DANGER)
        self.assertTrue(d.metadata["evidence_values"]["head_drop_observed"])
        self.assertFalse(d.metadata["evidence_values"]["torso_observed"])
        self.assertIsNone(derived.eyes_closed)

    def test_no_unreviewed_or_future_neutral_reference(self):
        for profile in ({},dict(neutral_window=[1,2],provenance="synthetic fixture")):
            session=RevisionSession(self.cfg,self.rules,profile)
            for i in range(10):
                derived,d=session.update(RawFeatures(i/10,True,True,True,ear_left=.3,ear_right=.3,
                    head_pitch=0,head_yaw=0,torso_angle=0,head_drop_ratio=-.4,pose_head_x=0))
                self.assertEqual(d.state,DriverState.UNAVAILABLE)

    def test_missing_head_is_not_filled_with_negative_collapse_evidence(self):
        session=RevisionSession(self.cfg,self.rules,dict(neutral_window=[0,.8],provenance="fixture"))
        for i in range(10):
            session.update(RawFeatures(i/10,True,True,True,ear_left=.3,ear_right=.3,
                head_pitch=0,head_yaw=0,torso_angle=0,head_drop_ratio=-.4,pose_head_x=0))
        derived,d=session.update(RawFeatures(1,True,True,True,ear_left=.3,ear_right=.3,
                head_pitch=0,head_yaw=0,torso_angle=0,head_drop_ratio=None,pose_head_x=0))
        self.assertIsNone(d.metadata["prototype_frame"]["collapse_posture"])
        self.assertIsNone(derived.collapse_posture)

    def test_external_border_adds_space_and_preserves_source_dimensions(self):
        session=RevisionSession(self.cfg,self.rules,dict(neutral_window=[0,.8],provenance="fixture"))
        raw=RawFeatures(0,False,False,False)
        derived,d=session.update(raw)
        scene=np.random.default_rng(1).integers(0,256,(720,1280,3),dtype=np.uint8)
        output=draw_evidence_overlay(scene,raw,derived,d,None,460,self.cfg)
        self.assertEqual(output.shape,(736,1756,3))
        np.testing.assert_array_equal(output[8:728,8:1288],scene)
        self.assertEqual(tuple(output[0,0]),PALETTE["unavailable"][::-1])

    def test_panel_displays_actual_timers_and_masks_unknown(self):
        d=self.engine.update(replace(self.normal,eyes_closed=None,collapse_posture=True))
        c=evidence_content(None,d,self.rules)
        self.assertIsNone(c["meters"]["eyes_closed"]["elapsed"])
        self.assertEqual(c["meters"]["collapse_posture"]["limit"],.8)
        self.assertEqual(c["meters"]["collapse_stop"]["limit"],3)

    def test_stale_danger_and_unavailable_are_distinct_from_valid_caution(self):
        for i in range(40): d=self.engine.update(replace(self.normal,timestamp=i/10,collapse_posture=True))
        p=vehicle_control_packet(d,1)
        self.assertEqual(p["driver_state"],"Danger")
        self.assertTrue(p["stop_requested"])
        self.assertEqual(p["caution_timer_policy"],"same_collapse_only")
        stale=vehicle_control_packet(d,2,age=1)
        self.assertFalse(stale["vision_available"])
        self.assertFalse(stale["stop_requested"])

    def test_plain_posture_label_does_not_claim_an_unobserved_body_component(self):
        d=self.engine.update(replace(self.normal,collapse_posture=True))
        d.metadata["evidence_values"]=dict(head_drop_observed=True,torso_observed=False,
                                         relative_head_drop=.4,relative_torso=None)
        c=evidence_content(None,d,self.rules,self.cfg)
        self.assertEqual(c["posture_label"],"Head lowered")
        d.metadata["evidence_values"]=dict(head_drop_observed=False,torso_observed=True,
                                         relative_head_drop=None,relative_torso=40)
        self.assertEqual(evidence_content(None,d,self.rules,self.cfg)["posture_label"],"Body leaning")

    def test_stop_countdown_preserves_elapsed_meter_and_never_claims_vehicle_stopped(self):
        for i in range(34): d=self.engine.update(replace(self.normal,timestamp=i/10,collapse_posture=True))
        c=evidence_content(None,d,self.rules,self.cfg)
        self.assertEqual(c["stop_text"],"Stop request in 0.50s")
        self.assertEqual(c["stop_note"],"If the same posture continues")
        self.assertAlmostEqual(c["meters"]["collapse_stop"]["elapsed"],2.5)
        self.assertEqual(c["meters"]["collapse_stop"]["limit"],3)
        for i in range(34,40): d=self.engine.update(replace(self.normal,timestamp=i/10,collapse_posture=True))
        c=evidence_content(None,d,self.rules,self.cfg)
        self.assertEqual(c["stop_text"],"Stop requested")
        self.assertIsNone(c["stop_remaining_seconds"])
        self.assertNotIn("Vehicle stopped",c["stop_text"])

    def test_missing_posture_does_not_generate_a_stop_countdown(self):
        d=self.engine.update(replace(self.normal,collapse_posture=None))
        c=evidence_content(None,d,self.rules,self.cfg)
        self.assertEqual(c["posture_label"],"Unable to read")
        self.assertIsNone(c["stop_remaining_seconds"])
        self.assertEqual(c["stop_text"],"Unable to assess stop countdown")


if __name__=="__main__": unittest.main()
