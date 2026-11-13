"""Final-label integration tests, including two real loopback TCP connections.

No inference dependency, model import or Unity Editor execution is required.
"""

import json
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from can_adapter import CanAdapter
from can_scenario import load_can_scenario
from model_state_client import ModelConnectionConfig, ModelStateClient, extract_model_state
from vehicle_tcp_server import SharedData, ScenarioRunner, handle_unity_client, main


def model_packet(state, **changes):
    packet = dict(type="vehicle_control", driver_state=state.capitalize(),
                  fusion_state=state.lower(), fusion_available=True,
                  vision_state="normal", vision_available=True,
                  prediction_age_seconds=0.01)
    packet.update(changes)
    return packet


def listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        sock.settimeout(1.5)
    except OSError:
        sock.close()
        raise
    return sock


class TcpHarness:
    def __init__(self):
        self.shared = SharedData(model_input_enabled=True)
        self.model_listener = listener()
        self.unity_listener = listener()
        self.unity = None
        self.model = None
        self.pending = b""
        self.client = ModelStateClient(
            ModelConnectionConfig(port=self.model_listener.getsockname()[1],
                                  timeout_s=0.3, reconnect_delay_s=0.05),
            self.shared.receive_model_result,
            lambda: self.shared.connected_address is not None,
        )
        self.thread = threading.Thread(target=self._accept_unity, daemon=True)
        self.thread.start()
        self.client.start()

    def _accept_unity(self):
        try:
            connection, address = self.unity_listener.accept()
            handle_unity_client(connection, address, self.shared, 20.0)
        except (OSError, ConnectionError):
            pass

    def connect(self):
        self.unity = socket.create_connection(self.unity_listener.getsockname(), timeout=1.0)
        self.unity.settimeout(0.1)
        self.feedback("Idle")
        self.reconnect_model()

    def reconnect_model(self):
        self.model, _ = self.model_listener.accept()
        self.model.settimeout(0.5)

    def send(self, packet):
        self.model.sendall((json.dumps(packet, ensure_ascii=False) + "\n").encode("utf-8"))

    def feedback(self, phase):
        packet = dict(type="vehicle_state", emergency_phase=phase, speed_kph=60.0)
        self.unity.sendall((json.dumps(packet) + "\n").encode())

    def receive_state(self, state):
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            while b"\n" in self.pending:
                line, self.pending = self.pending.split(b"\n", 1)
                packet = json.loads(line)
                if packet["driver_state"] == state:
                    return packet
            try:
                chunk = self.unity.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                break
            self.pending += chunk
        raise AssertionError(f"Unity did not receive {state}")

    def wait_for_status(self, status):
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            if self.shared.model_input_status == status:
                return
            time.sleep(0.005)
        raise AssertionError(f"Model status is {self.shared.model_input_status}, expected {status}")

    def close(self):
        self.shared.running = False
        self.client.stop()
        for sock in (self.unity, self.model, self.unity_listener, self.model_listener):
            if sock is not None:
                sock.close()
        self.thread.join(timeout=2.0)


