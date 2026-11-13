from pathlib import Path
import json,socket,sys,time
from types import SimpleNamespace
import unittest
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from driver_state_tools.analysis_publisher import AnalysisPublisher
from driver_state_tools.decision_engine import Decision,DriverState
from driver_state_tools.multimodal_runtime import MultimodalProcessor,VisionObservation
from driver_state_tools.unity_bridge import UnityStateServer
from driver_state_tools.vision_features import RawFeatures


class Vision:
    def update(self,frame,t):
        state=DriverState.DANGER if t>=.2 else DriverState.CAUTION
        decision=Decision(t,state,[],{},dict(mode='prototype',prototype_frame={'collapse_posture':True}))
        return VisionObservation(RawFeatures(t,True,True,True),
                                 SimpleNamespace(eyes_closed=False,relative_pitch=None,relative_head_drop=None),decision)


class Signal:
    def advance(self,t):
        return dict(t=t,level='normal',reasons=[],hr=74.,available=True,result_fresh=True,channels={},synthetic=False)


class DriverResponseProtocolTests(unittest.TestCase):
    def wait(self,predicate):
        until=time.monotonic()+3
        while time.monotonic()<until:
            if predicate():return
            time.sleep(.005)
        self.fail('Timed out waiting for response transport')

    def test_receiver_rejects_duplicate_wrong_clip_and_expired_response(self):
        now=[0.];server=UnityStateServer(clock=lambda:now[0])
        d=Decision(0.,DriverState.CAUTION,[],{},dict(warning_request_id='warning-1'))
        server.publish(d,'clip')
        event=dict(type='driver_response',event_id='button-1',warning_request_id='warning-1',clip_id='clip',action='cancel_attempt')
        self.assertFalse(server.accept_driver_response(dict(event,clip_id='other')))
        self.assertTrue(server.accept_driver_response(event))
        self.assertFalse(server.accept_driver_response(event))
        received=server.driver_response_events('clip');self.assertEqual(len(received),1)
        now[0]=1.
        self.assertFalse(server.accept_driver_response(dict(event,event_id='new-press')))

    def test_real_tcp_ack_is_not_a_driver_response_and_cancel_does_not_cancel_danger(self):
        with UnityStateServer(port=0) as server:
            sock=socket.create_connection(('127.0.0.1',server.port),timeout=3)
            sock.settimeout(3)
            try:
                self.wait(lambda:server.connected.is_set())
                p=MultimodalProcessor(Vision(),Signal(),{},'clip',publisher=AnalysisPublisher(server))
                first=p.update(np.zeros((8,8,3),dtype=np.uint8),0.)
                request=first.decision.metadata['warning_request_id']
                ack=dict(type='driver_warning_ack',clip_id='clip',warning_request_id=request)
                telemetry=dict(type='vehicle_state',driver_response_input_connected=True)
                sock.sendall((json.dumps(ack)+'\n'+json.dumps(telemetry)+'\n').encode())
                self.wait(lambda:server.driver_response_input_connected)
                self.wait(lambda:len(server._warning_events)>0)
                second=p.update(np.zeros((8,8,3),dtype=np.uint8),.1)
                response=second.decision.metadata['monitoring_output']['driver_response']
                self.assertTrue(response['warning_display_confirmed'])
                self.assertIsNone(response['last_confirmed_response'])
                event=dict(type='driver_response',event_id='press-1',clip_id='clip',
                           warning_request_id=request,action='cancel_attempt')
                sock.sendall((json.dumps(event)+'\n').encode())
                self.wait(lambda:len(server._driver_response_events)>0)
                third=p.update(np.zeros((8,8,3),dtype=np.uint8),.2)
                self.assertEqual(third.decision.state,DriverState.DANGER)
                with sock.makefile('r') as reader:
                    for _ in range(100):
                        packet=json.loads(reader.readline())
                        if packet.get('source_timestamp')==.2 and packet.get('monitoring'):break
                    else:self.fail('Current monitoring output was not delivered')
                response=packet['monitoring']['driver_response']
                self.assertEqual(packet['driver_state'],'Danger')
                self.assertTrue(response['cancel_attempted'])
                self.assertEqual(response['last_confirmed_response']['applied_source_seconds'],.2)
                self.assertEqual(packet['monitoring']['vision']['danger']['source_seconds'],.2)
                self.assertTrue(packet['response_input_connected'])
            finally:sock.close()

    def test_camera_failure_publishes_unknown_instead_of_retaining_normal_observations(self):
        server=UnityStateServer()
        p=MultimodalProcessor(Vision(),Signal(),{},'clip',publisher=AnalysisPublisher(server))
        p.update(np.zeros((8,8,3),dtype=np.uint8),0.)
        p.publish_camera_failure(.1)
        packet=server.next_packet()
        self.assertFalse(packet['vision_available'])
        self.assertFalse(packet['monitoring']['observation_quality']['camera']['frame_read_ok'])
        self.assertIsNone(packet['monitoring']['vision']['face_visible'])

    def test_actual_tcp_vehicle_feedback_reaches_current_output_without_inventing_stop(self):
        with UnityStateServer(port=0) as server:
            sock=socket.create_connection(('127.0.0.1',server.port),timeout=3)
            try:
                self.wait(lambda:server.connected.is_set())
                processor=MultimodalProcessor(Vision(),Signal(),{},'clip',publisher=AnalysisPublisher(server))
                first=processor.update(np.zeros((8,8,3),dtype=np.uint8),.2)
                self.assertFalse(first.decision.metadata['monitoring_output']['vehicle_confirmation']['stop_confirmed'])
                session=first.decision.metadata['monitoring_output']['session_id']
                feedback=dict(type='vehicle_state',clip_id='clip',session_id=session,
                              has_source_timestamp=True,source_timestamp=.2,speed_kph=0.,emergency_phase='Stopped')
                sock.sendall((json.dumps(feedback)+'\n').encode())
                self.wait(lambda:(server.latest_vehicle_state or {}).get('session_id')==session)
                second=processor.update(np.zeros((8,8,3),dtype=np.uint8),.3)
                report=second.decision.metadata['monitoring_output']
                self.assertTrue(report['vehicle_confirmation']['stop_confirmed'])
                sock.settimeout(3)
                with sock.makefile('r') as reader:
                    for _ in range(100):
                        packet=json.loads(reader.readline())
                        if packet.get('source_timestamp')==.3 and packet.get('monitoring'):break
                    else:self.fail('Current vehicle confirmation was not delivered')
                self.assertTrue(packet['monitoring']['vehicle_confirmation']['stop_confirmed'])
            finally:sock.close()

    def test_first_frame_failure_still_has_a_complete_unknown_report(self):
        server=UnityStateServer();p=MultimodalProcessor(Vision(),Signal(),{},'clip',publisher=AnalysisPublisher(server))
        p.publish_camera_failure(0.)
        packet=server.next_packet()
        self.assertFalse(packet['monitoring']['observation_quality']['camera']['frame_read_ok'])
        self.assertIsNone(packet['monitoring']['vision']['eyes_closed_seconds'])
        self.assertIsNone(packet['monitoring']['vision']['hands_on_wheel'])


if __name__=='__main__':unittest.main()
