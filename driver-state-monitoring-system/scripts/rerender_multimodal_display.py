#!/usr/bin/env python3
"""Rerender verified recorded results without recomputing or changing any decision."""
from pathlib import Path
import argparse
from dataclasses import fields
import hashlib
import json
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
import cv2
import numpy as np
import imageio_ffmpeg
from driver_state_tools.decision_engine import Decision, DriverState
from driver_state_tools.multimodal_dashboard import draw_multimodal
from driver_state_tools.bpm_display import RecordedVitals, select_display_bpm
from run_multimodal_replay import make_index, resolve


def decision(d):
    return Decision(d['timestamp'], DriverState(d['state']), d['reasons'], d['durations'], d['metadata'])


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def rerender(pair, source, output):
    old = source/pair['id'];dest = output/pair['id']
    if dest.exists():
        raise ValueError('Output already exists; choose a new output folder')
    dest.mkdir(parents=True)
    summary = json.loads((old/'summary.json').read_text())
    rows = [json.loads(s) for s in (old/'fusion.jsonl').read_text().splitlines()]
    data = np.genfromtxt(resolve(pair['biosignal_folder'])/'biosignals.csv', delimiter=',', names=True)
    samples = np.column_stack([data['clip_s'], data['ecg_raw'], data['ppg_raw']])
    recorded_vitals = RecordedVitals(resolve(pair['biosignal_folder'])/'vitals.csv')
    cap = cv2.VideoCapture(str(resolve(pair['vision_video'])))
    annotation_fps = cap.get(cv2.CAP_PROP_FPS)
    # Annotation containers round 29.798844... to 29.799; the original source
    # clock in the saved decisions remains authoritative.
    fps = summary['fps']
    count = round(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if len(rows) != count or count != summary['frames'] or abs(annotation_fps-fps) > .01:
        raise ValueError('Source annotation and result times do not match')
    writer = None
    previews = set()
    idx = 0
    display_log=(dest/'display_bpm.jsonl').open('w')
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            r = rows[idx]
            if abs(r['timestamp']-idx/fps) > 1e-5:
                raise ValueError('Recorded time does not match frame')
            now = r['bio']['timeline_seconds']
            recorded = recorded_vitals.at(now)
            display_log.write(json.dumps(dict(timestamp=r['timestamp'],**select_display_bpm(r['bio'],recorded)),allow_nan=False)+'\n')
            a = np.searchsorted(samples[:,0], now-8., side='left')
            b = np.searchsorted(samples[:,0], now, side='right')
            annotated = draw_multimodal(frame, decision(r['fusion']), decision(r['vision']), r['bio'], samples[a:b],
                                        r['fusion']['metadata']['multimodal']['synchronization'], recorded_vitals=recorded)
            if writer is None:
                height,width = annotated.shape[:2]
                writer=cv2.VideoWriter(str(dest/'intermediate.mp4'),cv2.VideoWriter_fourcc(*'mp4v'),fps,(width,height))
                if not writer.isOpened():raise RuntimeError('Cannot create video')
            writer.write(annotated)
            for second in [0,1,4,6.5,12.5]:
                if second not in previews and r['timestamp']>=second:
                    cv2.imwrite(str(dest/f'preview_{second:g}s.jpg'),annotated)
                    if second==12.5:cv2.imwrite(str(dest/'preview.jpg'),annotated)
                    previews.add(second)
            idx+=1
    finally:
        display_log.close()
        cap.release()
        if writer:writer.release()
    if idx!=count:raise RuntimeError('Decode incomplete')
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),'-v','error','-y','-i',str(dest/'intermediate.mp4'),'-c:v','libx264',
                    '-crf','19','-pix_fmt','yuv420p','-movflags','+faststart',str(dest/'preview.mp4')],check=True)
    (dest/'intermediate.mp4').unlink()
    for name in ['fusion.jsonl','bio_recomputed.jsonl','unity_packets.jsonl']:
        shutil.copy2(old/name,dest/name)
        if sha(old/name)!=sha(dest/name):raise RuntimeError('Decision log modified during display render')
    check=cv2.VideoCapture(str(dest/'preview.mp4'))
    output_count=round(check.get(cv2.CAP_PROP_FRAME_COUNT));output_fps=check.get(cv2.CAP_PROP_FPS);check.release()
    if output_count!=count or abs(output_fps-fps)>.01:raise RuntimeError('Encoded video timing changed')
    result={k:summary[k] for k in ['id','frames','fps','duration_seconds','fused_state_frames','bio_state_frames','synthetic_biosignal','models']}
    result.update(operation='display_only_rerender',source_result=str(old),source_decision_sha256=sha(old/'fusion.jsonl'),
                  decision_logs_unchanged=True,filter='supplied biosignal.dashboard.filtered_waves(window_s=5, margin_s=3)',
                  display_history_seconds=8,display_window_seconds=5,bio_analyzer_rerun=False,
                  current_fusion_published=0,tcp_mock_session_this_render=False,
                  source_tcp_mock_verified=summary['tcp_mock_verified'],unity_editor_verified=False,
                  output_frames=output_count,output_fps=output_fps,output_size=[width,height])
    (dest/'summary.json').write_text(json.dumps(result,indent=2))
    print(json.dumps({k:result[k] for k in ['id','output_frames','output_fps','decision_logs_unchanged']}),flush=True)
    return result


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--source',type=Path,default=ROOT/'outputs/multimodal_original_bio')
    ap.add_argument('--output',type=Path,default=ROOT/'outputs/multimodal_original_bio_display')
    ap.add_argument('--pairs',type=Path,default=ROOT/'config/multimodal/replay_pairs.json')
    ap.add_argument('--only',nargs='+',help='Render selected clips; retain other completed clips')
    args=ap.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    pairs=json.loads(args.pairs.read_text())['pairs']
    for p in pairs:
        if not args.only or p['id'] in args.only:
            rerender(p,args.source,args.output)
    summaries=[json.loads((args.output/p['id']/'summary.json').read_text()) for p in pairs
               if (args.output/p['id']/'summary.json').exists()]
    (args.output/'summary.json').write_text(json.dumps(summaries,indent=2))
    make_index(args.output,summaries)


if __name__=='__main__':main()
