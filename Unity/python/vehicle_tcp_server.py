#!/usr/bin/env python3
"""Bidirectional TCP controller for the Unity Stage 1 vehicle demo.

Protocol: newline-delimited UTF-8 JSON (NDJSON)
- Unity -> Python: vehicle_state
- Python -> Unity: vehicle_control
"""

from __future__ import annotations

import argparse
import json
import socket
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class ControlCommand:
    type: str = "vehicle_control"
    sequence: int = 0
    throttle: float = 0.0
    brake: float = 0.0
    steering: float = 0.0
    hazard: bool = False
    ecall: bool = False
    remote_enabled: bool = True

    def clamp(self) -> None:
        self.throttle = min(1.0, max(0.0, self.throttle))
        self.brake = min(1.0, max(0.0, self.brake))
        self.steering = min(1.0, max(-1.0, self.steering))


class SharedData:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.command = ControlCommand()
        self.latest_state: dict[str, Any] | None = None
        self.connected_address: tuple[str, int] | None = None
        self.running = True

    def update_command(self, **changes: Any) -> None:
        with self.lock:
            for key, value in changes.items():
                setattr(self.command, key, value)
            self.command.clamp()

    def next_command_json(self) -> str:
        with self.lock:
            self.command.sequence += 1
            return json.dumps(asdict(self.command), separators=(",", ":")) + "\n"

    def set_state(self, state: dict[str, Any]) -> None:
        with self.lock:
            self.latest_state = state

    def snapshot(self) -> tuple[ControlCommand, dict[str, Any] | None]:
        with self.lock:
            command = ControlCommand(**asdict(self.command))
            state = None if self.latest_state is None else dict(self.latest_state)
            return command, state


def receive_available_lines(
    connection: socket.socket,
    pending: str,
    shared: SharedData,
) -> str:
    try:
        chunk = connection.recv(8192)
    except socket.timeout:
        return pending

    if not chunk:
        raise ConnectionError("Unity closed the connection")

    pending += chunk.decode("utf-8")
    while "\n" in pending:
        line, pending = pending.split("\n", 1)
        line = line.rstrip("\r")
        if not line:
            continue

        try:
            message = json.loads(line)
        except json.JSONDecodeError as exception:
            print(f"[WARN] 잘못된 Unity JSON: {exception}")
            continue

        if message.get("type") == "vehicle_state":
            shared.set_state(message)

    return pending


def handle_unity_client(
    connection: socket.socket,
    address: tuple[str, int],
    shared: SharedData,
    send_rate_hz: float,
) -> None:
    connection.settimeout(0.02)
    send_interval = 1.0 / max(1.0, send_rate_hz)
    next_send_time = time.monotonic()
    pending = ""

    shared.connected_address = address
    print(f"[CONNECTED] Unity {address[0]}:{address[1]}")

    try:
        while shared.running:
            pending = receive_available_lines(connection, pending, shared)

            now = time.monotonic()
            if now >= next_send_time:
                connection.sendall(shared.next_command_json().encode("utf-8"))
                next_send_time = now + send_interval

    finally:
        shared.connected_address = None
        connection.close()
        print("[DISCONNECTED] Unity 연결 종료, 재접속 대기 중")


def server_loop(host: str, port: int, shared: SharedData, send_rate_hz: float) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((host, port))
        server_socket.listen(1)
        server_socket.settimeout(0.5)
        print(f"[LISTENING] {host}:{port}")

        while shared.running:
            try:
                connection, address = server_socket.accept()
            except socket.timeout:
                continue

            try:
                handle_unity_client(connection, address, shared, send_rate_hz)
            except (ConnectionError, ConnectionResetError, BrokenPipeError, OSError) as exception:
                if shared.running:
                    print(f"[NETWORK] {exception}")


