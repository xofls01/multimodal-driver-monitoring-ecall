"""모든 합성 시나리오를 돌려 판정 결과와 지연 시간을 표로 출력한다.

실행: python tools/run_scenarios.py [--model models/hr_classifier.pt]
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np

from biosignal import BioSignalMonitor, Config
from biosignal import simulate as sim


PPG_ENCODER = PPG_HEAD = None


def run(name, seed=0, model=None):
    s = sim.SCENARIOS[name](seed=seed)
    mon = BioSignalMonitor(Config(), model_path=model, ppg_encoder_path=PPG_ENCODER, ppg_head_path=PPG_HEAD)
    rows, costs = [], []
    gen = sim.replay(mon, s)
    while True:
        t0 = time.perf_counter()
        st = next(gen, None)
        costs.append(time.perf_counter() - t0)
        if st is None:
            return s, rows, costs
        rows.append(st)


def first_after(rows, onset, level):
    for st in rows:
        if st.t >= onset and st.level == level:
            return st.t - onset
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model")
    ap.add_argument("--ppg-encoder")
    ap.add_argument("--ppg-head")
    ap.add_argument("--seeds", type=int, default=5)
    args = ap.parse_args()
    global PPG_ENCODER, PPG_HEAD
    PPG_ENCODER, PPG_HEAD = args.ppg_encoder, args.ppg_head

    print(f"판정 방식: {'학습 모델 ' + args.model if args.model else '규칙 기반'}")
    print(f"{'시나리오':<26}{'발병 전 최고':<14}{'최종 level':<13}{'hr_class':<13}{'주의':<7}{'위험':<7}사유")
    for name in sim.SCENARIOS:
        s, rows, _ = run(name, model=args.model)
        onset = s.onset if s.onset is not None else 0.0
        before = {st.level for st in rows if 30 < st.t < onset}
        worst = next((v for v in ("danger", "caution", "normal", "unavailable") if v in before), "-")
        c, d = first_after(rows, onset, "caution"), first_after(rows, onset, "danger")
        last = rows[-1]
        print(f"{name:<26}{worst:<14}{last.level:<13}{last.hr_class:<13}"
              f"{'-' if c is None else f'{c:.0f}s':<7}{'-' if d is None else f'{d:.0f}s':<7}"
              f"{','.join(last.reasons)}", flush=True)

    print(f"\n[오경보] 시드 {args.seeds}개, 30초 이후 전체 구간의 level 횟수")
    for name in ("normal", "driving_vibration", "electrode_off", "ecg_motion_artifact"):
        counts = {"normal": 0, "caution": 0, "danger": 0, "unavailable": 0}
        for seed in range(args.seeds):
            _, rows, _ = run(name, seed, args.model)
            for st in rows:
                if st.t > 30:
                    counts[st.level] += 1
        print(f"  {name:<22}{counts}", flush=True)

    print("\n[심박수 오차] 30초 이후. 요구: ≤ 5 bpm")
    for name in ("normal", "driving_vibration"):
        errs = []
        for seed in range(args.seeds):
            s, rows, _ = run(name, seed, args.model)
            errs += [abs(st.hr - s.true_hr(st.t)) for st in rows if st.t > 30 and st.hr]
        print(f"  {name:<20} 평균 {np.mean(errs):.2f}, 최대 {np.max(errs):.2f} bpm", flush=True)

    _, _, costs = run("normal", model=args.model)
    print(f"\n[처리 시간] update 1회 평균 {1000 * np.mean(costs):.1f} ms, "
          f"최대 {1000 * np.max(costs):.1f} ms (이 PC 기준, 라즈베리파이는 별도 측정 필요)")


if __name__ == "__main__":
    main()
