"""라즈베리파이 카메라 + Nano(ECG/PPG) 실시간 화면, 원할 때 15~30초 녹화.

노트북에서 실행하면 라즈베리파이에 SSH 로 접속해 아래 REMOTE 코드를 메모리에서 실행한다
(라즈베리파이에는 파일을 남기지 않음). 라즈베리파이는 카메라 JPEG 와 Nano 시리얼 줄을 노트북으로 보내고,
노트북은 화면에 보여주다가 [녹화 시작] 을 누르면 정한 시간만큼 영상·CSV 를 저장한다.

Nano 펌웨어: firmware/ppg_ecg_nano/ppg_ecg_nano.ino
  ECG:<raw>\tPPG:<raw>  250Hz,  1초마다 PPG_BPM/ECG_BPM/HR_BPM 상태 줄 (심박 계산·융합은 Nano 가 함)

라즈베리파이 없이 시험: py biosignal_camera_live_bpm.py --local --port COM6 --camera 0
"""
import argparse
import base64
import csv
import getpass
import json
import os
import queue
import shlex
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

ADC_REF_MV = 5000  # Nano/Uno default analog reference (USB supply)
MAINS_HZ = 60  # Korea
RECORD_CHOICES = (15, 20, 25, 30)  # 녹화 길이 선택지 (초)

ECG_STATE = {0: '전극 확인', 1: '찾는 중', 2: '측정 중'}
PPG_STATE = {0: '손가락 없음', 1: '찾는 중', 2: '측정 중'}
HR_SRC = {0: '측정 안 됨', 1: 'ECG', 2: 'PPG', 3: 'ECG + PPG', 4: 'ECG (PPG 와 불일치)'}
VITAL_FIELDS = ('ppg_bpm', 'ppg_state', 'ppg_pp', 'ecg_bpm', 'ecg_state', 'hr_bpm', 'hr_src', 'drop')

# Sent as an in-memory command. No script or recordings written on the Pi.
REMOTE = r'''
import sys, time, json, struct, threading, queue, re
import cv2, serial
cfg = json.loads(sys.argv[1])
stop = threading.Event()
packets = queue.Queue(maxsize=1500)
origin = time.monotonic_ns()
raw_pattern = re.compile(r"ECG:(\d+)\s+PPG:(\d+)")
vital_pattern = re.compile(r"PPG_BPM:([\d.]+)\tPPG_STATE:(\d)\tPPG_PP:(-?\d+)\tECG_BPM:([\d.]+)\tECG_STATE:(\d)"
                           r"\tHR_BPM:([\d.]+)\tHR_SRC:(\d)\tDROP:(\d+)")
errors = []

def stamp():
    return dict(pi_monotonic_ns=time.monotonic_ns(), pi_utc_ns=time.time_ns())

def emit(info, payload=b''):
    info['payload_bytes'] = len(payload)
    try:
        packets.put_nowait((info, payload))
    except queue.Full:
        errors.append('Pi transmission queue full: recording stopped; reduce video rate/size.')
        stop.set()

def bio():
    try:
        with serial.Serial(cfg['port'], 115200, timeout=1, exclusive=True) as ser:
            # Opening Nano serial often resets it. Discard startup bytes.
            time.sleep(2)
            ser.reset_input_buffer()
            index = vindex = 0
            last_valid = time.monotonic()
            while not stop.is_set():
                raw = ser.readline(256)
                ts = stamp()
                line = raw.decode('ascii', errors='replace').strip()
                match = raw_pattern.fullmatch(line)
                if match:
                    ecg, ppg = int(match.group(1)), int(match.group(2))
                    if 0 <= ecg <= 1023 and 0 <= ppg <= 1023:
                        emit(dict(type='bio', index=index, ecg=ecg, ppg=ppg, **ts))
                        index += 1
                        last_valid = time.monotonic()
                else:
                    match = vital_pattern.fullmatch(line)
                    if match:
                        g = match.groups()
                        emit(dict(type='vitals', index=vindex, ppg_bpm=float(g[0]), ppg_state=int(g[1]), ppg_pp=int(g[2]),
                                  ecg_bpm=float(g[3]), ecg_state=int(g[4]), hr_bpm=float(g[5]), hr_src=int(g[6]),
                                  drop=int(g[7]), **ts))
                        vindex += 1
                if time.monotonic() - last_valid > 10:
                    raise RuntimeError('No valid ECG/PPG for 10 seconds. Check firmware, wiring and other serial readers.')
    except Exception as exc:
        errors.append('Serial: ' + str(exc))
        stop.set()

def camera():
    cap = None
    try:
        src = cfg['camera']
        src = int(src) if str(src).isdigit() else src
        cap = cv2.VideoCapture(src, cv2.CAP_V4L2) if sys.platform.startswith('linux') else cv2.VideoCapture(src)
        if not cap.isOpened():
            raise RuntimeError('Cannot open camera')
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg['width'])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg['height'])
        cap.set(cv2.CAP_PROP_FPS, cfg['fps'])
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        emit(dict(type='camera_info', width=cap.get(3), height=cap.get(4), fps=cap.get(5)))
        index = 0
        while not stop.is_set():
            ok, frame = cap.read()
            ts = stamp()
            if not ok:
                raise RuntimeError('Camera frame read failed')
            ok, encoded = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if not ok:
                raise RuntimeError('JPEG encoding failed')
            emit(dict(type='frame', index=index, **ts), encoded.tobytes())
            index += 1
    except Exception as exc:
        errors.append('Camera: ' + str(exc))
        stop.set()
    finally:
        if cap is not None:
            cap.release()

def send(info, payload=b''):
    info['payload_bytes'] = len(payload)
    header = json.dumps(info).encode()
    sys.stdout.buffer.write(struct.pack('!I', len(header)) + header + payload)
    sys.stdout.buffer.flush()

def control():
    sys.stdin.buffer.read(1)  # q or SSH EOF
    stop.set()

send(dict(type='hello', origin_ns=origin, timestamp_kind='Pi software read completion'))
threading.Thread(target=control, daemon=True).start()
threads = [threading.Thread(target=bio, daemon=True), threading.Thread(target=camera, daemon=True)]
for thread in threads:
    thread.start()
try:
    while not stop.is_set():
        try:
            info, payload = packets.get(timeout=0.2)
        except queue.Empty:
            continue
        send(info, payload)
    for thread in threads:
        thread.join(timeout=2)
    # Drain already accepted records before ending.
    while not packets.empty():
        info, payload = packets.get_nowait()
        send(info, payload)
    send(dict(type='end', errors=errors))
except (BrokenPipeError, OSError):
    stop.set()
'''

