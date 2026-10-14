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


class TemporalBoxTracker:
    """Short-gap box persistence to remove detector flicker without inventing long tracks."""

    def __init__(self, hold_seconds: float = 0.8, smoothing: float = 0.65, min_confirmations: int = 1) -> None:
        self.hold_seconds = hold_seconds
        self.smoothing = smoothing
        self.min_confirmations = max(1, int(min_confirmations))
        self._boxes: list[Box] = []
        self._last_detection_at: float | None = None
        self._pending_boxes: list[Box] = []
        self._pending_hits = 0

    def reset(self) -> None:
        self._boxes = []
        self._last_detection_at = None
        self._pending_boxes = []
        self._pending_hits = 0

    def update(self, detections: list[Box], timestamp: float) -> TrackedBoxes:
        if detections:
            if not self._boxes and self.min_confirmations > 1:
                matched = any(
                    box_iou(previous, current) >= 0.15
                    for previous in self._pending_boxes
                    for current in detections
                )
                self._pending_hits = self._pending_hits + 1 if matched else 1
                self._pending_boxes = list(detections)
                if self._pending_hits < self.min_confirmations:
                    return TrackedBoxes([], None)
                self._pending_boxes = []
                self._pending_hits = 0
            smoothed: list[Box] = []
            for current in detections:
                previous = max(self._boxes, key=lambda box: box_iou(box, current), default=None)
                if previous is not None and box_iou(previous, current) >= 0.15:
                    alpha = self.smoothing
                    coords = tuple(int(round(alpha * current[i] + (1.0 - alpha) * previous[i])) for i in range(4))
                    smoothed.append(coords + (current[4],))
                else:
                    smoothed.append(current)
            self._boxes = smoothed
            self._last_detection_at = timestamp
            return TrackedBoxes(list(self._boxes), "detected")
        self._pending_boxes = []
        self._pending_hits = 0
        if self._boxes and self._last_detection_at is not None:
            elapsed = timestamp - self._last_detection_at
            if 0.0 <= elapsed <= self.hold_seconds:
                decay = math.exp(-elapsed / max(self.hold_seconds, 1e-6))
                held = [box[:4] + (box[4] * decay,) for box in self._boxes]
                return TrackedBoxes(held, "tracked")
        self.reset()
        return TrackedBoxes([], None)

    def peek(self, timestamp: float) -> TrackedBoxes:
        """An unmeasured frame is not a detector miss or a new confirmation."""
        if self._boxes and self._last_detection_at is not None:
            elapsed = timestamp-self._last_detection_at
            if 0 <= elapsed <= self.hold_seconds:
                decay=math.exp(-elapsed/max(self.hold_seconds,1e-6))
                return TrackedBoxes([b[:4]+(b[4]*decay,) for b in self._boxes], 'cached')
            self.reset()
        return TrackedBoxes([],None)