class ModelIntegrationTests(unittest.TestCase):
    def setUp(self):
        quiet = patch("builtins.print")
        quiet.start()
        self.addCleanup(quiet.stop)

    def test_accepts_three_labels_and_uses_final_fusion_not_vision(self):
        for state in ("normal", "CAUTION", "Danger"):
            with self.subTest(state=state):
                self.assertEqual(extract_model_state(model_packet(state)), state.capitalize())
                self.assertEqual(extract_model_state(dict(type="vehicle_control", driver_state=state)),
                                 state.capitalize())
        self.assertEqual(extract_model_state(model_packet("Caution", vision_state="danger")), "Caution")
        self.assertEqual(extract_model_state(model_packet("Caution", vision_state="unavailable",
                                                          vision_available=False)), "Caution")

    def test_unavailable_stale_invalid_and_conflicting_labels_are_rejected(self):
        cases = [None, [], {}, dict(type="vehicle_state", driver_state="Danger"),
                 model_packet("Danger", fusion_available=False),
                 model_packet("Caution", fusion_state="unavailable", fusion_available=False),
                 model_packet("Normal", driver_state="Danger"),
                 dict(type="vehicle_control", driver_state="Caution", vision_available=False)]
        for state in ("unavailable", "D", "", 1, None):
            cases.append(model_packet("Normal", fusion_state=state))
        for age in (-0.1, 0.76, float("nan"), float("inf"), True, None, "0.1"):
            cases.append(model_packet("Danger", prediction_age_seconds=age))
        for packet in cases:
            with self.subTest(packet=packet):
                self.assertIsNone(extract_model_state(packet))

    def test_configuration_rejects_invalid_transport_settings(self):
        for changes in (dict(port=0), dict(port=65536), dict(timeout_s=0),
                        dict(max_prediction_age_s=float("nan")), dict(reconnect_delay_s=-1)):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                ModelConnectionConfig(**changes)

    def test_model_mode_owns_state_and_preserves_existing_vehicle_modes(self):
        shared = SharedData(model_input_enabled=True)
        shared.update_command(control_mode="manual", throttle=0.37, steering=-0.08)
        shared.receive_model_result("Normal", "active")
        shared.receive_model_result("Caution", "active")
        command, _ = shared.snapshot()
        self.assertEqual((command.control_mode, command.throttle, command.steering),
                         ("manual", 0.37, -0.08))
        with self.assertRaises(ValueError):
            shared.set_driver_state("Normal")
        with self.assertRaises(ValueError):
            ScenarioRunner(shared).start("danger")
        shared.receive_model_result(None, "connection_lost")
        self.assertEqual(shared.snapshot()[0].driver_state, "Caution")
        shared.receive_model_result("Danger", "active")
        shared.receive_model_result(None, "connection_lost")
        self.assertEqual(shared.snapshot()[0].driver_state, "Danger")

    def test_short_danger_is_delivered_until_unity_emergency_acknowledges(self):
        shared = SharedData(model_input_enabled=True)
        shared.receive_model_result("Danger", "active")
        shared.receive_model_result("Normal", "active")
        self.assertEqual(json.loads(shared.next_command_json())["driver_state"], "Danger")
        shared.set_state(dict(emergency_phase="Idle"))
        self.assertEqual(json.loads(shared.next_command_json())["driver_state"], "Danger")
        shared.set_state(dict(emergency_phase="SignalAndSlow"))
        self.assertEqual(json.loads(shared.next_command_json())["driver_state"], "Normal")
        self.assertTrue(shared.can_control_locked)

    def test_model_results_activate_caution_can_limits_and_danger_priority(self):
        path = Path(__file__).resolve().parents[1] / "data/can_danger_steer_accel_15s.csv"
        scenario = load_can_scenario(path)
        for state in ("Normal", "Caution"):
            shared = SharedData(model_input_enabled=True)
            shared.receive_model_result(state, "active")
            shared.set_state(dict(speed_kph=60.0, heading_deg=0.0, position=dict(x=0, z=0)))
            adapter = CanAdapter(shared)
            adapter.on_start(scenario)
            corrected = limited = False
            for row in scenario.rows[:-1]:
                adapter.on_sample(row)
                command, _ = shared.snapshot()
                corrected |= command.steer_correction_active
                limited |= command.accel_limit_active
                self.assertEqual(command.driver_state, state)
            self.assertEqual((corrected, limited), (state == "Caution", state == "Caution"))
            shared.receive_model_result("Danger", "active")
            adapter.on_sample(scenario.rows[-1])
            command, _ = shared.snapshot()
            self.assertTrue(shared.can_control_locked)
            self.assertEqual((command.throttle, command.steering), (0.0, 0.0))
            self.assertFalse(command.steer_correction_active or command.accel_limit_active)
            self.assertTrue(command.hazard)

    def harness(self):
        harness = TcpHarness()
        self.addCleanup(harness.close)
        return harness

    def test_tcp_waits_for_unity_and_transmits_only_final_states(self):
        harness = self.harness()
        harness.model_listener.settimeout(0.1)
        with self.assertRaises(socket.timeout):
            harness.model_listener.accept()
        harness.model_listener.settimeout(1.5)
        harness.connect()
        payload = (json.dumps(model_packet("Normal", reasons=["한글"],
                           throttle=1.0, steering=1.0, control_mode="remote", hazard=True),
                           ensure_ascii=False) + "\n").encode("utf-8")
        split = payload.index("한".encode()) + 1
        harness.model.sendall(payload[:split])
        harness.model.sendall(payload[split:])
        normal = harness.receive_state("Normal")
        self.assertEqual((normal["control_mode"], normal["throttle"], normal["steering"], normal["hazard"]),
                         ("cruise", 0.0, 0.0, False))
        harness.send(model_packet("Caution", vision_state="danger"))
        harness.receive_state("Caution")
        # Two complete packets in one receive must not swallow the brief Danger.
        harness.model.sendall(b"not-json\n[]\n" + b"".join(
            (json.dumps(model_packet(state)) + "\n").encode() for state in ("Danger", "Normal")))
        danger = harness.receive_state("Danger")
        self.assertTrue(danger["hazard"])
        harness.feedback("SignalAndSlow")
        harness.receive_state("Normal")

    def test_tcp_watchdog_disconnect_reconnect_and_unavailable(self):
        harness = self.harness()
        harness.connect()
        harness.send(model_packet("Normal"))
        harness.receive_state("Normal")
        # Noise must not reset the prediction watchdog.
        harness.model.sendall(b"bad-json\n{\"type\":\"vehicle_state\"}\n")
        harness.wait_for_status("prediction_timeout")
        harness.receive_state("Caution")
        harness.send(model_packet("Normal"))
        harness.receive_state("Normal")
        harness.send(model_packet("Caution", fusion_state="unavailable", fusion_available=False))
        harness.wait_for_status("prediction_unavailable")
        harness.receive_state("Caution")
        harness.model.close()
        harness.wait_for_status("connection_lost")
        harness.reconnect_model()
        harness.send(model_packet("Normal"))
        harness.receive_state("Normal")
        harness.send(model_packet("Danger"))
        harness.receive_state("Danger")
        harness.model.close()
        harness.wait_for_status("connection_lost")
        self.assertEqual(harness.shared.snapshot()[0].driver_state, "Danger")

    def test_unity_disconnect_closes_model_stream(self):
        harness = self.harness()
        harness.connect()
        harness.send(model_packet("Normal"))
        harness.receive_state("Normal")
        harness.unity.close()
        harness.wait_for_status("waiting_for_unity")
        self.assertEqual(harness.model.recv(1), b"")

    def test_oversized_model_line_is_disconnected(self):
        harness = self.harness()
        harness.connect()
        harness.send(model_packet("Normal"))
        harness.receive_state("Normal")
        harness.model.sendall(b"x" * 262145)
        harness.wait_for_status("connection_lost")
        self.assertEqual(harness.shared.snapshot()[0].driver_state, "Caution")

    def test_cli_wires_client_without_importing_or_modifying_model(self):
        with (patch("sys.argv", ["vehicle_tcp_server.py", "--model-port", "5002"]),
              patch("vehicle_tcp_server.server_loop"),
              patch("vehicle_tcp_server.console_loop") as console,
              patch("vehicle_tcp_server.ModelStateClient") as client):
            main()
        shared = console.call_args.args[0]
        self.assertTrue(shared.model_input_enabled)
        self.assertEqual(client.call_args.args[0].port, 5002)
        client.return_value.start.assert_called_once()
        client.return_value.stop.assert_called_once()
        with (patch("sys.argv", ["vehicle_tcp_server.py", "--model-port", "5001"]),
              self.assertRaisesRegex(ValueError, "다른 포트")):
            main()

    def test_headless_cli_process_receives_model_and_serves_unity(self):
        model_server = listener()
        self.addCleanup(model_server.close)
        port_probe = listener()
        unity_port = port_probe.getsockname()[1]
        port_probe.close()
        process = subprocess.Popen(
            [sys.executable, "-B", str(Path(__file__).with_name("vehicle_tcp_server.py")),
             "--host", "127.0.0.1", "--port", str(unity_port),
             "--model-port", str(model_server.getsockname()[1]), "--no-console"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            deadline = time.monotonic() + 2.0
            while True:
                try:
                    unity = socket.create_connection(("127.0.0.1", unity_port), timeout=0.1)
                    break
                except OSError:
                    if time.monotonic() >= deadline or process.poll() is not None:
                        raise AssertionError("Headless vehicle server did not start")
                    time.sleep(0.01)
            with unity, model_server.accept()[0] as model:
                unity.settimeout(1.0)
                pending = b""
                for state in ("Normal", "Caution", "Danger"):
                    model.sendall((json.dumps(model_packet(state)) + "\n").encode())
                    deadline = time.monotonic() + 1.5
                    while time.monotonic() < deadline:
                        while b"\n" not in pending:
                            chunk = unity.recv(65536)
                            self.assertTrue(chunk)
                            pending += chunk
                        line, pending = pending.split(b"\n", 1)
                        received = json.loads(line)
                        if received["driver_state"] == state:
                            break
                    else:
                        self.fail(f"Headless server did not deliver {state}")
                    self.assertEqual(received["control_mode"], "cruise")
                self.assertIsNone(process.poll())
            process.send_signal(signal.SIGINT)
            output, _ = process.communicate(timeout=3.0)
            self.assertEqual(process.returncode, 0, output)
            self.assertIn("[MODEL INPUT] active", output)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=3.0)


if __name__ == "__main__":
    unittest.main()
