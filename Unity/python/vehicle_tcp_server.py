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
from pathlib import Path
from typing import Any

from can_adapter import CanAdapter, CanPlaybackConfig
from can_anomaly import CanDecision
from can_scenario import CanScenarioPlayer, CanSignals
from model_state_client import ModelConnectionConfig, ModelStateClient


@dataclass
class ControlCommand:
    type: str = "vehicle_control"
    sequence: int = 0
    throttle: float = 0.0
    brake: float = 0.0
    steering: float = 0.0
    hazard: bool = False
    ecall: bool = False
    remote_enabled: bool = False
    control_mode: str = "cruise"
    driver_state: str = "Normal"
    can_active: bool = False
    can_scenario: str = ""
    can_time_s: float = 0.0
    can_anomaly: bool = False
    can_risk_score: int = 0
    steer_anomaly: bool = False
    accel_anomaly: bool = False
    steer_correction_active: bool = False
    accel_limit_active: bool = False
    can_detect_reason: str = ""
    can_release_reason: str = ""
    can_steering: float = 0.0
    can_throttle: float = 0.0
    can_brake: float = 0.0
    can_target_speed_kph: float = 0.0

    def clamp(self) -> None:
        self.throttle = min(1.0, max(0.0, self.throttle))
        self.brake = min(1.0, max(0.0, self.brake))
        self.steering = min(1.0, max(-1.0, self.steering))
        self.driver_state = self.driver_state.strip().capitalize()
        if self.driver_state not in {"Normal", "Caution", "Danger"}:
            raise ValueError("driver_state must be Normal, Caution, or Danger")
        self.control_mode = self.control_mode.strip().lower()
        if self.control_mode not in {"cruise", "remote", "manual"}:
            raise ValueError("control_mode must be cruise, remote, or manual")


