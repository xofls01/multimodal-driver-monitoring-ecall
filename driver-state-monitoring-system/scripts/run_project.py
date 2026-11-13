#!/usr/bin/env python3
"""Reproduce the file-based review workflow without changing input videos."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def verify_models(root: Path) -> dict[str, str]:
    manifest = json.loads((root / "models/manifest.json").read_text(encoding="utf-8"))
    hashes = {}
    for name, expected in manifest["files"].items():
        path = root / "models" / name
        if not path.is_file():
            raise ValueError(f"Missing packaged model: {path}. Restore it from the repository.")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"Model checksum mismatch: {path}. Restore the original model.")
        hashes[name] = actual
    return hashes


def validate_paths(source: Path, output: Path, root: Path = ROOT) -> None:
    source, output, root = source.resolve(), output.resolve(), root.resolve()
    if not source.is_dir():
        raise ValueError(f"--input must be a video directory: {source}")
    if output == source or source in output.parents:
        raise ValueError("Output must not be inside the input directory.")
    if output in source.parents:
        raise ValueError("Input must not be inside the output directory.")
    output_base = (root / "outputs").resolve()
    if output_base not in output.parents:
        raise ValueError("Use a new experiment folder inside this project's outputs directory.")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError(f"Output is not empty: {output}. Choose a new name; existing results are preserved.")


def check_environment() -> dict:
    if sys.version_info[:2] != (3, 11):
        raise ValueError("Use Python 3.11 for the tested MediaPipe environment.")
    for module in ("cv2", "numpy", "mediapipe", "ultralytics", "imageio_ffmpeg"):
        importlib.import_module(module)
    import mediapipe as mp
    if not hasattr(mp, "solutions"):
        raise ValueError("MediaPipe Face Mesh API missing. Install the pinned requirements.")
    names = ("mediapipe", "ultralytics", "numpy", "opencv-python", "opencv-contrib-python",
             "torch", "torchvision", "jax", "jaxlib", "imageio-ffmpeg")
    return {"python": sys.version, "platform": platform.platform(),
            "packages": {name: importlib.metadata.version(name) for name in names}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", type=Path, help="Directory searched recursively (required unless --check)")
    ap.add_argument("--output", type=Path, help="New experiment folder inside outputs/; defaults to timestamp")
    ap.add_argument("--interval", type=float, default=3.0, help="Representative-frame interval in seconds")
    ap.add_argument("--prepare-frames", action="store_true", help="Also prepare unlabeled frames and source-video splits")
    ap.add_argument("--annotation-interval", type=float, default=1.0)
    ap.add_argument("--stage-input", action="store_true", help="Read temporary local copies of cloud-backed videos")
    ap.add_argument("--eyes-occluded", action="store_true", help="Disable unreadable eye measurements explicitly")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--check", action="store_true", help="Verify packaged weights and dependencies only")
    ap.add_argument("--mode", choices=("literature", "legacy"), default="literature")
    ap.add_argument("--calibration-profile", type=Path)
    ap.add_argument("--unity", action="store_true", help="Publish current analyzed decisions during the vision stage")
    args = ap.parse_args()
    try:
        hashes = verify_models(ROOT)
        if not args.check:
            if args.input is None:
                raise ValueError("--input is required.")
            if args.interval <= 0 or args.annotation_interval <= 0:
                raise ValueError("Frame intervals must be positive.")
            source = args.input.expanduser().resolve()
            output = (args.output or ROOT / "outputs" / datetime.now().strftime("experiment_%Y%m%d_%H%M%S")).expanduser().resolve()
            validate_paths(source, output)
        cache = ROOT / "outputs/.runtime_cache"
        cache.mkdir(parents=True, exist_ok=True)
        import os
        os.environ.setdefault("MPLCONFIGDIR", str(cache / "matplotlib"))
        os.environ.setdefault("YOLO_CONFIG_DIR", str(cache / "ultralytics"))
        environment = check_environment()
        if args.check:
            print("Packaged model checksums and Python dependencies verified. No inference was run.")
            return 0
        output.mkdir(parents=True, exist_ok=True)
        environment["model_sha256"] = hashes
        environment["config_sha256"] = {
            name: hashlib.sha256((ROOT / "config" / name).read_bytes()).hexdigest()
            for name in ("vision_baseline.json", "decision_rules.json", "video_inspection.json")
        }
        (output / "environment.json").write_text(json.dumps(environment, indent=2), encoding="utf-8")
        staged = ["--stage-input"] if args.stage_input else []
        stages = [
            ("inspect_videos.py", ["--input", str(source), "--output", str(output / "inspection"),
                                   "--interval", str(args.interval), *staged]),
        ]
        if args.prepare_frames:
            stages.append(("prepare_annotation_frames.py", ["--input", str(source), "--output", str(output / "annotation_prep"),
                          "--interval", str(args.annotation_interval), *staged]))
        stages.extend([
            ("run_vision_baseline.py", ["--input", str(source), "--output", str(output / "run"),
                "--device", args.device, *staged, *(["--eyes-occluded"] if args.eyes_occluded else []),
                *(["--config",str(ROOT/"config/legacy/vision_baseline.json"),"--rules",str(ROOT/"config/legacy/decision_rules.json")] if args.mode=="legacy" else []),
                *(["--calibration-profile",str(args.calibration_profile)] if args.calibration_profile else []),
                *(["--unity"] if args.unity else [])]),
            ("audit_vision_run.py", ["--run", str(output / "run")]),
            ("encode_review_videos.py", ["--run", str(output / "run")]),
            ("build_vision_review.py", ["--output", str(output), "--scenario-only"]),
        ])
        for script, arguments in stages:
            print(f"\n=== {script} ===", flush=True)
            subprocess.run([sys.executable, str(ROOT / "scripts" / script), *arguments], cwd=ROOT, check=True)
        print(f"\nReview page: {output / 'index.html'}\nVideos: {output / 'run/browser_videos'}")
        return 0
    except (ValueError, ImportError, FileNotFoundError, subprocess.CalledProcessError) as error:
        print(f"Stopped: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
