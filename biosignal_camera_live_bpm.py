import argparse
import base64
import csv
import getpass
import json
import queue
import shlex
import struct
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

ADC_REF_MV = 5000  # Nano/Uno default analog reference (USB supply)
MAINS_HZ = 60  # Korea

# Sent as an in-memory command. No script or recordings written on the Pi.
REMOTE = r'''
import sys, time, json, struct, threading, queue, re
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
        with serial.Serial(cfg['port'], 115200, timeout=1, exclusive=True) as ser:
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
                    # PPG field is ignored: sensor disconnected, floating A1 only mirrors ECG
                    ecg = int(match.group(1))
                    if 0 <= ecg <= 1023:
                        emit(dict(type='bio', index=index, ecg=ecg, **ts))
                        index += 1
                        last_valid = time.monotonic()
                if time.monotonic() - last_valid > 10:
                    raise RuntimeError('No valid ECG for 10 seconds. Check firmware, wiring and other serial readers.')
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


def estimate_ecg_bpm(rows):
    """Experimental R-peak interval estimate, not a validated medical detector.

    rows: (pi_elapsed_s, ecg_mv). Firmware sends 0 on lead-off, so windows with
    zeros or rail clipping are rejected instead of guessed.
    """
    import numpy as np
    from scipy.signal import find_peaks
    a = np.asarray(rows, dtype=float)
    a = a[a[:, 0] >= a[-1, 0] - 10]
    t, y = a[:, 0], a[:, 1]
    if len(t) < 2 or t[-1] - t[0] < 6:
        return None, 'Collecting (need 6 seconds)'
    if np.any(np.diff(t) <= 0):
        return None, 'Invalid timestamps'
    fs = (len(t) - 1) / (t[-1] - t[0])
    if fs < 100 or np.max(np.diff(t)) > 0.1:
        return None, 'Low sample rate / timing gap'
    if np.mean(y <= 0) > 0.02:
        return None, 'LEAD OFF - check electrodes'
    # Pi timestamps are near-uniform at 250 Hz; interpolate anyway before filtering.
    grid = np.arange(t[0], t[-1], 1 / fs)
    signal = clean_ecg(np.interp(grid, t, y), fs)
    noise = np.median(np.abs(signal - np.median(signal)))
    peaks, _ = find_peaks(signal, distance=int(fs * 0.33), prominence=max(150, 5 * noise))
    # Exclude filter boundary peaks; inspect the last six intervals.
    peaks = peaks[(grid[peaks] > grid[0] + .5) & (grid[peaks] < grid[-1] - .3)]
    if len(peaks) < 4:
        return None, 'Not enough R peaks'
    if grid[-1] - grid[peaks[-1]] > 2.2:
        return None, 'No recent R peak'
    intervals = np.diff(grid[peaks])[-6:]
    median = np.median(intervals)
    if np.any((intervals < 60/200) | (intervals > 60/35)):
        return None, 'Intervals outside estimator range'
    if np.max(np.abs(intervals - median)) > median * .25:
        return None, 'Unstable signal / irregular intervals'
    return float(60 / median), 'ECG R-peak estimate (not clinically validated)'


def autoscale_wave(ax, values, min_pad):
    """Fit visible samples with padding."""
    low, high = float(min(values)), float(max(values))
    padding = max(min_pad, (high - low) * .12)
    ax.set_ylim(low - padding, high + padding)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host', default='ecall.local')
    p.add_argument('--user', default='ecall')
    p.add_argument('--port', default='/dev/ttyUSB0')
    p.add_argument('--camera', default='/dev/video0')
    p.add_argument('--width', type=int, default=1280)
    p.add_argument('--height', type=int, default=720)
    p.add_argument('--fps', type=int, default=30)
    p.add_argument('--output', type=Path, default=Path(__file__).resolve().parent / 'recordings')
    args = p.parse_args()
    if min(args.width, args.height, args.fps) <= 0:
        p.error('width, height and fps must be positive')
    import cv2
    import numpy as np
    import paramiko
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation

    folder = args.output / datetime.now().strftime('SESSION_%Y%m%d_%H%M%S_%f')
    folder.mkdir(parents=True, exist_ok=False)
    meta = dict(host=args.host, requested_resolution=[args.width, args.height],
                nominal_video_fps=args.fps, start_laptop_utc=datetime.now(timezone.utc).isoformat(),
                timing='Pi read-completion timestamps; USB/camera buffering delay is uncalibrated. No ADC or exposure timestamps.',
                video_timing='AVI constant FPS. Use frames.csv times for alignment, not frame_index / FPS.',
                arduino='ECG:<0..1023> TAB PPG:<0..1023>; PPG ignored (sensor disconnected). No sample counter, so serial loss cannot be proven absent.',
                ecg_mv=f'ecg_raw * {ADC_REF_MV} / 1023 (assumes 5 V USB supply; Pi USB may give ~4.3-4.7 V). 0 = lead-off.',
                heart_rate=f'Display-only ECG R-peak interval estimate after {MAINS_HZ}/{2 * MAINS_HZ} Hz notch + 0.5-40 Hz band-pass; raw CSV unchanged. Range 35-200 BPM; not diagnostic.',
                status='starting')
    def save_meta():
        (folder / 'session.json').write_text(json.dumps(meta, indent=2), encoding='utf-8')
    save_meta()
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    known = Path.home() / '.ssh' / 'known_hosts'
    if known.exists():
        client.load_host_keys(str(known))
    # Reject unknown/changed keys; first verify through Windows ssh.
    stop = threading.Event()
    done = threading.Event()
    display_lock = threading.Lock()
    bio_display = deque(maxlen=30000)
    latest_frame = [None]
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
                bw.writerow(['sample_index', 'pi_elapsed_s', 'pi_monotonic_ns', 'pi_utc_ns', 'laptop_receive_utc', 'ecg_raw', 'ecg_mv'])
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
                        status[0] = 'Waiting for camera and ECG'
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
                            mv = info['ecg'] * ADC_REF_MV / 1023
                            bw.writerow(row + [info['ecg'], f'{mv:.1f}'])
                            with display_lock:
                                bio_display.append((elapsed, mv))
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
                            with display_lock:
                                latest_frame[0] = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
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
        print('Saving on LAPTOP:', folder)
        print('Close the dashboard to stop. Allow the final saving message before closing VS Code.')
        fig = plt.figure(figsize=(14, 9))
        grid = fig.add_gridspec(3, 2)
        ax_cam = fig.add_subplot(grid[:, 0])
        ax_ecg = fig.add_subplot(grid[0, 1])
        ax_filt = fig.add_subplot(grid[1, 1], sharex=ax_ecg)
        ax_hr = fig.add_subplot(grid[2, 1])
        ax_hr.set_title('ECG heart rate')
        ax_hr.axis('off')
        hr_text = ax_hr.text(.5, .5, '-- BPM', ha='center', va='center', fontsize=44,
                             fontweight='bold', color='#d25c25', transform=ax_hr.transAxes)
        last_hr_update = [-float('inf')]
        last_scale = [-float('inf')]
        image = ax_cam.imshow(np.zeros((args.height, args.width, 3), dtype=np.uint8))
        ax_cam.set_title('Camera'); ax_cam.axis('off')
        ecg_line, = ax_ecg.plot([], [], color='#168b65', linewidth=1)
        filt_line, = ax_filt.plot([], [], color='#9b4ed0', linewidth=1)
        ax_ecg.set(title='ECG raw (mV)', ylabel='mV', ylim=(0, ADC_REF_MV), xlim=(0, 5))
        ax_filt.set(title=f'ECG filtered ({MAINS_HZ}/{2 * MAINS_HZ} Hz notch + 0.5-40 Hz, display only)',
                    xlabel='Pi read time (s)', ylabel='mV', ylim=(-500, 500))
        for ax in (ax_ecg, ax_filt):
            ax.grid(alpha=0.25)
        def update(_):
            with display_lock:
                rows = list(bio_display)
                frame = latest_frame[0]
            if rows:
                all_data = np.asarray(rows)
                data = all_data[all_data[:, 0] >= all_data[-1, 0] - 5]
                now = time.monotonic()
                stale = last_seen['bio'] is None or now - last_seen['bio'] > 2
                if now - last_hr_update[0] >= 1:
                    bpm, _ = (None, 'No fresh data') if stale else estimate_ecg_bpm(all_data)
                    last_hr_update[0] = now
                    hr_text.set_text('-- BPM' if bpm is None else f'{bpm:.0f} BPM')
                ecg_line.set_data(data[:, 0], data[:, 1])
                # Filter a longer span than shown so the visible part is past the edge transient
                span = all_data[all_data[:, 0] >= all_data[-1, 0] - 10]
                filtered = None
                if len(span) > 500 and span[-1, 0] > span[0, 0]:
                    fs = (len(span) - 1) / (span[-1, 0] - span[0, 0])
                    filtered = clean_ecg(span[:, 1], fs)
                    filt_line.set_data(span[:, 0], filtered)
                if now - last_scale[0] >= 1:
                    autoscale_wave(ax_ecg, data[:, 1], 50)
                    if filtered is not None:
                        autoscale_wave(ax_filt, filtered[span[:, 0] >= data[0, 0]], 50)
                    last_scale[0] = now
                ax_ecg.set_xlim(max(0, data[-1, 0]-5), max(5, data[-1, 0]))
            if frame is not None:
                image.set_data(frame)
            ages = ' | '.join(f'{k}: {counts[k]} (age {time.monotonic()-v:.1f}s)' if v is not None else f'{k}: waiting' for k, v in last_seen.items())
            fig.suptitle(status[0] + '\n' + ages, fontsize=10)
            return image, ecg_line, filt_line, hr_text
        animation = FuncAnimation(fig, update, interval=100, cache_frame_data=False)
        fig.tight_layout(rect=(0, 0, 1, .92), h_pad=2.5)
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
