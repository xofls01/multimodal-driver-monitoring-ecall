#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import random
import shutil
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from driver_state_tools.video_common import (  # noqa: E402
    capture_condition,
    clip_id,
    discover_videos,
    read_frame_at,
    read_metadata,
    sample_timestamps,
    scenario_family,
    scenario_id,
    write_jpeg,
)


EXTENSIONS = [".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract unlabeled frames and create leakage-safe source-video split manifests."
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "outputs" / "annotation_prep")
    parser.add_argument("--interval", type=float, default=1.0, help="Frame interval in seconds")
    parser.add_argument("--seed", type=int, default=20261009)
    parser.add_argument("--jpeg-quality", type=int, default=95)
    parser.add_argument(
        "--stage-input",
        action="store_true",
        help="Copy each cloud-backed video to a temporary local file before reading it",
    )
    return parser.parse_args()


def assign_splits(videos: list[Path], seed: int) -> dict[Path, str]:
    by_stratum: dict[str, list[Path]] = defaultdict(list)
    for video in videos:
        by_stratum[f"{capture_condition(video)}:{scenario_family(video)}"].append(video)
    assignments: dict[Path, str] = {}
    rng = random.Random(seed)
    for stratum in sorted(by_stratum):
        group = sorted(by_stratum[stratum], key=lambda path: scenario_id(path))
        rng.shuffle(group)
        count = len(group)
        if count >= 5:
            val_count, test_count = 1, 1
        elif count >= 3:
            val_count, test_count = 1, 1
        elif count == 2:
            val_count, test_count = 1, 0
        else:
            val_count, test_count = 0, 0
        train_count = count - val_count - test_count
        for index, path in enumerate(group):
            assignments[path] = "train" if index < train_count else ("val" if index < train_count + val_count else "test")
    return assignments


def main() -> int:
    args = parse_args()
    if args.interval <= 0:
        raise SystemExit("--interval must be greater than zero")
    input_root = args.input.expanduser().resolve()
    output_root = args.output.expanduser().resolve()
    if not input_root.is_dir():
        raise SystemExit(f"input directory does not exist: {input_root}")
    if output_root == input_root or input_root in output_root.parents:
        raise SystemExit("output must be outside input")
    videos = discover_videos(input_root, EXTENSIONS)
    if any(n>1 for n in Counter(clip_id(v) for v in videos).values()):
        raise SystemExit("duplicate clip IDs: run into separate outputs rather than overwrite source provenance")
    assignments = assign_splits(videos, args.seed)
    output_root.mkdir(parents=True, exist_ok=True)

    split_rows: list[dict[str, object]] = []
    frame_rows: list[dict[str, object]] = []
    for video in videos:
        rel = video.relative_to(input_root).as_posix()
        cid = clip_id(video)
        sid = scenario_id(video)
        family = scenario_family(video)
        condition = capture_condition(video)
        split = assignments[video]
        staging_context = tempfile.TemporaryDirectory(prefix=f"driver-annotation-{cid}-") if args.stage_input else None
        if staging_context is not None:
            staged_path = Path(staging_context.name) / video.name
            shutil.copy2(video, staged_path)
            read_path = staged_path
        else:
            read_path = video
        metadata = read_metadata(read_path)
        source_key = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:8]
        split_rows.append(
            {
                "source_video": rel,
                "source_group_id": source_key,
                "clip_id": cid,
                "scenario_id": sid,
                "capture_condition": condition,
                "scenario_family": family,
                "split": split,
                "note": "scenario_id/family are provenance only, not training labels",
            }
        )
        for seconds in sample_timestamps(metadata.duration_seconds, args.interval):
            ok, frame = read_frame_at(read_path, seconds)
            if not ok or frame is None:
                print(f"WARN: could not read {rel} at {seconds:.3f}s", file=sys.stderr)
                continue
            timestamp_ms = int(round(seconds * 1000))
            frame_name = f"{cid}__{source_key}__{timestamp_ms:06d}ms.jpg"
            frame_rel = Path("images") / "unlabeled" / split / frame_name
            write_jpeg(output_root / frame_rel, frame, args.jpeg_quality)
            frame_rows.append(
                {
                    "image_path": frame_rel.as_posix(),
                    "source_video": rel,
                    "source_group_id": source_key,
                    "timestamp_seconds": f"{seconds:.3f}",
                    "split": split,
                    "review_status": "unlabeled",
                    "labels": "",
                }
            )
        if staging_context is not None:
            staging_context.cleanup()
        print(f"{cid}: {split} ({rel})")

    with (output_root / "video_split_manifest.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=split_rows[0].keys() if split_rows else ["source_video"])
        writer.writeheader()
        writer.writerows(split_rows)
    with (output_root / "frames_manifest.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=frame_rows[0].keys() if frame_rows else ["image_path", "source_video", "source_group_id", "timestamp_seconds", "split", "review_status", "labels"],
        )
        writer.writeheader()
        writer.writerows(frame_rows)
    (output_root / "README.txt").write_text(
        "These images are UNLABELED. Do not train until human review is complete.\n"
        "No empty YOLO label files were created because they would be interpreted as negative annotations.\n"
        "Splits are grouped by source video. Without verified participant IDs, they are development splits, not an independent performance evaluation.\n",
        encoding="utf-8",
    )
    print(f"Prepared {len(frame_rows)} unlabeled frames from {len(videos)} source videos at {output_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

