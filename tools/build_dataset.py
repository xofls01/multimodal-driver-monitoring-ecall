"""PhysioNet 실측 ECG로 학습/검증/테스트 데이터셋을 만든다.

실행: python tools/build_dataset.py --out <데이터 폴더>
필요 패키지: wfdb numpy scipy

데이터 누수를 막기 위해 환자(기록) 단위로 나눈다. 같은 환자인 mitdb 201·202는 함께 학습에 둔다.
잡음(nstdb)도 시간 구간을 나눠 앞 70%는 학습·검증, 뒤 30%는 테스트에만 쓴다.

라벨 기준
  danger      : VF/VFL/VT 리듬, 또는 HR ≤ 40, HR ≥ 150
  caution     : 40 < HR ≤ 50, 120 ≤ HR < 150, 또는 AF·불규칙 리듬(RR CV > 0.25)
  normal      : 그 외
  unavailable : 잡음·평탄·포화 등 심박을 판단할 수 없는 신호
평탄한 신호는 심정지와 전극 탈락을 파형만으로 구분할 수 없으므로 unavailable로 학습하고,
맥박 소실 판단은 BioSignalMonitor의 다중 센서 규칙이 맡는다.
"""
import argparse
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np

from biosignal.dsp import MODEL_WINDOW_S, model_input

FS = 250.0
W = MODEL_WINDOW_S
NORMAL, CAUTION, DANGER, UNAVAILABLE = 0, 1, 2, 3
BEATS = set("NLRBAaJSVrFejnE/fQ!")
DANGER_RHYTHMS = {"VF", "VFL", "VFIB", "VT"}
SKIP_RHYTHMS = {"ASYS", "VER"}  # 파형만으로 라벨을 정할 수 없어 제외

MITDB_TEST = "100 103 105 111 113 117 121 123 200 210 212 213 214 219 221 222 228 231 232 233 234".split()
MITDB_VAL = "108 114 209 223".split()
MITDB_TRAIN = ("101 106 109 112 115 116 118 119 122 124 201 202 203 205 207 208 215 220 230").split()
NSR = "16265 16272 16273 16420 16483 16539 16773 16786 16795 17052 17453 18177 18184 19088 19090 19093 19140 19830".split()
VFDB = "418 419 420 421 422 423 424 425 426 427 428 429 430 602 605 607 609 610 611 612 614 615".split()
CUDB = [f"cu{i:02d}" for i in range(1, 36)]

SPLITS = {
    "train": {"mitdb": MITDB_TRAIN, "nsrdb": NSR[:12], "vfdb": VFDB[:14], "cudb": CUDB[:24]},
    "val": {"mitdb": MITDB_VAL, "nsrdb": NSR[12:14], "vfdb": VFDB[14:18], "cudb": CUDB[24:29]},
    "test": {"mitdb": MITDB_TEST, "nsrdb": NSR[14:], "vfdb": VFDB[18:], "cudb": CUDB[29:]},
}
# 기록 하나당 추출할 창 수 (실제 심박 그대로, 심박 변환 포함, 잡음/측정불가)
COUNTS = {"mitdb": (500, 700, 150), "nsrdb": (250, 900, 150), "vfdb": (300, 0, 40), "cudb": (150, 0, 20)}


def _cache_path(root, db, rec):
    return os.path.join(root, "cache", f"{db}_{rec}.npz")


