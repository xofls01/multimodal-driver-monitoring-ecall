from __future__ import annotations

import math
from collections import deque
from dataclasses import asdict, dataclass, field
from statistics import median
from typing import Any, Iterable, Sequence

import numpy as np


# MediaPipe Face Mesh indexing.
LEFT_EYE = (33, 160, 158, 133, 153, 144)
RIGHT_EYE = (362, 385, 387, 263, 373, 380)
MOUTH_CORNERS = (78, 308)
MOUTH_LIPS = (13, 14)

# refine_landmarks=True adds one iris center per eye. Ratios are measured
# inside each eye so small changes in camera distance do not dominate them.
IRIS_EYES = (
    (468, 33, 133, 159, 145),
    (473, 362, 263, 386, 374),
)


def _distance(a: Sequence[float], b: Sequence[float]) -> float:
    return math.hypot(float(a[0]) - float(b[0]), float(a[1]) - float(b[1]))


def eye_aspect_ratio(points: Sequence[Sequence[float]], indices: Sequence[int]) -> float | None:
    if len(indices) != 6 or any(index >= len(points) for index in indices):
        return None
    p1, p2, p3, p4, p5, p6 = (points[index] for index in indices)
    width = 2.0 * _distance(p1, p4)
    if width <= 1e-6:
        return None
    return (_distance(p2, p6) + _distance(p3, p5)) / width


def mouth_aspect_ratio(points: Sequence[Sequence[float]]) -> float | None:
    if any(index >= len(points) for index in (*MOUTH_CORNERS, *MOUTH_LIPS)):
        return None
    width = _distance(points[MOUTH_CORNERS[0]], points[MOUTH_CORNERS[1]])
    if width <= 1e-6:
        return None
    return _distance(points[MOUTH_LIPS[0]], points[MOUTH_LIPS[1]]) / width


def iris_gaze_ratio(points: Sequence[Sequence[float]]) -> tuple[float, float] | None:
    """Return median iris position within the two visible eye apertures.

    These are camera-relative features, not anatomical gaze angles. They are
    only used after per-clip neutral calibration.
    """
    samples: list[tuple[float, float]] = []
    for iris, corner_a, corner_b, upper, lower in IRIS_EYES:
        if any(index >= len(points) for index in (iris, corner_a, corner_b, upper, lower)):
            continue
        x_min, x_max = sorted((float(points[corner_a][0]), float(points[corner_b][0])))
        y_min, y_max = sorted((float(points[upper][1]), float(points[lower][1])))
        if x_max - x_min <= 1e-6 or y_max - y_min <= 1e-6:
            continue
        x_ratio = (float(points[iris][0]) - x_min) / (x_max - x_min)
        y_ratio = (float(points[iris][1]) - y_min) / (y_max - y_min)
        if -0.25 <= x_ratio <= 1.25 and -0.5 <= y_ratio <= 1.5:
            samples.append((x_ratio, y_ratio))
    if not samples:
        return None
    return float(median(value[0] for value in samples)), float(median(value[1] for value in samples))


def estimate_head_pose(points: Sequence[Sequence[float]]) -> tuple[float, float, float] | None:
    """Return stable Face-Mesh yaw/pitch proxies and eye-line roll.

    Independent frame-wise solvePnP can jump between mathematically equivalent
    rotations for an oblique in-cabin camera.  Ratios in the tracked face mesh
    are less absolute, but much more stable after per-clip neutral calibration.
    The first two outputs use degree-like scaling and must not be interpreted as
    calibrated anatomical angles.
    """
    if any(index >= len(points) for index in (1, 33, 263)):
        return None
    left_eye = points[33]
    right_eye = points[263]
    nose = points[1]
    eye_distance = _distance(left_eye, right_eye)
    if eye_distance <= 1e-6:
        return None
    eye_mid_x = (float(left_eye[0]) + float(right_eye[0])) / 2.0
    eye_mid_y = (float(left_eye[1]) + float(right_eye[1])) / 2.0
    yaw_proxy = (float(nose[0]) - eye_mid_x) / eye_distance * 60.0
    pitch_proxy = (float(nose[1]) - eye_mid_y) / eye_distance * 60.0
    roll = math.degrees(math.atan2(float(right_eye[1]) - float(left_eye[1]), float(right_eye[0]) - float(left_eye[0])))
    return pitch_proxy, yaw_proxy, roll


