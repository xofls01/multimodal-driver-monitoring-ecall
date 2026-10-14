from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np


BASE_SCENARIO_RE = re.compile(r"^(?P<family>[NADE])(?P<number>[1-5])$", re.IGNORECASE)
CONDITIONED_SCENARIO_RE = re.compile(
    r"^(?P<condition>[OLHS])(?P<family>[NADE])(?P<number>[1-5])$", re.IGNORECASE
)


@dataclass(frozen=True)
class VideoMetadata:
    opened: bool
    fps: float
    frame_count: int
    width: int
    height: int
    duration_seconds: float


def video_name(path: Path) -> str:
    """Recognize a Drive copy suffix without renaming the original file."""
    name = unicodedata.normalize('NFC', path.name)
    return re.sub(r'\s*의\s*사본$', '', name)


def clip_id(path: Path) -> str:
    stem = Path(video_name(path)).stem
    candidate = stem.upper()
    return candidate if BASE_SCENARIO_RE.fullmatch(candidate) or CONDITIONED_SCENARIO_RE.fullmatch(candidate) else stem


def scenario_id(path: Path) -> str:
    """Return the canonical behaviour scenario, excluding capture conditions.

    Examples: ``A1.mp4 -> A1`` and ``OA1.mp4 -> A1``.  The complete source
    identifier remains available through :func:`clip_id`.
    """
    candidate = Path(video_name(path)).stem.upper()
    conditioned = CONDITIONED_SCENARIO_RE.fullmatch(candidate)
    if conditioned:
        return f"{conditioned.group('family')}{conditioned.group('number')}"
    return candidate if BASE_SCENARIO_RE.fullmatch(candidate) else Path(video_name(path)).stem


def scenario_family(path: Path) -> str:
    candidate = Path(video_name(path)).stem.upper()
    match = CONDITIONED_SCENARIO_RE.fullmatch(candidate) or BASE_SCENARIO_RE.fullmatch(candidate)
    return match.group("family") if match else "UNKNOWN"


def capture_condition(path: Path) -> str:
    match = CONDITIONED_SCENARIO_RE.fullmatch(Path(video_name(path)).stem.upper())
    return match.group("condition") if match else "N"


def natural_key(path: Path) -> list[object]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", str(path))]


def discover_videos(input_root: Path, extensions: Iterable[str]) -> list[Path]:
    normalized = {ext.casefold() if ext.startswith(".") else f".{ext.casefold()}" for ext in extensions}
    return sorted(
        (path for path in input_root.rglob("*") if path.is_file() and Path(video_name(path)).suffix.casefold() in normalized),
        key=natural_key,
    )


def read_metadata(path: Path) -> VideoMetadata:
    capture = cv2.VideoCapture(str(path))
    opened = capture.isOpened()
    fps = float(capture.get(cv2.CAP_PROP_FPS)) if opened else 0.0
    raw_count = float(capture.get(cv2.CAP_PROP_FRAME_COUNT)) if opened else 0.0
    width = int(round(capture.get(cv2.CAP_PROP_FRAME_WIDTH))) if opened else 0
    height = int(round(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))) if opened else 0
    capture.release()
    frame_count = max(0, int(round(raw_count)))
    duration = frame_count / fps if fps > 0 and frame_count > 0 else 0.0
    return VideoMetadata(opened, fps, frame_count, width, height, duration)


def timestamp_text(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    minutes, remainder = divmod(total_ms, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{minutes:02d}:{secs:02d}.{millis:03d}"


def sample_timestamps(duration_seconds: float, interval_seconds: float) -> list[float]:
    if interval_seconds <= 0:
        raise ValueError("sample interval must be greater than zero")
    if duration_seconds <= 0:
        return [0.0]
    # Random access at the final reported PTS is unreliable for many MP4 files.
    # Stay at least one small safety margin away from EOF.
    last = max(0.0, duration_seconds - min(0.5, interval_seconds * 0.25))
    values: list[float] = []
    current = 0.0
    while current <= last + 1e-9:
        values.append(round(current, 6))
        current += interval_seconds
    if last - values[-1] > interval_seconds * 0.35:
        values.append(round(last, 6))
    return values


def read_frame_at(path: Path, seconds: float) -> tuple[bool, np.ndarray | None]:
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        capture.release()
        return False, None
    capture.set(cv2.CAP_PROP_POS_MSEC, max(0.0, seconds) * 1000.0)
    ok, frame = capture.read()
    capture.release()
    return bool(ok and frame is not None and frame.size), frame


def letterbox(frame: np.ndarray, width: int, height: int) -> np.ndarray:
    canvas = np.full((height, width, 3), 28, dtype=np.uint8)
    if frame.size == 0:
        return canvas
    scale = min(width / frame.shape[1], height / frame.shape[0])
    resized_w = max(1, int(round(frame.shape[1] * scale)))
    resized_h = max(1, int(round(frame.shape[0] * scale)))
    resized = cv2.resize(frame, (resized_w, resized_h), interpolation=cv2.INTER_AREA)
    x = (width - resized_w) // 2
    y = (height - resized_h) // 2
    canvas[y : y + resized_h, x : x + resized_w] = resized
    return canvas


def write_jpeg(path: Path, image: np.ndarray, quality: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if not ok:
        raise RuntimeError(f"failed to encode JPEG: {path}")
    path.write_bytes(encoded.tobytes())

