#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
import math
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from driver_state_tools.video_common import (  # noqa: E402
    capture_condition,
    clip_id,
    discover_videos,
    letterbox,
    read_frame_at,
    read_metadata,
    sample_timestamps,
    scenario_family,
    scenario_id,
    timestamp_text,
    write_jpeg,
)


DEFAULT_CONFIG = PROJECT_ROOT / "config" / "video_inspection.json"
CSV_FIELDS = [
    "relative_path",
    "clip_id",
    "scenario_id",
    "capture_condition",
    "scenario_family",
    "file_size_bytes",
    "duration_seconds",
    "fps",
    "width",
    "height",
    "frame_count_reported",
    "decoded_frames",
    "open_success",
    "first_frame_read_success",
    "sample_frames_requested",
    "sample_frames_read",
    "full_decode_success",
    "read_success",
    "contact_sheet",
    "error",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recursively inspect videos and create timestamped contact sheets without modifying sources."
    )
    parser.add_argument("--input", required=True, type=Path, help="Input directory containing source videos")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "outputs" / "video_inspection")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--interval", type=float, help="Override representative-frame interval in seconds")
    parser.add_argument("--no-full-decode", action="store_true", help="Skip sequential decode validation")
    parser.add_argument(
        "--stage-input",
        action="store_true",
        help="Copy each cloud-backed video to a temporary local file before reading it",
    )
    return parser.parse_args()


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def validate_full_decode(path: Path, expected_frames: int) -> tuple[int, bool, str]:
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        capture.release()
        return 0, False, "could not open for full decode"
    decoded = 0
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        if frame is None or not frame.size:
            capture.release()
            return decoded, False, f"empty frame after {decoded} frames"
        decoded += 1
    capture.release()
    tolerance = max(2, int(round(expected_frames * 0.01))) if expected_frames else 0
    success = decoded > 0 and (expected_frames <= 0 or decoded + tolerance >= expected_frames)
    error = "" if success else f"decoded {decoded} of {expected_frames} reported frames"
    return decoded, success, error


