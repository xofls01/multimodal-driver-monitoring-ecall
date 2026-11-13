"""Observed handheld-object candidates, separate from driver-state decisions."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

import numpy as np

from .object_tracking import box_iou


@dataclass
class ObjectTrack:
    identifier: int
    class_id: int
    box: tuple[float, float, float, float]
    confidence: float
    last_seen: float
    hits: int = 1
    near_mouth: bool = False


class HandheldObjectDetector:
    """Require object evidence near a hand and repeated, matched observations.

    A candidate is an object, never a claim of eating, drinking or smoking.
    Class names are resolved from the loaded weights, not assumed class IDs.
    """

    def __init__(self, model: Any, config: dict[str, Any]) -> None:
        self.model = model
        self.config = config
        self.enabled = bool(config.get("handheld_objects_enabled", False))
        names = model.names
        self.names = dict(enumerate(names)) if isinstance(names, list) else dict(names)
        requested = config.get("handheld_object_classes", {})
        available = set(self.names.values())
        missing = set(requested) - available
        if self.enabled and missing:
            raise ValueError(f"Object classes absent from loaded weights: {sorted(missing)}")
        self.classes = {int(i): str(requested[name]) for i, name in self.names.items() if name in requested}
        self.rate = float(config.get("handheld_detection_fps", 3.0))
        self.hold = float(config.get("handheld_hold_seconds", 0.6))
        self.confirmations = int(config.get("handheld_confirm_frames", 2))
        if self.rate <= 0 or self.hold <= 0 or self.confirmations < 1:
            raise ValueError("Handheld-object rate, hold time and confirmations must be positive")
        self.reset()

    def reset(self) -> None:
        self._tracks: list[ObjectTrack] = []
        self._next_at = 0.0
        self._next_id = 1
        self._last_timestamp: float | None = None

    def _predict(self, images: list[np.ndarray], image_size: int) -> list[list[tuple]]:
        results = self.model.predict(
            images, imgsz=image_size,
            conf=float(self.config.get("handheld_object_confidence", 0.35)),
            classes=sorted(self.classes), device=str(self.config.get("device", "cpu")),
            verbose=False,
        )
        output = []
        for result in results:
            boxes = result.boxes
            if boxes is None or len(boxes) == 0:
                output.append([])
                continue
            output.append([
                (*map(float, xy), float(score), int(class_id))
                for xy, score, class_id in zip(
                    boxes.xyxy.detach().cpu().numpy(), boxes.conf.detach().cpu().numpy(),
                    boxes.cls.detach().cpu().numpy(),
                )
            ])
        return output

    @staticmethod
    def _distance_to_box(point: tuple, box: tuple) -> float:
        x, y = point[:2]
        return math.hypot(max(box[0] - x, 0, x - box[2]), max(box[1] - y, 0, y - box[3]))

    def _near_hand(self, box: tuple, hands: list, keypoints: dict, scale: float) -> bool:
        padding = max(6.0 * scale, min(box[2] - box[0], box[3] - box[1]) * 0.15)
        expanded = (box[0] - padding, box[1] - padding, box[2] + padding, box[3] + padding)
        for points in hands:
            if sum(expanded[0] <= p[0] <= expanded[2] and expanded[1] <= p[1] <= expanded[3] for p in points) >= 2:
                return True
        # If hands are missing, require a visible wrist close to the box.
        if not hands:
            radius = min(35.0 * scale, max(10.0 * scale, min(box[2] - box[0], box[3] - box[1]) * 0.3))
            return any(p is not None and p[2] >= 0.5 and self._distance_to_box(p, box) <= radius
                       for p in (keypoints.get(9), keypoints.get(10)))
        return False

    def _views(self, frame: np.ndarray, hands: list, keypoints: dict) -> list[tuple]:
        height, width = frame.shape[:2]
        views = [(0, 0, width, height)]
        centers = [np.mean(np.asarray([p[i][:2] for i in (0, 5, 9, 13, 17)]), axis=0) for p in hands]
        if not centers:
            centers = [p[:2] for i in (9, 10) if (p := keypoints.get(i)) is not None and p[2] >= 0.5]
        radius = max(64, round(min(height, width) * 0.22))
        for x, y in centers[:2]:
            view = (max(0, int(x) - radius), max(0, int(y) - radius),
                    min(width, int(x) + radius), min(height, int(y) + radius))
            if min(view[2] - view[0], view[3] - view[1]) >= 32 and all(
                    box_iou(view, previous) < 0.85 for previous in views):
                views.append(view)
        return views

    def _observations(self, frame: np.ndarray, pose: dict, hands: dict, face: dict) -> list[tuple]:
        hand_sets, keypoints = hands.get("landmarks", []), pose.get("keypoints", {})
        if not hand_sets and not any(keypoints.get(i) is not None and keypoints[i][2] >= 0.5 for i in (9, 10)):
            return []
        height, width = frame.shape[:2]
        views = self._views(frame, hand_sets, keypoints)
        full = self._predict([frame], int(self.config.get("handheld_full_image_size", 960)))
        cropped = self._predict([frame[y1:y2, x1:x2] for x1, y1, x2, y2 in views[1:]],
                                int(self.config.get("handheld_crop_image_size", 640))) if len(views) > 1 else []
        candidates = []
        for view, detections in zip(views, full + cropped):
            for x1, y1, x2, y2, score, class_id in detections:
                box = (max(0., x1 + view[0]), max(0., y1 + view[1]),
                       min(float(width), x2 + view[0]), min(float(height), y2 + view[1]))
                if (class_id not in self.classes or not all(math.isfinite(v) for v in (*box, score))
                        or score < float(self.config.get("handheld_object_confidence", 0.35))
                        or box[2] <= box[0] or box[3] <= box[1]):
                    continue
                if (box[2] - box[0]) * (box[3] - box[1]) / (width * height) > float(self.config.get("handheld_max_area_ratio", 0.12)):
                    continue
                if not self._near_hand(box, hand_sets, keypoints, min(height, width) / 720):
                    continue
                landmarks, face_box = face.get("landmarks", {}), face.get("box")
                near_mouth = bool(face.get("readable") and face_box and 13 in landmarks and
                                  self._distance_to_box(landmarks[13], box) <= (face_box[2] - face_box[0]) * 0.25)
                candidates.append((box, score, class_id, near_mouth))
        # Suppress duplicate full/crop observations and competing labels on the same object.
        kept = []
        for candidate in sorted(candidates, key=lambda c: c[1], reverse=True):
            if all(box_iou(candidate[0], prior[0]) < (0.45 if candidate[2] == prior[2] else 0.65) for prior in kept):
                kept.append(candidate)
        return kept

    def update(self, frame: np.ndarray, pose: dict, hands: dict, face: dict, timestamp: float) -> list[dict]:
        if not math.isfinite(timestamp) or (self._last_timestamp is not None and timestamp < self._last_timestamp):
            raise ValueError("Handheld-object timestamps must be finite and monotonic")
        self._last_timestamp = timestamp
        if not self.enabled or not self.classes:
            return []
        self._tracks = [t for t in self._tracks if timestamp - t.last_seen <= self.hold + 1e-9]
        measured = timestamp + 1e-9 >= self._next_at
        seen = set()
        if measured:
            self._next_at = timestamp + 1.0 / self.rate
            for box, score, class_id, near_mouth in self._observations(frame, pose, hands, face):
                options = [t for t in self._tracks if t.class_id == class_id and t.identifier not in seen and box_iou(t.box, box) >= 0.15]
                track = max(options, key=lambda t: box_iou(t.box, box), default=None)
                if track is None:
                    track = ObjectTrack(self._next_id, class_id, box, score, timestamp, near_mouth=near_mouth)
                    self._next_id += 1
                    self._tracks.append(track)
                else:
                    track.box, track.confidence, track.last_seen = box, score, timestamp
                    track.hits += 1
                    track.near_mouth = near_mouth
                seen.add(track.identifier)
            # A miss breaks pending confirmation; it cannot accumulate unrelated sightings.
            self._tracks = [t for t in self._tracks if t.hits >= self.confirmations or t.identifier in seen]
        output = []
        for track in self._tracks:
            if track.hits < self.confirmations:
                continue
            age = max(0., timestamp - track.last_seen)
            output.append({
                "track_id": track.identifier, "class_id": track.class_id,
                "label": self.names[track.class_id], "category": self.classes[track.class_id],
                "box": [round(v, 2) for v in track.box],
                "confidence": round(track.confidence * math.exp(-age / self.hold), 4),
                "mode": "detected" if track.identifier in seen else "tracked" if measured else "cached",
                "age_seconds": round(age, 4),
                "near_mouth": track.near_mouth if age <= 1e-9 else None,
            })
        return output
