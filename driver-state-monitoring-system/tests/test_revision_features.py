import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from hashlib import sha256

import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from driver_state_tools.nod_events import NodEventDetector
from driver_state_tools.revision_session import RevisionSession, load_profile
from driver_state_tools.vision_features import RawFeatures
from driver_state_tools.gaze_zones import GazeZoneEstimator
from driver_state_tools.decision_engine import DriverState


class NodEventsTest(unittest.TestCase):
    def setUp(self):
        # Synthetic fixture values ONLY, never shipped as a participant profile.
        p=dict(measurement="eye_mid_y_over_image_height",reviewed=True,provenance="synthetic unit fixture",
               upper_boundary=.3,lower_boundary=.5,descent_min_speed=.01,descent_max_speed=10,
               recovery_min_speed=.01,recovery_max_speed=10)
        self.nods=NodEventDetector(p)

    def test_no_fabricated_default_calibration(self):
        result=NodEventDetector(None).update(0,.5,True)
        self.assertEqual(result.status,"needs_calibration")
        self.assertFalse(result.suspected)

    def test_closed_descent_suspected_then_open_recovery_completes(self):
        self.nods.update(0,.2,True)
        d=self.nods.update(.1,.35,True)
        self.assertFalse(d.suspected)
        d=self.nods.update(.2,.55,True)
        self.assertTrue(d.suspected)
        self.assertFalse(d.completed)
        self.nods.update(.3,.4,False)
        d=self.nods.update(.4,.2,False)
        self.assertTrue(d.completed)
        self.assertEqual(d.count,1)
        self.assertFalse(self.nods.update(.5,.2,False).completed)

    def test_eyes_open_while_going_down_cancels(self):
        self.nods.update(0,.2,True)
        self.nods.update(.1,.35,True)
        d=self.nods.update(.2,.55,False)
        self.assertFalse(d.suspected)
        self.assertEqual(d.count,0)
        self.assertEqual(d.reason,"eyes_open_during_descent")

    def test_return_before_lower_boundary_cancels(self):
        self.nods.update(0,.2,True)
        self.nods.update(.1,.4,True)
        d=self.nods.update(.2,.3,True)
        self.assertEqual(d.phase,"idle")
        self.assertEqual(d.count,0)

    def test_missing_position_is_not_a_completed_return(self):
        self.nods.update(0,.2,True)
        self.nods.update(.1,.55,True)
        d=self.nods.update(.2,None,None)
        self.assertEqual(d.status,"unavailable")
        d=self.nods.update(.3,.2,False)
        self.assertFalse(d.completed)
        self.assertEqual(d.count,0)

    def test_eyes_open_downward_movement_is_not_drowsy_nod(self):
        self.nods.update(0,.2,False)
        self.nods.update(.1,.35,False)
        d=self.nods.update(.2,.55,False)
        self.assertFalse(d.suspected)
        self.assertEqual(d.count,0)


