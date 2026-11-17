"""라즈베리파이 e-Call 서비스.

비전(PC)이 보낸 trigger를 받아 MSD를 만들고 폰(MQTT 클라이언트 앱)으로 전송한다.
- 실패하면 SQLite에 PENDING으로 남겨 두고 5초마다 다시 보낸다 (store-and-forward)
- 같은 event_id는 한 번만 처리한다
- 연구용 테스트 시스템. 119·관제센터로 보내지 않는다.

사진: PC 가 trigger 직전에 …/media/<event_id>/<caution|danger> 로 JPEG 를 보내면 media/<event_id>/ 에 저장하고,
같은 공유기 안에서만 열리는 작은 웹 서버(기본 8080 포트)로 보여 준다. MSD 와 알림 문장에 사진 링크를 넣는다.
(비밀번호 없는 시연용. 다른 사람이 쓰는 공유기에서는 쓰지 말 것)

실행 : python3 ecall_service.py
"""
import functools
import http.server
import json
import os
import queue
import re
import socket
import sqlite3
import threading
import time

from mqtt_compat import load_config, make_client, rc_ok, topics
from msd import MEDIA_KINDS, build_msd, format_msd_text, validate_msd

cfg = load_config()
VID = cfg["vehicle_id"]
T = topics(VID)
RETRY_SEC = 5
MEDIA_DIR = os.path.abspath(cfg.get("media_dir", "media"))
MEDIA_PORT = int(cfg.get("media_http_port", 8080))
MEDIA_MAX_BYTES = 3_000_000
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def lan_ip():
    """폰이 접속할 Pi 의 LAN 주소 (패킷은 보내지 않는다)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


MEDIA_BASE_URL = cfg.get("media_base_url") or f"http://{lan_ip()}:{MEDIA_PORT}"


class MediaHandler(http.server.SimpleHTTPRequestHandler):
    def list_directory(self, path):        # 폴더 목록은 보여 주지 않는다 (사진 주소를 아는 사람만)
        self.send_error(404)
        return None

    def log_message(self, *args):
        pass


def start_media_server():
    os.makedirs(MEDIA_DIR, exist_ok=True)
    handler = functools.partial(MediaHandler, directory=MEDIA_DIR)
    server = http.server.ThreadingHTTPServer(("0.0.0.0", MEDIA_PORT), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[MEDIA] 사진 웹 서버 {MEDIA_BASE_URL}/ (저장 위치 {MEDIA_DIR})")


def save_media(topic, payload):
    """…/media/<event_id>/<kind> 의 JPEG 를 저장한다."""
    parts = topic[len(T["media"]) + 1:].split("/")
    if len(parts) != 2 or not SAFE_ID.match(parts[0]) or parts[1] not in MEDIA_KINDS:
        print("[MEDIA] 알 수 없는 사진 토픽, 무시:", topic)
        return
    if not payload.startswith(b"\xff\xd8") or len(payload) > MEDIA_MAX_BYTES:
        print("[MEDIA] JPEG 가 아니거나 너무 큼, 무시:", topic, len(payload))
        return
    folder = os.path.join(MEDIA_DIR, parts[0])
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, parts[1] + ".jpg"), "wb") as f:
        f.write(payload)
    print(f"[MEDIA] 사진 저장 {parts[0]}/{parts[1]}.jpg ({len(payload) // 1024} KB)")


def media_urls(event_id):
    found = {}
    if SAFE_ID.match(event_id or ""):
        for kind in MEDIA_KINDS:
            if os.path.isfile(os.path.join(MEDIA_DIR, event_id, kind + ".jpg")):
                found[kind] = f"{MEDIA_BASE_URL}/{event_id}/{kind}.jpg"
    return found

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
    c.subscribe(T["media"] + "/+/+", qos=1)
    c.publish(T["status"], "online", qos=1, retain=True)
    print(f"[MQTT] 브로커 연결됨. 대기 중: {T['trigger']}")


def on_disconnect(c, userdata, *args):
    print("[MQTT] 연결 끊김. 자동 재연결 시도 중...")


def on_message(c, userdata, m):
    # 사진과 trigger 를 같은 큐로 받은 순서대로 처리한다 (PC 는 사진을 trigger 보다 먼저 보낸다)
    inbox.put((m.topic, m.payload))


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

    urls = media_urls(event_id) or media_urls(trig.get("model_event_id"))
    if urls:
        trig["media_urls"] = urls
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
    start_media_server()
except OSError as exc:                     # 포트 사용 중 등: 사진 링크 없이 e-Call 은 계속
    print("[MEDIA] 사진 웹 서버를 열 수 없음:", exc)
try:
    while True:
        try:
            topic, payload = inbox.get(timeout=RETRY_SEC)
            if topic.startswith(T["media"] + "/"):
                save_media(topic, payload)
            else:
                handle_trigger(payload)
        except queue.Empty:
            pass
        retry_pending()
except KeyboardInterrupt:
    print("\n[ECALL] 종료")
finally:
    publish_and_wait(T["status"], "offline", timeout=2)
    client.loop_stop()
    client.disconnect()
