"""실시간 입력: Pi 의 카메라 + 아두이노(ECG/PPG)를 SSH 로 받아 오거나, 녹화 폴더를 실시간처럼 흘려보낸다.

두 소스 모두 같은 콜백을 부른다.
    on_bio(t, ecg_raw, ppg_raw)      t: Pi 시계 기준 초 (스트림 시작 = 0)
    on_frame(t, frame)               카메라 프레임 하나. Pi 는 JPEG bytes(디코딩은 처리할 프레임만),
                                     녹화 폴더는 BGR 배열
    on_end(error_or_None)            스트림이 끝났을 때 한 번

Pi 쪽 수집 코드는 팀원의 biosignal_camera_live_bpm.py(biomodel 브랜치)와 같은 방식이다.
Pi 에 파일을 남기지 않고 SSH 명령으로 메모리에서 실행하며, Pi 비밀번호는 실행할 때 직접 입력받는다.
"""
from __future__ import annotations

import base64
import csv
import json
import shlex
import struct
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np

# Pi 에서 실행되는 코드 (python3 + opencv + pyserial 필요). 팀원 스크립트와 같은 패킷 형식.
REMOTE = r'''
import sys, time, json, struct, threading, queue, re, glob
import cv2, serial
cfg = json.loads(sys.argv[1])
stop = threading.Event()
packets = queue.Queue(maxsize=1500)
origin = time.monotonic_ns()
pattern = re.compile(r"ECG:(\d+)\s+PPG:(\d+)")
errors = []

def emit(info, payload=b''):
    info['payload_bytes'] = len(payload)
    try:
        packets.put_nowait((info, payload))
    except queue.Full:
        if info['type'] == 'frame':
            return                      # 네트워크가 느리면 영상 프레임만 버린다 (생체신호는 지킨다)
        errors.append('Pi transmission queue full')
        stop.set()

def bio():
    try:
        port = cfg['port']
        if port == 'auto':
            found = sorted(glob.glob('/dev/ttyUSB*')) + sorted(glob.glob('/dev/ttyACM*'))
            if not found:
                raise RuntimeError('No Arduino serial port found (/dev/ttyUSB*, /dev/ttyACM*). Check the USB cable.')
            port = found[0]
        with serial.Serial(port, 115200, timeout=1, exclusive=True) as ser:
            time.sleep(2)               # Nano 는 포트를 열면 재시작된다
            ser.reset_input_buffer()
            index = 0
            last_valid = time.monotonic()
            while not stop.is_set():
                raw = ser.readline(256)
                ts = time.monotonic_ns()
                m = pattern.fullmatch(raw.decode('ascii', errors='replace').strip())
                if m:
                    ecg, ppg = map(int, m.groups())
                    if 0 <= ecg <= 1023 and 0 <= ppg <= 1023:
                        emit(dict(type='bio', index=index, ecg=ecg, ppg=ppg, ns=ts))
                        index += 1
                        last_valid = time.monotonic()
                if time.monotonic() - last_valid > 10:
                    raise RuntimeError('No valid ECG/PPG for 10 seconds. Check firmware, wiring and other serial readers.')
    except Exception as exc:
        errors.append('Serial: ' + str(exc))
        stop.set()

def camera():
    cap = None
    try:
        cap = cv2.VideoCapture(cfg['camera'], cv2.CAP_V4L2)
        if not cap.isOpened():
            raise RuntimeError('Cannot open camera ' + str(cfg['camera']))
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg['width'])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg['height'])
        cap.set(cv2.CAP_PROP_FPS, cfg['fps'])
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        emit(dict(type='camera_info', width=cap.get(3), height=cap.get(4), fps=cap.get(5)))
        index = 0
        while not stop.is_set():
            ok, frame = cap.read()
            ts = time.monotonic_ns()
            if not ok:
                raise RuntimeError('Camera frame read failed')
            ok, enc = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, cfg['jpeg_quality']])
            if ok:
                emit(dict(type='frame', index=index, ns=ts), enc.tobytes())
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
    sys.stdin.buffer.read(1)
    stop.set()

send(dict(type='hello', origin_ns=origin))
threading.Thread(target=control, daemon=True).start()
threads = [threading.Thread(target=bio, daemon=True), threading.Thread(target=camera, daemon=True)]
for th in threads:
    th.start()
try:
    while not stop.is_set():
        try:
            info, payload = packets.get(timeout=0.2)
        except queue.Empty:
            continue
        send(info, payload)
    for th in threads:
        th.join(timeout=2)
    while not packets.empty():
        info, payload = packets.get_nowait()
        send(info, payload)
    send(dict(type='end', errors=errors))
except (BrokenPipeError, OSError):
    stop.set()
'''