class SharedData:
    def __init__(self, *, model_input_enabled: bool = False) -> None:
        # Model results and CAN samples share one atomic state/control transition.
        self.lock = threading.RLock()
        self.command = ControlCommand(driver_state="Caution" if model_input_enabled else "Normal")
        self.latest_state: dict[str, Any] | None = None
        self.connected_address: tuple[str, int] | None = None
        self.running = True
        self.latest_can_state: CanSignals | None = None
        self.can_decision: CanDecision | None = None
        self.can_control_locked = False
        self.model_input_enabled = model_input_enabled
        self.model_input_status = "waiting_for_unity" if model_input_enabled else "disabled"
        self._model_danger_pending = False

    def update_command(self, **changes: Any) -> None:
        with self.lock:
            if self.command.can_active and any(key in changes for key in (
                "control_mode", "remote_enabled", "throttle", "brake", "steering"
            )):
                raise ValueError("CAN 재생 중에는 일반 제어 명령을 사용할 수 없습니다. can stop 먼저 실행하세요.")
            for key, value in changes.items():
                setattr(self.command, key, value)
            self.command.clamp()
            if self.command.driver_state == "Danger":
                self.can_control_locked = True
                self.command.steer_correction_active = False
                self.command.accel_limit_active = False
                if self.command.can_active:
                    self.command.throttle = 0.0
                    self.command.brake = 0.0
                    self.command.steering = 0.0
                self.command.hazard = True

    def set_driver_state(self, predicted_state: str, *, from_model: bool = False) -> None:
        """상태만 변경합니다. Cruise/Remote/Manual 모드에는 손대지 않습니다."""
        state = predicted_state.strip().capitalize()
        if state not in {"Normal", "Caution", "Danger"}:
            raise ValueError("운전자 상태는 Normal, Caution, Danger 중 하나여야 합니다.")
        with self.lock:
            if self.model_input_enabled and not from_model:
                raise ValueError("모델 연결 모드에서는 state/scenario 대신 실제 모델 결과를 사용합니다.")
            previous = self.command.driver_state
            self.update_command(driver_state=state)
            if state == "Normal":
                self.command.steer_correction_active = False
                self.command.accel_limit_active = False
                if self.can_control_locked and not self.command.can_active:
                    self.command.control_mode = "cruise"
                    self.command.remote_enabled = False
                    self.command.throttle = 0.0
                    self.command.brake = 0.0
                    self.command.steering = 0.0
                    self.command.hazard = False
                    self.command.ecall = False
        if previous != state:
            print(f"[MODEL] final driver state {previous} -> {state}")

    def receive_model_result(self, state: str | None, status: str) -> None:
        """Accept a final label only; no model packet can change vehicle controls.

        Missing/stale input keeps a warning, never invents Normal or a new
        Danger. A previously received Danger is retained on input failure.
        Unity's emergency latch remains authoritative even after a fresh Normal.
        """
        with self.lock:
            previous_status = self.model_input_status
            self.model_input_status = status
            if state is None:
                state = "Danger" if self.command.driver_state == "Danger" else "Caution"
            elif state == "Danger":
                # Preserve brief Danger -> Normal bursts until Unity has
                # acknowledged its existing emergency latch via vehicle_state.
                self._model_danger_pending = True
            self.set_driver_state(state, from_model=True)
        if status != previous_status:
            print(f"[MODEL INPUT] {status}; driver_state={state}")

    def next_command_json(self) -> str:
        with self.lock:
            self.command.sequence += 1
            packet = asdict(self.command)
            if self._model_danger_pending:
                packet.update(driver_state="Danger", hazard=True)
            return json.dumps(packet, separators=(",", ":")) + "\n"

    def set_state(self, state: dict[str, Any]) -> None:
        with self.lock:
            self.latest_state = state
            if state.get("emergency_phase") in {
                "SignalAndSlow", "MoveToShoulder", "StraightenAndBrake", "Stopped", "StopInPlace"
            }:
                self._model_danger_pending = False

    def snapshot(self) -> tuple[ControlCommand, dict[str, Any] | None]:
        with self.lock:
            command = ControlCommand(**asdict(self.command))
            state = None if self.latest_state is None else dict(self.latest_state)
            return command, state

    def begin_can_playback(self, name: str) -> None:
        with self.lock:
            if self.command.driver_state == "Danger" or self._model_danger_pending:
                if self.model_input_enabled:
                    raise ValueError("Danger 상태입니다. 모델이 정상으로 복귀한 뒤 Unity R로 초기화하세요.")
                raise ValueError("Danger 상태입니다. Unity R 초기화 전에 state normal을 전송하세요.")
            phase = None if self.latest_state is None else self.latest_state.get("emergency_phase")
            if phase not in (None, "Idle", "Unavailable"):
                raise ValueError("Unity 비상제어가 아직 활성화돼 있습니다. R 초기화 후 다시 시작하세요.")
            self.can_control_locked = False
            self.latest_can_state = None
            self.can_decision = None
            self.command.can_active = True
            self.command.can_scenario = name
            self.command.can_time_s = 0.0
            self.command.can_anomaly = False
            self.command.can_risk_score = 0
            self.command.steer_anomaly = False
            self.command.accel_anomaly = False
            self.command.steer_correction_active = False
            self.command.accel_limit_active = False
            self.command.can_detect_reason = ""
            self.command.can_release_reason = ""
            self.command.control_mode = "remote"
            self.command.remote_enabled = True
            self.command.throttle = 0.0
            self.command.brake = 0.0
            self.command.steering = 0.0
            self.command.hazard = False
            self.command.ecall = False

    def apply_can_sample(
        self, signals: CanSignals, decision: CanDecision,
        steering: float, throttle: float, brake: float,
        *, raw_steering: float, raw_throttle: float, raw_brake: float,
        steer_correction: bool, accel_limit: bool,
        unrestricted_steering: float, unrestricted_throttle: float,
    ) -> bool:
        with self.lock:
            if not self.command.can_active:
                return False
            if self.command.driver_state != "Caution":
                # State may have changed since the adapter took its snapshot.
                steer_correction = accel_limit = False
                steering, throttle = unrestricted_steering, unrestricted_throttle
            self.latest_can_state = signals
            self.can_decision = decision
            self.command.can_time_s = signals.time_s
            self.command.can_anomaly = decision.can_anomaly
            self.command.can_risk_score = decision.can_risk_score
            self.command.steer_anomaly = decision.steer_anomaly
            self.command.accel_anomaly = decision.accel_anomaly
            self.command.steer_correction_active = (
                steer_correction and not self.can_control_locked and self.command.can_active)
            self.command.accel_limit_active = (
                accel_limit and not self.can_control_locked and self.command.can_active)
            self.command.can_detect_reason = ",".join(decision.active_conditions)
            self.command.can_release_reason = ",".join(filter(None, (
                decision.steer_release_reason, decision.accel_release_reason)))
            self.command.can_steering = raw_steering
            self.command.can_throttle = raw_throttle
            self.command.can_brake = raw_brake
            self.command.can_target_speed_kph = signals.vehicle_speed_kph
            if self.can_control_locked:
                return False
            self.command.control_mode = "remote"
            self.command.remote_enabled = True
            self.command.steering = steering
            self.command.throttle = throttle
            self.command.brake = brake
            self.command.hazard = signals.hazard
            self.command.ecall = signals.ecall
            return True

    def finish_can_playback(self, release_control: bool) -> None:
        with self.lock:
            self.command.can_active = False
            self.command.can_anomaly = False
            self.command.can_risk_score = 0
            self.command.steer_anomaly = False
            self.command.accel_anomaly = False
            self.command.steer_correction_active = False
            self.command.accel_limit_active = False
            if release_control or self.can_control_locked:
                self.command.throttle = 0.0
                self.command.brake = 0.0
                self.command.steering = 0.0
                if not self.can_control_locked:
                    self.command.control_mode = "cruise"
                    self.command.remote_enabled = False
                    self.command.hazard = False
                    self.command.ecall = False

    def on_unity_disconnect(self) -> None:
        """Discard stale feedback and CAN control before a later reconnection."""
        with self.lock:
            self.latest_state = None
            if not self.command.can_active:
                return
            self.command.can_active = False
            self.command.can_anomaly = False
            self.command.can_risk_score = 0
            self.command.steer_anomaly = False
            self.command.accel_anomaly = False
            self.command.steer_correction_active = False
            self.command.accel_limit_active = False
            self.command.throttle = 0.0
            self.command.brake = 0.0
            self.command.steering = 0.0
            if not self.can_control_locked:
                self.command.control_mode = "cruise"
                self.command.remote_enabled = False


