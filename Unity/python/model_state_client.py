"""Read final driver labels from the unmodified model's NDJSON TCP server.

Only classification results cross this boundary. Model packets never supply
vehicle controls, CAN inputs, warning acknowledgements or stop confirmations.
"""

from __future__ import annotations

import json
import math
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ModelConnectionConfig:
    host: str = "127.0.0.1"
    port: int = 5002
    timeout_s: float = 1.0
    max_prediction_age_s: float = 0.75
    reconnect_delay_s: float = 1.0
    max_line_bytes: int = 262144

    def __post_init__(self) -> None:
        if not self.host or not 1 <= self.port <= 65535:
            raise ValueError("Invalid model TCP host/port")
        for value in (self.timeout_s, self.max_prediction_age_s, self.reconnect_delay_s):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Model timeouts must be finite and positive")
        if self.max_line_bytes <= 0:
            raise ValueError("Model NDJSON line limit must be positive")


def extract_model_state(packet: Any, max_age_s: float = 0.75) -> str | None:
    """Return only a fresh final label, never an individual vision/bio label.

    The supplied model maps unavailable to driver_state=Caution for older
    clients. Its availability flags must therefore be checked before that label.
    A valid fused result may exist even when vision alone is unavailable.
    """
    if not isinstance(packet, dict) or packet.get("type") != "vehicle_control":
        return None
    if "fusion_state" in packet:
        if packet.get("fusion_available") is not True:
            return None
        label = packet["fusion_state"]
    else:
        if packet.get("vision_available") is False:
            return None
        label = packet.get("driver_state")
    if "prediction_age_seconds" in packet:
        age = packet["prediction_age_seconds"]
        if (isinstance(age, bool) or not isinstance(age, (int, float))
                or not math.isfinite(age) or not 0 <= age <= max_age_s):
            return None
    if not isinstance(label, str):
        return None
    state = label.strip().capitalize()
    if state not in {"Normal", "Caution", "Danger"}:
        return None
    compatibility_label = packet.get("driver_state", label)
    if not isinstance(compatibility_label, str) or compatibility_label.strip().capitalize() != state:
        return None
    return state


class ModelStateClient:
    """Reconnect in the background and consume complete, bounded UTF-8 lines.

    Wait for the downstream Unity connection so a recorded model scenario does
    not start playing before Unity Play. Disconnect upstream when Unity leaves;
    the supplied model pauses at its existing client-connection wait point.
    """

    def __init__(
        self, config: ModelConnectionConfig,
        on_result: Callable[[str | None, str], None],
        unity_ready: Callable[[], bool],
    ) -> None:
        self.config = config
        self.on_result = on_result
        self.unity_ready = unity_ready
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._connection: socket.socket | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("Model client already started")
        self._thread = threading.Thread(target=self._run, daemon=True, name="ModelStateClient")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        connection = self._connection
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self.unity_ready():
                self.on_result(None, "waiting_for_unity")
                self._stop.wait(0.05)
                continue
            try:
                with socket.create_connection((self.config.host, self.config.port), timeout=0.5) as connection:
                    self._connection = connection
                    connection.settimeout(0.1)
                    connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    self.on_result(None, "waiting_for_prediction")
                    self._receive(connection)
            except (OSError, ConnectionError):
                if not self._stop.is_set():
                    self.on_result(None, "connection_lost")
            finally:
                self._connection = None
            self._stop.wait(self.config.reconnect_delay_s)

    def _receive(self, connection: socket.socket) -> None:
        pending = bytearray()
        last_packet_at = time.monotonic()
        while not self._stop.is_set():
            if not self.unity_ready():
                self.on_result(None, "waiting_for_unity")
                return
            try:
                chunk = connection.recv(65536)
            except socket.timeout:
                chunk = None
            if chunk == b"":
                raise ConnectionError("Model closed the connection")
            if chunk:
                pending.extend(chunk)
                while b"\n" in pending:
                    line, _, remainder = pending.partition(b"\n")
                    pending = bytearray(remainder)
                    if len(line) > self.config.max_line_bytes:
                        raise ConnectionError("Model NDJSON line limit exceeded")
                    try:
                        packet = json.loads(line.decode("utf-8"))
                    except (ValueError, UnicodeError):
                        continue
                    if not isinstance(packet, dict) or packet.get("type") != "vehicle_control":
                        continue
                    last_packet_at = time.monotonic()
                    state = extract_model_state(packet, self.config.max_prediction_age_s)
                    if state is None:
                        self.on_result(None, "prediction_unavailable")
                    else:
                        self.on_result(state, "active")
                if len(pending) > self.config.max_line_bytes:
                    raise ConnectionError("Model NDJSON line limit exceeded")
            if time.monotonic() - last_packet_at > self.config.timeout_s:
                self.on_result(None, "prediction_timeout")