def read_exact(stream, size: int) -> bytes:
    out = bytearray()
    while len(out) < size:
        data = stream.read(size - len(out))
        if not data:
            raise EOFError('Pi stream ended')
        out.extend(data)
    return bytes(out)


def read_packet(stream) -> tuple[dict, bytes]:
    size, = struct.unpack('!I', read_exact(stream, 4))
    if not 0 < size <= 65536:
        raise ValueError('Invalid packet header')
    info = json.loads(read_exact(stream, size))
    length = info.get('payload_bytes')
    if not isinstance(length, int) or not 0 <= length <= 20_000_000:
        raise ValueError('Invalid payload size')
    return info, read_exact(stream, length)


class StreamHandler:
    """Pi 패킷 → 콜백. 순서·시계를 검사한다 (네트워크 없이 시험할 수 있게 분리)."""

    def __init__(self, on_bio, on_frame, decode_jpeg: Optional[Callable[[bytes], Optional[np.ndarray]]] = None):
        self.on_bio, self.on_frame, self.decode = on_bio, on_frame, decode_jpeg
        self.origin_ns: Optional[int] = None
        self.expected_bio = 0
        self.camera_info: Optional[dict] = None
        self.dropped_frames = 0
        self._last_frame_index = -1

    def handle(self, info: dict, payload: bytes) -> bool:
        """처리하고 계속 읽을지 돌려준다. 'end' 면 False."""
        kind = info.get('type')
        if kind == 'hello':
            self.origin_ns = int(info['origin_ns'])
        elif kind == 'camera_info':
            self.camera_info = info
        elif kind == 'end':
            if info.get('errors'):
                raise RuntimeError('Pi: ' + '; '.join(info['errors']))
            return False
        elif kind in ('bio', 'frame'):
            if self.origin_ns is None:
                raise RuntimeError('Pi data arrived before hello')
            t = (int(info['ns']) - self.origin_ns) / 1e9
            if kind == 'bio':
                if info['index'] != self.expected_bio:
                    raise RuntimeError(f"ECG/PPG sample sequence gap: expected {self.expected_bio}, got {info['index']}")
                self.expected_bio += 1
                self.on_bio(t, float(info['ecg']), float(info['ppg']))
            else:
                index = int(info['index'])
                self.dropped_frames += max(0, index - self._last_frame_index - 1)
                self._last_frame_index = index
                if not payload:
                    raise RuntimeError('Empty camera frame')
                if self.decode is None:
                    self.on_frame(t, payload)           # JPEG 그대로 (30 fps 전부 디코딩하지 않는다)
                else:
                    frame = self.decode(payload)
                    if frame is None:
                        raise RuntimeError('JPEG decode failed')
                    self.on_frame(t, frame)
        return True


