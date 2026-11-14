"""e-Call 연결 시험 (Unity·Pi·MQTT 브로커 없이).  실행: py -m pytest -q ecall_mqtt/test_ecall_link.py

가짜 모델 서버와 가짜 Unity 를 실제 TCP 로 붙여 통합본 차량 서버(수정하지 않은 원본)를 그대로 돌린다.
"""
import json
import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "Unity" / "python"))

from ecall_link import EcallLink, build_trigger, map_reasons, unity_to_latlon  # noqa: E402
from msd import build_msd, format_msd_text, validate_msd  # noqa: E402
from run_vehicle_with_ecall import install_hooks  # noqa: E402

CFG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
DANGER_PACKET = {
    "type": "vehicle_control", "driver_state": "Danger", "fusion_state": "danger", "fusion_available": True,
    "vision_available": True, "prediction_age_seconds": 0.01, "clip_id": "E4",
    "reasons": ["VISION:collapse_posture", "VISION:sustained_collapse_stop", "BIO:EXTREME_TACHY", "FUSION:X"],
    "bio_hr": 142.0, "stop_basis": "sustained_collapse_stop",
    "monitoring": {"danger": {"event_id": "danger-0001"}},
}


def fake_link(**kw):
    """기본은 완전 정차 대기 없이(settle_hold_s=0) 속도 0 인 Stopped 를 받자마자 보낸다."""
    kw.setdefault("settle_hold_s", 0.0)
    sent = []
    link = EcallLink(broker="test", vehicle_id=CFG["vehicle_id"], base_lat=35.8468, base_lon=127.1294,
                     publish=lambda topic, payload: sent.append((topic, json.loads(payload))), **kw)
    return link, sent


def vstate(phase, x=4.6, z=812.0, heading=0.4, speed=None):
    if speed is None:
        speed = 0.0 if phase == "Stopped" else 20.0
    return {"type": "vehicle_state", "emergency_phase": phase, "position": {"x": x, "y": 0.4, "z": z},
            "heading_deg": heading, "speed_kph": speed}


