"""Unity 정차(Stopped) → 라즈베리파이 e-Call 서비스로 MQTT trigger 한 번 발행 (PC 에서 사용).

    py run_vehicle_with_ecall.py --ecall-broker <Pi IP> [vehicle_tcp_server.py 옵션 그대로]
    통합본 코드는 수정하지 않는다. run_vehicle_with_ecall.py 가 실행 중에만 연결한다.

흐름
  1. 모델 패킷(5002)에서 Danger 가 나올 때의 사유·심박·정차 근거를 기억한다 (on_model_packet).
  2. Unity vehicle_state 의 emergency_phase 가 'Stopped' 로 바뀐 뒤 차가 완전히 멈추면 trigger 를 만든다
     (on_vehicle_state). Unity 는 0.8 km/h 이하에서 Stopped 를 보내고 그때부터 제동하므로,
     속도 0 이 SETTLE_HOLD_S 동안 유지될 때(최대 SETTLE_TIMEOUT_S) 최종 정차 위치로 보낸다.
  3. trigger 는 ../../ecall_mqtt 의 형식 그대로 ecall/<vehicle_id>/trigger 로 QoS 1 발행.
     Pi 의 ecall_service.py 가 MSD 를 만들어 폰으로 보내고 ecall/<vehicle_id>/result 로 결과를 돌려준다.
  4. 같은 비상 상황에서는 한 번만 보낸다. Unity 가 Idle 로 돌아오면(R 리셋, Play 재시작) 다시 보낼 수 있다.

위치는 Unity 좌표(m)를 기준 위경도에 더한 가상 값이다 (confidence='simulated').
연구용 테스트이며 119·관제센터로 보내지 않는다.
"""
from __future__ import annotations

import json
import math
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ECALL_DIR = Path(__file__).resolve().parent
STOPPED = "Stopped"
IDLE_PHASES = {None, "", "Idle", "Unavailable"}
REARM_SPEED_KPH = 5.0   # e-Call 을 보낸 뒤 이 속도 이상으로 다시 달려야 다음 e-Call 을 보냄
STILL_SPEED_KPH = 0.05  # 이 속도 이하면 멈춘 것으로 본다
SETTLE_HOLD_S = 0.5     # 멈춘 상태가 이만큼 이어지면 최종 정차로 보고 전송
SETTLE_TIMEOUT_S = 3.0  # Stopped 후 이 시간이 지나면 완전히 멈추지 않았어도 전송

# 모델 사유 → ecall_mqtt/msd.py 의 REASON_KO 코드 (알림 문장에 한국어로 표시됨)
REASON_MAP = {
    "VISION:eyes_closed": "eyes_closed",
    "VISION:repeated_long_closures": "eyes_closed",
    "VISION:eye_ratio_high": "eyes_closed",
    "VISION:collapse_posture": "slumped_forward",
    "VISION:sustained_collapse_stop": "slumped_forward",
    "VISION:torso_lean": "body_tilt",
    "VISION:hand_near_chest": "unwell_posture",
    "VISION:nod_proxy": "head_down",
    "VISION:repeated_head_dips": "head_down",
    "BIO:EXTREME_TACHY": "hr_spike",
    "BIO:HR_HIGH": "hr_spike",
    "BIO:HR_RISING": "hr_spike",
    "BIO:EXTREME_BRADY": "hr_drop",
    "BIO:HR_LOW": "hr_drop",
    "BIO:HR_FALLING": "hr_drop",
    "BIO:NO_PULSE_CONFIRMED": "hr_missing",
    "BIO:NO_PULSE_UNCONFIRMED": "hr_missing",
    "BIO:ABNORMAL_ECG_NO_PULSE": "hr_missing",
    "BIO:HEART_PAUSE": "hr_missing",
}


def map_reasons(model_reasons: list[str]) -> list[str]:
    """알려진 사유만 e-Call 코드로 바꾸고 순서를 지키며 중복을 없앤다."""
    out: list[str] = []
    for r in model_reasons or []:
        code = REASON_MAP.get(r) or REASON_MAP.get("VISION:" + r)
        if code and code not in out:
            out.append(code)
    return out


