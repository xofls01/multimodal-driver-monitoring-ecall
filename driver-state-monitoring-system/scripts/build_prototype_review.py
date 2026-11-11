#!/usr/bin/env python3
"""Finalize English evidence videos from CURRENT prototype decisions, no rejudging."""
import argparse
from bisect import bisect_right
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import shutil
import sys
from types import SimpleNamespace

import cv2
import imageio_ffmpeg
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from driver_state_tools.decision_engine import Decision, DriverState
from driver_state_tools.evidence_dashboard import draw_evidence_overlay
from driver_state_tools.vision_features import RawFeatures


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def finalize(run,cid,config,rules):
    source=run/cid/"annotated_videos"/f"{cid}.mp4"
    records_path=run/cid/"frame_records"/f"{cid}.jsonl"
    before=digest(records_path)
    summary=json.loads((run/cid/"clip_summary.json").read_text())
    rows=[json.loads(line) for line in records_path.read_text().splitlines()]
    timestamps=[r["timestamp"] for r in rows]
    if not rows or any(r["decision"]["metadata"].get("mode")!="prototype" for r in rows):
        raise ValueError("Expected actual prototype-mode records")
    cap=cv2.VideoCapture(str(source))
    fps=cap.get(cv2.CAP_PROP_FPS)
    width,height=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    expected=int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    destination=run/"browser_videos"/f"{cid}.mp4"
    destination.parent.mkdir(exist_ok=True)
    if destination.exists(): raise FileExistsError(destination)
    encoder=subprocess.Popen([imageio_ffmpeg.get_ffmpeg_exe(),"-hide_banner","-loglevel","error","-nostdin",
        "-f","rawvideo","-pix_fmt","bgr24","-s",f"{width}x{height}","-r",str(fps),"-i","pipe:0",
        "-an","-c:v","libx264","-crf","18","-preset","fast","-pix_fmt","yuv420p","-movflags","+faststart",str(destination)],
        stdin=subprocess.PIPE,stderr=subprocess.PIPE)
    b=int(summary["outside_border_pixels"]);sw,sh=summary["source_width"],summary["source_height"]
    if width!=sw+config["dashboard_width"]+2*b or height!=sh+2*b:
        raise ValueError("Expected full-resolution scene plus rail and outside border")
    index=0;last=None;previews={round(t*fps) for t in (2,5,8,12,14)}
    first_warning=next((r["timestamp"] for r in rows if r["decision"]["state"]=="caution"),None)
    first_stop=next((r["timestamp"] for r in rows if r["decision"]["state"]=="danger"),None)
    previews.update(round(t*fps) for t in (first_warning,first_stop) if t is not None)
    preview_dir=run/"preview_frames"/cid;preview_dir.mkdir(parents=True,exist_ok=True)
    try:
        while True:
            ok,frame=cap.read()
            if not ok: break
            ri=bisect_right(timestamps,index/fps+1e-6)-1
            if ri<0: raise ValueError("Future observation would be required")
            if ri!=last:
                row=rows[ri];r=row["decision"]
                decision=Decision(row["timestamp"],DriverState(r["state"]),r["reasons"],r["durations"],r["metadata"])
                dummy=RawFeatures(row["timestamp"],False,False,False)
                scene=frame[b:b+sh,b:b+sw]
                rendered=draw_evidence_overlay(scene,dummy,SimpleNamespace(**row["derived"]),decision,
                    SimpleNamespace(**row["rppg"]),config["dashboard_width"],config)
                if not np.array_equal(rendered[b:b+sh,b:b+sw],scene):
                    raise AssertionError("Existing scene changed before encoding")
                template=rendered;last=ri
            output=template.copy()
            output[b:b+sh,b:b+sw]=frame[b:b+sh,b:b+sw]
            if index in previews:
                cv2.imwrite(str(preview_dir/f"{index:04d}.png"),output)
            encoder.stdin.write(output.tobytes());index+=1
    finally:
        cap.release();encoder.stdin.close()
    error=encoder.stderr.read().decode()
    if encoder.wait(): raise RuntimeError(error)
    check=cv2.VideoCapture(str(destination));decoded=0
    check_fps=check.get(cv2.CAP_PROP_FPS)
    check_shape=int(check.get(cv2.CAP_PROP_FRAME_WIDTH)),int(check.get(cv2.CAP_PROP_FRAME_HEIGHT))
    while check.read()[0]: decoded+=1
    check.release()
    assert index==decoded==expected and abs(check_fps-fps)<.001 and check_shape==(width,height)
    assert digest(records_path)==before
    if summary.get("analysis_execution")=="saved_feature_replay":
        # This is a copied render input in a NEW replay output directory.
        # Keep both visible video paths current; the original source run is untouched.
        shutil.copy2(destination,source)
    result=dict(clip_id=cid,frames=decoded,width=width,height=height,fps=check_fps,
        source_scene_unchanged_before_encoding=True,judgments_unchanged=True,
        records_sha256=before,video_sha256=digest(destination),first_caution=first_warning,first_danger=first_stop,
        state_counts=dict(Counter(r["decision"]["state"] for r in rows)),
        current_published=summary["current_decisions_published"])
    print(json.dumps(result),flush=True)
    return result


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run",type=Path,required=True)
    args=ap.parse_args()
    report=json.loads((args.run/"verification.json").read_text())
    config=json.loads((args.run/"config_used.json").read_text())
    rules=json.loads((args.run/"rules_used.json").read_text())
    config["decision_rules"]=rules
    clips=[finalize(args.run,c["clip_id"],config,rules) for c in report["clips"]]
    replay=report.get("analysis_execution")=="saved_feature_replay"
    result=dict(clips=clips,unity_editor_executed=False,actual_inference_and_tcp_mock=not replay,
        analysis_execution="saved_feature_replay" if replay else "actual_inference",
        note="Saved measurements rejudged; no new inference or live publication in this run." if replay else
             "Same decisions as the current inference run. H264 re-encoding is lossy. No generalization/medical accuracy claim.")
    (args.run/"video_verification.json").write_text(json.dumps(result,indent=2)+"\n")
    cards=[]
    for c in clips:
        cid=c["clip_id"]
        counts=" / ".join(f"{k.upper()} {v}" for k,v in c["state_counts"].items())
        cards.append(f'<article><div class="meta">RECORDED RUN / {cid}</div><h2>{cid}</h2>'
            f'<video controls preload="metadata" src="browser_videos/{cid}.mp4"></video>'
            f'<a class="button" download href="browser_videos/{cid}.mp4">Download MP4</a>'
            f'<details><summary>Decision log and timing</summary><p>{counts}</p>'
            f'<p>First Caution: {c["first_caution"]}s / First Danger: {c["first_danger"]}s</p>'
            f'<a href="{cid}/frame_records/{cid}.jsonl">Frame records</a> / '
            f'<a href="{cid}/unity_packets/{cid}.jsonl">Unity packets</a></details></article>')
    page='''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Driver / Evidence</title><style>
    body{margin:0;background:#0b131c;color:#ecf2fa;font:16px/1.7 -apple-system,BlinkMacSystemFont,sans-serif}main{max-width:1440px;margin:48px auto;padding:0 28px}.meta{color:#7d9bb8;font-size:12px;letter-spacing:2px}h1{font-size:40px;margin:10px 0}h2{font-size:26px}p{color:#9fb2c8}.legend{display:flex;gap:26px;flex-wrap:wrap;margin:24px 0}.legend span{border-left:4px solid;padding-left:12px}article{border:1px solid #26364a;background:#101c2a;border-radius:16px;padding:24px;margin:30px 0}video{width:100%;height:auto;background:#080e16;display:block;margin:12px 0 20px}a{color:#8dd6ff}.button{border:1px solid #456079;border-radius:8px;padding:8px 16px;text-decoration:none;display:inline-block}details{margin-top:20px;color:#a6bbd0}.note{border-left:3px solid #617c9a;padding-left:20px;max-width:1100px}</style><main>
    <div class="meta">DRIVER MONITORING / PROTOTYPE</div><h1>See the evidence. Follow the decision.</h1>
    <p>Live duration bars, recent event counts and a visible state border. Scene height and source frame rate are preserved.</p>
    <div class="legend"><span style="border-color:#5de09a">NORMAL</span><span style="border-color:#ffc33e">CAUTION</span><span style="border-color:#ff4e63">DANGER / STOP REQUEST</span><span style="border-color:#93a3b4">UNAVAILABLE</span></div>
    <div class="note"><p>Eye and head estimates are relative measurements. The posture stop path is an explicit simulator rule: posture alert, then the same posture remains observed for 3 seconds. Chest contact, mouth opening and missing observations do not advance that timer.</p><p>Unvalidated eye-only stopping remains disabled. A partial body view can still provide valid head/shoulder evidence. These acted clips demonstrate implementation, not diagnosis or generalization accuracy. Current decisions were sent to a local TCP mock. Unity Editor and vehicle physics were not executed.</p></div>
    '''+"".join(cards)+'''<p><a href="verification.json">Inference and TCP verification</a> / <a href="video_verification.json">Video verification</a></p></main></html>'''
    if replay:
        page=page.replace("Current decisions were sent to a local TCP mock. Unity Editor and vehicle physics were not executed.",
            "This review re-evaluates saved measurements with the updated state logic. Detection was not re-run and these replay results were not published live. Unity Editor and vehicle physics were not executed.")
        page=page.replace("Live duration bars, recent event counts and a visible state border.",
            "Observed eye closure stays NORMAL while EVALUATING, then triggers CAUTION at the configured threshold. Actual missing observations remain UNAVAILABLE. Duration bars, event counts and a visible state border.")
    if report.get("render_execution")=="ui_wording_only":
        page=page.replace("This review re-evaluates saved measurements with the updated state logic.",
            "This review uses unchanged saved decisions with clearer English labels and a remaining-time display.")
        page=page.replace("Observed eye closure stays NORMAL while EVALUATING, then triggers CAUTION at the configured threshold. Actual missing observations remain UNAVAILABLE. Duration bars, event counts and a visible state border.",
            "Plain English labels, time until a stop request, and the same duration bars, event counts and state border.")
    (args.run/"index.html").write_text(page)


if __name__=="__main__": main()
