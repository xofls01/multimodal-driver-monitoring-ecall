#!/usr/bin/env python3
"""Reproduce the received E4 display calculation and verify integration separation."""
from pathlib import Path
import argparse
import ast
import csv
import json
import sys
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from driver_state_tools.recent_pulse import RecentPulseRates


def rows(path):
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--results',type=Path,default=ROOT/'outputs/multimodal_recordings')
    ap.add_argument('--previous',type=Path,default=ROOT/'outputs/multimodal_fast5')
    args=ap.parse_args()
    manifest=json.loads((ROOT/'config/multimodal/system_pairs.json').read_text())
    e4=next(pair for pair in manifest['pairs'] if pair['id']=='E4')
    folder=ROOT/e4['biosignal_folder']
    # Execute just the inspected display-calculation prefix; no video generation or scenario labels.
    reference=ROOT/'reference/supplied_overlay/E4_overlay.py'
    tree=ast.parse(reference.read_text().split('# 심실세동 구간은')[0])
    tree.body=[n for n in tree.body if not (isinstance(n,ast.Assign) and
               any(isinstance(t,ast.Name) and t.id=='VIDEO' for t in n.targets))]
    ctx={'__file__':str(folder/'_overlay.py')}
    exec(compile(tree,str(reference),'exec'),ctx)
    original_bpm=float(ctx['bpm_at'](ctx['r_t'],6.8))
    assert 187<original_bpm<190, original_bpm
    with (folder/'biosignals.csv').open() as f:
        raw=np.array([[float(r[k]) for k in ('clip_s','ecg_raw','ppg_raw')] for r in csv.DictReader(f)])
    causal=RecentPulseRates(raw).at(6.8)
    assert abs(causal['ecg']['bpm']-original_bpm)<5
    assert causal['last_sample_timestamp']<=6.8
    clips=[]
    for pair in manifest['pairs']:
        name=pair['id'];dest=args.results/name
        before=rows(args.previous/name/'bio_recomputed.jsonl')
        after=rows(dest/'bio_recomputed.jsonl')
        assert before==after, 'Monitor fields changed: '+name
        frames=rows(dest/'fusion.jsonl');packets=rows(dest/'unity_packets.jsonl')
        display=rows(dest/'display_bpm.jsonl')
        assert len(frames)==len(packets)==len(display)
        for frame,packet,rate in zip(frames,packets,display):
            assert packet['bio_state']==frame['bio']['level']
            if rate['source'] and rate['source'].startswith('recent_'):
                assert packet['bio_display_hr']==rate['bpm']
                assert packet['bio_display_hr_used_for_grade'] is False
                assert packet['bio_hr']==(frame['bio']['hr'] if frame['bio']['available'] else None)
            if frame['bio'].get('display_pulse'):
                pulse=frame['bio']['display_pulse']
                assert pulse['last_sample_timestamp']<=frame['bio']['timeline_seconds']
                assert pulse['used_for_grade'] is False
        item=dict(id=name,original_monitor_updates_unchanged=len(after),frames_checked=len(frames))
        if name=='E4':
            item['display_at_6_8s']=min(display,key=lambda r:abs(r['timestamp']-6.8))
            item['display_at_7_13s']=min(display,key=lambda r:abs(r['timestamp']-7.13))
            assert item['display_at_6_8s']['bpm']>170
        clips.append(item)
    report=dict(received_overlay_at_6_8s=original_bpm,prefix_only_ecg_at_6_8s=causal['ecg']['bpm'],
                difference_reason='Full-file source filtering vs received-prefix filtering and peak confirmation',
                monitor_fields_unchanged=True,scenario_labels_used=False,future_samples_used=False,
                display_values_used_for_grade=False,clips=clips,unity_editor_verified=False)
    (args.results/'bpm_display_verification.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
