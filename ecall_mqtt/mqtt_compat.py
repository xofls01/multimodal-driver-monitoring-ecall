"""paho-mqtt 1.x(라즈베리파이 OS apt 버전)와 2.x(pip 최신) 모두에서 동작하게 하는 도우미."""
import json
import os

import paho.mqtt.client as mqtt

HERE = os.path.dirname(os.path.abspath(__file__))


def load_config(path=None):
    with open(path or os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def topics(vehicle_id):
    base = f"ecall/{vehicle_id}"
    return {
        "trigger": f"{base}/trigger",   # PC(비전) → Pi : e-Call 요청
        "msd": f"{base}/msd",           # Pi → 폰 : MSD (JSON, 프로그램용)
        "text": f"{base}/text",         # Pi → 폰 : 같은 내용을 사람이 읽기 좋은 글로
        "result": f"{base}/result",     # Pi → PC : 전송 결과
        "status": f"{base}/status",     # Pi → 폰 : online / offline (LWT)
    }


def make_client(client_id, clean_session=True):
    """paho 2.x면 VERSION2 콜백, 1.x면 기존 방식으로 생성.
    on_connect는 (client, userdata, flags, rc, properties=None) 형태로 쓰면 둘 다 호환된다."""
    kwargs = dict(client_id=client_id, clean_session=clean_session, protocol=mqtt.MQTTv311)
    if hasattr(mqtt, "CallbackAPIVersion"):
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, **kwargs)
    return mqtt.Client(**kwargs)


def rc_ok(rc):
    """1.x는 int, 2.x는 ReasonCode. 둘 다 0이면 성공."""
    try:
        return int(rc) == 0
    except (TypeError, ValueError):
        return not getattr(rc, "is_failure", True)
