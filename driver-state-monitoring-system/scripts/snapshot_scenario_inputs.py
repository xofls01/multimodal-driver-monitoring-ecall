#!/usr/bin/env python3
"""Read-only Drive import into an immutable, hashed local 20-scenario snapshot."""
import argparse
import hashlib
import json
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from driver_state_tools.video_common import clip_id, discover_videos, scenario_family, video_name


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source = args.input.expanduser().resolve()
    target = args.output.expanduser().resolve()
    if not source.is_dir() or target == source or source in target.parents:
        raise SystemExit('Input must exist; output must be outside the source tree.')
    if target.exists() and any(target.iterdir()):
        raise SystemExit('Use a new empty snapshot folder; existing copies will not be replaced.')
    videos = discover_videos(source, ['.mp4', '.mov', '.m4v', '.avi', '.mkv', '.webm'])
    ids = Counter(clip_id(video) for video in videos)
    expected = {f'{family}{number}' for family in 'NDAE' for number in range(1, 6)}
    if set(ids) != expected or any(count != 1 for count in ids.values()):
        raise SystemExit(f'Expected exactly N/D/A/E 1..5 once each, got {dict(ids)}')
    target.mkdir(parents=True, exist_ok=True)
    manifest = []
    for index, video in enumerate(videos, 1):
        cid = clip_id(video)
        # Preserve the actual container suffix for supported non-MP4 input too.
        destination = target / scenario_family(video) / f'{cid}{Path(video_name(video)).suffix.lower()}'
        destination.parent.mkdir(parents=True, exist_ok=True)
        print(f'[{index}/{len(videos)}] copying {cid}', flush=True)
        before = video.stat()
        shutil.copy2(video, destination)
        copied_hash = sha256(destination)
        source_hash = sha256(video)
        after = video.stat()
        if (copied_hash != source_hash or before.st_size != after.st_size or
                before.st_mtime_ns != after.st_mtime_ns):
            raise RuntimeError(f'Source changed while copying: {video}')
        manifest.append(dict(clip_id=cid, source_relative_path=str(video.relative_to(source)),
                             snapshot_relative_path=str(destination.relative_to(target)),
                             size_bytes=destination.stat().st_size, sha256=copied_hash))
    (target / 'source_manifest.json').write_text(json.dumps(dict(
        created_at_utc=datetime.now(timezone.utc).isoformat(), input=str(source), video_count=len(manifest),
        capture_conditions_in_scope=False, files=manifest), ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Imported and hash-verified {len(manifest)} clips -> {target}')


if __name__ == '__main__':
    main()
