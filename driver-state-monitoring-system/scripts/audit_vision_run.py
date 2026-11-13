#!/usr/bin/env python3
"""Check rendered output integrity and make review snapshots, not accuracy scores."""
import argparse
import json
from collections import Counter
from pathlib import Path
import cv2


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', type=Path, default=Path('outputs/driver_monitor_20/run'))
    args = parser.parse_args()
    target = args.run / 'review_audit'
    target.mkdir(parents=True, exist_ok=True)
    audits = []
    for path in sorted((args.run / 'frame_records').glob('*.jsonl')):
        records = [json.loads(line) for line in path.open(encoding='utf-8')]
        video = args.run / 'annotated_videos' / (path.stem + '.mp4')
        capture = cv2.VideoCapture(str(video))
        fps = capture.get(cv2.CAP_PROP_FPS)
        size = [int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)), int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))]
        expected = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        count = 0
        while capture.read()[0]:
            count += 1
        capture.release()
        if expected <= 0 or count != expected:
            raise RuntimeError(f'{path.stem}: incomplete rendered video ({count}/{expected})')
        states = Counter(row['decision']['state'] for row in records)
        reasons = Counter(reason for row in records for reason in row['decision']['reasons'])
        phone = Counter(row['raw'].get('phone_track_mode') or 'unconfirmed' for row in records)
        rppg = Counter(row['rppg']['status'] for row in records)
        snapshots = {}
        selections = [('phone', next((r for r in records if r['raw'].get('phone_track_mode') == 'detected'), None)),
                      ('caution', next((r for r in records if r['decision']['state'] == 'caution'), None)),
                      ('late', records[-1] if records else None)]
        for label, row in selections:
            if row is None:
                continue
            capture = cv2.VideoCapture(str(video))
            capture.set(cv2.CAP_PROP_POS_MSEC, row['timestamp'] * 1000)
            ok, image = capture.read()
            capture.release()
            if not ok:
                raise RuntimeError(f'{path.stem}: snapshot failed at {row["timestamp"]}')
            name = f'{path.stem}_{label}.jpg'
            if not cv2.imwrite(str(target / name), image):
                raise RuntimeError(f'cannot save {name}')
            snapshots[label] = dict(timestamp=row['timestamp'], image=name)
        audits.append(dict(clip_id=path.stem, rendered_size=size, fps=fps, decoded_frames=count,
                           analyzed_frames=len(records), states=dict(states), reasons=dict(reasons),
                           phone_modes=dict(phone), rppg_states=dict(rppg), snapshots=snapshots))
    report = dict(note='Observed pipeline outputs, not accuracy: no adjudicated ground truth.',
                  completed_clips=len(audits), clips=audits)
    (target / 'audit.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'{len(audits)} completed clips checked -> {target}')


if __name__ == '__main__':
    main()
