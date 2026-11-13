#!/usr/bin/env python3
"""Current inference on paired raw video and ECG/PPG files, in one Python process."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
os.environ.setdefault('MPLCONFIGDIR',str(ROOT/'outputs/.runtime_cache/matplotlib'))
os.environ.setdefault('YOLO_CONFIG_DIR',str(ROOT/'outputs/.runtime_cache/ultralytics'))

import cv2
import torch
import imageio_ffmpeg
from biosignal import BioSignalMonitor, Config
from driver_state_tools.analysis_publisher import AnalysisPublisher
from driver_state_tools.biosignal_replay import BiosignalReplay, validate_frame_clock
from driver_state_tools.biosignal_startup import load_startup_profile
from driver_state_tools.bpm_display import RecordedVitals, select_display_bpm, bpm_range
from driver_state_tools.multimodal_dashboard import draw_multimodal
from driver_state_tools.multimodal_runtime import CurrentVisionSource, MultimodalProcessor
from driver_state_tools.evidence_export import export_evidence
from driver_state_tools.revision_session import load_profile
from driver_state_tools.rppg import RPPGResult
from driver_state_tools.unity_bridge import UnityStateServer, vehicle_control_packet
from driver_state_tools.vision_pipeline import VisionModels, draw_overlay
from run_multimodal_replay import TCPProbe, make_index
from run_vision_baseline import raw_record


def resolve(value):
    p=Path(value).expanduser()
    return p if p.is_absolute() else ROOT/p


def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()


def process(pair, manifest, config, rules, models, output, args, external_server=None):
    source=resolve(pair['video']);folder=resolve(pair['biosignal_folder'])
    if digest(source)!=pair['source_sha256']:raise ValueError('Source SHA256 mismatch: '+pair['id'])
    dest=output/pair['id'];dest.mkdir(parents=True,exist_ok=True)
    cap=cv2.VideoCapture(str(source))
    if not cap.isOpened():raise RuntimeError('Cannot open raw video '+str(source))
    fps=cap.get(cv2.CAP_PROP_FPS);count=round(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if fps<=0 or count<=0:raise ValueError('Invalid raw video timing')
    every=max(1,round(fps/config['analysis_fps']))
    sync=validate_frame_clock(folder,count,fps,pair['sync_basis'])
    sync.update(bio_offset_seconds=pair['bio_offset_seconds'],source_sha256=pair['source_sha256'])
    profile=load_profile(resolve(config['calibration_profile']),source)
    vision=CurrentVisionSource(models,config,rules,profile,eyes_occluded=bool(pair.get('eyes_occluded',False)))
    bio_cfg,startup=load_startup_profile(args.bio_startup_profile)
    monitor=BioSignalMonitor(bio_cfg,model_path=str(resolve(manifest['models']['ecg'])),
        ppg_encoder_path=str(resolve(manifest['models']['ppg_encoder'])),
        ppg_head_path=str(resolve(manifest['models']['ppg_head'])))
    display_settings=manifest.get('bpm_display',{})
    signal=BiosignalReplay(folder,monitor,offset_seconds=pair['bio_offset_seconds'],
        display_peak_rates=display_settings.get('method')=='recent_two_rr',
        display_confirmation_s=display_settings.get('confirmation_seconds',.08))
    vitals=RecordedVitals(folder/'vitals.csv')
    signal.recorded_vitals=vitals
    probe=TCPProbe(dest) if args.verify_tcp else None
    server=probe.server if probe else external_server
    publisher=AnalysisPublisher(server)
    if server:server.mark_unavailable('new_video_session')
    processor=MultimodalProcessor(vision,signal,sync,pair['id'],vision_every_frames=every,publisher=publisher)
    writer=None;idx=0;counts=Counter();bio_counts=Counter();vision_counts=Counter();bpm_sources=Counter();first_states={}
    started=time.monotonic();pace_started=None;expected=set();rows=[]
    if args.show:
        cv2.namedWindow('Driver monitoring - current video + ECG/PPG',cv2.WINDOW_NORMAL)
    try:
        with (dest/'fusion.jsonl').open('w') as log, (dest/'vision_frames.jsonl').open('w') as v_log, (dest/'unity_packets.jsonl').open('w') as p_log, (dest/'display_bpm.jsonl').open('w') as h_log, (dest/'model_output.jsonl').open('w') as output_log:
            while True:
                if external_server is not None:
                    pause=time.monotonic()
                    while not external_server.connected.wait(.05):pass
                    if pace_started is not None:pace_started+=time.monotonic()-pause
                ok,frame=cap.read()
                if not ok:
                    if idx<count:processor.publish_camera_failure(idx/fps)
                    break
                t=idx/fps
                if args.pace or external_server is not None:
                    if pace_started is None:pace_started=time.monotonic()
                    while time.monotonic()<pace_started+t:
                        time.sleep(min(.02,max(0,pace_started+t-time.monotonic())))
                result=processor.update(frame,t)
                if probe and not probe.server.wait_until_sent(result.publish_revision,timeout=2):
                    raise RuntimeError('Current inference did not reach the TCP receiver')
                v,b,d=result.vision,result.bio,result.decision
                expected.add((round(t,5),d.state.value))
                packet=vehicle_control_packet(d,idx+1,clip_id=pair['id'])
                p_log.write(json.dumps(packet,allow_nan=False)+'\n')
                report=d.metadata['monitoring_output']
                output_log.write(json.dumps(report,allow_nan=False)+'\n')
                log.write(json.dumps(dict(timestamp=t,vision=asdict(v.decision),bio=b,fusion=asdict(d),
                    vision_inferred_this_frame=result.vision_inferred,analysis_origin='current_video_and_ecg_ppg_inference'),allow_nan=False)+'\n')
                if result.vision_inferred:
                    v_log.write(json.dumps(dict(timestamp=t,source_sha256=pair['source_sha256'],
                        raw=raw_record(v.raw),derived=v.derived.to_dict(),decision=asdict(v.decision),
                        prototype_frame=vision.session.record_frame()),allow_nan=False)+'\n')
                recorded=vitals.at(b['timeline_seconds']);rate=select_display_bpm(b,recorded)
                rate['bpm_range']=bpm_range(rate['bpm'])
                h_log.write(json.dumps(dict(timestamp=t,**rate),allow_nan=False)+'\n')
                bpm_sources[rate['source'] or 'missing']+=1
                counts[d.state.value]+=1;bio_counts[b['level']]+=1;vision_counts[v.decision.state.value]+=1
                first_states.setdefault(d.state.value,t)
                pulse=RPPGResult(timestamp=t,status='disabled',source='external_biosignals',note='Not used for fusion')
                vision_image=draw_overlay(frame,v.raw,v.derived,v.decision,pulse,int(config['dashboard_width']),config)
                image=draw_multimodal(vision_image,d,v.decision,b,signal.waveform(t,8.),sync,recorded_vitals=recorded)
                if writer is None:
                    height,width=image.shape[:2]
                    writer=cv2.VideoWriter(str(dest/'intermediate.mp4'),cv2.VideoWriter_fourcc(*'mp4v'),fps,(width,height))
                    if not writer.isOpened():raise RuntimeError('Cannot write output video')
                writer.write(image)
                if idx==0 or (idx>0 and (idx-1)/fps<1<=t):cv2.imwrite(str(dest/'early_preview.jpg'),image)
                if idx>0 and (idx-1)/fps<12.5<=t:cv2.imwrite(str(dest/'preview.jpg'),image)
                if args.show:
                    cv2.imshow('Driver monitoring - current video + ECG/PPG',image)
                    if cv2.waitKey(1)&0xff==ord('q'):raise KeyboardInterrupt()
                idx+=1
        if idx!=count:raise RuntimeError(f'Video decode stopped early {idx}/{count}')
        if probe:
            seen={(round(p['source_timestamp'],5),p['fusion_state']) for p in probe.packets
                if p.get('pipeline')=='multimodal_analysis' and p.get('clip_id')==pair['id']}
            if expected-seen:raise RuntimeError(f'TCP missed {len(expected-seen)} current decisions')
            (dest/'received_packets.jsonl').write_text(''.join(json.dumps(p)+'\n' for p in probe.packets))
        (dest/'bio_recomputed.jsonl').write_text(''.join(json.dumps(s,allow_nan=False)+'\n' for s in signal.history))
        (dest/'latest_output.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False))
        reports=[json.loads(l) for l in (dest/'model_output.jsonl').read_text().splitlines()]
        danger_reports=[r for i,r in enumerate(reports) if r['danger']['active'] and
                        (i==0 or r['danger']['event_id']!=reports[i-1]['danger']['event_id'])]
        (dest/'danger_events.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in danger_reports))
        vision_danger_reports=[r for i,r in enumerate(reports) if r['vision']['danger']['active'] and
            (i==0 or r['vision']['danger']['event_id']!=reports[i-1]['vision']['danger']['event_id'])]
        (dest/'vision_danger_events.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in vision_danger_reports))
        example=next((r for r in reports if r['vision']['danger']['active']),reports[-1])
        (dest/'example_output.json').write_text(json.dumps(example,ensure_ascii=False,indent=2,allow_nan=False))
    finally:
        cap.release()
        if writer:writer.release()
        signal.close();publisher.finish()
        if probe:probe.close()
        if args.show:cv2.destroyAllWindows()
    elapsed=time.monotonic()-started
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),'-y','-v','error','-i',str(dest/'intermediate.mp4'),
        '-c:v','libx264','-crf','19','-pix_fmt','yuv420p','-movflags','+faststart',str(dest/'preview.mp4')],check=True)
    (dest/'intermediate.mp4').unlink()
    check=cv2.VideoCapture(str(dest/'preview.mp4'))
    encoded_count=round(check.get(cv2.CAP_PROP_FRAME_COUNT));encoded_fps=check.get(cv2.CAP_PROP_FPS);check.release()
    if encoded_count!=count or abs(encoded_fps-fps)>.01:raise RuntimeError('Output timing changed')
    evidence=export_evidence(dest,pair['id'])
    summary=dict(id=pair['id'],frames=idx,fps=fps,duration_seconds=idx/fps,output_frames=encoded_count,
        output_fps=encoded_fps,output_size=[width,height],analysis_origin='current_video_and_ecg_ppg_inference',
        input_mode='paired_files',cached_vision_used=False,vision_inference_calls=processor.vision_count,
        bio_model_updates=len(signal.history),current_fusion_published=publisher.published_count,
        tcp_mock_verified=bool(probe),external_unity_publisher_enabled=external_server is not None,
        unity_editor_verified=False,processing_seconds=elapsed,processing_fps=idx/elapsed,
        fused_state_frames=dict(counts),vision_state_frames=dict(vision_counts),bio_state_frames=dict(bio_counts),
        display_bpm_sources=dict(bpm_sources),first_state_seconds=first_states,synthetic_biosignal=signal.synthetic,
        source_sha256=pair['source_sha256'],synchronization=sync,biosignal_startup=startup,
        bpm_display_settings=display_settings,
        evidence_output='incident_output.json',stop_confirmed=evidence['stop_confirmation']['confirmed'],
        models={k:dict(path=v,sha256=digest(resolve(v))) for k,v in manifest['models'].items()})
    (dest/'summary.json').write_text(json.dumps(summary,indent=2))
    print(json.dumps({k:summary[k] for k in ['id','vision_inference_calls','bio_model_updates','current_fusion_published','processing_fps','fused_state_frames']}),flush=True)
    return summary


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pairs',type=Path,default=ROOT/'config/multimodal/system_pairs.json')
    p.add_argument('--output',type=Path,help='Result folder; default follows the selected startup profile')
    p.add_argument('--only',nargs='+')
    p.add_argument('--bio-startup-profile',type=Path,help='Startup-only settings; original10 and fast5 are available')
    group=p.add_mutually_exclusive_group()
    group.add_argument('--verify-tcp',action='store_true',help='Verify current decisions with a local Python TCP receiver')
    group.add_argument('--unity',action='store_true',help='Publish CURRENT inference to a connected Unity TCP client')
    p.add_argument('--unity-host',default='127.0.0.1')
    p.add_argument('--unity-port',type=int,default=5001)
    p.add_argument('--show',action='store_true')
    p.add_argument('--pace',action='store_true',help='Do not process file time faster than 1x; slow inference may still lag')
    args=p.parse_args()
    manifest=json.loads(args.pairs.read_text());pairs=[x for x in manifest['pairs'] if not args.only or x['id'] in args.only]
    args.bio_startup_profile=(args.bio_startup_profile.expanduser().resolve() if args.bio_startup_profile else
        resolve(manifest['biosignal_startup_profile']) if manifest.get('biosignal_startup_profile') else None)
    bio_cfg,startup=load_startup_profile(args.bio_startup_profile)
    if not pairs:p.error('No matching input pairs')
    config=json.loads((ROOT/'config/prototype/vision_baseline.json').read_text())
    rules=json.loads((ROOT/'config/prototype/decision_rules.json').read_text());config['decision_rules']=rules
    default_folder=('multimodal_output_contract' if manifest.get('bpm_display',{}).get('method')=='recent_two_rr' and startup['profile']=='fast5_experiment' else
                    'multimodal_recent_bpm_original10' if manifest.get('bpm_display',{}).get('method')=='recent_two_rr' and startup['original_defaults'] else
                    'multimodal_recent_bpm_experiment' if manifest.get('bpm_display',{}).get('method')=='recent_two_rr' else
                    'multimodal_fast5' if startup['profile']=='fast5_experiment' else
                    'multimodal_system' if startup['original_defaults'] else 'multimodal_startup_experiment')
    output=args.output.resolve() if args.output else ROOT/'outputs'/default_folder
    for pair in pairs:
        for source_dir in [resolve(pair['video']).parent.resolve(),resolve(pair['biosignal_folder']).resolve()]:
            if output==source_dir or source_dir in output.parents:p.error('Output must be separate from input data')
    output.mkdir(parents=True,exist_ok=True)
    (output/'input_pairs_used.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    torch.set_num_threads(2)
    (output/'biosignal_config_used.json').write_text(json.dumps(asdict(bio_cfg),indent=2))
    (output/'biosignal_startup_used.json').write_text(json.dumps(startup,indent=2))
    (output/'vision_config_used.json').write_text(json.dumps(config,indent=2))
    models=VisionModels(config,ROOT)
    server=UnityStateServer(args.unity_host,args.unity_port,log_path=output/'unity_transport.jsonl').start() if args.unity else None
    if server:print(f'Current inference TCP server {args.unity_host}:{server.port}; waiting for Unity client',flush=True)
    summaries=[]
    try:
        for pair in pairs:
            print('Analyzing raw video and ECG/PPG:',pair['id'],flush=True)
            summaries.append(process(pair,manifest,config,rules,models,output,args,server))
    except KeyboardInterrupt:
        return 130
    finally:
        models.close()
        if server:server.stop()
    (output/'summary.json').write_text(json.dumps(summaries,indent=2));make_index(output,summaries)
    review=output/'index.html'
    page=review.read_text().replace('<h1>Vision + ECG/PPG</h1>',
        '<h1>Vision + ECG/PPG — 최종 출력</h1>')
    page=page.replace('파란색 Measuring은 초기 측정 준비를 뜻하며, 실제 신호 불량과 구분해서 표시합니다.',
        '생체신호 상단과 숫자색은 현재 BPM 구간을 즉시 표시합니다. 실제 값이 없으면 판단 불가입니다.')
    page=page.replace('초록색은 정상, 노란색은 주의, 빨간색은 시뮬레이터 정차 요청, 회색은 판단 불가입니다.',
        '영상 테두리와 전체 상태는 통합 판단입니다. 생체신호 상단과 BPM 숫자색은 현재 BPM 구간입니다.')
    for summary in summaries:
        name=summary['id']
        page=page.replace(f'<a href="{name}/summary.json">실행 결과</a>',
            f'<a href="{name}/example_output.json">출력 예시 JSON</a>'
            f'<a href="{name}/model_output.jsonl" download>프레임별 최종 출력</a>'
            f'<a href="{name}/danger_events.jsonl" download>Danger 기록</a>'
            f'<a href="{name}/summary.json">실행 결과</a>')
    review.write_text(page)
    return 0


if __name__=='__main__':raise SystemExit(main())
