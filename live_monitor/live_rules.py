"""실시간 전용 판정 규칙 (원본 모델 파일은 수정하지 않는다).

경고 후 무응답 정차: 원본 prototype 판정은 눈 감김만으로는 정차하지 않는다
("Eye stop remains disabled until separately validated"). 심박이 정상인데 의식을 잃는 경우(기절 등)에
대비해, 눈 감김 경고가 실제로 나간 뒤에도 운전자가 눈을 뜨지 않으면 Danger(정차)로 올린다.

  1) 원본 판정이 눈 감김 주의(reasons 에 eyes_closed)를 낸다 → 차량 서버를 거쳐 Unity 가 경고 화면·경고음을 낸다.
     경고는 차량 서버가 연결돼 있을 때만 실제로 전달되므로, 그때부터 센다(warning_delivered).
  2) 그 뒤 response_seconds 동안 눈이 계속 '감김'으로 관측되면 → Danger, 사유 eye_closure_no_response.
  3) 그 사이 눈이 '뜸'으로 관측되면 타이머 초기화. 얼굴이 잠깐 안 보이는 것은 뜸으로 보지 않고 타이머를 유지만 한다
     (얼굴이 안 보이는 쓰러짐은 원본의 쓰러진 자세 규칙이 맡는다).

연구용 시연 설정이며 검증된 임상 기준이 아니다.
"""
from __future__ import annotations

from typing import Callable

from driver_state_tools.decision_engine import DriverState
from driver_state_tools.multimodal_runtime import CurrentVisionSource

NO_RESPONSE_REASON = 'eye_closure_no_response'


class EyeNoResponseRule:
    def __init__(self, response_seconds: float, warning_delivered: Callable[[], bool] = lambda: True,
                 open_confirm_seconds: float = 1.0):
        self.response_seconds = float(response_seconds or 0.0)
        self.warning_delivered = warning_delivered
        # 눈을 '뜸'으로 이만큼 이어서 봐야 반응으로 인정한다. 한두 프레임 뜸(깜빡 인식 흔들림)으로
        # 타이머가 초기화되지 않게 한다 (2026-11-15 실시간 시험: 한 프레임 뜸 → 3초가 10초로 늘어남).
        self.open_confirm_seconds = float(open_confirm_seconds)
        self.warned_at: float | None = None       # 눈 감김 경고가 전달되기 시작한 시각 (판단 시각)
        self.open_since: float | None = None

    def apply(self, decision, timestamp: float):
        if self.response_seconds <= 0:
            return decision
        frame = decision.metadata.get('prototype_frame') or {}
        observed = bool(frame.get('calibrated') and frame.get('face_readable'))
        eyes_closed = frame.get('eyes_closed') if observed else None
        if eyes_closed is False:                   # 눈을 뜸 → 1초 이상 이어지면 반응한 것
            if self.open_since is None:
                self.open_since = timestamp
            if timestamp - self.open_since + 1e-9 >= self.open_confirm_seconds:
                self.warned_at = None
            return decision
        if eyes_closed is True:
            self.open_since = None
        warning_seen = 'eyes_closed' in decision.reasons or (
            self.warned_at is not None and decision.state is DriverState.CAUTION)
        if eyes_closed is True and warning_seen and self.warned_at is not None:
            # 원본이 회복 확인(recovery_pending) 등으로 사유를 바꿔도, 경고 후 계속 감고 있으면 센다
            waited = timestamp - self.warned_at
            if waited + 1e-9 >= self.response_seconds and decision.state is DriverState.CAUTION:
                return self._raise(decision, waited)
            return decision
        warning = decision.state in (DriverState.CAUTION, DriverState.DANGER) and 'eyes_closed' in decision.reasons
        if eyes_closed is not True or not warning:
            return decision                        # 아직 경고 전이거나 얼굴이 안 보임: 타이머 유지
        if self.warned_at is None and self.warning_delivered():
            self.warned_at = timestamp             # 눈 감김 경고가 Unity 로 전달되기 시작
        return decision

    def _raise(self, decision, waited: float):
        decision.state = DriverState.DANGER
        if NO_RESPONSE_REASON not in decision.reasons:
            decision.reasons.append(NO_RESPONSE_REASON)
        decision.metadata['stop_basis'] = 'live_no_response_after_eye_closure_warning'
        decision.metadata['live_rule'] = dict(name='eye_closure_no_response',
                                              response_seconds=self.response_seconds,
                                              seconds_since_warning=round(waited, 2))
        return decision


class LiveVisionSource(CurrentVisionSource):
    """원본 CurrentVisionSource + 실시간 전용 규칙."""

    def __init__(self, *args, no_response_rule: EyeNoResponseRule | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.no_response_rule = no_response_rule

    def update(self, frame, timestamp):
        observation = super().update(frame, timestamp)
        if self.no_response_rule is not None:
            self.no_response_rule.apply(observation.decision, timestamp)
        return observation