def unity_to_latlon(x: float, z: float, base_lat: float, base_lon: float) -> tuple[float, float]:
    """Unity +z(도로 진행 방향)를 북쪽, +x 를 동쪽으로 보고 m 를 위경도로 바꾼다."""
    lat = base_lat + z / 111_320.0
    lon = base_lon + x / (111_320.0 * math.cos(math.radians(base_lat)))
    return round(lat, 7), round(lon, 7)


def _valid_hr(hr: Any) -> float | None:
    if isinstance(hr, bool) or not isinstance(hr, (int, float)) or not math.isfinite(hr):
        return None
    return round(float(hr), 1) if 0 < hr < 300 else None


# 맥박 소실을 뜻하는 생체 사유 (모델 bio_reasons, 또는 reasons 의 "BIO:" 접두 제거)
PULSE_LOST_CONFIRMED = {"NO_PULSE_CONFIRMED"}
PULSE_LOST_UNCONFIRMED = {"NO_PULSE_UNCONFIRMED", "ABNORMAL_ECG_NO_PULSE", "HEART_PAUSE"}
PULSE_LOST = PULSE_LOST_CONFIRMED | PULSE_LOST_UNCONFIRMED
HR_MAX_AGE_S = 120.0    # 이보다 오래된 마지막 심박은 보내지 않음
# 모델이 맥박 소실을 표시하기까지 걸리는 시간. E4 에서 세동 시작(7.5 s) → HEART_PAUSE(8.11 s) 약 0.6 s.
# 그 사이 ECG 봉우리로 잰 숫자는 이미 부정맥 파형이므로, 소실 감지 시각에서 이만큼 앞선 값을 마지막 심박으로 쓴다.
LOSS_GUARD_S = 0.5


def _bio_reasons(packet: dict) -> set[str]:
    out = {str(r) for r in packet.get("bio_reasons") or []}
    out |= {str(r)[4:] for r in packet.get("reasons") or [] if str(r).startswith("BIO:")}
    return out


