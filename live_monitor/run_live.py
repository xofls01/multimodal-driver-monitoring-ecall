"""실시간 운전자 모니터: Pi 카메라 + ECG/PPG → 원본 비전·생체 모델 → Unity(5002) → 정차 → e-Call.

    # 터미널 1 (모델용 Python, 저장소 루트에서). MediaPipe 는 한글 경로에서 동작하지 않으므로
    # 가상환경은 영문 경로(C:\\Users\\<이름>\\.venvs\\driver-live)에 둔다.
    %USERPROFILE%\\.venvs\\driver-live\\Scripts\\python -B live_monitor\\run_live.py --pi-host 192.168.0.64
    # 터미널 2
    py -B ecall_mqtt\\run_vehicle_with_ecall.py --ecall-broker 192.168.0.64 --model-port 5002
    # 그다음 Unity Play

Pi 없이 시험(녹화 폴더를 실시간처럼):
    %USERPROFILE%\\.venvs\\driver-live\\Scripts\\python -B live_monitor\\run_live.py --folder <녹화폴더> --video <영상> [--crop-height 720]

통합본(driver-state-monitoring-system) 코드는 수정하지 않고 불러다 쓴다.
- 비전: CurrentVisionSource(VisionModels)  - 생체: 원본 BioSignalMonitor (입력만 live_bio.LiveBiosignalSource)
- 융합·출력: MultimodalProcessor, UnityStateServer  - 화면: draw_overlay + BPM 숫자만(live_ui)
기록: --output 폴더에 video.avi, frames.csv, biosignals.csv(ECG·PPG 원본), session.json, unity_packets.jsonl.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
MODEL_ROOT = REPO / 'driver-state-monitoring-system'
for p in (str(HERE), str(MODEL_ROOT / 'src')):
    if p not in sys.path:
        sys.path.insert(0, p)
# ultralytics·matplotlib 캐시. ultralytics 는 한글 경로에 쓰지 못하면 작업 폴더에 Ultralytics/ 를 만든다
# → 영문 경로(사용자 LOCALAPPDATA)에 둔다.
_cache = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'driver-live-cache'
os.environ.setdefault('YOLO_CONFIG_DIR', str(_cache / 'ultralytics'))
os.environ.setdefault('MPLCONFIGDIR', str(_cache / 'matplotlib'))
for _d in (os.environ['YOLO_CONFIG_DIR'], os.environ['MPLCONFIGDIR']):
    Path(_d).mkdir(parents=True, exist_ok=True)     # 없으면 ultralytics 가 '쓸 수 없음'으로 보고 작업 폴더를 쓴다

CLIP_ID = 'LIVE'


class LatestFrame:
    """수신 스레드는 계속 넣고, 처리 스레드는 가장 최근 프레임만 가져간다 (느리면 중간 프레임은 건너뜀)."""

    def __init__(self):
        self._cond = threading.Condition()
        self._item = None
        self.closed = False
        self.skipped = 0

    def put(self, t, frame):
        with self._cond:
            if self._item is not None:
                self.skipped += 1
            self._item = (t, frame)
            self._cond.notify()

    def close(self):
        with self._cond:
            self.closed = True
            self._cond.notify_all()

    def take(self, timeout=0.5):
        with self._cond:
            if self._item is None and not self.closed:
                self._cond.wait(timeout)
            item, self._item = self._item, None
            return item


class Recorder:
    """원본 입력을 그대로 남긴다: 카메라 전 프레임, ECG·PPG 전 샘플, 그리고 모델 출력.

    Pi 카메라 프레임은 받은 JPEG 를 다시 인코딩하지 않고 video.mjpeg 에 이어 붙인다
    (프레임 시각은 frames.csv, 바이트 위치는 frames.csv 의 offset/size).
    녹화 폴더를 재생할 때는 입력 영상이 이미 있으므로 영상은 다시 저장하지 않는다.
    """

    def __init__(self, folder: Path, meta: dict):
        self.folder, self.meta = folder, meta
        folder.mkdir(parents=True, exist_ok=False)
        self._lock = threading.Lock()
        self._bio_file = (folder / 'biosignals.csv').open('w', newline='', encoding='utf-8')
        self._frame_file = (folder / 'frames.csv').open('w', newline='', encoding='utf-8')
        self.bio_csv, self.frame_csv = csv.writer(self._bio_file), csv.writer(self._frame_file)
        self.bio_csv.writerow(['index', 'clip_s', 'ecg_raw', 'ppg_raw'])
        self.frame_csv.writerow(['frame_index', 'clip_s', 'mjpeg_offset', 'mjpeg_size'])
        self.packets = (folder / 'unity_packets.jsonl').open('w', encoding='utf-8')
        self.video = None
        self._video_bytes = 0
        self.counts = dict(bio=0, frame=0, processed=0)
        self.last_t = 0.0                                   # 가장 최근 받은 카메라 프레임 시각
        self.save_meta('recording')

    def save_meta(self, status):
        self.meta.update(status=status, counts=dict(self.counts),
                         updated_utc=datetime.now(timezone.utc).isoformat())
        (self.folder / 'session.json').write_text(json.dumps(self.meta, indent=2, ensure_ascii=False), encoding='utf-8')

    def bio(self, t, ecg, ppg):
        with self._lock:
            self.bio_csv.writerow([self.counts['bio'], f'{t:.6f}', int(ecg), int(ppg)])
            self.counts['bio'] += 1

    def frame(self, t, data):
        with self._lock:
            offset = size = ''
            if isinstance(data, (bytes, bytearray)):
                if self.video is None:
                    self.video = (self.folder / 'video.mjpeg').open('wb')
                offset, size = self._video_bytes, len(data)
                self.video.write(data)
                self._video_bytes += size
            self.frame_csv.writerow([self.counts['frame'], f'{t:.6f}', offset, size])
            self.counts['frame'] += 1
            self.last_t = t

    def packet(self, packet: dict):
        self.packets.write(json.dumps(packet, allow_nan=False) + '\n')
        self.counts['processed'] += 1

    def close(self, status):
        with self._lock:
            for f in (self.video, self._bio_file, self._frame_file, self.packets):
                if f is None:
                    continue
                f.close()
        self.save_meta(status)


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_argument_group('입력 (둘 중 하나)')
    src.add_argument('--pi-host', help='카메라·아두이노가 연결된 Pi 주소 (예: 192.168.0.64)')
    src.add_argument('--folder', type=Path, help='Pi 대신 녹화 폴더(biosignals.csv, frames.csv)를 실시간처럼 재생')
    pi = ap.add_argument_group('Pi')
    pi.add_argument('--pi-user', default='ecall')
    pi.add_argument('--serial', default='auto', help='Pi 의 아두이노 포트 (auto: /dev/ttyUSB* → /dev/ttyACM*)')
    pi.add_argument('--camera', default='/dev/video0')
    pi.add_argument('--width', type=int, default=1280)
    pi.add_argument('--height', type=int, default=720)
    pi.add_argument('--fps', type=int, default=15,
                    help='카메라 fps. 판단은 최신 프레임만 쓰므로(초당 약 5~9장) 15면 충분')
    fo = ap.add_argument_group('녹화 폴더 재생')
    fo.add_argument('--video', type=Path, help='--folder 와 짝인 영상')
    fo.add_argument('--crop-height', type=int, help='영상 위쪽만 사용 (예: video_overlay.mp4 → 720)')
    fo.add_argument('--speed', type=float, default=1.0)
    ap.add_argument('--unity-host', default='127.0.0.1')
    ap.add_argument('--unity-port', type=int, default=5002, help='run_vehicle_with_ecall.py --model-port 와 같아야 함')
    ap.add_argument('--neutral-window', type=float, nargs=2, default=(0.5, 5.0), metavar=('START', 'END'),
                    help='시작 후 정면을 보는 보정 구간(초). 이 동안 운전 자세로 앞을 보세요 '
                         '(비전 보정에 최소 8장 필요: 초당 판단 수 × 길이 ≥ 8)')
    ap.add_argument('--max-gap', type=float, default=0.6,
                    help='관찰 간격 허용(초). 원본 0.25 는 녹화 재생(초당 15장) 기준이라, CPU 실시간(가끔 0.3~0.5초 걸리는 '
                         '프레임)에서는 지속 시간 타이머가 계속 리셋된다. 판정 기준(지속 초)은 바꾸지 않는다')
    ap.add_argument('--phone-fps', type=float, default=1.0,
                    help='휴대폰 검출 빈도(초당, 원본 5). 시간 기준이라 CPU 실시간에서는 매 프레임 960px 검출(약 0.4초)이 '
                         '돌아 더 느려진다. 결과는 원본대로 0.8초 유지. 0 이면 원본 그대로')
    ap.add_argument('--handheld-fps', type=float, default=2.0,
                    help='손에 든 물건 검출 빈도(초당, 원본 3). 0 이면 원본 그대로')
    ap.add_argument('--no-response-stop', type=float, default=3.0,
                    help='실시간 전용: 눈 감김 경고가 Unity 로 전달된 뒤 이 시간(초) 동안 눈을 뜨지 않으면 Danger(정차). '
                         '원본은 눈 감김만으로 정차하지 않는다. 0 이면 원본 그대로')
    ap.add_argument('--media-outbox', type=Path, default=REPO / 'ecall_mqtt' / 'media_outbox',
                    help='주의·위험 첫 장면 사진을 사건별로 저장하는 폴더. 차량 서버(e-Call)가 정차 후 Pi 로 올려 MSD 에 링크를 넣는다')
    ap.add_argument('--auto-recalibrate', type=float, default=2.0,
                    help='자세 기준이 없을 때, 정상 상태로 얼굴·어깨·엉덩이가 이 시간(초) 이상 보이면 자동으로 다시 보정. 0 이면 끔')
    ap.add_argument('--warmup-seconds', type=float, default=None,
                    help='Pi: 스트림 처음 이 시간 동안 모델만 돌려 초기화(판단·송신 없음). 기본 8초, 녹화 폴더는 0')
    ap.add_argument('--warmup-frames', type=int, default=60,
                    help='녹화 폴더: 스트림 전에 영상 앞부분 이 장수로 모델 초기화')
    ap.add_argument('--bio-startup', type=Path, default=MODEL_ROOT / 'config/multimodal/bio_startup_fast5.json')
    ap.add_argument('--output', type=Path, help='기록 폴더 (기본: live_monitor/sessions/LIVE_날짜시각)')
    ap.add_argument('--no-show', action='store_true', help='화면 없이 실행')
    ap.add_argument('--max-seconds', type=float, help='이 시간 뒤 자동 종료 (시험용)')
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if bool(args.pi_host) == bool(args.folder):
        build_parser().error('--pi-host 또는 --folder 중 하나만 지정하세요')
    if args.folder and not args.video:
        build_parser().error('--folder 에는 --video 가 필요합니다')
    a, b = args.neutral_window
    if not 0 <= a < b <= 10:
        build_parser().error('--neutral-window 는 0 <= 시작 < 끝 <= 10')

    import cv2
    import torch
    from biosignal import BioSignalMonitor
    from driver_state_tools.analysis_publisher import AnalysisPublisher
    from driver_state_tools.biosignal_startup import load_startup_profile
    from driver_state_tools.multimodal_runtime import MultimodalProcessor
    from driver_state_tools.rppg import RPPGResult
    from driver_state_tools.unity_bridge import UnityStateServer, vehicle_control_packet
    from driver_state_tools.vision_pipeline import VisionModels, draw_overlay
    from live_bio import LiveBiosignalSource
    from live_rules import EyeNoResponseRule, LiveVisionSource
    from live_ui import draw_banner, draw_live
    from streams import FolderStream, PiStream

    class LivePublisher(AnalysisPublisher):
        """원본 Processor 는 입력 방식을 'paired_files' 로 적는다 → 실시간임을 표시만 바꿔서 보낸다."""
        def publish(self, decision, clip_id):
            meta = decision.metadata
            meta.update(input_mode='live_pi_stream', analysis_origin='live_camera_and_ecg_ppg_inference')
            return super().publish(decision, clip_id)

    config = json.loads((MODEL_ROOT / 'config/prototype/vision_baseline.json').read_text())
    rules = json.loads((MODEL_ROOT / 'config/prototype/decision_rules.json').read_text())
    config['decision_rules'] = rules
    # 실시간 전용 설정 (파일은 바꾸지 않고 이 실행에서만). 기록에 남긴다.
    live_overrides = {}
    if args.max_gap is not None and args.max_gap != rules['max_observation_gap_seconds']:
        live_overrides['max_observation_gap_seconds'] = dict(original=rules['max_observation_gap_seconds'],
                                                             live=args.max_gap)
        rules['max_observation_gap_seconds'] = args.max_gap
        config['max_observation_gap_seconds'] = args.max_gap
    for key, value, default in (('phone_detection_fps', args.phone_fps, config['analysis_fps']),
                                ('handheld_detection_fps', args.handheld_fps, 3.0)):
        if value:
            live_overrides[key] = dict(original=config.get(key, default), live=value)
            config[key] = value
    if args.no_response_stop > 0:
        live_overrides['eye_closure_no_response_stop_seconds'] = dict(original='disabled', live=args.no_response_stop)
    # 원본은 보정 표본을 3초까지만 모은다(calibration_max_seconds). 실시간은 초당 판단 수가 적으므로
    # 보정 구간(--neutral-window) 끝까지 모으게 한다.
    if b > float(config['calibration_max_seconds']):
        live_overrides['calibration_max_seconds'] = dict(original=config['calibration_max_seconds'], live=b)
        config['calibration_max_seconds'] = b
    pairs = json.loads((MODEL_ROOT / 'config/multimodal/system_pairs.json').read_text())
    model_paths = {k: MODEL_ROOT / v for k, v in pairs['models'].items()}
    for path in [*model_paths.values(), MODEL_ROOT / config['pose_model'], MODEL_ROOT / config['detect_model']]:
        if not Path(path).is_file():
            print(f'[LIVE] 모델 파일이 없습니다: {path}', file=sys.stderr)
            return 2

    out = args.output or HERE / 'sessions' / datetime.now().strftime('LIVE_%Y%m%d_%H%M%S')
    profile = dict(schema='reviewed_neutral_window_v1', neutral_window=[a, b], reviewed=True,
                   provenance='live run: operator instructs the driver to look ahead during this window')
    bio_cfg, startup = load_startup_profile(args.bio_startup)
    sync = dict(basis='live_pi_clock', camera_timestamps=None, video_frames=None,
                acquisition_delay_calibrated=False, bio_offset_seconds=0.0, source_sha256=None,
                note='Live: camera frames and ECG/PPG stamped on the Pi monotonic clock at read; '
                     'USB/camera buffering delay uncalibrated')
    meta = dict(kind='live_session', clip_id=CLIP_ID,
                source=dict(pi_host=args.pi_host, camera=args.camera, serial=args.serial,
                            requested=[args.width, args.height, args.fps]) if args.pi_host
                else dict(folder=str(args.folder), video=str(args.video), crop_height=args.crop_height),
                neutral_window=[a, b], biosignal_startup=startup, unity=f'{args.unity_host}:{args.unity_port}',
                live_overrides=live_overrides,
                timing='clip_s = Pi monotonic seconds since stream start (both camera and ECG/PPG)',
                note='Research prototype. Not diagnostic.', started_utc=datetime.now(timezone.utc).isoformat())
    recorder = Recorder(out, meta)

    torch.set_num_threads(2)
    print('[LIVE] 모델 불러오는 중...', flush=True)
    models = VisionModels(config, MODEL_ROOT)
    import numpy as np
    # 첫 추론들은 느리다(YOLO 가 새 입력 크기마다 초기화). 실제 사람 영상으로 미리 돌려야 판단 초반에
    # 보정(초당 판단 수 × 보정 구간 ≥ 8장)과 관찰 간격(max-gap)을 지킬 수 있다.
    # 녹화 폴더: 스트림 전에 영상 앞부분으로 / Pi: 스트림 첫 warmup 초 동안 카메라 영상으로.
    if args.folder:
        cap = cv2.VideoCapture(str(args.video))
        for i in range(args.warmup_frames):
            ok, f = cap.read()
            if not ok:
                break
            models.extract(f[:args.crop_height] if args.crop_height else f, i / 30, collect_rppg=False)
        cap.release()
    warmup_s = args.warmup_seconds if args.warmup_seconds is not None else (0.0 if args.folder else 8.0)

    def new_bio():
        monitor = BioSignalMonitor(bio_cfg, model_path=str(model_paths['ecg']),
                                   ppg_encoder_path=str(model_paths['ppg_encoder']),
                                   ppg_head_path=str(model_paths['ppg_head']))
        return LiveBiosignalSource(monitor, startup_settings=dict(
            calibration_s=bio_cfg.calibration_s, min_ready_s=bio_cfg.min_ready_s,
            calibration_min_readings=bio_cfg.calibration_min_readings))

    server = UnityStateServer(args.unity_host, args.unity_port, log_path=out / 'unity_transport.jsonl').start()
    publisher = LivePublisher(server)
    server.mark_unavailable('new_video_session')
    # 판단 세션: 워밍업이 끝날 때, 또는 화면에서 'c'(다시 보정)를 누를 때 새로 만든다
    live = dict(bio=None, processor=None, vision=None, pose_frames=0, frames=0, posture_ok=None, warned=False)
    bio_history: list = []
    auto_recal = dict(last=-1e9, count=0)
    snaps = dict(caution=None, saved=set())     # 주의 첫 장면 (판단 시각, JPEG), 사진을 저장한 Danger 사건

    def save_snapshot(event_id, kind, data):
        folder = Path(args.media_outbox) / str(event_id)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f'{kind}.jpg').write_bytes(data)
        (out / 'evidence').mkdir(exist_ok=True)
        (out / 'evidence' / f'{event_id}_{kind}.jpg').write_bytes(data)
    print(f'[LIVE] 판단 결과 송신: {args.unity_host}:{server.port} (차량 서버 --model-port 로 연결)', flush=True)
    for key, v in live_overrides.items():
        print(f'[LIVE] 실시간 설정: {key} {v["original"]} → {v["live"]}', flush=True)

    latest = LatestFrame()
    ended = dict(error=None, done=False)
    clock = dict(t0=None)          # 판단 시작 시각(스트림 시계). 판단·생체 모델에는 t - t0 를 준다

    def on_bio(t, e, p):
        recorder.bio(t, e, p)      # 원본은 워밍업 중에도 모두 기록
        t0, bio = clock['t0'], live['bio']
        if t0 is not None and bio is not None and t >= t0:
            bio.feed(t - t0, e, p)

    def start_judging(t_stream, reason):
        """새 판단 세션: 비전 보정·생체 측정 준비를 처음부터. 판단 시각은 t - t0."""
        old = live['bio']
        live['bio'] = None
        if old is not None:
            bio_history.extend(old.history)
            old.close()
        rule = EyeNoResponseRule(args.no_response_stop, warning_delivered=server.connected.is_set)
        live.update(vision=LiveVisionSource(models, config, rules, profile, no_response_rule=rule), bio=new_bio(),
                    pose_frames=0, frames=0, posture_ok=None, warned=False, visible_since=None)
        live['processor'] = MultimodalProcessor(live['vision'], live['bio'], sync, CLIP_ID,
                                                vision_every_frames=1, publisher=publisher)
        clock['t0'] = t_stream
        recorder.meta.setdefault('judge_starts_clip_s', []).append(dict(clip_s=t_stream, reason=reason))
        print(f'[LIVE] 판단 시작 ({reason}, 스트림 {t_stream:.1f}s). 지금부터 {a:g}~{b:g}초 동안 운전 자세로 정면을 보세요 '
              f'(어깨·엉덩이가 카메라에 보여야 함). 다시 보정: 화면에서 c / 종료: q', flush=True)

    def on_frame(t, frame):
        recorder.frame(t, frame)
        latest.put(t, frame)

    def on_end(error):
        ended.update(error=error, done=True)
        latest.close()

    source = (PiStream(args.pi_host, args.pi_user, port=args.serial, camera=args.camera,
                       width=args.width, height=args.height, fps=args.fps)
              if args.pi_host else FolderStream(args.folder, args.video, speed=args.speed,
                                                crop_height=args.crop_height))
    title = 'Live driver monitor | q: exit'
    status = 'stopped'
    try:
        source.start(on_bio, on_frame, on_end)
        print(f'[LIVE] 기록 폴더: {out}', flush=True)
        if warmup_s > 0:
            print(f'[LIVE] 워밍업 {warmup_s:g}초 (모델 초기화). 그다음 판단 시작 후 {a:g}~{b:g}초 동안 정면을 보세요.',
                  flush=True)
        if not args.no_show:
            cv2.namedWindow(title, cv2.WINDOW_NORMAL)
            cv2.resizeWindow(title, 1100, 720)
        last_t, started, rate = None, time.monotonic(), None
        first_t = None
        prev_state = None
        stats = dict(n=0, model=0.0, total=0.0, lag=0.0, since=time.monotonic())
        while True:
            item = latest.take()
            if item is None:
                if ended['done']:
                    break
                if args.max_seconds and time.monotonic() - started > args.max_seconds:
                    break
                if not args.no_show and cv2.waitKey(1) & 0xFF == ord('q'):
                    break
                continue
            t_stream, frame = item
            tic = time.monotonic()
            if isinstance(frame, (bytes, bytearray)):              # Pi JPEG: 처리할 프레임만 디코딩
                frame = cv2.imdecode(np.frombuffer(frame, dtype=np.uint8), cv2.IMREAD_COLOR)
                if frame is None:
                    continue
            if first_t is None:
                first_t = t_stream
            if clock['t0'] is None:
                if t_stream - first_t < warmup_s:                   # 워밍업: 추론만, 판단·송신 없음
                    models.extract(frame, t_stream, collect_rppg=False)
                    if not args.no_show:
                        cv2.imshow(title, draw_banner(frame, 'WARMING UP', f'{warmup_s - (t_stream - first_t):.0f}s'))
                        if cv2.waitKey(1) & 0xFF == ord('q'):
                            break
                    continue
                start_judging(t_stream, 'warmup_done')
                last_t = None
            t = t_stream - clock['t0']
            if last_t is not None and t <= last_t:
                continue
            step = live['processor'].update(frame, t)
            last_t = t
            v, d = step.vision, step.decision
            # 보정 확인: 자세(어깨+엉덩이)가 보여야 쓰러짐을 판단할 수 있다
            if t <= b:
                live['frames'] += 1
                live['pose_frames'] += bool(v.raw.pose_readable)
            elif live['posture_ok'] is None:
                prof = live['vision'].session.tracker.profile
                live['posture_ok'] = prof.neutral_torso is not None and prof.neutral_head_drop is not None
                recorder.meta.setdefault('calibrations', []).append(dict(
                    clip_s=clock['t0'], frames=live['frames'], pose_readable_frames=live['pose_frames'],
                    posture_baseline=live['posture_ok'], eye_baseline=prof.neutral_ear is not None))
                if live['posture_ok']:
                    print(f'[LIVE] 보정 완료: 자세 기준 OK (몸 인식 {live["pose_frames"]}/{live["frames"]}장)', flush=True)
                else:
                    print(f'[LIVE] ⚠ 자세 기준 없음: 보정 중 어깨·엉덩이가 보인 장면 {live["pose_frames"]}/{live["frames"]}장. '
                          f'이 상태로는 쓰러짐을 판단할 수 없습니다. 엉덩이까지 보이게 고쳐 앉으면 자동으로 다시 보정합니다'
                          f'(또는 화면에서 c).', flush=True)
            # 자동 재보정: 자세 기준이 없을 때, 정상 상태로 얼굴과 몸(어깨+엉덩이)이 auto_recal 초 이상 이어서
            # 보이면 그 자리에서 다시 보정한다 (시작 때 엉덩이가 가려졌다가 고쳐 앉은 경우).
            if live['posture_ok'] is False and args.auto_recalibrate > 0:
                good = bool(v.raw.pose_readable and v.raw.face_readable) and d.state.value == 'normal'
                live['visible_since'] = (live.get('visible_since') or t) if good else None
                if (good and t - live['visible_since'] >= args.auto_recalibrate
                        and t_stream - auto_recal['last'] >= 10.0):
                    auto_recal['last'] = t_stream
                    auto_recal['count'] += 1
                    print('[LIVE] 몸이 잘 보여서 자동으로 다시 보정합니다. 지금 운전 자세로 정면을 보세요.', flush=True)
                    server.mark_unavailable('recalibrating')
                    start_judging(t_stream, 'auto_recalibrate')
                    last_t = None
                    prev_state = None
                    continue
            if t <= b:
                calib_note = (f'CALIBRATING {b - t:.0f}s - look ahead | body '
                              + ('OK' if v.raw.pose_readable else 'NOT VISIBLE (show hips)'))
            elif live['posture_ok'] is False:
                calib_note = ('NO POSTURE BASELINE - sit back so hips show (auto recalibrates) or press c'
                              if args.auto_recalibrate > 0 else 'NO POSTURE BASELINE - show shoulders+hips, press c')
            else:
                calib_note = None
            packet = vehicle_control_packet(d, recorder.counts['processed'] + 1, clip_id=CLIP_ID)
            recorder.packet(packet)
            state = d.state.value
            # 사진: 주의가 시작된 장면을 기억해 두고, Danger 사건이 생기면 그 사건 id 로 주의·위험 사진을 저장
            if state == 'caution' and prev_state != 'caution':
                ok_jpg, enc = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                if ok_jpg:
                    snaps['caution'] = (t, enc.tobytes())
            danger_id = ((d.metadata.get('monitoring_output') or {}).get('danger') or {}).get('event_id')
            if state == 'danger' and danger_id and danger_id not in snaps['saved']:
                snaps['saved'].add(danger_id)
                ok_jpg, enc = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                if ok_jpg:
                    save_snapshot(danger_id, 'danger', enc.tobytes())
                if snaps['caution'] is not None and t - snaps['caution'][0] <= 120:
                    save_snapshot(danger_id, 'caution', snaps['caution'][1])
                print(f'[LIVE] 사진 저장: 사건 {danger_id} (위험' + (', 주의' if snaps['caution'] else '') + ')', flush=True)
            if state != prev_state:
                print(f'[LIVE] {t:7.2f}s 상태 {prev_state} → {state}  사유={list(d.reasons)[:4]}', flush=True)
                prev_state = state
            model_s = time.monotonic() - tic
            if not args.no_show:
                pulse = RPPGResult(timestamp=t, status='disabled', source='external_biosignals', note='Not used')
                vision_image = draw_overlay(frame, v.raw, v.derived, v.decision, pulse,
                                            int(config['dashboard_width']), config)
                notice = calib_note or (None if server.connected.is_set() else 'Vehicle server not connected (5002)')
                cv2.imshow(title, draw_live(vision_image, d, v.decision, step.bio, elapsed=t, fps=rate, notice=notice))
                key = cv2.waitKey(1) & 0xFF
                if key == ord('q'):
                    break
                if key == ord('c'):                                  # 다시 보정 (카메라·자세 조정 후)
                    server.mark_unavailable('recalibrating')
                    start_judging(t_stream, 'recalibrate_key')
                    last_t = None
                    prev_state = None
                    continue
            took = time.monotonic() - tic
            rate = 1 / took if rate is None else 0.8 * rate + 0.2 / max(took, 1e-3)
            stats['n'] += 1
            stats['model'] += model_s
            stats['total'] += took
            stats['lag'] = max(stats['lag'], recorder.last_t - t_stream)
            if time.monotonic() - stats['since'] >= 5:
                n = max(stats['n'], 1)
                print(f'[LIVE] 처리 {n / (time.monotonic() - stats["since"]):4.1f} fps  모델 {1000 * stats["model"] / n:4.0f} ms  '
                      f'화면 {1000 * (stats["total"] - stats["model"]) / n:4.0f} ms  최대지연 {stats["lag"]:.2f}s  '
                      f'BPM입력 {live["bio"].samples_received}', flush=True)
                stats.update(n=0, model=0.0, total=0.0, lag=0.0, since=time.monotonic())
            if args.max_seconds and time.monotonic() - started > args.max_seconds:
                break
        status = 'error' if ended['error'] else 'stopped'
        if ended['error']:
            print(f'[LIVE] 입력 오류: {ended["error"]}', file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        status = 'stopped'
    finally:
        source.stop()
        latest.close()
        publisher.finish('live_stopped')
        server.stop()
        models.close()
        if live['bio'] is not None:
            bio_history.extend(live['bio'].history)
            live['bio'].close()
        recorder.meta.update(skipped_frames=latest.skipped, auto_recalibrations=auto_recal['count'],
                             bio_updates=len(bio_history), error=str(ended['error']) if ended['error'] else None)
        recorder.close(status)
        (out / 'bio_status.jsonl').write_text(''.join(json.dumps(s, allow_nan=False) + '\n' for s in bio_history))
        if not args.no_show:
            cv2.destroyAllWindows()
        print(f'[LIVE] 종료 ({status}). 카메라 {recorder.counts["frame"]}프레임, ECG/PPG {recorder.counts["bio"]}샘플, '
              f'판단 {recorder.counts["processed"]}회 → {out}', flush=True)
    return 1 if status == 'error' else 0


if __name__ == '__main__':
    raise SystemExit(main())
