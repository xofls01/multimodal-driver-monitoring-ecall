from __future__ import annotations

import math
from dataclasses import dataclass


Box = tuple[int, int, int, int, float]


def box_iou(first: Box, second: Box) -> float:
    x1 = max(first[0], second[0])
    y1 = max(first[1], second[1])
    x2 = min(first[2], second[2])
    y2 = min(first[3], second[3])
    intersection = max(0, x2 - x1) * max(0, y2 - y1)
    first_area = max(1, first[2] - first[0]) * max(1, first[3] - first[1])
    second_area = max(1, second[2] - second[0]) * max(1, second[3] - second[1])
    return intersection / float(first_area + second_area - intersection)


@dataclass
class TrackedBoxes:
    boxes: list[Box]
    mode: str | None


@dataclass
class _BoxTrack:
    box: Box
    last_detection_at: float
    hits: int = 1


class TemporalBoxTracker:
    """Confirm each object independently; every box expires on its own clock."""

    def __init__(self, hold_seconds: float = 0.8, smoothing: float = 0.65, min_confirmations: int = 1) -> None:
        if not math.isfinite(hold_seconds) or hold_seconds <= 0 or not 0 <= smoothing <= 1:
            raise ValueError("Tracker hold must be positive; smoothing must be in [0, 1]")
        self.hold_seconds = hold_seconds
        self.smoothing = smoothing
        self.min_confirmations = max(1, int(min_confirmations))
        self.reset()

    def reset(self) -> None:
        self._tracks: list[_BoxTrack] = []
        self._last_timestamp: float | None = None

    def _prune(self, timestamp: float) -> None:
        if not math.isfinite(timestamp):
            raise ValueError("Tracker timestamp must be finite")
        if self._last_timestamp is not None and timestamp < self._last_timestamp:
            self.reset()
        self._last_timestamp = timestamp
        self._tracks = [t for t in self._tracks
                        if 0 <= timestamp-t.last_detection_at <= self.hold_seconds]

    def _visible(self, timestamp: float, measured: bool = False) -> TrackedBoxes:
        confirmed = [t for t in self._tracks if t.hits >= self.min_confirmations]
        if not confirmed:
            return TrackedBoxes([], None)
        boxes = [t.box[:4] + (t.box[4]*math.exp(-(timestamp-t.last_detection_at)/self.hold_seconds),)
                 for t in confirmed]
        current = measured and any(t.last_detection_at == timestamp for t in confirmed)
        return TrackedBoxes(boxes, "detected" if current else "tracked" if measured else "cached")

    def update(self, detections: list[Box], timestamp: float) -> TrackedBoxes:
        self._prune(timestamp)
        # Greedy one-to-one matching prevents one existing phone from validating
        # an unrelated candidate or duplicate crop predictions from adding hits.
        pairs = sorted(((box_iou(t.box, b), i, j) for i,t in enumerate(self._tracks)
                        for j,b in enumerate(detections)), reverse=True)
        matched_tracks, matched_boxes = set(), set()
        for overlap,i,j in pairs:
            if overlap < .15:
                break
            if i in matched_tracks or j in matched_boxes:
                continue
            track, current = self._tracks[i], detections[j]
            alpha = self.smoothing
            coords = tuple(int(round(alpha*current[k]+(1-alpha)*track.box[k])) for k in range(4))
            track.box = coords + (current[4],)
            track.last_detection_at = timestamp
            track.hits += 1
            matched_tracks.add(i)
            matched_boxes.add(j)
        # A measured miss cancels a pending candidate. Confirmed objects may
        # bridge a short miss, without another object's detections refreshing them.
        self._tracks = [t for i,t in enumerate(self._tracks)
                        if i in matched_tracks or t.hits >= self.min_confirmations]
        self._tracks.extend(_BoxTrack(b,timestamp) for j,b in enumerate(detections) if j not in matched_boxes)
        return self._visible(timestamp, measured=True)

    def peek(self, timestamp: float) -> TrackedBoxes:
        """An unmeasured frame never provides a confirmation or refreshes age."""
        self._prune(timestamp)
        return self._visible(timestamp)
