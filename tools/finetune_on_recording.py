"""측정 기록으로 ECG·PPG 모델의 마지막 판정층만 미세조정한다. 기존 모델 파일은 덮어쓰지 않는다.

실행:
python tools/finetune_on_recording.py "<측정 폴더>" --ecg-data <ECG 데이터> --ppg-data <PPG 데이터> --out-dir models

- 앞부분(ECG 특징층, PaPaGei)은 고정하고, 공개 데이터와 측정 파형을 섞어 판정층만 학습한다.
- 주의·위험 예시는 측정 파형의 시간축을 변환해 만든다. 라벨은 ECG R 피크로 잰 심박수에서 계산한다.
- 먼저 앞 12초로 학습해 뒤 10초에서 확인하고(2초 겹침), 최종 모델은 20초 전체로 학습한다.
"""
import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import torch
from torch import nn

from biosignal.dsp import MODEL_WINDOW_S, detect_r_peaks, model_input, rr_intervals
from biosignal.hr_model import CLASSES, HeartNet
from biosignal.ppg_model import load_encoder, make_head, ppg_model_input

FS = 250.0
W = MODEL_WINDOW_S
N_REAL = 3000


def load_recording(folder):
    with open(os.path.join(folder, "biosignals.csv"), encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    t = np.array([float(r["clip_s"]) for r in rows])
    grid = np.arange(t[0], t[-1], 1 / FS)
    return (np.interp(grid, t, [float(r["ecg_raw"]) for r in rows]),
            np.interp(grid, t, [float(r["ppg_raw"]) for r in rows]))


def hr_label(hr):
    if hr <= 40 or hr >= 150:
        return 2
    return 1 if (hr <= 50 or hr >= 120) else 0


def real_windows(ecg, ppg, n, seed):
    """측정 구간에서 창을 뽑는다. 등급마다 비슷한 수가 되도록 목표 심박을 고른다."""
    rng = np.random.default_rng(seed)
    dur = len(ecg) / FS
    n_out = int(W * FS)
    xe, xp, ys = [], [], []
    while len(ys) < n:
        band = rng.integers(3)
        target = (rng.uniform(55, 115) if band == 0 else
                  rng.choice([rng.uniform(41, 50), rng.uniform(120, 145)]) if band == 1 else
                  rng.uniform(28, 40))
        probe = ecg[: int(W * FS)]
        peaks, _, _ = detect_r_peaks(probe, FS)
        base = 60.0 / np.median(rr_intervals(peaks / FS))
        scale = target / base
        if W * scale >= dur - 0.2:
            continue
        t0 = rng.uniform(0.3 if dur > W * scale + 0.4 else 0.0, dur - W * scale - 0.1)
        i0, m = int(t0 * FS), int(W * scale * FS)
        seg_e, seg_p = ecg[i0:i0 + m], ppg[i0:i0 + m]
        peaks, _, _ = detect_r_peaks(seg_e, FS)
        rr = rr_intervals(peaks / FS)
        if len(rr) < 3:
            continue
        hr = 60.0 / np.median(rr) * scale
        idx = np.linspace(0, m - 1, n_out)
        e = np.interp(idx, np.arange(m), seg_e)
        p = np.interp(idx, np.arange(m), seg_p)
        e = e + rng.uniform(0, 8) * rng.normal(size=n_out)
        p = p + rng.uniform(0, 4) * rng.normal(size=n_out)
        xe.append(model_input(e, FS))
        xp.append(ppg_model_input(p, FS))
        ys.append(hr_label(hr))
    return np.stack(xe), np.stack(xp), np.array(ys)


@torch.no_grad()
def ecg_features(net, x):
    out = []
    for i in range(0, len(x), 512):
        z = net.features(torch.from_numpy(x[i:i + 512].astype(np.float32))[:, None])
        out.append(torch.cat([z.mean(dim=-1), z.amax(dim=-1)], dim=1))
    return torch.cat(out)


@torch.no_grad()
def ppg_features(enc, x, mean, std):
    out = []
    for i in range(0, len(x), 256):
        out.append((enc(torch.from_numpy(x[i:i + 256].astype(np.float32))[:, None]) - mean) / std)
    return torch.cat(out)


def train_head(head, public, real, epochs=12, lr=3e-4, seed=0):
    """매 묶음에 공개 데이터 192개와 측정 파형 64개를 섞어 학습한다."""
    torch.manual_seed(seed)
    zp, yp = public
    zr, yr = real
    counts = torch.bincount(yp, minlength=4).float()
    ce = nn.CrossEntropyLoss(weight=(counts.sum() / (4 * counts)).clamp(max=4.0), label_smoothing=0.05)
    opt = torch.optim.AdamW(head.parameters(), lr=lr, weight_decay=1e-3)
    for _ in range(epochs):
        head.train()
        perm = torch.randperm(len(zp))
        for i in range(0, len(perm) - 192, 192):
            b = perm[i:i + 192]
            r = torch.randint(0, len(zr), (64,))
            loss = ce(head(torch.cat([zp[b], zr[r]])), torch.cat([yp[b], yr[r]]))
            opt.zero_grad()
            loss.backward()
            opt.step()
    head.eval()
    return head


def temperature(logits, y):
    t = nn.Parameter(torch.ones(1))
    lb = torch.optim.LBFGS([t], lr=0.05, max_iter=100)

    def closure():
        lb.zero_grad()
        loss = nn.functional.cross_entropy(logits / t.clamp(min=0.05), y)
        loss.backward()
        return loss

    lb.step(closure)
    return float(t.detach().clamp(min=0.05))


@torch.no_grad()
def summary(head, z, y, T):
    p = torch.softmax(head(z) / T, 1)
    pred = p.argmax(1)
    n = y == 0
    rec = lambda c: float((pred[y == c] == c).float().mean()) if (y == c).any() else float("nan")
    return (f"정확도 {float((pred == y).float().mean()):.3f} | 재현율 정상 {rec(0):.3f} 주의 {rec(1):.3f} "
            f"위험 {rec(2):.3f} 측정불가 {rec(3):.3f} | 정상→위험 {float((pred[n] == 2).float().mean()):.4f} "
            f"정상→주의 {float((pred[n] == 1).float().mean()):.4f}")


@torch.no_grad()
def holdout(name, head, z, T):
    p = torch.softmax(head(z) / T, 1)[0]
    print(f"  {name}: " + " ".join(f"{c}={v:.2f}" for c, v in zip(CLASSES, p.tolist())), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--ecg-data", required=True)
    ap.add_argument("--ppg-data", required=True)
    ap.add_argument("--out-dir", default="models")
    args = ap.parse_args()
    join = os.path.join

    ecg, ppg = load_recording(args.folder)
    print(f"측정 기록 {len(ecg) / FS:.1f}초", flush=True)

    ck_e = torch.load(join(args.out_dir, "hr_classifier.pt"), map_location="cpu", weights_only=True)
    net = HeartNet()
    net.load_state_dict(ck_e["state_dict"])
    net.eval()
    ck_p = torch.load(join(args.out_dir, "ppg_head.pt"), map_location="cpu", weights_only=True)
    enc = load_encoder(join(args.out_dir, "papagei_s.pt"))
    mean, std = ck_p["mean"], ck_p["std"]

    print("공개 데이터 특징 추출", flush=True)
    data = {}
    for s in ("train", "val", "test"):
        d = np.load(join(args.ecg_data, f"{s}.npz"))
        data[("ecg", s)] = (ecg_features(net, d["x"]), torch.from_numpy(d["y"].astype(np.int64)))
        d = np.load(join(args.ppg_data, f"{s}.npz"))
        z = torch.from_numpy(np.load(join(args.ppg_data, f"{s}_emb.npy")))
        data[("ppg", s)] = ((z - mean) / std, torch.from_numpy(d["y"].astype(np.int64)))

    def fresh_ecg_head():
        h = nn.Linear(128, 4)
        h.weight.data.copy_(ck_e["state_dict"]["cls_head.weight"])
        h.bias.data.copy_(ck_e["state_dict"]["cls_head.bias"])
        return h.eval()

    def fresh_ppg_head():
        h = make_head()
        h.load_state_dict(ck_p["state_dict"])
        return h.eval()

    T_e0, T_p0 = ck_e["temperature"], ck_p["temperature"]
    print("\n[기존 모델: 공개 테스트셋]", flush=True)
    print("  ECG", summary(fresh_ecg_head(), *data[("ecg", "test")], T_e0), flush=True)
    print("  PPG", summary(fresh_ppg_head(), *data[("ppg", "test")], T_p0), flush=True)

    # 1) 앞 12초로 학습해 뒤 10초에서 확인
    cut = int(12 * FS)
    he, hp = ecg[-int(W * FS):], ppg[-int(W * FS):]
    he_z = ecg_features(net, model_input(he, FS)[None])
    hp_z = ppg_features(enc, ppg_model_input(hp, FS)[None], mean, std)
    print("\n[뒤 10초 확인, 정답은 normal]", flush=True)
    holdout("ECG 기존", fresh_ecg_head(), he_z, T_e0)
    holdout("PPG 기존", fresh_ppg_head(), hp_z, T_p0)
    xe, xp, y = real_windows(ecg[:cut], ppg[:cut], N_REAL, seed=1)
    print("  앞 12초 합성 등급 분포", np.bincount(y, minlength=3).tolist(), flush=True)
    ry = torch.from_numpy(y.astype(np.int64))
    h = train_head(fresh_ecg_head(), data[("ecg", "train")], (ecg_features(net, xe), ry))
    holdout("ECG 미세조정", h, he_z, T_e0)
    h = train_head(fresh_ppg_head(), data[("ppg", "train")], (ppg_features(enc, xp, mean, std), ry))
    holdout("PPG 미세조정", h, hp_z, T_p0)

    # 2) 20초 전체로 최종 학습
    xe, xp, y = real_windows(ecg, ppg, N_REAL, seed=2)
    print("\n전체 20초 합성 등급 분포", np.bincount(y, minlength=3).tolist(), flush=True)
    ry = torch.from_numpy(y.astype(np.int64))
    he_final = train_head(fresh_ecg_head(), data[("ecg", "train")], (ecg_features(net, xe), ry))
    hp_final = train_head(fresh_ppg_head(), data[("ppg", "train")], (ppg_features(enc, xp, mean, std), ry))

    with torch.no_grad():
        T_e = temperature(he_final(data[("ecg", "val")][0]), data[("ecg", "val")][1])
        T_p = temperature(hp_final(data[("ppg", "val")][0]), data[("ppg", "val")][1])
    print("\n[미세조정 모델: 공개 테스트셋]", flush=True)
    print("  ECG", summary(he_final, *data[("ecg", "test")], T_e), flush=True)
    print("  PPG", summary(hp_final, *data[("ppg", "test")], T_p), flush=True)

    state = {k: v.clone() for k, v in ck_e["state_dict"].items()}
    state["cls_head.weight"] = he_final.weight.detach().clone()
    state["cls_head.bias"] = he_final.bias.detach().clone()
    torch.save({"state_dict": state, "temperature": T_e, "classes": list(CLASSES)},
               join(args.out_dir, "hr_classifier_ft.pt"))
    torch.save({"state_dict": hp_final.state_dict(), "temperature": T_p, "mean": mean, "std": std,
                "classes": list(CLASSES)}, join(args.out_dir, "ppg_head_ft.pt"))
    print("저장: hr_classifier_ft.pt, ppg_head_ft.pt", flush=True)


if __name__ == "__main__":
    main()
