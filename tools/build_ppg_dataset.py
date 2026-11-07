"""BIDMC PPG로 판정부 학습용 데이터셋을 만든다. 기록(환자) 단위로 나눈다.

실행: python tools/build_ppg_dataset.py --out <데이터 폴더> --ecg-data <ECG 데이터 폴더>
잡음은 ECG 데이터셋 생성 때 받은 nstdb 캐시를 재사용한다.

라벨: 환자 모니터의 HR 수치 기준으로 ECG 데이터셋과 같은 구간을 쓴다.
     HR ≤ 40·≥ 150 위험 / 40–50·120–150 주의 / 그 외 정상 / 잡음·평탄·포화 측정 불가
PPG가 망가져 모니터 HR과 맥박 피크 HR이 15 bpm 넘게 다른 실제 창은 라벨을 믿을 수 없어 뺀다.
실제 서맥·빈맥이 적어 시간축 변환으로 만들며, 실제 PPG 맥박 소실이나 부정맥 라벨은 없다.
"""
import argparse
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np

from biosignal.dsp import MODEL_WINDOW_S, detect_pulse_peaks, rr_intervals
from biosignal.ppg_model import ppg_model_input

FS = 125.0
W = MODEL_WINDOW_S
NORMAL, CAUTION, DANGER, UNAVAILABLE = 0, 1, 2, 3
RECORDS = [f"bidmc{i:02d}" for i in range(1, 54)]
order = np.random.default_rng(0).permutation(53)
SPLITS = {"train": [RECORDS[i] for i in order[:37]], "val": [RECORDS[i] for i in order[37:43]],
          "test": [RECORDS[i] for i in order[43:]]}
N_REAL, N_SCALED, N_UNAVAIL = 300, 500, 120


def download(root, rec):
    import wfdb
    path = os.path.join(root, "cache", f"{rec}.npz")
    if os.path.exists(path):
        return
    r = wfdb.rdrecord(rec, pn_dir="bidmc")
    names = [n.strip(" ,").upper() for n in r.sig_name]
    ppg = np.nan_to_num(r.p_signal[:, names.index("PLETH")]).astype(np.float32)
    n = wfdb.rdrecord(rec + "n", pn_dir="bidmc")
    names = [x.strip(" ,").upper() for x in n.sig_name]
    hr = n.p_signal[:, names.index("HR")].astype(np.float32)
    np.savez_compressed(path, ppg=ppg, hr=hr)


def hr_label(hr):
    if hr <= 40 or hr >= 150:
        return DANGER
    return CAUTION if (hr <= 50 or hr >= 120) else NORMAL


def load_noise(ecg_root, split):
    out = []
    for name in ("em", "ma"):
        sig = np.load(os.path.join(ecg_root, "cache", f"nstdb_{name}.npz"))["sig"]
        cut = int(0.7 * len(sig))
        out.append(sig[cut:] if split == "test" else sig[:cut])
    return out


def sample_noise(rng, bank, n):
    sig = bank[rng.integers(len(bank))]
    i = rng.integers(0, len(sig) - n)
    return sig[i:i + n, rng.integers(sig.shape[1])].astype(float)


def corrupt(rng, x, bank):
    """실측 PPG처럼 ADC 잡음, 전원 잡음, 움직임 잡음을 섞는다. 약 40%는 그대로 둔다."""
    if rng.random() > 0.6:
        return x, False
    y = x - x.mean()
    span = np.ptp(y) + 1e-9
    t = np.arange(len(y)) / FS
    y = y + rng.uniform(0.01, 0.2) * span * rng.normal(size=len(y))
    if rng.random() < 0.3:
        y = y + rng.uniform(0.02, 0.2) * span * np.sin(2 * np.pi * 60 * t + rng.uniform(0, 6))
    if rng.random() < 0.3:
        n = sample_noise(rng, bank, len(y))
        n = n - n.mean()
        snr = rng.uniform(8, 24)
        y = y + n * np.sqrt(np.mean(y ** 2) / (np.mean(n ** 2) + 1e-9) / 10 ** (snr / 10))
    return y, True