class RevisionFeaturesTest(unittest.TestCase):
    def setUp(self):
        self.cfg=json.loads((ROOT/"config/vision_baseline.json").read_text())
        self.rules=json.loads((ROOT/"config/decision_rules.json").read_text())

    def raw(self,t,**changes):
        fields=dict(timestamp=t,face_readable=True,eyes_readable=True,pose_readable=True,
                    ear_left=.3,ear_right=.3,mar=.1,head_pitch=0,head_yaw=0,
                    torso_angle=0,head_drop_ratio=-.4,pose_head_x=0,
                    gaze_observation=dict(state="forward",zone="front",status="ready"))
        fields.update(changes)
        return RawFeatures(**fields)

    def test_closed_start_not_accepted_as_neutral_open(self):
        session=RevisionSession(self.cfg,self.rules)
        for i in range(31):
            derived,decision=session.update(self.raw(i/10,ear_left=.1,ear_right=.1))
        self.assertIsNone(session.tracker.profile.neutral_ear)
        self.assertIsNone(derived.eyes_closed)
        self.assertEqual(decision.state,DriverState.UNAVAILABLE)
        self.assertEqual(decision.metadata["feature_status"]["eyes"],"needs_calibration")

    def test_modes_cannot_silently_mix_measurement_and_decision_settings(self):
        legacy=json.loads((ROOT/"config/legacy/vision_baseline.json").read_text())
        with self.assertRaisesRegex(ValueError,"must not be mixed"):
            RevisionSession(legacy,self.rules)
        with self.assertRaisesRegex(ValueError,"must not be mixed"):
            RevisionSession(self.cfg,{})

    def test_mean_baseline_coefficient_and_clamp_are_logged(self):
        profile=dict(eye_baseline_mean=.4,provenance="test calibration")
        session=RevisionSession(self.cfg,self.rules,profile)
        for i in range(12):
            _,d=session.update(self.raw(i/10))
        calibration=d.metadata["eye_calibration"]
        self.assertAlmostEqual(calibration["requested_threshold"],.3)
        self.assertAlmostEqual(calibration["applied_threshold"],.28)
        self.assertTrue(calibration["clamp_changed_threshold"])
        self.assertFalse(d.metadata["eye_danger_validated"])

    def test_old_mouth_and_chest_conditions_do_not_raise_new_warning(self):
        session=RevisionSession(self.cfg,self.rules,dict(eye_baseline_mean=.3))
        for i in range(100):
            _,d=session.update(self.raw(i/10,mar=.8))
        self.assertEqual(d.state,DriverState.NORMAL)
        self.assertNotIn("yawning",d.reasons)
        self.assertEqual(d.metadata["feature_status"]["mouth"],"mapping_and_calibration_required")

    def test_incompatible_eye_validation_does_not_unlock_stop_path(self):
        profile=dict(eye_baseline_mean=.3,eye_validation=dict(passed=True,applied_threshold=.1,
                     baseline_method="mean",eye_combine_method="mean"))
        session=RevisionSession(self.cfg,self.rules,profile)
        for i in range(12):
            _,d=session.update(self.raw(i/10))
        self.assertFalse(d.metadata["eye_danger_validated"])

    def test_profile_bound_to_video_not_scenario_filename(self):
        with tempfile.TemporaryDirectory() as temp:
            video=Path(temp)/"N1.mp4"
            video.write_bytes(b"source-content")
            p=Path(temp)/"profile.json"
            profile=dict(schema="reviewed_video_calibration_v1",source_sha256=sha256(video.read_bytes()).hexdigest(),
                         eye_baseline_mean=.3,neutral_open_reviewed=True,neutral_sample_count=8,
                         provenance="test reviewed intervals")
            p.write_text(json.dumps(profile))
            self.assertEqual(load_profile(p,video)["eye_baseline_mean"],.3)
            video.write_bytes(b"different-person-or-video")
            with self.assertRaises(ValueError): load_profile(p,video)

    def test_real_eye_validation_helper_uses_mean_and_separate_review_intervals(self):
        spec=importlib.util.spec_from_file_location("calibration_script",ROOT/"scripts/build_eye_calibration.py")
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            video=Path(temp)/"arbitrary.mp4"
            video.write_bytes(b"fixture")
            rows=[dict(timestamp=i/10,raw=dict(eyes_readable=True,ear_left=.3,ear_right=.3)) for i in range(20)]
            rows += [dict(timestamp=2+i/10,raw=dict(eyes_readable=True,ear_left=.1,ear_right=.1)) for i in range(10)]
            a=dict(reviewed=True,reviewer="synthetic unit fixture",source_sha256=sha256(video.read_bytes()).hexdigest(),
                   neutral_open_intervals=[[0,.8]],validation_open_intervals=[[1,1.8]],validation_closed_intervals=[[2,2.8]])
            profile=module.build_profile(video,rows,a,self.cfg)
            self.assertAlmostEqual(profile["eye_baseline_mean"],.3)
            self.assertAlmostEqual(profile["applied_threshold"],.225)
            self.assertTrue(profile["eye_validation"]["passed"])

    def test_literal_mar_last_term_is_p4_p5_not_an_unannounced_repair(self):
        p=dict(formula="r1_eq1_literal",landmark_mapping_reviewed=True,maximum_open_reviewed=True,
               provenance="synthetic formula fixture, not a verified MediaPipe-to-paper mapping",
               maximum_open_seconds=5,maximum_open_mean_mar=1.9,mediapipe_points_p1_to_p8=list(range(8)))
        session=RevisionSession(self.cfg,self.rules,dict(eye_baseline_mean=.3,mouth=p))
        points=dict(enumerate(((0,0),(1,-1),(2,-2),(3,-1),(4,0),(3,1),(2,2),(1,1))))
        opened,status=session._mouth(self.raw(0,face_landmarks=points))
        self.assertEqual(status,"ready")
        self.assertFalse(opened)  # Literal (2+4+sqrt(2))/8 < .95; symmetric repair would be 1.

    def test_time_reversal_resets_feature_timers_and_session_identity(self):
        session=RevisionSession(self.cfg,self.rules,dict(eye_baseline_mean=.3))
        for i in range(20): session.update(self.raw(i/10))
        old=session.session_id
        _,decision=session.update(self.raw(0))
        self.assertNotEqual(session.session_id,old)
        self.assertEqual(decision.durations["eyes_closed"],0)

    def test_missing_nod_profile_not_claimed_full_system_ready(self):
        session=RevisionSession(self.cfg,self.rules,dict(eye_baseline_mean=.3))
        for i in range(12): _,d=session.update(self.raw(i/10))
        self.assertFalse(d.metadata["full_system_ready"])
        self.assertEqual(d.metadata["feature_status"]["nod"],"needs_calibration")


