"""아두이노를 노트북 USB에 직접 꽂고 ECG·PPG를 측정한다 (라즈베리파이·카메라 없이).

    python tools/live_serial.py --port COM4

라즈베리파이 경로와 같은 판정(LiveAssessor)과 화면(BioDashboard)을 쓰고,
측정 폴더에 biosignals.csv와 bio_status.jsonl을 같은 형식으로 저장한다.
시각은 노트북에서 한 줄을 다 읽은 시점이다. 대시보드 창을 닫으면 끝난다.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PATTERN = re.compile(r"ECG:(\d+)\s+PPG:(\d+)")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", default="COM4")
    p.add_argument("--output", type=Path, default=ROOT / "recordings")
    models = ROOT / "models"
    p.add_argument("--bio-model", type=Path, default=models / "hr_classifier_ft.pt")
    p.add_argument("--ppg-encoder", type=Path, default=models / "papagei_s.pt")
    p.add_argument("--ppg-head", type=Path, default=models / "ppg_head_ft.pt")
    args = p.parse_args()

    import numpy as np
    import serial
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation
    from biosignal.dashboard import BioDashboard
    from biosignal.live import LiveAssessor

    folder = args.output / datetime.now().strftime("SESSION_%Y%m%d_%H%M%S_%f")
    folder.mkdir(parents=True, exist_ok=False)
    meta = dict(source=f"laptop serial {args.port}", start_laptop_utc=datetime.now(timezone.utc).isoformat(),
                timing="Laptop read-completion timestamps (pi_* columns hold laptop monotonic time). No camera.",
                status="starting")

    def save_meta():
        (folder / "session.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    save_meta()

    assessor = LiveAssessor(model_path=str(args.bio_model), log_path=str(folder / "bio_status.jsonl"),
                            ppg_encoder_path=str(args.ppg_encoder), ppg_head_path=str(args.ppg_head))
    stop = threading.Event()
    lock = threading.Lock()
    display = deque(maxlen=3000)
    state = {"n": 0, "last": None, "msg": "Opening " + args.port}

    def receive():
        try:
            with serial.Serial(args.port, 115200, timeout=1) as ser, \
                 (folder / "biosignals.csv").open("w", newline="", encoding="utf-8") as f:
                time.sleep(2)  # 포트를 열면 Nano가 리셋된다. 시작 바이트를 버린다
                ser.reset_input_buffer()
                w = csv.writer(f)
                w.writerow(["sample_index", "pi_elapsed_s", "pi_monotonic_ns", "pi_utc_ns",
                            "laptop_receive_utc", "ecg_raw", "ppg_raw"])
                origin = time.monotonic_ns()
                flushed = time.monotonic()
                last_valid = time.monotonic()
                state["msg"] = "Recording"
                while not stop.is_set():
                    raw = ser.readline(256)
                    mono, utc = time.monotonic_ns(), time.time_ns()
                    m = PATTERN.fullmatch(raw.decode("ascii", errors="replace").strip())
                    if m:
                        ecg, ppg = map(int, m.groups())
                        if 0 <= ecg <= 1023 and 0 <= ppg <= 1023:
                            elapsed = (mono - origin) / 1e9
                            w.writerow([state["n"], f"{elapsed:.9f}", mono, utc,
                                        datetime.now(timezone.utc).isoformat(), ecg, ppg])
                            with lock:
                                display.append((elapsed, ecg, ppg))
                            assessor.push(elapsed, ecg, ppg)
                            state["n"] += 1
                            state["last"] = last_valid = time.monotonic()
                    if time.monotonic() - last_valid > 10:
                        raise RuntimeError("10초 동안 ECG/PPG 줄이 없습니다. 펌웨어, 포트, 다른 시리얼 프로그램을 확인하세요.")
                    if time.monotonic() - flushed > 1:
                        f.flush()
                        flushed = time.monotonic()
            meta["status"] = "stopped"
        except Exception as exc:
            meta["status"] = "error"
            meta["error"] = str(exc)
            state["msg"] = "ERROR: " + str(exc)
            print(state["msg"])
        finally:
            meta["counts"] = {"bio": state["n"]}
            meta["end_laptop_utc"] = datetime.now(timezone.utc).isoformat()
            save_meta()

    worker = threading.Thread(target=receive, daemon=True)
    worker.start()
    assessor.start()
    print("Saving on LAPTOP:", folder)
    print("Close the dashboard to stop.")
    try:
        dashboard = BioDashboard()

        def update(_):
            with lock:
                rows = np.asarray(display) if display else None
            age = f"age {time.monotonic() - state['last']:.1f}s" if state["last"] else "waiting"
            dashboard.update(rows, assessor.latest(), assessor.error,
                             footer=f"{state['msg']}\nbio: {state['n']} ({age})")
            return dashboard.artists()

        _anim = FuncAnimation(dashboard.fig, update, interval=100, cache_frame_data=False)
        plt.show()
    finally:
        stop.set()
        worker.join(timeout=5)
        assessor.stop()
        print("Recording closed. Laptop folder:", folder)
        print("Samples:", state["n"])


if __name__ == "__main__":
    main()
