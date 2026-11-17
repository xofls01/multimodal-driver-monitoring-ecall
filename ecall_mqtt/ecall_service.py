"""라즈베리파이 e-Call 서비스.

비전(PC)이 보낸 trigger를 받아 MSD를 만들고 폰(MQTT 클라이언트 앱)으로 전송한다.
- 실패하면 SQLite에 PENDING으로 남겨 두고 5초마다 다시 보낸다 (store-and-forward)
- 같은 event_id는 한 번만 처리한다
- 연구용 테스트 시스템. 119·관제센터로 보내지 않는다.

실행 : python3 ecall_service.py
"""
import json
import queue
import sqlite3
import time

from mqtt_compat import load_config, make_client, rc_ok, topics
from msd import build_msd, format_msd_text, validate_msd

cfg = load_config()
VID = cfg["vehicle_id"]
T = topics(VID)
RETRY_SEC = 5

db = sqlite3.connect("ecall_log.db")
db.execute("""CREATE TABLE IF NOT EXISTS msd(
    msd_id TEXT PRIMARY KEY, event_id TEXT, body TEXT,
    status TEXT, retry_count INTEGER, created_utc TEXT, sent_mono REAL)""")
db.commit()

inbox = queue.Queue()      # 콜백 안에서 오래 기다리면 MQTT 통신이 멈추므로 큐로 넘긴다


def on_connect(c, userdata, flags, rc, properties=None):
    if not rc_ok(rc):
        print("[MQTT] 연결 실패:", rc)
        return
    c.subscribe(T["trigger"], qos=1)
    c.publish(T["status"], "online", qos=1, retain=True)
    print(f"[MQTT] 브로커 연결됨. 대기 중: {T['trigger']}")


def on_disconnect(c, userdata, *args):
    print("[MQTT] 연결 끊김. 자동 재연결 시도 중...")


def on_message(c, userdata, m):
    inbox.put(m.payload)


client = make_client(f"vehicle-{VID}", clean_session=False)
client.on_connect = on_connect
client.on_disconnect = on_disconnect
client.on_message = on_message
client.will_set(T["status"], "offline", qos=1, retain=True)
client.reconnect_delay_set(min_delay=1, max_delay=5)
client.connect(cfg["broker_host"], cfg["broker_port"], keepalive=10)
client.loop_start()


def publish_and_wait(topic, body, timeout=3.0):
    info = client.publish(topic, body, qos=1)
    try:
        info.wait_for_publish(timeout=timeout)
    except Exception:          # 연결이 없으면 예외가 날 수 있음
        pass
    return info.is_published()


def report(event_id, msd_id, status):
    client.publish(T["result"], json.dumps(
        {"event_id": event_id, "msd_id": msd_id, "status": status}), qos=1)


def try_send(msd_id, event_id, body, retry_count):
    if publish_and_wait(T["msd"], body):
        # 같은 내용을 사람이 읽기 좋은 글로도 보낸다 (실패해도 MSD 전송 결과에는 영향 없음)
        client.publish(T["text"], format_msd_text(json.loads(body)), qos=1)
        db.execute("UPDATE msd SET status='SENT', retry_count=?, sent_mono=? WHERE msd_id=?",
                   (retry_count, time.monotonic(), msd_id))
        db.commit()
        report(event_id, msd_id, "SENT")
        print(f"[ECALL] MSD 전송 완료 {msd_id} (재시도 {retry_count}회)")
        return True
    db.execute("UPDATE msd SET retry_count=? WHERE msd_id=?", (retry_count + 1, msd_id))
    db.commit()
    print(f"[ECALL] 전송 실패, {RETRY_SEC}초 후 재시도 {msd_id}")
    return False


def retry_pending():
    rows = db.execute("SELECT msd_id, event_id, body, retry_count FROM msd "
                      "WHERE status='PENDING'").fetchall()
    for msd_id, event_id, body, n in rows:
        try_send(msd_id, event_id, body, n)


def handle_trigger(raw):
    try:
        trig = json.loads(raw)
    except json.JSONDecodeError:
        print("[ECALL] trigger JSON 오류:", raw[:80])
        return
    event_id = trig.get("event_id")
    if not event_id:
        print("[ECALL] event_id 없는 trigger는 무시")
        return
    if db.execute("SELECT 1 FROM msd WHERE event_id=?", (event_id,)).fetchone():
        print("[ECALL] 이미 처리한 event_id, 무시:", event_id)
        return

    msd = build_msd(trig, cfg)
    errs = validate_msd(msd)
    if errs:
        print("[ECALL] MSD 검사 실패:", errs)
        report(event_id, msd["msd_id"], "INVALID")
        return

    body = json.dumps(msd, ensure_ascii=False)
    db.execute("INSERT INTO msd VALUES(?,?,?,?,?,?,NULL)",
               (msd["msd_id"], event_id, body, "PENDING", 0, msd["time_utc"]))
    db.commit()
    print(f"[ECALL] trigger 수신 event_id={event_id} → MSD 생성 {msd['msd_id']}")
    try_send(msd["msd_id"], event_id, body, 0)


print(f"[ECALL] 서비스 시작. 차량 {VID}, 브로커 {cfg['broker_host']}:{cfg['broker_port']}")
try:
    while True:
        try:
            handle_trigger(inbox.get(timeout=RETRY_SEC))
        except queue.Empty:
            pass
        retry_pending()
except KeyboardInterrupt:
    print("\n[ECALL] 종료")
finally:
    publish_and_wait(T["status"], "offline", timeout=2)
    client.loop_stop()
    client.disconnect()