class PureLogicTests(unittest.TestCase):
    def setUp(self):
        p = patch("builtins.print"); p.start(); self.addCleanup(p.stop)

    def test_reason_mapping(self):
        self.assertEqual(map_reasons(DANGER_PACKET["reasons"]), ["slumped_forward", "hr_spike"])
        self.assertEqual(map_reasons(["BIO:NO_PULSE_CONFIRMED", "VISION:hand_near_chest"]),
                         ["hr_missing", "unwell_posture"])
        self.assertEqual(map_reasons(None), [])

    def test_position(self):
        lat, lon = unity_to_latlon(0.0, 0.0, 35.8468, 127.1294)
        self.assertEqual((lat, lon), (35.8468, 127.1294))
        lat, lon = unity_to_latlon(0.0, 1113.2, 35.8468, 127.1294)      # 북쪽 약 1.1 km
        self.assertAlmostEqual(lat - 35.8468, 0.01, places=4)

    def test_trigger_becomes_valid_msd_on_pi(self):
        link, _ = fake_link()
        link.on_model_packet(DANGER_PACKET)
        trig = build_trigger(vstate("Stopped", heading=-10), link._danger, base_lat=35.8468, base_lon=127.1294)
        self.assertEqual(trig["model_event_id"], "danger-0001")
        self.assertTrue(trig["event_id"].startswith("danger-0001-"))
        self.assertEqual(trig["direction_deg"], 350.0)                  # 0~359 로 정규화
        msd = build_msd(trig, CFG)                                      # Pi 의 ecall_service 와 같은 함수
        self.assertEqual(validate_msd(msd), [])
        self.assertEqual(msd["driver_state"]["hr_bpm"], 142.0)
        self.assertIn("핸들 앞으로 쓰러짐", format_msd_text(msd))
        self.assertEqual(msd["position"]["unity"], {"x": 4.6, "y": 0.4, "z": 812.0})
        self.assertIn("위치(Unity) : X 4.60, Y 0.40, Z 812.00 (m)", format_msd_text(msd))

    def test_old_trigger_without_unity_position_still_valid(self):
        """send_test_trigger.py 처럼 Unity 좌표가 없는 trigger 도 그대로 동작."""
        msd = build_msd({"event_id": "x", "position": {"lat": 35.8, "lon": 127.1}, "reason": []}, CFG)
        self.assertNotIn("unity", msd["position"])
        self.assertEqual(validate_msd(msd), [])
        self.assertIn("위치 : 35.80000, 127.10000", format_msd_text(msd))

    def _packet(self, state, *, bio_hr=None, disp=None, bio_reasons=(), reasons=()):
        return {"type": "vehicle_control", "fusion_state": state, "driver_state": state.capitalize(),
                "clip_id": "E4", "bio_hr": bio_hr, "bio_display_hr": disp,
                "bio_display_hr_source": "recent_ecg_peaks" if disp else None, "bio_display_hr_verified": False,
                "bio_reasons": list(bio_reasons), "reasons": list(reasons),
                "monitoring": {"danger": {"event_id": "e4-danger"}}}

    def test_pulse_lost_sends_last_hr_before_collapse_not_as_current(self):
        """E4 처럼 빈맥(ECG 193 bpm) → 심장 멈춤 의심 → 맥박 미검출: 현재 심박은 비우고 마지막 심박을 따로 보냄."""
        t = [0.0]
        link, sent = fake_link(clock=lambda: t[0])
        timeline = [
            (1.5, self._packet("normal", disp=74.2, bio_reasons=["CALIBRATING"])),
            (5.1, self._packet("caution", bio_hr=74.5, disp=101.6)),
            (7.6, self._packet("caution", bio_hr=74.5, disp=193.4)),
            (7.9, self._packet("caution", bio_hr=74.5, disp=113.5)),     # 세동 시작, 모델은 아직 모름
            (8.1, self._packet("caution", disp=113.5, bio_reasons=["HEART_PAUSE"])),
            (10.1, self._packet("caution", disp=205.2, bio_reasons=["HOLD_PREVIOUS_LEVEL"])),   # 세동 파형 숫자
            (11.1, self._packet("danger", bio_reasons=["HOLD_PREVIOUS_LEVEL"],
                                reasons=["VISION:collapse_posture", "BIO:HOLD_PREVIOUS_LEVEL"])),
            (15.7, self._packet("danger", bio_reasons=["NO_PULSE_UNCONFIRMED", "HOLD_PREVIOUS_LEVEL"],
                                reasons=["VISION:collapse_posture", "BIO:NO_PULSE_UNCONFIRMED"])),
        ]
        for at, packet in timeline:
            t[0] = at; link.on_model_packet(packet)
        t[0] = 25.0
        for ph in ["SignalAndSlow", "MoveToShoulder", "Stopped"]:
            link.on_vehicle_state(vstate(ph))
        trig = sent[0][1]
        self.assertIsNone(trig["hr_bpm"])                                   # 맥박 없음 → 현재 심박 없음
        self.assertEqual(trig["pulse_status"], "not_detected_unconfirmed")
        self.assertEqual(trig["hr_last_bpm"], 193.4)    # 소실 감지 0.5초 전 값 (113.5·205 는 세동 파형 숫자)
        self.assertEqual(trig["hr_last_age_s"], 17)
        self.assertEqual((trig["hr_last_source"], trig["hr_last_verified"]), ("ecg_peaks", False))
        self.assertEqual(trig["reason"], ["slumped_forward", "hr_missing"])
        msd = build_msd(trig, CFG)
        self.assertEqual(validate_msd(msd), [])
        text = format_msd_text(msd)
        self.assertIn("심박 : 현재 측정 안 됨 — 맥박 미검출(센서 기준, 미확정)", text)
        self.assertIn("마지막 확인 심박 : 193 bpm (심한 빈맥, 정차 약 17초 전, ECG·미검증)", text)
        self.assertNotIn("사망", text)

    def test_unconscious_with_pulse_sends_current_hr(self):
        """의식은 없고 맥박은 있는 경우(심한 서맥 35 bpm): 현재 심박을 그대로 보냄."""
        link, sent = fake_link()
        link.on_model_packet(self._packet("danger", bio_hr=35.0, bio_reasons=["EXTREME_BRADY"],
                                          reasons=["VISION:collapse_posture", "BIO:EXTREME_BRADY"]))
        for ph in ["SignalAndSlow", "MoveToShoulder", "Stopped"]:
            link.on_vehicle_state(vstate(ph))
        trig = sent[0][1]
        self.assertEqual((trig["hr_bpm"], trig["pulse_status"]), (35.0, "detected"))
        self.assertEqual(trig["reason"], ["slumped_forward", "hr_drop"])
        text = format_msd_text(build_msd(trig, CFG))
        self.assertIn("심박 : 35 bpm (심한 서맥)", text)
        self.assertNotIn("마지막 확인 심박", text)

    def test_already_stopped_unity_on_connect_does_not_send(self):
        """이전 실행에서 정차한 채 남아 있던 Unity 가 새 재생기에 붙으면 첫 상태부터 Stopped → 보내지 않음."""
        link, sent = fake_link()
        link.on_model_packet(DANGER_PACKET)
        for _ in range(30):
            link.on_vehicle_state(vstate("Stopped"))
        self.assertEqual(sent, [])
        link.on_vehicle_state(vstate("Idle", speed=0.0))                 # R 리셋
        link.on_vehicle_state(vstate("Idle", speed=40.0))                # 다시 주행 → 재무장
        link.on_model_packet(DANGER_PACKET)
        for ph in ["SignalAndSlow", "MoveToShoulder", "Stopped"]:
            link.on_vehicle_state(vstate(ph))
        self.assertEqual(len(sent), 1)

    def test_waits_for_full_stop_and_sends_final_position(self):
        """Unity 는 0.8 km/h 에서 Stopped 를 보낸다 → 속도 0 이 0.5 초 이어진 뒤의 위치로 보낸다."""
        t = [100.0]
        link, sent = fake_link(settle_hold_s=0.5, settle_timeout_s=3.0, clock=lambda: t[0])
        link.on_model_packet(DANGER_PACKET)
        link.on_vehicle_state(vstate("MoveToShoulder"))
        for z, speed in [(590.0, 0.8), (590.5, 0.4), (590.7, 0.1), (590.75, 0.0), (590.75, 0.0)]:
            link.on_vehicle_state(vstate("Stopped", z=z, speed=speed)); t[0] += 0.05
        self.assertEqual(sent, [])                                        # 아직 0.5 초 안 됨
        while not sent and t[0] < 102:
            link.on_vehicle_state(vstate("Stopped", z=590.75, speed=0.0)); t[0] += 0.05
        self.assertEqual(len(sent), 1)
        trig = sent[0][1]
        self.assertEqual(trig["unity_position"], {"x": 4.6, "y": 0.4, "z": 590.75})
        self.assertEqual(trig["speed_kph_at_send"], 0.0)
        for _ in range(40):                                               # 계속 Stopped 여도 한 번만
            link.on_vehicle_state(vstate("Stopped", z=590.75, speed=0.0)); t[0] += 0.05
        self.assertEqual(len(sent), 1)

    def test_sends_after_timeout_if_never_fully_still(self):
        t = [0.0]
        link, sent = fake_link(settle_hold_s=0.5, settle_timeout_s=3.0, clock=lambda: t[0])
        link.on_vehicle_state(vstate("StraightenAndBrake", speed=5.0))
        link.on_vehicle_state(vstate("Stopped", speed=0.3))
        while not sent and t[0] < 5:
            t[0] += 0.05; link.on_vehicle_state(vstate("Stopped", speed=0.3))
        self.assertEqual(len(sent), 1)
        self.assertGreaterEqual(sent[0][1]["settle_ms"], 3000)

    def test_reset_before_full_stop_cancels(self):
        t = [0.0]
        link, sent = fake_link(settle_hold_s=0.5, clock=lambda: t[0])
        link.on_vehicle_state(vstate("Stopped", speed=0.5))
        t[0] += 0.1; link.on_vehicle_state(vstate("Idle", speed=0.0))      # R 리셋
        for _ in range(100):
            t[0] += 0.05; link.on_vehicle_state(vstate("Idle", speed=0.0))
        self.assertEqual(sent, [])

    def test_once_per_emergency_and_rearm_after_reset(self):
        link, sent = fake_link()
        link.on_model_packet(DANGER_PACKET)
        for ph in ["Idle", "SignalAndSlow", "MoveToShoulder", "StraightenAndBrake", "Stopped", "Stopped", "Stopped"]:
            link.on_vehicle_state(vstate(ph))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], f"ecall/{CFG['vehicle_id']}/trigger")
        self.assertEqual(sent[0][1]["reason"], ["slumped_forward", "hr_spike"])   # Danger 뒤 Idle 이 와도 근거 유지
        self.assertEqual(sent[0][1]["hr_bpm"], 142.0)
        link.on_vehicle_state(vstate("Stopped"))                        # 재접속 후 같은 정차 → 다시 안 보냄
        self.assertEqual(len(sent), 1)
        link.on_vehicle_state(vstate("Idle"))                           # Unity R 리셋
        for ph in ["SignalAndSlow", "StopInPlace", "Stopped"]:          # 제자리 정차 경로도 Stopped 로 끝남
            link.on_vehicle_state(vstate(ph))
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[1][1]["reason"], [])                      # 새 비상: 이전 근거를 재사용하지 않음
        self.assertNotEqual(sent[1][1]["event_id"], sent[0][1]["event_id"])

    def test_replayed_model_event_gets_new_pi_event_id_each_run(self):
        """저장 결과 재생은 매번 같은 모델 event_id → Pi 가 중복으로 버리지 않게 실행마다 새 event_id."""
        ids = []
        for _ in range(2):                                              # 재생기를 두 번 실행
            link, sent = fake_link()
            link.on_model_packet(DANGER_PACKET)
            for ph in ["SignalAndSlow", "MoveToShoulder", "Stopped"]:
                link.on_vehicle_state(vstate(ph))
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0][1]["model_event_id"], "danger-0001")
            ids.append(sent[0][1]["event_id"])
        self.assertNotEqual(ids[0], ids[1])

    def test_single_danger_packet_survives_idle_frames(self):
        """모델이 Danger 를 한 번만 보내고 Unity 가 몇 프레임 Idle 을 보낸 뒤 비상 정차를 시작하는 경우."""
        link, sent = fake_link()
        for _ in range(5):
            link.on_vehicle_state(vstate("Idle"))
        link.on_model_packet(DANGER_PACKET)
        for _ in range(10):                                             # Unity 가 반응하기 전 0.5초
            link.on_vehicle_state(vstate("Idle"))
        for ph in ["SignalAndSlow", "MoveToShoulder", "Stopped"]:
            link.on_vehicle_state(vstate(ph))
        self.assertEqual(sent[0][1]["reason"], ["slumped_forward", "hr_spike"])
        self.assertEqual(sent[0][1]["model_event_id"], "danger-0001")

    def test_reset_while_still_danger_does_not_resend(self):
        """정차 후 state normal 없이 R → 출발점(속도 0)에서 다시 제자리 정차: 같은 사고, 다시 보내지 않음."""
        link, sent = fake_link()
        for ph in ["Idle", "SignalAndSlow", "MoveToShoulder", "Stopped"]:
            link.on_vehicle_state(vstate(ph))
        for _ in range(3):                                              # R 을 세 번
            link.on_vehicle_state(vstate("Idle", x=0.75, z=-5, speed=0.0))
            for ph in ["SignalAndSlow", "StopInPlace", "Stopped"]:
                link.on_vehicle_state(vstate(ph, x=0.75, z=-5, speed=0.0))
        self.assertEqual(len(sent), 1)
        link.on_vehicle_state(vstate("Idle", speed=0.0))                # state normal + R → 다시 달림
        link.on_vehicle_state(vstate("Idle", speed=30.0))
        for ph in ["SignalAndSlow", "MoveToShoulder", "Stopped"]:
            link.on_vehicle_state(vstate(ph))
        self.assertEqual(len(sent), 2)

    def test_heading_near_360_is_valid(self):
        for h in (359.96, 360.0, -0.02, 719.99):
            trig = build_trigger(vstate("Stopped", heading=h), None, base_lat=35.8468, base_lon=127.1294)
            self.assertTrue(0 <= trig["direction_deg"] < 360, (h, trig["direction_deg"]))
            self.assertEqual(validate_msd(build_msd(trig, CFG)), [], h)

    def test_bad_hr_is_dropped(self):
        link, sent = fake_link()
        link.on_model_packet({**DANGER_PACKET, "bio_hr": float("nan")})
        link.on_vehicle_state(vstate("Idle")); link.on_vehicle_state(vstate("Stopped"))
        self.assertIsNone(sent[0][1]["hr_bpm"])
        self.assertEqual(validate_msd(build_msd(sent[0][1], CFG)), [])


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class EndToEndTcpTests(unittest.TestCase):
    """가짜 모델(5002 역할) → 원본 차량 서버 + 알림 → 가짜 Unity(5001 역할)."""

    def setUp(self):
        p = patch("builtins.print"); p.start(); self.addCleanup(p.stop)

    def test_model_danger_then_unity_stopped_publishes_once(self):
        import model_state_client as msc
        import vehicle_tcp_server as vts
        original_set_state, original_extract = vts.SharedData.set_state, msc.extract_model_state
        link, sent = fake_link()
        restore = install_hooks(link)
        try:
            # 가짜 모델 서버: 접속하면 Danger 패킷을 계속 보냄
            model_port, unity_port = free_port(), free_port()
            msrv = socket.socket(); msrv.bind(("127.0.0.1", model_port)); msrv.listen(1)
            stop = threading.Event()

            def model_server():
                conn, _ = msrv.accept()
                with conn:
                    while not stop.is_set():
                        conn.sendall((json.dumps(DANGER_PACKET) + "\n").encode()); time.sleep(0.05)
            threading.Thread(target=model_server, daemon=True).start()

            shared = vts.SharedData(model_input_enabled=True)
            threading.Thread(target=vts.server_loop, args=("127.0.0.1", unity_port, shared, 20.0),
                             daemon=True).start()
            client = vts.ModelStateClient(
                vts.ModelConnectionConfig(host="127.0.0.1", port=model_port), shared.receive_model_result,
                unity_ready=lambda: shared.connected_address is not None)
            client.start()

            unity = None
            for _ in range(50):
                try:
                    unity = socket.create_connection(("127.0.0.1", unity_port), timeout=1); break
                except OSError:
                    time.sleep(0.05)
            self.assertIsNotNone(unity)
            deadline = time.monotonic() + 3
            while shared.command.driver_state != "Danger" and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(shared.command.driver_state, "Danger")        # 원래 판정 경로 그대로
            for ph in ["Idle", "SignalAndSlow", "MoveToShoulder", "Stopped", "Stopped"]:
                unity.sendall((json.dumps(vstate(ph)) + "\n").encode()); time.sleep(0.1)
            time.sleep(0.3)
            self.assertEqual(len(sent), 1)
            trig = sent[0][1]
            self.assertEqual(trig["reason"], ["slumped_forward", "hr_spike"])
            self.assertEqual(trig["hr_bpm"], 142.0)
            self.assertEqual(trig["scenario"], "E4")
            self.assertEqual(validate_msd(build_msd(trig, CFG)), [])
            self.assertEqual(shared.latest_state["emergency_phase"], "Stopped")   # 원래 set_state 동작 유지
            stop.set(); unity.close(); client.stop(); shared.running = False; msrv.close()
        finally:
            restore()
        self.assertIs(vts.SharedData.set_state, original_set_state)          # 끝나면 원본으로 복구
        self.assertIs(msc.extract_model_state, original_extract)

    def test_play_can_entry_is_hooked(self):
        """play_can_model 은 extract_model_state 를 이름으로 따로 가져가므로 그 이름도 감싸야 한다."""
        import model_state_client as msc
        import play_can_model as pcm
        import vehicle_tcp_server as vts
        original_extract = msc.extract_model_state
        link, sent = fake_link()
        restore = install_hooks(link, extra_modules=[pcm])
        try:
            self.assertIsNot(pcm.extract_model_state, original_extract)
            # play_can_model.replay_states 와 같은 호출: 저장된 패킷 → 판정
            shared = vts.SharedData(model_input_enabled=True)
            shared.receive_model_result(pcm.extract_model_state(dict(DANGER_PACKET), 1e9), "saved_result_replay")
            self.assertEqual(shared.command.driver_state, "Danger")
            for ph in ["Idle", "SignalAndSlow", "MoveToShoulder", "Stopped", "Stopped"]:
                shared.set_state(vstate(ph))
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0][1]["scenario"], "E4")
        finally:
            restore()
        self.assertIs(pcm.extract_model_state, original_extract)


if __name__ == "__main__":
    unittest.main()
