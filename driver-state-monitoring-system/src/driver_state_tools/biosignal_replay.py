"""Causal replay of supplied ECG/PPG samples. No stored grades or scenario labels as inputs."""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from .biosignal_startup import load_startup_profile
from .recent_pulse import RecentPulseRates

import numpy as np


class BiosignalReplay:
    def __init__(self, folder, monitor, *, offset_seconds=0., update_seconds=1., stale_seconds=1.5,
                 display_peak_rates=False, display_confirmation_s=.08):
        self.folder = Path(folder)
        self.session = json.loads((self.folder / 'session.json').read_text())
        self.synthetic = self.session.get('status') == 'synthetic' or bool(self.session.get('synthetic'))
        with (self.folder / 'biosignals.csv').open(newline='') as f:
            rows = list(csv.DictReader(f))
        if not rows or 'clip_s' not in rows[0]:
            raise ValueError('biosignals.csv needs explicit clip_s, ecg_raw, ppg_raw columns')
        self.samples = np.array([[float(r[k]) for k in ('clip_s', 'ecg_raw', 'ppg_raw')] for r in rows])
        if not np.isfinite(self.samples).all() or np.any(np.diff(self.samples[:, 0]) <= 0):
            raise ValueError('Biosignal times must be finite and strictly increasing')
        if not math.isfinite(offset_seconds) or update_seconds <= 0 or stale_seconds <= 0:
            raise ValueError('Invalid clock configuration')
        # bio clip_s = video seconds + offset. A positive offset skips earlier bio data.
        # No rescaling of the clock and no interpolation through gaps.
        self.offset = offset_seconds
        self.interval, self.stale = update_seconds, stale_seconds
        self.monitor = monitor
        self.cursor = 0
        self.last_video_t = -math.inf
        self.next_update = self.samples[0, 0] + update_seconds
        self.latest = None
        self.history = []
        self.display_rates = RecentPulseRates(self.samples, confirmation_s=display_confirmation_s) if display_peak_rates else None
        cfg=getattr(monitor,'cfg',None)
        original,_=load_startup_profile()
        self.startup_settings=dict(calibration_s=cfg.calibration_s if cfg else original.calibration_s,
            min_ready_s=cfg.min_ready_s if cfg else original.min_ready_s,
            calibration_min_readings=cfg.calibration_min_readings if cfg else original.calibration_min_readings)

    def advance(self, video_t):
        if not math.isfinite(video_t) or video_t < self.last_video_t:
            raise ValueError('Use a new replay instance after seeking backwards or changing clips')
        self.last_video_t = video_t
        target = video_t + self.offset
        while self.cursor < len(self.samples) and self.samples[self.cursor, 0] <= target + 1e-9:
            t, ecg, ppg = self.samples[self.cursor]
            self.monitor.push_ecg(float(t), float(ecg))
            self.monitor.push_ppg(float(t), float(ppg))
            self.cursor += 1
            if t >= self.next_update:
                self.latest = self.monitor.update(float(t)).to_dict()
                self.history.append(self.latest)
                # Same fixed 1 Hz replay schedule as the supplied tools/replay_recording.py.
                self.next_update += self.interval
        status = dict(self.latest) if self.latest else dict(t=None, level='unavailable', reasons=['COLLECTING_SAMPLES'], channels={})
        status['model_reported_level'] = status['level']
        # The received monitor owns the grade, reason codes and missing BPM semantics.
        # Never replace its Normal/Caution/Danger based on an extra quality or BPM rule.
        sample_t = float(self.samples[self.cursor - 1, 0]) if self.cursor else None
        result_age = target - status['t'] if status.get('t') is not None else None
        sample_age = target - sample_t if sample_t is not None else None
        # No extrapolation past EOF. No borrowed future sample or future result.
        out_of_range = target < self.samples[0, 0] or target > self.samples[-1, 0]
        stale = result_age is None or result_age > self.stale or sample_age is None or sample_age > self.stale
        if out_of_range or stale:
            status.update(level='unavailable', hr=None, reasons=['OUTSIDE_RECORDING' if out_of_range else 'SIGNAL_STALE'])
        status.update(available=status['level'] != 'unavailable', synthetic=self.synthetic,
                      sample_timestamp=sample_t, sample_age_seconds=sample_age,
                      age_seconds=result_age, timeline_seconds=target, out_of_range=bool(out_of_range),
                      result_fresh=not (out_of_range or stale))
        status['startup_settings']=dict(self.startup_settings)
        if self.display_rates is not None and status['result_fresh']:
            status['display_pulse'] = self.display_rates.at(target)
        return status

    def waveform(self, video_t, seconds=5.):
        target = video_t + self.offset
        a = np.searchsorted(self.samples[:, 0], target - seconds, side='left')
        b = min(self.cursor, np.searchsorted(self.samples[:, 0], target, side='right'))
        return self.samples[a:b].copy()

    def close(self):
        self.monitor.close()


def validate_frame_clock(folder, frame_count, fps, sync_basis):
    """Only synthetic frame tables map 1:1. Measured N was explicitly resampled by Pi time."""
    with (Path(folder) / 'frames.csv').open(newline='') as f:
        times = [float(r['clip_s']) for r in csv.DictReader(f)]
    if not times or not all(math.isfinite(t) for t in times) or any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError('Invalid frames.csv clock')
    if sync_basis == 'synthetic_clip_clock':
        if len(times) != frame_count or max(abs(t - i / fps) for i, t in enumerate(times)) > .02:
            raise ValueError('Synthetic frame clock does not match this source video')
    elif sync_basis == 'measured_resampled_pi_clock':
        session = json.loads((Path(folder) / 'session.json').read_text())
        if session.get('status') != 'complete' or session.get('video_frames') != frame_count or abs(session.get('video_fps', 0) - fps) > .01:
            raise ValueError('Measured recording resampling metadata does not match video')
    else:
        raise ValueError('Explicit supported synchronization basis required')
    return dict(basis=sync_basis, camera_timestamps=len(times), video_frames=frame_count,
                acquisition_delay_calibrated=False,
                note='Synthetic replay alignment, not simultaneous acquisition' if sync_basis.startswith('synthetic') else
                     'Video resampled by Pi receipt clock; ADC and exposure delay uncalibrated')
