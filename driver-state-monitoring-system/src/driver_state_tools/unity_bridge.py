"""State-only TCP integration for the supplied Unity.zip simulator.

No throttle, brake, steering, mode, hazard or e-Call commands are sent here.
Unity owns its emergency manoeuvre and its latched emergency state.
"""
from __future__ import annotations

import copy
import json
import math
import select
import socket
import threading
import time
from dataclasses import fields
from pathlib import Path
from collections import deque
from typing import Any, Callable

from .decision_engine import Decision, DriverState, FeatureFrame, TemporalDecisionEngine


def vehicle_control_packet(decision: Decision, sequence: int, *, clip_id: str = "",
                           age: float = 0.0, stale_after: float = 0.75) -> dict[str, Any]:
    available = decision.state is not DriverState.UNAVAILABLE and age <= stale_after
    state = decision.state.value if available else "unavailable"
    # Old Unity supports only three labels. An unknown prediction falls back to
    # caution, never normal. The supplied compatibility patch displays unavailable.
    driver_state = state.capitalize() if available else "Caution"
    reasons = list(decision.reasons)
    if age > stale_after:
        reasons = ["prediction_stale"]
    packet = dict(type="vehicle_control", sequence=sequence, driver_state=driver_state,
                vision_state=state, vision_available=available, clip_id=clip_id,
                source_timestamp=decision.timestamp, prediction_age_seconds=round(age, 6),
                caution_seconds=decision.durations.get("caution_continuous", 0.0) if available else 0.0,
                reasons=reasons)
    packet["durations"] = dict(decision.durations) if available else {}
    packet["judgment_mode"] = decision.metadata.get("mode", "legacy")
    packet["readiness"] = [dict(feature=k,status=v) for k,v in decision.metadata.get("feature_status", {}).items()]
    packet["full_system_ready"] = decision.metadata.get("full_system_ready", False)
    packet["warning_request_id"] = decision.metadata.get("warning_request_id") if available else None
    packet["warning_issued_at"] = decision.metadata.get("warning_issued_at") if available else None
    packet["warning_source"] = decision.metadata.get("warning_source") if available else None
    packet["warning_display_confirmed"] = bool(available and decision.metadata.get("warning_issued_at") is not None)
    # Unity JsonUtility has a non-nullable double; the boolean disambiguates 0.
    if packet["warning_issued_at"] is None:
        packet["warning_issued_at"] = 0.0
    packet["caution_timer_policy"] = ("same_collapse_only" if decision.metadata.get("mode") == "prototype" else
                                       "not_used" if decision.metadata.get("mode") == "literature" else "legacy")
    if decision.metadata.get("mode") == "prototype":
        packet["caution_seconds"] = decision.durations.get("collapse_after_caution",0.) if available else 0.
    packet["observation_status"] = decision.metadata.get("observation_status", {}) if available else {}
    packet["stop_basis"] = decision.metadata.get("stop_basis") if available else None
    packet["decision_phase"] = decision.metadata.get("decision_phase") if available else "unavailable"
    packet["attention_status"] = decision.metadata.get("attention_status") if available else "unavailable"
    packet["pending_events"] = decision.metadata.get("pending_events", []) if available else []
    packet["stop_requested"] = bool(available and decision.metadata.get("stop_requested", decision.state is DriverState.DANGER))
    return packet


def rejudge_recording(path: Path, rules: dict[str, Any]) -> list[tuple[str, Decision]]:
    """Use actual recorded features and current rules, never scenario labels."""
    if rules.get("mode") == "prototype":
        from .prototype_engine import PrototypeDecisionEngine, PrototypeFrame
        engine = PrototypeDecisionEngine(rules)
        result = []
        clip = None
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                item = json.loads(line)
                current = str(item.get("clip_id",path.stem))
                if clip is not None and current != clip:
                    raise ValueError("One replay must contain only one clip")
                clip = current
                if not isinstance(item.get("prototype_frame"),dict):
                    raise ValueError("Prototype replay requires prototype observations")
                result.append((clip,engine.update(PrototypeFrame(**item["prototype_frame"]))))
        if not result:
            raise ValueError("No recorded prototype observations")
        return result
    if rules.get("mode") == "literature":
        from .literature_engine import LiteratureDecisionEngine, LiteratureFrame
        engine = LiteratureDecisionEngine(rules)
        output = []
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                record = json.loads(line)
                if not isinstance(record.get("literature_frame"), dict):
                    raise ValueError("Literature replay requires recorded literature observations; use explicit legacy rules for old logs")
                frame = LiteratureFrame(**record["literature_frame"])
                metadata = record.get("decision", {}).get("metadata", {})
                issued = metadata.get("warning_issued_at")
                if issued is not None:
                    engine.confirm_warning(metadata.get("warning_request_id"), issued, "recorded_warning_replay")
                output.append((str(record.get("clip_id", path.stem)), engine.update(frame)))
        if not output:
            raise ValueError("No recorded literature observations")
        return output
    engine = TemporalDecisionEngine(rules)
    keys = {f.name for f in fields(FeatureFrame)}
    output = []
    clip_id = None
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("record must be a JSON object")
                timestamp = record["timestamp"]
                if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp) or timestamp < 0:
                    raise ValueError("invalid video timestamp")
                current_id = str(record.get("clip_id", path.stem))
                if clip_id is not None and current_id != clip_id:
                    raise ValueError("one replay must contain only one source clip")
                clip_id = current_id
                derived = record["derived"]
                if not isinstance(derived, dict):
                    raise ValueError("derived must be a JSON object")
                values = {k:v for k,v in derived.items() if k in keys}
                values.update(timestamp=timestamp, yawning=derived.get("mouth_open", derived.get("yawning")),
                              warning_active=False, response_received=None)
                for key, value in values.items():
                    if key != "timestamp" and value is not None and not isinstance(value, bool):
                        raise ValueError(f"{key} must be boolean or null")
                output.append((clip_id, engine.update(FeatureFrame(**values))))
            except (ValueError, TypeError, KeyError) as error:
                raise ValueError(f"{path.name}:{number}: {error}") from error
    if not output:
        raise ValueError("No recorded feature frames found")
    return output


