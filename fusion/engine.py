"""생체신호·비전·CAN을 합쳐 정상/주의/위험을 판단한다. 판단 이유는 fusion/README.md에 있다.

원칙
- 위험은 Unity에서 취소되지 않는 비상 정차와 e-Call로 이어지므로, 서로 다른 근거 둘 또는 경고 후 무응답이 있어야 한다.
- 응답은 운전자의 실제 조작(CAN 또는 버튼)으로만 인정한다. 눈을 뜨고 있는 것은 응답이 아니다.
- 근거가 사라진 것과 측정이 끊긴 것은 다르다. 쓰러져 얼굴이 가려져도 응답 확인은 계속된다.
- 위험은 reset() 전까지 유지한다.

입력 시각은 모두 이 프로그램이 받은 시각을 쓴다. 라즈베리파이와 Unity의 시계가 서로 다르기 때문이다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .can_features import CanFeatures, CanTracker
from .config import FusionConfig

NORMAL, CAUTION, DANGER, UNAVAILABLE = "normal", "caution", "danger", "unavailable"

# 생체 쪽이 "두 번째 근거가 필요하다"고 넘기는 사유. 다른 센서가 정상이라고 반박한 SENSOR_CONFLICT는 뺀다
BIO_STRONG = {"NO_PULSE_UNCONFIRMED", "HEART_PAUSE", "EXTREME_BRADY_UNCONFIRMED",
              "EXTREME_TACHY_UNCONFIRMED", "MODEL_DANGER_UNCONFIRMED"}
V_DROWSY = {"eyes_closed", "yawning", "perclos_high", "long_blinks_repeated", "nods_repeated"}
V_ATTENTION = {"attention_away", "head_abnormal", "attention_away_repeated", "attention_away_cumulative"}

# 경고 문구 분류 → Unity driver_state. Unity에 건강 경고 코드가 없어 health·system은 임시로 A를 쓴다
_UNITY_CODE = {"emergency": "E", "drowsy": "D", "attention": "A", "health": "A", "system": "A"}


@dataclass
class ResponseCheck:
    started: float
    window: float
    sources: Set[str]
    kind: str


@dataclass
class FusionDecision:
    t: float
    level: str
    driver_state: str = "N"
    alert_category: Optional[str] = None
    reasons: List[str] = field(default_factory=list)
    response_remaining_s: Optional[float] = None  # 응답 확인 중이면 남은 시간
    coverage: Dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict:
        left = self.response_remaining_s
        return {"t": round(self.t, 3), "level": self.level, "driver_state": self.driver_state,
                "alert_category": self.alert_category, "reasons": self.reasons,
                "response_remaining_s": None if left is None else round(left, 2),
                "coverage": self.coverage}


class FusionEngine:
    """사용법: 받을 때마다 push_bio()/push_vision()/push_can(), 약 0.1~1초마다 update(now)."""

    def __init__(self, cfg: Optional[FusionConfig] = None, log_path: Optional[str] = None):
        self.cfg = cfg or FusionConfig()
        self.can = CanTracker(self.cfg)
        self._log = open(log_path, "a", encoding="utf-8") if log_path else None
        self._reset_state()

    def _reset_state(self) -> None:
        self._bio = self._vision = None
        self._bio_t = self._vision_t = None
        self._first_t: Optional[float] = None
        self._check: Optional[ResponseCheck] = None
        self._grace_until = float("-inf")
        self._button_t: Optional[float] = None
        self._latched: Optional[List[str]] = None
        self._level = UNAVAILABLE
        self._last_caution_t: Optional[float] = None
        self._category: Optional[str] = None

    def reset(self) -> None:
        """위험 잠금을 푼다. 재시험이나 사람이 상황을 확인한 뒤에만 부른다."""
        self.can = CanTracker(self.cfg)
        self._reset_state()

    def close(self) -> None:
        if self._log:
            self._log.close()
            self._log = None

    # ---------- 입력 ----------
    def push_bio(self, now: float, status) -> None:
        """status: BioStatus. level, reasons를 쓴다."""
        self._bio, self._bio_t = status, now

    def push_vision(self, now: float, decision) -> None:
        """decision: 비전의 Decision. state, reasons, durations를 쓴다.

        응답 확인은 여기서 하므로 비전 쪽 FeatureFrame에는 warning_active=False를 넣는다.
        """
        self._vision, self._vision_t = decision, now

    def push_can(self, now: float, steering: float, throttle: float, brake: float,
                 driver_input_valid: bool = True, speed_kph: Optional[float] = None) -> None:
        self.can.push(now, steering, throttle, brake, driver_input_valid, speed_kph)

    def push_button(self, now: float) -> None:
        """운전자가 '괜찮음' 버튼을 눌렀다."""
        self._button_t = now

    # ---------- 판단 ----------
    def update(self, now: float) -> FusionDecision:
        if self._first_t is None:
            self._first_t = now
        decision = self._decide(now)
        if self._log:
            self._log.write(json.dumps(decision.to_dict(), ensure_ascii=False) + "\n")
            self._log.flush()
        return decision

    def _decide(self, now: float) -> FusionDecision:
        cfg = self.cfg
        bio = self._fresh(self._bio, self._bio_t, now, cfg.bio_stale_s, "level")
        vision = self._fresh(self._vision, self._vision_t, now, cfg.vision_stale_s, "state")
        can = self.can.features(now)
        coverage = {"bio": bio is not None, "vision": vision is not None, "can": can.valid}

        if self._latched is not None:
            return self._emit(now, DANGER, "emergency", self._latched, coverage)

        bio_level = bio.level if bio else None
        bio_set = set(bio.reasons) if bio else set()
        bio_danger = bio_level == DANGER
        bio_strong = bio_level == CAUTION and bool(bio_set & BIO_STRONG)
        vision_state = _state_name(vision) if vision else None
        v_set = set(vision.reasons) if vision else set()
        v_durations = getattr(vision, "durations", {}) if vision else {}
        vision_ok = vision_state == NORMAL

        eyes_closed = "eyes_closed" in v_set
        collapse = "collapse_posture" in v_set
        away = bool(v_set & {"attention_away", "head_abnormal"})
        asleep = eyes_closed and v_durations.get("eyes_closed", 0.0) >= cfg.sleep_s
        no_input = self._no_input(can, cfg.no_input_support_s)
        control_active = can.valid and not can.stopped and not no_input

        reasons = [f"BIO:{r}" for r in (bio.reasons if bio else [])]
        reasons += [f"VISION:{r}" for r in (vision.reasons if vision else [])]

        # 1) 즉시 위험: 서로 다른 근거가 겹치고, 운전자가 조작하고 있지 않다
        if vision_state == DANGER:
            return self._latch(now, reasons + ["VISION_EXPLICIT_TIMEOUT"], coverage)
        if bio_danger and cfg.bio_danger_immediate:
            return self._latch(now, reasons + ["BIO_DANGER_IMMEDIATE"], coverage)
        if (bio_danger or bio_strong) and (asleep or collapse) and not control_active:
            return self._latch(now, reasons + ["MULTIMODAL_INCAPACITATION"], coverage)

        # 2) 응답 확인을 시작하는 근거
        triggers = self._triggers(bio_danger, bio_strong, bio_level, eyes_closed, collapse, away, no_input,
                                  vision, vision_ok, v_set, can)
        # 3) 응답 확인 진행
        status_note = self._advance_check(now, triggers, bio, vision, bio_danger)
        if status_note == "TIMEOUT":
            return self._latch(now, reasons + [f"NO_RESPONSE:{self._check.kind}"], coverage)
        if status_note:
            reasons.append(status_note)

        # 4) 주의 근거
        caution = self._check is not None or bio_level in (CAUTION, DANGER) or vision_state == CAUTION
        if can.valid and can.abrupt_count >= cfg.abrupt_repeat_count:
            caution = True
            reasons.append("CAN:ERRATIC_CONTROL")
        if self._no_input(can, cfg.no_input_alone_s):
            if vision is None:
                caution = True
            reasons.append("CAN:NO_INPUT")
        if can.hard_brake_recent:
            reasons.append("CAN:HARD_BRAKE")
        if bio is None and vision is None:
            if now - self._first_t >= cfg.startup_s:
                caution = True
                reasons.append("DRIVER_MONITORING_LOST")
            elif not caution:
                return self._emit(now, UNAVAILABLE, None, reasons + ["STARTING"], coverage)
        if self._check is not None:
            reasons.append(f"RESPONSE_CHECK:{self._check.kind}")

        if caution:
            self._last_caution_t = now
            category = self._caution_category(bio_danger, bio_strong, bio_level, v_set, reasons,
                                              bio is None and vision is None)
            self._category = category
            return self._emit(now, CAUTION, category, reasons, coverage)
        if (self._level == CAUTION and self._last_caution_t is not None
                and now - self._last_caution_t < cfg.clear_hold_s):
            return self._emit(now, CAUTION, self._category, reasons + ["HOLD_PREVIOUS_LEVEL"], coverage)
        return self._emit(now, NORMAL, None, reasons, coverage)

    def _triggers(self, bio_danger, bio_strong, bio_level, eyes_closed, collapse, away, no_input,
                  vision, vision_ok, v_set, can: CanFeatures) -> List[Tuple[float, str, str]]:
        """(확인 창, 근거 이름, 출처) 목록."""
        cfg = self.cfg
        fast, single = cfg.window_corroborated_s, cfg.window_single_s
        out: List[Tuple[float, str, str]] = []
        if bio_danger:
            # 응답을 받을 수단이 없고 비전도 정상이면 바로 비상 정차하지 않고 더 지켜본다
            window = cfg.window_bio_unconfirmed_s if vision_ok and not can.valid else fast
            out.append((window, "BIO_DANGER", "bio"))
        if bio_strong:
            out.append((fast if no_input else single, "BIO_UNCONFIRMED", "bio"))
        if eyes_closed:
            out.append((fast if no_input else single, "EYES_CLOSED", "vision"))
        if collapse:
            out.append((fast if no_input else single, "COLLAPSE", "vision"))
        if away:
            out.append((cfg.window_attention_s, "ATTENTION_AWAY", "vision"))
        if "distress_gesture" in v_set and bio_level in (CAUTION, DANGER):
            out.append((fast, "DISTRESS_WITH_BIO", "vision"))
        if vision is None and self._no_input(can, cfg.no_input_alone_s):
            out.append((fast if bio_level in (CAUTION, DANGER) else single, "NO_INPUT_UNMONITORED", "can"))
        return out

    def _advance_check(self, now, triggers, bio, vision, bio_danger) -> Optional[str]:
        cfg = self.cfg
        check = self._check
        if check is None:
            if triggers and now >= self._grace_until:
                window = min(w for w, _, _ in triggers)
                kind = min(triggers, key=lambda x: x[0])[1]
                self._check = ResponseCheck(now, window, {s for _, _, s in triggers}, kind)
            return None

        button = self._button_t is not None and self._button_t >= check.started
        if button or self.can.gestures_since(check.started) >= cfg.response_gestures:
            self._check = None
            self._grace_until = now + (cfg.regrace_bio_s if bio_danger else cfg.regrace_behavior_s)
            return "RESPONSE_CONFIRMED"

        active = {s for _, _, s in triggers}
        check.sources |= active
        if triggers:
            check.window = min(check.window, min(w for w, _, _ in triggers))
        elif self._all_cleared(check.sources, bio, vision):
            self._check = None
            return "RESPONSE_CHECK_CLEARED"
        if now - check.started >= check.window:
            return "TIMEOUT"
        return None

    def _all_cleared(self, sources: Set[str], bio, vision) -> bool:
        """근거를 낸 입력이 정상을 직접 확인했을 때만 해제한다. 측정이 끊긴 것은 해제가 아니다."""
        for source in sources:
            if source == "bio" and bio is None:
                return False
            if source == "vision" and vision is None:
                return False
        return True

    def _caution_category(self, bio_danger, bio_strong, bio_level, v_set, reasons, unmonitored) -> str:
        if bio_danger or bio_strong:
            return "health"
        if v_set == {"recovering"} and self._category:
            return self._category  # 분류가 바뀌면 Unity가 새 경고로 보고 경고창을 다시 띄운다
        if v_set & V_DROWSY or "CAN:ERRATIC_CONTROL" in reasons:
            return "drowsy"
        if v_set & V_ATTENTION:
            return "attention"
        if unmonitored:
            return "system"
        return "health" if bio_level == CAUTION or v_set else (self._category or "attention")

    # ---------- 내부 ----------
    def _fresh(self, value, t, now, stale_s, attr):
        """끊겼거나 스스로 측정 불가라고 한 입력은 없는 것으로 친다."""
        if value is None or t is None or now - t > stale_s:
            return None
        state = getattr(value, attr)
        return None if getattr(state, "value", state) == UNAVAILABLE else value

    def _no_input(self, can: CanFeatures, seconds: float) -> bool:
        return can.valid and not can.stopped and can.no_input_s is not None and can.no_input_s >= seconds

    def _latch(self, now, reasons, coverage) -> FusionDecision:
        self._latched = reasons
        return self._emit(now, DANGER, "emergency", reasons, coverage)

    def _emit(self, now, level, category, reasons, coverage) -> FusionDecision:
        self._level = level
        left = None
        if self._check is not None and level != DANGER:
            left = max(0.0, self._check.started + self._check.window - now)
        code = _UNITY_CODE[category] if level in (CAUTION, DANGER) else "N"
        return FusionDecision(now, level, code, category, list(reasons), left, coverage)


def _state_name(vision) -> str:
    return getattr(vision.state, "value", vision.state)