def torso_angle(keypoints: dict[int, tuple[float, float, float]]) -> float | None:
    required = (5, 6, 11, 12)
    if any(index not in keypoints or keypoints[index][2] <= 0 for index in required):
        return None
    left_shoulder = keypoints[5]
    right_shoulder = keypoints[6]
    left_hip = keypoints[11]
    right_hip = keypoints[12]
    shoulder_mid = ((left_shoulder[0] + right_shoulder[0]) / 2.0, (left_shoulder[1] + right_shoulder[1]) / 2.0)
    hip_mid = ((left_hip[0] + right_hip[0]) / 2.0, (left_hip[1] + right_hip[1]) / 2.0)
    dx = shoulder_mid[0] - hip_mid[0]
    dy = hip_mid[1] - shoulder_mid[1]
    if abs(dx) + abs(dy) <= 1e-6:
        return None
    return math.degrees(math.atan2(dx, dy))


def head_drop_ratio(keypoints: dict[int, tuple[float, float, float]]) -> float | None:
    required = (0, 5, 6)
    if any(index not in keypoints or keypoints[index][2] <= 0 for index in required):
        return None
    nose = keypoints[0]
    left = keypoints[5]
    right = keypoints[6]
    shoulder_width = _distance(left, right)
    if shoulder_width <= 1e-6:
        return None
    shoulder_y = (left[1] + right[1]) / 2.0
    return (nose[1] - shoulder_y) / shoulder_width


def point_in_normalized_roi(
    point: tuple[float, float], roi: Sequence[float] | None, width: int, height: int
) -> bool | None:
    if roi is None:
        return None
    if len(roi) != 4:
        raise ValueError("wheel_roi must be [x1, y1, x2, y2] in normalized coordinates")
    x, y = point[0] / width, point[1] / height
    x1, y1, x2, y2 = map(float, roi)
    return x1 <= x <= x2 and y1 <= y <= y2


@dataclass
class RawFeatures:
    timestamp: float
    face_readable: bool
    eyes_readable: bool
    pose_readable: bool
    ear_left: float | None = None
    ear_right: float | None = None
    mar: float | None = None
    head_pitch: float | None = None
    head_yaw: float | None = None
    head_roll: float | None = None
    gaze_x: float | None = None
    gaze_y: float | None = None
    pose_head_x: float | None = None
    torso_angle: float | None = None
    head_drop_ratio: float | None = None
    phone_confidence: float | None = None
    left_wrist_on_wheel: bool | None = None
    right_wrist_on_wheel: bool | None = None
    face_box: tuple[int, int, int, int] | None = None
    person_box: tuple[int, int, int, int] | None = None
    phone_boxes: list[tuple[int, int, int, int, float]] = field(default_factory=list)
    phone_track_mode: str | None = None
    pose_keypoints: dict[int, tuple[float, float, float]] = field(default_factory=dict)
    face_landmarks: dict[int, tuple[float, float]] = field(default_factory=dict)
    hand_landmarks: list[list[tuple[float, float, float]]] = field(default_factory=list)
    hand_on_wheel: list[bool] = field(default_factory=list)
    hand_phone_overlap: list[bool] = field(default_factory=list)
    hand_on_chest: list[bool] = field(default_factory=list)
    rppg_forehead_rgb: tuple[float, float, float] | None = None
    rppg_cheeks_rgb: tuple[float, float, float] | None = None
    rppg_forehead_skin_ratio: float | None = None
    rppg_cheeks_skin_ratio: float | None = None
    rppg_forehead_box: tuple[int, int, int, int] | None = None
    rppg_cheek_boxes: list[tuple[int, int, int, int]] = field(default_factory=list)
    quality_reasons: list[str] = field(default_factory=list)

    @property
    def ear(self) -> float | None:
        values = [value for value in (self.ear_left, self.ear_right) if value is not None]
        return min(values) if len(values) == 2 else None


@dataclass
class CalibrationProfile:
    status: str = "calibrating"
    sample_count: int = 0
    neutral_ear: float | None = None
    neutral_mar: float | None = None
    neutral_pitch: float | None = None
    neutral_yaw: float | None = None
    neutral_gaze_x: float | None = None
    neutral_gaze_y: float | None = None
    neutral_torso: float | None = None
    neutral_head_drop: float | None = None
    neutral_pose_head_x: float | None = None


