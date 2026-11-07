"""PaPaGei-S 특징 위에 등급 판정부를 학습하고, 테스트셋에서 피크+임계값 규칙과 비교한다.

인코더는 고정한다. 실행:
python tools/train_ppg_head.py --data <데이터 폴더> --encoder models/papagei_s.pt --out models/ppg_head.pt
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import torch
from torch import nn

from biosignal.dsp import MODEL_FS, beat_sqi, detect_pulse_peaks, rr_intervals
from biosignal.hr_model import CLASSES
from biosignal.ppg_model import load_encoder, make_head


def embed(encoder, x, cache):
    if os.path.exists(cache):
        return torch.from_numpy(np.load(cache))
    out = []
    with torch.no_grad():
        for i in range(0, len(x), 256):
            out.append(encoder(torch.from_numpy(x[i:i + 256].astype(np.float32))[:, None]))
    z = torch.cat(out)
    np.save(cache, z.numpy())
    return z


def rule(x):
    peaks, _, _ = detect_pulse_peaks(x, MODEL_FS)
    if beat_sqi(x, peaks, MODEL_FS, "ppg") < 0.5:
        return 3
    rr = rr_intervals(peaks / MODEL_FS)
    if len(rr) < 2:
        return 3
    hr = 60.0 / np.median(rr)
    return 2 if (hr <= 40 or hr >= 150) else 1 if (hr <= 50 or hr >= 120) else 0


def report(name, y, p):
    print(f"\n=== {name} (n={len(y)}) ===")
    print("실제\\예측     " + "".join(f"{c:>12}" for c in CLASSES))
    for i, c in enumerate(CLASSES):
        print(f"{c:<13}" + "".join(f"{((y == i) & (p == j)).sum():>12}" for j in range(4)))
    f1s = []
    for i, c in enumerate(CLASSES):
        if not (y == i).any():
            continue
        tp = ((y == i) & (p == i)).sum()
        pr, rc = tp / max((p == i).sum(), 1), tp / (y == i).sum()
        f1s.append(2 * pr * rc / max(pr + rc, 1e-9))
        print(f"  {c:<12} 정밀도 {pr:.3f}  재현율 {rc:.3f}")
    print(f"  정확도 {np.mean(y == p):.3f}  macro-F1 {np.mean(f1s):.3f}")
    if (y == 0).any():
        print(f"  정상→위험 {np.mean(p[y == 0] == 2):.4f}  정상→주의 {np.mean(p[y == 0] == 1):.4f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--encoder", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--epochs", type=int, default=60)
    args = ap.parse_args()
    torch.manual_seed(0)
    encoder = load_encoder(args.encoder)

    sets = {}
    for s in ("train", "val", "test"):
        d = np.load(os.path.join(args.data, f"{s}.npz"))
        t0 = time.time()
        z = embed(encoder, d["x"], os.path.join(args.data, f"{s}_emb.npy"))
        print(s, len(z), f"특징 추출 {time.time() - t0:.0f}s", flush=True)
        sets[s] = (z, torch.from_numpy(d["y"].astype(np.int64)), d)

    ztr, ytr, _ = sets["train"]
    mean, std = ztr.mean(0), ztr.std(0).clamp(min=1e-6)
    norm = lambda z: (z - mean) / std
    counts = torch.bincount(ytr, minlength=4).float()
    weights = (counts.sum() / (4 * counts)).clamp(max=4.0)
    head = make_head()
    opt = torch.optim.AdamW(head.parameters(), lr=1e-3, weight_decay=1e-3)
    ce = nn.CrossEntropyLoss(weight=weights, label_smoothing=0.05)
    Ztr, Zva, yva = norm(ztr), norm(sets["val"][0]), sets["val"][1]
    best, best_state = -1.0, None
    for ep in range(args.epochs):
        head.train()
        perm = torch.randperm(len(Ztr))
        for i in range(0, len(perm), 256):
            b = perm[i:i + 256]
            opt.zero_grad()
            ce(head(Ztr[b]), ytr[b]).backward()
            opt.step()
        head.eval()
        with torch.no_grad():
            p = head(Zva).argmax(1)
        f1 = []
        for c in range(4):
            tp = ((p == c) & (yva == c)).sum().item()
            f1.append(2 * tp / max(2 * tp + ((p == c) & (yva != c)).sum().item() + ((p != c) & (yva == c)).sum().item(), 1))
        f = float(np.mean(f1))
        if f > best:
            best, best_state = f, {k: v.clone() for k, v in head.state_dict().items()}
        if (ep + 1) % 10 == 0:
            print(f"epoch {ep + 1} val_macroF1 {f:.3f} best {best:.3f}", flush=True)

    head.load_state_dict(best_state)
    head.eval()
    with torch.no_grad():
        lva = head(Zva)
    t = nn.Parameter(torch.ones(1))
    lbfgs = torch.optim.LBFGS([t], lr=0.05, max_iter=100)

    def closure():
        lbfgs.zero_grad()
        loss = nn.functional.cross_entropy(lva / t.clamp(min=0.05), yva)
        loss.backward()
        return loss

    lbfgs.step(closure)
    temperature = float(t.detach().clamp(min=0.05))
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    torch.save({"state_dict": best_state, "temperature": temperature, "mean": mean, "std": std,
                "classes": list(CLASSES), "val_macro_f1": best}, args.out)
    print(f"저장 {args.out} val_macroF1={best:.3f} temperature={temperature:.3f}", flush=True)

    zte, yte, d = sets["test"]
    with torch.no_grad():
        P = torch.softmax(head(norm(zte)) / temperature, 1).numpy()
    y = yte.numpy()
    pm, conf = P.argmax(1), P.max(1)
    pr = np.array([rule(w) for w in d["x"].astype(float)])
    sc, rec = d["scaled"], d["rec"]
    for name, m in (("전체", np.ones(len(y), bool)), ("실제 심박", ~sc), ("시간축 변환", sc)):
        report(f"PaPaGei+판정부 | {name}", y[m], pm[m])
        report(f"피크+임계값 규칙 | {name}", y[m], pr[m])
    for th in (0.7, 0.9):
        p = pm.copy()
        p[conf < th] = 3
        report(f"PaPaGei 신뢰도 {th} 미만은 측정 불가 | 전체", y, p)
    print("\n환자별 등급 정확도 (실제 심박, 측정 불가 제외)")
    rows = []
    for r in np.unique(rec):
        m = (rec == r) & ~sc & (y != 3)
        if m.sum() >= 20:
            rows.append((float(np.mean(y[m] == pm[m])), float(np.mean(y[m] == pr[m])), str(r), int(m.sum())))
    for a, b, r, n in sorted(rows):
        print(f"  {r} n={n:<4} 모델 {a:.3f} 규칙 {b:.3f}")


if __name__ == "__main__":
    main()
