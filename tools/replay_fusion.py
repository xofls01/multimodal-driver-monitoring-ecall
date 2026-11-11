"""미리 만든 생체신호·비전·CAN 파일을 하나의 시간축에 놓고 통합 판단을 재생한다.

엔진은 넘겨받은 시각으로만 판단하므로, 파일 시각을 그대로 넣으면 실시간으로 돌린 것과 결과가 같다.
따로 만든 파일은 시계가 다르므로 --*-offset으로 시나리오 시각에 맞춘다.

실행 예:
  python tools/replay_fusion.py --ecg synthetic_ecg_250hz_60s.csv --can can_caution_drowsy_drift_15s.csv --can-offset 30

입력 형식
  --ecg     CSV. 시각 열(time_s, pi_elapsed_s, clip_s 중 하나)과 ECG 열(ecg_adc_simulated 또는 ecg_raw).
            ppg_raw 열이 있으면 PPG로도 쓴다. 측정 폴더의 biosignals.csv를 그대로 넣을 수 있다.
  --ppg     PPG만 따로 있을 때. 시각 열과 ppg_raw 또는 ppg_adc_simulated 열.
  --vision  JSONL. 한 줄에 timestamp(또는 t), state, reasons. durations는 없어도 된다.
            비전 브랜치의 replay_response_events.py 결과를 이벤트 없이(빈 목록 [] 파일) 만들면 이 형식이다.
  --can     CSV. steering_angle_deg, accelerator_pedal_pct, brake_pedal_pct, 선택: vehicle_speed_kph,
            steering_torque_nm, ground_truth_state. ground_truth_state가 있으면 판단과 비교한다.
결과는 --out JSONL에 저장하고, 입력 파일은 수정하지 않는다.
"""
import argparse
import csv
import json
import os
import sys
from collections import Counter
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from biosignal import BioSignalMonitor, Config
from fusion import FusionEngine, can_from_csv_row

TIME_COLS = ("time_s", "pi_elapsed_s", "clip_s")


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def pick(row, names, what, path):
    for name in names:
        if name in row:
            return name
    raise SystemExit(f"{path}: {what} 열이 없습니다. 다음 중 하나가 필요합니다: {', '.join(names)}")


def load_signal(path, value_cols, offset):
    rows = read_csv(path)
    tc = pick(rows[0], TIME_COLS, "시각", path)
    vc = pick(rows[0], value_cols, "신호", path)
    return [(float(r[tc]) + offset, float(r[vc])) for r in rows], rows, tc


