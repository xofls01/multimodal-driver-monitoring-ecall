from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .object_tracking import TemporalBoxTracker, box_iou
from .dashboard import draw_overlay
from .vision_features import (
    LEFT_EYE,
    RIGHT_EYE,
    RawFeatures,
    eye_aspect_ratio,
    estimate_head_pose,
    head_drop_ratio,
    iris_gaze_ratio,
    mouth_aspect_ratio,
    point_in_normalized_roi,
    torso_angle,
)


class VisionModels:
    """YOLO + MediaPipe feature extractor with explicit quality gates."""

    def __init__(self, config: dict[str, Any], project_root: Path) -> None:
        try:
            import mediapipe as mp
            import torch
            from ultralytics import YOLO
        except ImportError as error:  # pragma: no cover - exercised by CLI environment
            raise RuntimeError("Install requirements.txt before running vision inference") from error

        self.config = config
        torch.set_num_threads(int(config.get("cpu_threads", 2)))
        self._eye_measurement_disabled = bool(config.get("eye_measurement_disabled", False))
        self._next_phone_at = 0.0
        self._last_phone_mode = None
        self.mp = mp
        self.pose_model = YOLO(str(self._resolve_model(config["pose_model"], project_root)))
        self.detect_model = YOLO(str(self._resolve_model(config["detect_model"], project_root)))
        self.phone_tracker = TemporalBoxTracker(
            float(config.get("phone_hold_seconds", 0.8)),
            min_confirmations=int(config.get("phone_confirm_frames", 2)),
        )
        self.face_landmarker = mp.solutions.face_mesh.FaceMesh(
            static_image_mode=False,
            max_num_faces=1,
            refine_landmarks=True,
            min_detection_confidence=float(config["face_detection_confidence"]),
            min_tracking_confidence=float(config["face_tracking_confidence"]),
        )
        self.hand_landmarker = mp.solutions.hands.Hands(
            static_image_mode=False,
            max_num_hands=2,
            min_detection_confidence=float(config.get("hand_detection_confidence", 0.35)),
            min_tracking_confidence=float(config.get("hand_tracking_confidence", 0.45)),
        )

    def start_clip(self, eyes_occluded: bool = False) -> None:
        # Reset MediaPipe's graphs too: independent source clips are independent sessions.
        self.face_landmarker.reset()
        self.hand_landmarker.reset()
        self.phone_tracker.reset()
        self._next_phone_at = 0.0
        self._last_phone_mode = None
        self._eye_measurement_disabled = eyes_occluded or bool(self.config.get("eye_measurement_disabled", False))

    @staticmethod
    def _resolve_model(value: str, project_root: Path) -> Path | str:
        candidate = Path(value)
        if candidate.is_absolute():
            return candidate
        local = project_root / candidate
        return local if local.exists() else value

    def close(self) -> None:
        self.face_landmarker.close()
        self.hand_landmarker.close()

    def _extract_face(self, frame: np.ndarray) -> dict[str, Any]:
        height, width = frame.shape[:2]
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        rgb.flags.writeable = False
        result = self.face_landmarker.process(rgb)
        if not result.multi_face_landmarks:
            return {"readable": False, "eyes_readable": False, "reason": "face_not_detected"}
        landmarks = result.multi_face_landmarks[0].landmark
        points = [(item.x * width, item.y * height, item.z * width) for item in landmarks]
        landmark_xy = {index: (float(point[0]), float(point[1])) for index, point in enumerate(points)}
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        box = (
            max(0, int(min(xs))),
            max(0, int(min(ys))),
            min(width - 1, int(max(xs))),
            min(height - 1, int(max(ys))),
        )
        face_width = box[2] - box[0]
        if face_width < int(self.config["face_min_width_pixels"]):
            return {
                "readable": False,
                "eyes_readable": False,
                "box": box,
                "landmarks": landmark_xy,
                "reason": "face_too_small",
            }
        interocular = float(np.linalg.norm(np.asarray(points[33][:2]) - np.asarray(points[263][:2])))
        eyes_readable = interocular >= float(self.config["interocular_min_pixels"])
        eye_reason = None
        if self._eye_measurement_disabled:
            eyes_readable, eye_reason = False, "eyes_occluded_declared"
        elif eyes_readable:
            # FaceMesh may predict plausible eye landmarks behind opaque lenses.
            # Check image evidence before trusting those coordinates.
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            for eye in (LEFT_EYE, RIGHT_EYE):
                ex = [points[i][0] for i in eye]
                ey = [points[i][1] for i in eye]
                pad = max(3, int((max(ex) - min(ex)) * 0.15))
                tile = gray[max(0,int(min(ey))-pad):min(height,int(max(ey))+pad+1),
                            max(0,int(min(ex))):min(width,int(max(ex))+1)]
                if not tile.size or float(np.mean(tile)) < float(self.config.get("eye_min_mean_brightness",25)) or float(np.mean(tile < 35)) > float(self.config.get("eye_max_dark_fraction",.8)) or float(np.std(tile)) < float(self.config.get("eye_min_contrast",6)):
                    eyes_readable, eye_reason = False, "eye_image_quality_low"
                    break
        left = eye_aspect_ratio(points, LEFT_EYE) if eyes_readable else None
        right = eye_aspect_ratio(points, RIGHT_EYE) if eyes_readable else None
        if left is None or right is None or not (0.02 <= left <= 0.65 and 0.02 <= right <= 0.65):
            eyes_readable = False
            left = right = None
        pose = estimate_head_pose(points)
        gaze = iris_gaze_ratio(points) if eyes_readable else None
        return {
            "readable": pose is not None,
            "eyes_readable": eyes_readable and pose is not None,
            "ear_left": left,
            "ear_right": right,
            "mar": mouth_aspect_ratio(points),
            "head_pose": pose,
            "gaze": gaze,
            "box": box,
            "landmarks": landmark_xy,
            "reason": eye_reason if pose is not None else "head_pose_failed",
        }

    def _extract_pose(self, frame: np.ndarray) -> dict[str, Any]:
        result = self.pose_model.predict(
            frame,
            imgsz=int(self.config["image_size"]),
            conf=float(self.config["pose_confidence"]),
            device=str(self.config["device"]),
            verbose=False,
        )[0]
        if result.boxes is None or len(result.boxes) == 0 or result.keypoints is None:
            return {"readable": False, "reason": "person_pose_not_detected", "keypoints": {}}
        boxes = result.boxes.xyxy.detach().cpu().numpy()
        confidences = result.boxes.conf.detach().cpu().numpy()
        areas = np.maximum(1.0, (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1]))
        index = int(np.argmax(confidences * np.sqrt(areas)))
        xy = result.keypoints.xy[index].detach().cpu().numpy()
        if result.keypoints.conf is None:
            kconf = np.ones(len(xy), dtype=float)
        else:
            kconf = result.keypoints.conf[index].detach().cpu().numpy()
        keypoints = {i: (float(pt[0]), float(pt[1]), float(kconf[i])) for i, pt in enumerate(xy)}
        angle = torso_angle({i: value for i, value in keypoints.items() if value[2] >= 0.30})
        drop = head_drop_ratio({i: value for i, value in keypoints.items() if value[2] >= 0.30})
        readable = angle is not None
        return {
            "readable": readable,
            "reason": None if readable else "shoulder_or_nose_keypoints_unreadable",
            "keypoints": keypoints,
            "box": tuple(int(value) for value in boxes[index]),
            "torso_angle": angle,
            "head_drop_ratio": drop,
        }

    def _phone_predictions(
        self, images: list[np.ndarray], image_size: int, confidence: float
    ) -> list[list[tuple[int, int, int, int, float]]]:
        if not images:
            return []
        predictions = self.detect_model.predict(
            images,
            imgsz=image_size,
            conf=confidence,
            classes=[int(self.config["phone_class_id"])],
            device=str(self.config["device"]),
            verbose=False,
        )
        output: list[list[tuple[int, int, int, int, float]]] = []
        for result in predictions:
            if result.boxes is None or len(result.boxes) == 0:
                output.append([])
                continue
            boxes = result.boxes.xyxy.detach().cpu().numpy()
            confidences = result.boxes.conf.detach().cpu().numpy()
            output.append([tuple(int(v) for v in box) + (float(conf),) for box, conf in zip(boxes, confidences)])
        return output

    @staticmethod
    def _nms(
        boxes: list[tuple[int, int, int, int, float]], threshold: float = 0.45
    ) -> list[tuple[int, int, int, int, float]]:
        kept: list[tuple[int, int, int, int, float]] = []
        for candidate in sorted(boxes, key=lambda item: item[4], reverse=True):
            if all(box_iou(candidate, previous) < threshold for previous in kept):
                kept.append(candidate)
        return kept

    def _extract_objects(
        self, frame: np.ndarray, pose: dict[str, Any], hands: dict[str, Any], timestamp: float
    ) -> tuple[list[tuple[int, int, int, int, float]], str | None]:
        rate=float(self.config.get('phone_detection_fps',self.config['analysis_fps']))
        if timestamp+1e-6 < self._next_phone_at:
            held=self.phone_tracker.peek(timestamp)
            mode='tracked' if held.boxes and self._last_phone_mode == 'tracked' else held.mode
            return held.boxes,mode
        while self._next_phone_at <= timestamp+1e-6:
            self._next_phone_at += 1/max(rate,.1)
        full = self._phone_predictions(
            [frame], int(self.config.get("phone_full_image_size", 960)), float(self.config["object_confidence"])
        )[0]
        height, width = frame.shape[:2]
        crop_boxes: list[tuple[int, int, int, int]] = []
        person_box = pose.get("box")
        if person_box:
            x1, y1, x2, y2 = person_box
            expand_x, expand_y = int((x2 - x1) * 0.06), int((y2 - y1) * 0.06)
            crop_boxes.append(
                (max(0, x1 - expand_x), max(0, y1 - expand_y), min(width, x2 + expand_x), min(height, y2 + expand_y))
            )
        radius = max(80, int(min(width, height) * 0.16))
        for points in hands.get("landmarks", []):
            palm = np.mean(np.asarray([points[i][:2] for i in (0,5,9,13,17)]), axis=0)
            x,y = int(palm[0]),int(palm[1])
            crop_boxes.append((max(0,x-radius),max(0,y-radius),min(width,x+radius),min(height,y+radius)))
            # A second, wider view preserves the edges of a landscape phone
            # extending beyond the palm; it is not a fabricated detection.
            dx,dy = int(radius*1.6),int(radius*1.2)
            crop_boxes.append((max(0,x-dx),max(0,y-dy),min(width,x+dx),min(height,y+dy)))
        for index in (9, 10):
            wrist = pose.get("keypoints", {}).get(index)
            if wrist is None or wrist[2] < 0.30:
                continue
            x, y = int(wrist[0]), int(wrist[1])
            crop_boxes.append((max(0, x - radius), max(0, y - radius), min(width, x + radius), min(height, y + radius)))
        unique_crops: list[tuple[int, int, int, int]] = []
        for candidate in crop_boxes:
            proxy = candidate + (1.0,)
            if candidate[2] - candidate[0] >= 64 and candidate[3] - candidate[1] >= 64 and all(
                box_iou(proxy, previous + (1.0,)) < 0.85 for previous in unique_crops
            ):
                unique_crops.append(candidate)
        crop_images = [frame[y1:y2, x1:x2] for x1, y1, x2, y2 in unique_crops]
        crop_predictions = self._phone_predictions(
            crop_images,
            int(self.config.get("phone_crop_image_size", 640)),
            float(self.config.get("phone_crop_confidence", 0.10)),
        )
        combined = list(full)
        for crop_box, detections in zip(unique_crops, crop_predictions):
            offset_x, offset_y = crop_box[0], crop_box[1]
            for x1, y1, x2, y2, confidence in detections:
                combined.append((x1 + offset_x, y1 + offset_y, x2 + offset_x, y2 + offset_y, confidence))
        # A tilted reflective phone can be out of distribution for upright COCO
        # images. Rotate input crops, then map real detections back to source pixels.
        for rotation in self.config.get("phone_crop_rotations", []):
            if int(rotation) != 90:
                continue
            rotated = [cv2.rotate(crop, cv2.ROTATE_90_CLOCKWISE) for crop in crop_images]
            predictions = self._phone_predictions(rotated, int(self.config.get("phone_crop_image_size",640)),
                                                  float(self.config.get("phone_crop_confidence",.10)))
            for crop_box, crop, detections in zip(unique_crops, crop_images, predictions):
                ch = crop.shape[0]
                ox, oy = crop_box[:2]
                for rx1, ry1, rx2, ry2, confidence in detections:
                    combined.append((ox+ry1, oy+ch-rx2, ox+ry2, oy+ch-rx1, confidence))
        contextual: list[tuple[int, int, int, int, float]] = []
        minimum = float(self.config.get("phone_context_confidence", 0.25))
        minimum_elongation = float(self.config.get("phone_min_elongation", 1.55))
        maximum_area_ratio = float(self.config.get("phone_max_area_ratio", 0.04))
        hand_sets = hands.get("landmarks", [])
        wrists = [pose.get("keypoints", {}).get(index) for index in (9, 10)]
        for candidate in self._nms(combined):
            x1, y1, x2, y2, confidence = candidate
            if confidence < minimum:
                continue
            box_width, box_height = max(1, x2 - x1), max(1, y2 - y1)
            elongation = max(box_width / box_height, box_height / box_width)
            area_ratio = (box_width * box_height) / float(max(1, width * height))
            if elongation < minimum_elongation or area_ratio > maximum_area_ratio:
                continue
            if elongation > float(self.config.get('phone_max_elongation',3.3)):
                continue
            pad_x = max(8, int((x2 - x1) * 0.22))
            pad_y = max(8, int((y2 - y1) * 0.22))
            left, top, right, bottom = x1 - pad_x, y1 - pad_y, x2 + pad_x, y2 + pad_y
            overlapped_hands = sum(
                sum(left <= point[0] <= right and top <= point[1] <= bottom for point in points) >= 2
                for points in hand_sets
            )
            # A phone in the current driving protocol is held by one hand. Very
            # large false boxes around the steering wheel overlap both hands.
            hand_hit = overlapped_hands == 1
            # A steering-wheel segment can overlap one surviving hand too.
            # Reject objects implausibly large relative to the nearby palm.
            if hand_hit:
                nearby_palm_spans=[]
                for points in hand_sets:
                    if sum(left<=p[0]<=right and top<=p[1]<=bottom for p in points)<2:
                        continue
                    palm=np.asarray([points[i][:2] for i in (0,5,9,13,17)])
                    nearby_palm_spans.append(float(np.max(np.linalg.norm(palm[:,None,:]-palm[None,:,:],axis=2))))
                limit=max(120*min(width,height)/720,3*max(nearby_palm_spans,default=0))
                if max(box_width,box_height)>limit:
                    continue
            # If MediaPipe loses a hand, accept only a stronger detection close
            # to a YOLO wrist. This keeps brief occlusions from killing a track.
            center_x, center_y = (x1 + x2) / 2.0, (y1 + y2) / 2.0
            wrist_radius = max(70.0, 1.35 * max(x2 - x1, y2 - y1))
            wrist_hit = any(
                wrist is not None
                and wrist[2] >= 0.30
                and float(np.hypot(center_x - wrist[0], center_y - wrist[1])) <= wrist_radius
                for wrist in wrists
            )
            if hand_hit or (not hand_sets and confidence >= 0.40 and wrist_hit):
                contextual.append(candidate)
        tracked = self.phone_tracker.update(contextual, timestamp)
        self._last_phone_mode = tracked.mode
        return tracked.boxes, tracked.mode

    def _extract_hands(self, frame: np.ndarray) -> dict[str, Any]:
        height, width = frame.shape[:2]
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        rgb.flags.writeable = False
        result = self.hand_landmarker.process(rgb)
        if not result.multi_hand_landmarks:
            return {"landmarks": [], "on_wheel": []}
        wheel_roi = self.config.get("wheel_roi")
        hands: list[list[tuple[float, float, float]]] = []
        on_wheel: list[bool] = []
        for detected in result.multi_hand_landmarks:
            points = [(float(item.x * width), float(item.y * height), float(item.z * width)) for item in detected.landmark]
            hands.append(points)
            palm = np.mean(np.asarray([points[index][:2] for index in (0, 5, 9, 13, 17)], dtype=float), axis=0)
            on_wheel.append(bool(point_in_normalized_roi((float(palm[0]), float(palm[1])), wheel_roi, width, height)))
        return {"landmarks": hands, "on_wheel": on_wheel}

    @staticmethod
    def _hand_phone_context(
        hand_landmarks: list[list[tuple[float, float, float]]],
        hand_on_wheel: list[bool],
        phone_boxes: list[tuple[int, int, int, int, float]],
    ) -> tuple[list[bool], list[bool]]:
        """Correct wheel contact when a detected phone overlaps a hand."""
        corrected = list(hand_on_wheel)
        phone_overlap: list[bool] = []
        palm_indices = (0, 5, 9, 13, 17)
        for hand_index, points in enumerate(hand_landmarks):
            valid_palm = [points[index] for index in palm_indices if index < len(points)]
            palm_x = float(np.mean([point[0] for point in valid_palm])) if valid_palm else -1.0
            palm_y = float(np.mean([point[1] for point in valid_palm])) if valid_palm else -1.0
            overlaps = False
            for x1, y1, x2, y2, _ in phone_boxes:
                pad_x = max(8, int((x2 - x1) * 0.22))
                pad_y = max(8, int((y2 - y1) * 0.22))
                left, top, right, bottom = x1 - pad_x, y1 - pad_y, x2 + pad_x, y2 + pad_y
                landmark_hits = sum(left <= point[0] <= right and top <= point[1] <= bottom for point in points)
                palm_hit = left <= palm_x <= right and top <= palm_y <= bottom
                if palm_hit or landmark_hits >= 2:
                    overlaps = True
                    break
            phone_overlap.append(overlaps)
            if overlaps and hand_index < len(corrected):
                corrected[hand_index] = False
        return corrected, phone_overlap

    @staticmethod
    def _hand_chest_context(
        hand_landmarks: list[list[tuple[float, float, float]]],
        pose_keypoints: dict[int, tuple[float, float, float]],
    ) -> list[bool]:
        """Locate a palm in the upper-torso region without naming a diagnosis."""
        required = (5, 6, 11, 12)
        if any(index not in pose_keypoints or pose_keypoints[index][2] < 0.30 for index in required):
            return [False] * len(hand_landmarks)
        left_shoulder = np.asarray(pose_keypoints[5][:2], dtype=np.float32)
        right_shoulder = np.asarray(pose_keypoints[6][:2], dtype=np.float32)
        left_hip = np.asarray(pose_keypoints[11][:2], dtype=np.float32)
        right_hip = np.asarray(pose_keypoints[12][:2], dtype=np.float32)
        left_bottom = left_shoulder + 0.58 * (left_hip - left_shoulder)
        right_bottom = right_shoulder + 0.58 * (right_hip - right_shoulder)
        # Inset the sides to avoid classifying an arm resting beside the torso.
        top_left = 0.88 * left_shoulder + 0.12 * right_shoulder
        top_right = 0.12 * left_shoulder + 0.88 * right_shoulder
        bottom_left = 0.88 * left_bottom + 0.12 * right_bottom
        bottom_right = 0.12 * left_bottom + 0.88 * right_bottom
        chest = np.asarray([top_left, top_right, bottom_right, bottom_left], dtype=np.float32)
        results: list[bool] = []
        for points in hand_landmarks:
            palm_points = [points[index][:2] for index in (0, 5, 9, 13, 17) if index < len(points)]
            if not palm_points:
                results.append(False)
                continue
            palm = np.mean(np.asarray(palm_points, dtype=np.float32), axis=0)
            results.append(cv2.pointPolygonTest(chest, (float(palm[0]), float(palm[1])), False) >= 0)
        return results

    def _rppg_roi_color(
        self, frame: np.ndarray, box: tuple[int, int, int, int]
    ) -> tuple[tuple[float, float, float] | None, float, int]:
        height, width = frame.shape[:2]
        x1, y1, x2, y2 = box
        x1, x2 = max(0, min(width - 1, x1)), max(0, min(width, x2))
        y1, y2 = max(0, min(height - 1, y1)), max(0, min(height, y2))
        if x2 <= x1 or y2 <= y1:
            return None, 0.0, 0
        roi = frame[y1:y2, x1:x2]
        ycrcb = cv2.cvtColor(roi, cv2.COLOR_BGR2YCrCb)
        y_channel, cr, cb = cv2.split(ycrcb)
        # Broad gates reject hair, deep shadows, and clipped highlights while
        # avoiding a narrow skin-tone assumption.
        mask = (
            (y_channel >= 40)
            & (y_channel <= 245)
            & (cr >= 125)
            & (cr <= 185)
            & (cb >= 65)
            & (cb <= 140)
        )
        count = int(np.count_nonzero(mask))
        ratio = count / float(mask.size) if mask.size else 0.0
        if count < int(self.config.get("rppg_min_skin_pixels", 160)) or ratio < float(self.config.get("rppg_skin_ratio_min", 0.25)):
            return None, ratio, count
        mean_bgr = np.mean(roi[mask], axis=0)
        return (float(mean_bgr[2]), float(mean_bgr[1]), float(mean_bgr[0])), ratio, count

    def _extract_rppg(self, frame: np.ndarray, face: dict[str, Any]) -> dict[str, Any]:
        if not self.config.get("rppg_enabled", True) or not face.get("readable") or not face.get("box"):
            return {}
        x1, y1, x2, y2 = face["box"]
        face_width = max(1, x2 - x1)
        face_height = max(1, y2 - y1)

        def relative_box(left: float, top: float, right: float, bottom: float) -> tuple[int, int, int, int]:
            return (
                int(x1 + left * face_width),
                int(y1 + top * face_height),
                int(x1 + right * face_width),
                int(y1 + bottom * face_height),
            )

        forehead_box = relative_box(0.29, 0.08, 0.71, 0.30)
        cheek_boxes = [relative_box(0.12, 0.53, 0.38, 0.75), relative_box(0.62, 0.53, 0.88, 0.75)]
        forehead_rgb, forehead_ratio, _ = self._rppg_roi_color(frame, forehead_box)
        cheek_samples = [self._rppg_roi_color(frame, box) for box in cheek_boxes]
        valid_cheeks = [(rgb, count) for rgb, _, count in cheek_samples if rgb is not None]
        cheeks_rgb = None
        if valid_cheeks:
            total = sum(count for _, count in valid_cheeks)
            cheeks_rgb = tuple(
                sum(rgb[channel] * count for rgb, count in valid_cheeks) / total for channel in range(3)
            )
        cheek_ratio = float(np.mean([ratio for _, ratio, _ in cheek_samples])) if cheek_samples else 0.0
        return {
            "forehead_rgb": forehead_rgb,
            "cheeks_rgb": cheeks_rgb,
            "forehead_ratio": forehead_ratio,
            "cheeks_ratio": cheek_ratio,
            "forehead_box": forehead_box,
            "cheek_boxes": cheek_boxes,
        }

    def extract(self, frame: np.ndarray, timestamp: float, collect_rppg: bool = True) -> RawFeatures:
        face = self._extract_face(frame)
        pose = self._extract_pose(frame)
        hands = self._extract_hands(frame)
        phones, phone_track_mode = self._extract_objects(frame, pose, hands, timestamp)
        hand_on_wheel, hand_phone_overlap = self._hand_phone_context(
            hands.get("landmarks", []), hands.get("on_wheel", []), phones
        )
        hand_on_chest = self._hand_chest_context(hands.get("landmarks", []), pose.get("keypoints", {}))
        hand_on_wheel = [
            on_wheel and not (index < len(hand_on_chest) and hand_on_chest[index])
            for index, on_wheel in enumerate(hand_on_wheel)
        ]
        rppg = self._extract_rppg(frame, face) if collect_rppg else {}
        reasons = [item for item in (face.get("reason"), pose.get("reason")) if item]
        head_pose = face.get("head_pose") or (None, None, None)
        gaze = face.get("gaze") or (None, None)
        height, width = frame.shape[:2]
        wheel_roi = self.config.get("wheel_roi")
        keypoints = pose.get("keypoints", {})
        nose,left,right = (keypoints.get(i) for i in (0,5,6))
        pose_head_x = None
        if nose and left and right and min(nose[2],left[2],right[2]) >= .3:
            sw = float(np.hypot(left[0]-right[0],left[1]-right[1]))
            if sw > 1:
                pose_head_x = (nose[0]-(left[0]+right[0])/2)/sw

        def wrist_on_wheel(index: int) -> bool | None:
            item = keypoints.get(index)
            if item is None or item[2] < 0.30:
                return None
            return point_in_normalized_roi((item[0], item[1]), wheel_roi, width, height)

        return RawFeatures(
            timestamp=timestamp,
            face_readable=bool(face.get("readable")),
            eyes_readable=bool(face.get("eyes_readable")),
            pose_readable=bool(pose.get("readable")),
            ear_left=face.get("ear_left"),
            ear_right=face.get("ear_right"),
            mar=face.get("mar"),
            head_pitch=head_pose[0],
            head_yaw=head_pose[1],
            head_roll=head_pose[2],
            gaze_x=gaze[0],
            gaze_y=gaze[1],
            pose_head_x=pose_head_x,
            torso_angle=pose.get("torso_angle"),
            head_drop_ratio=pose.get("head_drop_ratio"),
            phone_confidence=max((box[4] for box in phones), default=None),
            left_wrist_on_wheel=wrist_on_wheel(9),
            right_wrist_on_wheel=wrist_on_wheel(10),
            face_box=face.get("box"),
            person_box=pose.get("box"),
            phone_boxes=phones,
            phone_track_mode=phone_track_mode,
            pose_keypoints=keypoints,
            face_landmarks=face.get("landmarks", {}),
            hand_landmarks=hands.get("landmarks", []),
            hand_on_wheel=hand_on_wheel,
            hand_phone_overlap=hand_phone_overlap,
            hand_on_chest=hand_on_chest,
            rppg_forehead_rgb=rppg.get("forehead_rgb"),
            rppg_cheeks_rgb=rppg.get("cheeks_rgb"),
            rppg_forehead_skin_ratio=rppg.get("forehead_ratio"),
            rppg_cheeks_skin_ratio=rppg.get("cheeks_ratio"),
            rppg_forehead_box=rppg.get("forehead_box"),
            rppg_cheek_boxes=rppg.get("cheek_boxes", []),
            quality_reasons=reasons,
        )

    def extract_rppg_only(self, frame: np.ndarray, timestamp: float) -> RawFeatures:
        """Run only FaceMesh and color-ROI extraction on intermediate frames.

        YOLO remains at the configured analysis FPS, while rPPG receives the
        original camera frame rate needed for a less aliased color trace.
        """
        face = self._extract_face(frame)
        rppg = self._extract_rppg(frame, face)
        return RawFeatures(
            timestamp=timestamp,
            face_readable=bool(face.get("readable")),
            eyes_readable=bool(face.get("eyes_readable")),
            pose_readable=False,
            face_box=face.get("box"),
            face_landmarks=face.get("landmarks", {}),
            rppg_forehead_rgb=rppg.get("forehead_rgb"),
            rppg_cheeks_rgb=rppg.get("cheeks_rgb"),
            rppg_forehead_skin_ratio=rppg.get("forehead_ratio"),
            rppg_cheeks_skin_ratio=rppg.get("cheeks_ratio"),
            rppg_forehead_box=rppg.get("forehead_box"),
            rppg_cheek_boxes=rppg.get("cheek_boxes", []),
            quality_reasons=[] if face.get("readable") else [face.get("reason") or "face_unreadable"],
        )


