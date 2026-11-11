"""Real TCP + analysis entry point, synthetic measured features, mock Unity only."""
import importlib.util
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest

import cv2
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from driver_state_tools.unity_bridge import UnityStateServer, vehicle_control_packet
from driver_state_tools.vision_features import RawFeatures
from driver_state_tools.decision_engine import DriverState, Decision


class CurrentAnalysisTransportTest(unittest.TestCase):
    def test_brief_danger_not_overwritten_before_tcp_send(self):
        with UnityStateServer(port=0) as server:
            server.publish(Decision(6,DriverState.DANGER),"clip")
            server.publish(Decision(6.1,DriverState.NORMAL),"clip")
            with socket.create_connection(("127.0.0.1",server.port),timeout=2) as client:
                client.settimeout(2)
                with client.makefile("rb") as stream:
                    self.assertEqual(json.loads(stream.readline())["driver_state"],"Danger")
                    self.assertEqual(json.loads(stream.readline())["driver_state"],"Normal")

    def test_current_analysis_reaches_publish_and_mock_receives_all_states(self):
        spec=importlib.util.spec_from_file_location("current_analysis_runner",ROOT/"scripts/run_vision_baseline.py")
        runner=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        cfg=json.loads((ROOT/"config/vision_baseline.json").read_text())
        rules=json.loads((ROOT/"config/decision_rules.json").read_text())
        cfg.update(analysis_fps=10,save_annotated_video=False,save_preview_frames_every_seconds=0,decision_rules=rules)

        class SyntheticMeasuredFeatures:
            def start_clip(self,**kwargs): pass
            def extract(self,image,timestamp,**kwargs):
                away=1 <= timestamp < 8
                return RawFeatures(timestamp,True,True,True,ear_left=.3,ear_right=.3,mar=.05,
                                   head_pitch=0,head_yaw=0,torso_angle=0,head_drop_ratio=-.4,pose_head_x=0,
                                   image_height=180,image_width=320,
                                   gaze_observation=dict(state="away" if away else "forward",zone="console" if away else "front",status="ready"))

        class ObservedServer(UnityStateServer):
            def __init__(self):
                super().__init__(port=0,rate_hz=60)
                self.published=[]
            def publish(self,decision,clip_id=""):
                self.published.append(decision)
                revision=super().publish(decision,clip_id)
                # Test synchronization only: guarantee mock consumption of each revision.
                if not self.wait_until_sent(revision,1):
                    raise AssertionError("Current analysis decision did not reach TCP sender")
                return revision

        with tempfile.TemporaryDirectory() as temp, ObservedServer() as server:
            folder=Path(temp)
            video=folder/"synthetic_measurements.mp4"
            writer=cv2.VideoWriter(str(video),cv2.VideoWriter_fourcc(*"mp4v"),10,(320,180))
            self.assertTrue(writer.isOpened())
            for _ in range(100): writer.write(np.zeros((180,320,3),np.uint8))
            writer.release()
            packets=[]
            stop=threading.Event()
            mock_latched=[False]
            errors=[]
            def mock_unity():
                try:
                    with socket.create_connection(("127.0.0.1",server.port),timeout=2) as client:
                        client.settimeout(2)
                        with client.makefile("rb") as stream:
                            acknowledged=set()
                            while not stop.is_set():
                                line=stream.readline()
                                if not line: break
                                packet=json.loads(line)
                                packets.append(packet)
                                if packet["vision_available"] and packet["driver_state"]=="Danger":
                                    mock_latched[0]=True
                                request=packet.get("warning_request_id")
                                if request and request not in acknowledged and packet["vision_available"]:
                                    acknowledged.add(request)
                                    # This is a simulated acknowledgement, NOT a Unity render/physics test.
                                    client.sendall((json.dumps(dict(type="driver_warning_ack",warning_request_id=request,
                                                                   clip_id=packet["clip_id"]))+"\n").encode())
                except Exception as error:
                    errors.append(error)
            client=threading.Thread(target=mock_unity,daemon=True)
            client.start()
            self.assertTrue(server.connected.wait(2))
            out=folder/"run"
            out.mkdir()
            try:
                summary=runner.analyze_video(video,Path(video.name),out,cfg,rules,SyntheticMeasuredFeatures(),
                            state_server=server,calibration_profile=dict(eye_baseline_mean=.3,provenance="synthetic test only"))
                time.sleep(.08)
                self.assertEqual(summary["current_decisions_published"],100)
                self.assertEqual(len(server.published),100)
                self.assertTrue({"Normal","Caution","Danger"}.issubset({p["driver_state"] for p in packets if p["vision_available"]}))
                self.assertTrue(any(not p["vision_available"] and p["vision_state"]=="unavailable" for p in packets))
                self.assertTrue(mock_latched[0])
                self.assertTrue(any(p["driver_state"]=="Normal" for p in packets[-15:] if p["vision_available"]))
                self.assertFalse(summary["unity_editor_run_verified"])
                rows=[json.loads(line) for line in (out/"frame_records/synthetic_measurements.jsonl").read_text().splitlines()]
                self.assertEqual([r["decision"]["state"] for r in rows],[d.state.value for d in server.published])
                dangers=[r for r in rows if r["decision"]["state"]=="danger"]
                self.assertTrue(dangers)
                self.assertTrue(all("away_after_displayed_warning" in r["decision"]["reasons"] for r in dangers))
            finally:
                stop.set()
                client.join(3)
            self.assertFalse(client.is_alive())
            self.assertFalse(errors,errors)

    def test_stale_packet_cannot_request_warning_or_stop(self):
        decision=Decision(1,DriverState.DANGER,metadata=dict(mode="literature",stop_requested=True,warning_request_id="request"))
        packet=vehicle_control_packet(decision,1,age=1)
        self.assertFalse(packet["vision_available"])
        self.assertEqual(packet["vision_state"],"unavailable")
        self.assertFalse(packet["stop_requested"])
        self.assertIsNone(packet["warning_request_id"])
        self.assertFalse(packet["warning_display_confirmed"])
        self.assertEqual(packet["caution_timer_policy"],"not_used")


if __name__=="__main__": unittest.main()
