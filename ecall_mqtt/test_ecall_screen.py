"""e-Call 수신 화면 시험 (브로커·브라우저 없이).  실행: py -B -m pytest -q -p no:cacheprovider ecall_mqtt/test_ecall_screen.py"""
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from ecall_screen import Hub, demo_msd, screen_payload, start_mqtt  # noqa: E402
from msd import build_msd, format_msd_text  # noqa: E402
from mqtt_compat import topics  # noqa: E402

CFG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
T = topics(CFG["vehicle_id"])


class FakeMsg:
    def __init__(self, topic, payload):
        self.topic, self.payload = topic, payload if isinstance(payload, bytes) else payload.encode("utf-8")


def photo_msd():
    return build_msd({
        "position": {"lat": 35.8, "lon": 127.1, "confidence": "simulated"}, "direction_deg": 90,
        "reason": ["slumped_forward"], "hr_bpm": 38,
        "media_urls": {"caution": "http://192.168.0.64:8080/e1/caution.jpg",
                       "danger": "http://192.168.0.64:8080/e1/danger.jpg"},
    }, CFG)


class ScreenPayloadTest(unittest.TestCase):
    def test_rows_match_phone_text(self):
        m = demo_msd(CFG)
        p = screen_payload(m, T["msd"])
        text = format_msd_text(m)
        for sec in p["sections"]:
            for r in sec:
                self.assertIn(f"{r['label']} : {r['value']}", text)
        self.assertEqual(p["errors"], [])
        self.assertEqual(p["photos"], [])

    def test_photos_are_split_out_as_extension(self):
        p = screen_payload(photo_msd(), T["msd"])
        labels = [r["label"] for sec in p["sections"] for r in sec]
        self.assertFalse(any(label.startswith("사진") for label in labels))
        self.assertEqual([ph["label"] for ph in p["photos"]], ["사진(주의)", "사진(위험)"])


class OpenOnEcallTest(unittest.TestCase):
    def test_hook_gets_open_viewer_count(self):
        calls = []
        hub = Hub(on_ecall=calls.append, broker="test", topic=T["msd"])
        hub.receive(screen_payload(demo_msd(CFG), T["msd"]))     # 열린 화면 없음 → 0
        q = hub.join()
        hub.receive(screen_payload(demo_msd(CFG), T["msd"]))     # 화면 1개 열림 → 1
        hub.leave(q)
        self.assertEqual(calls, [0, 1])


class MqttPathTest(unittest.TestCase):
    def setUp(self):
        self.hub = Hub(broker="test", topic=T["msd"])
        self.client = start_mqtt(self.hub, "127.0.0.1", 1, CFG["vehicle_id"])   # 연결은 실패해도 된다

    def tearDown(self):
        self.client.loop_stop()

    def test_msd_and_status_reach_screen(self):
        m = photo_msd()
        self.client.on_message(self.client, None, FakeMsg(T["status"], "online"))
        self.client.on_message(self.client, None, FakeMsg(T["msd"], json.dumps(m)))
        q = self.hub.join()
        state = json.loads(q.get_nowait())
        self.assertEqual(state["pi"], "online")
        self.assertEqual(state["count"], 1)
        self.assertEqual(state["ecall"]["msd_id"], m["msd_id"])

    def test_broken_msd_is_ignored(self):
        self.client.on_message(self.client, None, FakeMsg(T["msd"], b"{not json"))
        self.assertIsNone(self.hub.state["ecall"])


if __name__ == "__main__":
    unittest.main()
