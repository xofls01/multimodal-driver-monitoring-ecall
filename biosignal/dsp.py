"""신호처리: 필터, 피크 검출, 신호 품질 지표(SQI). 상태를 갖지 않는 순수 함수들."""
from __future__ import annotations

from typing import Tuple

import numpy as np
from scipy import signal


def moving_average(x: np.ndarray, n: int) -> np.ndarray:
    if n <= 1 or len(x) < n:
        return np.asarray(x, dtype=float).copy()
    pad = n // 2
    padded = np.pad(x, (pad, n - 1 - pad), mode="edge")
    return np.convolve(padded, np.ones(n) / n, mode="valid")


def bandpass(x: np.ndarray, fs: float, lo: float, hi: float, order: int = 3) -> np.ndarray:
    """위상 지연 없는 Butterworth 대역통과."""
    hi = min(hi, 0.45 * fs)
    sos = signal.butter(order, [lo, hi], btype="band", fs=fs, output="sos")
    padlen = min(len(x) - 1, 3 * (2 * len(sos) + 1) * 3)
    return signal.sosfiltfilt(sos, x, padlen=max(padlen, 0))


def notch(x: np.ndarray, fs: float, f0: float, q: float = 30.0) -> np.ndarray:
    if f0 >= 0.45 * fs:
        return x
    b, a = signal.iirnotch(f0, q, fs=fs)
    return signal.filtfilt(b, a, x, padlen=min(len(x) - 1, 30))


def clean_ecg(x: np.ndarray, fs: float, mains_hz: float = 60.0) -> np.ndarray:
    """전원 잡음 notch → 0.5–40 Hz 대역통과 → 약 12 ms 이동평균."""
    y = notch(x - np.median(x), fs, mains_hz)
    y = bandpass(y, fs, 0.5, 40.0)
    return moving_average(y, max(1, int(round(0.012 * fs))))


MODEL_FS = 125.0
MODEL_WINDOW_S = 10.0
MODEL_SAMPLES = int(MODEL_FS * MODEL_WINDOW_S)


def model_input(raw: np.ndarray, fs: float, mains_hz: float = 60.0) -> np.ndarray:
    """ECG 원신호 10초 → 모델 입력(125 Hz, 진폭 정규화). 학습과 추론이 같은 함수를 쓴다.

    실측 ECG는 공개 데이터보다 5 Hz 이하 흔들림이 훨씬 커서, 이 대역을 남기면 모델이
    정상 파형을 잡음으로 판정한다. QRS 판단에 필요한 5–40 Hz만 입력으로 쓴다.
    """
    x = np.asarray(raw, dtype=float)
    y = bandpass(notch(x - np.median(x), fs, mains_hz), fs, 5.0, 40.0)
    y = moving_average(y, max(1, int(round(0.012 * fs))))
    t_out = np.arange(MODEL_SAMPLES) / MODEL_FS
    y = np.interp(t_out, np.arange(len(y)) / fs, y)
    y = y - np.median(y)
    scale = np.percentile(np.abs(y), 99.5)
    if scale < 1e-9:
        return np.zeros(MODEL_SAMPLES, dtype=np.float32)
    return np.clip(y / scale, -3.0, 3.0).astype(np.float32)


def clean_pulse(x: np.ndarray, fs: float) -> np.ndarray:
    """PPG용 0.5–4 Hz 대역통과(30–240 bpm) → 약 40 ms 이동평균."""
    y = bandpass(x - np.median(x), fs, 0.5, 4.0)
    return moving_average(y, max(1, int(round(0.04 * fs))))


