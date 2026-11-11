from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest
import socket

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from driver_state_tools.prototype_engine import PrototypeFrame,PrototypeDecisionEngine
from driver_state_tools.decision_engine import DriverState
from driver_state_tools.evidence_dashboard import evidence_content
from driver_state_tools.unity_bridge import vehicle_control_packet
from driver_state_tools.unity_bridge import UnityStateServer
from driver_state_tools.analysis_publisher import AnalysisPublisher


class EyeClosurePendingTests(unittest.TestCase):
    def setUp(self):
        self.rules=json.loads((ROOT/"config/prototype/decision_rules.json").read_text())
        self.engine=PrototypeDecisionEngine(self.rules)
        self.base=PrototypeFrame(0,True,True,False,False,False,False,False,False,False)

    def closed(self,t,**kwargs):
        return replace(self.base,timestamp=t,eyes_closed=True,attention_away=None,**kwargs)

    def test_real_session_shape_normal_pending_then_caution_at_1_5(self):
        self.engine.update(self.base)
        for i in range(16):
            d=self.engine.update(self.closed(1+i/10))
            if i<15:
                self.assertEqual(d.state,DriverState.NORMAL)
                self.assertEqual(d.metadata["decision_phase"],"evaluating")
                self.assertAlmostEqual(d.metadata["pending_events"][0]["elapsed_seconds"],i/10)
                self.assertEqual(d.metadata["pending_events"][0]["threshold_seconds"],1.5)
            else:
                self.assertEqual(d.state,DriverState.CAUTION)
                self.assertIn("eyes_closed",d.reasons)
                self.assertFalse(d.metadata["pending_events"])

    def test_brief_blink_never_turns_into_unavailable(self):
        states=[]
        for i in range(9):
            f=self.closed(i/10) if 2<=i<=4 else replace(self.base,timestamp=i/10)
            states.append(self.engine.update(f).state)
        self.assertEqual(set(states),{DriverState.NORMAL})

    def test_unknown_eyes_or_face_still_unavailable(self):
        for f in (replace(self.base,eyes_closed=None,attention_away=None),self.closed(0,face_readable=False)):
            d=PrototypeDecisionEngine(self.rules).update(f)
            self.assertEqual(d.state,DriverState.UNAVAILABLE)

    def test_unknown_attention_with_open_eyes_is_not_normal(self):
        d=self.engine.update(replace(self.base,attention_away=None))
        self.assertEqual(d.state,DriverState.UNAVAILABLE)
        self.assertEqual(d.metadata["attention_status"],"unavailable")

    def test_pending_does_not_fabricate_forward_or_away(self):
        for i in range(12): d=self.engine.update(self.closed(i/10))
        self.assertIsNone(d.metadata["prototype_frame"]["attention_away"])
        self.assertFalse(d.metadata["observation_status"]["attention"])
        self.assertEqual(d.durations["attention_window_seconds"],0)
        self.assertEqual(d.durations["attention_episode_count"],0)
        self.assertEqual(d.metadata["attention_status"],"not_assessed_eyes_closed")

    def test_previous_attention_warning_is_not_cleared_by_closing_eyes(self):
        for i in range(36): d=self.engine.update(replace(self.base,timestamp=i/10,attention_away=True))
        self.assertEqual(d.state,DriverState.CAUTION)
        for i in range(36,45):
            d=self.engine.update(self.closed(i/10))
            self.assertEqual(d.state,DriverState.CAUTION)
            self.assertEqual(d.reasons,["attention_recovery_unconfirmed"])
        for i in range(45,50): d=self.engine.update(replace(self.base,timestamp=i/10))
        self.assertEqual(d.state,DriverState.NORMAL)

    def test_previous_posture_warning_requires_real_recovery(self):
        for i in range(20): self.engine.update(replace(self.base,timestamp=i/10,collapse_posture=True))
        d=self.engine.update(self.closed(2,collapse_posture=None))
        self.assertEqual(d.state,DriverState.UNAVAILABLE)
        self.assertEqual(d.reasons,["recovery_unobserved"])

    def test_other_valid_warning_overrides_pending_eye_interval(self):
        for i in range(10): self.engine.update(replace(self.base,timestamp=i/10,collapse_posture=True))
        d=self.engine.update(self.closed(1,collapse_posture=True))
        self.assertEqual(d.state,DriverState.CAUTION)
        self.assertIn("collapse_posture",d.reasons)
        self.assertTrue(d.metadata["pending_events"])

    def test_panel_and_packet_explain_pending_without_claiming_attention_available(self):
        d=self.engine.update(self.closed(0))
        c=evidence_content(None,d,self.rules)
        self.assertEqual(c["reason"],"Eyes closed - checking duration")
        self.assertEqual(c["attention_label"],"Eyes closed")
        self.assertFalse(c["meters"]["attention_away"]["known"])
        p=vehicle_control_packet(d,1)
        self.assertEqual(p["driver_state"],"Normal")
        self.assertTrue(p["vision_available"])
        self.assertFalse(p["observation_status"]["attention"])
        self.assertEqual(p["decision_phase"],"evaluating")
        self.assertTrue(p["pending_events"])
        stale=vehicle_control_packet(d,2,age=1)
        self.assertFalse(stale["vision_available"])
        self.assertEqual(stale["decision_phase"],"unavailable")
        self.assertEqual(stale["pending_events"],[])

    def test_gap_resets_pending_timer_and_never_triggers_stop(self):
        for i in range(14): self.engine.update(self.closed(i/10))
        d=self.engine.update(self.closed(3))
        self.assertEqual(d.state,DriverState.NORMAL)
        self.assertEqual(d.durations["eyes_closed"],0)
        self.assertFalse(d.metadata["stop_requested"])

    def test_pending_decision_reaches_real_tcp_mock_without_becoming_unavailable(self):
        # Synthetic observations and a TCP mock only, not Unity rendering/physics.
        with UnityStateServer(port=0,rate_hz=60) as server:
            with socket.create_connection(("127.0.0.1",server.port),timeout=2) as client:
                client.settimeout(2)
                self.assertTrue(server.connected.wait(2))
                publisher=AnalysisPublisher(server)
                with client.makefile("rb") as stream:
                    for i in range(16):
                        d=self.engine.update(self.closed(i/10))
                        revision=publisher.publish(d,"synthetic_eye_pending")
                        while True:
                            packet=json.loads(stream.readline())
                            if packet["prediction_sequence"]>=revision:break
                        self.assertTrue(packet["vision_available"])
                        self.assertEqual(packet["driver_state"],"Normal" if i<15 else "Caution")
                        self.assertFalse(packet["observation_status"]["attention"])
                        if i<15:self.assertEqual(packet["decision_phase"],"evaluating")
                self.assertEqual(publisher.published_count,16)


if __name__=="__main__": unittest.main()
