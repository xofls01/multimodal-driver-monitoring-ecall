#!/usr/bin/env python3
"""Build synchronized offline previews. TCP verification connects only to a local mock client."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import html
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

import cv2
import torch
from biosignal import BioSignalMonitor, Config
from driver_state_tools.biosignal_replay import BiosignalReplay, validate_frame_clock
from driver_state_tools.decision_engine import Decision, DriverState
from driver_state_tools.multimodal_fusion import combine
from driver_state_tools.multimodal_dashboard import draw_multimodal
from driver_state_tools.bpm_display import RecordedVitals, select_display_bpm
from driver_state_tools.analysis_publisher import AnalysisPublisher
from driver_state_tools.unity_bridge import UnityStateServer, vehicle_control_packet


def digest(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def resolve(value):
    p = Path(value).expanduser()
    return p if p.is_absolute() else ROOT / p


class TCPProbe:
    """Real NDJSON socket, not the Unity application. Every publish waits for its transport revision."""
    def __init__(self, output):
        self.server = UnityStateServer(port=0, log_path=output / 'transport.jsonl').start()
        self.packets = []
        self.sock = socket.create_connection(('127.0.0.1', self.server.port), timeout=5)
        self.sock.settimeout(5)
        self.thread = threading.Thread(target=self.read, daemon=True)
        self.thread.start()

    def read(self):
        try:
            with self.sock.makefile('r') as f:
                for line in f:
                    self.packets.append(json.loads(line))
        except (OSError, ValueError):
            pass

    def close(self):
        self.server.stop()
        self.sock.close()
        self.thread.join(timeout=6)


def as_decision(record):
    d = record['decision']
    return Decision(float(record['timestamp']), DriverState(d['state']), d['reasons'], d['durations'], d.get('metadata', {}))


def process(pair, config, output, verify_tcp=False, fresh_vision=False):
    source, folder = resolve(pair['video']), resolve(pair['biosignal_folder'])
    if digest(source) != pair['source_sha256']:
        raise ValueError('Wrong source video for pair ' + pair['id'])
    target = output / pair['id']
    target.mkdir(parents=True, exist_ok=True)
    vp, rp = resolve(pair['vision_video']), resolve(pair['vision_records'])
    provenance = 'source-hash-verified previous actual vision inference'
    if fresh_vision or not vp.exists() or not rp.exists():
        vision_output = target / 'vision'
        subprocess.run([sys.executable, str(ROOT / 'scripts/run_vision_baseline.py'), '--input', str(source),
            '--output', str(vision_output), '--config', str(ROOT / 'config/prototype/vision_baseline.json'),
            '--rules', str(ROOT / 'config/prototype/decision_rules.json')], check=True, cwd=ROOT)
        rp = next((vision_output / 'frame_records').glob('*.jsonl'))
        vp = next((vision_output / 'annotated_videos').glob('*.mp4'))
        provenance = 'fresh actual vision inference'
    records = [json.loads(line) for line in rp.read_text().splitlines() if line.strip()]
    if not records or any(r['source_sha256'] != pair['source_sha256'] for r in records):
        raise ValueError('Vision records belong to a different source')
    if records[0]['timestamp'] != 0 or any(b['timestamp'] <= a['timestamp'] for a, b in zip(records, records[1:])):
        raise ValueError('Invalid vision record timeline')
    raw_capture = cv2.VideoCapture(str(source))
    fps = raw_capture.get(cv2.CAP_PROP_FPS)
    count = round(raw_capture.get(cv2.CAP_PROP_FRAME_COUNT))
    raw_capture.release()
    sync = validate_frame_clock(folder, count, fps, pair['sync_basis'])
    sync.update(bio_offset_seconds=pair['bio_offset_seconds'], source_sha256=pair['source_sha256'])
    monitor = BioSignalMonitor(Config(), model_path=str(resolve(config['models']['ecg'])),
        ppg_encoder_path=str(resolve(config['models']['ppg_encoder'])), ppg_head_path=str(resolve(config['models']['ppg_head'])))
    bio = BiosignalReplay(folder, monitor, offset_seconds=pair['bio_offset_seconds'])
    recorded_vitals = RecordedVitals(folder/'vitals.csv')
    capture = cv2.VideoCapture(str(vp))
    if not capture.isOpened() or abs(capture.get(cv2.CAP_PROP_FPS) - fps) > .01 or round(capture.get(cv2.CAP_PROP_FRAME_COUNT)) != count:
        raise ValueError('Annotated vision video does not preserve source timing')
    probe = TCPProbe(target) if verify_tcp else None
    publisher = AnalysisPublisher(probe.server if probe else None)
    writer = None
    idx, cursor = 0, 0
    state_counts, bio_counts, grade_sources = Counter(), Counter(), Counter()
    first_states, packets, model_history = {}, [], []
    preview_written = False
    summary = {}
    try:
        with (target / 'fusion.jsonl').open('w') as log, (target / 'unity_packets.jsonl').open('w') as packet_log, (target/'display_bpm.jsonl').open('w') as display_log:
            while True:
                ok, image = capture.read()
                if not ok:
                    break
                t = idx / fps
                while cursor + 1 < len(records) and records[cursor + 1]['timestamp'] <= t + 1e-6:
                    cursor += 1
                vision = as_decision(records[cursor])
                status = bio.advance(t)
                combined = combine(vision, status, sync, t)
                revision = publisher.publish(combined, pair['id'])
                if probe and not probe.server.wait_until_sent(revision, timeout=2):
                    raise RuntimeError('Current fusion publish was not transmitted')
                packet = vehicle_control_packet(combined, idx + 1, clip_id=pair['id'])
                packet_log.write(json.dumps(packet, allow_nan=False) + '\n')
                log.write(json.dumps(dict(timestamp=t, vision=asdict(vision), bio=status, fusion=asdict(combined)), allow_nan=False) + '\n')
                state_counts[combined.state.value] += 1
                bio_counts[status['level']] += 1
                grade_sources[status.get('hr_class_source', 'collecting')] += 1
                first_states.setdefault(combined.state.value, t)
                recorded = recorded_vitals.at(status['timeline_seconds'])
                display_log.write(json.dumps(dict(timestamp=t,**select_display_bpm(status,recorded)),allow_nan=False)+'\n')
                annotated = draw_multimodal(image, combined, vision, status, bio.waveform(t, seconds=8.), sync, recorded_vitals=recorded)
                if writer is None:
                    height, width = annotated.shape[:2]
                    writer = cv2.VideoWriter(str(target / 'preview_intermediate.mp4'), cv2.VideoWriter_fourcc(*'mp4v'), fps, (width, height))
                    if not writer.isOpened():
                        raise RuntimeError('Cannot create video')
                writer.write(annotated)
                if not preview_written and t >= min(12.5, count / fps - .2):
                    cv2.imwrite(str(target / 'preview.jpg'), annotated)
                    preview_written = True
                idx += 1
        if idx != count:
            raise RuntimeError(f'Unexpected video EOF: {idx}/{count}')
        if probe:
            expected = set()
            for line in (target / 'fusion.jsonl').read_text().splitlines():
                r = json.loads(line);expected.add((round(r['timestamp'], 5), r['fusion']['state']))
            seen = {(round(p['source_timestamp'], 5), p['fusion_state']) for p in probe.packets if p.get('pipeline') == 'multimodal_replay'}
            missing = expected - seen
            if missing:
                raise RuntimeError(f'TCP client missed {len(missing)} current results')
            (target / 'received_packets.jsonl').write_text(''.join(json.dumps(p) + '\n' for p in probe.packets))
        summary = dict(id=pair['id'], frames=idx, fps=fps, duration_seconds=idx / fps,
            fused_state_frames=dict(state_counts), bio_state_frames=dict(bio_counts), first_state_seconds=first_states,
            grade_sources=dict(grade_sources), vision_analysis=provenance, vision_records_sha256=digest(rp),
            source_sha256=pair['source_sha256'], synthetic_biosignal=bio.synthetic, synchronization=sync,
            raw_biosignals_sha256=digest(folder / 'biosignals.csv'),
            supplied_status_log_used=False, labels_used_for_decisions=False,
            tcp_mock_verified=bool(probe), current_fusion_published=publisher.published_count,
            received_packets=len(probe.packets) if probe else 0, unity_editor_verified=False,
            bio_model_updates=len(bio.history), models={k:dict(path=v, sha256=digest(resolve(v))) for k,v in config['models'].items()})
        (target / 'bio_recomputed.jsonl').write_text(''.join(json.dumps(s, allow_nan=False) + '\n' for s in bio.history))
    finally:
        capture.release()
        if writer:
            writer.release()
        bio.close()
        publisher.finish()
        if probe:
            probe.close()
    import imageio_ffmpeg
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-y', '-v', 'error', '-i', str(target / 'preview_intermediate.mp4'),
        '-c:v', 'libx264', '-crf', '19', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(target / 'preview.mp4')], check=True)
    (target / 'preview_intermediate.mp4').unlink()
    check = cv2.VideoCapture(str(target / 'preview.mp4'))
    summary['output_frames'] = round(check.get(cv2.CAP_PROP_FRAME_COUNT))
    summary['output_fps'] = check.get(cv2.CAP_PROP_FPS)
    summary['output_size'] = [round(check.get(3)), round(check.get(4))]
    check.release()
    if summary['output_frames'] != count or abs(summary['output_fps'] - fps) > .01:
        raise RuntimeError('Browser encoding changed timing')
    (target / 'summary.json').write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


def make_index(output, summaries):
    cards = []
    for s in summaries:
        name = html.escape(s['id'])
        label = '합성 생체신호 시험' if s['synthetic_biosignal'] else '동시 촬영한 정상 기록'
        cards.append(f'<article><h2>{name} <small>{label}</small></h2><video controls preload="metadata" poster="{name}/preview.jpg" src="{name}/preview.mp4"></video>'
            f'<p><a href="{name}/preview.mp4" download>영상 다운로드</a><a href="{name}/fusion.jsonl" download>전체 판단 기록</a><a href="{name}/summary.json">실행 결과</a></p></article>')
    (output / 'index.html').write_text('''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Vision + ECG/PPG</title>
<style>body{margin:0;background:#0b131e;color:#eaf3ff;font:16px system-ui}main{max-width:1500px;margin:40px auto;padding:0 28px}h1{font-size:30px}p{line-height:1.8;color:#adbed3}article{margin:32px 0 50px;padding:22px;background:#142131;border:1px solid #293b50;border-radius:18px}h2{margin:0 0 18px}small{font-size:14px;color:#aabed6;margin-left:14px}video{width:100%;display:block;background:#000;border-radius:10px}a{color:#77d6ed;margin-right:28px}</style>
<main><h1>Vision + ECG/PPG</h1><p>영상 속 행동과 센서 파형을 같은 재생 시간에 맞춘 통합 시험입니다. 위에는 통합 상태, 오른쪽에는 기존 비전 판단과 지속 시간, 아래에는 ECG와 PPG 분석 결과를 표시합니다.<br>
N은 센서와 함께 촬영한 별도 정상 기록입니다. D3, E4, The new A는 정상 측정값을 바탕으로 만든 합성 신호를 영상 시간에 배치했습니다. 실제 응급 상태에서 측정한 자료가 아닙니다.<br>생체 분석은 전달받은 실행 파일의 기본 가중치(_ft)를 사용합니다. 비전과 생체 상태를 합치는 판단은 별도로 작성한 시험용 규칙입니다.<br>
초록색은 정상, 노란색은 주의, 빨간색은 시뮬레이터 정차 요청, 회색은 판단 불가입니다. 파란색 Measuring은 초기 측정 준비를 뜻하며, 실제 신호 불량과 구분해서 표시합니다. 생체신호만 정상이면 비전 경고가 해제되는 구조가 아닙니다. Unity 실행과 차량 정차 동작은 검증하지 않았습니다.</p>''' + ''.join(cards) + '</main></html>')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pairs', type=Path, default=ROOT / 'config/multimodal/replay_pairs.json')
    ap.add_argument('--output', type=Path, default=ROOT / 'outputs/multimodal_bpm')
    ap.add_argument('--only', nargs='+')
    ap.add_argument('--verify-tcp', action='store_true', help='Local mock receiver, not Unity')
    ap.add_argument('--fresh-vision', action='store_true', help='Rerun YOLO and MediaPipe rather than reuse source-bound records')
    args = ap.parse_args()
    config = json.loads(args.pairs.read_text())
    pairs = [p for p in config['pairs'] if not args.only or p['id'] in args.only]
    if not pairs:
        ap.error('No matching pairs')
    output = args.output.resolve()
    for pair in pairs:
        folder = resolve(pair['biosignal_folder']).resolve()
        if output == folder or folder in output.parents or output == resolve(pair['video']).parent:
            ap.error('Output must be separate from input data')
    output.mkdir(parents=True, exist_ok=True)
    torch.set_num_threads(2)
    (output / 'biosignal_config_used.json').write_text(json.dumps(asdict(Config()), indent=2))
    summaries = [process(p, config, output, args.verify_tcp, args.fresh_vision) for p in pairs]
    (output / 'summary.json').write_text(json.dumps(summaries, indent=2))
    make_index(output, summaries)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
