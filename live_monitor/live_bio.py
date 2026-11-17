"""실시간 ECG/PPG 입력. 팀원의 BiosignalReplay(파일 재생)와 같은 규칙으로 동작한다.

- 받은 샘플만 쓴다 (미래 샘플 없음). advance(t) 때 t 까지의 샘플을 원본 BioSignalMonitor 에 넣는다.
- 판정은 1초마다 monitor.update() 한 번 (BiosignalReplay 와 같은 1 Hz).
- 결과·샘플이 stale_seconds 보다 오래되면 'unavailable / SIGNAL_STALE' (센서가 빠졌거나 연결이 끊긴 경우).
- 화면용 BPM(display_pulse)은 원본 RecentPulseRates 를 그대로 쓴다. 기준 진폭은 처음 3.5초(calm),
  계산은 최근 window_seconds 구간만 넣어서 실시간으로 계산량이 늘지 않게 한다.
등급·사유·심박 판단은 모두 원본 BioSignalMonitor 가 한다. 여기서 바꾸지 않는다.
"""
from __future__ import annotations

import math
import threading
from collections import deque

import numpy as np

CALM_SECONDS = 3.5            # RecentPulseRates 의 기준 구간 (원본 코드의 t < 3.5)


class LiveBiosignalSource:
    def __init__(self, monitor, *, startup_settings: dict, update_seconds: float = 1.0,
                 stale_seconds: float = 1.5, window_seconds: float = 12.0, display_every_seconds: float = 0.25,
                 pulse_rates_factory=None):
        self.monitor = monitor
        self.interval, self.stale = update_seconds, stale_seconds
        self.window, self.display_every = window_seconds, display_every_seconds
        self.startup_settings = dict(startup_settings)
        self.synthetic = False
        self.recorded_vitals = None
        self.history: list[dict] = []
        self.latest = None
        self._pending: deque = deque()
        self._lock = threading.Lock()
        self._first_t = None
        self._last_t = None
        self.next_update = None
        self.last_video_t = -math.inf
        self._calm: list = []                      # 처음 3.5초 샘플 (기준 진폭)
        self._recent: deque = deque()              # 최근 window 초 샘플
        self._display_cache = (None, None)         # (계산 시각, 결과)
        if pulse_rates_factory is None:
            from driver_state_tools.recent_pulse import RecentPulseRates
            pulse_rates_factory = lambda samples: RecentPulseRates(samples, confirmation_s=.08)
        self._pulse_rates = pulse_rates_factory
        self.samples_received = 0

    # 수신 스레드에서 호출
    def feed(self, t: float, ecg: float, ppg: float) -> None:
        if not (math.isfinite(t) and math.isfinite(ecg) and math.isfinite(ppg)):
            return
        with self._lock:
            if self._last_t is not None and t <= self._last_t:
                return                              # 시계가 거꾸로 가면 버린다
            self._last_t = t
            self._pending.append((t, ecg, ppg))
            self.samples_received += 1

    # 처리 스레드에서 호출 (MultimodalProcessor.update 안)
    def advance(self, video_t: float) -> dict:
        if not math.isfinite(video_t) or video_t < self.last_video_t:
            raise ValueError('Live time must increase')
        self.last_video_t = video_t
        with self._lock:
            ready = []
            while self._pending and self._pending[0][0] <= video_t + 1e-9:
                ready.append(self._pending.popleft())
        for t, ecg, ppg in ready:
            if self._first_t is None:
                self._first_t = t
                self.next_update = t + self.interval
            self.monitor.push_ecg(float(t), float(ecg))
            self.monitor.push_ppg(float(t), float(ppg))
            if t - self._first_t < CALM_SECONDS:
                self._calm.append((t, ecg, ppg))
            self._recent.append((t, ecg, ppg))
            if t >= self.next_update:
                self.latest = self.monitor.update(float(t)).to_dict()
                self.history.append(self.latest)
                self.next_update += self.interval
        while self._recent and self._recent[0][0] < video_t - self.window:
            self._recent.popleft()

        status = dict(self.latest) if self.latest else dict(t=None, level='unavailable',
                                                          reasons=['COLLECTING_SAMPLES'], channels={})
        status['model_reported_level'] = status['level']
        sample_t = self._recent[-1][0] if self._recent else None
        result_age = video_t - status['t'] if status.get('t') is not None else None
        sample_age = video_t - sample_t if sample_t is not None else None
        no_samples = sample_t is None
        stale = result_age is None or result_age > self.stale or sample_age is None or sample_age > self.stale
        if no_samples or stale:
            status.update(level='unavailable', hr=None,
                          reasons=['COLLECTING_SAMPLES' if no_samples or status.get('t') is None else 'SIGNAL_STALE'])
        status.update(available=status['level'] != 'unavailable', synthetic=False,
                      sample_timestamp=sample_t, sample_age_seconds=sample_age, age_seconds=result_age,
                      timeline_seconds=video_t, out_of_range=False, result_fresh=not (no_samples or stale))
        status['startup_settings'] = dict(self.startup_settings)
        if status['result_fresh']:
            status['display_pulse'] = self._display_pulse(video_t)
        return status

    def _display_pulse(self, now: float):
        cached_at, cached = self._display_cache
        if cached is not None and now - cached_at < self.display_every:
            return dict(cached, timestamp=now)
        rows = list(self._calm) + [r for r in self._recent if not self._calm or r[0] > self._calm[-1][0]]
        if len(rows) < 2:
            return None
        samples = np.asarray(rows, dtype=float)
        # RecentPulseRates 는 시간축 t<3.5 를 기준 구간으로 본다 → 첫 샘플을 0 으로 옮겨 넣는다
        shift = self._first_t or 0.0
        samples[:, 0] -= shift
        result = self._pulse_rates(samples).at(now - shift)
        result['timestamp'] = now
        if result.get('last_sample_timestamp') is not None:
            result['last_sample_timestamp'] += shift
        for name in ('ecg', 'ppg'):
            if result.get(name, {}).get('last_peak_timestamp') is not None:
                result[name]['last_peak_timestamp'] += shift
        self._display_cache = (now, result)
        return result

    def waveform(self, video_t, seconds=5.):
        rows = [r for r in self._recent if video_t - seconds <= r[0] <= video_t]
        return np.asarray(rows, dtype=float).reshape(-1, 3)

    def close(self):
        self.monitor.close()
