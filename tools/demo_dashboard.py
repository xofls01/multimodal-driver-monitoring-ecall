"""장비 없이 화면을 확인한다. 합성 시나리오나 측정 기록을 판정 엔진에 흘려보낸다.

실행:
  python tools/demo_dashboard.py cardiac_arrest
  python tools/demo_dashboard.py --recording "<측정 폴더>"
  python tools/demo_dashboard.py cardiac_arrest --no-model --snapshot 30,62,72 --out outputs/demo
시나리오 목록은 biosignal/simulate.py의 SCENARIOS를 본다. 합성 신호는 동작 확인용이다.
"""
import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np

from biosignal import BioSignalMonitor, Config
from biosignal import simulate as sim

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def load_scenario(name, seed):
    s = sim.SCENARIOS[name](seed=seed)
    ppg = np.interp(s.ecg_t, s.ppg_t, s.ppg)
    contact = np.interp(s.ecg_t, s.ppg_t, s.ppg_contact.astype(float)) > 0.5
    return s.ecg_t, s.ecg, ppg, s.ecg_lo, contact


def load_recording(folder):
    with open(os.path.join(folder, "biosignals.csv"), encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise SystemExit(f"샘플이 없는 기록입니다: {folder}")
    key = "pi_elapsed_s" if "pi_elapsed_s" in rows[0] else "clip_s"
    t = np.array([float(r[key]) for r in rows])
    return (t - t[0], np.array([float(r["ecg_raw"]) for r in rows]),
            np.array([float(r["ppg_raw"]) for r in rows]), None, None)


class Player:
    """샘플을 시간순으로 넣고 1초마다 판정한다."""

    def __init__(self, monitor, data):
        self.monitor = monitor
        self.t, self.ecg, self.ppg, self.lo, self.contact = data
        self.i = 0
        self.next_update = float(self.t[0]) + 1.0
        self.status = None

    @property
    def done(self):
        return self.i >= len(self.t)

    def advance(self, until):
        while self.i < len(self.t) and self.t[self.i] <= until:
            t = float(self.t[self.i])
            self.monitor.push_ecg(t, self.ecg[self.i], None if self.lo is None else bool(self.lo[self.i]))
            self.monitor.push_ppg(t, self.ppg[self.i], None if self.contact is None else bool(self.contact[self.i]))
            if t >= self.next_update:
                self.status = self.monitor.update(t)
                self.next_update += 1.0
            self.i += 1

    def samples(self, seconds=6.0):
        lo = int(np.searchsorted(self.t, self.t[max(self.i - 1, 0)] - seconds))
        return np.column_stack([self.t[lo:self.i], self.ecg[lo:self.i], self.ppg[lo:self.i]])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenario", nargs="?", default="cardiac_arrest", choices=sorted(sim.SCENARIOS))
    ap.add_argument("--recording", help="측정 폴더. 지정하면 시나리오 대신 재생한다")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--no-model", action="store_true", help="학습 모델 없이 규칙으로만 판정")
    ap.add_argument("--snapshot", help="화면을 띄우지 않고 이 시각(초, 쉼표 구분)의 화면을 PNG로 저장")
    ap.add_argument("--out", default=os.path.join("outputs", "dashboard"))
    args = ap.parse_args()

    models = {} if args.no_model else dict(
        model_path=os.path.join(ROOT, "models", "hr_classifier_ft.pt"),
        ppg_encoder_path=os.path.join(ROOT, "models", "papagei_s.pt"),
        ppg_head_path=os.path.join(ROOT, "models", "ppg_head_ft.pt"))
    data = load_recording(args.recording) if args.recording else load_scenario(args.scenario, args.seed)
    player = Player(BioSignalMonitor(Config(), **models), data)

    if args.snapshot:
        import matplotlib
        matplotlib.use("Agg")
    from biosignal.dashboard import BioDashboard
    dash = BioDashboard()
    label = os.path.basename(args.recording.rstrip("/\\")) if args.recording else args.scenario

    if args.snapshot:
        os.makedirs(args.out, exist_ok=True)
        times = sorted(float(x) for x in args.snapshot.split(","))
        for now in np.arange(1.0, times[-1] + 1.0):
            player.advance(now)
            dash.update(player.samples(), player.status, footer=f"{label}  t={now:.0f}s")
            if now in times:
                path = os.path.join(args.out, f"{label}_{now:03.0f}s.png")
                dash.fig.savefig(path, dpi=80)
                st = player.status
                print(f"{path}: {st.level} {st.reasons} hr={st.hr} baseline={st.hr_baseline} delta={st.hr_delta}")
        return

    from matplotlib import pyplot as plt
    from matplotlib.animation import FuncAnimation
    clock = [float(player.t[0])]
    interval_ms = 100

    def tick(_):
        clock[0] += args.speed * interval_ms / 1000.0
        player.advance(clock[0])
        dash.update(player.samples(), player.status, footer=f"{label}  t={clock[0]:.0f}s  (재생)")
        return dash.artists()

    animation = FuncAnimation(dash.fig, tick, interval=interval_ms, cache_frame_data=False)
    plt.show()
    del animation


if __name__ == "__main__":
    main()
