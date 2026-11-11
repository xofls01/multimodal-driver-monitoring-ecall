"""Selected literature timings with separate measurement readiness and warnings.

No legacy caution timer, scenario IDs, medical diagnoses or hidden gaze proxies.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math
from typing import Any

from .decision_engine import Decision, DriverState


@dataclass
class LiteratureFrame:
    timestamp: float
    session_id: str
    eyes_closed: bool | None = None
    eye_danger_validated: bool = False
    gaze: str = "unknown"
    gaze_zone: str = "unknown"
    mouth_open: bool | None = None
    nod_suspected: bool = False
    nod_completed: bool = False
    feature_status: dict[str, str] = field(default_factory=dict)


class FixedClosureWindows:
    """Fixed, full-duration windows. Transitions and missing spans are unknown.

    Bounds C/T .. (C+U)/T use the fixed wall/video-time denominator.
    """
    def __init__(self, seconds: float, ratio: float, max_gap: float, lifetime: float):
        self.seconds, self.ratio, self.max_gap, self.lifetime = seconds, ratio, max_gap, lifetime
        self.start = 0.0
        self.closed = self.open = 0.0
        self.previous: tuple[float, bool | None] | None = None
        self.result: dict[str, Any] | None = None

    def update(self, timestamp: float, value: bool | None) -> dict[str, Any]:
        left = self.previous[0] if self.previous is not None else self.start
        state = (value if self.previous is not None and value is not None
                 and self.previous[1] is value and timestamp-left <= self.max_gap+1e-9 else None)
        while left < timestamp-1e-9:
            end = min(timestamp, self.start+self.seconds)
            amount = max(0.0, end-left)
            if state is True:
                self.closed += amount
            elif state is False:
                self.open += amount
            left = end
            if end+1e-9 >= self.start+self.seconds:
                unknown = max(0.0, self.seconds-self.closed-self.open)
                low, high = self.closed/self.seconds, (self.closed+unknown)/self.seconds
                status = "high" if low+1e-9 >= self.ratio else "low" if high < self.ratio-1e-9 else "unknown"
                self.result = dict(status=status, window_start=self.start, window_end=end,
                                   closed_seconds=self.closed, unknown_seconds=unknown,
                                   lower_ratio=low, upper_ratio=high, expires_at=end+self.lifetime)
                self.start = end
                self.closed = self.open = 0.0
        self.previous = timestamp, value
        if self.result is not None and timestamp < self.result["expires_at"]:
            return dict(self.result, current_window_collected_seconds=timestamp-self.start)
        return dict(status="collecting", current_window_collected_seconds=timestamp-self.start,
                    window_start=self.start, window_seconds=self.seconds)


class LiteratureDecisionEngine:
    def __init__(self, rules: dict[str, Any]):
        self.rules = dict(rules)
        positive = ("eyes_closed_caution_seconds", "eyes_prolonged_seconds", "eyes_closed_danger_seconds",
                    "attention_away_caution_seconds", "attention_window_seconds", "attention_cumulative_seconds",
                    "attention_after_warning_seconds", "mouth_open_caution_seconds", "eye_ratio_window_seconds",
                    "eye_ratio_result_lifetime_seconds", "max_observation_gap_seconds", "recovery_seconds")
        if any(not math.isfinite(float(rules[k])) or float(rules[k]) <= 0 for k in positive):
            raise ValueError("Literature timing settings must be finite and positive")
        if not 0 < float(rules["eye_ratio_high"]) <= 1:
            raise ValueError("Invalid eye closure ratio")
        self.reset()

    def reset(self, session_id: str | None = None):
        self.session_id = session_id
        self.last: LiteratureFrame | None = None
        self.starts: dict[str, float | None] = {"eyes": None, "away": None, "mouth": None}
        self.intervals: deque[tuple[float, float, str]] = deque()
        self.cumulative_armed = True
        self.attention_active = False
        self.warning_request_id: str | None = None
        self.warning_issued_at: float | None = None
        self.warning_source: str | None = None
        self.warning_count = 0
        self.recovery_fields: set[str] = set()
        self.healthy_since: float | None = None
        self.windows = FixedClosureWindows(float(self.rules["eye_ratio_window_seconds"]),
                                          float(self.rules["eye_ratio_high"]),
                                          float(self.rules["max_observation_gap_seconds"]),
                                          float(self.rules["eye_ratio_result_lifetime_seconds"]))

    def confirm_warning(self, request_id: str, timestamp: float, source: str) -> bool:
        # A request/send is not evidence that a warning was actually displayed.
        if (request_id != self.warning_request_id or not self.attention_active or self.last is None
                or self.last.gaze != "away" or self.warning_issued_at is not None
                or not math.isfinite(timestamp) or timestamp < self.last.timestamp):
            return False
        self.warning_issued_at, self.warning_source = timestamp, source
        return True

    def _timer(self, name: str, active: bool, t: float, gap: bool) -> float:
        if not active:
            self.starts[name] = None
            return 0.0
        if self.starts[name] is None or gap:
            self.starts[name] = t
        return max(0.0, t-float(self.starts[name]))

    def update(self, frame: LiteratureFrame) -> Decision:
        t = frame.timestamp
        if not math.isfinite(t) or t < 0:
            raise ValueError("Invalid observation timestamp")
        reset_reason = None
        if self.session_id != frame.session_id:
            self.reset(frame.session_id)
            reset_reason = "session_changed"
        elif self.last is not None and t < self.last.timestamp:
            self.reset(frame.session_id)
            reset_reason = "timestamp_reversed"
        previous = self.last
        gap = previous is not None and t-previous.timestamp > float(self.rules["max_observation_gap_seconds"])+1e-9
        if frame.gaze not in ("forward", "away", "unknown", "eyes_closed"):
            raise ValueError("Unsupported gaze state")
        # Closed or unreadable eyes never count as an observed off-road gaze.
        effective_gaze = frame.gaze if frame.eyes_closed is False else "unknown"
        old_gaze = previous.gaze if previous is not None and previous.eyes_closed is False else "unknown"
        if previous is not None:
            category = effective_gaze if not gap and old_gaze == effective_gaze else "unknown"
            self.intervals.append((previous.timestamp, t, category))
        cutoff = t-float(self.rules["attention_window_seconds"])
        while self.intervals and self.intervals[0][1] <= cutoff:
            self.intervals.popleft()
        cumulative = sum(max(0., end-max(start,cutoff)) for start,end,value in self.intervals if value == "away")
        unknown = sum(max(0., end-max(start,cutoff)) for start,end,value in self.intervals if value == "unknown")
        if cumulative < float(self.rules["attention_cumulative_seconds"])-1e-9:
            self.cumulative_armed = True
        eyes = self._timer("eyes", frame.eyes_closed is True, t, gap)
        away = self._timer("away", effective_gaze == "away", t, gap)
        mouth = self._timer("mouth", frame.mouth_open is True, t, gap)
        ratio = self.windows.update(t, frame.eyes_closed)
        if gap or effective_gaze != "away":
            self.attention_active = False
            self.warning_request_id = self.warning_issued_at = self.warning_source = None
        continuous_trigger = effective_gaze == "away" and away+1e-9 >= float(self.rules["attention_away_caution_seconds"])
        cumulative_trigger = (effective_gaze == "away" and self.cumulative_armed and
                              cumulative+1e-9 >= float(self.rules["attention_cumulative_seconds"]))
        if continuous_trigger or cumulative_trigger:
            if not self.attention_active:
                self.warning_count += 1
                self.warning_request_id = f"{frame.session_id}:distraction:{self.warning_count}:{t:.6f}"
                self.attention_active = True
            if cumulative_trigger:
                self.cumulative_armed = False
        reasons, active_fields = [], set()
        if frame.eyes_closed is True and eyes+1e-9 >= float(self.rules["eyes_closed_caution_seconds"]):
            reasons.append("eyes_closed")
            active_fields.add("eyes")
            if eyes+1e-9 >= float(self.rules["eyes_prolonged_seconds"]):
                reasons.append("prolonged_eye_closure")
        if self.attention_active:
            reasons.append("attention_away" if continuous_trigger else "attention_away_cumulative")
            active_fields.add("gaze")
        if frame.mouth_open is True and mouth+1e-9 >= float(self.rules["mouth_open_caution_seconds"]):
            reasons.append("sustained_mouth_opening")
            active_fields.add("mouth")
        if frame.nod_suspected:
            reasons.append("nod_suspected")
            active_fields.add("nod")
        if frame.nod_completed:
            reasons.append("nod_completed")
        if ratio["status"] == "high":
            reasons.append("long_term_eye_closure_ratio")
        # Both Danger paths are tied to a specified observation, never total caution.
        stop_reasons = []
        if frame.eye_danger_validated and frame.eyes_closed is True and eyes+1e-9 >= float(self.rules["eyes_closed_danger_seconds"]):
            stop_reasons.append("validated_eye_closure_6s")
        warning_elapsed = 0.0
        if self.warning_issued_at is not None and self.attention_active and effective_gaze == "away":
            start = max(self.warning_issued_at, self.starts["away"] or 0.0)
            warning_elapsed = max(0.0, t-start)
            if warning_elapsed+1e-9 >= float(self.rules["attention_after_warning_seconds"]):
                stop_reasons.append("away_after_displayed_warning")
        available = {"eyes": frame.eyes_closed is not None,
                     "gaze": effective_gaze in ("forward", "away"),
                     "mouth": frame.mouth_open is not None,
                     "nod": frame.feature_status.get("nod") == "ready"}
        current_active = {"eyes": frame.eyes_closed is True, "gaze": effective_gaze == "away",
                          "mouth": frame.mouth_open is True, "nod": frame.nod_suspected}
        if stop_reasons:
            state = DriverState.DANGER
            reasons.extend(stop_reasons)
        elif reasons:
            state = DriverState.CAUTION
        elif any(not available.get(k, False) for k in self.recovery_fields):
            state = DriverState.UNAVAILABLE
            reasons = ["recovery_unobserved"]
            self.healthy_since = None
        elif any(current_active.get(k, False) for k in self.recovery_fields):
            state = DriverState.CAUTION
            reasons = ["recovering"]
            self.healthy_since = None
        elif any(not available.get(k, False) for k in self.rules["required_observations"]):
            state = DriverState.UNAVAILABLE
            reasons = ["required_observation_unavailable"]
            self.healthy_since = None
        elif self.recovery_fields:
            if self.healthy_since is None:
                self.healthy_since = t
            if t-self.healthy_since+1e-9 < float(self.rules["recovery_seconds"]):
                state, reasons = DriverState.CAUTION, ["recovering"]
            else:
                state = DriverState.NORMAL
                self.recovery_fields.clear()
        else:
            state = DriverState.NORMAL
        if active_fields:
            self.recovery_fields.update(active_fields)
            self.healthy_since = None
        metadata = dict(mode="literature", session_id=frame.session_id,
                        feature_status=dict(frame.feature_status), gaze=effective_gaze, gaze_zone=frame.gaze_zone,
                        warning_request_id=self.warning_request_id,
                        warning_issued_at=self.warning_issued_at, warning_source=self.warning_source,
                        stop_requested=bool(stop_reasons), eye_ratio=ratio,
                        observation_reset=reset_reason, observation_gap=gap,
                        eye_danger_validated=frame.eye_danger_validated,
                        required_observations_ready=all(available.get(k,False) for k in self.rules["required_observations"]),
                        full_system_ready=all(frame.feature_status.get(k)=="ready" for k in ("eyes","gaze","nod","mouth","eye_stop")))
        self.last = frame
        return Decision(t, state, reasons, dict(eyes_closed=eyes, attention_away=away,
                        attention_window_seconds=cumulative, attention_unknown_seconds=unknown,
                        mouth_open=mouth, after_displayed_warning=warning_elapsed), metadata)