def publish_prediction(shared: SharedData, predicted_label: str) -> None:
    """실제 분류 모델이 호출할 단일 진입점입니다."""
    shared.set_driver_state(predicted_label)


SCENARIO_STEPS: dict[str, tuple[tuple[str, float | None], ...]] = {
    "danger": (("Normal", 10.0), ("Danger", None)),
    "caution": (("Normal", 5.0), ("Caution", 8.0)),
}


class ScenarioRunner:
    """상태 시나리오를 네트워크/콘솔과 분리된 스레드에서 실행합니다."""

    def __init__(self, shared: SharedData) -> None:
        self.shared = shared
        self.lock = threading.Lock()
        self.stop_event: threading.Event | None = None
        self.thread: threading.Thread | None = None

    def start(self, name: str) -> None:
        if name not in SCENARIO_STEPS:
            raise ValueError(f"지원하지 않는 시나리오: {name}")
        if self.shared.model_input_enabled:
            raise ValueError("모델 연결 모드에서는 실제 모델 결과를 사용합니다.")

        self.stop(wait=True)
        stop_event = threading.Event()
        thread = threading.Thread(
            target=self._run,
            args=(name, stop_event),
            daemon=True,
            name=f"Scenario-{name}",
        )
        with self.lock:
            self.stop_event = stop_event
            self.thread = thread
        thread.start()
        print(f"[SCENARIO] {name} 시작")

    def stop(self, wait: bool = False) -> bool:
        with self.lock:
            stop_event = self.stop_event
            thread = self.thread

        if stop_event is None or thread is None or not thread.is_alive():
            return False

        stop_event.set()
        if wait and thread is not threading.current_thread():
            thread.join(timeout=1.5)
        return True

    def _run(self, name: str, stop_event: threading.Event) -> None:
        try:
            for state, duration in SCENARIO_STEPS[name]:
                if stop_event.is_set() or not self.shared.running:
                    print(f"[SCENARIO] {name} 중단")
                    return

                publish_prediction(self.shared, state)
                if duration is None:
                    held_at = time.monotonic()
                    while not stop_event.is_set() and self.shared.running:
                        elapsed = time.monotonic() - held_at
                        print(
                            f"[SCENARIO {name}] "
                            f"{elapsed:4.1f}s state={state} 유지"
                        )
                        stop_event.wait(1.0)
                    print(f"[SCENARIO] {name} 중단")
                    return

                started_at = time.monotonic()
                while not stop_event.is_set() and self.shared.running:
                    elapsed = time.monotonic() - started_at
                    if elapsed >= duration:
                        break
                    print(
                        f"[SCENARIO {name}] "
                        f"{elapsed:4.1f}/{duration:.1f}s state={state}"
                    )
                    stop_event.wait(min(1.0, duration - elapsed))

                if stop_event.is_set() or not self.shared.running:
                    print(f"[SCENARIO] {name} 중단")
                    return
            publish_prediction(self.shared, "Normal")
            print(f"[SCENARIO] {name} 완료, state=Normal")
        finally:
            with self.lock:
                if self.stop_event is stop_event:
                    self.stop_event = None
                    self.thread = None


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
        shared.on_unity_disconnect()
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
        f"remote={command.remote_enabled} "
        f"control_mode={command.control_mode} "
        f"driver_state={command.driver_state} "
        f"can_active={command.can_active} can_scenario={command.can_scenario} "
        f"can_time_s={command.can_time_s:.1f} can_anomaly={command.can_anomaly} "
        f"steer_anomaly={command.steer_anomaly} accel_anomaly={command.accel_anomaly} "
        f"steer_correction={command.steer_correction_active} "
        f"accel_limit={command.accel_limit_active} "
        f"reason={command.can_detect_reason or '-'} "
        f"release={command.can_release_reason or '-'} "
        f"can_locked={shared.can_control_locked}"
    )
    if shared.model_input_enabled:
        print(f"[MODEL INPUT] {shared.model_input_status}")

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
        f"mode={state.get('control_mode', '?')} "
        f"driver_state={state.get('driver_state', '?')} "
        f"emergency_phase={state.get('emergency_phase', '?')}"
    )


