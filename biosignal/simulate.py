"""합성 신호 시나리오(ECG·PPG).

실제 심정지는 사람으로 시연하거나 검증할 수 없으므로 이 신호로 판단 로직을 시험한다.
시연 때 위험 상황을 재생하는 용도로도 쓸 수 있다.
합성 신호 결과는 로직 동작 확인일 뿐, 실제 성능 수치가 아니다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Iterator, Optional, Tuple

import numpy as np
from scipy import signal

ECG_FS, PPG_FS = 250.0, 100.0
ECG_MID, ECG_GAIN = 512.0, 150.0
PPG_DC, PPG_GAIN = 60000.0, 800.0

_PQRST = [(-0.20, 0.025, 0.15), (-0.04, 0.010, -0.15), (0.0, 0.012, 1.0),
          (0.04, 0.012, -0.25), (0.25, 0.050, 0.30)]
_PULSE = [(0.18, 0.09, 1.0), (0.48, 0.12, 0.4)]


def _render(t: np.ndarray, beats: np.ndarray, shape, fs: float) -> np.ndarray:
    y = np.zeros_like(t)
    for b in beats:
        for mu, sigma, amp in shape:
            c = b + mu
            lo = max(0, int((c - 5 * sigma - t[0]) * fs))
            hi = min(len(t), int((c + 5 * sigma - t[0]) * fs) + 1)
            if lo < hi:
                y[lo:hi] += amp * np.exp(-((t[lo:hi] - c) ** 2) / (2 * sigma ** 2))
    return y


def beat_times(hr_fn: Callable[[float], float], dur: float, rng: np.random.Generator,
               rr_sd: float = 0.02, stop_at: Optional[float] = None) -> np.ndarray:
    beats, t = [], 0.3
    while t < dur and (stop_at is None or t < stop_at):
        beats.append(t)
        t += max(0.25, 60.0 / hr_fn(t) * (1 + rng.normal(0, rr_sd)))
    return np.array(beats)


def ecg_clean(dur: float, beats: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    t = np.arange(0, dur, 1 / ECG_FS)
    return t, _render(t, beats, _PQRST, ECG_FS)


def ecg_noise(t: np.ndarray, rng: np.random.Generator, vibration: float = 0.0) -> np.ndarray:
    """기저선 흔들림 + 전원 60 Hz + 백색잡음 + (선택) 주행 진동. 단위는 R파=1."""
    n = (0.10 * np.sin(2 * np.pi * 0.3 * t + rng.uniform(0, 6))
         + 0.06 * np.sin(2 * np.pi * 60 * t)
         + 0.02 * rng.normal(size=len(t)))
    if vibration > 0:
        sos = signal.butter(2, [8, 30], btype="band", fs=ECG_FS, output="sos")
        v = signal.sosfilt(sos, rng.normal(size=len(t)))
        n = n + vibration * v / (np.std(v) + 1e-9)
    return n


def to_ecg_adc(y: np.ndarray) -> np.ndarray:
    return np.clip(ECG_MID + ECG_GAIN * y, 0, 1023)


@dataclass
class Streams:
    ecg_t: np.ndarray
    ecg: np.ndarray
    ecg_lo: np.ndarray
    ppg_t: np.ndarray
    ppg: np.ndarray
    ppg_contact: np.ndarray
    onset: Optional[float] = None
    true_hr: Callable[[float], float] = field(default=lambda t: float("nan"))


def build(dur: float = 120.0, seed: int = 0, hr_fn: Callable[[float], float] = lambda t: 72.0,
          stop_at: Optional[float] = None, rr_sd: float = 0.02, vibration: float = 0.0,
          onset: Optional[float] = None) -> Streams:
    rng = np.random.default_rng(seed)
    beats = beat_times(hr_fn, dur, rng, rr_sd, stop_at)

    t_e, y = ecg_clean(dur, beats)
    ecg = to_ecg_adc(y + ecg_noise(t_e, rng, vibration))

    t_p = np.arange(0, dur, 1 / PPG_FS)
    ppg = (PPG_DC + PPG_GAIN * _render(t_p, beats, _PULSE, PPG_FS)
           + 30 * np.sin(2 * np.pi * 0.25 * t_p) + 8 * rng.normal(size=len(t_p)))

    return Streams(t_e, ecg, np.zeros(len(t_e), bool), t_p, ppg, np.ones(len(t_p), bool),
                   onset=onset, true_hr=hr_fn)


def _step(before: float, after: float, at: float) -> Callable[[float], float]:
    return lambda t: before if t < at else after


# ---------- 시나리오 ----------
def normal(seed=0) -> Streams:
    return build(seed=seed)


def driving_vibration(seed=0) -> Streams:
    return build(seed=seed, vibration=0.08)


def cardiac_arrest(seed=0, onset=60.0) -> Streams:
    return build(seed=seed, stop_at=onset, onset=onset)


def ventricular_fibrillation(seed=0, onset=60.0) -> Streams:
    """ECG는 빠르고 무질서하게 흔들리지만 맥박은 사라지는 상황."""
    s = cardiac_arrest(seed, onset)
    rng = np.random.default_rng(seed + 200)
    mask = s.ecg_t >= onset
    sos = signal.butter(2, [3, 9], btype="band", fs=ECG_FS, output="sos")
    vf = signal.sosfilt(sos, rng.normal(size=len(s.ecg_t)))
    vf = 0.4 * vf / np.std(vf)
    s.ecg[mask] = to_ecg_adc(vf[mask])
    return s


def bradycardia(seed=0, onset=60.0) -> Streams:
    return build(seed=seed, hr_fn=_step(72, 35, onset), onset=onset)


def tachycardia(seed=0, onset=60.0) -> Streams:
    return build(seed=seed, hr_fn=_step(72, 165, onset), onset=onset)


def abrupt_rise(seed=0, onset=60.0) -> Streams:
    return build(seed=seed, hr_fn=_step(70, 110, onset), onset=onset)


def irregular_rhythm(seed=0, onset=60.0) -> Streams:
    s = build(seed=seed, onset=onset)
    rng = np.random.default_rng(seed + 300)
    b1 = beat_times(lambda t: 72.0, onset, rng)
    b2 = onset + beat_times(lambda t: 85.0, s.ecg_t[-1] - onset, rng, rr_sd=0.28)
    beats = np.concatenate([b1, b2])
    _, y = ecg_clean(s.ecg_t[-1] + 1 / ECG_FS, beats)
    s.ecg = to_ecg_adc(y[:len(s.ecg_t)] + ecg_noise(s.ecg_t, rng))
    return s


def electrode_off(seed=0, onset=60.0) -> Streams:
    """ECG 전극만 탈락(LO 신호 있음). 맥박은 PPG로 계속 확인된다."""
    s = build(seed=seed, onset=onset)
    mask = s.ecg_t >= onset
    s.ecg[mask] = 1023.0
    s.ecg_lo[mask] = True
    return s


def all_contact_lost(seed=0, onset=60.0) -> Streams:
    """ECG·PPG가 모두 떨어진 상황. 측정 불가이지 심정지가 아니다."""
    s = electrode_off(seed, onset)
    rng = np.random.default_rng(seed + 400)
    m = s.ppg_t >= onset
    s.ppg[m] = 1000.0 + 5 * rng.normal(size=m.sum())
    s.ppg_contact[m] = False
    return s


def ecg_motion_artifact(seed=0, onset=60.0) -> Streams:
    """ECG에 심한 움직임 잡음, PPG는 정상. 위험으로 판정하면 안 된다."""
    s = build(seed=seed, onset=onset)
    rng = np.random.default_rng(seed + 500)
    mask = s.ecg_t >= onset
    sos = signal.butter(2, [2, 25], btype="band", fs=ECG_FS, output="sos")
    a = signal.sosfilt(sos, rng.normal(size=len(s.ecg_t)))
    s.ecg[mask] = np.clip(s.ecg[mask] + 200 * a[mask] / np.std(a), 0, 1023)
    return s


def ecg_only_arrest(seed=0, onset=60.0) -> Streams:
    """전극은 붙어 있는데 PPG 접촉이 없고 ECG만 평탄해진 상황. 근거가 하나뿐이다."""
    s = cardiac_arrest(seed, onset)
    rng = np.random.default_rng(seed + 600)
    s.ppg[:] = 1000.0 + 5 * rng.normal(size=len(s.ppg))
    s.ppg_contact[:] = False
    return s


def ecg_only_tachycardia(seed=0, onset=60.0) -> Streams:
    """가슴 전극만 붙인 시연 구성에서의 빈맥. PPG 접촉이 없어 근거가 하나뿐이다."""
    s = tachycardia(seed, onset)
    rng = np.random.default_rng(seed + 800)
    s.ppg[:] = 1000.0 + 5 * rng.normal(size=len(s.ppg))
    s.ppg_contact[:] = False
    return s


def drowsy(seed=0, onset=90.0) -> Streams:
    return build(dur=240.0, seed=seed, onset=onset, rr_sd=0.03,
                 hr_fn=lambda t: 72.0 if t < onset else 62.0)


def _ramp(before: float, after: float, start: float, end: float) -> Callable[[float], float]:
    return lambda t: before + (after - before) * min(1.0, max(0.0, (t - start) / (end - start)))


def tachycardia_mild(seed=0, onset=60.0) -> Streams:
    return build(seed=seed, hr_fn=_step(72, 125, onset), onset=onset)


def bradycardia_mild(seed=0, onset=60.0) -> Streams:
    return build(seed=seed, hr_fn=_step(72, 45, onset), onset=onset)


def gradual_rise(seed=0, onset=60.0) -> Streams:
    return build(dur=150.0, seed=seed, hr_fn=_ramp(72, 112, onset, onset + 60), onset=onset)


def abrupt_fall(seed=0, onset=60.0) -> Streams:
    """기준선보다 크게 떨어졌지만 절대값은 정상 범위인 경우."""
    return build(seed=seed, hr_fn=_step(95, 62, onset), onset=onset)


def high_start_then_settles(seed=0, onset=40.0) -> Streams:
    """시동 직후 흥분 상태. 안정된 뒤의 심박수를 기준선으로 잡아야 한다."""
    return build(seed=seed, hr_fn=_step(118, 74, onset), onset=onset)


def athlete_low_baseline(seed=0) -> Streams:
    """평소 심박이 46인 사람. 계속 경고하면 안 된다."""
    return build(seed=seed, hr_fn=lambda t: 46.0)


def pulseless_electrical_activity(seed=0, onset=60.0) -> Streams:
    """ECG 파형은 그대로인데 맥박만 사라진 경우."""
    s = build(seed=seed, onset=onset)
    rng = np.random.default_rng(seed + 900)
    m = s.ppg_t >= onset
    s.ppg[m] = PPG_DC + 8 * rng.normal(size=m.sum())
    return s


def bradycardia_to_arrest(seed=0, onset=60.0) -> Streams:
    """심박이 30초에 걸쳐 떨어지다가 멈추는 경우."""
    return build(seed=seed, hr_fn=_ramp(72, 28, onset, onset + 30), stop_at=onset + 30, onset=onset)


SCENARIOS: Dict[str, Callable[..., Streams]] = {
    "tachycardia_125": tachycardia_mild,
    "bradycardia_45": bradycardia_mild,
    "gradual_rise_72_112": gradual_rise,
    "abrupt_fall_95_62": abrupt_fall,
    "high_start_settles": high_start_then_settles,
    "athlete_low_baseline": athlete_low_baseline,
    "pulseless_electrical_activity": pulseless_electrical_activity,
    "bradycardia_to_arrest": bradycardia_to_arrest,
    "normal": normal,
    "driving_vibration": driving_vibration,
    "cardiac_arrest": cardiac_arrest,
    "ventricular_fibrillation": ventricular_fibrillation,
    "bradycardia_35": bradycardia,
    "tachycardia_165": tachycardia,
    "abrupt_rise_70_110": abrupt_rise,
    "irregular_rhythm": irregular_rhythm,
    "electrode_off": electrode_off,
    "all_contact_lost": all_contact_lost,
    "ecg_motion_artifact": ecg_motion_artifact,
    "ecg_only_arrest": ecg_only_arrest,
    "ecg_only_tachycardia": ecg_only_tachycardia,
    "drowsy": drowsy,
}


def replay(monitor, s: Streams, update_period: float = 1.0) -> Iterator:
    """신호를 시간순으로 모니터에 넣고, 주기마다 판정 결과를 내보낸다."""
    e = p = 0
    end = float(s.ecg_t[-1])
    now = update_period
    while now <= end:
        while e < len(s.ecg_t) and s.ecg_t[e] <= now:
            monitor.push_ecg(s.ecg_t[e], s.ecg[e], bool(s.ecg_lo[e]))
            e += 1
        while p < len(s.ppg_t) and s.ppg_t[p] <= now:
            monitor.push_ppg(s.ppg_t[p], s.ppg[p], bool(s.ppg_contact[p]))
            p += 1
        yield monitor.update(now)
        now += update_period
