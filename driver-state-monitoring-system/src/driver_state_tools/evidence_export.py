"""Export real result frames and playable incident clips. EOF never means the car stopped."""
from __future__ import annotations
from copy import deepcopy
from pathlib import Path
import hashlib
import json
import subprocess

import cv2
import imageio_ffmpeg


def confirmed_stop(report):
    item=report.get('vehicle_confirmation') or {}
    return bool(item.get('stop_confirmed') is True and item.get('status')=='confirmed_stopped'
                and item.get('stop_request_observed') is True and item.get('receipt_fresh') is True
                and item.get('source_time_matches') is True and item.get('clip_matches') is True
                and item.get('session_matches') is True
                and report.get('is_current') is True and report.get('observation_quality',{}).get('camera',{}).get('frame_read_ok') is True)


def plan_evidence(reports):
    if not reports:raise ValueError('No actual frame reports')
    def first(state):
        return next((i for i,r in enumerate(reports) if r.get('is_current') is True
                     and r['vision'].get('current') is True and r['vision']['state']==state),None)
    caution,danger=first('caution'),first('danger')
    start=min((x for x in (caution,danger) if x is not None),default=None)
    stopped=next((i for i,r in enumerate(reports) if start is not None and i>=start and confirmed_stop(r)),None)
    return dict(caution_frame=caution,danger_frame=danger,after_stop_frame=stopped,start_frame=start,
                end_frame=stopped if stopped is not None else len(reports)-1,
                stop_confirmed=stopped is not None,
                clip_coverage=('not_applicable' if start is None else
                               'caution_to_confirmed_unity_stop' if stopped is not None and start==caution else
                               'danger_to_confirmed_unity_stop' if stopped is not None else
                               'caution_to_recording_end' if start==caution else 'danger_to_recording_end'))


def export_evidence(clip_dir,clip_id=None):
    clip_dir=Path(clip_dir);clip_id=clip_id or clip_dir.name
    reports=[json.loads(line) for line in (clip_dir/'model_output.jsonl').read_text().splitlines() if line.strip()]
    video=clip_dir/'preview.mp4';cap=cv2.VideoCapture(str(video))
    fps=cap.get(cv2.CAP_PROP_FPS);count=round(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if not cap.isOpened() or fps<=0 or count!=len(reports):
        cap.release();raise ValueError('Evidence needs the exact result video and its per-frame output')
    plan=plan_evidence(reports);dest=clip_dir/'evidence';dest.mkdir(parents=True,exist_ok=True)
    media=dict(full_video=dict(path='preview.mp4',mime_type='video/mp4'),
               caution_photo=None,danger_photo=None,after_stop_photo=None,incident_video=None)
    try:
        for key,filename in [('caution','caution.jpg'),('danger','danger.jpg'),('after_stop','after_stop.jpg')]:
            index=plan[key+'_frame']
            if index is None:continue
            cap.set(cv2.CAP_PROP_POS_FRAMES,index);ok,frame=cap.read()
            if not ok:raise RuntimeError('Could not read the actual evidence frame')
            path=dest/filename
            if not cv2.imwrite(str(path),frame,[cv2.IMWRITE_JPEG_QUALITY,95]):raise RuntimeError('Could not save photo')
            media[key+'_photo']=dict(path=str(path.relative_to(clip_dir)),mime_type='image/jpeg',
                frame_index=index,source_time_seconds=reports[index]['source_time_seconds'],
                vision_state=reports[index]['vision']['state'],confirmed_unity_stop=key=='after_stop')
    finally:cap.release()
    if plan['start_frame'] is not None:
        name='caution_to_stop.mp4' if plan['stop_confirmed'] else 'caution_to_recording_end.mp4'
        if plan['start_frame']!=plan['caution_frame']:name=name.replace('caution','danger')
        path=dest/name;first,last=plan['start_frame'],plan['end_frame']
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),'-y','-v','error','-i',str(video),
            '-vf',f'trim=start_frame={first}:end_frame={last+1},setpts=PTS-STARTPTS','-an',
            '-c:v','libx264','-crf','19','-pix_fmt','yuv420p',
            '-r',format(fps,'.12g'),'-fps_mode','cfr','-movflags','+faststart',str(path)],check=True)
        check=cv2.VideoCapture(str(path));encoded=round(check.get(cv2.CAP_PROP_FRAME_COUNT));clip_fps=check.get(cv2.CAP_PROP_FPS);check.release()
        if encoded!=last-first+1 or abs(clip_fps-fps)>.01:raise RuntimeError('Incident clip timing mismatch')
        media['incident_video']=dict(path=str(path.relative_to(clip_dir)),mime_type='video/mp4',
            start_frame=first,end_frame=last,frames=encoded,fps=clip_fps,
            start_source_seconds=reports[first]['source_time_seconds'],
            last_frame_source_seconds=reports[last]['source_time_seconds'],
            coverage=plan['clip_coverage'],includes_confirmed_stop=plan['stop_confirmed'])
    chosen=plan['danger_frame'] if plan['danger_frame'] is not None else plan['caution_frame']
    chosen=chosen if chosen is not None else len(reports)-1
    result=dict(schema='driver_incident_output.v1',clip_id=clip_id,
        monitoring=deepcopy(reports[chosen]),latest_driver_response=deepcopy(reports[plan['end_frame']]['driver_response']),
        stop_confirmation=dict(confirmed=plan['stop_confirmed'],
            status='confirmed_unity_stop' if plan['stop_confirmed'] else 'not_received',
            source_seconds=reports[plan['after_stop_frame']]['source_time_seconds'] if plan['stop_confirmed'] else None),
        media=media,selection_basis='first actual vision Caution and Danger; confirmed stop only',
        source_video_sha256=hashlib.sha256(video.read_bytes()).hexdigest())
    (clip_dir/'incident_output.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))
    return result
