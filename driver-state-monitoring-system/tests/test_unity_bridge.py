import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from driver_state_tools.decision_engine import Decision, DriverState
from driver_state_tools.unity_bridge import UnityStateServer, rejudge_recording, replay_decisions, vehicle_control_packet


class UnityPacketTest(unittest.TestCase):
    def test_state_only_command_never_overwrites_vehicle_controls(self):
        for state in (DriverState.NORMAL, DriverState.CAUTION, DriverState.DANGER):
            packet = vehicle_control_packet(Decision(4, state), 1)
            self.assertEqual(packet['type'], 'vehicle_control')
            self.assertEqual(packet['driver_state'], state.value.capitalize())
            self.assertTrue(packet['vision_available'])
            self.assertTrue({'throttle','brake','steering','control_mode','remote_enabled','hazard','ecall'}.isdisjoint(packet))

    def test_unavailable_is_not_mapped_to_normal(self):
        packet = vehicle_control_packet(Decision(0, DriverState.UNAVAILABLE), 1)
        self.assertEqual(packet['driver_state'], 'Caution')  # safe old-receiver fallback
        self.assertEqual(packet['vision_state'], 'unavailable')
        self.assertFalse(packet['vision_available'])

    def test_stale_prediction_is_unknown_not_a_fresh_normal_or_danger(self):
        for state in (DriverState.NORMAL, DriverState.DANGER):
            packet = vehicle_control_packet(Decision(0, state), 1, age=2)
            self.assertFalse(packet['vision_available'])
            self.assertEqual(packet['reasons'], ['prediction_stale'])

    def test_heartbeats_do_not_advance_the_engine(self):
        now = [0.]
        server = UnityStateServer(clock=lambda:now[0])
        server.publish(Decision(4, DriverState.CAUTION, durations={'caution_continuous':.2}))
        self.assertEqual(server.next_packet()['caution_seconds'], .2)
        now[0] = 4
        self.assertEqual(server.next_packet()['vision_state'], 'unavailable')

    def test_rejudge_uses_features_not_recorded_labels_or_scenario_name(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'N1.jsonl'
            path.write_text(''.join(json.dumps(dict(timestamp=i/10, clip_id='N1', derived=dict(
                face_readable=True, eyes_readable=True, pose_readable=True, distress_gesture=True)))+'\n'
                for i in range(51)))
            decisions = rejudge_recording(path, {})
            self.assertEqual(decisions[-1][1].state, DriverState.DANGER)

    def test_rejudge_rejects_nonmonotonic_and_mixed_clip_records(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'records.jsonl'
            base = dict(face_readable=True, eyes_readable=True, pose_readable=True)
            for rows in (
                [dict(timestamp=1, derived=base),dict(timestamp=0, derived=base)],
                [dict(timestamp=0, clip_id='A',derived=base),dict(timestamp=1,clip_id='B',derived=base)],
                [dict(timestamp=float('nan'),derived=base)],
                [dict(timestamp=0,derived=dict(base, eyes_closed='false'))],
            ):
                path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
                with self.assertRaises(ValueError):
                    rejudge_recording(path,{})

    def test_cli_help_does_not_load_vision_models(self):
        result = subprocess.run([sys.executable, str(ROOT/'scripts/serve_unity.py'),'--help'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('--records',result.stdout)


class UnitySocketTest(unittest.TestCase):
    def wait_for(self, predicate, timeout=2):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            if predicate(): return
            time.sleep(.01)
        self.fail('Timed out waiting for TCP state')

    def test_fragmented_telemetry_bad_json_and_bidirectional_delivery(self):
        with UnityStateServer(port=0) as server:
            server.publish(Decision(4,DriverState.DANGER))
            with socket.create_connection(('127.0.0.1',server.port),timeout=2) as client:
                client.settimeout(2)
                with client.makefile('rb') as reader:
                    packet=json.loads(reader.readline())
                    self.assertEqual(packet['driver_state'],'Danger')
                    client.sendall(b'not json\n')
                    telemetry=dict(type='vehicle_state',speed_kph=0,ecall=True,emergency_phase='Stopped',note='정차')
                    encoded=(json.dumps(telemetry,ensure_ascii=False)+'\n').encode()
                    for byte in encoded:
                        client.sendall(bytes([byte]))
                    self.wait_for(lambda:server.latest_vehicle_state is not None)
                    self.assertEqual(server.latest_vehicle_state,telemetry)
                    client.sendall(b'{"type":"vehicle_control","driver_state":"Normal"}\n')
                    self.assertEqual(json.loads(reader.readline())['driver_state'],'Danger')

    def test_disconnect_and_reconnect_does_not_reuse_stale_normal(self):
        with UnityStateServer(port=0,stale_after=.1) as server:
            server.publish(Decision(0,DriverState.NORMAL))
            client=socket.create_connection(('127.0.0.1',server.port),timeout=2)
            self.wait_for(server.connected.is_set)
            client.close()
            self.wait_for(lambda:not server.connected.is_set())
            time.sleep(.15)
            with socket.create_connection(('127.0.0.1',server.port),timeout=2) as next_client:
                next_client.settimeout(2)
                with next_client.makefile('rb') as reader:
                    self.assertFalse(json.loads(reader.readline())['vision_available'])

    def test_last_danger_frame_is_sent_before_end_of_recording(self):
        with UnityStateServer(port=0) as server:
            rows=[('test',Decision(0,DriverState.NORMAL)),('test',Decision(.1,DriverState.CAUTION)),
                  ('test',Decision(.2,DriverState.DANGER))]
            cancelled=threading.Event()
            producer=threading.Thread(target=replay_decisions,args=(server,rows,cancelled),daemon=True)
            producer.start()
            try:
                with socket.create_connection(('127.0.0.1',server.port),timeout=2) as client:
                    client.settimeout(2)
                    with client.makefile('rb') as reader:
                        seen=[]
                        deadline=time.monotonic()+2
                        while time.monotonic()<deadline:
                            packet=json.loads(reader.readline())
                            seen.append(packet['vision_state'])
                            if 'danger' in seen and packet['reasons']==['recording_finished']:
                                break
                        self.assertIn('danger',seen)
                        self.assertEqual(packet['reasons'],['recording_finished'])
            finally:
                cancelled.set()
                producer.join(2)
                self.assertFalse(producer.is_alive())


if __name__=='__main__': unittest.main()
