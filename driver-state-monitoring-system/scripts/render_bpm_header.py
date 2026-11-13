#!/usr/bin/env python3
"""Re-render only the BPM header on verified result videos; keep inference and control logs."""
from pathlib import Path
import argparse
import hashlib
import html
import json
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import cv2
import imageio_ffmpeg
from PIL import Image,ImageDraw
import numpy as np
from driver_state_tools.multimodal_dashboard import draw_bpm_header
from driver_state_tools.bpm_display import RecordedVitals,bpm_range


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input',type=Path,default=ROOT/'outputs/multimodal_recordings')
    ap.add_argument('--output',type=Path,default=ROOT/'outputs/multimodal_bpm_header')
    args=ap.parse_args()
    if args.output.exists():raise ValueError('Use a new result folder; originals are preserved')
    manifest=json.loads((args.input/'input_pairs_used.json').read_text())
    args.output.mkdir(parents=True)
    summaries=[]
    for pair in manifest['pairs']:
        name=pair['id'];source=args.input/name;dest=args.output/name;dest.mkdir()
        rows=[json.loads(l) for l in (source/'fusion.jsonl').read_text().splitlines()]
        original_rates=[json.loads(l) for l in (source/'display_bpm.jsonl').read_text().splitlines()]
        vitals=RecordedVitals(ROOT/pair['biosignal_folder']/'vitals.csv')
        cap=cv2.VideoCapture(str(source/'preview.mp4'));fps=cap.get(cv2.CAP_PROP_FPS)
        count=round(cap.get(cv2.CAP_PROP_FRAME_COUNT));width=round(cap.get(3));height=round(cap.get(4))
        assert len(rows)==len(original_rates)==count and fps>0
        writer=cv2.VideoWriter(str(dest/'intermediate.mp4'),cv2.VideoWriter_fourcc(*'mp4v'),fps,(width,height))
        if not writer.isOpened():raise RuntimeError('Video writer failed')
        headers=[];index=0
        try:
            while True:
                ok,frame=cap.read()
                if not ok:break
                bio=rows[index]['bio'];t=rows[index]['timestamp']
                recorded=vitals.at(bio['timeline_seconds'])
                # Only the top bio rectangle is changed; no camera pixels are cropped or resized.
                patch=Image.fromarray(cv2.cvtColor(frame[8:84,560:849],cv2.COLOR_BGR2RGB))
                state=draw_bpm_header(ImageDraw.Draw(patch),bio,recorded,x=8,clear=True)
                frame[8:84,560:849]=cv2.cvtColor(np.asarray(patch),cv2.COLOR_RGB2BGR)
                expected=original_rates[index]['bpm_range']
                assert state['state']=='measuring' or state['state']==expected
                assert state['bpm']==original_rates[index]['bpm']
                headers.append(dict(timestamp=t,**state,algorithm_state=bio['level']))
                writer.write(frame)
                if index==round(7.7*fps):cv2.imwrite(str(dest/'matched_header.jpg'),frame)
                if index==round(min(12.5,(count-1)/fps)*fps):cv2.imwrite(str(dest/'preview.jpg'),frame)
                index+=1
        finally:
            cap.release();writer.release()
        assert index==count
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),'-y','-v','error','-i',str(dest/'intermediate.mp4'),
            '-c:v','libx264','-crf','18','-pix_fmt','yuv420p','-movflags','+faststart',str(dest/'preview.mp4')],check=True)
        (dest/'intermediate.mp4').unlink()
        check=cv2.VideoCapture(str(dest/'preview.mp4'))
        assert round(check.get(cv2.CAP_PROP_FRAME_COUNT))==count and abs(check.get(cv2.CAP_PROP_FPS)-fps)<.01
        check.release()
        preserved={}
        for filename in ['fusion.jsonl','display_bpm.jsonl','bio_recomputed.jsonl','unity_packets.jsonl']:
            shutil.copy2(source/filename,dest/filename)
            preserved[filename]=sha(dest/filename)
            assert preserved[filename]==sha(source/filename)
        (dest/'header_display.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in headers))
        summary=dict(id=name,frames=count,fps=fps,source_result_video_sha256=sha(source/'preview.mp4'),
                     display_only=True,inference_and_control_logs_unchanged=True,
                     unchanged_logs=preserved,source_analysis=(str(args.input.resolve().relative_to(ROOT))
                         if args.input.resolve().is_relative_to(ROOT) else str(args.input.resolve())),
                     title='BIO STATE (BPM)',header_matches_bpm_range=True,
                     measuring_display_removed=True,models_rerun=False,unity_editor_verified=False)
        (dest/'summary.json').write_text(json.dumps(summary,indent=2));summaries.append(summary)
        print(name,'header aligned:',count,'frames',flush=True)
    cards=[]
    for item in summaries:
        name=html.escape(item['id'])
        cards.append(f'<section><h2>{name}</h2><video controls preload="metadata" poster="{name}/matched_header.jpg" src="{name}/preview.mp4"></video><p><a href="{name}/preview.mp4" download>영상 저장</a></p></section>')
    (args.output/'summary.json').write_text(json.dumps(summaries,indent=2))
    (args.output/'index.html').write_text('<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>BPM 상태 표시 수정</title><style>body{background:#09111a;color:#e8f2fb;font:16px system-ui;margin:24px}video{width:100%;max-width:1756px}section{margin:32px 0}a{color:#69c8ef}p{line-height:1.7}</style><h1>BPM 상태 표시 수정</h1><p>상단 BIO STATE (BPM)과 하단 숫자색이 같은 구간 기준을 사용합니다. MEASURING 표시 없이 실제 값을 즉시 표시합니다. 기존 판단 알고리즘과 Unity 제어 결과는 변경하지 않았습니다.</p>'+''.join(cards)+'</html>')
    shutil.copy2(args.input/'input_pairs_used.json',args.output/'input_pairs_used.json')


if __name__=='__main__':main()
