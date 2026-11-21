"""e-Call 수신 화면. MQTT 로 받은 MSD 를 터미널·MQTT 앱 대신 브라우저 화면으로 보여 준다(시연용).

    py ecall_mqtt/ecall_screen.py --broker 192.168.0.64     # Pi 브로커 구독 → 브라우저가 자동으로 열린다
    py ecall_mqtt/ecall_screen.py --demo                    # 연습용: 브로커 없이 예시 MSD 를 몇 초 뒤 표시
    py ecall_mqtt/ecall_screen.py --broker localhost --open-on-ecall   # 차량이 정차해 e-Call 이 올 때 브라우저를 연다

Windows 에서는 Edge(없으면 Chrome) 앱 창으로, 화면 가로 절반 크기로 가운데에, 다른 창보다 위(항상 위)에 띄운다 (screen_window.py).
화면에서 R 키(또는 오른쪽 아래 '초기화')를 누르면 대기 화면으로 돌아간다. --demo 에서는 초기화하면 예시를 다시 보낸다.
같은 공유기의 폰·노트북에서도 보려면 --host 0.0.0.0 으로 켜고 http://<이 PC IP>:8765 를 연다.
표시 문구는 폰 알림(ecall/<VIN>/text)과 같은 msd.msd_sections() 를 쓴다.
"""
import argparse
import datetime
import json
import os
import queue
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from msd import MEDIA_KINDS, MEDIA_KO, build_msd, msd_sections, validate_msd
from mqtt_compat import load_config, make_client, rc_ok, topics
from screen_window import show_screen

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "ecall_screen.html")
PHOTO_LABELS = set(MEDIA_KO.values())


class Hub:
    """화면 상태 하나를 들고, 바뀔 때마다 열려 있는 모든 브라우저(SSE)에 보낸다."""

    def __init__(self, on_ecall=None, **state):
        self.lock = threading.Lock()
        self.clients = []
        self.on_ecall = on_ecall              # MSD 를 받을 때 부를 함수 (예: 브라우저 열기). 열린 화면 수를 넘겨 준다
        self.state = {"connected": False, "pi": "unknown", "ecall": None, "count": 0, **state}

    def update(self, **changes):
        with self.lock:
            self.state.update(changes)
            snapshot = json.dumps(self.state, ensure_ascii=False)
            for q in self.clients:
                q.put(snapshot)

    def receive(self, ecall):
        with self.lock:
            count = self.state["count"] + 1
            viewers = len(self.clients)
        self.update(ecall=ecall, count=count)
        if self.on_ecall:
            self.on_ecall(viewers)

    def join(self):
        q = queue.Queue()
        with self.lock:
            self.clients.append(q)
            q.put(json.dumps(self.state, ensure_ascii=False))
        return q

    def leave(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)


def screen_payload(m, topic):
    """MSD → 화면에 그릴 내용. 사진 링크는 '확장 데이터'로 따로 뺀다."""
    head, sections = msd_sections(m)
    rows = [[{"label": k, "value": v} for k, v in sec if k not in PHOTO_LABELS] for sec in sections]
    media = m.get("media") or {}
    photos = [{"label": MEDIA_KO[k], "url": media[k + "_photo_url"]}
              for k in MEDIA_KINDS if media.get(k + "_photo_url")]
    return {
        "head": head,
        "sections": [sec for sec in rows if sec],
        "photos": photos,
        "errors": validate_msd(m),
        "msd_id": m.get("msd_id"),
        "topic": topic,
        "received": datetime.datetime.now().strftime("%H:%M:%S"),
    }


def demo_msd(cfg):
    """통합본이 실제로 보내는 모양의 예시 (쓰러짐 + 심박 급감소, Unity 좌표 포함)."""
    trigger = {
        "event_id": str(uuid.uuid4()),
        "state": "EMERGENCY",
        "activation": "automatic",
        "position": {"lat": 35.84680, "lon": 127.12940, "confidence": "simulated"},
        "unity_position": {"x": 3.42, "y": 0.0, "z": 412.87},
        "direction_deg": 45,
        "reason": ["slumped_forward", "hr_drop"],
        "hr_bpm": 38,
        "pulse_status": "detected",
    }
    return build_msd(trigger, cfg)


