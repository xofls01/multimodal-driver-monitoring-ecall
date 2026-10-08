"""MediaPipe Tasks API 래퍼: 얼굴 랜드마크 / 손 랜드마크."""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks.python import BaseOptions, vision

from . import geometry as G
from .models import ensure_model


def _model_buffer(name: str) -> bytes:
    # 경로에 한글이 있으면 MediaPipe 가 파일을 못 열어서, 파일은 파이썬으로 읽고 바이트로 넘긴다
    with open(ensure_model(name), "rb") as f:
        return f.read()


@dataclass
class FaceObs:
    pts: np.ndarray                 # (478,2) 픽셀 좌표
    ear_l: float
    ear_r: float
    ear: float
    mar: float
    pitch: float                    # deg, +가 숙임 (sign 보정 전)
    yaw: float
    roll: float
    nose_ratio: float
    blink_l: float = 0.0            # blendshape eyeBlinkLeft
    blink_r: float = 0.0
    bbox: tuple = (0, 0, 0, 0)


@dataclass
class HandObs:
    pts: np.ndarray                 # (21,2) 픽셀 좌표
    label: str                      # "Left" | "Right" (MediaPipe 기준)
    score: float
    curl: float = 0.0
    on_wheel: bool = False
    in_ratio: float = 0.0


def _to_mp_image(frame_bgr: np.ndarray) -> mp.Image:
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    return mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)


class Perception:
    def __init__(self, enable_hands: bool = True, num_hands: int = 2):
        face_opts = vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_buffer=_model_buffer("face_landmarker.task")),
            running_mode=vision.RunningMode.VIDEO,
            num_faces=1,
            min_face_detection_confidence=0.5,
            min_face_presence_confidence=0.5,
            min_tracking_confidence=0.5,
            output_face_blendshapes=True,
            output_facial_transformation_matrixes=True,
        )
        self.face = vision.FaceLandmarker.create_from_options(face_opts)
        self.hands = None
        if enable_hands:
            hand_opts = vision.HandLandmarkerOptions(
                base_options=BaseOptions(model_asset_buffer=_model_buffer("hand_landmarker.task")),
                running_mode=vision.RunningMode.VIDEO,
                num_hands=num_hands,
                min_hand_detection_confidence=0.4,
                min_hand_presence_confidence=0.4,
                min_tracking_confidence=0.4,
            )
            self.hands = vision.HandLandmarker.create_from_options(hand_opts)
        self._last_ts = -1

    def _ts(self, ts_ms: int) -> int:
        # VIDEO 모드는 timestamp 가 단조 증가해야 함
        ts_ms = max(int(ts_ms), self._last_ts + 1)
        self._last_ts = ts_ms
        return ts_ms

    def process(self, frame_bgr: np.ndarray, ts_ms: int) -> tuple[FaceObs | None, list[HandObs]]:
        h, w = frame_bgr.shape[:2]
        img = _to_mp_image(frame_bgr)
        ts = self._ts(ts_ms)

        face_obs = None
        fr = self.face.detect_for_video(img, ts)
        if fr.face_landmarks:
            lm = fr.face_landmarks[0]
            pts = np.array([[p.x * w, p.y * h] for p in lm], dtype=np.float32)
            ear_l = G.eye_aspect_ratio(pts, G.LEFT_EYE)
            ear_r = G.eye_aspect_ratio(pts, G.RIGHT_EYE)
            pitch = yaw = roll = 0.0
            if fr.facial_transformation_matrixes:
                pitch, yaw, roll = G.euler_from_matrix(np.array(fr.facial_transformation_matrixes[0]))
            bl = br = 0.0
            if fr.face_blendshapes:
                bs = {c.category_name: c.score for c in fr.face_blendshapes[0]}
                bl, br = bs.get("eyeBlinkLeft", 0.0), bs.get("eyeBlinkRight", 0.0)
            x0, y0 = pts.min(axis=0)
            x1, y1 = pts.max(axis=0)
            face_obs = FaceObs(
                pts=pts, ear_l=ear_l, ear_r=ear_r, ear=(ear_l + ear_r) / 2,
                mar=G.mouth_aspect_ratio(pts), pitch=pitch, yaw=yaw, roll=roll,
                nose_ratio=G.nose_drop_ratio(pts), blink_l=bl, blink_r=br,
                bbox=(int(x0), int(y0), int(x1), int(y1)),
            )

        hands: list[HandObs] = []
        if self.hands is not None:
            hr = self.hands.detect_for_video(img, ts)
            for i, lm in enumerate(hr.hand_landmarks or []):
                pts = np.array([[p.x * w, p.y * h] for p in lm], dtype=np.float32)
                cat = hr.handedness[i][0] if hr.handedness else None
                hands.append(HandObs(
                    pts=pts,
                    label=cat.category_name if cat else "?",
                    score=cat.score if cat else 0.0,
                    curl=G.hand_curl(pts),
                ))
        return face_obs, hands

    def close(self):
        self.face.close()
        if self.hands is not None:
            self.hands.close()