def run_demo(shared: SharedData) -> None:
    print("[DEMO] 직진 가속 → 짧은 차선 변경 → 방향 정렬 → 감속")
    shared.update_command(
        remote_enabled=True,
        control_mode="remote",
        throttle=0.35,
        brake=0.0,
        steering=0.0,
        hazard=False,
        ecall=False,
    )
    time.sleep(3.0)
    shared.update_command(steering=0.08)
    time.sleep(1.0)
    shared.update_command(steering=-0.08)
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
  state normal|caution|danger     모델 분류 상태 전송
  cruise                          Unity 정속주행 활성화
  manual                          Cruise를 끄고 키보드 제어로 복귀
  remote                          Python 원격 제어 활성화
  scenario danger                 Normal 10초 후 Danger 유지
  scenario caution                Normal 5초, Caution 8초, Normal
  scenario stop                   진행 중인 상태 시나리오 중단
  can normal|caution|distraction|danger  CAN CSV 재생 시작
  can stop                        CAN 재생 중지, 정속주행 복귀
  can restart                     로드된 CAN 시나리오 처음부터 재생
  can status                      CAN 재생 시간과 Unity 비상 단계
  demo                            기본 가속·조향·정차 시나리오
  status                          최근 차량 상태와 명령 확인
  help                            명령어 표시
  quit                            서버 종료
