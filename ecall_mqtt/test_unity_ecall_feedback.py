"""Unity 완료 표시 전달만 검증한다. 실제 MQTT 발행·Unity 실행 없음."""
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "Unity" / "python"))

from run_vehicle_with_ecall import UnityEcallLink, install_hooks


def state(phase):
    return {"type": "vehicle_state", "emergency_phase": phase,
            "speed_kph": 0.0 if phase == "Stopped" else 60.0,
            "position": {"x": 4.6, "y": 0.4, "z": 812.0}, "heading_deg": 0.0}


class UnityFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.print_mock = patch("builtins.print").start()
        self.addCleanup(patch.stopall)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.log = Path(folder.name) / "results.jsonl"

    def link(self, **kwargs):
        kwargs.setdefault("publish", lambda topic, payload: None)
        return UnityEcallLink(broker="test", vehicle_id="VIN-TEST-0001", base_lat=35.8468,
                              base_lon=127.1294, settle_hold_s=0.0, log_path=self.log, **kwargs)

    def stop_vehicle(self, link):
        link.on_vehicle_state(state("Idle"))
        link.on_vehicle_state(state("MoveToShoulder"))
        return link.on_vehicle_state(state("Stopped"))["event_id"]

    def result(self, link, event_id, status="SENT", msd_id="msd-1"):
        link._log({"type": "result", "event_id": event_id, "status": status, "msd_id": msd_id})

    def test_original_mqtt_callback_logs_every_result_and_only_success_updates_ui(self):
        client = MagicMock()
        link = self.link(publish=None)
        with patch("mqtt_compat.make_client", return_value=client):
            link.start()
        self.addCleanup(link.stop)
        event_id = self.stop_vehicle(link)
        results = [
            {"event_id": event_id, "status": "INVALID", "msd_id": None},
            {"event_id": "unrelated", "status": "SENT", "msd_id": "other-msd"},
            {"event_id": event_id, "status": "SENT", "msd_id": ""},
            {"event_id": event_id, "status": "SENT", "msd_id": "msd-1"},
            {"event_id": event_id, "status": "SENT", "msd_id": "msd-1"},
        ]
        for i, result in enumerate(results):
            client.on_message(client, None, SimpleNamespace(payload=json.dumps(result).encode()))
            if i < 3:
                self.assertEqual(link.unity_feedback(), {})
        self.assertEqual(link.unity_feedback(), {"ecall_status": "SENT", "ecall_event_id": event_id,
                                                 "ecall_msd_id": "msd-1"})
        records = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual([{k: v for k, v in row.items() if k not in {"logged_utc", "type"}}
                          for row in records if row["type"] == "result"], results)
        printed = [c for c in self.print_mock.call_args_list if "Pi 결과" in str(c)]
        self.assertEqual(len(printed), len(results))
        self.assertEqual(client.publish.call_count, 1)  # 기존 trigger 외에 추가 발행 없음

    def test_reset_rejects_late_old_result_until_new_request(self):
        link = self.link()
        first = self.stop_vehicle(link)
        self.result(link, first)
        link.on_vehicle_state(state("Idle"))
        self.assertEqual(link.unity_feedback(), {})
        self.result(link, first)
        self.assertEqual(link.unity_feedback(), {})
        link.settle_hold_s = 1.0
        link.on_vehicle_state(state("MoveToShoulder"))
        link.on_vehicle_state(state("Stopped"))
        self.result(link, first)
        self.assertEqual(link.unity_feedback(), {})  # 새 정차라도 아직 요청을 보내지 않음
        link.settle_hold_s = 0.0
        second = link.on_vehicle_state(state("Stopped"))["event_id"]
        self.result(link, first)
        self.assertEqual(link.unity_feedback(), {})
        self.result(link, second)
        self.assertEqual(link.unity_feedback()["ecall_event_id"], second)
        self.assertNotEqual(first, second)

    def test_fast_result_during_publish_is_not_lost(self):
        def publish(topic, payload):
            self.result(link, json.loads(payload)["event_id"])
        link = self.link(publish=publish)
        event_id = self.stop_vehicle(link)
        self.assertEqual(link.unity_feedback()["ecall_event_id"], event_id)

    def test_control_fields_unchanged_and_hooks_restored(self):
        import vehicle_tcp_server as vts
        link = self.link()
        event_id = self.stop_vehicle(link)
        original = vts.SharedData.next_command_json
        restore = install_hooks(link)
        try:
            shared, baseline = vts.SharedData(), vts.SharedData()
            # 응답 전에는 원래 직렬화 결과와 완전히 같다.
            self.assertEqual(shared.next_command_json(), original(baseline))
            self.result(link, event_id)
            for driver_state in ("Normal", "Caution", "Danger"):
                shared.set_driver_state(driver_state)
                baseline.set_driver_state(driver_state)
                packet = json.loads(shared.next_command_json())
                feedback = {k: packet.pop(k) for k in ("ecall_status", "ecall_event_id", "ecall_msd_id")}
                self.assertEqual(packet, json.loads(original(baseline)))
                self.assertEqual(feedback["ecall_event_id"], event_id)
            shared.set_state(state("Idle"))
            self.assertNotIn("ecall_status", json.loads(shared.next_command_json()))
        finally:
            restore()
        self.assertIs(vts.SharedData.next_command_json, original)

    def test_display_failure_preserves_original_command(self):
        import vehicle_tcp_server as vts
        link = self.link()
        original = vts.SharedData.next_command_json
        restore = install_hooks(link)
        try:
            shared, baseline = vts.SharedData(), vts.SharedData()
            with patch.object(link, "unity_feedback", side_effect=RuntimeError("display only")):
                self.assertEqual(shared.next_command_json(), original(baseline))
        finally:
            restore()

    def test_sent_result_reaches_unity_over_tcp(self):
        import vehicle_tcp_server as vts
        link = self.link()
        restore = install_hooks(link)
        shared = vts.SharedData()
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        server = threading.Thread(target=vts.server_loop,
                                  args=("127.0.0.1", port, shared, 20.0), daemon=True)
        unity = None
        try:
            server.start()
            for _ in range(50):
                try:
                    unity = socket.create_connection(("127.0.0.1", port), timeout=1)
                    break
                except OSError:
                    time.sleep(0.02)
            self.assertIsNotNone(unity)
            for phase in ("Idle", "MoveToShoulder", "Stopped"):
                unity.sendall((json.dumps(state(phase)) + "\n").encode())
            deadline = time.monotonic() + 2
            while not link.sent_triggers and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(link.sent_triggers), 1)
            event_id = link.sent_triggers[0]["event_id"]
            self.result(link, event_id)
            packet = {}
            with unity.makefile("rb") as stream:
                while time.monotonic() < deadline:
                    packet = json.loads(stream.readline())
                    if packet.get("ecall_status") == "SENT":
                        break
            self.assertEqual(packet.get("ecall_status"), "SENT")
            self.assertEqual(packet.get("ecall_event_id"), event_id)
            self.assertEqual(packet.get("ecall_msd_id"), "msd-1")
            self.assertEqual(shared.latest_state["emergency_phase"], "Stopped")
        finally:
            shared.running = False
            if unity is not None:
                unity.close()
            restore()
            server.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
