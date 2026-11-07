"""ECG·PPG로 심박수(bpm)를 구하고 정상/주의/위험을 판정한다.

결과는 두 가지다.
- hr_class : 심박 등급과 신뢰도. ECG 모델 → PPG 모델 → 심박수 임계값 순으로 판정
- level    : ECG와 PPG 교차 확인까지 마친 최종 생체신호 등급

판정 원칙
- 시작 후 첫 10초의 심박수를 개인 기준선으로 고정하고, 이후 상승·하강을 비교한다.
- 직접 잰 bpm이 범위를 벗어나면 모델이 정상이라고 해도 주의 이상으로 본다.
- 두 센서의 근거가 일치해야 level을 danger로 올린다. 근거가 하나뿐이면 caution과
  needs_second_opinion을 함께 내서 이후 통합 판단에 넘긴다.
- 센서가 떨어진 것은 심정지가 아니다. 측정 불가는 unavailable로 분리한다.
"""
from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional

import numpy as np

from . import dsp
from .channel import ChannelAnalyzer, ChannelReading
from .config import Config

# 비전 쪽 DriverState 값과 같은 이름을 쓴다
NORMAL, CAUTION, DANGER, UNAVAILABLE = "normal", "caution", "danger", "unavailable"
_RANK = {UNAVAILABLE: 0, NORMAL: 1, CAUTION: 2, DANGER: 3}


@dataclass
class BioStatus:
    t: float
    level: str
    reasons: List[str] = field(default_factory=list)
    needs_second_opinion: bool = False
    hr: Optional[float] = None
    hr_baseline: Optional[float] = None
    hr_delta: Optional[float] = None  # 현재 심박수 - 기준선
    hr_class: str = UNAVAILABLE
    hr_class_confidence: Optional[float] = None
    hr_class_source: str = "rule"     # ecg_model, ppg_model 또는 rule
    n_reliable: int = 0
    rmssd_ms: Optional[float] = None
    sdnn_ms: Optional[float] = None
    drowsy_hint: bool = False
    channels: Dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        def r(v, n=1):
            return None if v is None else round(float(v), n)
        return {
            "t": round(self.t, 3),
            "level": self.level,
            "reasons": self.reasons,
            "needs_second_opinion": self.needs_second_opinion,
            "hr": r(self.hr),
            "hr_baseline": r(self.hr_baseline),
            "hr_delta": r(self.hr_delta),
            "hr_class": self.hr_class,
            "hr_class_confidence": r(self.hr_class_confidence, 3),
            "hr_class_source": self.hr_class_source,
            "n_reliable": self.n_reliable,
            "rmssd_ms": r(self.rmssd_ms),
            "sdnn_ms": r(self.sdnn_ms),
            "drowsy_hint": self.drowsy_hint,
            "channels": self.channels,
        }