def load_vision(path, offset):
    """비전 판단을 읽는다. durations가 없으면 눈 감김 지속 시간을 연속 구간으로 추정한다."""
    out, closed_since = [], None
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            t = float(r.get("timestamp", r.get("t"))) + offset
            reasons = list(r.get("reasons", []))
            durations = dict(r.get("durations") or {})
            if "eyes_closed" not in durations:
                if "eyes_closed" in reasons:
                    closed_since = t if closed_since is None else closed_since
                    # 비전은 2초가 지나야 eyes_closed 사유를 붙인다
                    durations["eyes_closed"] = 2.0 + (t - closed_since)
                else:
                    closed_since = None
            out.append((t, SimpleNamespace(state=r["state"], reasons=reasons, durations=durations)))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ecg")
    ap.add_argument("--ppg")
    ap.add_argument("--vision")
    ap.add_argument("--can")
    ap.add_argument("--bio-offset", type=float, default=0.0)
    ap.add_argument("--vision-offset", type=float, default=0.0)
    ap.add_argument("--can-offset", type=float, default=0.0)
    ap.add_argument("--steer-full-deg", type=float, default=30.0,
                    help="조향 -1~1에 대응하는 각도. 기본 30도(바퀴 조향각). 핸들 각도 로그면 약 450~540")
    ap.add_argument("--can-manual", choices=["yes", "no"], default="yes",
                    help="CAN 값이 운전자 조작인가. 크루즈 중 기록이면 no")
    ap.add_argument("--model")
    ap.add_argument("--ppg-encoder")
    ap.add_argument("--ppg-head")
    ap.add_argument("--rate", type=float, default=10.0, help="통합 판단 주기(Hz)")
    ap.add_argument("--out", default="fusion_status.jsonl")
    args = ap.parse_args()
    if not (args.ecg or args.ppg or args.vision or args.can):
        ap.error("입력 파일이 하나 이상 필요합니다")
    for p in (args.ecg, args.ppg, args.vision, args.can):
        if p and os.path.abspath(p) == os.path.abspath(args.out):
            ap.error("--out이 입력 파일을 덮어씁니다")

    events = []  # (시각, 순서, 종류, 값)
    if args.ecg:
        ecg, rows, tc = load_signal(args.ecg, ("ecg_adc_simulated", "ecg_raw"), args.bio_offset)
        events += [(t, 0, "ecg", v) for t, v in ecg]
        if "ppg_raw" in rows[0] and not args.ppg:
            events += [(float(r[tc]) + args.bio_offset, 0, "ppg", float(r["ppg_raw"])) for r in rows]
    if args.ppg:
        ppg, _, _ = load_signal(args.ppg, ("ppg_adc_simulated", "ppg_raw"), args.bio_offset)
        events += [(t, 0, "ppg", v) for t, v in ppg]
    if args.vision:
        events += [(t, 1, "vision", d) for t, d in load_vision(args.vision, args.vision_offset)]
    truth = []
    if args.can:
        for r in read_csv(args.can):
            t = float(r[pick(r, TIME_COLS, "시각", args.can)]) + args.can_offset
            kw = can_from_csv_row(r, args.steer_full_deg)
            kw["driver_input_valid"] = args.can_manual == "yes"
            events.append((t, 2, "can", kw))
            if r.get("ground_truth_state"):
                truth.append((t, r["ground_truth_state"].strip().lower(), r.get("ground_truth_event", "")))
    events.sort(key=lambda e: (e[0], e[1]))
    if not events:
        raise SystemExit("입력에 데이터가 없습니다")

    if os.path.exists(args.out):
        os.remove(args.out)
    fusion = FusionEngine(log_path=args.out)
    bio = None
    if args.ecg or args.ppg:
        bio = BioSignalMonitor(Config(), model_path=args.model,
                               ppg_encoder_path=args.ppg_encoder, ppg_head_path=args.ppg_head)

    step = 1.0 / args.rate
    start, end = events[0][0], events[-1][0]
    now, i, next_bio = start + step, 0, start + 1.0
    prev, rows, levels = None, [], Counter()
    print(f"{'시각':>7}  {'판단':<12}{'Unity':<6}{'남은 확인':>8}  사유")
    while now <= end + step / 2:
        while i < len(events) and events[i][0] <= now:
            t, _, kind, value = events[i]
            if kind == "ecg":
                bio.push_ecg(t, value)
            elif kind == "ppg":
                bio.push_ppg(t, value)
            elif kind == "vision":
                fusion.push_vision(t, value)
            else:
                fusion.push_can(t, **value)
            i += 1
        if bio is not None and now >= next_bio:
            fusion.push_bio(now, bio.update(now))
            next_bio += 1.0
        d = fusion.update(now)
        rows.append(d)
        levels[d.level] += 1
        key = (d.level, d.driver_state, tuple(r for r in d.reasons if not r.startswith("BIO:HOLD")))
        if key != prev:
            left = "-" if d.response_remaining_s is None else f"{d.response_remaining_s:.1f}s"
            print(f"{now:7.1f}  {d.level:<12}{d.driver_state:<6}{left:>8}  {', '.join(d.reasons)}")
            prev = key
        now = round(now + step, 6)
    fusion.close()

    print("\n판단 횟수:", dict(levels))
    if truth:
        compare(rows, truth)
    print(f"결과: {args.out}")


def compare(rows, truth):
    """CAN 파일의 정답 라벨 구간과 통합 판단을 비교한다."""
    t0, t1 = truth[0][0], truth[-1][0]
    inside = [d for d in rows if t0 <= d.t <= t1]
    label_at = lambda t: max((x for x in truth if x[0] <= t), key=lambda x: x[0])[1]
    pairs = Counter((label_at(d.t), d.level) for d in inside)
    match = sum(n for (a, b), n in pairs.items() if a == b)
    print(f"\n정답 라벨 비교 ({t0:.1f}–{t1:.1f}초, 판단 {len(inside)}회): 일치 {match}회 ({match / max(1, len(inside)):.0%})")
    for (label, level), n in sorted(pairs.items()):
        print(f"  정답 {label:<8} → 판단 {level:<12} {n}회")
    # 라벨이 Normal에서 바뀐 구간마다 판단이 따라간 시점
    prev = "normal"
    for t, label, event in truth:
        if label != prev and label != "normal":
            hit = next((d.t for d in rows if d.t >= t and d.level == label), None)
            delay = "판단 안 됨" if hit is None else f"{hit - t:+.1f}초"
            print(f"  {t:6.1f}초 정답 {label} ({event}) → {delay}")
        prev = label


if __name__ == "__main__":
    main()