def unavailable(rng, seg, bank):
    n = int(W * FS)
    t = np.arange(n) / FS
    kind = rng.integers(6)
    if kind == 0:
        return sample_noise(rng, bank, n)
    if kind == 1:
        return np.full(n, rng.uniform(0, 1)) + rng.uniform(1e-4, 5e-3) * rng.normal(size=n)
    if kind == 2:
        x = np.full(n, 1.0)
        for _ in range(rng.integers(0, 4)):
            a = rng.integers(n)
            x[a:a + rng.integers(30, 400)] = 0.0
        return x + rng.uniform(0, 0.01) * rng.normal(size=n)
    if kind == 3:
        return np.sin(2 * np.pi * 60 * t) + rng.uniform(0.01, 0.3) * rng.normal(size=n)
    if kind == 4:  # 맥박이 잡음에 묻힘
        y = seg[:n] - seg[:n].mean()
        m = sample_noise(rng, bank, n)
        return y + m * np.sqrt(np.mean(y ** 2) / (np.mean(m ** 2) + 1e-9) * rng.uniform(2, 15))
    return np.sin(2 * np.pi * rng.uniform(0.05, 0.4) * t) + rng.uniform(0.2, 1.5) * rng.normal(size=n)


def make_record(args):
    root, ecg_root, split, rec, seed = args
    rng = np.random.default_rng(seed)
    d = np.load(os.path.join(root, "cache", f"{rec}.npz"))
    ppg, hr_track = d["ppg"].astype(float), d["hr"].astype(float)
    bank = load_noise(ecg_root, split)
    dur = len(ppg) / FS
    out = []

    def window(t0, span):
        i0 = int(t0 * FS)
        return ppg[i0:i0 + int(span * FS)]

    def monitor_hr(t0, span):
        v = hr_track[max(0, int(t0)):int(t0 + span) + 1]
        v = v[np.isfinite(v) & (v > 20)]
        return float(np.median(v)) if len(v) else None

    def trustworthy(seg, hr):
        peaks, _, _ = detect_pulse_peaks(seg, FS)
        rr = rr_intervals(peaks / FS)
        return len(rr) >= 3 and abs(60.0 / np.median(rr) - hr) <= 15.0

    for _ in range(N_REAL):
        t0 = rng.uniform(0, dur - W - 1)
        seg = window(t0, W)
        hr = monitor_hr(t0, W)
        if hr is None or not trustworthy(seg, hr):
            continue
        x, noisy = corrupt(rng, seg, bank)
        out.append((ppg_model_input(x, FS), hr_label(hr), hr, noisy, False))

    n = int(W * FS)
    for _ in range(N_SCALED):
        band = rng.integers(3)
        target = (rng.uniform(52, 118) if band == 0 else
                  rng.choice([rng.uniform(41, 50), rng.uniform(120, 149)]) if band == 1 else
                  rng.choice([rng.uniform(28, 40), rng.uniform(150, 220)]))
        t0 = rng.uniform(0, dur / 2)
        src = monitor_hr(t0, W)
        if src is None:
            continue
        scale = target / src
        if not 0.4 <= scale <= 2.6 or t0 + W * scale + 1 >= dur:
            continue
        src = monitor_hr(t0, W * scale)
        seg = window(t0, W * scale)
        if src is None or not trustworthy(seg, src):
            continue
        seg = np.interp(np.linspace(0, len(seg) - 1, n), np.arange(len(seg)), seg)
        hr = src * scale
        x, noisy = corrupt(rng, seg, bank)
        out.append((ppg_model_input(x, FS), hr_label(hr), hr, noisy, True))

    for _ in range(N_UNAVAIL):
        t0 = rng.uniform(0, dur - W - 1)
        out.append((ppg_model_input(unavailable(rng, window(t0, W), bank), FS), UNAVAILABLE, np.nan, True, False))

    return dict(x=np.stack([o[0] for o in out]).astype(np.float16), y=np.array([o[1] for o in out], np.int8),
                hr=np.array([o[2] for o in out], np.float32), noisy=np.array([o[3] for o in out]),
                scaled=np.array([o[4] for o in out]), rec=np.array([rec] * len(out)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--ecg-data", required=True)
    ap.add_argument("--workers", type=int, default=10)
    args = ap.parse_args()
    os.makedirs(os.path.join(args.out, "cache"), exist_ok=True)
    t0 = time.time()
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda r: download(args.out, r), RECORDS))
    print(f"다운로드 완료 {len(RECORDS)}개 {time.time() - t0:.0f}s", flush=True)
    for split, recs in SPLITS.items():
        tasks = [(args.out, args.ecg_data, split, r, int.from_bytes(f"{split}{r}".encode(), "little") % 2 ** 31)
                 for r in recs]
        with ProcessPoolExecutor(args.workers) as pool:
            parts = list(pool.map(make_record, tasks))
        merged = {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}
        np.savez(os.path.join(args.out, f"{split}.npz"), **merged)
        c = np.bincount(merged["y"], minlength=4)
        print(f"{split}: {len(merged['y'])}개 normal={c[0]} caution={c[1]} danger={c[2]} unavailable={c[3]} "
              f"({time.time() - t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
