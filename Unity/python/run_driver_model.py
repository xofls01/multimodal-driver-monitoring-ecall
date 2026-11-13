#!/usr/bin/env python3
"""Launch the unmodified driver model with one result directory per input dataset.

Examples (from the repository root):
  python3 -B Unity/python/run_driver_model.py --only E4 --show
  python3 -B Unity/python/run_driver_model.py --only E4 --unity --show

Model options are forwarded unchanged. --unity defaults to port 5002 so the
existing vehicle server can continue listening for Unity on port 5001.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys


def dataset_directory(model_root: Path, model_args: list[str]) -> Path:
    """Identify the selected video/signal contents, independently of file location."""
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--pairs", type=Path, default=Path("config/multimodal/system_pairs.json"))
    parser.add_argument("--only", nargs="+")
    options, _ = parser.parse_known_args(model_args)

    def resolve(path: str | Path) -> Path:
        path = Path(path).expanduser()
        return path if path.is_absolute() else model_root / path

    manifest = json.loads(resolve(options.pairs).read_text())
    pairs = [p for p in manifest["pairs"] if not options.only or p["id"] in options.only]
    if not pairs:
        raise ValueError("선택한 입력 데이터가 없습니다. --only 값을 확인하세요.")
    pairs.sort(key=lambda pair: str(pair["id"]))
    identity = []
    for pair in pairs:
        folder = resolve(pair["biosignal_folder"])
        files = {"video": resolve(pair["video"])}
        files.update({name: folder / name for name in ("biosignals.csv", "frames.csv", "session.json")})
        if (folder / "vitals.csv").is_file():
            files["vitals.csv"] = folder / "vitals.csv"
        hashes = {}
        for name, path in files.items():
            digest = hashlib.sha256()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            hashes[name] = digest.hexdigest()
        identity.append(dict(id=pair["id"], files=hashes,
                             bio_offset_seconds=float(pair.get("bio_offset_seconds", 0.0)),
                             sync_basis=pair.get("sync_basis"),
                             eyes_occluded=bool(pair.get("eyes_occluded", False))))
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]
    label = re.sub(r"[^a-zA-Z0-9_-]+", "_", "_".join(str(p["id"]) for p in pairs))[:64]
    return model_root / "outputs" / f"data_{label}_{fingerprint}"


def lock_output(output: Path) -> int:
    """Keep the same-dataset lock through exec; release automatically on exit."""
    descriptor = os.open(output / ".model_run.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.set_inheritable(descriptor, True)
    except OSError as error:
        os.close(descriptor)
        raise ValueError("같은 데이터의 분석이 이미 실행 중입니다. 종료 후 다시 실행하세요.") from error
    return descriptor


def archive_transport_log(output: Path) -> bool:
    """Preserve previous TCP records in one history file, then allow a fresh log."""
    current = output / "unity_transport.jsonl"
    if not current.exists():
        return False
    history = output / "unity_transport.history.jsonl"
    if current.is_symlink() or history.is_symlink():
        raise ValueError("TCP 로그는 일반 파일이어야 합니다.")
    with current.open("rb") as source, history.open("ab") as target:
        shutil.copyfileobj(source, target)
        target.flush()
        os.fsync(target.fileno())
    current.unlink()
    return True


def prepare_launch(
    argv: list[str] | None = None, *, project_root: Path | None = None,
) -> tuple[list[str], dict[str, str], Path]:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                     allow_abbrev=False)
    parser.add_argument("--output", type=Path, help="Optional new or empty result directory; defaults to a reusable dataset directory")
    parser.add_argument("--model-python", type=Path, help="Optional model Python executable; defaults to the model's .venv")
    options, model_args = parser.parse_known_args(argv)
    root = project_root or Path(__file__).resolve().parents[2]
    model_root = (root / "driver-state-monitoring-system").resolve()
    python = (options.model_python.expanduser().resolve() if options.model_python else
              model_root / ".venv/bin/python")
    script = model_root / "scripts/run_multimodal_system.py"
    if not script.is_file():
        raise ValueError(f"모델 실행 파일이 없습니다: {script}")
    if not python.is_file():
        raise ValueError(f"모델 Python 환경이 없습니다: {python}")
    output_base = model_root / "outputs"
    output_base.mkdir(parents=True, exist_ok=True)
    if options.output is not None:
        output = options.output.expanduser().resolve()
        if output.exists() and (not output.is_dir() or any(output.iterdir())):
            raise ValueError(f"기존 결과를 보존합니다: {output}\n--output을 생략하면 입력 데이터의 결과 폴더를 자동 선택합니다.")
        output.mkdir(parents=True, exist_ok=True)
    else:
        output = dataset_directory(model_root, model_args)
        output.mkdir(parents=True, exist_ok=True)
    if "--unity" in model_args and not any(
        arg == "--unity-port" or arg.startswith("--unity-port=") for arg in model_args
    ):
        model_args.extend(["--unity-port", "5002"])
    cache = output_base / ".runtime_cache"
    for directory in (cache / "matplotlib", cache / "ultralytics/Ultralytics", cache / "fontconfig"):
        directory.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment.setdefault("MPLCONFIGDIR", str(cache / "matplotlib"))
    environment.setdefault("YOLO_CONFIG_DIR", str(cache / "ultralytics"))
    environment.setdefault("XDG_CACHE_HOME", str(cache))
    command = [str(python), "-B", str(script), *model_args, "--output", str(output)]
    return command, environment, model_root


def main() -> int:
    descriptor = None
    try:
        command, environment, model_root = prepare_launch()
        output = Path(command[-1])
        descriptor = lock_output(output)
        archived = archive_transport_log(output)
        print(f"[MODEL LAUNCH] Results: {output}", flush=True)
        if archived:
            print("[MODEL LAUNCH] 기존 TCP 기록을 unity_transport.history.jsonl에 보존했습니다.", flush=True)
        if "--unity" in command:
            print("[MODEL LAUNCH] 차량 서버와 Unity Play 연결을 기다립니다.", flush=True)
        os.chdir(model_root)
        os.execve(command[0], command, environment)
    except (ValueError, OSError) as error:
        print(f"[MODEL LAUNCH] {error}", file=sys.stderr)
        return 2
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
