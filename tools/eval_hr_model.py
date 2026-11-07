"""학습에 쓰지 않은 환자의 테스트셋으로 모델과 규칙 기반 판정을 비교한다.

실행: python tools/eval_hr_model.py --data <데이터 폴더> --model models/hr_classifier.pt
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import torch

from biosignal.dsp import MODEL_FS, beat_sqi, detect_r_peaks, rr_intervals
from biosignal.hr_model import CLASSES, HR_SCALE, HeartNet

N, C, D, U = 0, 1, 2, 3


def rule_predict(x):
    """같은 입력에 대한 규칙 기반 판정: 피크 검출 → 품질 → 심박수 임계값."""
    peaks, _, _ = detect_r_peaks(x, MODEL_FS)
    if beat_sqi(x, peaks, MODEL_FS, "ecg") < 0.5 or len(peaks) < 3:
        return U, np.nan
    rr = rr_intervals(peaks / MODEL_FS)
    if len(rr) < 2:
        return U, np.nan
    hr = 60.0 / np.median(rr)
    if hr <= 40 or hr >= 150:
        return D, hr
    if hr <= 50 or hr >= 120:
        return C, hr
    return N, hr


def report(name, y, p, hr_true, hr_pred):
    print(f"\n=== {name} (n={len(y)}) ===")
    print("실제\\예측     " + "".join(f"{c:>12}" for c in CLASSES))
    for i, c in enumerate(CLASSES):
        print(f"{c:<13}" + "".join(f"{((y == i) & (p == j)).sum():>12}" for j in range(4)))
    f1s = []
    for i, c in enumerate(CLASSES):
        tp = ((y == i) & (p == i)).sum()
        prec = tp / max((p == i).sum(), 1)
        rec = tp / max((y == i).sum(), 1)
        f1 = 2 * prec * rec / max(prec + rec, 1e-9)
        if (y == i).sum():
            f1s.append(f1)
        print(f"  {c:<12} 정밀도 {prec:.3f}  재현율 {rec:.3f}  F1 {f1:.3f}")
    print(f"  정확도 {np.mean(y == p):.3f}  macro-F1 {np.mean(f1s):.3f}")
    n_mask = y == N
    if n_mask.any():
        print(f"  실제 정상을 위험으로 본 비율(오경보) {np.mean(p[n_mask] == D):.4f}")
    m = ~np.isnan(hr_true) & ~np.isnan(hr_pred)
    if m.any():
        e = np.abs(hr_true[m] - hr_pred[m])
        cover = m.sum() / max((~np.isnan(hr_true)).sum(), 1)
        print(f"  심박수 오차 평균 {e.mean():.2f}  P95 {np.percentile(e, 95):.2f} bpm  "
              f"≤5bpm {np.mean(e <= 5):.3f}  출력 비율 {cover:.3f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--model", required=True)
    args = ap.parse_args()

    d = np.load(os.path.join(args.data, "test.npz"))
    x = d["x"].astype(np.float32)
    y = d["y"].astype(int)
    hr = d["hr"].astype(float)
    noisy, scaled, rec = d["noisy"], d["scaled"], d["rec"]

    ckpt = torch.load(args.model, map_location="cpu", weights_only=True)
    net = HeartNet()
    net.load_state_dict(ckpt["state_dict"])
    net.eval()
    logits, hrs = [], []
    with torch.inference_mode():
        for i in range(0, len(x), 1024):
            lo, h = net(torch.from_numpy(x[i:i + 1024])[:, None])
            logits.append(lo)
            hrs.append(h)
    pm = torch.cat(logits).argmax(1).numpy()
    hm = torch.cat(hrs).numpy().astype(float) * HR_SCALE
    hm[pm == U] = np.nan

    rule = [rule_predict(w) for w in x.astype(float)]
    pr = np.array([r[0] for r in rule])
    hr_r = np.array([r[1] for r in rule])

    subsets = {
        "전체": np.ones(len(y), bool),
        "실제 심박 그대로(변환 없음)": ~scaled,
        "실제 심박 + 잡음 없음": ~scaled & ~noisy,
        "실제 심박 + 잡음 있음": ~scaled & noisy,
        "시간축 변환으로 만든 서맥·빈맥": scaled,
    }
    for name, m in subsets.items():
        report(f"모델 | {name}", y[m], pm[m], hr[m], hm[m])
        report(f"규칙 | {name}", y[m], pr[m], hr[m], hr_r[m])

    print("\n=== 실제 VF/VT 창의 위험 검출률 (데이터베이스별) ===")
    for db in ("mitdb", "vfdb", "cudb"):
        m = (y == D) & ~scaled & np.isnan(hr) & np.char.startswith(rec.astype(str), db)
        if m.any():
            print(f"  {db:<6} n={m.sum():<6} 모델 {np.mean(pm[m] == D):.3f}  규칙 {np.mean(pr[m] == D):.3f}")

    print("\n=== 환자별 정상 오경보 상위 5개 (모델) ===")
    rates = []
    for r in np.unique(rec):
        m = (rec == r) & (y == N) & ~scaled
        if m.sum() >= 20:
            rates.append((float(np.mean(pm[m] == D)), str(r), int(m.sum())))
    for rate, r, n in sorted(rates, reverse=True)[:5]:
        print(f"  {r:<14} {rate:.3f} (n={n})")

    t0 = time.perf_counter()
    torch.set_num_threads(1)
    with torch.inference_mode():
        for i in range(100):
            net(torch.from_numpy(x[i:i + 1])[:, None])
    print(f"\n추론 시간(CPU 1스레드, 이 PC): {(time.perf_counter() - t0) * 10:.2f} ms/창")
    print(f"파라미터 수 {sum(p.numel() for p in net.parameters())}, "
          f"파일 크기 {os.path.getsize(args.model) / 1024:.0f} KB")


if __name__ == "__main__":
    main()