def download(root, db, rec):
    """기록을 받아 250 Hz로 변환하고 박동·리듬 주석과 함께 저장한다."""
    import wfdb
    path = _cache_path(root, db, rec)
    if os.path.exists(path):
        return path
    header = wfdb.rdheader(rec, pn_dir=db)
    sampto = min(header.sig_len, int(header.fs * 1800))
    r = wfdb.rdrecord(rec, pn_dir=db, sampto=sampto)
    t_in = np.arange(r.p_signal.shape[0]) / r.fs
    t_out = np.arange(0, t_in[-1], 1 / FS)
    sig = np.stack([np.interp(t_out, t_in, np.nan_to_num(r.p_signal[:, c]))
                    for c in range(r.p_signal.shape[1])], axis=1).astype(np.float32)
    if db == "nstdb":
        np.savez_compressed(path, sig=sig)
        return path
    a = wfdb.rdann(rec, "atr", pn_dir=db, sampto=sampto)
    beats = [s / r.fs for s, sym in zip(a.sample, a.symbol) if sym in BEATS]
    rhythms, cur, cur_t = [], None, 0.0
    flutter_on = None
    for s, sym, aux in zip(a.sample, a.symbol, a.aux_note):
        t = s / r.fs
        if sym == "[":
            flutter_on = t
        elif sym == "]":
            rhythms.append((flutter_on if flutter_on is not None else 0.0, t, "VF"))
            flutter_on = None
        note = (aux or "").strip("\x00 ")
        if note.startswith("("):
            if cur is not None:
                rhythms.append((cur_t, t, cur))
            cur, cur_t = note[1:], t
    end = sampto / r.fs
    if flutter_on is not None:
        rhythms.append((flutter_on, end, "VF"))
    if cur is not None:
        rhythms.append((cur_t, end, cur))
    np.savez_compressed(
        path, sig=sig, beats=np.array(beats),
        r_start=np.array([x[0] for x in rhythms]), r_end=np.array([x[1] for x in rhythms]),
        r_name=np.array([x[2] for x in rhythms], dtype="U8"))
    return path


def overlap(rec, t0, t1, names):
    total = 0.0
    for s, e, n in zip(rec["r_start"], rec["r_end"], rec["r_name"]):
        if n in names:
            total += max(0.0, min(e, t1) - max(s, t0))
    return total / (t1 - t0)


def hr_label(hr, irregular):
    if hr <= 40 or hr >= 150:
        return DANGER
    if hr <= 50 or hr >= 120:
        return CAUTION
    return CAUTION if irregular else NORMAL


class NoiseBank:
    def __init__(self, root, split):
        self.items = {}
        for name in ("em", "ma", "bw"):
            sig = np.load(_cache_path(root, "nstdb", name))["sig"]
            cut = int(0.7 * len(sig))
            self.items[name] = sig[cut:] if split == "test" else sig[:cut]

    def sample(self, rng, n, kinds=("em", "ma", "bw")):
        sig = self.items[kinds[rng.integers(len(kinds))]]
        start = rng.integers(0, len(sig) - n)
        return sig[start:start + n, rng.integers(sig.shape[1])].astype(float)


def add_noise(rng, x, bank, snr_db):
    n = bank.sample(rng, len(x))
    n = n - n.mean()
    p_sig = np.mean((x - np.median(x)) ** 2) + 1e-9
    p_noise = np.mean(n ** 2) + 1e-9
    return x + n * np.sqrt(p_sig / p_noise / 10 ** (snr_db / 10))


def mains(rng, x):
    t = np.arange(len(x)) / FS
    return x + rng.uniform(0.05, 0.4) * np.ptp(x) * np.sin(2 * np.pi * 60 * t + rng.uniform(0, 6))


def corrupt(rng, x, bank):
    """학습 창에 잡음을 넣는다. 약 40%는 잡음 없이 둔다."""
    noisy = rng.random() < 0.6
    if noisy:
        x = add_noise(rng, x, bank, rng.uniform(6, 24))
        if rng.random() < 0.25:
            x = mains(rng, x)
    if rng.random() < 0.3:
        x = -x
    return x, noisy