class GazeReadinessTest(unittest.TestCase):
    def test_no_model_is_unknown_not_head_proxy(self):
        estimator=GazeZoneEstimator({},ROOT)
        out=estimator.predict(np.zeros((100,100,3),np.uint8),dict(readable=True,box=(0,0,100,100)))
        self.assertEqual(out["state"],"unknown")
        self.assertEqual(out["status"],"model_not_connected")

    def test_invalid_artifact_not_silently_loaded(self):
        estimator=GazeZoneEstimator(dict(gaze_model_metadata="does-not-exist.json"),ROOT)
        self.assertEqual(estimator.status,"model_load_error")

    def test_verified_torchscript_contract_and_closed_class_separate(self):
        import torch
        class Toy(torch.nn.Module):
            def forward(self,x):
                # A constant synthetic class fixture, NOT a gaze model.
                return x.mean(dim=(1,2,3),keepdim=False).reshape(-1,1)*0+torch.tensor([[0.,0.,0.,0.,0.,0.,10.]])
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            model=folder/"toy.pt"
            torch.jit.trace(Toy(),torch.zeros(1,3,16,16)).save(str(model))
            meta=dict(weights="toy.pt",sha256=sha256(model.read_bytes()).hexdigest(),
                      class_names=["front","left","right","console","mirror","cluster","eyes_closed"],
                      forward_zones=["front"],eyes_closed_zone="eyes_closed",mean=[0,0,0],std=[1,1,1],
                      image_size=[16,16],crop="face",input_modality="RGB",license="test fixture",provenance="synthetic test only",
                      camera_forward_confirmed=True,deployment_validated=True,minimum_confidence=.8,output_kind="logits")
            path=folder/"meta.json"
            path.write_text(json.dumps(meta))
            estimator=GazeZoneEstimator(dict(gaze_model_metadata=str(path)),ROOT)
            self.assertEqual(estimator.status,"ready")
            result=estimator.predict(np.zeros((100,100,3),np.uint8),dict(readable=True,box=(0,0,100,100)))
            self.assertEqual(result["state"],"eyes_closed")
            self.assertEqual(estimator.predict(np.zeros((100,100,3),np.uint8),{})["state"],"unknown")
            meta["input_modality"]="IR"
            path.write_text(json.dumps(meta))
            self.assertEqual(GazeZoneEstimator(dict(gaze_model_metadata=str(path)),ROOT).status,"input_modality_mismatch")


if __name__=="__main__": unittest.main()
