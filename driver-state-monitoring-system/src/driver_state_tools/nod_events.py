"""R3-inspired two-boundary event detector; numeric boundaries require calibration."""
from __future__ import annotations

from dataclasses import dataclass, asdict
import math
from typing import Any


@dataclass
class NodEvent:
    status: str
    phase: str = "idle"
    suspected: bool = False
    completed: bool = False
    count: int = 0
    speed: float | None = None
    reason: str | None = None

    def to_dict(self):
        return asdict(self)


class NodEventDetector:
    def __init__(self, profile: dict[str, Any] | None, max_gap: float = .25):
        self.profile = profile or {}
        self.max_gap = max_gap
        required = ("upper_boundary", "lower_boundary", "descent_min_speed", "descent_max_speed",
                    "recovery_min_speed", "recovery_max_speed")
        self.ready = (self.profile.get("measurement") == "eye_mid_y_over_image_height"
                      and self.profile.get("reviewed") is True
                      and bool(self.profile.get("provenance"))
                      and all(isinstance(self.profile.get(k), (float, int)) and not isinstance(self.profile[k], bool)
                              and math.isfinite(self.profile[k]) for k in required))
        if self.ready:
            p = self.profile
            if not (p["upper_boundary"] < p["lower_boundary"] and
                    0 <= p["descent_min_speed"] < p["descent_max_speed"] and
                    0 <= p["recovery_min_speed"] < p["recovery_max_speed"]):
                raise ValueError("Invalid calibrated nod boundaries or speed ranges")
        self.count = 0
        self.reset()

    def reset(self):
        self.phase = "idle"
        self.last = None
        self.start_time = self.start_y = self.bottom_time = self.bottom_y = None

    def update(self, timestamp: float, y: float | None, eyes_closed: bool | None) -> NodEvent:
        if not self.ready:
            return NodEvent("needs_calibration", count=self.count)
        if self.last is not None and timestamp < self.last[0]:
            self.count = 0
            self.reset()
        if (y is None or eyes_closed is None or not math.isfinite(y)
                or self.last is not None and timestamp-self.last[0] > self.max_gap):
            self.reset()
            return NodEvent("unavailable", count=self.count, reason="observation_lost")
        previous = self.last
        self.last = (timestamp, y)
        if previous is None or timestamp <= previous[0]:
            return NodEvent("ready", count=self.count)
        speed = (y-previous[1])/(timestamp-previous[0])
        p = self.profile
        reason, completed = None, False
        if self.phase == "idle":
            if previous[1] <= p["upper_boundary"] < y and speed > 0 and eyes_closed:
                self.phase = "descending"
                self.start_time, self.start_y = previous
        if self.phase == "descending":
            if eyes_closed is False:
                self.phase, reason = "idle", "eyes_open_during_descent"
            elif y < previous[1] and y < p["lower_boundary"]:
                self.phase, reason = "idle", "returned_before_lower_boundary"
            elif y >= p["lower_boundary"]:
                descent_speed = (y-self.start_y)/max(timestamp-self.start_time, 1e-9)
                if p["descent_min_speed"] <= descent_speed <= p["descent_max_speed"]:
                    self.phase = "below_lower"
                    self.bottom_time, self.bottom_y = timestamp, y
                else:
                    self.phase, reason = "idle", "descent_speed_outside_profile"
        elif self.phase == "below_lower":
            if speed > 0 and eyes_closed is False:
                self.phase, reason = "idle", "eyes_open_during_descent"
            elif y > self.bottom_y:
                self.bottom_time, self.bottom_y = timestamp, y
            elif speed < 0:
                self.phase = "recovering"
        if self.phase == "recovering":
            # Opening the eyes on the upward return is permitted.
            if y <= p["upper_boundary"]:
                recovery_speed = (self.bottom_y-y)/max(timestamp-self.bottom_time, 1e-9)
                if p["recovery_min_speed"] <= recovery_speed <= p["recovery_max_speed"]:
                    self.count += 1
                    completed = True
                else:
                    reason = "recovery_speed_outside_profile"
                self.phase = "idle"
        return NodEvent("ready", self.phase, self.phase in ("below_lower", "recovering"),
                        completed, self.count, speed, reason)