def unavailable_window(rng, seg, bank):
    n = int(W * FS)
    kind = rng.integers(6)
    if kind == 0:   # 전극 움직임·근잡음만 있는 신호
        x = bank.sample(rng, n, ("em", "ma"))
    elif kind == 1:  # 평탄
        x = np.full(n, rng.uniform(-1, 1)) + rng.uniform(1e-4, 5e-3) * rng.normal(size=n)
    elif kind == 2:  # 포화(레일에 붙었다 떨어짐)
        x = np.full(n, 1.0)
        for _ in range(rng.integers(0, 3)):
            a = rng.integers(n)
            x[a:a + rng.integers(50, 1000)] = -1.0
        x += rng.uniform(0, 0.01) * rng.normal(size=n)
    elif kind == 3:  # 전원 잡음
        t = np.arange(n) / FS
        x = np.sin(2 * np.pi * 60 * t) + rng.uniform(0.01, 0.2) * rng.normal(size=n)
    elif kind == 4:  # ECG가 잡음에 묻힘(SNR -12 ~ -3 dB)
        x = add_noise(rng, seg[:n], bank, rng.uniform(-12, -3))
    else:           # 기저 변동과 백색잡음
        t = np.arange(n) / FS
        x = np.sin(2 * np.pi * rng.uniform(0.1, 1) * t) + rng.uniform(0.05, 1) * rng.normal(size=n)
    return x


