#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import tempfile
import time
from collections import Counter
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

import cv2

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
RUNTIME_CACHE = PROJECT_ROOT / "outputs" / ".runtime_cache"
RUNTIME_CACHE.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MPLCONFIGDIR", str(RUNTIME_CACHE / "matplotlib"))
os.environ.setdefault("YOLO_CONFIG_DIR", str(RUNTIME_CACHE / "ultralytics"))

from driver_state_tools.decision_engine import FeatureFrame, TemporalDecisionEngine  # noqa: E402
from driver_state_tools.rppg import RPPGResult, RPPGTracker  # noqa: E402
from driver_state_tools.video_common import (  # noqa: E402
    capture_condition,
    clip_id,
    discover_videos,
    scenario_family,
    scenario_id,
    write_jpeg,
)
from driver_state_tools.vision_features import FeatureTracker, RawFeatures  # noqa: E402
from driver_state_tools.vision_pipeline import VisionModels, draw_overlay  # noqa: E402


EXTENSIONS = [".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "vision_baseline.json"
DEFAULT_RULES = PROJECT_ROOT / "config" / "decision_rules.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a confidence-gated YOLO + face-landmark driver-state baseline.")
    parser.add_argument("--input", required=True, type=Path, help="A video file or a directory searched recursively")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "outputs" / "vision_baseline")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    parser.add_argument("--analysis-fps", type=float, help="Override feature extraction rate")
    parser.add_argument("--device", help="Ultralytics device, for example cpu, mps, or 0")
    parser.add_argument("--max-videos", type=int, help="Only run the first N discovered videos")
    parser.add_argument("--stage-input", action="store_true", help="Copy each cloud-backed input to a local temp file")
    parser.add_argument("--no-video", action="store_true", help="Do not save annotated MP4 files")
    parser.add_argument("--eyes-occluded", action="store_true", help="Explicitly disable eye measurements for occluded-eye recordings")
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def raw_record(raw: RawFeatures) -> dict[str, Any]:
    return {
        "face_readable": raw.face_readable,
        "eyes_readable": raw.eyes_readable,
        "pose_readable": raw.pose_readable,
        "ear_left": raw.ear_left,
        "ear_right": raw.ear_right,
        "mar": raw.mar,
        "head_pitch": raw.head_pitch,
        "head_yaw": raw.head_yaw,
        "head_roll": raw.head_roll,
        "gaze_x": raw.gaze_x,
        "gaze_y": raw.gaze_y,
        "torso_angle": raw.torso_angle,
        "head_drop_ratio": raw.head_drop_ratio,
        "pose_head_x": raw.pose_head_x,
        "phone_confidence": raw.phone_confidence,
        "phone_track_mode": raw.phone_track_mode,
        "hands_detected_count": len(raw.hand_landmarks),
        "hands_on_wheel_count": sum(raw.hand_on_wheel),
        "hands_on_phone_count": sum(raw.hand_phone_overlap),
        "hands_on_chest_count": sum(raw.hand_on_chest),
        "left_wrist_on_wheel": raw.left_wrist_on_wheel,
        "right_wrist_on_wheel": raw.right_wrist_on_wheel,
        "rppg_forehead_rgb": raw.rppg_forehead_rgb,
        "rppg_cheeks_rgb": raw.rppg_cheeks_rgb,
        "rppg_forehead_skin_ratio": raw.rppg_forehead_skin_ratio,
        "rppg_cheeks_skin_ratio": raw.rppg_cheeks_skin_ratio,
        "quality_reasons": raw.quality_reasons,
    }


def open_writer(path: Path, width: int, height: int, fps: float) -> cv2.VideoWriter:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    if not writer.isOpened():
        writer.release()
        raise RuntimeError(f"could not create annotated video: {path}")
    return writer


def resize_output(frame: Any, width: int) -> Any:
    if width <= 0 or frame.shape[1] == width:
        return frame
    height = int(round(frame.shape[0] * width / frame.shape[1]))
    return cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)


