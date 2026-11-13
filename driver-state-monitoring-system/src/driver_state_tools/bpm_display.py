"""Display-only rate values. Recorded Nano vitals NEVER enter BioSignalMonitor or fusion."""
from __future__ import annotations

import bisect
import csv
import math
from pathlib import Path

from biosignal.config import Config


def bpm_range(bpm):
    """The received overlay's instantaneous number-color bands, not its final bio grade."""
    if bpm is None or not math.isfinite(bpm):
        return 'unavailable'
    cfg=Config()
    if bpm <= cfg.hr_extreme_low or bpm >= cfg.hr_danger_high:
        return 'danger'
    if bpm <= cfg.hr_caution_low or bpm > cfg.hr_caution_high:
        return 'caution'
    return 'normal'


def assessed_rate_caption(bio):
    """Show the rate actually selected by BioSignalMonitor separately from display peaks."""
    hr=bio.get('hr')
    if not bio.get('result_fresh') or hr is None or not math.isfinite(hr):
        return 'Assessed HR: --'
    for name in ('ecg','ppg'):
        channel=bio.get('channels',{}).get(name,{})
        if channel.get('reliable') and channel.get('hr')==hr:
            return f'Assessed HR: {hr:.0f} ({name.upper()})'
    return f'Assessed HR: {hr:.0f}'


class RecordedVitals:
    def __init__(self, path, stale_seconds=None):
        self.path = Path(path)
        self.stale_seconds = Config().stale_s if stale_seconds is None else stale_seconds
        self.rows, self.times = [], []
        if not self.path.exists():
            return
        with self.path.open(newline='') as f:
            reader=csv.DictReader(f)
            if not {'clip_s','hr_bpm','hr_src'}.issubset(reader.fieldnames or []):
                raise ValueError('Recorded vitals need clip_s, hr_bpm and hr_src')
            for row in reader:
                t=float(row['clip_s'])
                if not math.isfinite(t) or (self.times and t<=self.times[-1]):
                    raise ValueError('Recorded vital timestamps must increase')
                try:
                    bpm, source=float(row['hr_bpm']), int(row['hr_src'])
                except (TypeError,ValueError):
                    bpm,source=None,0
                valid=(bpm is not None and math.isfinite(bpm) and bpm>0 and source in (1,2,3,4))
                self.times.append(t)
                self.rows.append(dict(timestamp=t,bpm=bpm if valid else None,hr_src=source))

    def at(self, signal_t):
        if not math.isfinite(signal_t):raise ValueError('Signal time must be finite')
        index=bisect.bisect_right(self.times,signal_t)-1
        if index<0:
            return dict(bpm=None,available=False,timestamp=None,age_seconds=None,hr_src=0)
        row=self.rows[index]
        age=signal_t-row['timestamp']
        fresh=0<=age<=self.stale_seconds
        return dict(row,age_seconds=age,available=bool(fresh and row['bpm'] is not None),
                    bpm=row['bpm'] if fresh else None)


def select_display_bpm(bio, recorded=None):
    """Display estimator is separate from the monitor's assessed HR and final grade."""
    pulse = bio.get('display_pulse')
    if pulse and bio.get('result_fresh'):
        for name in ('ecg', 'ppg'):
            reading = pulse.get(name, {})
            bpm = reading.get('bpm')
            if bpm is not None and math.isfinite(bpm) and bpm > 0:
                channel=bio.get('channels',{}).get(name,{})
                assessed=channel.get('hr')
                # A classifier can trust a channel's grade without being able to measure its BPM.
                # This agreement flag never changes the received monitor's grade or timers.
                verified=bool(channel.get('reliable') and assessed is not None and math.isfinite(assessed)
                              and abs(assessed-bpm)<=Config().hr_mismatch_bpm)
                return dict(bpm=bpm, source='recent_'+name+'_peaks',
                            source_timestamp=pulse['timestamp'], used_for_grade=False,
                            verified_by_monitor=verified,
                            last_peak_timestamp=reading.get('last_peak_timestamp'))
        # Do not keep showing a historic assessed rate when current peaks have disappeared.
        if any(pulse.get(n,{}).get('status') == 'no_recent_peaks' for n in ('ecg','ppg')):
            return dict(bpm=None,source=None,source_timestamp=None,used_for_grade=False)
    hr=bio.get('hr')
    if bio.get('result_fresh') and hr is not None and math.isfinite(hr):
        return dict(bpm=hr,source='analysis',source_timestamp=bio.get('t'))
    if recorded and recorded.get('available'):
        return dict(bpm=recorded['bpm'],source='recorded',source_timestamp=recorded['timestamp'])
    return dict(bpm=None,source=None,source_timestamp=None)
