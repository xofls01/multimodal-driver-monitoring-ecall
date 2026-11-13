#!/usr/bin/env python3
"""Play saved E4 visualization/results alongside can danger; no model inference."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import threading
import time

from can_adapter import CanAdapter
from can_scenario import CanScenarioPlayer
from model_state_client import extract_model_state
from vehicle_tcp_server import SharedData, server_loop


ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recording", type=Path, help="Folder containing preview.mp4 and unity_packets.jsonl")
    parser.add_argument("--port", type=int, default=5001)
    args = parser.parse_args()

    python = ROOT / "driver-state-monitoring-system/.venv/bin/python"
    if python.is_file() and Path(sys.prefix).resolve() != python.parent.parent.resolve():
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        os.execve(str(python), [str(python), "-B", str(Path(__file__).resolve()), *sys.argv[1:]], environment)

    import cv2

    if args.recording:
        recording = args.recording.expanduser().resolve()
    else:
        outputs = ROOT / "driver-state-monitoring-system/outputs"
        candidates = list(outputs.glob("*/E4/preview.mp4"))
        candidates += list(outputs.glob("*/inference/E4/preview.mp4"))
        candidates = [p for p in candidates
                      if (p.parent / "summary.json").is_file()
                      and (p.parent / "unity_packets.jsonl").is_file()
                      and (p.parent / "unity_packets.jsonl").stat().st_size > 0
                      and (p.parent / "unity_packets.jsonl").stat().st_mtime <= p.stat().st_mtime]
        if not candidates:
            parser.error("저장된 E4 분석 영상이 없습니다. --recording으로 분석 결과 폴더를 지정하세요.")
        recording = max(candidates, key=lambda p: p.stat().st_mtime).parent

    with (recording / "unity_packets.jsonl").open() as source:
        packets = [json.loads(line) for line in source if line.strip()]
    capture = cv2.VideoCapture(str(recording / "preview.mp4"))
    fps = capture.get(cv2.CAP_PROP_FPS)
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    ok, frame = capture.read()
    if not ok or fps <= 0:
        capture.release()
        parser.error("저장된 분석 영상을 열 수 없습니다.")

    shared = SharedData(model_input_enabled=True)
    adapter = CanAdapter(shared)
    player = CanScenarioPlayer(adapter.on_start, adapter.on_sample, adapter.on_finish)
    stop = threading.Event()
    server = threading.Thread(target=server_loop, args=("127.0.0.1", args.port, shared, 20.0), daemon=True)
    server.start()
    title = "Saved E4 results + CAN danger | q: exit"
    cv2.namedWindow(title, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(title, 1050, 620)
    cv2.imshow(title, frame)
    print(f"[REPLAY] 사전 분석 결과: {recording}")
    print(f"[REPLAY] Unity Play 연결 대기: 127.0.0.1:{args.port}")
    state_thread = None

    try:
        while shared.connected_address is None:
            if cv2.waitKey(50) & 0xFF == ord("q"):
                return

        player.start(ROOT / "Unity/data/can_danger_steer_accel_15s.csv", "danger")
        started_at = player.started_at

        def replay_states() -> None:
            for packet in packets:
                due = started_at + float(packet["source_timestamp"])
                if stop.wait(max(0.0, due - time.monotonic())):
                    return
                shared.receive_model_result(extract_model_state(packet), "saved_result_replay")

        state_thread = threading.Thread(target=replay_states, daemon=True)
        state_thread.start()
        print("[REPLAY] E4 영상 · 저장된 모델 판단 · can danger 동시 시작")
        current_frame = 0
        while shared.connected_address is not None:
            target = int((time.monotonic() - started_at) * fps)
            if target >= frame_count:
                break
            if target > current_frame:
                while current_frame + 1 < target:
                    if not capture.grab():
                        break
                    current_frame += 1
                ok, frame = capture.read()
                if not ok:
                    break
                current_frame += 1
                cv2.imshow(title, frame)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                return
            time.sleep(0.003)

        print("[REPLAY] 재생 완료. Unity 비상 정차를 확인한 뒤 q 또는 Ctrl+C로 종료하세요.")
        while shared.connected_address is not None:
            if cv2.waitKey(50) & 0xFF == ord("q"):
                break
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        player.stop()
        if state_thread is not None:
            state_thread.join(timeout=1.0)
        shared.running = False
        server.join(timeout=1.0)
        capture.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