def create_segments(records: list[dict[str, Any]], sample_period: float) -> list[dict[str, Any]]:
    if not records:
        return []
    segments: list[dict[str, Any]] = []
    current_state = records[0]["decision"]["state"]
    current_reasons = tuple(records[0]["decision"]["reasons"])
    start = float(records[0]["timestamp"])
    previous = start
    for record in records[1:]:
        state = record["decision"]["state"]
        reasons = tuple(record["decision"]["reasons"])
        timestamp = float(record["timestamp"])
        if state != current_state or reasons != current_reasons:
            segments.append(
                {
                    "start_seconds": f"{start:.3f}",
                    "end_seconds": f"{previous + sample_period:.3f}",
                    "duration_seconds": f"{previous + sample_period - start:.3f}",
                    "state": current_state,
                    "reasons": ";".join(current_reasons),
                }
            )
            start, current_state, current_reasons = timestamp, state, reasons
        previous = timestamp
    segments.append(
        {
            "start_seconds": f"{start:.3f}",
            "end_seconds": f"{previous + sample_period:.3f}",
            "duration_seconds": f"{previous + sample_period - start:.3f}",
            "state": current_state,
            "reasons": ";".join(current_reasons),
        }
    )
    return segments


def analyze_video(
    source: Path,
    relative_path: Path,
    output_root: Path,
    config: dict[str, Any],
    rules: dict[str, Any],
    models: VisionModels,
) -> dict[str, Any]:
    cid = clip_id(source)
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        capture.release()
        raise RuntimeError(f"could not open {source}")
    source_fps = float(capture.get(cv2.CAP_PROP_FPS))
    frame_count = int(round(capture.get(cv2.CAP_PROP_FRAME_COUNT)))
    width = int(round(capture.get(cv2.CAP_PROP_FRAME_WIDTH)))
    height = int(round(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)))
    analysis_fps = float(config["analysis_fps"])
    sample_period = 1.0 / analysis_fps
    sample_every = max(1, int(round(source_fps / analysis_fps)))
    actual_analysis_fps = source_fps / sample_every
    requested_width = int(config.get("output_width", 0))
    output_width = width if requested_width <= 0 else min(width, requested_width)
    output_height = int(round(height * output_width / width)) // 2 * 2
    dashboard_width = max(0, int(round(float(config.get("dashboard_width", 420)) * output_height / 720))) // 2 * 2
    panel_source_width = int(round(dashboard_width * width / output_width)) if output_width > 0 else dashboard_width
    writer_width = (output_width + dashboard_width) // 2 * 2
    output_fps = source_fps if config.get("output_source_fps", True) else actual_analysis_fps
    save_video = bool(config.get("save_annotated_video", True))
    video_path = output_root / "annotated_videos" / f"{cid}.mp4"
    writer = open_writer(video_path, writer_width, output_height, output_fps) if save_video else None
    family = scenario_family(source)
    included_families = {str(item).upper() for item in config.get("rppg_included_scenario_families", [])}
    collect_rppg = bool(config.get("rppg_enabled", True)) and (
        not included_families or family.upper() in included_families
    )
    rppg_exclusion_reason = "presentation_protocol_non_normal" if not collect_rppg else ""
    tracker = FeatureTracker(config)
    rppg_tracker = RPPGTracker(config, sample_rate_hint=source_fps)
    engine = TemporalDecisionEngine(rules)
    models.start_clip(eyes_occluded=capture_condition(source) == "S")
    raw = derived = decision = rppg = None
    records: list[dict[str, Any]] = []
    state_counts: Counter[str] = Counter()
    face_ok = eyes_ok = pose_ok = 0
    rppg_ready = 0
    rppg_sources: Counter[str] = Counter()
    rppg_heart_rates: list[float] = []
    phone_modes: Counter[str] = Counter()
    hand_frames = hands_on_wheel_frames = hands_on_phone_frames = 0
    preview_next = 0.0
    preview_interval = float(config.get("save_preview_frames_every_seconds", 0.0))
    start_time = time.monotonic()
    frame_index = -1
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            frame_index += 1
            timestamp = frame_index / source_fps
            if frame_index % sample_every:
                if collect_rppg:
                    rppg_tracker.update(models.extract_rppg_only(frame, timestamp))
                if writer is not None and config.get("output_source_fps", True) and raw is not None:
                    annotated = draw_overlay(frame, raw, derived, decision, rppg, panel_source_width, config)
                    writer.write(cv2.resize(annotated, (writer_width, output_height), interpolation=cv2.INTER_AREA))
                continue
            raw = models.extract(frame, timestamp, collect_rppg=collect_rppg)
            derived = tracker.update(raw)
            rppg = (
                rppg_tracker.update(raw)
                if collect_rppg
                else RPPGResult(
                    timestamp=timestamp,
                    status="disabled",
                    source="protocol_excluded",
                    note="rPPG intentionally collected only for normal-scenario presentation clips",
                )
            )
            feature_frame = FeatureFrame(
                timestamp=timestamp,
                face_readable=derived.face_readable,
                eyes_readable=derived.eyes_readable,
                pose_readable=derived.pose_readable,
                eyes_closed=derived.eyes_closed,
                attention_away=derived.attention_away,
                head_abnormal=derived.head_abnormal,
                yawning=derived.mouth_open,
                perclos_high=derived.perclos_high,
                torso_lean=derived.torso_lean,
                collapse_posture=derived.collapse_posture,
                distress_gesture=derived.distress_gesture,
                long_blinks_repeated=derived.long_blinks_repeated,
                nods_repeated=derived.nods_repeated,
                warning_active=False,
                response_received=None,
            )
            decision = engine.update(feature_frame)
            annotated = draw_overlay(frame, raw, derived, decision, rppg, panel_source_width, config)
            annotated = cv2.resize(annotated, (writer_width, output_height), interpolation=cv2.INTER_AREA)
            if writer is not None:
                writer.write(annotated)
            if preview_interval > 0 and timestamp + 1e-6 >= preview_next:
                write_jpeg(output_root / "preview_frames" / cid / f"{int(round(timestamp * 1000)):06d}ms.jpg", annotated, 92)
                preview_next += preview_interval
            decision_dict = {
                "state": decision.state.value,
                "reasons": decision.reasons,
                "durations": decision.durations,
            }
            records.append(
                {
                    "schema_version": "1.0",
                    "clip_id": cid,
                    "scenario_id": scenario_id(source),
                    "capture_condition": capture_condition(source),
                    "timestamp": round(timestamp, 6),
                    "raw": raw_record(raw),
                    "derived": derived.to_dict(),
                    "rppg": rppg.to_dict(),
                    "decision": decision_dict,
                }
            )
            state_counts[decision.state.value] += 1
            face_ok += int(derived.face_readable)
            eyes_ok += int(derived.eyes_readable)
            pose_ok += int(derived.pose_readable)
            if rppg.status == "ready" and rppg.heart_rate_bpm is not None:
                rppg_ready += 1
                rppg_sources[rppg.source or "unknown"] += 1
                rppg_heart_rates.append(float(rppg.heart_rate_bpm))
            if raw.phone_track_mode:
                phone_modes[raw.phone_track_mode] += 1
            if raw.hand_landmarks:
                hand_frames += 1
                hands_on_wheel_frames += int(any(raw.hand_on_wheel))
                hands_on_phone_frames += int(any(raw.hand_phone_overlap))
    finally:
        capture.release()
        if writer is not None:
            writer.release()

    record_path = output_root / "frame_records" / f"{cid}.jsonl"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    with record_path.open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    segments = create_segments(records, 1.0 / actual_analysis_fps)
    segment_path = output_root / "event_segments" / f"{cid}.csv"
    segment_path.parent.mkdir(parents=True, exist_ok=True)
    with segment_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer_csv = csv.DictWriter(
            stream, fieldnames=["start_seconds", "end_seconds", "duration_seconds", "state", "reasons"]
        )
        writer_csv.writeheader()
        writer_csv.writerows(segments)
    count = len(records)
    elapsed = time.monotonic() - start_time
    return {
        "relative_path": relative_path.as_posix(),
        "clip_id": cid,
        "scenario_id": scenario_id(source),
        "capture_condition": capture_condition(source),
        "scenario_family": scenario_family(source),
        "duration_seconds": round(frame_count / source_fps, 3) if source_fps > 0 else None,
        "source_fps": round(source_fps, 6),
        "source_width": width,
        "source_height": height,
        "output_fps": round(output_fps, 6),
        "decoded_frames": frame_index + 1,
        "analysis_fps": round(actual_analysis_fps, 6),
        "output_width": writer_width,
        "output_height": output_height,
        "analyzed_frames": count,
        "face_readable_ratio": round(face_ok / count, 4) if count else 0,
        "eyes_readable_ratio": round(eyes_ok / count, 4) if count else 0,
        "pose_readable_ratio": round(pose_ok / count, 4) if count else 0,
        "rppg_ready_ratio": round(rppg_ready / count, 4) if count else 0,
        "rppg_collected": collect_rppg,
        "rppg_exclusion_reason": rppg_exclusion_reason,
        "rppg_forehead_frames": rppg_sources["forehead"],
        "rppg_cheeks_frames": rppg_sources["cheeks"],
        "rppg_fusion_frames": rppg_sources["fusion"],
        "rppg_median_bpm": round(float(median(rppg_heart_rates)), 2) if rppg_heart_rates else None,
        "phone_detected_frames": phone_modes["detected"],
        "phone_tracked_frames": phone_modes["tracked"],
        "phone_cached_frames": phone_modes["cached"],
        "phone_detection_fps": config.get('phone_detection_fps',actual_analysis_fps),
        "hand_landmark_frames": hand_frames,
        "hand_on_wheel_frames": hands_on_wheel_frames,
        "hand_on_phone_frames": hands_on_phone_frames,
        "normal_frames": state_counts["normal"],
        "caution_frames": state_counts["caution"],
        "danger_frames": state_counts["danger"],
        "unavailable_frames": state_counts["unavailable"],
        "calibration_status": tracker.profile.status,
        "calibration_samples": tracker.profile.sample_count,
        "neutral_ear": tracker.profile.neutral_ear,
        "neutral_mar": tracker.profile.neutral_mar,
        "processing_seconds": round(elapsed, 3),
        "annotated_video": str(video_path.relative_to(output_root)) if writer is not None else "",
        "frame_records": str(record_path.relative_to(output_root)),
        "event_segments": str(segment_path.relative_to(output_root)),
        "note": "acted feasibility clip; states are prototype outputs, not medical diagnoses",
    }


