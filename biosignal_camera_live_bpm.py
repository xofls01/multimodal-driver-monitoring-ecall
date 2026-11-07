import argparse
import base64
import csv
import getpass
import json
import queue
import re
import shlex
import struct
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

PATTERN = re.compile(r"ECG:(\d+)\s+PPG:(\d+)")

def parse_sample(line):
    match = PATTERN.fullmatch(line.strip())
    if not match:
        return None
    values = tuple(map(int, match.groups()))
    return values if all(0 <= v <= 1023 for v in values) else None

# Sent as an in-memory command. No script or recordings written on the Pi.
REMOTE = r'''
import sys, time, json, struct, threading, queue, re, glob
import cv2, serial
cfg = json.loads(sys.argv[1])
stop = threading.Event()
packets = queue.Queue(maxsize=1500)
origin = time.monotonic_ns()
pattern = re.compile(r"ECG:(\d+)\s+PPG:(\d+)")
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
        port = cfg['port']
        if port == 'auto':
            # The Nano clone appears as ttyUSB, other boards as ttyACM.
            found = sorted(glob.glob('/dev/ttyUSB*')) + sorted(glob.glob('/dev/ttyACM*'))
            if not found:
                raise RuntimeError('No Arduino serial port found (/dev/ttyUSB*, /dev/ttyACM*). Check the USB cable.')
            port = found[0]
        with serial.Serial(port, 115200, timeout=1, exclusive=True) as ser:
            # Opening Nano serial often resets it. Discard startup bytes.
            time.sleep(2)
            ser.reset_input_buffer()
            index = 0
            last_valid = time.monotonic()
            while not stop.is_set():
                raw = ser.readline(256)
                ts = stamp()
                match = pattern.fullmatch(raw.decode('ascii', errors='replace').strip())
                if match:
                    ecg, ppg = map(int, match.groups())
                    if 0 <= ecg <= 1023 and 0 <= ppg <= 1023:
                        emit(dict(type='bio', index=index, ecg=ecg, ppg=ppg, **ts))
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



def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host', default='172.20.10.7')
    p.add_argument('--user', default='ecall')
    p.add_argument('--port', default='auto', help='Pi serial port. auto tries /dev/ttyUSB* then /dev/ttyACM*')
    p.add_argument('--camera', default='/dev/video0')
    p.add_argument('--width', type=int, default=1280)
    p.add_argument('--height', type=int, default=720)
    p.add_argument('--fps', type=int, default=30)
    p.add_argument('--output', type=Path, default=Path(__file__).resolve().parent / 'recordings')
    models = Path(__file__).resolve().parent / 'models'
    p.add_argument('--bio-model', type=Path, default=models / 'hr_classifier_ft.pt')
    p.add_argument('--ppg-encoder', type=Path, default=models / 'papagei_s.pt')
    p.add_argument('--ppg-head', type=Path, default=models / 'ppg_head_ft.pt')
    args = p.parse_args()
    if min(args.width, args.height, args.fps) <= 0:
        p.error('width, height and fps must be positive')
    for path in (args.bio_model, args.ppg_encoder, args.ppg_head):
        if not path.is_file():
            p.error(f'Model file not found: {path}')
    import cv2
    import numpy as np
    import paramiko
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation
    from biosignal.dashboard import BioDashboard
    from biosignal.live import LiveAssessor

    folder = args.output / datetime.now().strftime('SESSION_%Y%m%d_%H%M%S_%f')
    folder.mkdir(parents=True, exist_ok=False)
    meta = dict(host=args.host, requested_resolution=[args.width, args.height],
                nominal_video_fps=args.fps, start_laptop_utc=datetime.now(timezone.utc).isoformat(),
                timing='Pi read-completion timestamps; USB/camera buffering delay is uncalibrated. No ADC or exposure timestamps.',
                video_timing='AVI constant FPS. Use frames.csv times for alignment, not frame_index / FPS.',
                arduino='ECG:<0..1023> TAB PPG:<0..1023>; no sample counter, so serial loss cannot be proven absent.',
                heart_rate='biosignal package: ECG/PPG peak-interval BPM, ECG CNN and PaPaGei-S PPG grades; results in bio_status.jsonl; raw CSV unchanged. Not diagnostic.',
                status='starting')
    def save_meta():
        (folder / 'session.json').write_text(json.dumps(meta, indent=2), encoding='utf-8')
    save_meta()
    assessor = LiveAssessor(model_path=str(args.bio_model), log_path=str(folder / 'bio_status.jsonl'),
                            ppg_encoder_path=str(args.ppg_encoder), ppg_head_path=str(args.ppg_head))
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    known = Path.home() / '.ssh' / 'known_hosts'
    if known.exists():
        client.load_host_keys(str(known))
    # Reject unknown/changed keys; first verify through Windows ssh.
    stop = threading.Event()
    done = threading.Event()
    display_lock = threading.Lock()
    bio_display = deque(maxlen=3000)
    status = ['Connecting']
    counts = {'bio': 0, 'frame': 0}
    last_seen = {'bio': None, 'frame': None}
    stderr_lines = deque(maxlen=30)
    worker = None
    stdin = stdout = stderr = None

    def receive():
        video = None
        origin = None
        expected = {'bio': 0, 'frame': 0}
        try:
            with (folder / 'biosignals.csv').open('w', newline='', encoding='utf-8') as bf, \
                 (folder / 'frames.csv').open('w', newline='', encoding='utf-8') as ff:
                bw, fw = csv.writer(bf), csv.writer(ff)
                bw.writerow(['sample_index', 'pi_elapsed_s', 'pi_monotonic_ns', 'pi_utc_ns', 'laptop_receive_utc', 'ecg_raw', 'ppg_raw'])
                fw.writerow(['frame_index', 'pi_elapsed_s', 'pi_monotonic_ns', 'pi_utc_ns', 'laptop_receive_utc'])
                flushed = time.monotonic()
                dimensions = None
                while True:
                    info, payload = read_packet(stdout)
                    received = datetime.now(timezone.utc).isoformat()
                    kind = info['type']
                    if kind == 'hello':
                        origin = info['origin_ns']
                        meta['pi_origin_ns'] = origin
                        status[0] = 'Waiting for camera and ECG/PPG'
                    elif kind == 'camera_info':
                        meta['camera_reported'] = info
                    elif kind == 'end':
                        if info['errors']:
                            raise RuntimeError('; '.join(info['errors']))
                        meta['status'] = 'stopped'
                        break
                    elif kind in expected:
                        if origin is None or info['index'] != expected[kind]:
                            raise RuntimeError('Packet sequence mismatch')
                        expected[kind] += 1
                        elapsed = (info['pi_monotonic_ns'] - origin) / 1e9
                        row = [info['index'], f'{elapsed:.9f}', info['pi_monotonic_ns'], info['pi_utc_ns'], received]
                        if kind == 'bio':
                            bw.writerow(row + [info['ecg'], info['ppg']])
                            with display_lock:
                                bio_display.append((elapsed, info['ecg'], info['ppg']))
                            assessor.push(elapsed, info['ecg'], info['ppg'])
                        else:
                            frame = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
                            if frame is None:
                                raise RuntimeError('JPEG decode failed')
                            h, w = frame.shape[:2]
                            if video is None:
                                dimensions = (w, h)
                                video = cv2.VideoWriter(str(folder / 'video.avi'), cv2.VideoWriter_fourcc(*'MJPG'), args.fps, dimensions)
                                if not video.isOpened():
                                    raise RuntimeError('Cannot create laptop video.avi')
                            if dimensions != (w, h):
                                raise RuntimeError('Camera dimensions changed')
                            video.write(frame)
                            fw.writerow(row)
                        counts[kind] += 1
                        last_seen[kind] = time.monotonic()
                        status[0] = 'Recording on laptop'
                    if time.monotonic() - flushed > 1:
                        bf.flush()
                        ff.flush()
                        flushed = time.monotonic()
        except Exception as exc:
            meta['status'] = 'error'
            meta['error'] = str(exc)
            status[0] = 'ERROR: ' + str(exc)
            print(status[0])
            try:
                stdin.write(b'q')
                stdin.flush()
            except Exception:
                pass
        finally:
            if video is not None:
                video.release()
            meta['counts'] = dict(counts)
            meta['end_laptop_utc'] = datetime.now(timezone.utc).isoformat()
            save_meta()
            done.set()

    try:
        password = getpass.getpass(f'Pi account password for {args.user}@{args.host}: ')
        client.connect(args.host, username=args.user, password=password, timeout=10,
                       auth_timeout=15, banner_timeout=15, look_for_keys=False, allow_agent=False)
        del password
        client.get_transport().set_keepalive(10)
        cfg = dict(port=args.port, camera=args.camera, width=args.width, height=args.height, fps=args.fps)
        encoded = base64.b64encode(REMOTE.encode()).decode()
        bootstrap = "import base64;exec(base64.b64decode(" + repr(encoded) + "))"
        command = 'python3 -u -c ' + shlex.quote(bootstrap) + ' ' + shlex.quote(json.dumps(cfg))
        stdin, stdout, stderr = client.exec_command(command)
        stdout.channel.settimeout(20)
        def drain_errors():
            try:
                for line in stderr:
                    stderr_lines.append(line.rstrip())
                    print('Pi:', line.rstrip())
            except Exception:
                pass
        threading.Thread(target=drain_errors, daemon=True).start()
        worker = threading.Thread(target=receive, daemon=True)
        worker.start()
        assessor.start()
        print('Saving on LAPTOP:', folder)
        print('Close the dashboard to stop. Allow the final saving message before closing VS Code.')
        # The camera is still recorded to video.avi; this screen only shows biosignals.
        dashboard = BioDashboard()
        def update(_):
            with display_lock:
                rows = np.asarray(bio_display) if bio_display else None
            ages = ' | '.join(f'{k}: {counts[k]} (age {time.monotonic()-v:.1f}s)' if v is not None else f'{k}: waiting' for k, v in last_seen.items())
            dashboard.update(rows, assessor.latest(), assessor.error, footer=status[0] + '\n' + ages)
            return dashboard.artists()
        animation = FuncAnimation(dashboard.fig, update, interval=100, cache_frame_data=False)
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
                stdout.channel.close()
                worker.join(timeout=22)
        assessor.stop()
        client.close()
        if worker is None:
            meta['status'] = 'startup_failed'
            save_meta()
        if stderr_lines:
            (folder / 'pi_stderr.txt').write_text('\n'.join(stderr_lines), encoding='utf-8')
        print('Recording closed. Laptop folder:', folder)
        print('Samples / frames:', counts)

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Stopped.')
    except Exception as exc:
        print('ERROR:', exc)
        print('Verify SSH login/known_hosts, Pi packages, device paths, and close other camera/serial readers.')
        raise SystemExit(1)
