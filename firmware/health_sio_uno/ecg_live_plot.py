"""Live ECG plot from the Uno (health_sio_uno.ino) over USB serial.

Usage: py ecg_live_plot.py COM5
Close the window to stop. Close the Arduino IDE serial monitor first.
"""
import re
import sys
import threading
import time
from collections import deque

from scp_monitor import FrameDecoder, parse

WINDOW_S = 5
MAINS_HZ = 60  # Korea
ADC_REF_MV = 5000  # Uno/Nano default analog reference


def clean_ecg(values, fs):
    """Display-only cleanup: mains notch (60/120 Hz) + 0.5-40 Hz band-pass.

    Raw mV stays untouched; the report keeps filtering on the APP side.
    """
    import numpy as np
    from scipy.signal import butter, filtfilt, iirnotch, sosfiltfilt
    y = np.asarray(values, dtype=float)
    for f0 in (MAINS_HZ, 2 * MAINS_HZ):
        if f0 < fs / 2:
            b, a = iirnotch(f0, 30, fs)
            y = filtfilt(b, a, y)
    return sosfiltfilt(butter(2, [0.5, 40], btype='band', fs=fs, output='sos'), y)


def main():
    import serial
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation

    port = sys.argv[1] if len(sys.argv) > 1 else 'COM5'
    ser = serial.Serial(port, 115200, timeout=0.2)
    lock = threading.Lock()
    samples = deque(maxlen=250 * 30)  # (tick_s, mV)
    state = dict(lead_off=None, rate=250, gaps=0, bad=0, blocks=0, last_rx=None, mode='SCP')
    stop = threading.Event()

    def text_reader(pending):
        # Legacy firmware: "ECG:<0..1023>\tPPG:<0..1023>" lines, 250 Hz, no tick
        pattern = re.compile(rb'ECG:(\d+)\s+PPG:(\d+)')
        buf, n = pending, 0
        while not stop.is_set():
            buf += ser.read(512)
            *lines, buf = buf.split(b'\n')
            with lock:
                state['mode'] = 'text'
                for line in lines:
                    match = pattern.fullmatch(line.strip())
                    if not match:
                        state['bad'] += len(line)
                        continue
                    samples.append((n / state['rate'], int(match.group(1)) * ADC_REF_MV / 1023))
                    n += 1
                    state['last_rx'] = time.monotonic()
                state['blocks'] = n // 25

    def reader():
        decoder = FrameDecoder()
        last_seq = None
        # Opening the port resets the board; sniff the format once data flows
        first, deadline = b'', time.monotonic() + 4
        while len(first) < 128 and time.monotonic() < deadline and not stop.is_set():
            first += ser.read(512)
        if b'ECG:' in first:
            text_reader(first)
            return
        pending = first
        while not stop.is_set():
            data, pending = pending + ser.read(512), b''
            for cid, payload in decoder.feed(data):
                msg = parse(cid, payload)
                with lock:
                    state['bad'] = decoder.bad
                    state['last_rx'] = time.monotonic()
                    if msg['kind'] == 'ecg':
                        if last_seq is not None and msg['seq'] != (last_seq + 1) & 0xFFFF:
                            state['gaps'] += 1
                        last_seq = msg['seq']
                        state['blocks'] += 1
                        dt = 1 / state['rate']
                        for i, mv in enumerate(msg['mv']):
                            samples.append((msg['tick'] / 1000 + i * dt, mv))
                    elif msg['kind'] == 'status_ecg':
                        state['rate'] = msg['rate'] or 250
                        state['lead_off'] = bool(msg['extra'])
                    elif msg['kind'] == 'event' and msg['event'] == 'lead_off':
                        state['lead_off'] = bool(msg['state'])

    threading.Thread(target=reader, daemon=True).start()
    print(f'Reading {port} at 115200 baud. Close the window to stop.')

    fig, (ax, ax_f) = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
    line, = ax.plot([], [], color='#168b65', linewidth=1)
    ax.set(ylabel='Raw ECG (mV)')
    ax.grid(alpha=.25)
    line_f, = ax_f.plot([], [], color='#d25c25', linewidth=1)
    ax_f.set(xlabel='Arduino time (s)', ylabel='Filtered (mV)',
             title=f'{MAINS_HZ}/{2 * MAINS_HZ} Hz notch + 0.5-40 Hz band-pass (display only)')
    ax_f.grid(alpha=.25)

    def update(_):
        with lock:
            data = list(samples)
            info = dict(state)
        flat = False
        if data:
            t_end = data[-1][0]
            view = [(t, v) for t, v in data if t >= t_end - WINDOW_S]
            ts, vs = zip(*view)
            line.set_data(ts, vs)
            ax.set_xlim(t_end - WINDOW_S, t_end)
            low, high = min(vs), max(vs)
            pad = max(20, (high - low) * .15)
            ax.set_ylim(low - pad, high + pad)
            flat = high - low < 30
            # Filter a longer span than shown so the visible part is past the edge transient
            span = [(t, v) for t, v in data if t >= t_end - 2 * WINDOW_S]
            if len(span) >= 2 * info['rate']:
                ft = [t for t, _ in span]
                fv = clean_ecg([v for _, v in span], info['rate'])
                line_f.set_data(ft, fv)
                shown = fv[[i for i, t in enumerate(ft) if t >= t_end - WINDOW_S]]
                fpad = max(5, (shown.max() - shown.min()) * .15)
                ax_f.set_ylim(shown.min() - fpad, shown.max() + fpad)
        if info['last_rx'] is None:
            status = 'Waiting for data...'
        elif time.monotonic() - info['last_rx'] > 2:
            status = 'No data for 2 s (check USB / serial monitor)'
        elif info['lead_off']:
            status = 'LEAD OFF - check electrodes'
        elif flat:
            status = 'Signal flat - check electrode contact / wiring'
        else:
            status = 'Measuring'
        ax.set_title(f'ECG live ({port}, {info["mode"]})  |  {status}\n'
                     f'blocks {info["blocks"]}  seq gaps {info["gaps"]}  bad bytes {info["bad"]}', fontsize=10)
        return line, line_f

    animation = FuncAnimation(fig, update, interval=100, cache_frame_data=False)
    try:
        plt.show()
    finally:
        stop.set()
        time.sleep(0.3)
        ser.close()


if __name__ == '__main__':
    main()
