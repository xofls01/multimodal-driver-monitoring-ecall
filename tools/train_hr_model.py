"""HeartNet 학습. 검증셋 macro-F1이 가장 좋은 가중치를 저장한다. 테스트셋은 쓰지 않는다.

실행: python tools/train_hr_model.py --data <데이터 폴더> --out models/hr_classifier.pt
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import torch
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

from biosignal.hr_model import CLASSES, HR_SCALE, HeartNet


def load(path):
    d = np.load(path)
    x = torch.from_numpy(d["x"].astype(np.float32))[:, None]
    y = torch.from_numpy(d["y"].astype(np.int64))
    hr = torch.from_numpy(d["hr"].astype(np.float32))
    return x, y, hr


@torch.no_grad()  # inference_mode 텐서는 온도 보정의 역전파에 쓸 수 없다
def predict(net, x, batch=1024):
    net.eval()
    logits, hrs = [], []
    for i in range(0, len(x), batch):
        lo, h = net(x[i:i + batch])
        logits.append(lo)
        hrs.append(h)
    return torch.cat(logits), torch.cat(hrs)


def macro_f1(y, p, n=len(CLASSES)):
    scores = []
    for c in range(n):
        tp = ((p == c) & (y == c)).sum().item()
        fp = ((p == c) & (y != c)).sum().item()
        fn = ((p != c) & (y == c)).sum().item()
        scores.append(2 * tp / max(2 * tp + fp + fn, 1))
    return float(np.mean(scores)), scores


def fit_temperature(logits, y):
    """신뢰도가 실제 정답률과 맞도록 검증셋에서 온도 값을 보정한다."""
    t = torch.nn.Parameter(torch.ones(1))
    opt = torch.optim.LBFGS([t], lr=0.05, max_iter=100)
    loss_fn = nn.CrossEntropyLoss()

    def closure():
        opt.zero_grad()
        loss = loss_fn(logits / t.clamp(min=0.05), y)
        loss.backward()
        return loss

    opt.step(closure)
    return float(t.detach().clamp(min=0.05))


def save(path, state, temperature, val_f1):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    torch.save({"state_dict": state, "temperature": temperature,
                "classes": list(CLASSES), "val_macro_f1": val_f1}, tmp)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--epochs", type=int, default=25)
    ap.add_argument("--batch", type=int, default=256)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    xtr, ytr, hrtr = load(os.path.join(args.data, "train.npz"))
    xva, yva, hrva = load(os.path.join(args.data, "val.npz"))
    counts = torch.bincount(ytr, minlength=len(CLASSES)).float()
    weights = (counts.sum() / (len(CLASSES) * counts)).clamp(max=4.0)
    print(f"train {len(ytr)} val {len(yva)} counts {counts.tolist()} weights {weights.tolist()}", flush=True)

    net = HeartNet()
    print("파라미터 수", sum(p.numel() for p in net.parameters()), flush=True)
    loader = DataLoader(TensorDataset(xtr, ytr, hrtr), batch_size=args.batch, shuffle=True, drop_last=True)
    opt = torch.optim.AdamW(net.parameters(), lr=1e-3, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=2e-3, epochs=args.epochs,
                                                steps_per_epoch=len(loader))
    ce = nn.CrossEntropyLoss(weight=weights, label_smoothing=0.05)
    huber = nn.SmoothL1Loss()

    best, best_state = -1.0, None
    t0 = time.time()
    for epoch in range(args.epochs):
        net.train()
        total = 0.0
        for xb, yb, hb in loader:
            # 시간 이동과 진폭 변화 증강
            shift = int(torch.randint(-60, 61, (1,)))
            xb = torch.roll(xb, shift, dims=-1) * (0.8 + 0.4 * torch.rand(xb.size(0), 1, 1))
            logits, hr = net(xb)
            loss = ce(logits, yb)
            mask = ~torch.isnan(hb)
            if mask.any():
                loss = loss + 0.5 * huber(hr[mask], hb[mask] / HR_SCALE)
            opt.zero_grad()
            loss.backward()
            nn.utils.clip_grad_norm_(net.parameters(), 5.0)
            opt.step()
            sched.step()
            total += loss.item()
        logits, hr = predict(net, xva)
        f1, per = macro_f1(yva, logits.argmax(1))
        mask = ~torch.isnan(hrva)
        mae = (hr[mask] * HR_SCALE - hrva[mask]).abs().mean().item()
        print(f"epoch {epoch + 1:>2}/{args.epochs} loss {total / len(loader):.3f} val_macroF1 {f1:.3f} "
              f"per {[round(s, 3) for s in per]} val_HR_MAE {mae:.2f} ({time.time() - t0:.0f}s)", flush=True)
        if f1 > best:
            best = f1
            best_state = {k: v.clone() for k, v in net.state_dict().items()}
            # 중간에 멈춰도 결과가 남도록 최고 성능이 나올 때마다 저장한다
            save(args.out, best_state, 1.0, best)

    net.load_state_dict(best_state)
    logits, _ = predict(net, xva)
    temperature = fit_temperature(logits, yva)
    save(args.out, best_state, temperature, best)
    print(f"저장: {args.out} best_val_macroF1={best:.3f} temperature={temperature:.3f}", flush=True)


if __name__ == "__main__":
    main()