def print_status(shared: SharedData) -> None:
    command, state = shared.snapshot()
    print(
        "[COMMAND] "
        f"throttle={command.throttle:.2f} "
        f"brake={command.brake:.2f} "
        f"steering={command.steering:.2f} "
        f"hazard={command.hazard} "
        f"ecall={command.ecall} "
        f"remote={command.remote_enabled}"
    )

    if state is None:
        print("[STATE] 아직 Unity 차량 상태를 받지 못했습니다.")
        return

    position = state.get("position") or {}
    print(
        "[STATE] "
        f"speed={float(state.get('speed_kph', 0.0)):.1f}km/h "
        f"position=({float(position.get('x', 0.0)):.1f}, "
        f"{float(position.get('z', 0.0)):.1f}) "
        f"heading={float(state.get('heading_deg', 0.0)):.1f}° "
        f"mode={state.get('control_mode', '?')}"
    )


def run_demo(shared: SharedData) -> None:
    print("[DEMO] 3초 가속 → 1초 우측 조향 → 3초 감속")
    shared.update_command(
        remote_enabled=True,
        throttle=0.45,
        brake=0.0,
        steering=0.0,
        hazard=False,
        ecall=False,
    )
    time.sleep(3.0)
    shared.update_command(steering=0.08)
    time.sleep(1.0)
    shared.update_command(throttle=0.0, brake=0.65, steering=0.0, hazard=True)
    time.sleep(3.0)
    shared.update_command(brake=1.0, steering=0.0, hazard=True)
    print("[DEMO] 정차 명령 완료")


def print_help() -> None:
    print(
        """
명령어
  set <가속> <브레이크> <조향>  예: set 0.4 0 0.15
  forward                         가속 0.4, 직진
  left / right                    조향 -0.25 / +0.25
  center                          조향 0
  stop                            가속 0, 브레이크 1
  coast                           가속·브레이크 0
  hazard on|off                   비상등 상태
  ecall on|off                    e-Call 상태
  manual                          Unity 키보드 제어로 복귀
  remote                          Python 원격 제어 활성화
  demo                            기본 가속·조향·정차 시나리오
  status                          최근 차량 상태와 명령 확인
  help                            명령어 표시
  quit                            서버 종료
""".strip()
    )


def console_loop(shared: SharedData) -> None:
    print_help()
    while shared.running:
        try:
            raw = input("control> ").strip()
        except (EOFError, KeyboardInterrupt):
            raw = "quit"

        if not raw:
            continue

        parts = raw.lower().split()
        command = parts[0]

        try:
            if command == "set" and len(parts) == 4:
                shared.update_command(
                    remote_enabled=True,
                    throttle=float(parts[1]),
                    brake=float(parts[2]),
                    steering=float(parts[3]),
                )
            elif command == "forward":
                shared.update_command(
                    remote_enabled=True, throttle=0.4, brake=0.0, steering=0.0
                )
            elif command == "left":
                shared.update_command(remote_enabled=True, steering=-0.25)
            elif command == "right":
                shared.update_command(remote_enabled=True, steering=0.25)
            elif command == "center":
                shared.update_command(steering=0.0)
            elif command == "stop":
                shared.update_command(remote_enabled=True, throttle=0.0, brake=1.0)
            elif command == "coast":
                shared.update_command(throttle=0.0, brake=0.0)
            elif command == "hazard" and len(parts) == 2:
                shared.update_command(hazard=parts[1] == "on")
            elif command == "ecall" and len(parts) == 2:
                shared.update_command(ecall=parts[1] == "on")
            elif command == "manual":
                shared.update_command(
                    remote_enabled=False, throttle=0.0, brake=0.0, steering=0.0
                )
            elif command == "remote":
                shared.update_command(remote_enabled=True)
            elif command == "demo":
                run_demo(shared)
            elif command == "status":
                print_status(shared)
            elif command == "help":
                print_help()
            elif command == "quit":
                shared.running = False
            else:
                print("알 수 없는 명령입니다. help를 입력해 확인하세요.")
        except ValueError:
            print("숫자 형식이 잘못됐습니다. 예: set 0.4 0 0.15")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Unity vehicle bidirectional TCP server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--send-rate", type=float, default=20.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    shared = SharedData()
    server_thread = threading.Thread(
        target=server_loop,
        args=(args.host, args.port, shared, args.send_rate),
        daemon=True,
        name="UnityTcpServer",
    )
    server_thread.start()

    try:
        console_loop(shared)
    finally:
        shared.running = False
        server_thread.join(timeout=2.0)
        print("서버를 종료했습니다.")


if __name__ == "__main__":
    main()