POSE_EDGES = ((5, 6), (5, 7), (7, 9), (6, 8), (8, 10), (5, 11), (6, 12), (11, 12))
HAND_EDGES = (
    (0, 1), (1, 2), (2, 3), (3, 4),
    (0, 5), (5, 6), (6, 7), (7, 8),
    (0, 9), (9, 10), (10, 11), (11, 12),
    (0, 13), (13, 14), (14, 15), (15, 16),
    (0, 17), (17, 18), (18, 19), (19, 20),
    (5, 9), (9, 13), (13, 17),
)
FACE_OVAL = (10, 338, 297, 332, 284, 251, 389, 356, 454, 323, 361, 288, 397, 365, 379, 378,
             400, 377, 152, 148, 176, 149, 150, 136, 172, 58, 132, 93, 234, 127, 162, 21, 54, 103, 67, 109, 10)
LEFT_EYE_RING = (33, 160, 158, 133, 153, 144, 33)
RIGHT_EYE_RING = (362, 385, 387, 263, 373, 380, 362)
MOUTH_RING = (61, 146, 91, 181, 84, 17, 314, 405, 321, 375, 291, 308, 324, 318, 402, 317,
              14, 87, 178, 88, 95, 78, 61)


def _rounded_rect(image: np.ndarray, p1: tuple[int, int], p2: tuple[int, int], color: tuple[int, int, int], radius: int) -> None:
    x1, y1 = p1
    x2, y2 = p2
    radius = max(1, min(radius, (x2 - x1) // 2, (y2 - y1) // 2))
    cv2.rectangle(image, (x1 + radius, y1), (x2 - radius, y2), color, -1)
    cv2.rectangle(image, (x1, y1 + radius), (x2, y2 - radius), color, -1)
    for center in ((x1 + radius, y1 + radius), (x2 - radius, y1 + radius),
                   (x1 + radius, y2 - radius), (x2 - radius, y2 - radius)):
        cv2.circle(image, center, radius, color, -1, cv2.LINE_AA)


def _panel(canvas: np.ndarray, p1: tuple[int, int], p2: tuple[int, int], color: tuple[int, int, int], alpha: float, radius: int) -> None:
    overlay = canvas.copy()
    _rounded_rect(overlay, p1, p2, color, radius)
    cv2.addWeighted(overlay, alpha, canvas, 1.0 - alpha, 0, canvas)


def _points(raw: RawFeatures, indices: tuple[int, ...]) -> np.ndarray | None:
    if any(index not in raw.face_landmarks for index in indices):
        return None
    return np.asarray([(int(raw.face_landmarks[index][0]), int(raw.face_landmarks[index][1])) for index in indices], dtype=np.int32)


def _brackets(canvas: np.ndarray, box: tuple[int, int, int, int], color: tuple[int, int, int], scale: float) -> None:
    x1, y1, x2, y2 = box
    length = max(int(13 * scale), min(x2 - x1, y2 - y1) // 5)
    thickness = max(2, int(2 * scale))
    for a, b in (
        ((x1, y1 + length), (x1, y1)), ((x1, y1), (x1 + length, y1)),
        ((x2 - length, y1), (x2, y1)), ((x2, y1), (x2, y1 + length)),
        ((x1, y2 - length), (x1, y2)), ((x1, y2), (x1 + length, y2)),
        ((x2 - length, y2), (x2, y2)), ((x2, y2), (x2, y2 - length)),
    ):
        cv2.line(canvas, a, b, color, thickness, cv2.LINE_AA)


def _label(canvas: np.ndarray, text: str, origin: tuple[int, int], color: tuple[int, int, int], scale: float) -> None:
    font_scale = 0.42 * scale
    thickness = max(1, int(scale))
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness)
    x, y = origin
    _panel(canvas, (x, y - th - int(8 * scale)), (x + tw + int(14 * scale), y + int(5 * scale)), (9, 15, 25), 0.88, int(6 * scale))
    cv2.circle(canvas, (x + int(7 * scale), y - th // 2), max(2, int(2.5 * scale)), color, -1, cv2.LINE_AA)
    cv2.putText(canvas, text, (x + int(14 * scale), y), cv2.FONT_HERSHEY_SIMPLEX, font_scale, (245, 248, 252), thickness, cv2.LINE_AA)


def _metric_card(
    canvas: np.ndarray,
    x: int,
    y: int,
    width: int,
    height: int,
    title: str,
    value: str,
    detail: str,
    accent: tuple[int, int, int],
    scale: float,
) -> None:
    _panel(canvas, (x, y), (x + width, y + height), (8, 14, 24), 0.84, int(10 * scale))
    cv2.rectangle(canvas, (x, y + int(10 * scale)), (x + max(3, int(3 * scale)), y + height - int(10 * scale)), accent, -1)
    cv2.putText(canvas, title, (x + int(15 * scale), y + int(20 * scale)), cv2.FONT_HERSHEY_SIMPLEX, 0.34 * scale, (155, 170, 190), max(1, int(scale)), cv2.LINE_AA)
    cv2.putText(canvas, value, (x + int(15 * scale), y + int(46 * scale)), cv2.FONT_HERSHEY_SIMPLEX, 0.62 * scale, (248, 250, 252), max(1, int(1.5 * scale)), cv2.LINE_AA)
    cv2.putText(canvas, detail, (x + int(15 * scale), y + height - int(9 * scale)), cv2.FONT_HERSHEY_SIMPLEX, 0.30 * scale, (155, 170, 190), max(1, int(scale)), cv2.LINE_AA)


def _draw_overlay_legacy(frame: np.ndarray, raw: RawFeatures, derived: Any, decision: Any, rppg: Any | None = None) -> np.ndarray:
    canvas = frame.copy()
    state_colors = {
        "normal": (120, 245, 85),
        "caution": (0, 190, 255),
        "danger": (70, 55, 255),
        "unavailable": (165, 170, 185),
    }
    state = decision.state.value
    color = state_colors[state]
    height, width = canvas.shape[:2]
    scale = max(0.85, min(width / 960.0, height / 540.0))
    cyan = (255, 225, 35)
    magenta = (235, 70, 255)
    slate = (150, 160, 175)

    # A soft state-colored vignette and border make the overall result legible at a glance.
    glow = np.zeros_like(canvas)
    border_width = max(4, int(6 * scale))
    cv2.rectangle(glow, (0, 0), (width - 1, height - 1), color, border_width * 2)
    glow = cv2.GaussianBlur(glow, (0, 0), sigmaX=max(5, int(8 * scale)))
    cv2.addWeighted(glow, 0.50, canvas, 1.0, 0, canvas)
    cv2.rectangle(canvas, (1, 1), (width - 2, height - 2), color, border_width, cv2.LINE_AA)

    # FaceMesh contours make eye, mouth, and face tracking visible instead of hiding it in numbers.
    face_color = cyan if raw.face_readable else slate
    eye_color = (70, 80, 255) if derived.eyes_closed is True else cyan
    mouth_color = magenta if derived.mouth_open is True else cyan
    face_glow = np.zeros_like(canvas)
    for indices, feature_color, thickness in (
        (FACE_OVAL, face_color, max(1, int(1.4 * scale))),
        (LEFT_EYE_RING, eye_color, max(2, int(2.1 * scale))),
        (RIGHT_EYE_RING, eye_color, max(2, int(2.1 * scale))),
        (MOUTH_RING, mouth_color, max(1, int(1.7 * scale))),
    ):
        points = _points(raw, indices)
        if points is not None:
            cv2.polylines(face_glow, [points], True, feature_color, thickness * 5, cv2.LINE_AA)
            cv2.polylines(canvas, [points], True, feature_color, thickness, cv2.LINE_AA)
    if np.any(face_glow):
        face_glow = cv2.GaussianBlur(face_glow, (0, 0), sigmaX=max(3, int(4 * scale)))
        cv2.addWeighted(face_glow, 0.40, canvas, 1.0, 0, canvas)
    for iris in ((468, 469, 470, 471, 472), (473, 474, 475, 476, 477)):
        points = _points(raw, iris)
        if points is not None:
            center = tuple(np.mean(points, axis=0).astype(int))
            cv2.circle(canvas, center, max(2, int(3 * scale)), eye_color, -1, cv2.LINE_AA)

    # rPPG regions remain visually separate from landmarks. Forehead is the
    # primary region; cheeks are highlighted only when the fallback is active.
    if rppg is not None:
        roi_primary = (80, 245, 110)
        roi_fallback = magenta
        forehead_active = rppg.source == "forehead" or (rppg.source is None and rppg.status == "calibrating")
        cheeks_active = rppg.source == "cheeks" and rppg.status == "ready"
        if raw.rppg_forehead_box and forehead_active:
            cv2.rectangle(canvas, raw.rppg_forehead_box[:2], raw.rppg_forehead_box[2:], roi_primary, max(1, int(1.4 * scale)), cv2.LINE_AA)
        if cheeks_active:
            for cheek_box in raw.rppg_cheek_boxes:
                cv2.rectangle(canvas, cheek_box[:2], cheek_box[2:], roi_fallback, max(1, int(1.2 * scale)), cv2.LINE_AA)

    # Draw YOLO pose with a dark halo plus a crisp neon core.
    pose_glow = np.zeros_like(canvas)
    for first, second in POSE_EDGES:
        a, b = raw.pose_keypoints.get(first), raw.pose_keypoints.get(second)
        if a and b and a[2] >= 0.30 and b[2] >= 0.30:
            p1, p2 = (int(a[0]), int(a[1])), (int(b[0]), int(b[1]))
            cv2.line(pose_glow, p1, p2, cyan, max(8, int(9 * scale)), cv2.LINE_AA)
            cv2.line(canvas, p1, p2, (8, 20, 30), max(5, int(5 * scale)), cv2.LINE_AA)
            cv2.line(canvas, p1, p2, cyan, max(2, int(2 * scale)), cv2.LINE_AA)
    if np.any(pose_glow):
        pose_glow = cv2.GaussianBlur(pose_glow, (0, 0), sigmaX=max(4, int(5 * scale)))
        cv2.addWeighted(pose_glow, 0.28, canvas, 1.0, 0, canvas)
    for index, point in raw.pose_keypoints.items():
        if point[2] >= 0.30 and index in {5, 6, 7, 8, 9, 10, 11, 12}:
            center = (int(point[0]), int(point[1]))
            cv2.circle(canvas, center, max(4, int(5 * scale)), (8, 20, 30), -1, cv2.LINE_AA)
            cv2.circle(canvas, center, max(2, int(2.5 * scale)), cyan, -1, cv2.LINE_AA)
    for x1, y1, x2, y2, confidence in raw.phone_boxes:
        _panel(canvas, (x1, y1), (x2, y2), magenta, 0.12, int(8 * scale))
        _brackets(canvas, (x1, y1, x2, y2), magenta, scale)
        _label(canvas, f"PHONE  {confidence:.0%}", (x1, max(int(28 * scale), y1 - int(8 * scale))), magenta, scale)

    def fmt(value: float | None, digits: int = 2) -> str:
        return "N/A" if value is None else f"{value:.{digits}f}"

    # One consolidated top status bar replaces scattered labels and cards.
    margin = int(18 * scale)
    status_text = {"normal": "NORMAL", "caution": "CAUTION", "danger": "DANGER", "unavailable": "SIGNAL LOST"}[state]
    header_y = int(13 * scale)
    header_h = int(52 * scale)
    _panel(canvas, (margin, header_y), (width - margin, header_y + header_h), (8, 14, 24), 0.86, int(14 * scale))
    cv2.circle(canvas, (margin + int(21 * scale), header_y + header_h // 2), max(4, int(5 * scale)), color, -1, cv2.LINE_AA)
    cv2.putText(canvas, status_text, (margin + int(38 * scale), header_y + int(34 * scale)), cv2.FONT_HERSHEY_SIMPLEX,
                0.66 * scale, (250, 252, 255), max(1, int(1.7 * scale)), cv2.LINE_AA)
    divider_color = (65, 75, 90)
    first_divider = margin + int(194 * scale)
    second_divider = margin + int(398 * scale)
    time_divider = width - margin - int(91 * scale)
    for divider in (first_divider, second_divider, time_divider):
        cv2.line(canvas, (divider, header_y + int(10 * scale)), (divider, header_y + header_h - int(10 * scale)), divider_color, max(1, int(scale)), cv2.LINE_AA)

    quality = sum((derived.face_readable, derived.eyes_readable, derived.pose_readable))
    vision_x = first_divider + int(16 * scale)
    cv2.putText(canvas, "VISION", (vision_x, header_y + int(20 * scale)), cv2.FONT_HERSHEY_SIMPLEX,
                0.28 * scale, (145, 160, 180), max(1, int(scale)), cv2.LINE_AA)
    signal_items = (("FACE", derived.face_readable), ("EYES", derived.eyes_readable), ("POSE", derived.pose_readable))
    signal_x = vision_x
    for label, readable in signal_items:
        dot_color = cyan if readable else slate
        cv2.circle(canvas, (signal_x + int(4 * scale), header_y + int(38 * scale)), max(2, int(3 * scale)), dot_color, -1, cv2.LINE_AA)
        cv2.putText(canvas, label, (signal_x + int(11 * scale), header_y + int(42 * scale)), cv2.FONT_HERSHEY_SIMPLEX,
                    0.28 * scale, (235, 240, 246), max(1, int(scale)), cv2.LINE_AA)
        signal_x += int(59 * scale)

    rppg_x = second_divider + int(16 * scale)
    cv2.putText(canvas, "rPPG", (rppg_x, header_y + int(20 * scale)), cv2.FONT_HERSHEY_SIMPLEX,
                0.28 * scale, (145, 160, 180), max(1, int(scale)), cv2.LINE_AA)
    if rppg is None:
        rppg_value, rppg_detail, rcolor = "OFF", "", slate
    elif rppg.status == "ready":
        source_name = "FOREHEAD" if rppg.source == "forehead" else "CHEEK"
        rppg_value = f"{rppg.heart_rate_bpm:.0f} BPM"
        rppg_detail = f"{source_name}  SNR {rppg.snr_db:+.1f} dB"
        rcolor = (80, 245, 110) if rppg.source == "forehead" else magenta
    elif rppg.status == "calibrating":
        rppg_value = "CALIBRATING"
        rppg_detail = f"{rppg.observed_seconds:.1f}/{rppg.minimum_seconds:.0f}s"
        rcolor = slate
    else:
        rppg_value, rppg_detail, rcolor = "UNAVAILABLE", "LOW QUALITY", slate
    cv2.circle(canvas, (rppg_x + int(4 * scale), header_y + int(38 * scale)), max(2, int(3 * scale)), rcolor, -1, cv2.LINE_AA)
    cv2.putText(canvas, rppg_value, (rppg_x + int(12 * scale), header_y + int(43 * scale)), cv2.FONT_HERSHEY_SIMPLEX,
                0.46 * scale, (248, 250, 252), max(1, int(1.3 * scale)), cv2.LINE_AA)
    detail_x = rppg_x + int(112 * scale)
    cv2.putText(canvas, rppg_detail, (detail_x, header_y + int(42 * scale)), cv2.FONT_HERSHEY_SIMPLEX,
                0.27 * scale, (160, 174, 191), max(1, int(scale)), cv2.LINE_AA)
    cv2.putText(canvas, f"T+{derived.timestamp:04.1f}s", (time_divider + int(14 * scale), header_y + int(34 * scale)), cv2.FONT_HERSHEY_SIMPLEX,
                0.38 * scale, (235, 240, 246), max(1, int(scale)), cv2.LINE_AA)

    # One continuous bottom strip keeps the four feature groups aligned.
    dock_h = int(65 * scale)
    dock_y = height - dock_h - int(13 * scale)
    dock_x2 = width - margin
    _panel(canvas, (margin, dock_y), (dock_x2, dock_y + dock_h), (8, 14, 24), 0.84, int(11 * scale))
    cell_w = (dock_x2 - margin) // 4
    eye_value = "N/A" if derived.eyes_closed is None else ("CLOSED" if derived.eyes_closed else "OPEN")
    attention_value = "N/A" if derived.attention_away is None else ("AWAY" if derived.attention_away else "FORWARD")
    posture_value = "N/A" if derived.torso_lean is None else ("LEAN" if derived.torso_lean else "STABLE")
    perclos_value = "N/A" if derived.perclos is None else f"{derived.perclos:.0%}"
    cards = (
        ("EYES", eye_value, f"EAR  {fmt(derived.ear, 3)}", eye_color),
        ("ATTENTION", attention_value, f"YAW {fmt(derived.relative_yaw, 1)}  PITCH {fmt(derived.relative_pitch, 1)}", (0, 190, 255) if derived.attention_away else cyan),
        ("POSTURE", posture_value, f"LEAN {fmt(derived.relative_torso, 1)}  DROP {fmt(derived.relative_head_drop, 2)}", (0, 190, 255) if derived.torso_lean else cyan),
        ("PERCLOS", perclos_value, f"BLINKS  {derived.blink_count}", color if derived.perclos_high else cyan),
    )
    for index, card in enumerate(cards):
        title, value, detail, accent = card
        x = margin + index * cell_w
        if index:
            cv2.line(canvas, (x, dock_y + int(10 * scale)), (x, dock_y + dock_h - int(10 * scale)), divider_color, max(1, int(scale)), cv2.LINE_AA)
        cv2.circle(canvas, (x + int(15 * scale), dock_y + int(17 * scale)), max(2, int(3 * scale)), accent, -1, cv2.LINE_AA)
        cv2.putText(canvas, title, (x + int(25 * scale), dock_y + int(21 * scale)), cv2.FONT_HERSHEY_SIMPLEX,
                    0.30 * scale, (150, 165, 184), max(1, int(scale)), cv2.LINE_AA)
        cv2.putText(canvas, value, (x + int(15 * scale), dock_y + int(46 * scale)), cv2.FONT_HERSHEY_SIMPLEX,
                    0.56 * scale, (248, 250, 252), max(1, int(1.5 * scale)), cv2.LINE_AA)
        cv2.putText(canvas, detail, (x + int(140 * scale), dock_y + int(45 * scale)), cv2.FONT_HERSHEY_SIMPLEX,
                    0.27 * scale, (155, 170, 190), max(1, int(scale)), cv2.LINE_AA)
    return canvas


def draw_legacy_side_rail(
    frame: np.ndarray,
    raw: RawFeatures,
    derived: Any,
    decision: Any,
    rppg: Any | None = None,
    panel_width: int | None = None,
    ui_config: dict[str, Any] | None = None,
) -> np.ndarray:
    """Draw detections on an unobstructed frame and place all UI in a side rail."""
    scene = frame.copy()
    height, width = scene.shape[:2]
    scale = max(0.75, height / 720.0)
    panel_width = panel_width or int(round(360 * scale))
    ui_config = ui_config or {}
    presentation_style = str(ui_config.get("overlay_style", "presentation")).lower() == "presentation"
    show_rppg_rois = bool(ui_config.get("overlay_show_rppg_rois", False))
    show_hand_points = bool(ui_config.get("overlay_show_hand_points", False))
    cyan = (255, 225, 35)
    magenta = (235, 70, 255)
    green = (90, 240, 105)
    amber = (0, 185, 255)
    red = (70, 70, 255)
    slate = (145, 158, 176)
    state_colors = {
        "normal": green,
        "caution": amber,
        "danger": red,
        "unavailable": slate,
    }
    state = decision.state.value
    state_color = state_colors[state]

    # FaceMesh: only the landmarks whose location matters remain on the video.
    face_color = cyan if raw.face_readable else slate
    eye_color = red if derived.eyes_closed is True else cyan
    mouth_color = magenta if derived.mouth_open is True else cyan
    if presentation_style:
        if raw.face_box:
            _brackets(scene, raw.face_box, face_color, scale)
        for indices, active_color, active in (
            (LEFT_EYE_RING, red, derived.eyes_closed is True),
            (RIGHT_EYE_RING, red, derived.eyes_closed is True),
            (MOUTH_RING, magenta, derived.mouth_open is True),
        ):
            points = _points(raw, indices)
            if active and points is not None:
                cv2.polylines(scene, [points], True, active_color, max(2, int(1.8 * scale)), cv2.LINE_AA)
    else:
        for indices, feature_color, thickness in (
            (FACE_OVAL, face_color, max(1, int(1.2 * scale))),
            (LEFT_EYE_RING, eye_color, max(2, int(1.8 * scale))),
            (RIGHT_EYE_RING, eye_color, max(2, int(1.8 * scale))),
            (MOUTH_RING, mouth_color, max(1, int(1.5 * scale))),
        ):
            points = _points(raw, indices)
            if points is not None:
                cv2.polylines(scene, [points], True, feature_color, thickness, cv2.LINE_AA)

    # Forehead and cheeks are sampled in parallel. Both regions are visible,
    # but the thin outlines avoid hiding facial evidence.
    if show_rppg_rois and raw.rppg_forehead_box and raw.rppg_forehead_rgb is not None:
        cv2.rectangle(scene, raw.rppg_forehead_box[:2], raw.rppg_forehead_box[2:], green, max(1, int(scale)), cv2.LINE_AA)
    if show_rppg_rois and raw.rppg_cheeks_rgb is not None:
        for cheek_box in raw.rppg_cheek_boxes:
            cv2.rectangle(scene, cheek_box[:2], cheek_box[2:], magenta, max(1, int(scale)), cv2.LINE_AA)

    # YOLO body pose.
    pose_edges = ((5, 6), (5, 11), (6, 12), (11, 12)) if presentation_style else POSE_EDGES
    for first, second in pose_edges:
        a, b = raw.pose_keypoints.get(first), raw.pose_keypoints.get(second)
        if a and b and a[2] >= 0.30 and b[2] >= 0.30:
            p1, p2 = (int(a[0]), int(a[1])), (int(b[0]), int(b[1]))
            cv2.line(scene, p1, p2, (8, 20, 30), max(4, int(4 * scale)), cv2.LINE_AA)
            cv2.line(scene, p1, p2, cyan, max(1, int(2 * scale)), cv2.LINE_AA)
    visible_pose_points = {5, 6, 11, 12} if presentation_style else {5, 6, 7, 8, 9, 10, 11, 12}
    for index, point in raw.pose_keypoints.items():
        if point[2] >= 0.30 and index in visible_pose_points:
            center = (int(point[0]), int(point[1]))
            cv2.circle(scene, center, max(3, int(4 * scale)), (8, 20, 30), -1, cv2.LINE_AA)
            cv2.circle(scene, center, max(2, int(2 * scale)), cyan, -1, cv2.LINE_AA)

    # MediaPipe hand landmarks: green is wheel contact, magenta is phone-hand
    # overlap, and amber is a visible hand away from both regions.
    for hand_index, points in enumerate(raw.hand_landmarks):
        if hand_index < len(raw.hand_on_chest) and raw.hand_on_chest[hand_index]:
            hand_color = red
        elif hand_index < len(raw.hand_phone_overlap) and raw.hand_phone_overlap[hand_index]:
            hand_color = magenta
        elif hand_index < len(raw.hand_on_wheel) and raw.hand_on_wheel[hand_index]:
            hand_color = green
        else:
            hand_color = amber
        if presentation_style:
            palm_indices = [index for index in (0, 5, 9, 13, 17) if index < len(points)]
            if len(palm_indices) >= 3:
                palm = np.asarray([(int(points[index][0]), int(points[index][1])) for index in palm_indices], dtype=np.int32)
                cv2.polylines(scene, [palm], True, hand_color, max(1, int(1.8 * scale)), cv2.LINE_AA)
        else:
            for first, second in HAND_EDGES:
                if first < len(points) and second < len(points):
                    cv2.line(
                        scene,
                        (int(points[first][0]), int(points[first][1])),
                        (int(points[second][0]), int(points[second][1])),
                        hand_color,
                        max(1, int(1.4 * scale)),
                        cv2.LINE_AA,
                    )
        if show_hand_points:
            for point in points:
                cv2.circle(scene, (int(point[0]), int(point[1])), max(1, int(2 * scale)), hand_color, -1, cv2.LINE_AA)

    # Direct phone detections are magenta; short tracked gaps are amber.
    phone_color = magenta if raw.phone_track_mode == "detected" else amber
    for x1, y1, x2, y2, confidence in raw.phone_boxes:
        _brackets(scene, (x1, y1, x2, y2), phone_color, scale)
        label = f"PHONE {confidence:.0%}" if raw.phone_track_mode == "detected" else "PHONE TRACK"
        _label(scene, label, (x1, max(int(24 * scale), y1 - int(5 * scale))), phone_color, scale)

    # A subtle state line is the only UI element touching the video.
    cv2.rectangle(scene, (0, 0), (max(2, int(5 * scale)), height - 1), state_color, -1)

    # Dedicated right-side dashboard: no face or body pixels are covered.
    panel = np.zeros((height, panel_width, 3), dtype=np.uint8)
    gradient = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None, None]
    top = np.asarray([24, 19, 14], dtype=np.float32)[None, None, :]
    bottom = np.asarray([13, 17, 25], dtype=np.float32)[None, None, :]
    panel[:] = (top * (1.0 - gradient) + bottom * gradient).astype(np.uint8)
    cv2.rectangle(panel, (0, 0), (max(2, int(3 * scale)), height - 1), state_color, -1)

    pad = int(24 * scale)
    right = panel_width - pad
    white = (244, 247, 251)
    muted = (145, 160, 181)
    divider = (57, 68, 84)
    thin = max(1, int(scale))

    def text(value: str, x: int, y: int, size: float, color: tuple[int, int, int] = white, thickness: int | None = None) -> None:
        cv2.putText(panel, value, (x, y), cv2.FONT_HERSHEY_SIMPLEX, size * scale, color,
                    thickness or max(1, int(1.1 * scale)), cv2.LINE_AA)

    def rule(y: int) -> None:
        cv2.line(panel, (pad, y), (right, y), divider, thin, cv2.LINE_AA)

    def row(label: str, value: str, y: int, value_color: tuple[int, int, int] = white) -> None:
        text(label, pad, y, 0.31, muted)
        (tw, _), _ = cv2.getTextSize(value, cv2.FONT_HERSHEY_SIMPLEX, 0.43 * scale, max(1, int(1.2 * scale)))
        text(value, right - tw, y, 0.43, value_color)

    text("DRIVER MONITOR", pad, int(31 * scale), 0.42, white)
    cv2.circle(panel, (right - int(37 * scale), int(25 * scale)), max(2, int(3 * scale)), green, -1, cv2.LINE_AA)
    text("LIVE", right - int(28 * scale), int(30 * scale), 0.25, muted)
    text(f"T+{derived.timestamp:04.1f}s", pad, int(55 * scale), 0.27, muted)

    state_text = {"normal": "NORMAL", "caution": "CAUTION", "danger": "DANGER", "unavailable": "SIGNAL LOST"}[state]
    cv2.circle(panel, (pad + int(6 * scale), int(91 * scale)), max(4, int(5 * scale)), state_color, -1, cv2.LINE_AA)
    text(state_text, pad + int(22 * scale), int(101 * scale), 0.88, white, max(2, int(2 * scale)))
    reason_labels = {
        "collapse_posture": "COLLAPSE POSTURE",
        "distress_gesture": "CHEST DISTRESS GESTURE",
        "eyes_closed": "EYES CLOSED",
        "attention_away": "ATTENTION AWAY",
        "attention_away_repeated": "REPEATED GLANCES",
        "head_abnormal": "HEAD TURN",
        "yawning": "YAWN",
        "perclos_high": "HIGH PERCLOS",
        "torso_lean": "TORSO LEAN",
        "face_unreadable": "FACE UNREADABLE",
        "eyes_unreadable": "EYES UNREADABLE",
    }
    reason_priority = {
        "collapse_posture": 0,
        "distress_gesture": 1,
        "eyes_closed": 2,
        "attention_away": 3,
        "attention_away_repeated": 4,
    }
    visible_reasons = sorted(decision.reasons, key=lambda value: reason_priority.get(value, 10))
    reasons = ", ".join(reason_labels.get(value, value.replace("_", " ").upper()) for value in visible_reasons[:2])
    reasons = reasons if reasons else "MONITORING"
    text(reasons, pad, int(124 * scale), 0.24, state_color)
    rule(int(143 * scale))

    text("VISION SIGNAL", pad, int(169 * scale), 0.29, muted)
    signal_items = (
        ("FACE", derived.face_readable),
        ("EYES", derived.eyes_readable),
        ("POSE", derived.pose_readable),
        ("HANDS", bool(raw.hand_landmarks)),
    )
    signal_x = pad
    for label, available in signal_items:
        dot_color = cyan if available else slate
        cv2.circle(panel, (signal_x + int(3 * scale), int(194 * scale)), max(2, int(3 * scale)), dot_color, -1, cv2.LINE_AA)
        text(label, signal_x + int(10 * scale), int(199 * scale), 0.24, white)
        signal_x += int(76 * scale)
    rule(int(218 * scale))

    text("rPPG FUSION", pad, int(244 * scale), 0.29, muted)
    if rppg is not None and rppg.status == "disabled":
        main_rppg, source_label, rppg_color = "VISION ONLY", "rPPG: NORMAL MODE", slate
    elif rppg is not None and rppg.status == "ready":
        main_rppg = f"{rppg.heart_rate_bpm:.0f} BPM"
        source_label = "FUSED" if rppg.source == "fusion" else str(rppg.source or "SINGLE").upper()
        rppg_color = green if rppg.source in {"fusion", "forehead"} else magenta
    elif rppg is not None and rppg.status == "calibrating":
        main_rppg, source_label, rppg_color = "CALIBRATING", f"{rppg.observed_seconds:.1f}/{rppg.minimum_seconds:.0f}s", slate
    else:
        main_rppg, source_label, rppg_color = "UNAVAILABLE", "QUALITY GATE", slate
    text(main_rppg, pad, int(282 * scale), 0.72, white, max(2, int(2 * scale)))
    text(source_label, pad, int(302 * scale), 0.24, rppg_color)
    if rppg is not None:
        forehead_value = f"{rppg.forehead_bpm:.0f} BPM  {rppg.forehead_snr_db:+.1f}dB" if rppg.forehead_bpm is not None else "--"
        cheeks_value = f"{rppg.cheeks_bpm:.0f} BPM  {rppg.cheeks_snr_db:+.1f}dB" if rppg.cheeks_bpm is not None else "--"
    else:
        forehead_value = cheeks_value = "--"
    row("FOREHEAD", forehead_value, int(327 * scale), green if forehead_value != "--" else muted)
    row("CHEEKS", cheeks_value, int(353 * scale), magenta if cheeks_value != "--" else muted)
    rule(int(373 * scale))

    text("BEHAVIOR", pad, int(399 * scale), 0.29, muted)
    eye_value = "N/A" if derived.eyes_closed is None else ("CLOSED" if derived.eyes_closed else "OPEN")
    attention_value = "N/A" if derived.attention_away is None else ("AWAY" if derived.attention_away else "FORWARD")
    posture_value = "N/A" if derived.torso_lean is None else ("LEAN" if derived.torso_lean else "STABLE")
    perclos_value = "N/A" if derived.perclos is None else f"{derived.perclos:.0%}"
    phone_value = "NO"
    if raw.phone_track_mode == "detected":
        phone_value = "DETECTED"
    elif raw.phone_track_mode == "tracked":
        phone_value = "TRACKED"
    hands_detected = len(raw.hand_landmarks)
    hands_on_wheel = sum(raw.hand_on_wheel)
    hands_on_phone = sum(raw.hand_phone_overlap)
    hands_on_chest = sum(raw.hand_on_chest)
    if hands_detected == 0:
        hands_value = "N/A"
    elif hands_on_chest:
        hands_value = f"{hands_on_chest} CHEST CONTACT"
    elif hands_on_phone:
        hands_value = f"{hands_on_wheel} WHEEL / {hands_on_phone} PHONE"
    else:
        hands_value = f"{hands_on_wheel}/{hands_detected} ON WHEEL"
    row("EYES", eye_value, int(430 * scale), red if eye_value == "CLOSED" else white)
    row("ATTENTION", attention_value, int(469 * scale), amber if attention_value == "AWAY" else white)
    row("POSTURE", posture_value, int(508 * scale), amber if posture_value == "LEAN" else white)
    row("PERCLOS", perclos_value, int(547 * scale), amber if derived.perclos_high else white)
    row("PHONE", phone_value, int(586 * scale), magenta if phone_value != "NO" else white)
    hands_color = red if hands_on_chest else magenta if hands_on_phone else green if hands_on_wheel else amber if hands_detected else muted
    row("HANDS", hands_value, int(625 * scale), hands_color)
    rule(int(651 * scale))
    text("PROTOTYPE  /  NOT MEDICAL", pad, height - int(28 * scale), 0.25, muted)

    return np.hstack((scene, panel))
