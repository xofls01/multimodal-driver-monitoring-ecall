"""녹화 파일(측정 폴더)을 판정할 때 쓰는 예열.

실시간에서는 처음 10초를 개인 기준 심박 측정에 쓰고, 분석 창(최소 6초, 모델 10초)이 찰 때까지 판정하지 않는다.
15초 안팎의 시연 영상을 그대로 넣으면 판정할 시간이 거의 남지 않는다.
실제 차에서는 사건 전부터 측정하고 있으므로, 영상 시작 시점에는 기준선과 분석 창이 이미 차 있어야 한다.

그래서 파일의 앞부분(기본 3초)을 박동 경계에서 잘라 20초가 되도록 이어 붙이고, 영상 첫 시각 직전에 먼저 넣는다.
판정 알고리즘과 10초 측정 규칙은 그대로이며, 측정이 영상 시작 전에 끝나 있을 뿐이다.
결과는 영상 첫 시각 이후의 판정만 낸다.

- 파일 전체를 먼저 넣으면 안 된다. 끝에 있는 사건(예: 심정지)이 예열에 들어가 영상 처음부터 위험이 나온다.
- 앞 3초가 정상 상태라고 가정한다. 비전의 중립 구간 기준선과 같은 가정이다. 처음부터 이상한 상태로 시작하면 그 상태가 기준선이 된다.

    from biosignal.offline import assess_recording
    statuses = assess_recording("recordings/E4", model_path=..., ppg_encoder_path=..., ppg_head_path=...)
"""
from __future__ import annotations

import csv
import json
import os
from typing import List, Optional, Tuple

import numpy as np
from scipy import signal

from .config import Config
from .monitor import BioSignalMonitor, BioStatus

Signal = Tuple[np.ndarray, np.ndarray, np.ndarray]   # 시각(초), ECG 원값, PPG 원값

SELF_WARMUP_S = 3.0      # 파일 앞에서 정상 구간으로 쓰는 길이
WARMUP_TOTAL_S = 20.0    # 이어 붙인 예열 길이. 기준선 10초 + 분석 창 12초를 채운다


def read_recording(folder: str) -> Signal:
    """측정 폴더의 biosignals.csv 를 읽는다. REC_ 폴더는 clip_s, SESSION_ 폴더는 pi_elapsed_s 가 시각이다."""
    with open(os.path.join(folder, "biosignals.csv"), encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    col = "clip_s" if "clip_s" in rows[0] else "pi_elapsed_s"
    return (np.array([float(r[col]) for r in rows]),
            np.array([float(r["ecg_raw"]) for r in rows]),
            np.array([float(r["ppg_raw"]) for r in rows]))


def self_warmup(sig: Signal, cfg: Optional[Config] = None, seconds: float = SELF_WARMUP_S,
                total: float = WARMUP_TOTAL_S) -> Optional[Signal]:
    """파일 앞 seconds 초를 R 피크 직전에서 잘라 total 초가 되도록 이어 붙인다.

    자르는 양끝이 같은 박동 위치라 이음매에서 박동 간격이 흐트러지지 않는다.
    앞부분에서 박동을 3개 이상 찾지 못하면 None 을 돌려준다 (예열 없이 판정한다).
    """
    cfg = cfg or Config()
    t, e, p = sig
    fs = 1.0 / float(np.median(np.diff(t)))
    m = t < t[0] + seconds
    if m.sum() < fs:
        return None
    b, a = signal.iirnotch(cfg.mains_hz, 6.0, fs=fs)
    qrs = signal.sosfiltfilt(signal.butter(2, [5, 30], "band", fs=fs, output="sos"), signal.filtfilt(b, a, e))
    lead = int(0.24 * fs)                       # R 피크보다 이만큼 앞에서 자른다 (P파 앞)
    peaks, _ = signal.find_peaks(qrs[m], height=0.45 * np.percentile(qrs[m], 99.5), distance=int(0.3 * fs))
    peaks = peaks[peaks > lead]
    if len(peaks) < 3:
        return None
    i0, i1 = peaks[0] - lead, peaks[-1] - lead
    reps = int(np.ceil(total * fs / (i1 - i0)))
    we, wp = np.tile(e[i0:i1], reps), np.tile(p[i0:i1], reps)
    return np.arange(len(we)) / fs, we, wp


def warm_up(monitor: BioSignalMonitor, warm: Signal, start_t: float) -> Optional[BioStatus]:
    """예열 신호를 start_t 직전 시각으로 옮겨 넣는다. 이 구간의 판정은 돌려주지 않는다(마지막 것만 참고용)."""
    wt, we, wp = warm
    step = float(np.median(np.diff(wt)))
    wt = wt - wt[-1] + start_t - step           # 마지막 예열 표본이 대상 첫 표본 한 칸 앞에 오도록
    next_update, last = wt[0] + 1.0, None
    for ti, ev, pv in zip(wt, we, wp):
        monitor.push_ecg(float(ti), float(ev))
        monitor.push_ppg(float(ti), float(pv))
        if ti >= next_update:
            next_update += 1.0
            last = monitor.update(float(ti))
    return last


def assess_recording(folder: str, warmup: Optional[str] = "self", cfg: Optional[Config] = None,
                     log_path: Optional[str] = None, model_path: Optional[str] = None,
                     ppg_encoder_path: Optional[str] = None, ppg_head_path: Optional[str] = None,
                     period_s: float = 1.0) -> List[BioStatus]:
    """측정 폴더를 실시간과 같은 순서로 넣고 period_s 마다 판정한다.

    warmup: "self" 는 파일 앞 3초 예열, None 은 예열 없음(실시간과 같이 처음 10초 측정),
            그 밖의 문자열은 같은 사람의 정상 측정 폴더 경로로 보고 그 신호 전체로 예열한다.
    log_path 를 주면 대상 파일 구간의 판정만 JSONL 로 저장한다.
    """
    cfg = cfg or Config()
    sig = read_recording(folder)
    t, e, p = sig
    mon = BioSignalMonitor(cfg, model_path=model_path, ppg_encoder_path=ppg_encoder_path,
                           ppg_head_path=ppg_head_path)
    if warmup == "self":
        warm = self_warmup(sig, cfg)
    elif warmup:
        warm = read_recording(warmup)
    else:
        warm = None
    if warm is not None:
        warm_up(mon, warm, float(t[0]))

    out: List[BioStatus] = []
    next_update = float(t[0]) + period_s
    for ti, ev, pv in zip(t, e, p):
        mon.push_ecg(float(ti), float(ev))
        mon.push_ppg(float(ti), float(pv))
        if ti >= next_update:
            next_update += period_s
            out.append(mon.update(float(ti)))
    mon.close()
    if log_path:
        with open(log_path, "w", encoding="utf-8") as f:
            for st in out:
                f.write(json.dumps(st.to_dict(), ensure_ascii=False) + "\n")
    return out