class BioSignalMonitor:
    """사용법: 샘플마다 push_ecg()/push_ppg() 호출, 약 1초마다 update(now) 호출.

    모든 시각은 같은 시계 기준의 초 단위여야 한다.
    model_path를 주면 ECG 등급을, ppg_encoder_path와 ppg_head_path를 주면 PPG 등급을 학습 모델로
    판정한다. 모델이 없거나 판단을 못 하면 임계값 규칙으로 판정한다.
    """

    def __init__(self, cfg: Optional[Config] = None, log_path: Optional[str] = None,
                 model_path: Optional[str] = None, ppg_encoder_path: Optional[str] = None,
                 ppg_head_path: Optional[str] = None):
        self.cfg = cfg or Config()
        self.ecg = ChannelAnalyzer("ecg", self.cfg)
        self.ppg = ChannelAnalyzer("ppg", self.cfg)
        self._classifier = None
        if model_path:
            from .hr_model import HeartRateClassifier  # torch는 모델을 쓸 때만 불러온다
            self._classifier = HeartRateClassifier(model_path)
        self._ppg_classifier = None
        if ppg_encoder_path and ppg_head_path:
            from .ppg_model import PPGClassifier
            self._ppg_classifier = PPGClassifier(ppg_encoder_path, ppg_head_path)
        self._probs: Deque[np.ndarray] = deque(maxlen=self.cfg.model_smooth_n)
        self._ppg_probs: Deque[np.ndarray] = deque(maxlen=self.cfg.model_smooth_n)
        self._level = UNAVAILABLE
        self._lower_since: Optional[float] = None
        self._since: Dict[str, float] = {}
        self._last_t: Optional[float] = None
        self._first_t: Optional[float] = None
        self._baseline: Optional[float] = None
        self._calibration: Deque[tuple] = deque()
        self._rmssd_baseline: Optional[float] = None
        self._log = open(log_path, "a", encoding="utf-8") if log_path else None

    # ---------- 입력 ----------
    def push_ecg(self, t: float, value: float, leads_off: Optional[bool] = None) -> None:
        if self._first_t is None:
            self._first_t = t
        self.ecg.push(t, value, None if leads_off is None else not leads_off)

    def push_ppg(self, t: float, value: float, contact: Optional[bool] = None) -> None:
        if self._first_t is None:
            self._first_t = t
        self.ppg.push(t, value, contact)

    def close(self) -> None:
        if self._log:
            self._log.close()
            self._log = None

    # ---------- 판정 ----------
    def update(self, now: float) -> BioStatus:
        cfg = self.cfg
        dt = 0.0 if self._last_t is None else max(0.0, now - self._last_t)
        self._last_t = now

        ecg = self.ecg.analyze(now)
        ppg = self.ppg.analyze(now)
        model_label, model_conf = self._apply_model(ecg, now)
        ppg_label, ppg_conf = self._apply_ppg_model(ppg, now)
        readings = [ecg, ppg]
        reliable = [c for c in readings if c.reliable]
        hr = self._fuse_hr(reliable)
        rmssd, sdnn = self._hrv(now)

        reasons: List[str] = []
        second_opinion = False
        cand = NORMAL

        def raise_to(level: str, reason: str, ask: bool = False) -> None:
            nonlocal cand, second_opinion
            if _RANK[level] > _RANK[cand]:
                cand = level
            reasons.append(reason)
            second_opinion = second_opinion or ask

        # 1) 맥박 소실
        ecg_flat = self._absent(ecg, require_known_contact=False)
        ppg_flat = self._absent(ppg, require_known_contact=True)
        ecg_chaotic = (ecg.contact is not False and ecg.ready and not ecg.reliable
                       and not ecg.quiet and ecg.no_rhythm_s is not None
                       and ecg.no_rhythm_s >= cfg.no_pulse_s)
        if ecg_flat and ppg_flat:
            raise_to(DANGER, "NO_PULSE_CONFIRMED")
        elif ecg_chaotic and ppg_flat:
            raise_to(DANGER, "CHAOTIC_ECG_NO_PULSE")
        elif ppg_flat and ecg.reliable and model_label in (CAUTION, DANGER):
            # 맥박이 사라졌는데 모델도 ECG를 비정상으로 본다: VF를 caution으로 봐도 놓치지 않는다
            raise_to(DANGER, "ABNORMAL_ECG_NO_PULSE")
        elif ppg_flat or (ecg_flat and not reliable):
            # ECG 파형이 정상이어도 맥박이 없을 수 있다(무맥성 전기활동). 손가락이 떨어졌을 수도 있어 주의로 둔다
            raise_to(CAUTION, "NO_PULSE_UNCONFIRMED", ask=True)
        elif self._paused(readings, reliable):
            # 쓰러지는 순간을 놓치지 않도록, 위험 확정 전이라도 박동이 끊기면 바로 알린다
            raise_to(CAUTION, "HEART_PAUSE", ask=True)

        # 2) 위험 등급 심박: 모델이 위험으로 보거나 직접 잰 bpm이 위험 범위면 위험이다.
        #    깨끗하게 잰 값을 모델이 뒤집으면 안 된다
        use_model = model_label is not None
        use_ppg_model = ppg_label is not None
        ecg_danger = ecg.reliable and (model_label == DANGER or self._is_extreme(ecg.hr))
        ppg_danger = ppg.reliable and (ppg_label == DANGER or self._is_extreme(ppg.hr))
        votes = int(ecg_danger) + int(ppg_danger)
        contradicted = (ecg.reliable and not ecg_danger) or (ppg.reliable and not ppg_danger)
        if self._held("danger_hr", votes > 0, now, cfg.extreme_hold_s):
            tag = self._danger_tag(ecg, ppg, DANGER in (model_label, ppg_label))
            if votes == 2 or (ecg_danger and ppg_flat):
                raise_to(DANGER, tag)
            elif contradicted:
                raise_to(CAUTION, "SENSOR_CONFLICT", ask=True)
            else:
                raise_to(CAUTION, tag + "_UNCONFIRMED", ask=True)

        # 3) 주의 등급 심박: 모델 판정과 직접 잰 bpm을 각각 본다
        cv = self._rr_cv(now)
        model_caution = ((use_model and model_label == CAUTION and ecg.reliable)
                         or (not use_model and use_ppg_model and ppg_label == CAUTION and ppg.reliable))
        # 모델은 50 bpm 이하를 주의로 학습했다. 평소 심박이 낮은 사람의 규칙적인 평소 심박은 이상이 아니다
        if model_caution and self._usual_low_heart_rate(hr, cv):
            model_caution = False
        hr_low = hr is not None and not self._is_extreme(hr) and hr < self._low_limit()
        hr_high = hr is not None and not self._is_extreme(hr) and hr > cfg.hr_caution_high
        caution_now = model_caution or hr_low or hr_high
        if self._held("hr_low", hr_low, now, cfg.caution_hold_s):
            raise_to(CAUTION, "HR_LOW")
        if self._held("hr_high", hr_high, now, cfg.caution_hold_s):
            raise_to(CAUTION, "HR_HIGH")
        if self._held("model_caution", model_caution, now, cfg.caution_hold_s):
            raise_to(CAUTION, "MODEL_CAUTION")

        # 4) 개인 기준선 대비 상승·하강
        hr_delta = None if hr is None or self._baseline is None else hr - self._baseline
        deviated = False
        if hr_delta is not None:
            limit = max(cfg.abrupt_delta_bpm, cfg.abrupt_delta_ratio * self._baseline)
            deviated = abs(hr_delta) >= limit
        if self._held("abrupt", deviated, now, cfg.abrupt_hold_s):
            raise_to(CAUTION, "HR_RISING" if hr_delta > 0 else "HR_FALLING")

        # 5) 불규칙 리듬
        irregular = cv is not None and cv > cfg.irregular_rr_cv and ecg.reliable
        if self._held("irregular", irregular, now, cfg.irregular_hold_s):
            raise_to(CAUTION, "IRREGULAR_RHYTHM")

        # 6) 졸음 참고 지표: 약한 근거이므로 등급은 올리지 않는다
        drowsy_now = (hr is not None and self._baseline is not None and not deviated
                      and self._rmssd_baseline is not None and rmssd is not None
                      and hr <= self._baseline * (1 - cfg.drowsy_hr_drop_ratio)
                      and rmssd >= self._rmssd_baseline * (1 + cfg.drowsy_rmssd_rise_ratio))
        drowsy = self._held("drowsy", drowsy_now, now, cfg.drowsy_hold_s)
        if drowsy:
            reasons.append("DROWSY_HINT")

        self._calibrate(hr, now, abnormal=votes > 0)
        self._update_rmssd_baseline(rmssd, dt, frozen=deviated or caution_now or votes > 0 or drowsy_now)

        # 7) 측정 불가 처리. 기준선을 재는 동안에는 정상이라고 표시하지 않는다
        calibrating = (self._baseline is None and self._first_t is not None
                       and now - self._first_t < cfg.calibration_s)
        if cand == NORMAL and calibrating and not all(c.stale for c in readings):
            cand = UNAVAILABLE
            reasons.append("CALIBRATING")
        elif cand == NORMAL and not reliable:
            cand = UNAVAILABLE
            second_opinion = True
            if any(c.ready and c.contact is not False for c in readings):
                reasons.append("LOW_SIGNAL_QUALITY")
            else:
                reasons.append("SENSOR_LOST")

        level = self._apply_hysteresis(cand, now)
        if level != cand:
            reasons.append("HOLD_PREVIOUS_LEVEL")
            if level == CAUTION:
                second_opinion = True

        # ECG 모델 → PPG 모델 → 측정한 심박수의 임계값 순으로 등급을 낸다
        if use_model:
            hr_class, hr_conf, hr_source = model_label, model_conf, "ecg_model"
        elif use_ppg_model:
            hr_class, hr_conf, hr_source = ppg_label, ppg_conf, "ppg_model"
        else:
            hr_class, hr_conf, hr_source = self._rule_class(hr), None, "rule"
        if self._is_extreme(hr) and hr_class != DANGER:
            hr_class, hr_conf, hr_source = DANGER, None, "rule"
        status = BioStatus(
            t=now, level=level, reasons=reasons, needs_second_opinion=second_opinion,
            hr=hr, hr_baseline=self._baseline, hr_delta=hr_delta,
            hr_class=hr_class, hr_class_confidence=hr_conf, hr_class_source=hr_source,
            n_reliable=len(reliable), rmssd_ms=rmssd, sdnn_ms=sdnn, drowsy_hint=drowsy,
            channels={c.name: c.to_dict() for c in readings},
        )
        if self._log:
            self._log.write(json.dumps(status.to_dict(), ensure_ascii=False) + "\n")
            self._log.flush()
        return status

    # ---------- 내부 ----------
    def _apply_model(self, ecg: ChannelReading, now: float):
        """모델을 돌려 ECG 등급을 얻는다. 판정할 수 없으면 (None, None)을 돌려 규칙에 맡긴다."""
        if self._classifier is None:
            return None, None
        raw = None if (ecg.stale or ecg.contact is False) else self.ecg.last_window(now, dsp.MODEL_WINDOW_S)
        if raw is None:
            self._probs.clear()
            ecg.reliable = False
            return None, None

        out = self._classifier.predict(raw, self.cfg.ecg_fs, self.cfg.mains_hz)
        names = list(out.probs)
        self._probs.append(np.array([out.probs[k] for k in names]))
        mean = np.mean(self._probs, axis=0)
        i = int(np.argmax(mean))
        label, conf = names[i], float(mean[i])

        # 박동이 안 잡히고 최근 신호가 평탄하면 모델 판정과 무관하게 신뢰하지 않는다
        flat = ecg.quiet and ecg.hr is None
        if label == UNAVAILABLE or conf < self.cfg.model_min_confidence or flat:
            return None, None

        # bpm은 피크 간격으로 직접 잰 값만 쓴다. 파형 품질이 낮으면 bpm 없이 등급만 낸다
        if ecg.sqi < self.cfg.sqi_reliable:
            ecg.hr = None
        ecg.reliable = True
        ecg.no_rhythm_s = 0.0
        self.ecg.mark_reliable(now)
        return label, conf

    def _apply_ppg_model(self, ppg: ChannelReading, now: float):
        """PPG 등급을 얻는다. 모델이 측정 불가로 보면 PPG bpm도 믿지 않는다."""
        if self._ppg_classifier is None:
            return None, None
        raw = None if (ppg.stale or ppg.contact is False) else self.ppg.last_window(now, dsp.MODEL_WINDOW_S)
        if raw is None:
            self._ppg_probs.clear()
            return None, None
        out = self._ppg_classifier.predict(raw, self.cfg.ppg_fs)
        names = list(out.probs)
        self._ppg_probs.append(np.array([out.probs[k] for k in names]))
        mean = np.mean(self._ppg_probs, axis=0)
        i = int(np.argmax(mean))
        label, conf = names[i], float(mean[i])
        if conf < self.cfg.model_min_confidence:
            return None, None
        if label == UNAVAILABLE:
            ppg.reliable = False
            ppg.hr = None
            return None, None
        return (label, conf) if ppg.reliable else (None, None)

    def _danger_tag(self, ecg, ppg, from_model: bool) -> str:
        """직접 잰 bpm이 위험 범위면 그 이유를 우선한다. 사용자가 이해하기 쉽기 때문이다."""
        rates = [c.hr for c in (ecg, ppg) if c.reliable and self._is_extreme(c.hr)]
        if rates:
            return "EXTREME_BRADY" if min(rates) <= self.cfg.hr_extreme_low else "EXTREME_TACHY"
        return "MODEL_DANGER" if from_model else "EXTREME_TACHY"

    def _low_limit(self) -> float:
        """저심박 기준. 평소 심박이 낮은 사람이 계속 경고받지 않도록 기준선에 맞춰 낮춘다."""
        cfg = self.cfg
        if self._baseline is None:
            return cfg.hr_caution_low
        return max(cfg.hr_extreme_low, min(cfg.hr_caution_low, self._baseline - cfg.personal_low_margin))

    def _usual_low_heart_rate(self, hr: Optional[float], rr_cv: Optional[float]) -> bool:
        cfg = self.cfg
        return (hr is not None and self._baseline is not None and self._baseline < cfg.hr_caution_low
                and abs(hr - self._baseline) < cfg.personal_low_margin
                and (rr_cv is None or rr_cv <= cfg.irregular_rr_cv))

    def _rule_class(self, hr: Optional[float]) -> str:
        if hr is None:
            return UNAVAILABLE
        if self._is_extreme(hr):
            return DANGER
        if hr < self._low_limit() or hr > self.cfg.hr_caution_high:
            return CAUTION
        return NORMAL

    def _paused(self, readings: List[ChannelReading], reliable: List[ChannelReading]) -> bool:
        """접촉이 유지된 센서가 모두 박동을 잃었고, 그중 하나 이상은 신호가 잠잠해졌는가.

        잡음이 큰 동안 박동을 못 잡는 것은 정지가 아니다. 실측 기록에서 이 조건이 없으면 오경보가 났다.
        박동을 잡은 적 없는 센서는 판단에서 뺀다.
        """
        watched = [c for c in readings if not c.stale and c.contact is not False and c.no_rhythm_s is not None]
        return (not reliable and any(c.quiet for c in watched)
                and all(c.no_rhythm_s >= self.cfg.pause_s for c in watched))

    def _absent(self, c: ChannelReading, require_known_contact: bool) -> bool:
        """접촉이 유지된 채로, 이전엔 잡히던 박동이 사라지고 신호가 평탄해졌는가."""
        if c.stale or not c.ready or c.contact is False:
            return False
        if require_known_contact and c.contact is not True:
            return False
        return (not c.reliable and c.quiet and c.no_rhythm_s is not None
                and c.no_rhythm_s >= self.cfg.no_pulse_s)

    def _is_extreme(self, hr: Optional[float]) -> bool:
        return hr is not None and (hr <= self.cfg.hr_extreme_low or hr >= self.cfg.hr_extreme_high)

    def _held(self, key: str, active: bool, now: float, hold_s: float) -> bool:
        if not active:
            self._since.pop(key, None)
            return False
        start = self._since.setdefault(key, now)
        return now - start >= hold_s

    def _fuse_hr(self, reliable: List[ChannelReading]) -> Optional[float]:
        """ECG를 우선하고, ECG를 쓸 수 없을 때만 PPG 심박수를 쓴다."""
        for c in reliable:
            if c.name == "ecg" and c.hr is not None:
                return c.hr
        for c in reliable:
            if c.hr is not None:
                return c.hr
        return None

    def _beats_for_hrv(self, now: float) -> np.ndarray:
        since = now - self.cfg.hrv_window_s
        beats = self.ecg.beat_times(since)
        return beats if len(beats) > self.cfg.hrv_min_rr else self.ppg.beat_times(since)

    def _hrv(self, now: float):
        rr = dsp.rr_intervals(self._beats_for_hrv(now))
        if len(rr) < self.cfg.hrv_min_rr:
            return None, None
        # 기외수축·오검출로 생긴 급격한 RR 변화는 제외
        keep = np.abs(np.diff(rr)) <= 0.2 * rr[:-1]
        clean = rr[1:][keep]
        if len(clean) < self.cfg.hrv_min_rr // 2:
            return None, None
        return (float(np.sqrt(np.mean(np.diff(clean) ** 2)) * 1000.0),
                float(np.std(clean) * 1000.0))

    def _rr_cv(self, now: float) -> Optional[float]:
        rr = dsp.rr_intervals(self.ecg.beat_times(now - 30.0))
        if len(rr) < 15:
            return None
        return float(np.std(rr) / np.mean(rr))

    def _calibrate(self, hr: Optional[float], now: float, abnormal: bool) -> None:
        """최근 10초의 신뢰 측정값 중앙값을 기준선으로 한 번만 정한다.

        평소 값으로 볼 수 없는 심박수나 위험 소견이 있는 동안에는 기록을 비우고 다시 잰다.
        시작 직후 흥분 상태였다가 안정되면 그때 기준선이 잡힌다.
        """
        cfg = self.cfg
        if self._baseline is not None or self._first_t is None:
            return
        if hr is None or abnormal or not cfg.baseline_min_hr < hr <= cfg.baseline_max_hr:
            self._calibration.clear()
            return
        self._calibration.append((now, hr))
        while self._calibration[0][0] < now - cfg.calibration_s:
            self._calibration.popleft()
        if (now - self._first_t >= cfg.calibration_s
                and len(self._calibration) >= cfg.calibration_min_readings):
            self._baseline = float(np.median([v for _, v in self._calibration]))

    def _update_rmssd_baseline(self, rmssd, dt, frozen) -> None:
        if rmssd is None or frozen or self._baseline is None:
            return
        alpha = min(1.0, dt / self.cfg.baseline_tau_s)
        self._rmssd_baseline = (rmssd if self._rmssd_baseline is None
                                else self._rmssd_baseline + alpha * (rmssd - self._rmssd_baseline))

    def _apply_hysteresis(self, cand: str, now: float) -> str:
        """상승은 즉시, 하강은 clear_hold_s 유지 후 반영.

        danger는 신뢰할 수 있는 정상 신호가 확인되어야만 해제한다. 측정 불가로는
        내려가지 않는다. 쓰러지며 센서가 떨어져도 경보가 풀리면 안 되기 때문이다.
        반대로 normal은 측정이 안 되는 순간 바로 내린다. 신호 없이 정상이라고 표시하면 안 된다.
        """
        if _RANK[cand] >= _RANK[self._level] or (cand == UNAVAILABLE and self._level == NORMAL):
            self._level = cand
            self._lower_since = None
        elif cand == UNAVAILABLE and self._level == DANGER:
            self._lower_since = None
        else:
            if self._lower_since is None:
                self._lower_since = now
            if now - self._lower_since >= self.cfg.clear_hold_s:
                self._level = cand
                self._lower_since = None
        return self._level
