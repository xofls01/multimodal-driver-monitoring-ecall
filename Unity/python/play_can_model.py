#!/usr/bin/env python3
"""Play a saved model visualization/results alongside a selected CAN scenario."""

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
SCENARIOS = {
    "normal": ("N", "can_normal_60kmh_15s.csv"),
    "caution": ("D3", "can_caution_drowsy_drift_15s.csv"),
    "distraction": ("A_new", "can_distraction_held_steer_15s.csv"),
    "danger": ("E4", "can_danger_steer_accel_15s.csv"),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", nargs="?", choices=SCENARIOS, default="danger",
                        help="CAN scenario and matching saved model clip (default: danger)")
    parser.add_argument("--recording", type=Path, help="Folder containing preview.mp4 and unity_packets.jsonl")
    parser.add_argument("--port", type=int, default=5001)
    args = parser.parse_args()
    clip_id, csv_name = SCENARIOS[args.scenario]

    python = ROOT / "driver-state-monitoring-system/.venv/bin/python"
    if python.is_file() and Path(sys.prefix).resolve() != python.parent.parent.resolve():
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        os.execve(str(python), [str(python), "-B", str(Path(__file__).resolve()), *sys.argv[1:]], environment)

    import cv2

    if args.recording:
        recording = args.recording.expanduser().resolve()
    else:
        outputs = ROOT / "driver-state-monitoring-system/outputs"
        candidates = list(outputs.glob(f"*/{clip_id}/preview.mp4"))
        candidates += list(outputs.glob(f"*/inference/{clip_id}/preview.mp4"))
        candidates = [p for p in candidates
                      if (p.parent / "summary.json").is_file()
                      and (p.parent / "unity_packets.jsonl").is_file()
                      and (p.parent / "unity_packets.jsonl").stat().st_size > 0
                      and (p.parent / "unity_packets.jsonl").stat().st_mtime <= p.stat().st_mtime]
        if not candidates:
            parser.error(f"저장된 {clip_id} 분석 영상이 없습니다. --recording으로 해당 분석 결과 폴더를 지정하세요.")
        recording = max(candidates, key=lambda p: p.stat().st_mtime).parent

    with (recording / "unity_packets.jsonl").open() as source:
        packets = [json.loads(line) for line in source if line.strip()]
    if not packets or any(packet.get("clip_id") != clip_id for packet in packets):
        parser.error(f"{args.scenario}에는 {clip_id} 분석 결과가 필요합니다. --recording 경로를 확인하세요.")
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
    title = f"Saved {clip_id} results + CAN {args.scenario} | q: exit"
    cv2.namedWindow(title, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(title, 1050, 620)
    cv2.imshow(title, frame)
    print(f"[REPLAY] CAN {args.scenario} + 모델 {clip_id} 사전 분석 결과: {recording}")
    print(f"[REPLAY] Unity Play 연결 대기: 127.0.0.1:{args.port}")
    state_thread = None

    try:
        while shared.connected_address is None:
            if cv2.waitKey(50) & 0xFF == ord("q"):
                return

        player.start(ROOT / "Unity/data" / csv_name, args.scenario)
        started_at = player.started_at

        def replay_states() -> None:
            for packet in packets:
                due = started_at + float(packet["source_timestamp"])
                if stop.wait(max(0.0, due - time.monotonic())):
                    return
                shared.receive_model_result(extract_model_state(packet), "saved_result_replay")

        state_thread = threading.Thread(target=replay_states, daemon=True)
        state_thread.start()
        print(f"[REPLAY] {clip_id} 영상 · 저장된 모델 판단 · can {args.scenario} 동시 시작")
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

        print("[REPLAY] 재생 완료. Unity 동작을 확인한 뒤 q 또는 Ctrl+C로 종료하세요.")
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
