"""Nano(ppg_ecg_nano.ino) ECG + PPG 실시간 화면.

사용: py ecg_ppg_live_view.py COM6 [기록.csv]
창을 닫으면 끝. Arduino IDE 시리얼 모니터/플로터는 먼저 닫을 것 (포트를 같이 못 씀).

  왼쪽 위: ECG 파형 (60Hz 노치 + 0.5~40Hz, 화면 표시용)
  왼쪽 가운데: PPG 파형 (0.7~4Hz, 화면 표시용)
  왼쪽 아래: 심박 기록 (ECG / PPG / 융합)
  오른쪽: 융합 심박과 출처, ECG·PPG 각각의 BPM 과 상태
BPM 계산과 융합은 모두 Nano 가 한다. 이 화면은 받은 값을 보여주고 CSV 로 저장만 한다.
"""
import csv
import re
import sys
import threading
import time
from collections import deque
from datetime import datetime

WINDOW_S = 8
HISTORY_S = 120
FS = 250
MAINS_HZ = 60  # 한국

ECG_STATE = {0: '전극 확인', 1: '찾는 중', 2: '측정 중'}
PPG_STATE = {0: '손가락 없음', 1: '찾는 중', 2: '측정 중'}
HR_SRC = {0: '측정 안 됨', 1: 'ECG', 2: 'PPG', 3: 'ECG + PPG', 4: 'ECG (PPG 와 불일치)'}
STATE_COLOR = {0: '#888888', 1: '#d08000', 2: '#1a8f3a'}
ECG_COLOR, PPG_COLOR, HR_COLOR = '#c03030', '#2050c0', '#1a8f3a'

RAW = re.compile(r'ECG:(\d+)\tPPG:(\d+)')
STATUS = re.compile(r'PPG_BPM:([\d.]+)\tPPG_STATE:(\d)\tPPG_PP:(-?\d+)\tECG_BPM:([\d.]+)\tECG_STATE:(\d)'
                    r'\tHR_BPM:([\d.]+)\tHR_SRC:(\d)\tDROP:(\d+)')


