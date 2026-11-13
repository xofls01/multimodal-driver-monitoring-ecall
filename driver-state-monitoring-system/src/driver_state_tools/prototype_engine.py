"""Observable behavior -> simulator response. These extra rules are NOT paper results.

The strict literature engine remains separate. No scenario label is an input.
"""
from collections import deque
from dataclasses import asdict, dataclass
import math

from .decision_engine import Decision, DriverState
from .literature_engine import FixedClosureWindows


@dataclass
class PrototypeFrame:
    timestamp: float
    calibrated: bool
    face_readable: bool
    eyes_closed: bool | None = None
    attention_away: bool | None = None
    collapse_posture: bool | None = None
    torso_lean: bool | None = None
    mouth_open: bool | None = None
    hand_near_chest: bool | None = None
    nod_proxy: bool | None = None
    head_dip_count: int = 0
    long_closure_count: int = 0
    attention_source: str = "unavailable"


class PrototypeDecisionEngine:
    def __init__(self, rules):
        self.rules = dict(rules)
        self.thresholds = {k: float(rules[k+"_caution_seconds"]) for k in (
            "eyes_closed", "attention_away", "collapse_posture", "torso_lean",
            "mouth_open", "hand_near_chest", "nod_proxy")}
        numeric = list(self.thresholds.values()) + [float(rules[k]) for k in (
            "collapse_after_caution_seconds", "max_observation_gap_seconds", "recovery_seconds",
            "attention_window_seconds", "attention_episode_min_seconds", "attention_cumulative_seconds")]
        if any(not math.isfinite(v) or v <= 0 for v in numeric):
            raise ValueError("Prototype timings must be finite and positive")
        self.reset()

    def reset(self):
        self.previous = None
        self.starts = dict.fromkeys(self.thresholds)
        self.intervals = deque()
        self.episodes = deque()
        self.episode_counted = False
        self.collapse_caution_started = None
        self.recovery_fields = set()
        self.healthy_since = None
        self.ratio = FixedClosureWindows(float(self.rules["eye_ratio_window_seconds"]),
            float(self.rules["eye_ratio_high"]), float(self.rules["max_observation_gap_seconds"]),
            float(self.rules["eye_ratio_window_seconds"]))

    def update(self, f: PrototypeFrame):
        t = f.timestamp
        if not math.isfinite(t) or t < 0:
            raise ValueError("Timestamp must be finite and nonnegative")
        if self.previous and t < self.previous.timestamp:
            self.reset()
        previous = self.previous
        gap = previous is not None and t-previous.timestamp > float(self.rules["max_observation_gap_seconds"])+1e-9
        if gap:
            self.starts = dict.fromkeys(self.thresholds)
            self.collapse_caution_started = None
            self.episode_counted = False
            self.healthy_since = None
        values = {k: getattr(f, k) if f.calibrated else None for k in self.thresholds}
        durations = {}
        for key, active in values.items():
            if active is True:
                if self.starts[key] is None:
                    self.starts[key] = t
                durations[key] = t-self.starts[key]
            else:
                self.starts[key] = None
                durations[key] = 0.
        window = float(self.rules["attention_window_seconds"])
        if previous and not gap and f.calibrated and previous.calibrated and previous.attention_away is True and f.attention_away is True:
            self.intervals.append((previous.timestamp, t))
        while self.intervals and self.intervals[0][1] < t-window:
            self.intervals.popleft()
        while self.episodes and self.episodes[0] < t-window:
            self.episodes.popleft()
        if f.attention_away is not True or gap:
            self.episode_counted = False
        if (values["attention_away"] is True and not self.episode_counted and
                durations["attention_away"]+1e-9 >= float(self.rules["attention_episode_min_seconds"])):
            self.episodes.append(t)
            self.episode_counted = True
        durations["attention_window_seconds"] = sum(max(0,b-max(a,t-window)) for a,b in self.intervals)
        durations["attention_episode_count"] = len(self.episodes)
        reasons = [k for k, limit in self.thresholds.items()
                   if values[k] is True and durations[k]+1e-9 >= limit]
        history_support = float(self.rules["history_support_seconds"])
        if values["attention_away"] is True and durations["attention_away"] >= history_support:
            if durations["attention_window_seconds"] >= float(self.rules["attention_cumulative_seconds"]):
                reasons.append("attention_cumulative")
            if len(self.episodes) >= int(self.rules["attention_repeat_count"]):
                reasons.append("attention_repeated")
        if values["nod_proxy"] is True and f.head_dip_count >= int(self.rules["head_dip_repeat_count"]):
            reasons.append("repeated_head_dips")
        if (values["eyes_closed"] is True and durations["eyes_closed"] >= history_support
                and f.long_closure_count >= int(self.rules["long_closure_repeat_count"])):
            reasons.append("repeated_long_closures")
        ratio = self.ratio.update(t, f.eyes_closed if f.calibrated else None)
        if ratio["status"] == "high":
            reasons.append("eye_ratio_high")
        # A cause-specific timer: chest, mouth, nods, recovery and loss of vision
        # cannot contribute to the stop request.
        if "collapse_posture" in reasons:
            if self.collapse_caution_started is None:
                self.collapse_caution_started = t
        else:
            self.collapse_caution_started = None
        elapsed = t-self.collapse_caution_started if self.collapse_caution_started is not None else 0.
        durations["collapse_after_caution"] = elapsed
        stop = (self.collapse_caution_started is not None and
                elapsed+1e-9 >= float(self.rules["collapse_after_caution_seconds"]))
        # Eye stop remains disabled until separately validated. No generic 3s rule.
        if stop:
            reasons.append("sustained_collapse_stop")
        # A confirmed eye closure is observed behavior, not a failed eye read.
        # Attention is intentionally unassessed during closure; do not invent
        # FORWARD/AWAY or turn the pre-warning interval into vision failure.
        eyes_observed_closed = f.calibrated and f.face_readable and f.eyes_closed is True
        attention_blocked_by_closure = eyes_observed_closed and f.attention_away is None
        missing_recovery = {k for k in self.recovery_fields if values[k] is None}
        if reasons:
            state = DriverState.DANGER if stop else DriverState.CAUTION
            self.recovery_fields.update(k for k in self.thresholds if k in reasons)
            if any(k in reasons for k in ("attention_repeated", "attention_cumulative")):
                self.recovery_fields.add("attention_away")
            if "repeated_long_closures" in reasons:
                self.recovery_fields.add("eyes_closed")
            if "repeated_head_dips" in reasons:
                self.recovery_fields.add("nod_proxy")
            self.healthy_since = None
        elif missing_recovery == {"attention_away"} and attention_blocked_by_closure:
            state, reasons = DriverState.CAUTION, ["attention_recovery_unconfirmed"]
            self.healthy_since = None
        elif missing_recovery:
            state, reasons = DriverState.UNAVAILABLE, ["recovery_unobserved"]
            self.healthy_since = None
        elif any(values[k] is True for k in self.recovery_fields):
            state, reasons = DriverState.CAUTION, ["recovery_pending"]
            self.healthy_since = None
        elif (not f.calibrated or not f.face_readable or f.eyes_closed is None
              or f.attention_away is None and not attention_blocked_by_closure):
            state, reasons = DriverState.UNAVAILABLE, ["calibrating" if not f.calibrated else "insufficient_observation"]
            self.healthy_since = None
        elif self.recovery_fields:
            if self.healthy_since is None:
                self.healthy_since = t
            durations["recovery"] = t-self.healthy_since
            if t-self.healthy_since+1e-9 < float(self.rules["recovery_seconds"]):
                state, reasons = DriverState.CAUTION, ["recovery_pending"]
            else:
                state = DriverState.NORMAL
                self.recovery_fields.clear()
        else:
            state = DriverState.NORMAL
        pending = []
        if eyes_observed_closed and durations["eyes_closed"]+1e-9 < self.thresholds["eyes_closed"]:
            pending.append(dict(event="eyes_closed",elapsed_seconds=durations["eyes_closed"],
                                threshold_seconds=self.thresholds["eyes_closed"]))
        phase = ("evaluating" if state is DriverState.NORMAL and pending else
                 "monitoring" if state is DriverState.NORMAL else
                 "unavailable" if state is DriverState.UNAVAILABLE else
                 "recovering" if reasons in (["recovery_pending"],["attention_recovery_unconfirmed"]) else "alert")
        self.previous = f
        return Decision(t, state, reasons, durations, dict(mode="prototype", stop_requested=stop,
            stop_basis="simulator_sustained_posture" if stop else None,
            caution_timer_policy="same_collapse_only", eye_ratio=ratio,
            eye_stop_enabled=False, medical_event_confirmed=False,
            attention_source=f.attention_source, gaze_zone_model_connected=False,
            observation_gap=gap, prototype_frame=asdict(f),
            pending_events=pending, decision_phase=phase,
            attention_status="not_assessed_eyes_closed" if attention_blocked_by_closure else
                             "away" if f.attention_away is True else "forward" if f.attention_away is False else "unavailable",
            observation_status=dict(eyes=f.eyes_closed is not None,
                attention=f.attention_away is not None, posture=f.collapse_posture is not None),
            full_system_ready=False))
