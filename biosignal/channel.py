"""센서 채널(ECG 또는 PPG) 하나의 실시간 분석: 버퍼링 → 재샘플링 → 박동 검출 → 품질 평가."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional

import numpy as np

from . import dsp
from .config import Config


@dataclass
class ChannelReading:
    name: str
    ready: bool = False
    stale: bool = False               # 샘플이 끊김
    contact: Optional[bool] = None    # True 접촉 / False 탈락 / None 판별 불가
    hr: Optional[float] = None
    sqi: float = 0.0
    reliable: bool = False
    quiet: bool = False               # 최근 신호가 평소보다 훨씬 작음(평탄)
    no_rhythm_s: Optional[float] = None  # 마지막 신뢰 박동 이후 경과. 한 번도 없었으면 None

    def to_dict(self) -> dict:
        return {
            "ready": self.ready,
            "stale": self.stale,
            "contact": self.contact,
            "hr": None if self.hr is None else round(self.hr, 1),
            "sqi": round(self.sqi, 2),
            "reliable": self.reliable,
            "quiet": self.quiet,
            "no_rhythm_s": None if self.no_rhythm_s is None else round(self.no_rhythm_s, 1),
        }


class ChannelAnalyzer:
    def __init__(self, kind: str, cfg: Config):
        if kind not in ("ecg", "ppg"):
            raise ValueError(f"알 수 없는 채널: {kind}")
        self.kind = kind
        self.cfg = cfg
        self.fs = cfg.ecg_fs if kind == "ecg" else cfg.ppg_fs
        self.window_s = cfg.window_s
        self._t: Deque[float] = deque()
        self._v: Deque[float] = deque()
        self._contact: Optional[bool] = None
        self._ref_amp: Optional[float] = None
        self._beats: List[float] = []
        self._last_reliable_t: Optional[float] = None
        self._latest_clean: Optional[np.ndarray] = None

    def push(self, t: float, value: float, contact: Optional[bool] = None) -> None:
        self._t.append(float(t))
        self._v.append(float(value))
        self._contact = contact

    def beat_times(self, since: float) -> np.ndarray:
        return np.array([b for b in self._beats if b >= since])

    def latest_clean(self) -> Optional[np.ndarray]:
        """마지막 분석 창의 필터링 파형(화면 표시용)."""
        return self._latest_clean

    def mark_reliable(self, now: float) -> None:
        """학습 모델이 이 채널의 리듬을 확인했음을 기록한다."""
        self._last_reliable_t = now

    def last_window(self, now: float, seconds: float) -> Optional[np.ndarray]:
        """최근 구간의 원신호를 일정한 샘플링 주기로 돌려준다. 길이가 모자라면 None."""
        if not self._t or self._t[-1] - self._t[0] < seconds:
            return None
        ts = np.fromiter(self._t, dtype=float)
        raw = np.fromiter(self._v, dtype=float)
        grid = ts[-1] - seconds + np.arange(int(seconds * self.fs)) / self.fs
        return np.interp(grid, ts, raw)

    def _trim(self, now: float) -> None:
        horizon = now - self.window_s
        while self._t and self._t[0] < horizon:
            self._t.popleft()
            self._v.popleft()

    def _infer_contact(self, raw: np.ndarray) -> Optional[bool]:
        """센서가 준 플래그를 우선하고, 없으면 원신호로 가능한 만큼만 추정한다."""
        if self._contact is not None:
            return self._contact
        cfg = self.cfg
        low, high = ((cfg.ecg_rail_low, cfg.ecg_rail_high) if self.kind == "ecg"
                     else (cfg.ppg_rail_low, cfg.ppg_rail_high))
        if np.mean((raw <= low) | (raw >= high)) > cfg.rail_fraction:
            return False
        if self.kind == "ppg" and cfg.ppg_contact_dc_min is not None:
            return bool(np.median(raw) >= cfg.ppg_contact_dc_min)
        # 맥박을 잡은 적이 있고 포화되지 않았으면 접촉이 유지된 것으로 본다.
        # 실측에서 손가락을 떼면 PPG가 약 966에 포화됐다. 접촉 플래그가 없는 현재 장비에서
        # 맥박 소실을 판단할 유일한 근거이고, 위험 판정에는 ECG 이상이 함께 있어야 한다
        if self.kind == "ppg" and self._last_reliable_t is not None:
            return True
        return None

    def analyze(self, now: float) -> ChannelReading:
        self._trim(now)
        r = ChannelReading(name=self.kind)
        if self._last_reliable_t is not None:
            r.no_rhythm_s = now - self._last_reliable_t

        if not self._t or now - self._t[-1] > self.cfg.stale_s:
            r.stale = True
            r.contact = False
            return r
        if self._t[-1] - self._t[0] < self.cfg.min_ready_s:
            return r

        r.ready = True
        ts = np.fromiter(self._t, dtype=float)
        raw = np.fromiter(self._v, dtype=float)
        r.contact = self._infer_contact(raw)
        grid = np.arange(ts[0], ts[-1], 1.0 / self.fs)
        x = np.interp(grid, ts, raw)
        self._analyze_beats(r, x, grid, now)

        if self._last_reliable_t is not None:
            r.no_rhythm_s = now - self._last_reliable_t
        return r

    def _analyze_beats(self, r: ChannelReading, x: np.ndarray, grid: np.ndarray,
                       now: float) -> None:
        cfg = self.cfg
        if self.kind == "ecg":
            clean = dsp.clean_ecg(x, self.fs, cfg.mains_hz)
            peaks, amps, band = dsp.detect_r_peaks(x, self.fs)
        else:
            clean = dsp.clean_pulse(x, self.fs)
            peaks, amps, band = dsp.detect_pulse_peaks(x, self.fs)
        self._latest_clean = clean

        # 박동이 멈추면 창 안의 잡음이 상대적으로 커 보이므로, 평소 진폭 기준으로 걸러낸다
        if self._ref_amp is not None and len(peaks):
            keep = amps >= cfg.amp_floor_ratio * self._ref_amp
            peaks, amps = peaks[keep], amps[keep]

        recent = band[grid >= grid[-1] - 4.0]
        if self._ref_amp is not None and len(recent):
            spread = np.percentile(recent, 99) - np.percentile(recent, 1)
            r.quiet = bool(spread < cfg.quiet_ratio * self._ref_amp)

        if r.contact is False:
            return

        r.sqi = dsp.beat_sqi(clean, peaks, self.fs, self.kind)
        if len(peaks) < 3:
            return

        times = grid[peaks]
        rr = dsp.rr_intervals(times)
        if len(rr) < 2:
            return
        med_rr = float(np.median(rr))
        # 박동이 끊겼는데 과거 박동으로 심박수를 계속 보고하지 않도록 한다
        if now - times[-1] > max(3.0 * med_rr, 3.5):
            return

        r.hr = 60.0 / med_rr
        r.reliable = r.sqi >= cfg.sqi_reliable
        if r.reliable:
            self._last_reliable_t = float(times[-1])
            self._commit_beats(times, med_rr, now)
            if r.sqi >= 0.7:
                level = float(np.median(amps))
                self._ref_amp = level if self._ref_amp is None else 0.8 * self._ref_amp + 0.2 * level

    def _commit_beats(self, times: np.ndarray, med_rr: float, now: float) -> None:
        """필터 경계 영향이 없는 박동만 장기 목록(HRV용)에 추가한다.

        평소 RR의 절반보다 짧은 간격은 잡음 피크로 보고 넣지 않는다. 한 번 들어간 잡음이
        30초 동안 불규칙 리듬 지표를 흔들기 때문이다.
        """
        min_gap = max(0.2, 0.5 * med_rr)
        for b in times:
            if b > now - 0.3:
                break
            if not self._beats or b - self._beats[-1] > min_gap:
                self._beats.append(float(b))
        horizon = now - self.cfg.hrv_window_s - 5.0
        while self._beats and self._beats[0] < horizon:
            self._beats.pop(0)