def main() -> int:
    args = parse_args()
    config = load_json(args.config)
    rules = load_json(args.rules)
    if args.analysis_fps is not None:
        config["analysis_fps"] = args.analysis_fps
    if args.device:
        config["device"] = args.device
    if args.no_video:
        config["save_annotated_video"] = False
    if args.eyes_occluded:
        config["eye_measurement_disabled"] = True
    if float(config["analysis_fps"]) <= 0:
        raise SystemExit("analysis FPS must be greater than zero")
    input_path = args.input.expanduser().resolve()
    output_root = args.output.expanduser().resolve()
    if not input_path.exists():
        raise SystemExit(f"input does not exist: {input_path}")
    if input_path.is_dir() and (output_root == input_path or input_path in output_root.parents):
        raise SystemExit("output must be outside input so source videos remain untouched")
    videos = [input_path] if input_path.is_file() else discover_videos(input_path, EXTENSIONS)
    if args.max_videos is not None:
        videos = videos[: max(0, args.max_videos)]
    if not videos:
        raise SystemExit("no videos found")
    duplicate_ids = [key for key,count in Counter(clip_id(video) for video in videos).items() if count > 1]
    if duplicate_ids:
        raise SystemExit(f"duplicate clip IDs would overwrite results: {duplicate_ids}; run these sources into separate output folders")
    output_root.mkdir(parents=True, exist_ok=True)
    models = VisionModels(config, PROJECT_ROOT)
    summaries: list[dict[str, Any]] = []
    try:
        for index, video in enumerate(videos, start=1):
            relative = video.name if input_path.is_file() else video.relative_to(input_path)
            context = tempfile.TemporaryDirectory(prefix=f"vision-{clip_id(video)}-") if args.stage_input else nullcontext(None)
            print(f"[{index}/{len(videos)}] preparing {relative}", flush=True)
            try:
                with context as temp_dir:
                    if temp_dir:
                        staged = Path(temp_dir) / video.name
                        shutil.copy2(video, staged)
                        read_path = staged
                    else:
                        read_path = video
                    print(f"[{index}/{len(videos)}] analyzing {relative}", flush=True)
                    summary = analyze_video(read_path, Path(relative), output_root, config, rules, models)
                    # Staging changes the path stem only if a caller renamed it; provenance must use source.
                    summary["clip_id"] = clip_id(video)
                    summary["scenario_id"] = scenario_id(video)
                    summary["capture_condition"] = capture_condition(video)
                    summary["scenario_family"] = scenario_family(video)
                    summaries.append(summary)
                    print(
                        f"  -> face {summary['face_readable_ratio']:.1%}, eyes {summary['eyes_readable_ratio']:.1%}, "
                        f"pose {summary['pose_readable_ratio']:.1%}, {summary['processing_seconds']:.1f}s",
                        flush=True,
                    )
            except Exception as error:
                summaries.append(
                    {
                        "relative_path": Path(relative).as_posix(),
                        "clip_id": clip_id(video),
                        "scenario_id": scenario_id(video),
                        "capture_condition": capture_condition(video),
                        "scenario_family": scenario_family(video),
                        "error": f"{type(error).__name__}: {error}",
                    }
                )
                print(f"  -> ERROR: {error}", file=sys.stderr, flush=True)
            # Save each completed result before touching the next cloud-backed path.
            checkpoints = output_root / 'clip_summaries'
            checkpoints.mkdir(parents=True, exist_ok=True)
            (checkpoints / f'{clip_id(video)}.json').write_text(
                json.dumps(summaries[-1], ensure_ascii=False, indent=2), encoding='utf-8')
            with (output_root / 'video_summary.csv').open('w', encoding='utf-8-sig', newline='') as stream:
                checkpoint_writer = csv.DictWriter(stream, fieldnames=sorted({key for row in summaries for key in row}))
                checkpoint_writer.writeheader()
                checkpoint_writer.writerows(summaries)
    finally:
        models.close()

    summary_csv = output_root / "video_summary.csv"
    fieldnames = sorted({key for row in summaries for key in row})
    with summary_csv.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(summaries)
    run_summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "input": str(input_path),
        "output": str(output_root),
        "video_count": len(videos),
        "success_count": sum("error" not in row for row in summaries),
        "config": config,
        "decision_rules": rules,
        "limitations": [
            "The clips contain acted behavior and do not establish drowsiness, loss of consciousness, or cardiac events.",
            "Pretrained COCO YOLO is used only for pose and phone detection; eye closure and mouth opening are landmark-derived.",
            "No-response is not inferred from video, so the video-only run does not enter danger on that basis.",
            "Hands-on-wheel remains unknown until wheel_roi is calibrated for the camera.",
        ],
    }
    (output_root / "run_summary.json").write_text(json.dumps(run_summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Finished: {run_summary['success_count']}/{len(videos)} videos -> {output_root}")
    return 0 if run_summary["success_count"] == len(videos) else 1


if __name__ == "__main__":
    raise SystemExit(main())