def read_exact(stream, size):
    result = bytearray()
    while len(result) < size:
        data = stream.read(size - len(result))
        if not data:
            raise EOFError('SSH stream ended before a complete packet arrived')
        result.extend(data)
    return bytes(result)


def read_packet(stream):
    size, = struct.unpack('!I', read_exact(stream, 4))
    if not 0 < size <= 65536:
        raise ValueError('Invalid packet header')
    info = json.loads(read_exact(stream, size))
    length = info['payload_bytes']
    if not isinstance(length, int) or not 0 <= length <= 20_000_000:
        raise ValueError('Invalid payload size')
    return info, read_exact(stream, length)



def clean_ecg(y, fs):
    """Display-only cleanup: mains notch (60/120 Hz) + 0.5-40 Hz band-pass. Raw CSV unchanged."""
    import numpy as np
    from scipy.signal import butter, filtfilt, iirnotch, sosfiltfilt
    y = np.asarray(y, dtype=float)
    for f0 in (MAINS_HZ, 2 * MAINS_HZ):
        if f0 < fs / 2:
            b, a = iirnotch(f0, 30, fs)
            y = filtfilt(b, a, y)
    return sosfiltfilt(butter(2, [0.5, 40], btype='band', fs=fs, output='sos'), y)


def clean_ppg(y, fs):
    """Display-only pulse shape: 0.7-4 Hz band-pass. BPM comes from the Nano."""
    import numpy as np
    from scipy.signal import butter, sosfiltfilt
    return sosfiltfilt(butter(2, [0.7, 4], btype='band', fs=fs, output='sos'), np.asarray(y, dtype=float))


def autoscale_wave(ax, values, min_pad):
    """Fit visible samples with padding."""
    low, high = float(min(values)), float(max(values))
    padding = max(min_pad, (high - low) * .12)
    ax.set_ylim(low - padding, high + padding)


VIDEO_FPS = 30      # 저장 영상 fps. 카메라 실제 fps(라즈베리파이 약 14fps)와 상관없이 시각에 맞춰 프레임을 복제/생략
PREROLL_S = 5       # 겹침 영상 파형 창 길이. 녹화 직전 5초도 같이 넣어 처음부터 파형이 차 있게 함
ECG_FS = 250        # Nano ECG/PPG 줄 주기
STATE_SHORT = {0: 'NO SIGNAL', 1: 'SEARCHING', 2: 'OK'}
SRC_SHORT = {0: '--', 1: 'ECG', 2: 'PPG', 3: 'ECG+PPG', 4: 'ECG (PPG disagrees)'}