def make_contact_sheet(
    frames: list[tuple[float, np.ndarray]], columns: int, tile_width: int, title: str
) -> np.ndarray:
    if not frames:
        raise ValueError("at least one frame is required")
    aspect = frames[0][1].shape[0] / frames[0][1].shape[1]
    image_height = max(1, int(round(tile_width * aspect)))
    label_height = 32
    title_height = 48
    rows = math.ceil(len(frames) / columns)
    sheet = np.full((title_height + rows * (image_height + label_height), columns * tile_width, 3), 245, np.uint8)
    cv2.putText(sheet, title, (14, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (25, 25, 25), 2, cv2.LINE_AA)
    for index, (seconds, frame) in enumerate(frames):
        row, column = divmod(index, columns)
        x = column * tile_width
        y = title_height + row * (image_height + label_height)
        sheet[y : y + image_height, x : x + tile_width] = letterbox(frame, tile_width, image_height)
        cv2.rectangle(sheet, (x, y + image_height), (x + tile_width, y + image_height + label_height), (20, 20, 20), -1)
        cv2.putText(
            sheet,
            timestamp_text(seconds),
            (x + 10, y + image_height + 23),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )
    return sheet


def make_overview(items: list[tuple[str, np.ndarray]], columns: int = 5, tile_width: int = 240) -> np.ndarray:
    image_height = 135
    label_height = 34
    title_height = 54
    rows = max(1, math.ceil(len(items) / columns))
    canvas = np.full((title_height + rows * (image_height + label_height), columns * tile_width, 3), 242, np.uint8)
    cv2.putText(canvas, "Dataset overview - one middle frame per video", (16, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.85, (20, 20, 20), 2, cv2.LINE_AA)
    for index, (label, frame) in enumerate(items):
        row, column = divmod(index, columns)
        x = column * tile_width
        y = title_height + row * (image_height + label_height)
        canvas[y : y + image_height, x : x + tile_width] = letterbox(frame, tile_width, image_height)
        cv2.rectangle(canvas, (x, y + image_height), (x + tile_width, y + image_height + label_height), (20, 20, 20), -1)
        cv2.putText(canvas, label, (x + 10, y + image_height + 24), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2, cv2.LINE_AA)
    return canvas


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    interval = float(args.interval if args.interval is not None else config["sample_interval_seconds"])
    if interval <= 0:
        raise SystemExit("--interval must be greater than zero")
    input_root = args.input.expanduser().resolve()
    output_root = args.output.expanduser().resolve()
    if not input_root.is_dir():
        raise SystemExit(f"input directory does not exist: {input_root}")
    if output_root == input_root or input_root in output_root.parents:
        raise SystemExit("output must be outside the input directory so source data remains untouched")

    videos = discover_videos(input_root, config["extensions"])
    if any(n>1 for n in Counter(clip_id(v) for v in videos).values()):
        raise SystemExit("duplicate clip IDs: use separate output folders to avoid overwriting contact sheets")
    output_root.mkdir(parents=True, exist_ok=True)
    contact_dir = output_root / "contact_sheets"
    contact_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    overview_items: list[tuple[str, np.ndarray]] = []
    full_decode = bool(config.get("full_decode", True)) and not args.no_full_decode

    for index, path in enumerate(videos, start=1):
        rel = path.relative_to(input_root)
        cid = clip_id(path)
        sid = scenario_id(path)
        staging_context = tempfile.TemporaryDirectory(prefix=f"driver-video-{cid}-") if args.stage_input else None
        if staging_context is not None:
            staged_path = Path(staging_context.name) / path.name
            shutil.copy2(path, staged_path)
            read_path = staged_path
        else:
            read_path = path
        metadata = read_metadata(read_path)
        errors: list[str] = []
        first_ok, _ = read_frame_at(read_path, 0.0) if metadata.opened else (False, None)
        timestamps = sample_timestamps(metadata.duration_seconds, interval)
        sampled: list[tuple[float, np.ndarray]] = []
        for seconds in timestamps:
            ok, frame = read_frame_at(read_path, seconds)
            if ok and frame is not None:
                sampled.append((seconds, frame))
            else:
                errors.append(f"sample read failed at {seconds:.3f}s")

        contact_rel = ""
        if sampled:
            contact_rel = f"contact_sheets/{cid}.jpg"
            sheet = make_contact_sheet(
                sampled,
                int(config["contact_sheet_columns"]),
                int(config["tile_width"]),
                f"{cid} | scenario {sid} | representative frames every {interval:g}s",
            )
            write_jpeg(output_root / contact_rel, sheet, int(config["jpeg_quality"]))
            middle_seconds = metadata.duration_seconds / 2 if metadata.duration_seconds > 0 else sampled[len(sampled) // 2][0]
            middle_ok, middle_frame = read_frame_at(read_path, middle_seconds)
            if middle_ok and middle_frame is not None:
                overview_items.append((f"{cid}  {timestamp_text(middle_seconds)}", middle_frame))

        decoded_frames = 0
        full_decode_success: bool | str = "not_run"
        if full_decode and metadata.opened:
            decoded_frames, full_decode_success, decode_error = validate_full_decode(read_path, metadata.frame_count)
            if decode_error:
                errors.append(decode_error)
        metadata_ok = metadata.fps > 0 and metadata.frame_count > 0 and metadata.width > 0 and metadata.height > 0
        samples_ok = len(sampled) == len(timestamps) and len(timestamps) > 0
        read_success = metadata.opened and metadata_ok and first_ok and samples_ok and full_decode_success is not False
        if not metadata.opened:
            errors.append("open failed")
        elif not metadata_ok:
            errors.append("incomplete metadata")
        rows.append(
            {
                "relative_path": rel.as_posix(),
                "clip_id": cid,
                "scenario_id": sid,
                "capture_condition": capture_condition(path),
                "scenario_family": scenario_family(path),
                "file_size_bytes": path.stat().st_size,
                "duration_seconds": f"{metadata.duration_seconds:.3f}",
                "fps": f"{metadata.fps:.6f}",
                "width": metadata.width,
                "height": metadata.height,
                "frame_count_reported": metadata.frame_count,
                "decoded_frames": decoded_frames if full_decode else "",
                "open_success": metadata.opened,
                "first_frame_read_success": first_ok,
                "sample_frames_requested": len(timestamps),
                "sample_frames_read": len(sampled),
                "full_decode_success": full_decode_success,
                "read_success": read_success,
                "contact_sheet": contact_rel,
                "error": "; ".join(errors),
            }
        )
        if staging_context is not None:
            staging_context.cleanup()
        print(f"[{index:02d}/{len(videos):02d}] {rel} -> {'OK' if read_success else 'CHECK'}")

    csv_path = output_root / "video_metadata.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    if overview_items:
        write_jpeg(output_root / "overview.jpg", make_overview(overview_items), int(config["jpeg_quality"]))
    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_root": str(input_root),
        "output_root": str(output_root),
        "video_count": len(videos),
        "read_success_count": sum(row["read_success"] is True for row in rows),
        "read_failure_count": sum(row["read_success"] is not True for row in rows),
        "sample_interval_seconds": interval,
        "full_decode": full_decode,
        "stage_input": args.stage_input,
        "capture_condition_counts": {
            condition: sum(row["capture_condition"] == condition for row in rows)
            for condition in sorted({str(row["capture_condition"]) for row in rows})
        },
        "scenario_family_counts": {
            family: sum(row["scenario_family"] == family for row in rows)
            for family in sorted({str(row["scenario_family"]) for row in rows})
        },
        "source_modified": False,
    }
    (output_root / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["read_failure_count"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())

