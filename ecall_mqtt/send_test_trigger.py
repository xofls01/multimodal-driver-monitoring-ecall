"""비전(PC)이 e-Call을 요청했다고 가정하고 trigger를 보내는 테스트 도구.

예)
  python send_test_trigger.py --broker 192.168.0.20             # Pi의 ecall_service로 요청
  python send_test_trigger.py --broker 192.168.0.20 --scenario E8
  python send_test_trigger.py --direct                          # Pi 서비스 없이 MSD를 바로 폰으로 (단독 확인용)

나중에 비전 코드에서는 request_ecall()만 가져다 쓰면 된다.
"""
import argparse
import json
import threading
import time
import uuid

from mqtt_compat import load_config, make_client, rc_ok, topics
from msd import build_msd, format_msd_text, validate_msd

# 시나리오별 예시 요청 (위치는 예시 좌표, 심박은 측정값이 없으면 None)
SCENARIOS = {
    "E2": {"reason": ["unwell_posture", "no_response"], "hr_bpm": None},
    "E4": {"reason": ["slumped_forward", "no_response"], "hr_bpm": None},
    "E8": {"reason": ["hr_spike", "slumped_forward", "no_response"], "hr_bpm": None},
    "E10": {"reason": ["hr_spike", "unwell_posture"], "hr_bpm": 135},
    "MANUAL": {"reason": ["manual_sos"], "hr_bpm": None, "activation": "manual"},
}


def make_trigger(scenario, lat, lon, direction):
    s = SCENARIOS[scenario]
    return {
        "event_id": str(uuid.uuid4()),
        "scenario": scenario,
        "state": "EMERGENCY",
        "activation": s.get("activation", "automatic"),
        "position": {"lat": lat, "lon": lon, "confidence": "simulated"},
        "direction_deg": direction,
        "reason": s["reason"],
        "hr_bpm": s["hr_bpm"],
    }


def request_ecall(client, vehicle_id, trigger):
    """비전 코드에서 Unity STOPPED를 받았을 때 호출할 함수"""
    return client.publish(topics(vehicle_id)["trigger"], json.dumps(trigger), qos=1)


def main():
    cfg = load_config()
    ap = argparse.ArgumentParser()
    ap.add_argument("--broker", default=cfg["broker_host"], help="브로커(Pi) IP")
    ap.add_argument("--port", type=int, default=cfg["broker_port"])
    ap.add_argument("--scenario", default="E4", choices=SCENARIOS)
    ap.add_argument("--lat", type=float, default=35.8468)
    ap.add_argument("--lon", type=float, default=127.1294)
    ap.add_argument("--direction", type=float, default=90)
    ap.add_argument("--direct", action="store_true", help="Pi 서비스 없이 MSD를 직접 발행")
    a = ap.parse_args()

    VID = cfg["vehicle_id"]
    T = topics(VID)
    trig = make_trigger(a.scenario, a.lat, a.lon, a.direction)

    got_result = threading.Event()

    def on_connect(c, u, flags, rc, properties=None):
        if rc_ok(rc):
            c.subscribe(T["result"], qos=1)

    def on_message(c, u, m):
        r = json.loads(m.payload)
        if r.get("event_id") == trig["event_id"]:
            print("[결과]", r)
            got_result.set()

    c = make_client(f"pc-vision-test-{uuid.uuid4().hex[:6]}")
    c.on_connect, c.on_message = on_connect, on_message
    c.connect(a.broker, a.port, keepalive=10)
    c.loop_start()
    time.sleep(0.5)

    if a.direct:
        msd = build_msd(trig, cfg)
        errs = validate_msd(msd)
        if errs:
            print("MSD 검사 실패:", errs)
            return
        info = c.publish(T["msd"], json.dumps(msd, ensure_ascii=False), qos=1)
        info.wait_for_publish(timeout=3)
        c.publish(T["text"], format_msd_text(msd), qos=1).wait_for_publish(timeout=3)
        print("[직접 전송]", "완료" if info.is_published() else "실패", msd["msd_id"])
    else:
        t0 = time.monotonic()
        request_ecall(c, VID, trig).wait_for_publish(timeout=3)
        print(f"[요청] {a.scenario} event_id={trig['event_id']}")
        if got_result.wait(timeout=10):
            print(f"[지연] 요청 → 전송 결과 {1000 * (time.monotonic() - t0):.0f} ms")
        else:
            print("[결과] 10초 안에 응답 없음. Pi에서 ecall_service.py가 실행 중인지 확인")

    c.loop_stop()
    c.disconnect()


if __name__ == "__main__":
    main()
