from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
import math
from typing import Any


class DriverState(str, Enum):
    NORMAL = "normal"
    CAUTION = "caution"
    DANGER = "danger"
    UNAVAILABLE = "unavailable"


@dataclass
class FeatureFrame:
    timestamp: float
    face_readable: bool
    eyes_readable: bool
    pose_readable: bool
    eyes_closed: bool | None = None
    attention_away: bool | None = None
    head_abnormal: bool | None = None
    yawning: bool | None = None
    perclos_high: bool | None = None
    torso_lean: bool | None = None
    collapse_posture: bool | None = None
    distress_gesture: bool | None = None
    long_blinks_repeated: bool | None = None
    nods_repeated: bool | None = None
    warning_active: bool = False
    # None means no response measurement exists. False must only be supplied by
    # an explicit button/interaction timeout, never inferred from video.
    response_received: bool | None = None


@dataclass
class Decision:
    timestamp: float
    state: DriverState
    reasons: list[str] = field(default_factory=list)
    durations: dict[str, float] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


DEFAULT_RULES: dict[str, float | bool | str] = {
    "eyes_closed_caution_seconds": 1.0,
    "attention_away_caution_seconds": 1.5,
    "attention_repeat_count": 3,
    "attention_repeat_window_seconds": 10.0,
    "head_abnormal_caution_seconds": 2.0,
    "yawn_caution_seconds": 1.5,
    "perclos_caution_seconds": 1.0,
    "torso_lean_caution_seconds": 2.0,
    "collapse_caution_seconds": 0.8,
    "distress_gesture_caution_seconds": 1.0,
    "combined_drowsy_caution_seconds": 0.4,
    "history_support_seconds": 0.5,
    "danger_policy": "sustained_caution",
    "caution_to_danger_seconds": 3.0,
    "caution_continuity_gap_seconds": 0.5,
    "danger_after_warning_seconds": 5.0,
    "signal_gap_tolerance_seconds": 0.2,
    "unknown_if_face_unreadable": True,
    "attention_episode_min_seconds": 0.35,
    "attention_cumulative_caution_seconds": 4.0,
    "recovery_seconds": 0.3,
}


