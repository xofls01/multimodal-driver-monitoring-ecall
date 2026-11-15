"""주행 중에만 운전자 감시 결과를 Unity 로 보낸다 (PC, 차량 서버 안에서 동작).

정차 중(신호 대기·주차, 시동만 켜짐)에는 딴 데 보기·눈 감기·고개 숙이기가 자연스럽다. 이때 경고음이나
비상 정차·e-Call 이 나오면 곤란하므로, 모델 판단(Normal/Caution/Danger)을 Unity 로 넘기기 전에 거른다.

  주행 감지   : Unity 속도가 on_speed_kph 이상으로 on_hold_s 이어지면 감시 켬
  정차 감지   : off_speed_kph 이하로 off_hold_s 이어지면 감시 끔 (처음 시작도 '정차')
  정차 중     : Caution·Danger·판단 불가 → Normal (경고음·정차 없음)
                예외: 생체 위험이 비전으로 교차 확인된 Danger(stop_basis =
                'biosignal_alert_and_observed_posture_or_eye_closure': 맥박 소실·심박 위험 +
                쓰러진 자세/눈 감김 3초)는 그대로 보낸다 → 이미 서 있으므로 제자리 정차 후 e-Call.
  비상 정차 중: Unity 가 비상 단계(SignalAndSlow… Stopped)에 있으면 거르지 않는다 (감속 중에도 유지).

센서가 떨어지거나 핸들을 놓아 맥박이 안 잡히는 것은 원본 생체 모델에서 '판단 불가'(SENSOR_LOST 등)이고,
생체 위험(맥박 소실)은 센서가 붙은 채 ECG·PPG 박동이 모두 8초 사라져야 하므로 이 예외에 걸리지 않는다.
"""
from __future__ import annotations

BIO_CORROBORATED = "biosignal_alert_and_observed_posture_or_eye_closure"
IDLE_PHASES = {None, "", "Idle", "Unavailable"}


class DrivingGate:
    def __init__(self, *, on_speed_kph: float = 10.0, on_hold_s: float = 2.0,
                 off_speed_kph: float = 3.0, off_hold_s: float = 3.0, enabled: bool = True,
                 clock=None, log=print):
        import time
        self.on_speed, self.on_hold = on_speed_kph, on_hold_s
        self.off_speed, self.off_hold = off_speed_kph, off_hold_s
        self.enabled = enabled
        self._clock = clock or time.monotonic
        self._log = log
        self.driving = False
        self._since = None              # 반대 상태 조건이 시작된 시각
        self.emergency = False
        self.suppressed = 0             # 정차 중이라 거른 판단 수

    def on_vehicle_state(self, state: dict) -> None:
        if not self.enabled or not isinstance(state, dict):
            return
        self.emergency = state.get("emergency_phase") not in IDLE_PHASES
        speed = state.get("speed_kph")
        if isinstance(speed, bool) or not isinstance(speed, (int, float)):
            return
        now = self._clock()
        crossing = speed >= self.on_speed if not self.driving else speed <= self.off_speed
        if not crossing:
            self._since = None
            return
        if self._since is None:
            self._since = now
        if now - self._since + 1e-9 >= (self.off_hold if self.driving else self.on_hold):
            self.driving = not self.driving
            self._since = None
            self._log("[GATE] 주행 감지: 운전자 감시 켬" if self.driving else
                      "[GATE] 정차 감지: 비전 경고·정차 끔 (생체 위험이 확인된 Danger 만 정차·e-Call)")

    def filter(self, label: str | None, packet: dict | None) -> str | None:
        """모델 최종 판단(label)을 Unity 로 보낼 값으로 바꾼다."""
        if not self.enabled or self.driving or self.emergency:
            return label
        if label == "Danger" and isinstance(packet, dict) and packet.get("stop_basis") == BIO_CORROBORATED:
            return label
        if label != "Normal":
            self.suppressed += 1
        return "Normal"