def detect_r_peaks(x: np.ndarray, fs: float) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Pan-Tompkins 방식의 R 피크 검출.

    Returns: (피크 인덱스, 피크별 QRS 진폭, QRS 대역 신호)
    """
    band = bandpass(x - np.median(x), fs, 5.0, 18.0)
    energy = np.gradient(band) ** 2
    # 제곱 에너지를 그대로 쓰면 큰 기외수축 하나가 임계값을 끌어올려 정상 박동을 놓친다
    mwi = np.sqrt(moving_average(energy, max(1, int(round(0.12 * fs)))))

    top = np.percentile(mwi, 99)
    if top <= 0:
        return np.array([], dtype=int), np.array([]), band

    cand, props = signal.find_peaks(mwi, height=0.3 * top, distance=max(1, int(0.2 * fs)))
    heights = props["peak_heights"]

    # 직전 박동 360 ms 안의 작은 피크는 T파로 보고 제거
    kept = []
    for i, c in enumerate(cand):
        if kept:
            prev = kept[-1]
            if (c - cand[prev]) < 0.36 * fs and heights[i] < 0.5 * heights[prev]:
                continue
        kept.append(i)
    cand = cand[kept]

    half = max(1, int(0.10 * fs))
    amp_half = max(1, int(0.06 * fs))
    peaks, amps = [], []
    for c in cand:
        lo, hi = max(0, c - half), min(len(band), c + half + 1)
        r = lo + int(np.argmax(np.abs(band[lo:hi])))
        a_lo, a_hi = max(0, r - amp_half), min(len(band), r + amp_half + 1)
        peaks.append(r)
        amps.append(float(np.ptp(band[a_lo:a_hi])))
    return np.array(peaks, dtype=int), np.array(amps), band


def detect_pulse_peaks(x: np.ndarray, fs: float) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """PPG 수축기 피크 검출. Returns: (피크 인덱스, prominence, 필터링된 신호)"""
    band = clean_pulse(x, fs)
    spread = np.percentile(band, 95) - np.percentile(band, 5)
    if spread <= 0:
        return np.array([], dtype=int), np.array([]), band
    peaks, props = signal.find_peaks(
        band, distance=max(1, int(0.3 * fs)), prominence=0.3 * spread
    )
    prominences = props["prominences"]
    # 맥박 사이의 작은 봉우리(중복 절흔, 잡음)를 박동으로 세지 않도록 한 번 더 거른다
    if len(peaks) >= 3:
        keep = prominences >= 0.5 * np.median(prominences)
        peaks, prominences = peaks[keep], prominences[keep]
    return peaks, prominences, band


def template_correlation(x: np.ndarray, peaks: np.ndarray, fs: float,
                         before_s: float, after_s: float) -> float:
    """각 박동과 평균 박동 파형의 상관계수 평균. 파형이 일정할수록 1에 가깝다."""
    pre, post = int(before_s * fs), int(after_s * fs)
    beats = [x[p - pre:p + post] for p in peaks if p - pre >= 0 and p + post <= len(x)]
    if len(beats) < 3:
        return 0.0
    beats = np.asarray(beats)
    template = np.median(beats, axis=0)
    t = template - template.mean()
    b = beats - beats.mean(axis=1, keepdims=True)
    denom = np.linalg.norm(b, axis=1) * np.linalg.norm(t)
    if not np.all(denom > 0):
        return 0.0
    return float(np.mean(b @ t / denom))


def rr_intervals(beat_times: np.ndarray, lo: float = 0.25, hi: float = 2.5) -> np.ndarray:
    rr = np.diff(beat_times)
    return rr[(rr >= lo) & (rr <= hi)]


def beat_sqi(x: np.ndarray, peaks: np.ndarray, fs: float, kind: str) -> float:
    """박동 파형 일관성과 RR 타당성을 합친 품질 지표(0~1)."""
    if len(peaks) < 3:
        return 0.0
    if kind == "ecg":
        corr = template_correlation(x, peaks, fs, 0.10, 0.15)
    else:
        corr = template_correlation(x, peaks, fs, 0.15, 0.25)
    shape = float(np.clip((corr - 0.6) / 0.3, 0.0, 1.0))

    raw_rr = np.diff(peaks) / fs
    plausible = float(np.mean((raw_rr >= 0.25) & (raw_rr <= 2.5)))
    sqi = shape * plausible

    if kind != "ecg" and len(raw_rr) >= 3:
        # 협대역 잡음은 파형이 그럴듯해 보이므로 PPG는 박동 간격 규칙성도 함께 본다
        cv = float(np.std(raw_rr) / np.mean(raw_rr))
        sqi *= float(np.clip(1.0 - (cv - 0.15) / 0.25, 0.0, 1.0))
    return sqi


def snr_db(reference: np.ndarray, measured: np.ndarray) -> float:
    """깨끗한 기준 신호를 아는 경우의 SNR. 합성 신호 평가에 사용한다."""
    ref = reference - np.mean(reference)
    err = (measured - np.mean(measured)) - ref
    return float(10.0 * np.log10(np.sum(ref ** 2) / max(np.sum(err ** 2), 1e-12)))