class VitalsTracker:
    """모델 패킷마다 맥박 상태와 '맥박이 잡히던 동안의 마지막 심박'을 기억한다.

    - 현재 심박(hr_bpm)은 모델이 확인한 bio_hr 만 쓴다(맥박 소실 중에는 보내지 않음).
    - 마지막 심박은 맥박이 잡히는 동안의 값만 기록한다. 맥박 소실 후 ECG 에서 잡히는 숫자
      (예: 심실세동 파형의 봉우리)는 심박이 아니므로 기록하지 않는다.
    - ECG 봉우리로 잰 화면용 심박(bio_display_hr)이 모델 확인 심박보다 빨리 변화를 따라가므로 먼저 쓰고,
      그 값이 모델 검증을 거치지 않았다는 것(hr_last_verified=False)도 함께 보낸다.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.pulse_status = "unknown"
        self.current_hr: float | None = None
        self.last: dict | None = None      # {bpm, at, source, verified}
        self._recent: list[dict] = []      # 맥박이 잡히던 동안의 최근 값들

    def update(self, packet: dict, now: float) -> None:
        reasons = _bio_reasons(packet)
        verified_hr = _valid_hr(packet.get("bio_hr"))
        was_lost = self.pulse_status.startswith("not_detected")
        if reasons & PULSE_LOST and not was_lost and self._recent:
            # 맥박 소실을 처음 감지: 감지 지연 구간(LOSS_GUARD_S) 이전의 마지막 값으로 고정
            before = [r for r in self._recent if r["at"] <= now - LOSS_GUARD_S]
            self.last = (before or self._recent)[-1]
        if reasons & PULSE_LOST_CONFIRMED:
            self.pulse_status = "not_detected_confirmed"
        elif reasons & PULSE_LOST_UNCONFIRMED:
            self.pulse_status = "not_detected_unconfirmed"
        elif verified_hr is not None:
            self.pulse_status = "detected"
        # 그 밖(HOLD_PREVIOUS_LEVEL, CALIBRATING 등)은 직전 상태 유지
        lost = self.pulse_status.startswith("not_detected")
        self.current_hr = None if lost else verified_hr
        if lost or reasons & PULSE_LOST:
            return
        display_hr = _valid_hr(packet.get("bio_display_hr"))
        reading = None
        if display_hr is not None:
            source = str(packet.get("bio_display_hr_source") or "display")
            reading = {"bpm": display_hr, "at": now,
                       "source": "ecg_peaks" if "ecg" in source else "ppg_peaks" if "ppg" in source else source,
                       "verified": bool(packet.get("bio_display_hr_verified"))}
        elif verified_hr is not None:
            reading = {"bpm": verified_hr, "at": now, "source": "model", "verified": True}
        if reading is not None:
            self.last = reading
            self._recent = [r for r in self._recent if r["at"] >= now - 10.0] + [reading]

    def fields(self, now: float) -> dict:
        last = self.last if self.last and now - self.last["at"] <= HR_MAX_AGE_S else None
        return {
            "pulse_status": self.pulse_status,
            "hr_last_bpm": last["bpm"] if last else None,
            "hr_last_age_s": round(now - last["at"]) if last else None,
            "hr_last_source": last["source"] if last else None,
            "hr_last_verified": last["verified"] if last else None,
        }


def build_trigger(state: dict, danger: dict | None, *, base_lat: float, base_lon: float,
                  vitals: dict | None = None) -> dict:
    """ecall_mqtt/send_test_trigger.make_trigger() 와 같은 형식 + 근거 필드(Pi MSD 에는 들어가지 않음).

    vitals: VitalsTracker.fields() — 맥박 상태, 마지막 심박. 있으면 현재 심박도 그 판단을 따른다.
    """
    pos = state.get("position") or {}
    lat, lon = unity_to_latlon(float(pos.get("x", 0.0)), float(pos.get("z", 0.0)), base_lat, base_lon)
    heading = state.get("heading_deg")
    # 359.96 → round → 360.0 이 되면 Pi 검사(0~359)에 걸리므로 한 번 더 360 으로 나눈 나머지
    direction = round(float(heading) % 360.0, 1) % 360.0 if isinstance(heading, (int, float)) else None
    danger = danger or {}
    model_reasons = list(danger.get("reasons") or [])
    model_event_id = danger.get("event_id")
    vitals = vitals or {}
    hr_now = vitals["current_hr"] if "current_hr" in vitals else _valid_hr(danger.get("bio_hr"))
    trigger = {
        # Pi 는 같은 event_id 를 한 번만 처리한다. 저장된 결과 재생은 매번 같은 모델 event_id 를
        # 보내므로, 발송마다 새 id 를 붙이고 모델 id 는 model_event_id 로 따로 남긴다.
        "event_id": f"{model_event_id}-{uuid.uuid4().hex[:8]}" if model_event_id else str(uuid.uuid4()),
        "model_event_id": model_event_id,
        "scenario": danger.get("clip_id") or state.get("clip_id") or "",
        "state": "EMERGENCY",
        "activation": "automatic",
        "position": {"lat": lat, "lon": lon, "confidence": "simulated"},
        "direction_deg": direction,
        "reason": map_reasons(model_reasons),
        "hr_bpm": hr_now,                       # 현재 심박(모델 확인값). 맥박 소실 중이면 None
        # Unity 좌표(m). Pi 의 msd.py 가 MSD position.unity 와 알림 문장에 넣는다.
        "unity_position": {k: _round_coord(pos.get(k)) for k in ("x", "y", "z")},
        "speed_kph_at_send": state.get("speed_kph"),
        # 아래는 근거 기록용. Pi 의 build_msd() 는 읽지 않는다.
        "model_reasons": model_reasons,
        "stop_basis": danger.get("stop_basis"),
        "stopped_at_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    }
    for k in ("pulse_status", "hr_last_bpm", "hr_last_age_s", "hr_last_source", "hr_last_verified"):
        if k in vitals:
            trigger[k] = vitals[k]               # Pi 의 msd.py 가 MSD driver_state 와 알림 문장에 넣는다
    return trigger


def _round_coord(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return round(float(v), 2)


class EcallLink:
    """vehicle_tcp_server 에서 호출. publish(topic, payload) 는 테스트에서 바꿔 끼울 수 있다."""

    def __init__(self, *, broker: str, port: int = 1883, vehicle_id: str,
                 base_lat: float, base_lon: float, log_path: Path | None = None,
                 publish: Callable[[str, str], None] | None = None,
                 settle_hold_s: float = SETTLE_HOLD_S, settle_timeout_s: float = SETTLE_TIMEOUT_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.broker, self.port, self.vehicle_id = broker, port, vehicle_id
        self.settle_hold_s, self.settle_timeout_s, self._clock = settle_hold_s, settle_timeout_s, clock
        self._pending_since: float | None = None   # Stopped 를 받은 시각 (완전 정차 대기 중)
        self._still_since: float | None = None     # 속도 0 이 시작된 시각
        self.base_lat, self.base_lon = base_lat, base_lon
        self.log_path = log_path
        self.trigger_topic = f"ecall/{vehicle_id}/trigger"
        self.result_topic = f"ecall/{vehicle_id}/result"
        self._publish = publish
        self._client = None
        self._lock = threading.Lock()
        self._danger: dict | None = None
        self._armed = True
        self._last_phase: str | None = None
        self._sent: dict[str, float] = {}      # event_id → 발행 시각(monotonic)
        self._sent_model_events: set[str] = set()   # 이번 실행에서 이미 보낸 모델 Danger event_id
        self._vitals = VitalsTracker()
        self._model_seen = False                   # 모델 패킷을 받은 적이 있는지 (수동 state danger 와 구분)
        self.sent_triggers: list[dict] = []

    # ---------- MQTT ----------
    def start(self) -> None:
        if self._publish is not None:
            return
        if str(ECALL_DIR) not in sys.path:
            sys.path.insert(0, str(ECALL_DIR))
        from mqtt_compat import make_client, rc_ok  # paho-mqtt 필요 (pip install paho-mqtt)

        client = make_client(f"pc-vehicle-bridge-{uuid.uuid4().hex[:6]}")

        def on_connect(c, userdata, flags, rc, properties=None):
            if rc_ok(rc):
                c.subscribe(self.result_topic, qos=1)
                print(f"[ECALL] Pi 브로커 연결됨 {self.broker}:{self.port}")
            else:
                print(f"[ECALL] Pi 브로커 연결 실패: {rc}")

        def on_message(c, userdata, m):
            try:
                r = json.loads(m.payload)
            except ValueError:
                return
            t0 = self._sent.get(r.get("event_id"))
            took = f", 요청→전송결과 {1000 * (self._clock() - t0):.0f} ms" if t0 else ""
            print(f"[ECALL] Pi 결과 {r.get('status')} msd_id={r.get('msd_id')}{took}")
            self._log({"type": "result", **r})

        client.on_connect, client.on_message = on_connect, on_message
        client.reconnect_delay_set(min_delay=1, max_delay=5)
        client.connect_async(self.broker, self.port, keepalive=10)   # Pi 가 늦게 켜져도 계속 재시도
        client.loop_start()
        self._client = client
        self._publish = lambda topic, payload: client.publish(topic, payload, qos=1)

    def stop(self) -> None:
        if self._client is not None:
            self._client.loop_stop()
            self._client.disconnect()

    # ---------- vehicle_tcp_server 에서 호출 ----------
    def on_model_packet(self, packet: dict) -> None:
        """모든 패킷에서 맥박·심박을, Danger 패킷에서는 근거를 기억 (정차 시점에는 모델이 이미 Normal 일 수 있음)."""
        with self._lock:
            self._model_seen = True
            self._vitals.update(packet, self._clock())
        label = packet.get("fusion_state") or packet.get("driver_state")
        if not isinstance(label, str) or label.strip().lower() != "danger":
            return
        monitoring = packet.get("monitoring") or {}
        danger = monitoring.get("danger") if isinstance(monitoring, dict) else None
        with self._lock:
            self._danger = {
                "reasons": list(packet.get("reasons") or []),
                "bio_hr": packet.get("bio_hr"),
                "stop_basis": packet.get("stop_basis"),
                "clip_id": packet.get("clip_id"),
                "event_id": danger.get("event_id") if isinstance(danger, dict) else None,
            }

    def on_vehicle_state(self, state: dict) -> dict | None:
        """Stopped 후 완전히 멈춘 순간 trigger 를 발행하고 그 내용을 돌려준다. 그 외에는 None."""
        phase = state.get("emergency_phase")
        now = self._clock()
        with self._lock:
            previous, self._last_phase = self._last_phase, phase
            if phase != STOPPED:
                self._pending_since = self._still_since = None   # 정차 전 리셋 등 → 대기 취소
            if phase in IDLE_PHASES:
                # Danger 직후 Unity 가 반응하기 전에도 Idle 이 온다 → 그때는 근거를 지우면 안 된다.
                # 비상 단계에서 Idle 로 돌아온 경우(R 리셋, Play 재시작)에만 이전 근거를 버린다.
                if previous not in IDLE_PHASES:
                    self._danger = None
                    self._vitals.reset()
                # R 리셋 직후 Danger 가 그대로면 출발점에서 다시 제자리 정차한다 → 같은 사고로 보고
                # 다시 보내지 않는다. 차가 실제로 다시 달린 뒤에야 다음 e-Call 을 받을 준비를 한다.
                speed = state.get("speed_kph")
                if not self._armed and isinstance(speed, (int, float)) and speed >= REARM_SPEED_KPH:
                    self._armed = True
                    print("[ECALL] 차량이 다시 주행 중: 다음 비상 정차 때 e-Call 을 보냄")
                return None
            if phase == STOPPED and previous is None and self._armed:
                # 처음 받은 상태가 이미 Stopped: 이전 실행에서 정차해 있던 Unity 가 다시 붙은 것.
                # 이번 실행의 비상 정차가 아니므로 보내지 않고, 차가 다시 달린 뒤에만 다음 e-Call 을 받는다.
                self._armed = False
                print("[ECALL] 연결 시점에 이미 정차(Stopped) 상태 → 이전 정차로 보고 e-Call 을 보내지 않음. "
                      "Unity 를 새로 시작(Stop → Play 또는 R)하세요.")
                return None
            if phase != STOPPED or not self._armed:
                return None
            if previous != STOPPED:
                self._pending_since, self._still_since = now, None
                print("[ECALL] Unity 정차(Stopped) 감지 → 완전히 멈추면 e-Call 전송")
            if self._pending_since is None:
                return None
            speed = state.get("speed_kph")
            if isinstance(speed, (int, float)) and abs(speed) <= STILL_SPEED_KPH:
                if self._still_since is None:
                    self._still_since = now
            else:
                self._still_since = None
            settled = self._still_since is not None and now - self._still_since >= self.settle_hold_s
            timed_out = now - self._pending_since >= self.settle_timeout_s
            if not (settled or timed_out):
                return None
            settle_ms = round(1000 * (now - self._pending_since))
            self._pending_since = self._still_since = None
            vitals = ({**self._vitals.fields(now), "current_hr": self._vitals.current_hr}
                      if self._model_seen else None)
            trigger = build_trigger(state, self._danger, base_lat=self.base_lat, base_lon=self.base_lon,
                                    vitals=vitals)
            trigger["settle_ms"] = settle_ms
            model_event = trigger["model_event_id"]
            if model_event and model_event in self._sent_model_events:   # 같은 실행에서 같은 모델 사건
                self._armed = False
                return None
            if model_event:
                self._sent_model_events.add(model_event)
            self._armed = False
            self._danger = None             # 다음 비상은 새 근거로
            self._sent[trigger["event_id"]] = now
            self.sent_triggers.append(trigger)
        self._publish(self.trigger_topic, json.dumps(trigger, ensure_ascii=False))
        self._log({"type": "trigger", **trigger})
        u = trigger["unity_position"]
        how = "완전 정차 확인" if settled else f"{self.settle_timeout_s:.0f}초 대기 후"
        print(f"[ECALL] {how}(Stopped 후 {settle_ms} ms) → e-Call 요청 발행 event_id={trigger['event_id']} "
              f"사유={trigger['reason'] or trigger['model_reasons']} 심박={trigger['hr_bpm']} "
              f"맥박={trigger.get('pulse_status', '-')} 마지막심박={trigger.get('hr_last_bpm')}"
              f"({trigger.get('hr_last_age_s')}초 전) "
              f"Unity 위치=(X {u['x']}, Y {u['y']}, Z {u['z']})")
        return trigger

    def _log(self, record: dict) -> None:
        if self.log_path is None:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"logged_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                                **record}, ensure_ascii=False) + "\n")
