"""Display estimates from the received _overlay.py, using only samples received so far.

The source overlay uses whole-file filtfilt and scenario metadata to label VF/force zero.
Those two shortcuts are not used here. These rates NEVER replace BioSignalMonitor.hr/level.
"""
from __future__ import annotations

import math
import numpy as np
from scipy import signal


def bpm_at(beats, now):
    """Received overlay formula: mean of latest two RR intervals, with its peak timeout."""
    past = beats[beats <= now]
    if len(past) < 2:
        return None
    rr = np.diff(past[-3:])
    if now - past[-1] > max(1.2, 2.5 * rr[-1]):
        return 0.0
    return 60.0 / rr.mean()


class RecentPulseRates:
    """Same 250 Hz display filters and peak settings; prefix-only, independent of grade.

    confirmation_s is a technical filter-boundary guard, not a medical threshold.
    The received first-3.5-second amplitude reference uses only its observed prefix.
    """
    def __init__(self, samples, *, confirmation_s=.08, fs=250.):
        self.samples = np.asarray(samples, dtype=float)
        if self.samples.ndim != 2 or self.samples.shape[1] != 3 or not len(self.samples):
            raise ValueError('Need time, raw ECG and raw PPG samples')
        if not np.isfinite(self.samples).all() or np.any(np.diff(self.samples[:, 0]) <= 0):
            raise ValueError('Pulse sample times must increase and values must be finite')
        if not math.isfinite(confirmation_s) or confirmation_s < 0 or fs <= 120:
            raise ValueError('Invalid display filter settings')
        self.confirmation_s, self.fs = confirmation_s, fs

    def at(self, now):
        if not math.isfinite(now):
            raise ValueError('Display time must be finite')
        part = self.samples[self.samples[:, 0] <= now]
        out = dict(timestamp=now, method='received_overlay_two_rr_prefix',
                   confirmation_seconds=self.confirmation_s, used_for_grade=False,
                   last_sample_timestamp=float(part[-1, 0]) if len(part) else None,
                   ecg=dict(bpm=None, status='collecting'), ppg=dict(bpm=None, status='collecting'))
        if len(part) < self.fs or part[-1, 0] - part[0, 0] < 1.:
            return out
        t, e, p = part.T
        b, a = signal.iirnotch(60., 6., fs=self.fs)
        def band(x, lo, hi):
            return signal.sosfiltfilt(signal.butter(2, [lo, hi], 'band', fs=self.fs, output='sos'), x)
        qrs = band(signal.filtfilt(b, a, e), 5., 30.)
        pulse = band(p, .5, 5.)
        calm = t < 3.5
        if not np.any(calm):
            return out
        r_pk, _ = signal.find_peaks(qrs, height=.45 * np.percentile(qrs[calm], 99.5),
                                     distance=int(.2 * self.fs))
        p_pk, _ = signal.find_peaks(pulse, prominence=.4 * np.percentile(pulse[calm], 99),
                                     distance=int(.3 * self.fs))
        for name, peaks in [('ecg', r_pk), ('ppg', p_pk)]:
            beats = t[peaks]
            beats = beats[beats <= now - self.confirmation_s]
            value = bpm_at(beats, now)
            # Timeout means no recent detected peaks, not a diagnosis or a measured 0 BPM.
            out[name] = dict(bpm=float(value) if value is not None and value > 0 else None,
                             status='collecting' if value is None else
                                    'no_recent_peaks' if value == 0 else 'estimated',
                             last_peak_timestamp=float(beats[-1]) if len(beats) else None)
        return out