class TemporalDecisionEngine:
    """Prototype rule engine; thresholds are configuration, not clinical facts."""

    def __init__(self, rules: dict[str, Any] | None = None) -> None:
        self.rules = {**DEFAULT_RULES, **(rules or {})}
        if self.rules["danger_policy"] not in ("sustained_caution", "external_response"):
            raise ValueError("danger_policy must be sustained_caution or external_response")
        if any(not math.isfinite(float(self.rules[key])) or float(self.rules[key]) <= 0
               for key in ("caution_to_danger_seconds", "caution_continuity_gap_seconds")):
            raise ValueError("caution escalation times must be positive")
        self._caution_started: float | None = None
        self._caution_last_seen: float | None = None
        self._starts: dict[str, float | None] = {
            "eyes_closed": None,
            "attention_away": None,
            "head_abnormal": None,
            "yawning": None,
            "perclos_high": None,
            "torso_lean": None,
            "collapse_posture": None,
            "distress_gesture": None,
            "warning_active": None,
        }
        self._last_timestamp: float | None = None
        self._last_true: dict[str, float | None] = {key: None for key in self._starts}
        self._attention_events: deque[float] = deque()
        self._previous_attention: bool | None = None
        self._attention_counted = False
        self._attention_intervals: deque[tuple[float, float]] = deque()
        self._healthy_since: float | None = None
        self._was_caution = False
        self._recovery_fields: set[str] = set()

    def reset(self) -> None:
        self._caution_started = None
        self._caution_last_seen = None
        for key in self._starts:
            self._starts[key] = None
            self._last_true[key] = None
        self._last_timestamp = None
        self._attention_events.clear()
        self._previous_attention = None
        self._attention_counted = False
        self._attention_intervals.clear()
        self._healthy_since = None
        self._was_caution = False
        self._recovery_fields.clear()

    def _update_timer(self, name: str, active: bool | None, timestamp: float) -> float:
        if active is True:
            if self._starts[name] is None:
                self._starts[name] = timestamp
            self._last_true[name] = timestamp
            return max(0.0, timestamp - float(self._starts[name]))
        last_true = self._last_true[name]
        if (
            active is None
            and self._starts[name] is not None
            and last_true is not None
            and timestamp - last_true <= float(self.rules["signal_gap_tolerance_seconds"])
        ):
            return max(0.0, last_true - float(self._starts[name]))
        self._starts[name] = None
        self._last_true[name] = None
        return 0.0

    def update(self, frame: FeatureFrame) -> Decision:
        """Escalate a continuously observed caution, not a raw symptom timer.

        Normal, unavailable, and long observation gaps break continuity.
        Recovery debounce is still caution and therefore counts until normal.
        This is a simulator rule, not an inference of unconsciousness.
        """
        decision = self._update_base(frame)
        elapsed = 0.0
        if decision.state is DriverState.CAUTION:
            if (self._caution_started is None or self._caution_last_seen is None or
                    frame.timestamp - self._caution_last_seen > float(self.rules["caution_continuity_gap_seconds"])):
                self._caution_started = frame.timestamp
            self._caution_last_seen = frame.timestamp
            elapsed = frame.timestamp - self._caution_started
            if (self.rules["danger_policy"] == "sustained_caution" and
                    elapsed + 1e-9 >= float(self.rules["caution_to_danger_seconds"])):
                decision.state = DriverState.DANGER
                decision.reasons.append("caution_persisted")
        else:
            self._caution_started = self._caution_last_seen = None
        decision.durations["caution_continuous"] = elapsed
        decision.durations["caution_to_danger_seconds"] = float(self.rules["caution_to_danger_seconds"])
        return decision

    def _update_base(self, frame: FeatureFrame) -> Decision:
        if self._last_timestamp is not None and frame.timestamp < self._last_timestamp:
            raise ValueError("timestamps must be monotonic")
        previous_time = self._last_timestamp
        self._last_timestamp = frame.timestamp
        repeat_window = float(self.rules["attention_repeat_window_seconds"])
        while self._attention_events and self._attention_events[0] < frame.timestamp - repeat_window:
            self._attention_events.popleft()
        if (previous_time is not None and self._previous_attention is True
                and frame.attention_away is True and frame.timestamp - previous_time <= 0.35):
            self._attention_intervals.append((previous_time, frame.timestamp))
        self._previous_attention = frame.attention_away
        durations = {
            "eyes_closed": self._update_timer("eyes_closed", frame.eyes_closed, frame.timestamp),
            "attention_away": self._update_timer("attention_away", frame.attention_away, frame.timestamp),
            "head_abnormal": self._update_timer("head_abnormal", frame.head_abnormal, frame.timestamp),
            "yawning": self._update_timer("yawning", frame.yawning, frame.timestamp),
            "perclos_high": self._update_timer("perclos_high", frame.perclos_high, frame.timestamp),
            "torso_lean": self._update_timer("torso_lean", frame.torso_lean, frame.timestamp),
            "collapse_posture": self._update_timer("collapse_posture", frame.collapse_posture, frame.timestamp),
            "distress_gesture": self._update_timer("distress_gesture", frame.distress_gesture, frame.timestamp),
            "warning_active": self._update_timer("warning_active", frame.warning_active, frame.timestamp),
        }
        thresholds = {
            "eyes_closed": float(self.rules["eyes_closed_caution_seconds"]),
            "attention_away": float(self.rules["attention_away_caution_seconds"]),
            "head_abnormal": float(self.rules["head_abnormal_caution_seconds"]),
            "yawning": float(self.rules["yawn_caution_seconds"]),
            "torso_lean": float(self.rules["torso_lean_caution_seconds"]),
            "collapse_posture": float(self.rules["collapse_caution_seconds"]),
            "distress_gesture": float(self.rules["distress_gesture_caution_seconds"]),
        }
        # A brief threshold flicker must not count as a separate glance.
        if frame.attention_away is False or durations["attention_away"] == 0:
            self._attention_counted = False
        if (frame.attention_away is True and not self._attention_counted
                and durations["attention_away"] >= float(self.rules["attention_episode_min_seconds"])):
            self._attention_events.append(frame.timestamp)
            self._attention_counted = True
        while self._attention_intervals and self._attention_intervals[0][1] < frame.timestamp-repeat_window:
            self._attention_intervals.popleft()
        durations["attention_window_seconds"] = sum(
            max(0, end-max(start, frame.timestamp-repeat_window))
            for start,end in self._attention_intervals
        )
        durations["attention_episode_count"] = len(self._attention_events)
        reasons = [name for name, threshold in thresholds.items()
                   if getattr(frame, name) is True and durations[name] >= threshold]
        # Rolling history is retained for inspection, but cannot by itself keep
        # a recovered driver in caution or turn a normal blink into a new alert.
        history_seconds = float(self.rules["history_support_seconds"])
        attention_support = (frame.attention_away is True and
                             durations["attention_away"] >= history_seconds)
        if attention_support and len(self._attention_events) >= int(self.rules["attention_repeat_count"]):
            reasons.append("attention_away_repeated")
        if attention_support and durations["attention_window_seconds"] >= float(self.rules["attention_cumulative_caution_seconds"]):
            reasons.append("attention_away_cumulative")
        posture_fields = ("head_abnormal", "torso_lean", "collapse_posture")
        drowsy_support = [name for name in ("eyes_closed", *posture_fields)
                          if getattr(frame, name) is True and durations[name] >= history_seconds]
        if (frame.perclos_high is True and drowsy_support and
                durations["perclos_high"] >= float(self.rules["perclos_caution_seconds"])):
            reasons.append("perclos_high")
        if frame.long_blinks_repeated is True and drowsy_support:
            reasons.append("long_blinks_repeated")
        if frame.nods_repeated is True and (drowsy_support or attention_support):
            reasons.append("nods_repeated")
        combined_seconds = float(self.rules["combined_drowsy_caution_seconds"])
        combined_posture = [name for name in posture_fields
                            if getattr(frame, name) is True and durations[name] >= combined_seconds]
        if (frame.eyes_closed is True and durations["eyes_closed"] >= combined_seconds
                and combined_posture):
            reasons.append("eyes_closed_with_posture_change")
        if not reasons and self.rules["unknown_if_face_unreadable"] and (not frame.face_readable or not frame.eyes_readable):
            self._healthy_since = None
            return Decision(frame.timestamp, DriverState.UNAVAILABLE,
                            ["face_unreadable" if not frame.face_readable else "eyes_unreadable"], durations)
        if not reasons:
            if self._was_caution:
                # Require an observed recovery of the signals that raised the
                # warning. Missing pose/eye measurements never count as recovery.
                missing = [name for name in self._recovery_fields if getattr(frame, name) is None]
                still_active = any(getattr(frame, name) is True for name in self._recovery_fields)
                if missing:
                    self._healthy_since = None
                    return Decision(frame.timestamp, DriverState.UNAVAILABLE,
                                    ["recovery_unobserved"], durations)
                if still_active:
                    self._healthy_since = None
                    return Decision(frame.timestamp, DriverState.CAUTION, ["recovering"], durations)
                if self._healthy_since is None:
                    self._healthy_since = frame.timestamp
                if frame.timestamp-self._healthy_since < float(self.rules["recovery_seconds"]):
                    return Decision(frame.timestamp, DriverState.CAUTION, ["recovering"], durations)
            self._was_caution = False
            self._recovery_fields.clear()
            return Decision(frame.timestamp, DriverState.NORMAL, [], durations)
        self._healthy_since = None
        self._was_caution = True
        for reason in reasons:
            if reason in thresholds:
                self._recovery_fields.add(reason)
            elif reason in ("attention_away_repeated", "attention_away_cumulative"):
                self._recovery_fields.add("attention_away")
            elif reason in ("perclos_high", "long_blinks_repeated"):
                self._recovery_fields.add("eyes_closed")
                self._recovery_fields.update(drowsy_support)
            elif reason == "nods_repeated":
                self._recovery_fields.add("attention_away")
                self._recovery_fields.update(drowsy_support)
            elif reason == "eyes_closed_with_posture_change":
                self._recovery_fields.add("eyes_closed")
                self._recovery_fields.update(combined_posture)
        explicit_no_response = frame.response_received is False
        warning_elapsed = durations["warning_active"] >= float(self.rules["danger_after_warning_seconds"])
        if self.rules["danger_policy"] == "external_response" and warning_elapsed and explicit_no_response:
            return Decision(frame.timestamp, DriverState.DANGER, reasons + ["explicit response timeout after warning"], durations)
        return Decision(frame.timestamp, DriverState.CAUTION, reasons, durations)

