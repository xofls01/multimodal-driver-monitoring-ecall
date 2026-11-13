from pathlib import Path
import copy,json,sys
from types import SimpleNamespace
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from driver_state_tools.monitoring_output import MonitoringOutput,output_for_packet
from driver_state_tools.decision_engine import Decision,DriverState
from driver_state_tools.vision_features import RawFeatures
from driver_state_tools.unity_bridge import vehicle_control_packet,UnityStateServer


class MonitoringOutputTests(unittest.TestCase):
    def setUp(self):
        self.tracker=MonitoringOutput('clip',dict(collapse_head_drop_delta=.25,wheel_roi=[0,0,1,1]),
                                     clock=lambda:'2026-11-12T07:00:00.000Z')

    def step(self,t,state='normal',*,final=None,head=.3,known=True,eyes=False,**kwargs):
        raw=RawFeatures(t,True,eyes is not None,True,face_box=(10,10,100,150),
                        left_wrist_on_wheel=True,right_wrist_on_wheel=False,
                        observation_quality={'face_detected':True,'face_width_pixels':90})
        v=Decision(t,DriverState(state),[],dict(eyes_closed=1.6 if eyes else 0,collapse_posture=5.),
                   dict(evidence_values={'head_drop_observed':known}))
        vision=SimpleNamespace(raw=raw,derived=SimpleNamespace(eyes_closed=eyes,relative_head_drop=head,relative_pitch=12.),decision=v)
        d=Decision(t,DriverState(final or state),[],{},dict(v.metadata))
        bio=dict(level='normal',hr=74.5,result_fresh=True,t=t,sample_timestamp=t,
                 sample_age_seconds=0.,channels={'ppg':dict(ready=True,sqi=.8,reliable=True)})
        output=self.tracker.update(vision,bio,d,t,frame_shape=(720,1280,3),**kwargs)
        return output,d

    def test_vision_and_fused_danger_times_are_separate_and_not_repeated_every_frame(self):
        first,_=self.step(0.,'caution',final='danger')
        self.assertEqual(first['danger']['source_seconds'],0.)
        self.assertFalse(first['vision']['danger']['detected'])
        second,_=self.step(.1,'danger')
        third,_=self.step(.2,'danger')
        self.assertEqual(second['vision']['danger']['source_seconds'],.1)
        self.assertEqual(third['vision']['danger'],second['vision']['danger'])
        self.assertEqual(third['danger']['source_seconds'],0.)

    def test_head_time_is_not_a_body_lean_timer_and_gaps_are_unknown(self):
        a,_=self.step(0.,head=0.)
        self.assertFalse(a['vision']['head_lowered'])
        self.assertEqual(a['vision']['head_lowering_seconds'],0.)
        self.step(.1,head=.3)
        b,_=self.step(.2,head=.3)
        self.assertAlmostEqual(b['vision']['head_lowering_seconds'],.1)
        c,_=self.step(.3,known=False)
        self.assertIsNone(c['vision']['head_lowering_seconds'])
        d,_=self.step(.4,head=.3)
        self.assertEqual(d['vision']['head_lowering_seconds'],0.)

    def test_missing_eye_measurement_is_null_not_open_or_zero_closure(self):
        result,_=self.step(0.,eyes=None)
        self.assertIsNone(result['vision']['eyes_closed'])
        self.assertIsNone(result['vision']['eyes_closed_seconds'])
        self.assertFalse(result['vision']['eyes_observed'])

    def test_proxy_is_not_reported_as_a_calibrated_angle_or_confidence(self):
        output,_=self.step(0.)
        self.assertIsNone(output['vision']['head_lowering_angle_degrees'])
        self.assertFalse(output['vision']['head_angle_calibrated'])
        self.assertEqual(output['vision']['head_pitch_proxy'],12.)
        self.assertIsNone(output['observation_quality']['judgment_confidence_score'])
        self.assertIsNone(output['observation_quality']['face_detection_confidence'])
        self.assertIsNone(output['observation_quality']['camera']['camera_hardware_ok'])
        self.assertIsNone(output['observation_quality']['sensors']['hardware_ok'])

    def test_wheel_position_is_available_but_physical_grip_is_not_inferred(self):
        output,_=self.step(0.)
        self.assertTrue(output['vision']['hands_on_wheel'])
        self.assertEqual(output['vision']['hands_on_wheel_method'],'wrist_region_overlap')
        self.assertIsNone(output['vision']['physical_grip_confirmed'])

    def test_display_ack_is_not_a_driver_response_or_proof_of_consciousness(self):
        first,_=self.step(0.,'caution')
        request=first['driver_response']['warning_request_id']
        observed,_=self.step(.1,'caution',warning_events=[{'warning_request_id':request}])
        self.assertTrue(observed['driver_response']['warning_display_confirmed'])
        self.assertIsNone(observed['driver_response']['last_confirmed_response'])
        self.assertIsNone(observed['driver_response']['cancel_attempted'])
        recovered,_=self.step(.2)
        self.assertTrue(recovered['driver_response']['visual_recovery']['after_confirmed_warning'])
        self.assertIsNone(recovered['driver_response']['consciousness_confirmed'])
        self.assertIsNone(recovered['driver_response']['last_confirmed_response'])

    def test_unconfirmed_video_warning_does_not_become_a_confirmed_warning_response(self):
        self.step(0.,'caution');recovered,_=self.step(.1)
        self.assertFalse(recovered['driver_response']['visual_recovery']['after_confirmed_warning'])

    def test_explicit_cancel_attempt_is_recorded_without_cancelling_danger(self):
        first,_=self.step(0.,'caution');request=first['driver_response']['warning_request_id']
        event=dict(clip_id='clip',warning_request_id=request,event_id='press-1',action='cancel_attempt',source='unity_button')
        output,decision=self.step(.1,'danger',response_events=[event],response_input_connected=True)
        self.assertTrue(output['driver_response']['cancel_attempted'])
        self.assertEqual(output['driver_response']['last_confirmed_response']['applied_source_seconds'],.1)
        self.assertEqual(decision.state,DriverState.DANGER)
        repeated,_=self.step(.2,'danger',response_events=[event],response_input_connected=True)
        self.assertEqual(repeated['driver_response']['last_confirmed_response'],output['driver_response']['last_confirmed_response'])

    def test_stale_packets_clear_observations_but_keep_danger_history(self):
        output,decision=self.step(0.,'danger');decision.metadata['monitoring_output']=output
        before=copy.deepcopy(output)
        packet=vehicle_control_packet(decision,1,age=1.)
        self.assertFalse(packet['monitoring']['is_current'])
        self.assertIsNone(packet['monitoring']['vision']['eyes_closed_seconds'])
        self.assertFalse(packet['monitoring']['vision']['danger']['active'])
        self.assertTrue(packet['monitoring']['vision']['danger']['detected'])
        self.assertEqual(output,before)

    def test_camera_observation_age_is_independent_of_transport_age(self):
        output,_=self.step(0.,'caution')
        packet=output_for_packet(output,.4,.75)
        self.assertTrue(packet['is_current'])
        self.assertFalse(packet['vision']['current'])
        self.assertIsNone(packet['vision']['face_visible'])

    def test_vehicle_feedback_can_expire_before_the_prediction(self):
        output,_=self.step(0.,'danger')
        output['vehicle_confirmation']=dict(stop_confirmed=True,status='confirmed_stopped',
            receipt_fresh=True,feedback_age_seconds=.7)
        packet=output_for_packet(output,.1,.75)
        self.assertTrue(packet['is_current'])
        self.assertFalse(packet['vehicle_confirmation']['stop_confirmed'])
        self.assertFalse(packet['vehicle_confirmation']['receipt_fresh'])

    def test_report_is_json_serializable_and_new_clips_have_no_previous_events(self):
        output,_=self.step(0.,'danger');json.dumps(output,allow_nan=False)
        other=MonitoringOutput('other')
        self.assertEqual(other.dangers,{})
        self.assertIsNone(other.last_response)
        self.assertIsNone(other.last_visual_recovery)

    def test_end_of_recording_preserves_history_without_reviving_current_values(self):
        output,decision=self.step(0.,'danger');decision.metadata['monitoring_output']=output
        server=UnityStateServer(clock=lambda:0.)
        server.publish(decision,'clip');server._critical_pending=None
        server.mark_unavailable('recording_finished')
        packet=server.next_packet()
        self.assertFalse(packet['monitoring']['is_current'])
        self.assertTrue(packet['monitoring']['danger']['detected'])
        self.assertFalse(packet['monitoring']['danger']['active'])
        self.assertIsNone(packet['monitoring']['vision']['eyes_closed_seconds'])


if __name__=='__main__':unittest.main()