def make_handler(hub, on_reset):
    page = open(PAGE, encoding="utf-8").read().encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):          # 요청마다 찍히는 로그는 끈다
            pass

        def _send(self, code, body=b"", ctype="text/plain; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, page, "text/html; charset=utf-8")
            elif self.path == "/events":
                self._events()
            else:
                self._send(404, b"not found")

        def do_POST(self):
            if self.path == "/reset":
                on_reset()
                self._send(204)
            else:
                self._send(404, b"not found")

        def _events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            q = hub.join()
            try:
                while True:
                    try:
                        msg = q.get(timeout=3)
                        self.wfile.write(f"data: {msg}\n\n".encode("utf-8"))
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")   # 연결 유지 + 닫힌 탭을 빨리 알아채기(--open-on-ecall)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            finally:
                hub.leave(q)

    return Handler


def start_mqtt(hub, broker, port, vehicle_id):
    T = topics(vehicle_id)
    client = make_client(f"ecall-screen-{uuid.uuid4().hex[:8]}")

    def on_connect(c, userdata, flags, rc, properties=None):
        if rc_ok(rc):
            c.subscribe([(T["msd"], 1), (T["status"], 1)])
            hub.update(connected=True)
            print(f"[화면] 브로커 연결 {broker}:{port}, 구독 {T['msd']}")
        else:
            hub.update(connected=False)
            print(f"[화면] 브로커 연결 실패 rc={rc}")

    def on_disconnect(c, userdata, *args):
        hub.update(connected=False)
        print("[화면] 브로커 연결 끊김, 다시 연결하는 중")

    def on_message(c, userdata, msg):
        if msg.topic == T["status"]:
            hub.update(pi=msg.payload.decode("utf-8", "replace").strip() or "unknown")
            return
        try:
            m = json.loads(msg.payload.decode("utf-8"))
            payload = screen_payload(m, msg.topic)
        except Exception as e:                  # 형식이 깨진 MSD 도 화면이 죽지 않게
            print(f"[화면] MSD 해석 실패: {e}")
            return
        hub.receive(payload)
        print(f"[화면] MSD 수신 {payload['received']} id={payload['msd_id']}")

    client.on_connect, client.on_disconnect, client.on_message = on_connect, on_disconnect, on_message
    client.reconnect_delay_set(min_delay=1, max_delay=5)
    client.connect_async(broker, port, keepalive=30)
    client.loop_start()
    return client


def main():
    cfg = load_config()
    ap = argparse.ArgumentParser(description="e-Call 수신 화면 (브라우저)")
    ap.add_argument("--broker", default=cfg["broker_host"], help="브로커(Pi) IP")
    ap.add_argument("--port", type=int, default=cfg["broker_port"])
    ap.add_argument("--host", default="127.0.0.1", help="화면 서버 주소. 다른 기기에서 보려면 0.0.0.0")
    ap.add_argument("--http-port", type=int, default=8765)
    ap.add_argument("--demo", action="store_true", help="브로커 없이 예시 MSD 로 화면 확인")
    ap.add_argument("--demo-delay", type=float, default=4.0, help="--demo 에서 예시를 보내기까지 초")
    ap.add_argument("--no-browser", action="store_true", help="브라우저를 자동으로 열지 않음")
    ap.add_argument("--open-on-ecall", action="store_true",
                    help="시작할 때가 아니라 e-Call(MSD)이 도착할 때 브라우저를 연다. 화면이 이미 열려 있으면 새로 열지 않음")
    a = ap.parse_args()

    url = f"http://localhost:{a.http_port}/"

    def open_if_closed(viewers):
        # 창이 없으면 새로 열고, 이미 열려 있으면 맨 앞으로 가져온다 (Windows: 앱 창, 화면 절반 크기, 항상 위)
        print("[화면] e-Call 도착 → 화면 " + ("열기" if viewers == 0 else "맨 앞으로"))
        show_screen(url)

    vehicle_id = cfg["vehicle_id"]
    msd_topic = topics(vehicle_id)["msd"]
    hub = Hub(on_ecall=open_if_closed if a.open_on_ecall and not a.no_browser else None,
              broker="데모 (브로커 없음)" if a.demo else f"{a.broker}:{a.port}",
              topic=msd_topic, demo=a.demo)
    timer = [None]

    def send_demo():
        hub.receive(screen_payload(demo_msd(cfg), msd_topic))

    def schedule_demo():
        if timer[0]:
            timer[0].cancel()
        timer[0] = threading.Timer(a.demo_delay, send_demo)
        timer[0].daemon = True
        timer[0].start()

    def on_reset():
        hub.update(ecall=None)
        if a.demo:
            schedule_demo()

    if a.demo:
        hub.update(connected=True, pi="online")
        schedule_demo()
    else:
        start_mqtt(hub, a.broker, a.port, vehicle_id)

    server = ThreadingHTTPServer((a.host, a.http_port), make_handler(hub, on_reset))
    server.daemon_threads = True
    print(f"[화면] {url} (끄려면 Ctrl+C)" + (" — e-Call 이 오면 브라우저가 열립니다" if a.open_on_ecall else ""))
    if not a.no_browser and not a.open_on_ecall:
        threading.Timer(0.5, show_screen, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