@dataclass
class DerivedFeatures:
    timestamp: float
    calibration_status: str
    face_readable: bool
    eyes_readable: bool
    pose_readable: bool
    eyes_closed: bool | None
    mouth_open: bool | None
    attention_away: bool | None
    head_abnormal: bool | None
    gaze_away: bool | None
    torso_lean: bool | None
    collapse_posture: bool | None
    distress_gesture: bool | None
    phone_visible: bool | None
    hands_on_wheel: bool | None
    ear: float | None
    mar: float | None
    relative_pitch: float | None
    relative_yaw: float | None
    relative_gaze_x: float | None
    relative_gaze_y: float | None
    relative_torso: float | None
    relative_head_drop: float | None
    perclos: float | None
    perclos_high: bool | None
    blink_count: int
    quality_reasons: list[str]
    eye_closed_seconds: float = 0.0
    eye_observed_seconds: float = 0.0
    eye_coverage: float = 0.0
    blink_rate_per_minute: float | None = None
    last_blink_duration: float | None = None
    no_blink_seconds: float | None = None
    long_blink_count: int = 0
    long_blinks_repeated: bool | None = None
    nod_count: int = 0
    nods_repeated: bool | None = None
    attention_source: str = "unavailable"
    measurement_coverage: str = "unavailable"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FeatureTracker:
    """Calibrate per clip, smooth continuous signals, and derive tri-state events."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.profile = CalibrationProfile()
        self._samples: dict[str, list[float]] = {
            name: [] for name in ("ear", "mar", "pitch", "yaw", "gaze_x", "gaze_y", "torso", "head_drop", "pose_head_x")
        }
        self._ema: dict[str, float] = {}
        self._eye_state: bool | None = None
        self._eye_state_started: float | None = None
        self._blink_count = 0
        self._perclos: deque[tuple[float, float, bool]] = deque()
        self._eye_previous_timestamp: float | None = None
        self._eye_previous_state: bool | None = None
        self._blinks: deque[tuple[float,float]] = deque()
        self._last_blink_duration: float | None = None
        self._open_started: float | None = None
        self._eye_observed_seconds = 0.0
        self._eye_coverage = 0.0
        self._nods: deque[float] = deque()
        self._nod_started: float | None = None
        self._head_history: dict[str, deque[float]] = {
            "relative_pitch": deque(maxlen=int(config["head_median_window_samples"])),
            "relative_yaw": deque(maxlen=int(config["head_median_window_samples"])),
        }

    def _smooth(self, name: str, value: float | None) -> float | None:
        if value is None or not math.isfinite(value):
            return None
        alpha = float(self.config["ema_alpha"])
        current = self._ema.get(name, value)
        current = alpha * value + (1.0 - alpha) * current
        self._ema[name] = current
        return current

    def _smooth_head(self, name: str, value: float | None) -> float | None:
        if value is None or not math.isfinite(value):
            return None
        self._head_history[name].append(value)
        return self._smooth(name, median(self._head_history[name]))

    def _collect_calibration(self, raw: RawFeatures) -> None:
        if self.profile.status != "calibrating" or raw.timestamp > float(self.config["calibration_max_seconds"]):
            return
        candidates = {
            "ear": raw.ear if raw.eyes_readable else None,
            "mar": raw.mar if raw.face_readable else None,
            "pitch": raw.head_pitch if raw.face_readable else None,
            "yaw": raw.head_yaw if raw.face_readable else None,
            "gaze_x": raw.gaze_x if raw.eyes_readable else None,
            "gaze_y": raw.gaze_y if raw.eyes_readable else None,
            "torso": raw.torso_angle if raw.pose_readable else None,
            "head_drop": raw.head_drop_ratio if raw.pose_readable else None,
            "pose_head_x": raw.pose_head_x,
        }
        for name, value in candidates.items():
            if value is not None and math.isfinite(value):
                self._samples[name].append(float(value))

    def _finalize_calibration_if_ready(self, timestamp: float) -> None:
        minimum = int(self.config["calibration_min_samples"])
        if timestamp < float(self.config["calibration_seconds"]):
            return
        if self.profile.status != "calibrating":
            return
        face_ready = all(len(self._samples[name]) >= minimum for name in ("pitch","yaw"))
        pose_ready = all(len(self._samples[name]) >= minimum for name in ("torso","head_drop"))
        if not (face_ready or pose_ready):
            if timestamp < float(self.config["calibration_max_seconds"]):
                return
            self.profile.status = "insufficient"
            self.profile.sample_count = max(len(self._samples["pitch"]), len(self._samples["torso"]))
            return
        def circular_mean(values: list[float]) -> float:
            radians = np.radians(values)
            return math.degrees(math.atan2(float(np.mean(np.sin(radians))), float(np.mean(np.cos(radians)))))

        self.profile = CalibrationProfile(
            status="ready",
            sample_count=max(len(self._samples["pitch"]), len(self._samples["torso"])),
            # Upper-eye and lower-mouth quantiles resist blinks and speech/yawns
            # during the otherwise neutral calibration interval.
            neutral_ear=float(np.quantile(self._samples["ear"], 0.70)) if len(self._samples["ear"]) >= minimum else None,
            neutral_mar=float(np.quantile(self._samples["mar"], 0.30)) if self._samples["mar"] else None,
            neutral_pitch=circular_mean(self._samples["pitch"]) if face_ready else None,
            neutral_yaw=circular_mean(self._samples["yaw"]) if face_ready else None,
            neutral_gaze_x=median(self._samples["gaze_x"]) if self._samples["gaze_x"] else None,
            neutral_gaze_y=median(self._samples["gaze_y"]) if self._samples["gaze_y"] else None,
            neutral_torso=median(self._samples["torso"]) if self._samples["torso"] else None,
            neutral_head_drop=median(self._samples["head_drop"]) if self._samples["head_drop"] else None,
            neutral_pose_head_x=median(self._samples["pose_head_x"]) if self._samples["pose_head_x"] else None,
        )

    def _eye_closed(self, ear: float | None, timestamp: float) -> bool | None:
        if ear is None or self.profile.neutral_ear is None:
            self._eye_state = None
            self._eye_state_started = None
            return None
        close_threshold = min(
            float(self.config["eye_threshold_max"]),
            max(float(self.config["eye_threshold_min"]), self.profile.neutral_ear * float(self.config["eye_close_ratio"])),
        )
        open_threshold = close_threshold + float(self.config["eye_hysteresis"])
        previous = self._eye_state
        if previous is True:
            state = not (ear >= open_threshold)
        elif previous is False:
            state = ear <= close_threshold
        else:
            state = ear <= close_threshold
        if state != previous:
            if previous is True and self._eye_state_started is not None:
                duration = timestamp - self._eye_state_started
                if 0.08 <= duration <= 0.8:
                    self._blink_count += 1
                self._last_blink_duration = duration
                self._blinks.append((timestamp,duration))
            self._eye_state_started = timestamp if state else None
        self._eye_state = state
        return state

    def _rolling_perclos(self, timestamp: float, eyes_closed: bool | None) -> tuple[float | None, bool | None]:
        previous = self._eye_previous_timestamp
        if previous is not None and eyes_closed is not None and self._eye_previous_state is not None:
            if 0 < timestamp-previous <= 0.25:
                self._perclos.append((previous,timestamp,self._eye_previous_state))
        self._eye_previous_timestamp, self._eye_previous_state = timestamp, eyes_closed
        window = float(self.config["perclos_window_seconds"])
        while self._perclos and self._perclos[0][1] < timestamp - window:
            self._perclos.popleft()
        if not self._perclos:
            self._eye_observed_seconds = self._eye_coverage = 0.0
            return None, None
        cutoff = timestamp-window
        observed = sum(max(0,end-max(start,cutoff)) for start,end,_ in self._perclos)
        span = min(window, timestamp-self._perclos[0][0])
        self._eye_observed_seconds = observed
        self._eye_coverage = observed/max(span,1e-6)
        if eyes_closed is None or observed < float(self.config["perclos_min_observed_seconds"]) or self._eye_coverage < float(self.config.get("perclos_min_coverage",.65)):
            return None, None
        ratio = sum(max(0,end-max(start,cutoff)) for start,end,closed in self._perclos if closed)/observed
        return ratio, ratio >= float(self.config["perclos_caution_ratio"])

    def update(self, raw: RawFeatures) -> DerivedFeatures:
        self._collect_calibration(raw)
        self._finalize_calibration_if_ready(raw.timestamp)
        # EMA can erase short blinks: measure durations from the unsmoothed aperture.
        ear = raw.ear if raw.eyes_readable else None
        mar = self._smooth("mar", raw.mar)
        torso = self._smooth("torso", raw.torso_angle)
        head_drop = self._smooth("head_drop", raw.head_drop_ratio)
        ready = self.profile.status == "ready"
        def angular_delta(value: float | None, neutral: float | None) -> float | None:
            if value is None or neutral is None:
                return None
            return (value - neutral + 180.0) % 360.0 - 180.0

        pitch_delta = angular_delta(raw.head_pitch, self.profile.neutral_pitch) if ready else None
        yaw_delta = angular_delta(raw.head_yaw, self.profile.neutral_yaw) if ready else None
        relative_pitch = self._smooth_head("relative_pitch", pitch_delta)
        relative_yaw = self._smooth_head("relative_yaw", yaw_delta)
        relative_gaze_x = (
            self._smooth("relative_gaze_x", raw.gaze_x - self.profile.neutral_gaze_x)
            if ready and raw.gaze_x is not None and self.profile.neutral_gaze_x is not None
            else None
        )
        relative_gaze_y = (
            self._smooth("relative_gaze_y", raw.gaze_y - self.profile.neutral_gaze_y)
            if ready and raw.gaze_y is not None and self.profile.neutral_gaze_y is not None
            else None
        )
        relative_torso = torso - self.profile.neutral_torso if ready and torso is not None and self.profile.neutral_torso is not None else None
        relative_head_drop = (
            head_drop - self.profile.neutral_head_drop
            if ready and head_drop is not None and self.profile.neutral_head_drop is not None
            else None
        )
        pose_allows_eyes = (
            relative_yaw is not None
            and relative_pitch is not None
            and abs(relative_yaw) <= float(self.config["eye_pose_max_yaw_degrees"])
            and abs(relative_pitch) <= float(self.config["eye_pose_max_pitch_degrees"])
        )
        eyes_readable = bool(raw.eyes_readable and ready and self.profile.neutral_ear is not None and ear is not None and pose_allows_eyes)
        eyes_closed = self._eye_closed(ear if eyes_readable else None, raw.timestamp)
        perclos, perclos_high = self._rolling_perclos(raw.timestamp, eyes_closed)
        while self._blinks and self._blinks[0][0] < raw.timestamp-float(self.config["perclos_window_seconds"]):
            self._blinks.popleft()
        long_blink_count = sum(float(self.config.get("long_blink_seconds",.5)) <= d < 2 and t >= raw.timestamp-30 for t,d in self._blinks)
        if eyes_closed is False:
            if self._open_started is None:
                self._open_started = raw.timestamp
        else:
            self._open_started = None
        no_blink_seconds = raw.timestamp-self._open_started if self._open_started is not None else None
        blink_rate = (60*sum(.08<=d<=.8 for _,d in self._blinks)/self._eye_observed_seconds
                      if eyes_readable and self._eye_observed_seconds >= float(self.config.get("blink_rate_min_seconds",20)) else None)
        mouth_threshold = None
        if ready and self.profile.neutral_mar is not None:
            mouth_threshold = max(float(self.config["mouth_open_min"]), self.profile.neutral_mar + float(self.config["mouth_open_delta"]))
        mouth_open = mar >= mouth_threshold if mar is not None and mouth_threshold is not None else None
        head_abnormal = (
            abs(relative_yaw) >= float(self.config["head_yaw_degrees"])
            or abs(relative_pitch) >= float(self.config["head_pitch_degrees"])
            if relative_yaw is not None and relative_pitch is not None
            else None
        )
        gaze_away = (
            abs(relative_gaze_x) >= float(self.config.get("gaze_horizontal_ratio", 0.18))
            or abs(relative_gaze_y) >= float(self.config.get("gaze_vertical_ratio", 0.20))
            if eyes_readable and eyes_closed is False and relative_gaze_x is not None and relative_gaze_y is not None
            else None
        )
        phone_visible = raw.phone_confidence is not None
        pose_head_delta = raw.pose_head_x-self.profile.neutral_pose_head_x if ready and raw.pose_head_x is not None and self.profile.neutral_pose_head_x is not None else None
        pose_away = (abs(pose_head_delta) >= float(self.config.get("pose_head_turn_ratio",.22)) or relative_head_drop >= float(self.config.get("nod_drop_ratio",.12))) if pose_head_delta is not None and relative_head_drop is not None else None
        attention_votes = [value for value in (head_abnormal,gaze_away,pose_away) if value is not None]
        attention_away = any(attention_votes) if attention_votes else None
        attention_source = "HEAD+IRIS PROXY" if gaze_away is not None and head_abnormal is not None else "HEAD DIRECTION" if head_abnormal is not None else "POSE PROXY" if pose_away is not None else "UNAVAILABLE"
        torso_lean = abs(relative_torso) >= float(self.config["torso_lean_degrees"]) if relative_torso is not None else None
        collapse_votes = []
        if relative_torso is not None:
            collapse_votes.append(abs(relative_torso) >= float(self.config["collapse_lean_degrees"]))
        if relative_head_drop is not None:
            collapse_votes.append(relative_head_drop >= float(self.config["collapse_head_drop_delta"]))
        collapse_posture = any(collapse_votes) if collapse_votes else None
        nod_active = relative_head_drop is not None and relative_head_drop >= float(self.config.get("nod_drop_ratio",.12))
        if relative_head_drop is None:
            # Losing the head/shoulders is not an observed recovery from a nod.
            self._nod_started = None
        elif nod_active and self._nod_started is None:
            self._nod_started = raw.timestamp
        elif not nod_active and self._nod_started is not None:
            duration = raw.timestamp-self._nod_started
            if .25 <= duration <= 3:
                self._nods.append(raw.timestamp)
            self._nod_started = None
        while self._nods and self._nods[0] < raw.timestamp-30:
            self._nods.popleft()
        distress_gesture = any(raw.hand_on_chest) if raw.hand_landmarks and raw.pose_readable else None
        if raw.left_wrist_on_wheel is None or raw.right_wrist_on_wheel is None:
            hands_on_wheel = None
        else:
            hands_on_wheel = raw.left_wrist_on_wheel and raw.right_wrist_on_wheel
        reasons = list(raw.quality_reasons)
        if self.profile.status != "ready":
            reasons.append(f"calibration_{self.profile.status}")
        return DerivedFeatures(
            timestamp=raw.timestamp,
            calibration_status=self.profile.status,
            face_readable=raw.face_readable,
            eyes_readable=eyes_readable,
            pose_readable=raw.pose_readable,
            eyes_closed=eyes_closed,
            mouth_open=mouth_open,
            attention_away=attention_away,
            head_abnormal=head_abnormal,
            gaze_away=gaze_away,
            torso_lean=torso_lean,
            collapse_posture=collapse_posture,
            distress_gesture=distress_gesture,
            phone_visible=phone_visible,
            hands_on_wheel=hands_on_wheel,
            ear=ear,
            mar=mar,
            relative_pitch=relative_pitch,
            relative_yaw=relative_yaw,
            relative_gaze_x=relative_gaze_x,
            relative_gaze_y=relative_gaze_y,
            relative_torso=relative_torso,
            relative_head_drop=relative_head_drop,
            perclos=perclos,
            perclos_high=perclos_high,
            blink_count=self._blink_count,
            quality_reasons=sorted(set(reasons)),
            eye_closed_seconds=raw.timestamp-self._eye_state_started if eyes_closed is True and self._eye_state_started is not None else 0.0,
            eye_observed_seconds=self._eye_observed_seconds,
            eye_coverage=self._eye_coverage,
            blink_rate_per_minute=blink_rate,
            last_blink_duration=self._last_blink_duration,
            no_blink_seconds=no_blink_seconds,
            long_blink_count=long_blink_count,
            long_blinks_repeated=long_blink_count >= 3 if eyes_readable else None,
            nod_count=len(self._nods),
            nods_repeated=len(self._nods) >= 2 if raw.pose_readable else None,
            attention_source=attention_source,
            measurement_coverage="full" if eyes_readable and raw.pose_readable else "limited" if ready and (raw.face_readable or raw.pose_readable) else "unavailable",
        )


def median_or_none(values: Iterable[float | None]) -> float | None:
    valid = [value for value in values if value is not None and math.isfinite(value)]
    return median(valid) if valid else None
