"""순수 기하 계산 함수들 (모델 없이 단위 테스트 가능)."""
from __future__ import annotations

import math
import numpy as np

# MediaPipe Face Mesh 인덱스 (이미지 기준 왼/오른쪽)
# EAR 6점: p1(바깥 코너), p2, p3(위), p4(안쪽 코너), p5, p6(아래)
LEFT_EYE = [33, 160, 158, 133, 153, 144]
RIGHT_EYE = [362, 385, 387, 263, 373, 380]
# 입: 좌우 코너, 위/아래 안쪽 입술 3쌍
MOUTH = {"corners": (78, 308), "pairs": [(81, 178), (13, 14), (311, 402)]}
NOSE_TIP = 1
CHIN = 152
FOREHEAD = 10


def _d(a, b) -> float:
    return float(np.linalg.norm(np.asarray(a, dtype=float) - np.asarray(b, dtype=float)))


def eye_aspect_ratio(pts: np.ndarray, idx: list[int]) -> float:
    """EAR = (|p2-p6| + |p3-p5|) / (2|p1-p4|). pts: (N,2) 픽셀 좌표."""
    p1, p2, p3, p4, p5, p6 = (pts[i] for i in idx)
    w = _d(p1, p4)
    if w < 1e-6:
        return 0.0
    return (_d(p2, p6) + _d(p3, p5)) / (2.0 * w)


def mouth_aspect_ratio(pts: np.ndarray) -> float:
    l, r = MOUTH["corners"]
    w = _d(pts[l], pts[r])
    if w < 1e-6:
        return 0.0
    h = sum(_d(pts[a], pts[b]) for a, b in MOUTH["pairs"]) / len(MOUTH["pairs"])
    return h / w


def euler_from_matrix(m: np.ndarray) -> tuple[float, float, float]:
    """MediaPipe facial transformation matrix(4x4) -> (pitch, yaw, roll) [deg].

    MediaPipe 좌표계: y 위, z 카메라 쪽. 정면이면 R≈I.
    고개를 숙이면(턱이 가슴 쪽) pitch > 0 이 되도록 정의.
    """
    R = np.asarray(m, dtype=float)[:3, :3]
    # 스케일 제거
    R = R / np.linalg.norm(R, axis=0, keepdims=True)
    pitch = math.degrees(math.atan2(R[2, 1], R[2, 2]))
    yaw = math.degrees(math.asin(max(-1.0, min(1.0, -R[2, 0]))))
    roll = math.degrees(math.atan2(R[1, 0], R[0, 0]))
    return pitch, yaw, roll


def nose_drop_ratio(pts: np.ndarray) -> float:
    """보조 지표: (코끝 - 이마) / (턱 - 이마) 세로 비율. 고개를 숙일수록 커짐."""
    top, nose, chin = pts[FOREHEAD], pts[NOSE_TIP], pts[CHIN]
    h = chin[1] - top[1]
    if abs(h) < 1e-6:
        return 0.0
    return float((nose[1] - top[1]) / h)


# ---------------- 손 ----------------
# 파지 판정에 쓰는 key point: 손목, MCP 4개, 손끝 5개
HAND_KEYPOINTS = [0, 5, 9, 13, 17, 4, 8, 12, 16, 20]
FINGER_TIPS = [8, 12, 16, 20]


def hand_curl(pts: np.ndarray) -> float:
    """손가락 말림 정도: 손끝~손바닥중심 평균거리 / 손바닥 크기. 작을수록 주먹(쥔 상태)."""
    palm_c = pts[[0, 5, 9, 13, 17]].mean(axis=0)
    palm_size = _d(pts[0], pts[9]) + 1e-6
    return float(np.mean([_d(pts[i], palm_c) for i in FINGER_TIPS]) / palm_size)