def main():
    import numpy as np
    import serial
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation
    from scipy.signal import butter, iirnotch, sosfiltfilt, filtfilt

    plt.rcParams['font.family'] = 'Malgun Gothic'
    plt.rcParams['axes.unicode_minus'] = False

    port = sys.argv[1] if len(sys.argv) > 1 else 'COM6'
    log_path = sys.argv[2] if len(sys.argv) > 2 else f'vitals_log_{datetime.now():%Y%m%d_%H%M%S}.csv'
    ser = serial.Serial(port, 115200, timeout=0.2)
    time.sleep(2)  # 포트를 열면 Nano 가 리셋됨
    ser.reset_input_buffer()

    ecg = deque(maxlen=WINDOW_S * FS)
    ppg = deque(maxlen=WINDOW_S * FS)
    hist = deque()  # (t, ecg_bpm, ppg_bpm, hr_bpm)
    st = {'ppg_bpm': 0.0, 'ppg_state': 0, 'ppg_pp': 0, 'ecg_bpm': 0.0, 'ecg_state': 1,
          'hr_bpm': 0.0, 'hr_src': 0, 'drop': 0}
    lock = threading.Lock()
    stop = threading.Event()
    t0 = time.monotonic()

    def reader():
        with open(log_path, 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(['t_s', 'kind', 'ecg', 'ppg', 'ppg_bpm', 'ppg_state', 'ppg_pp',
                        'ecg_bpm', 'ecg_state', 'hr_bpm', 'hr_src', 'drop'])
            while not stop.is_set():
                try:
                    line = ser.readline().decode('ascii', errors='replace').strip()
                except serial.SerialException:
                    return
                t = time.monotonic() - t0
                m = RAW.fullmatch(line)
                if m:
                    e, p = int(m.group(1)), int(m.group(2))
                    with lock:
                        ecg.append(e)
                        ppg.append(p)
                    w.writerow([f'{t:.4f}', 'raw', e, p] + [''] * 8)
                    continue
                m = STATUS.fullmatch(line)
                if m:
                    v = dict(ppg_bpm=float(m.group(1)), ppg_state=int(m.group(2)), ppg_pp=int(m.group(3)),
                             ecg_bpm=float(m.group(4)), ecg_state=int(m.group(5)),
                             hr_bpm=float(m.group(6)), hr_src=int(m.group(7)), drop=int(m.group(8)))
                    with lock:
                        st.update(v)
                        hist.append((t, v['ecg_bpm'], v['ppg_bpm'], v['hr_bpm']))
                        while hist and t - hist[0][0] > HISTORY_S:
                            hist.popleft()
                    w.writerow([f'{t:.4f}', 'status', '', ''] + list(v.values()))
                    f.flush()

    threading.Thread(target=reader, daemon=True).start()

    fig = plt.figure(figsize=(13, 8))
    fig.canvas.manager.set_window_title(f'ECG + PPG 측정 - {port}')
    gs = fig.add_gridspec(3, 4)
    ax_ecg = fig.add_subplot(gs[0, :3])
    ax_ppg = fig.add_subplot(gs[1, :3])
    ax_hist = fig.add_subplot(gs[2, :3])
    ax_info = fig.add_subplot(gs[:, 3])

    xs = np.arange(-WINDOW_S * FS, 0) / FS
    (l_ecg,) = ax_ecg.plot([], [], lw=1, color=ECG_COLOR)
    (l_ppg,) = ax_ppg.plot([], [], lw=1.2, color=PPG_COLOR)
    (h_ecg,) = ax_hist.plot([], [], lw=1, color=ECG_COLOR, alpha=0.6, label='ECG')
    (h_ppg,) = ax_hist.plot([], [], lw=1, color=PPG_COLOR, alpha=0.6, label='PPG')
    (h_hr,) = ax_hist.plot([], [], lw=2.2, color=HR_COLOR, label='융합')
    ax_ecg.set(xlim=(-WINDOW_S, 0), title=f'ECG ({MAINS_HZ}Hz 노치 + 0.5~40Hz, 화면 표시용)')
    ax_ppg.set(xlim=(-WINDOW_S, 0), title='PPG 맥박 (0.7~4Hz, 화면 표시용)')
    ax_hist.set(xlim=(-HISTORY_S, 0), ylim=(40, 180), title='심박 기록 (Nano 계산값)', xlabel='초', ylabel='BPM')
    ax_hist.legend(loc='upper left', ncol=3, fontsize=9)
    for ax in (ax_ecg, ax_ppg, ax_hist):
        ax.grid(alpha=0.3)
    ax_info.axis('off')
    ax_info.text(0.5, 0.97, '심박 (융합)', ha='center', va='top', fontsize=15)
    t_hr = ax_info.text(0.5, 0.83, '--', ha='center', va='center', fontsize=64, weight='bold', color=HR_COLOR)
    ax_info.text(0.5, 0.71, 'BPM', ha='center', va='center', fontsize=16)
    t_src = ax_info.text(0.5, 0.64, '', ha='center', va='center', fontsize=13)
    ax_info.text(0.5, 0.52, 'ECG', ha='center', va='center', fontsize=14, color=ECG_COLOR, weight='bold')
    t_ecg = ax_info.text(0.5, 0.45, '--', ha='center', va='center', fontsize=30, color=ECG_COLOR)
    t_ecg_st = ax_info.text(0.5, 0.39, '', ha='center', va='center', fontsize=12)
    ax_info.text(0.5, 0.29, 'PPG', ha='center', va='center', fontsize=14, color=PPG_COLOR, weight='bold')
    t_ppg = ax_info.text(0.5, 0.22, '--', ha='center', va='center', fontsize=30, color=PPG_COLOR)
    t_ppg_st = ax_info.text(0.5, 0.16, '', ha='center', va='center', fontsize=12)
    t_foot = ax_info.text(0.5, 0.04, '', ha='center', va='center', fontsize=10, color='#555555')

    nb, na = iirnotch(MAINS_HZ, 10, FS)
    ecg_sos = butter(2, [0.5, 40], btype='band', fs=FS, output='sos')
    ppg_sos = butter(2, [0.7, 4], btype='band', fs=FS, output='sos')

    def set_wave(ax, line, y):
        x = xs[-y.size:]
        line.set_data(x, y)
        a = max(5.0, np.abs(y[FS // 2:]).max() * 1.15)
        ax.set_ylim(-a, a)

    def update(_):
        with lock:
            e = np.array(ecg, dtype=float)
            p = np.array(ppg, dtype=float)
            s = dict(st)
            h = np.array(hist) if hist else np.zeros((0, 4))
        if e.size > 3 * FS:
            set_wave(ax_ecg, l_ecg, sosfiltfilt(ecg_sos, filtfilt(nb, na, e)))
            set_wave(ax_ppg, l_ppg, sosfiltfilt(ppg_sos, p))
        t_hr.set_text(f"{s['hr_bpm']:.0f}" if s['hr_src'] else '--')
        t_src.set_text('출처: ' + HR_SRC.get(s['hr_src'], '?'))
        t_ecg.set_text(f"{s['ecg_bpm']:.0f}" if s['ecg_state'] == 2 else '--')
        t_ecg_st.set_text(ECG_STATE.get(s['ecg_state'], '?'))
        t_ecg_st.set_color(STATE_COLOR.get(s['ecg_state'], 'k'))
        t_ppg.set_text(f"{s['ppg_bpm']:.0f}" if s['ppg_state'] == 2 else '--')
        t_ppg_st.set_text(f"{PPG_STATE.get(s['ppg_state'], '?')}  (박동 진폭 {s['ppg_pp']})")
        t_ppg_st.set_color(STATE_COLOR.get(s['ppg_state'], 'k'))
        t_foot.set_text(f"데이터 유실 {s['drop']}")
        if h.size:
            x = h[:, 0] - (time.monotonic() - t0)
            for line, col in ((h_ecg, 1), (h_ppg, 2), (h_hr, 3)):
                line.set_data(x, np.where(h[:, col] > 0, h[:, col], np.nan))
        return l_ecg, l_ppg, h_ecg, h_ppg, h_hr, t_hr, t_src, t_ecg, t_ecg_st, t_ppg, t_ppg_st, t_foot

    _anim = FuncAnimation(fig, update, interval=100, cache_frame_data=False)
    fig.tight_layout()
    print(f'기록: {log_path}')
    try:
        plt.show()
    finally:
        stop.set()
        ser.close()


if __name__ == '__main__':
    main()
