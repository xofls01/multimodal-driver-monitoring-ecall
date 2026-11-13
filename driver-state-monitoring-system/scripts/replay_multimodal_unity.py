#!/usr/bin/env python3
"""Send a saved integration trial at 1x media time. Explicit replay, not live sensing."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from driver_state_tools.decision_engine import Decision, DriverState
from driver_state_tools.unity_bridge import UnityStateServer, replay_decisions


def load(path):
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if not rows or any(b['timestamp'] <= a['timestamp'] for a, b in zip(rows, rows[1:])):
        raise ValueError('Need one clip with strictly increasing timestamps')
    result = []
    for r in rows:
        d = r['fusion']
        if abs(d['timestamp'] - r['timestamp']) > 1e-6:
            raise ValueError('Fusion timestamp mismatch')
        if d.get('metadata', {}).get('pipeline') not in ('multimodal_replay','multimodal_analysis'):
            raise ValueError('Not a multimodal replay')
        d['metadata']['original_analysis_origin']=d['metadata'].get('analysis_origin')
        d['metadata']['pipeline']='multimodal_replay'
        d['metadata']['analysis_origin']='saved_decision_replay'
        result.append((path.parent.name, Decision(d['timestamp'], DriverState(d['state']), d['reasons'], d['durations'], d['metadata'])))
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--recording', type=Path, required=True)
    ap.add_argument('--port', type=int, default=5001)
    ap.add_argument('--allow-synthetic-control', action='store_true', help='Permit simulator commands based on synthetic signals')
    args = ap.parse_args()
    rows = load(args.recording)
    if any(d.metadata['multimodal']['synthetic'] for _,d in rows) and not args.allow_synthetic_control:
        ap.error('These signals are synthetic. For a simulator trial, explicitly use --allow-synthetic-control.')
    print(f'1x FILE REPLAY / waiting for simulator TCP client on 127.0.0.1:{args.port}', flush=True)
    with UnityStateServer(port=args.port) as server:
        try:
            replay_decisions(server, rows)
        except KeyboardInterrupt:
            server.mark_unavailable('replay_cancelled')


if __name__ == '__main__':
    main()