def make_record(args):
    root, split, db, rec_id, seed = args
    rng = np.random.default_rng(seed)
    d = np.load(_cache_path(root, db, rec_id))
    rec = {k: d[k] for k in d.files}
    sig, beats = rec["sig"], rec["beats"]
    bank = NoiseBank(root, split)
    dur = len(sig) / FS
    n_real, n_scaled, n_unavail = COUNTS[db]
    out = []  # (x, label, hr, noisy, scaled)
    has_beats = db in ("mitdb", "nsrdb")

    def segment(t0, length):
        i0 = int(t0 * FS)
        ch = 0 if (sig.shape[1] == 1 or rng.random() < 0.8) else 1
        return sig[i0:i0 + int(length * FS), ch].astype(float)

    def rhythm_window():
        """주석이 달린 위험 리듬(VF·VT) 또는 잡음 구간에서 창을 뽑는다."""
        t0 = rng.uniform(0, dur - W - 1)
        d_frac = overlap(rec, t0, t0 + W, DANGER_RHYTHMS)
        if d_frac >= 0.6:
            return t0, DANGER
        return (t0, UNAVAILABLE) if overlap(rec, t0, t0 + W, {"NOISE"}) >= 0.9 else (None, None)

    # 위험 리듬: 주석 구간 자체를 사용한다
    if db in ("vfdb", "cudb", "mitdb"):
        want = n_real if db != "mitdb" else 80
        danger_spans = [(s, e) for s, e, n in zip(rec["r_start"], rec["r_end"], rec["r_name"])
                        if n in DANGER_RHYTHMS and e - s >= 6.0]
        total = sum(e - s for s, e in danger_spans)
        for _ in range(want if total > 0 else 0):
            s, e = danger_spans[rng.choice(len(danger_spans), p=[(b - a) / total for a, b in danger_spans])]
            t0 = rng.uniform(max(0, s - 2.0), max(s - 2.0, e - W + 2.0))
            if t0 + W + 0.1 >= dur or t0 < 0:
                continue
            if overlap(rec, t0, t0 + W, DANGER_RHYTHMS) < 0.6:
                continue
            x, noisy = corrupt(rng, segment(t0, W), bank)
            out.append((model_input(x, FS), DANGER, np.nan, noisy, False))
    if db in ("vfdb",):
        for _ in range(60):
            t0, label = rhythm_window()
            if t0 is not None and label == UNAVAILABLE:
                x = segment(t0, W)
                out.append((model_input(x, FS), UNAVAILABLE, np.nan, True, False))

    if has_beats:
        def beat_window(scale):
            span = W * scale
            t0 = rng.uniform(0, dur - span - 3)
            if overlap(rec, t0, t0 + span, DANGER_RHYTHMS | SKIP_RHYTHMS | {"NOISE"}) > 0:
                return None
            local = beats[(beats >= t0 - 1) & (beats <= t0 + span + 1)]
            rr = np.diff(local)
            rr = rr[(rr > 0.2) & (rr < 3.0)]
            if len(rr) < 3:
                return None
            hr = 60.0 / np.median(rr)
            cv = float(np.std(rr) / np.mean(rr))
            irregular = cv > 0.25 or overlap(rec, t0, t0 + span, {"AFIB", "AFL"}) >= 0.5
            return t0, hr, irregular

        for _ in range(n_real):  # 실제 심박 그대로
            w = beat_window(1.0)
            if w is None:
                continue
            t0, hr, irregular = w
            x, noisy = corrupt(rng, segment(t0, W), bank)
            out.append((model_input(x, FS), hr_label(hr, irregular), hr, noisy, False))

        for _ in range(n_scaled):  # 시간축 변환으로 서맥·빈맥 생성
            band = rng.integers(3)
            target = (rng.uniform(52, 118) if band == 0 else
                      rng.choice([rng.uniform(41, 50), rng.uniform(120, 149)]) if band == 1 else
                      rng.choice([rng.uniform(28, 40), rng.uniform(150, 220)]))
            probe = beat_window(1.0)
            if probe is None:
                continue
            scale = target / probe[1]
            if not 0.4 <= scale <= 2.6:
                continue
            w = beat_window(scale)
            if w is None:
                continue
            t0, hr_src, irregular = w
            seg = segment(t0, W * scale)
            n = int(W * FS)
            seg = np.interp(np.linspace(0, len(seg) - 1, n), np.arange(len(seg)), seg)
            hr = hr_src * scale
            x, noisy = corrupt(rng, seg, bank)
            out.append((model_input(x, FS), hr_label(hr, irregular), hr, noisy, True))

    for _ in range(n_unavail):  # 측정 불가
        t0 = rng.uniform(0, dur - W - 1)
        seg = segment(t0, W)
        out.append((model_input(unavailable_window(rng, seg, bank), FS), UNAVAILABLE, np.nan, True, False))

    if not out:
        return None
    x = np.stack([o[0] for o in out]).astype(np.float16)
    return dict(x=x, y=np.array([o[1] for o in out], np.int8),
                hr=np.array([o[2] for o in out], np.float32),
                noisy=np.array([o[3] for o in out]), scaled=np.array([o[4] for o in out]),
                rec=np.array([f"{db}_{rec_id}"] * len(out)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=10)
    args = ap.parse_args()
    os.makedirs(os.path.join(args.out, "cache"), exist_ok=True)

    t0 = time.time()
    jobs = [("nstdb", n) for n in ("em", "ma", "bw")]
    jobs += [(db, r) for s in SPLITS.values() for db, recs in s.items() for r in recs]
    failed = []
    with ThreadPoolExecutor(8) as pool:
        futs = {pool.submit(download, args.out, db, r): (db, r) for db, r in jobs}
        for f, key in futs.items():
            try:
                f.result()
            except Exception as e:
                failed.append(key)
                print("다운로드 실패", key, repr(e), flush=True)
    print(f"다운로드 완료 {len(jobs) - len(failed)}/{len(jobs)}개, {time.time() - t0:.0f}s", flush=True)

    bad = set(failed)
    for split, by_db in SPLITS.items():
        tasks = [(args.out, split, db, r, int.from_bytes(f"{split}{db}{r}".encode(), "little") % (2 ** 31))
                 for db, recs in by_db.items() for r in recs if (db, r) not in bad]
        with ProcessPoolExecutor(args.workers) as pool:
            parts = [p for p in pool.map(make_record, tasks) if p is not None]
        merged = {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}
        np.savez(os.path.join(args.out, f"{split}.npz"), **merged)
        counts = np.bincount(merged["y"], minlength=4)
        print(f"{split}: {len(merged['y'])}개 normal={counts[0]} caution={counts[1]} "
              f"danger={counts[2]} unavailable={counts[3]} ({time.time() - t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