class PiStream:
    """SSH 로 Pi 에서 REMOTE 를 실행하고 패킷을 받는다. 비밀번호는 getpass 로 사용자가 입력."""

    def __init__(self, host: str, user: str, *, port: str = 'auto', camera: str = '/dev/video0',
                 width: int = 1280, height: int = 720, fps: int = 15, jpeg_quality: int = 80):
        self.host, self.user = host, user
        self.cfg = dict(port=port, camera=camera, width=width, height=height, fps=fps, jpeg_quality=jpeg_quality)
        self.client = None
        self._stdin = None
        self._thread: Optional[threading.Thread] = None
        self.handler: Optional[StreamHandler] = None

    def start(self, on_bio, on_frame, on_end) -> None:
        import getpass
        import paramiko

        self.client = paramiko.SSHClient()
        self.client.load_system_host_keys()
        known = Path.home() / '.ssh' / 'known_hosts'
        if known.exists():
            self.client.load_host_keys(str(known))
        # 모르는 호스트 키는 거부 (처음이면 Windows 터미널에서 ssh 로 한 번 접속해 키를 등록)
        password = getpass.getpass(f'Pi 비밀번호 ({self.user}@{self.host}): ')
        try:
            self.client.connect(self.host, username=self.user, password=password, timeout=10, auth_timeout=15,
                                banner_timeout=15, look_for_keys=False, allow_agent=False)
        finally:
            del password
        self.client.get_transport().set_keepalive(10)
        encoded = base64.b64encode(REMOTE.encode()).decode()
        bootstrap = 'import base64;exec(base64.b64decode(' + repr(encoded) + '))'
        command = 'python3 -u -c ' + shlex.quote(bootstrap) + ' ' + shlex.quote(json.dumps(self.cfg))
        stdin, stdout, stderr = self.client.exec_command(command)
        stdout.channel.settimeout(20)
        self._stdin = stdin
        self.handler = StreamHandler(on_bio, on_frame)

        def drain_stderr():
            try:
                for line in stderr:
                    print('[Pi]', line.rstrip(), flush=True)
            except Exception:
                pass

        def run():
            error = None
            try:
                while self.handler.handle(*read_packet(stdout)):
                    pass
            except Exception as exc:     # 연결 끊김, Pi 오류
                error = exc
            on_end(error)

        threading.Thread(target=drain_stderr, daemon=True).start()
        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        try:
            if self._stdin is not None:
                self._stdin.write(b'q')
                self._stdin.flush()
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self.client is not None:
            self.client.close()


class FolderStream:
    """녹화 폴더(biosignals.csv + frames.csv + 영상)를 실제 시간 속도로 흘려보낸다. Pi 없이 시험·시연용."""

    def __init__(self, folder: Path, video: Path, *, speed: float = 1.0, crop_height: Optional[int] = None):
        self.folder, self.video, self.speed, self.crop_height = Path(folder), Path(video), speed, crop_height
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self, on_bio, on_frame, on_end) -> None:
        import cv2

        with (self.folder / 'biosignals.csv').open(newline='') as f:
            bio = [(float(r['clip_s']), float(r['ecg_raw']), float(r['ppg_raw'])) for r in csv.DictReader(f)]
        with (self.folder / 'frames.csv').open(newline='') as f:
            frame_times = [float(r['clip_s']) for r in csv.DictReader(f)]
        start = time.monotonic()

        def wait_until(t: float) -> bool:
            delay = start + t / self.speed - time.monotonic()
            return not (delay > 0 and self._stop.wait(delay))

        def bio_loop():
            for t, e, p in bio:
                if not wait_until(t):
                    return
                on_bio(t, e, p)

        def frame_loop():
            error = None
            cap = cv2.VideoCapture(str(self.video))
            try:
                for t in frame_times:
                    ok, frame = cap.read()
                    if not ok:
                        break
                    if self.crop_height:
                        frame = frame[:self.crop_height]
                    if not wait_until(t):
                        return
                    on_frame(t, frame)
            except Exception as exc:
                error = exc
            finally:
                cap.release()
            on_end(error)

        self._threads = [threading.Thread(target=bio_loop, daemon=True), threading.Thread(target=frame_loop, daemon=True)]
        for th in self._threads:
            th.start()

    def stop(self) -> None:
        self._stop.set()
        for th in self._threads:
            th.join(timeout=2)