""".strip()
    )


def console_loop(
    shared: SharedData, scenarios: ScenarioRunner,
    can_player: CanScenarioPlayer, can_adapter: CanAdapter,
    can_paths: dict[str, Path],
) -> None:
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
                    control_mode="remote",
                    throttle=float(parts[1]),
                    brake=float(parts[2]),
                    steering=float(parts[3]),
                )
            elif command == "forward":
                shared.update_command(
                    remote_enabled=True,
                    control_mode="remote",
                    throttle=0.4,
                    brake=0.0,
                    steering=0.0,
                )
            elif command == "left":
                shared.update_command(
                    remote_enabled=True, control_mode="remote", steering=-0.25
                )
            elif command == "right":
                shared.update_command(
                    remote_enabled=True, control_mode="remote", steering=0.25
                )
            elif command == "center":
                shared.update_command(steering=0.0)
            elif command == "stop":
                shared.update_command(
                    remote_enabled=True,
                    control_mode="remote",
                    throttle=0.0,
                    brake=1.0,
                )
            elif command == "coast":
                shared.update_command(throttle=0.0, brake=0.0)
            elif command == "hazard" and len(parts) == 2:
                shared.update_command(hazard=parts[1] == "on")
            elif command == "ecall" and len(parts) == 2:
                shared.update_command(ecall=parts[1] == "on")
            elif command == "state" and len(parts) == 2:
                scenarios.stop(wait=True)
                publish_prediction(shared, parts[1])
                print(f"[MODEL] 운전자 상태를 {parts[1].capitalize()}로 설정했습니다.")
            elif command == "cruise":
                shared.update_command(
                    remote_enabled=False,
                    control_mode="cruise",
                    throttle=0.0,
                    brake=0.0,
                    steering=0.0,
                )
            elif command == "manual":
                shared.update_command(
                    remote_enabled=False,
                    control_mode="manual",
                    throttle=0.0,
                    brake=0.0,
                    steering=0.0,
                )
            elif command == "remote":
                shared.update_command(remote_enabled=True, control_mode="remote")
            elif command == "scenario" and len(parts) == 2:
                if parts[1] == "stop":
                    stopped = scenarios.stop(wait=True)
                    print(
                        "[SCENARIO] 중단 요청 완료"
                        if stopped
                        else "[SCENARIO] 실행 중인 시나리오가 없습니다."
                    )
                else:
                    if shared.model_input_enabled:
                        raise ValueError("모델 연결 모드에서는 상태 시나리오를 실행할 수 없습니다. CAN 재생은 가능합니다.")
                    can_player.stop()
                    shared.finish_can_playback(release_control=True)
                    scenarios.start(parts[1])
            elif command == "can" and len(parts) == 2 and parts[1] in can_paths:
                scenarios.stop(wait=True)
                can_player.start(can_paths[parts[1]], parts[1])
            elif command == "can" and len(parts) == 2 and parts[1] == "stop":
                can_player.stop()
                shared.finish_can_playback(release_control=True)
                print("[CAN] replay stopped; cruise restored unless Danger is latched")
            elif command == "can" and len(parts) == 2 and parts[1] == "restart":
                scenarios.stop(wait=True)
                can_player.restart()
            elif command == "can" and len(parts) == 2 and parts[1] == "status":
                name, elapsed, running = can_player.status()
                _, unity_state = shared.snapshot()
                print(f"[CAN] scenario={name} elapsed={elapsed:.1f}s running={running} "
                      "vehicle-anomaly-monitor=on "
                      f"Unity emergency={None if unity_state is None else unity_state.get('emergency_phase')}")
            elif command == "demo":
                run_demo(shared)
            elif command == "status":
                print_status(shared)
            elif command == "help":
                print_help()
            elif command == "quit":
                scenarios.stop(wait=True)
                can_player.stop()
                shared.running = False
            else:
                print("알 수 없는 명령입니다. help를 입력해 확인하세요.")
        except (ValueError, OSError, RuntimeError) as exception:
            print(f"입력 오류: {exception}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Unity vehicle bidirectional TCP server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--send-rate", type=float, default=20.0)
    parser.add_argument("--model-host", default="127.0.0.1")
    parser.add_argument("--model-port", type=int, help="Enable final-label input from the unmodified model (recommended: 5002)")
    parser.add_argument("--model-timeout", type=float, default=1.0, help="Seconds without a valid result before warning")
    parser.add_argument("--model-max-age", type=float, default=0.75, help="Maximum model prediction age in seconds")
    parser.add_argument("--no-console", action="store_true", help="Run continuously without interactive commands")
    data_dir = Path(__file__).resolve().parents[1] / "data"
    parser.add_argument("--can-normal-csv", type=Path, default=data_dir / "can_normal_60kmh_15s.csv")
    parser.add_argument("--can-caution-csv", type=Path, default=data_dir / "can_caution_drowsy_drift_15s.csv")
    parser.add_argument("--can-distraction-csv", type=Path, default=data_dir / "can_distraction_held_steer_15s.csv")
    parser.add_argument("--can-danger-csv", type=Path, default=data_dir / "can_danger_steer_accel_15s.csv")
    parser.add_argument("--can-max-steering-angle", type=float, default=30.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.can_max_steering_angle <= 0:
        raise ValueError("--can-max-steering-angle must be positive")
    model_config = None
    if args.model_port is not None:
        if args.model_host in {"localhost", "127.0.0.1", "::1"} and args.model_port == args.port:
            raise ValueError("Unity와 로컬 모델 서버는 다른 포트를 사용해야 합니다. --model-port 5002를 사용하세요.")
        model_config = ModelConnectionConfig(
            host=args.model_host, port=args.model_port,
            timeout_s=args.model_timeout, max_prediction_age_s=args.model_max_age,
        )
    shared = SharedData(model_input_enabled=model_config is not None)
    scenarios = ScenarioRunner(shared)
    can_adapter = CanAdapter(
        shared,
        CanPlaybackConfig(
            max_steering_angle_deg=args.can_max_steering_angle,
        ),
    )
    can_player = CanScenarioPlayer(
        can_adapter.on_start, can_adapter.on_sample, can_adapter.on_finish,
    )
    can_paths = {
        "normal": args.can_normal_csv,
        "caution": args.can_caution_csv,
        "distraction": args.can_distraction_csv,
        "danger": args.can_danger_csv,
    }
    server_thread = threading.Thread(
        target=server_loop,
        args=(args.host, args.port, shared, args.send_rate),
        daemon=True,
        name="UnityTcpServer",
    )
    server_thread.start()
    model_client = None
    if model_config is not None:
        model_client = ModelStateClient(
            model_config, shared.receive_model_result,
            unity_ready=lambda: shared.connected_address is not None,
        )
        model_client.start()
        print(f"[MODEL INPUT] {model_config.host}:{model_config.port}; waiting for Unity Play. "
              "Only final Normal/Caution/Danger labels are applied; state/scenario commands are disabled.")

    try:
        if args.no_console:
            while shared.running:
                time.sleep(0.2)
        else:
            console_loop(shared, scenarios, can_player, can_adapter, can_paths)
    except KeyboardInterrupt:
        pass
    finally:
        if model_client is not None:
            model_client.stop()
        scenarios.stop(wait=True)
        can_player.stop()
        shared.running = False
        server_thread.join(timeout=2.0)
        print("서버를 종료했습니다.")


if __name__ == "__main__":
    main()
