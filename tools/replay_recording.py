"""측정 폴더(biosignals.csv, vitals.csv)를 실시간과 같은 순서로 알고리즘에 넣어 결과를 본다.

실행: python tools/replay_recording.py "<측정 폴더>" [--model models/hr_classifier.pt] [--expect normal]
결과는 측정 폴더를 건드리지 않고 --out 경로(기본: bio_status.jsonl)에 저장한다.

예열 (--warmup, 기본 self)
  self     파일 앞 3초를 이어 붙여 20초 예열한 뒤, 파일 첫 시각(영상 0초)부터 판정한다. 녹화 영상과 합칠 때 쓴다.
  none     예열 없음. 실시간과 같이 처음 10초를 기준선 측정에 쓴다. 장비 기록 점검용.
  <폴더>   같은 사람의 정상 측정 폴더 전체로 예열한다.
자세한 내용은 biosignal/offline.py.
"""
import argparse
import csv
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np

from biosignal import Config
from biosignal.offline import assess_recording


def read_csv(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--model")
    ap.add_argument("--ppg-encoder")
    ap.add_argument("--ppg-head")
    ap.add_argument("--out", default="bio_status.jsonl")
    ap.add_argument("--expect", choices=["normal", "caution", "danger"])
    ap.add_argument("--warmup", default="self",
                    help="self(기본, 파일 앞 3초로 예열) / none(예열 없음, 실시간과 같음) / 정상 측정 폴더 경로")
    args = ap.parse_args()

    vitals_path = os.path.join(args.folder, "vitals.csv")
    vitals = read_csv(vitals_path) if os.path.exists(vitals_path) else []
    nano_t = np.array([float(v["clip_s"]) for v in vitals])
    nano_hr = np.array([float(v["hr_bpm"]) for v in vitals])

    warmup = None if args.warmup.lower() == "none" else args.warmup
    statuses = assess_recording(args.folder, warmup=warmup, cfg=Config(), log_path=args.out,
                                model_path=args.model, ppg_encoder_path=args.ppg_encoder,
                                ppg_head_path=args.ppg_head)
    print({"self": "예열: 파일 앞 3초를 이어 붙여 먼저 넣음", None: "예열 없음 (처음 10초 기준선 측정)"}
          .get(warmup, f"예열: {warmup} 전체를 먼저 넣음") + "\n")
    print(f"{'시각':>5} {'level':<12}{'bpm':>6} {'hr_class':<12}{'신뢰도':>6} {'근거':<10}"
          f"{'ECG':>6}{'품질':>6}{'PPG':>7}{'품질':>6}{'Nano':>7}  사유")

    levels, classes, diffs_e, diffs_p = Counter(), Counter(), [], []
    fmt = lambda v: "-" if v is None else f"{v:.1f}"
    for st in statuses:
        levels[st.level] += 1
        classes[st.hr_class] += 1
        e, p = st.channels["ecg"], st.channels["ppg"]
        nano = nano_hr[np.argmin(np.abs(nano_t - st.t))] if len(nano_t) else float("nan")
        if e["hr"] and nano > 0 and e["reliable"]:
            diffs_e.append(abs(e["hr"] - nano))
        if p["hr"] and nano > 0 and p["reliable"]:
            diffs_p.append(abs(p["hr"] - nano))
        conf = "-" if st.hr_class_confidence is None else f"{st.hr_class_confidence:.2f}"
        print(f"{st.t:5.1f} {st.level:<12}{fmt(st.hr):>6} {st.hr_class:<12}{conf:>6} {st.hr_class_source:<10}"
              f"{fmt(e['hr']):>6}{e['sqi']:>6.2f}{fmt(p['hr']):>7}{p['sqi']:>6.2f}{nano:>7.1f}  "
              f"{','.join(st.reasons)}")

    print("\nlevel 횟수:", dict(levels))
    print("hr_class 횟수:", dict(classes))
    for name, d in (("ECG", diffs_e), ("PPG", diffs_p)):
        if d:
            print(f"{name}와 Nano HR 차이: 평균 {np.mean(d):.2f}, 최대 {np.max(d):.2f} bpm (n={len(d)})")
    print("주의: Nano 값은 의료용 기준 장비가 아니라 비교용 참고값입니다.")
    if args.expect:
        judged = sum(n for k, n in levels.items() if k != "unavailable")
        wrong = sum(n for k, n in levels.items() if k not in ("unavailable", args.expect))
        print(f"기대값 {args.expect}: 판정 {judged}회 중 불일치 {wrong}회")


if __name__ == "__main__":
    main()