class Clip:
    """One user-triggered recording of a fixed number of seconds of Pi time.

    While recording, CSVs are written and camera JPEGs are kept in memory with their Pi timestamps
    (30 s ~ 450 frames ~ 50 MB). finalize() then writes the videos at VIDEO_FPS by picking, for every
    output tick, the latest camera frame at that time, so the video plays in real time whatever the
    camera actually delivered. Only the receive thread calls write()/close(); finalize() runs in its own thread.
    """

    def __init__(self, root, seconds, base_meta, preroll, vitals_now):
        self.folder = root / datetime.now().strftime('REC_%Y%m%d_%H%M%S_%f')
        self.folder.mkdir(parents=True, exist_ok=False)
        self.seconds = seconds
        self.start = None  # Pi elapsed time of the first record
        self.last = None
        self.frames = []                   # (pi_elapsed, jpeg bytes)
        self.bio = list(preroll)           # (pi_elapsed, ecg_raw, ppg_raw), from PREROLL_S before the clip
        self.vitals = [(-float('inf'), dict(vitals_now))] if vitals_now else []
        self.counts = {'bio': 0, 'frame': 0, 'vitals': 0}
        self.meta = dict(base_meta, requested_seconds=seconds,
                         start_laptop_utc=datetime.now(timezone.utc).isoformat(), status='recording')
        self.files = {k: (self.folder / f'{k}.csv').open('w', newline='', encoding='utf-8')
                      for k in ('biosignals', 'frames', 'vitals')}
        self.w = {k: csv.writer(f) for k, f in self.files.items()}
        head = ['index', 'pi_elapsed_s', 'clip_s', 'pi_monotonic_ns', 'pi_utc_ns', 'laptop_receive_utc']
        self.w['biosignals'].writerow(head + ['ecg_raw', 'ecg_mv', 'ppg_raw'])
        self.w['frames'].writerow(head)
        self.w['vitals'].writerow(head + list(VITAL_FIELDS))

    def elapsed(self):
        return 0.0 if self.start is None else self.last - self.start

    def finished(self, pi_elapsed):
        return self.start is not None and pi_elapsed - self.start >= self.seconds

    def write(self, kind, info, pi_elapsed, received, payload=b''):
        if self.start is None:
            self.start = pi_elapsed
        self.last = pi_elapsed
        row = [info['index'], f'{pi_elapsed:.6f}', f'{pi_elapsed - self.start:.6f}',
               info['pi_monotonic_ns'], info['pi_utc_ns'], received]
        if kind == 'bio':
            self.w['biosignals'].writerow(row + [info['ecg'], f"{info['ecg'] * ADC_REF_MV / 1023:.1f}", info['ppg']])
            self.bio.append((pi_elapsed, info['ecg'], info['ppg']))
        elif kind == 'vitals':
            self.w['vitals'].writerow(row + [info[k] for k in VITAL_FIELDS])
            self.vitals.append((pi_elapsed, {k: info[k] for k in VITAL_FIELDS}))
        else:
            self.w['frames'].writerow(row)
            self.frames.append((pi_elapsed, payload))
        self.counts[kind] += 1

    def close(self, status):
        """Close the CSVs (receive thread). Videos are written later by finalize()."""
        for f in self.files.values():
            f.close()
        self.meta.update(status=status, recorded_seconds=round(self.elapsed(), 3), counts=dict(self.counts),
                         end_laptop_utc=datetime.now(timezone.utc).isoformat(), video_status='saving')
        self._save_meta()

    def _save_meta(self):
        (self.folder / 'session.json').write_text(json.dumps(self.meta, indent=2, ensure_ascii=False), encoding='utf-8')

    def finalize(self):
        import cv2
        import numpy as np
        try:
            if not self.frames:
                self.meta['video_status'] = 'no camera frames'
                return
            ft = np.array([t for t, _ in self.frames]) - self.start
            duration = max(self.elapsed(), ft[-1])
            ticks = np.arange(max(1, int(round(duration * VIDEO_FPS)))) / VIDEO_FPS
            src = np.clip(np.searchsorted(ft, ticks, side='right') - 1, 0, len(ft) - 1)
            first = cv2.imdecode(np.frombuffer(self.frames[0][1], np.uint8), cv2.IMREAD_COLOR)
            h, w = first.shape[:2]
            panel_h = int(h * 0.36)
            sig = self._signals(np)
            # MP4(mp4v): 반복 프레임을 거의 공짜로 저장해 MJPG AVI 보다 약 5배 작다 (화질 PSNR 약 39dB).
            # 이 OpenCV 에 mp4v 인코더가 없으면 MJPG AVI 로 저장한다.
            for ext, codec in (('mp4', 'mp4v'), ('avi', 'MJPG')):
                fourcc = cv2.VideoWriter_fourcc(*codec)
                names = (f'video.{ext}', f'video_overlay.{ext}')
                plain = cv2.VideoWriter(str(self.folder / names[0]), fourcc, VIDEO_FPS, (w, h))
                over = cv2.VideoWriter(str(self.folder / names[1]), fourcc, VIDEO_FPS, (w, h + panel_h))
                if plain.isOpened() and over.isOpened():
                    break
                plain.release()
                over.release()
                for n in names:
                    (self.folder / n).unlink(missing_ok=True)
            else:
                raise RuntimeError('Cannot create video files')
            last_i, img = -1, first
            for k, tick in enumerate(ticks):
                i = int(src[k])
                if i != last_i:
                    img = cv2.imdecode(np.frombuffer(self.frames[i][1], np.uint8), cv2.IMREAD_COLOR)
                    if img.shape[:2] != (h, w):
                        img = cv2.resize(img, (w, h))
                    last_i = i
                plain.write(img)
                over.write(self._overlay(img, tick, sig, panel_h, cv2, np))
            plain.release()
            over.release()
            span = ft[-1] - ft[0]
            self.meta.update(video_status='saved', video_files=list(names), video_codec=codec, video_fps=VIDEO_FPS,
                             video_frames=len(ticks), camera_frames=len(ft),
                             camera_measured_fps=round((len(ft) - 1) / span, 2) if span > 0 else None,
                             video_note=f'{names[0]} = camera only, {names[1]} = camera + ECG/PPG waveforms + Nano BPM. '
                                        'Both resampled to video_fps by Pi timestamps (frames repeated/dropped), so they play in real time.')
        except Exception as exc:
            self.meta['video_status'] = 'error: ' + str(exc)
        finally:
            self.frames = []
            self._save_meta()
            print(f"녹화 저장 ({self.meta['status']}, {self.elapsed():.1f}초, 영상 {self.meta['video_status']}): {self.folder}")

    def _signals(self, np):
        """Zero-phase filtered ECG/PPG over the whole clip (plus pre-roll) for the overlay."""
        if len(self.bio) < 3 * ECG_FS:
            return None
        b = np.asarray(self.bio, dtype=float)
        t = b[:, 0] - self.start
        span = t[-1] - t[0]
        fs = (len(t) - 1) / span if span > 0 else ECG_FS
        return t, clean_ecg(b[:, 1] * ADC_REF_MV / 1023, fs), clean_ppg(b[:, 2], fs)

    def _vitals_at(self, tick):
        v = None
        for t, d in self.vitals:
            if t - self.start > tick:
                break
            v = d
        return v

    def _overlay(self, img, tick, sig, panel_h, cv2, np):
        h, w = img.shape[:2]
        out = np.zeros((h + panel_h, w, 3), np.uint8)
        out[:h] = img
        s = w / 1280
        thin, bold = max(1, int(2 * s)), max(2, int(5 * s))
        font = cv2.FONT_HERSHEY_SIMPLEX
        cv2.putText(out, f'REC {tick:5.1f} s', (int(16 * s), int(40 * s)), font, 1.0 * s, (40, 40, 230), thin)
        wave_w = int(w * 0.68)
        strip = panel_h // 2
        for row, (name, color, col) in enumerate((('ECG', (60, 60, 230), 1), ('PPG', (230, 120, 40), 2))):
            y0 = h + row * strip
            cv2.rectangle(out, (0, y0), (wave_w, y0 + strip - 1), (30, 30, 30), -1)
            cv2.putText(out, name, (int(8 * s), y0 + int(26 * s)), font, 0.7 * s, color, thin)
            if sig is None:
                continue
            t = sig[0]
            m = (t > tick - PREROLL_S) & (t <= tick)
            if m.sum() < 10:
                continue
            y = sig[col][m]
            lo, hi = np.percentile(y, 1), np.percentile(y, 99)
            rng = max(hi - lo, 50.0 if name == 'ECG' else 5.0)
            mid = (hi + lo) / 2
            xs = (t[m] - (tick - PREROLL_S)) / PREROLL_S * (wave_w - 1)
            ys = y0 + strip / 2 - (y - mid) / rng * (strip * 0.8)
            pts = np.stack([xs, np.clip(ys, y0, y0 + strip - 1)], axis=1).astype(np.int32)
            cv2.polylines(out, [pts], False, color, thin, cv2.LINE_AA)
        v = self._vitals_at(tick)
        x0 = wave_w + int(20 * s)
        if v is None:
            cv2.putText(out, 'no Nano BPM', (x0, h + int(60 * s)), font, 0.8 * s, (200, 200, 200), thin)
            return out
        hr = f"{v['hr_bpm']:.0f}" if v['hr_src'] else '--'
        cv2.putText(out, 'HR', (x0, h + int(40 * s)), font, 0.8 * s, (200, 200, 200), thin)
        cv2.putText(out, hr, (x0, h + int(120 * s)), font, 3.0 * s, (80, 200, 80), bold)
        cv2.putText(out, 'BPM  ' + SRC_SHORT.get(v['hr_src'], '?'), (x0, h + int(155 * s)), font, 0.7 * s,
                    (200, 200, 200), thin)
        for i, (name, key, state) in enumerate((('ECG', 'ecg_bpm', 'ecg_state'), ('PPG', 'ppg_bpm', 'ppg_state'))):
            val = f'{v[key]:.0f}' if v[state] == 2 else '--'
            cv2.putText(out, f'{name} {val}  {STATE_SHORT.get(v[state], "?")}', (x0, h + int((195 + 35 * i) * s)),
                        font, 0.7 * s, (60, 60, 230) if i == 0 else (230, 120, 40), thin)
        return out


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--host', default='ecall.local')
    p.add_argument('--user', default='ecall')
    p.add_argument('--port', default=None, help='Nano 시리얼 포트 (라즈베리파이 기본 /dev/ttyUSB0, --local 이면 예: COM6)')
    p.add_argument('--camera', default=None, help='카메라 (라즈베리파이 기본 /dev/video0, --local 기본 0)')
    p.add_argument('--width', type=int, default=1280)
    p.add_argument('--height', type=int, default=720)
    p.add_argument('--fps', type=int, default=30)
    p.add_argument('--record-seconds', type=int, default=RECORD_CHOICES[0], choices=RECORD_CHOICES,
                   help='녹화 길이 기본값 (화면에서도 바꿀 수 있음)')
    p.add_argument('--local', action='store_true', help='라즈베리파이 없이 이 PC 에서 실행 (Nano·카메라를 이 PC 에 연결)')
    p.add_argument('--output', type=Path, default=Path(__file__).resolve().parent / 'recordings')
    args = p.parse_args()
    if min(args.width, args.height, args.fps) <= 0:
        p.error('width, height and fps must be positive')
    if args.port is None:
        if args.local:
            p.error('--local 에서는 --port 로 Nano 포트를 지정하세요 (예: --port COM6)')
        args.port = '/dev/ttyUSB0'
    if args.camera is None:
        args.camera = '0' if args.local else '/dev/video0'
    import cv2
    import numpy as np
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation
    from matplotlib.widgets import Button, RadioButtons

    plt.rcParams['font.family'] = ['Malgun Gothic', 'DejaVu Sans']
    plt.rcParams['axes.unicode_minus'] = False
    plt.rcParams['keymap.home'] = [k for k in plt.rcParams['keymap.home'] if k != 'r']  # R 은 녹화 키

    args.output.mkdir(parents=True, exist_ok=True)
    base_meta = dict(source='local' if args.local else args.host, serial_port=args.port, camera=str(args.camera),
                     requested_resolution=[args.width, args.height], camera_requested_fps=args.fps,
                     timing='Pi read-completion timestamps; USB/camera buffering delay is uncalibrated. No ADC or exposure timestamps.',
                     video_timing='AVI constant FPS. Use frames.csv times for alignment, not frame_index / FPS.',
                     arduino='ppg_ecg_nano.ino: ECG:<0..1023> TAB PPG:<0..1023> at 250 Hz; 1 Hz vitals line computed on the Nano. '
                             'No sample counter, so serial loss cannot be proven absent (DROP counts Nano-side ring overflow only).',
                     ecg_mv=f'ecg_raw * {ADC_REF_MV} / 1023 (assumes 5 V USB supply; Pi USB may give ~4.3-4.7 V).',
                     heart_rate='vitals.csv: Nano ECG R-peak BPM, PPG BPM, fused HR_BPM (HR_SRC 0 none, 1 ECG, 2 PPG, '
                                '3 ECG+PPG average, 4 ECG with PPG disagreeing). Research prototype; not diagnostic.')

    stop = threading.Event()
    done = threading.Event()
    display_lock = threading.Lock()
    bio_display = deque(maxlen=30000)  # (pi_elapsed, ecg_raw, ppg_raw)
    latest_frame = [None]
    vitals = {}
    status = ['Connecting']
    counts = {'bio': 0, 'frame': 0, 'vitals': 0}
    last_seen = {'bio': None, 'frame': None}
    stderr_lines = deque(maxlen=30)
    # UI → receive thread. A Clip lives only in the receive thread; the UI reads clip_view.
    record_request = [None]   # seconds to start a clip
    stop_request = [False]
    clip_view = {'recording': False, 'elapsed': 0.0, 'seconds': 0, 'last': ''}
    savers = []                # finalize() threads that write the videos after a clip ends
    worker = None
    stdin = stdout = stderr = None
    client = proc = None

    def end_clip(clip, status_text):
        clip.close(status_text)
        clip_view.update(recording=False, last=str(clip.folder))
        saver = threading.Thread(target=clip.finalize, name='video-save')
        saver.start()
        savers.append(saver)

    def receive():
        origin = None
        clip = None
        expected = {'bio': 0, 'frame': 0, 'vitals': 0}
        try:
            while True:
                info, payload = read_packet(stdout)
                received = datetime.now(timezone.utc).isoformat()
                kind = info['type']
                if kind == 'hello':
                    origin = info['origin_ns']
                    status[0] = 'Waiting for camera and Nano'
                elif kind == 'camera_info':
                    base_meta['camera_reported'] = info
                elif kind == 'end':
                    if info['errors']:
                        raise RuntimeError('; '.join(info['errors']))
                    break
                elif kind in expected:
                    if origin is None or info['index'] != expected[kind]:
                        raise RuntimeError('Packet sequence mismatch')
                    expected[kind] += 1
                    elapsed = (info['pi_monotonic_ns'] - origin) / 1e9
                    frame = None
                    if kind == 'frame':
                        frame = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
                        if frame is None:
                            raise RuntimeError('JPEG decode failed')

                    if clip is not None and (stop_request[0] or clip.finished(elapsed)):
                        end_clip(clip, 'stopped_by_user' if stop_request[0] else 'complete')
                        clip = None
                        stop_request[0] = False
                    if clip is None and record_request[0]:
                        with display_lock:
                            preroll = [r for r in bio_display if r[0] >= elapsed - PREROLL_S]
                            now_vitals = dict(vitals)
                        clip = Clip(args.output, record_request[0], base_meta, preroll, now_vitals)
                        clip_view.update(recording=True, seconds=clip.seconds, elapsed=0.0)
                        record_request[0] = None
                        stop_request[0] = False
                    if clip is not None:
                        clip.write(kind, info, elapsed, received, payload)
                        clip_view['elapsed'] = clip.elapsed()

                    if kind == 'bio':
                        with display_lock:
                            bio_display.append((elapsed, info['ecg'], info['ppg']))
                    elif kind == 'vitals':
                        with display_lock:
                            vitals.update({k: info[k] for k in VITAL_FIELDS})
                    else:
                        with display_lock:
                            latest_frame[0] = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    counts[kind] += 1
                    if kind in last_seen:
                        last_seen[kind] = time.monotonic()
                    status[0] = 'Live'
        except Exception as exc:
            status[0] = 'ERROR: ' + str(exc)
            print(status[0])
            try:
                stdin.write(b'q')
                stdin.flush()
            except Exception:
                pass
        finally:
            if clip is not None:
                end_clip(clip, 'interrupted')
            done.set()

    try:
        cfg = dict(port=args.port, camera=args.camera, width=args.width, height=args.height, fps=args.fps)
        encoded = base64.b64encode(REMOTE.encode()).decode()
        bootstrap = "import base64;exec(base64.b64decode(" + repr(encoded) + "))"
        if args.local:
            env = dict(os.environ, OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS='0')  # Windows 카메라 여는 시간 단축
            proc = subprocess.Popen([sys.executable, '-u', '-c', bootstrap, json.dumps(cfg)], env=env,
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdin, stdout, stderr = proc.stdin, proc.stdout, proc.stderr
        else:
            import paramiko
            client = paramiko.SSHClient()
            client.load_system_host_keys()
            known = Path.home() / '.ssh' / 'known_hosts'
            if known.exists():
                client.load_host_keys(str(known))
            # Reject unknown/changed keys; first verify through Windows ssh.
            password = getpass.getpass(f'Pi account password for {args.user}@{args.host}: ')
            client.connect(args.host, username=args.user, password=password, timeout=10,
                           auth_timeout=15, banner_timeout=15, look_for_keys=False, allow_agent=False)
            del password
            client.get_transport().set_keepalive(10)
            command = 'python3 -u -c ' + shlex.quote(bootstrap) + ' ' + shlex.quote(json.dumps(cfg))
            stdin, stdout, stderr = client.exec_command(command)
            stdout.channel.settimeout(20)

        def drain_errors():
            try:
                for line in stderr:
                    line = line.decode(errors='replace') if isinstance(line, bytes) else line
                    stderr_lines.append(line.rstrip())
                    print('Pi:', line.rstrip())
            except Exception:
                pass
        threading.Thread(target=drain_errors, daemon=True).start()
        worker = threading.Thread(target=receive, daemon=True)
        worker.start()
        print('녹화 저장 폴더:', args.output)
        print('[녹화 시작] 버튼이나 R 키로 녹화. 창을 닫으면 종료.')

        fig = plt.figure(figsize=(15, 9))
        fig.canvas.manager.set_window_title('운전자 생체신호 + 카메라')
        grid = fig.add_gridspec(3, 3, bottom=0.14, top=0.90, hspace=0.45)
        ax_cam = fig.add_subplot(grid[:, :2])
        ax_ecg = fig.add_subplot(grid[0, 2])
        ax_ppg = fig.add_subplot(grid[1, 2], sharex=ax_ecg)
        ax_hr = fig.add_subplot(grid[2, 2])
        image = ax_cam.imshow(np.zeros((args.height, args.width, 3), dtype=np.uint8))
        ax_cam.set_title('카메라')
        ax_cam.axis('off')
        rec_badge = ax_cam.text(0.02, 0.96, '', transform=ax_cam.transAxes, fontsize=16, fontweight='bold',
                                color='white', va='top', bbox=dict(facecolor='#d0021b', edgecolor='none', alpha=0.0))
        ecg_line, = ax_ecg.plot([], [], color='#c03030', linewidth=1)
        ppg_line, = ax_ppg.plot([], [], color='#2050c0', linewidth=1.2)
        ax_ecg.set(title=f'ECG ({MAINS_HZ}Hz 노치 + 0.5~40Hz, 화면 표시용)', ylabel='mV')
        ax_ppg.set(title='PPG 맥박 (0.7~4Hz, 화면 표시용)', xlabel='라즈베리파이 시간 (초)')
        for ax in (ax_ecg, ax_ppg):
            ax.grid(alpha=0.25)
        ax_ecg.tick_params(labelbottom=False)
        ax_hr.axis('off')
        ax_hr.text(0.5, 0.98, '심박 (Nano 융합)', ha='center', va='top', fontsize=12, transform=ax_hr.transAxes)
        hr_text = ax_hr.text(0.5, 0.70, '--', ha='center', va='center', fontsize=46, fontweight='bold',
                             color='#1a8f3a', transform=ax_hr.transAxes)
        src_text = ax_hr.text(0.5, 0.45, '', ha='center', va='center', fontsize=11, transform=ax_hr.transAxes)
        sub_text = ax_hr.text(0.5, 0.18, '', ha='center', va='center', fontsize=11, transform=ax_hr.transAxes)

        # 녹화 조작: 길이 선택 + 시작/중지 버튼
        fig.text(0.02, 0.07, '녹화 길이', fontsize=11, va='center')
        ax_radio = fig.add_axes((0.08, 0.005, 0.09, 0.13), frameon=False)
        labels = [f'{s}초' for s in RECORD_CHOICES]
        radio = RadioButtons(ax_radio, labels, active=RECORD_CHOICES.index(args.record_seconds))
        for lbl in radio.labels:
            lbl.set_fontsize(11)
        ax_button = fig.add_axes((0.20, 0.035, 0.16, 0.07))
        button = Button(ax_button, '● 녹화 시작 (R)', color='#f2f2f2', hovercolor='#e0e0e0')
        button.label.set_fontsize(12)
        rec_text = fig.text(0.38, 0.07, '', fontsize=11, va='center')

        def selected_seconds():
            return RECORD_CHOICES[labels.index(radio.value_selected)]

        def toggle_record(_=None):
            if clip_view['recording']:
                stop_request[0] = True
            elif record_request[0]:
                record_request[0] = None          # 아직 시작 전이면 취소
            elif status[0] == 'Live':
                record_request[0] = selected_seconds()

        button.on_clicked(toggle_record)
        fig.canvas.mpl_connect('key_press_event', lambda e: toggle_record() if e.key in ('r', 'R') else None)

        last_scale = [-float('inf')]

        def update(_):
            with display_lock:
                rows = list(bio_display)
                frame = latest_frame[0]
                v = dict(vitals)
            now = time.monotonic()
            if len(rows) > 500:
                all_data = np.asarray(rows, dtype=float)
                span = all_data[all_data[:, 0] >= all_data[-1, 0] - 10]
                if span[-1, 0] > span[0, 0]:
                    fs = (len(span) - 1) / (span[-1, 0] - span[0, 0])
                    shown = span[:, 0] >= span[-1, 0] - 5
                    ecg = clean_ecg(span[:, 1] * ADC_REF_MV / 1023, fs)
                    ppg = clean_ppg(span[:, 2], fs)
                    ecg_line.set_data(span[shown, 0], ecg[shown])
                    ppg_line.set_data(span[shown, 0], ppg[shown])
                    ax_ecg.set_xlim(span[shown, 0][0], span[-1, 0])
                    if now - last_scale[0] >= 1:
                        autoscale_wave(ax_ecg, ecg[shown], 50)
                        autoscale_wave(ax_ppg, ppg[shown], 5)
                        last_scale[0] = now
            if frame is not None:
                image.set_data(frame)
            stale = last_seen['bio'] is None or now - last_seen['bio'] > 2
            if v and not stale:
                hr_text.set_text(f"{v['hr_bpm']:.0f}" if v['hr_src'] else '--')
                src_text.set_text('출처: ' + HR_SRC.get(v['hr_src'], '?'))
                ecg_s = f"{v['ecg_bpm']:.0f}" if v['ecg_state'] == 2 else '--'
                ppg_s = f"{v['ppg_bpm']:.0f}" if v['ppg_state'] == 2 else '--'
                sub_text.set_text(f"ECG {ecg_s} ({ECG_STATE.get(v['ecg_state'], '?')})\n"
                                  f"PPG {ppg_s} ({PPG_STATE.get(v['ppg_state'], '?')}, 진폭 {v['ppg_pp']})")
            else:
                hr_text.set_text('--')
                src_text.set_text('Nano 심박 정보 없음' if not v else '데이터 끊김')
                sub_text.set_text('')
            if clip_view['recording']:
                left = max(0.0, clip_view['seconds'] - clip_view['elapsed'])
                rec_badge.set_text(f" ● REC {clip_view['elapsed']:4.1f} / {clip_view['seconds']}s ")
                rec_badge.get_bbox_patch().set_alpha(0.85)
                button.label.set_text('■ 녹화 중지 (R)')
                rec_text.set_text(f'녹화 중… {left:.0f}초 남음')
            else:
                rec_badge.set_text('')
                rec_badge.get_bbox_patch().set_alpha(0.0)
                button.label.set_text('● 녹화 시작 (R)')
                if any(s.is_alive() for s in savers):
                    rec_text.set_text('영상 저장 중… (' + Path(clip_view['last']).name + ')')
                elif clip_view['last']:
                    rec_text.set_text('마지막 녹화: ' + Path(clip_view['last']).name)
                else:
                    rec_text.set_text('녹화 대기' if status[0] == 'Live' else '연결 중…')
            ages = ' | '.join(f'{k}: {counts[k]} (age {now - t:.1f}s)' if t is not None else f'{k}: waiting'
                              for k, t in last_seen.items())
            fig.suptitle(status[0] + '\n' + ages, fontsize=10)
            return image, ecg_line, ppg_line, hr_text, src_text, sub_text, rec_badge

        animation = FuncAnimation(fig, update, interval=100, cache_frame_data=False)
        plt.show()
    finally:
        if stdin is not None:
            try:
                stdin.write(b'q'); stdin.flush()
            except Exception:
                pass
        if worker is not None:
            # Reader drains accepted records before CSV and AVI are finalized.
            worker.join(timeout=10)
            if worker.is_alive():
                if proc is not None:
                    proc.kill()
                else:
                    stdout.channel.close()
                worker.join(timeout=22)
        if any(s.is_alive() for s in savers):
            print('영상 저장이 끝날 때까지 기다리는 중...')
        for saver in savers:
            saver.join()
        if client is not None:
            client.close()
        if proc is not None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if stderr_lines:
            log = args.output / datetime.now().strftime('pi_stderr_%Y%m%d_%H%M%S.txt')
            log.write_text('\n'.join(stderr_lines), encoding='utf-8')
        print('종료. 수신 개수:', counts)

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Stopped.')
    except Exception as exc:
        print('ERROR:', exc)
        print('Verify SSH login/known_hosts, Pi packages, device paths, and close other camera/serial readers.')
        raise SystemExit(1)