class UnityStateServer:
    """One Unity TCP client, bounded NDJSON, heartbeat and freshness checking.

    A live vision producer can call publish() after every actual decision.
    Heartbeats do not count as new observations or advance the decision engine.
    """
    def __init__(self, host: str = "127.0.0.1", port: int = 5001, rate_hz: float = 20.0,
                 stale_after: float = 0.75, log_path: Path | None = None,
                 clock: Callable[[], float] = time.monotonic):
        if not 1 <= rate_hz <= 60 or not math.isfinite(stale_after) or stale_after <= 0:
            raise ValueError("rate_hz must be 1..60 and stale_after must be positive")
        if not 0 <= port <= 65535:
            raise ValueError("port must be 0..65535")
        self.host, self.port, self.rate_hz = host, port, rate_hz
        self.stale_after, self.clock = stale_after, clock
        self.log_path = log_path
        self.connected = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._decision = Decision(0.0, DriverState.UNAVAILABLE, ["no_prediction_yet"])
        self._clip_id = ""
        self._published_at: float | None = None
        self._sequence = 0
        self._revision = 0
        self._sent_revision = -1
        self._sent_condition = threading.Condition(self._lock)
        self._latest_vehicle_state = None
        self._warning_events = deque(maxlen=128)
        self._warning_requests: dict[str, str] = {}
        self._critical_pending = None
        self._listener = None
        self._thread = None
        self._log_stream = None

    def publish(self, decision: Decision, clip_id: str = "") -> int:
        with self._lock:
            if clip_id != self._clip_id:
                self._critical_pending = None
            self._decision = copy.deepcopy(decision)
            self._clip_id = clip_id
            self._published_at = self.clock()
            self._revision += 1
            # Do not lose a brief, genuine Danger when inference outruns TCP.
            # Its ORIGINAL publication time still participates in freshness checks.
            if decision.state is DriverState.DANGER and self._critical_pending is None:
                self._critical_pending = (copy.deepcopy(decision), clip_id, self._published_at, self._revision)
            request = decision.metadata.get("warning_request_id")
            if request:
                self._warning_requests[str(request)] = clip_id
                while len(self._warning_requests) > 32:
                    del self._warning_requests[next(iter(self._warning_requests))]
            return self._revision

    def warning_events(self, clip_id: str) -> list[dict[str, Any]]:
        with self._lock:
            result = [e for e in self._warning_events if e["clip_id"] == clip_id]
            self._warning_events.clear()
            return result

    def mark_unavailable(self, reason: str) -> None:
        with self._lock:
            if reason == "new_video_session":
                self._critical_pending = None
            self._decision = Decision(self._decision.timestamp, DriverState.UNAVAILABLE, [reason])
            self._published_at = self.clock()
            self._revision += 1

    def wait_until_sent(self, revision: int, timeout: float = 1.0) -> bool:
        with self._sent_condition:
            return self._sent_condition.wait_for(lambda: self._sent_revision >= revision, timeout)

    def next_packet(self) -> dict[str, Any]:
        with self._lock:
            self._sequence += 1
            decision,clip,published,revision = (self._critical_pending if self._critical_pending is not None
                                                else (self._decision,self._clip_id,self._published_at,self._revision))
            age = max(0.0, self.clock() - published) if published is not None else 0.0
            packet = vehicle_control_packet(decision, self._sequence, clip_id=clip,
                                            age=age, stale_after=self.stale_after)
            packet["prediction_sequence"] = revision
            return packet

    @property
    def latest_vehicle_state(self) -> dict[str, Any] | None:
        with self._lock:
            return copy.deepcopy(self._latest_vehicle_state)

    def _log(self, event: str, **data: Any) -> None:
        if self._log_stream is not None:
            with self._log_lock:
                self._log_stream.write(json.dumps(dict(event=event, monotonic=self.clock(), **data), ensure_ascii=False) + "\n")
                self._log_stream.flush()

    def start(self) -> "UnityStateServer":
        if self._thread is not None:
            raise RuntimeError("Create a new server instance for each session")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self.host, self.port))
            listener.listen(1)
            listener.setblocking(False)
            if self.log_path is not None:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                self._log_stream = self.log_path.open("x", encoding="utf-8")
        except Exception:
            listener.close()
            raise
        self._listener = listener
        self.port = listener.getsockname()[1]
        self._thread = threading.Thread(target=self._run, daemon=True, name="UnityStateServer")
        self._thread.start()
        return self

    def _run(self) -> None:
        client = None
        pending = bytearray()
        next_send = self.clock()
        try:
            while not self._stop.is_set():
                readers = [self._listener] + ([client] if client is not None else [])
                readable, _, _ = select.select(readers, [], [], min(.05, max(0, next_send - self.clock())) if client else .05)
                if self._listener in readable:
                    incoming, address = self._listener.accept()
                    if client is not None:
                        incoming.close()
                    else:
                        client = incoming
                        client.settimeout(.1)
                        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                        pending.clear()
                        next_send = self.clock()
                        self._log("connected", address=list(address))
                        self.connected.set()
                try:
                    if client is not None and client in readable:
                        data = client.recv(8192)
                        if not data:
                            raise ConnectionError("Unity disconnected")
                        pending.extend(data)
                        if len(pending) > 262144:
                            raise ConnectionError("Unity receive buffer limit exceeded")
                        while b"\n" in pending:
                            line, _, remainder = pending.partition(b"\n")
                            pending = bytearray(remainder)
                            if not line.strip():
                                continue
                            try:
                                message = json.loads(line.decode("utf-8"))
                            except (ValueError, UnicodeError):
                                self._log("invalid_vehicle_json")
                                continue
                            if isinstance(message, dict) and message.get("type") == "vehicle_state":
                                with self._lock:
                                    self._latest_vehicle_state = message
                                self._log("vehicle_state", payload=message)
                            elif isinstance(message, dict) and message.get("type") == "driver_warning_ack":
                                request = message.get("warning_request_id")
                                clip = message.get("clip_id")
                                with self._lock:
                                    valid = (isinstance(request, str) and isinstance(clip, str)
                                             and clip == self._clip_id
                                             and self._warning_requests.get(request) == clip)
                                    if valid:
                                        self._warning_events.append(dict(warning_request_id=request, clip_id=clip,
                                                                         received_monotonic=self.clock(), source="unity_ui"))
                                self._log("warning_ack" if valid else "invalid_warning_ack", payload=message)
                    if client is not None and self.clock() >= next_send:
                        packet = self.next_packet()
                        client.sendall((json.dumps(packet, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
                        with self._sent_condition:
                            if self._critical_pending is not None and self._critical_pending[3] == packet["prediction_sequence"]:
                                self._critical_pending = None
                            if "prediction_stale" not in packet["reasons"]:
                                self._sent_revision = max(self._sent_revision, packet["prediction_sequence"])
                            self._sent_condition.notify_all()
                        self._log("vehicle_control", payload=packet)
                        next_send = self.clock() + 1 / self.rate_hz
                except (ConnectionError, OSError):
                    if client is not None:
                        client.close()
                        client = None
                    self.connected.clear()
                    pending.clear()
                    self._log("disconnected")
        finally:
            self.connected.clear()
            if client is not None:
                client.close()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._listener is not None:
            self._listener.close()
        if self._log_stream is not None:
            self._log_stream.close()
            self._log_stream = None

    def __enter__(self) -> "UnityStateServer":
        return self.start()

    def __exit__(self, *args: Any) -> None:
        self.stop()


def replay_decisions(server: UnityStateServer, decisions: list[tuple[str, Decision]],
                     cancelled: threading.Event | None = None,
                     on_decision: Callable[[Decision], None] | None = None) -> None:
    """Replay source-video time at 1x. Pause the replay while disconnected."""
    cancelled = cancelled or threading.Event()
    started = None
    last_revision = None
    for clip_id, decision in decisions:
        while not cancelled.is_set():
            if not server.connected.is_set():
                pause_start = time.monotonic()
                while not server.connected.wait(.05) and not cancelled.is_set():
                    pass
                if started is not None:
                    started += time.monotonic() - pause_start
                continue
            if started is None:
                started = time.monotonic() - decisions[0][1].timestamp
            remaining = started + decision.timestamp - time.monotonic()
            if remaining <= 0:
                break
            cancelled.wait(min(.02, remaining))
        if cancelled.is_set():
            return
        last_revision = server.publish(decision, clip_id)
        if on_decision is not None:
            on_decision(decision)
    # A running publisher may republish current decisions while measuring; this
    # file replay has ended and must not masquerade as continuing live vision.
    sent = last_revision is not None and server.wait_until_sent(last_revision)
    server.mark_unavailable("recording_finished" if sent else "recording_finished_without_delivery")
