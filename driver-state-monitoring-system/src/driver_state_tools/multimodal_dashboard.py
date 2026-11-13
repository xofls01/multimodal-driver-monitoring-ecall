"""Keep the existing vision timers and add a separate, causal sensor panel."""
from __future__ import annotations

import cv2
import numpy as np
from PIL import Image, ImageDraw
from .evidence_dashboard import font, PALETTE
from biosignal.config import Config as BiosignalConfig
from biosignal.dashboard import filtered_waves
from .bpm_display import select_display_bpm, bpm_range

MEASURING_COLOR = (92, 199, 246)


def measurement_display(bio):
    """Presentation only: no new state is sent to the fusion engine or Unity."""
    reasons = set(bio.get('reasons', []))
    elapsed = max(0., bio.get('timeline_seconds', 0.))
    baseline = bio.get('startup_settings',{}).get('calibration_s',BiosignalConfig().calibration_s)
    only_startup_reasons = reasons <= {'CALIBRATING', 'COLLECTING_SAMPLES', 'SIGNAL_STALE', 'OUTSIDE_RECORDING'}
    gathering = (bio.get('t') is None and
        ((bio.get('sample_timestamp') is not None and
          bio.get('sample_age_seconds') is not None and
          0 <= bio['sample_age_seconds'] <= BiosignalConfig().stale_s and not bio.get('out_of_range'))
         or elapsed == 0.))
    measuring = bio['level'] == 'unavailable' and only_startup_reasons and (
        ('CALIBRATING' in reasons and bio.get('result_fresh')) or gathering or
        ('COLLECTING_SAMPLES' in reasons and not bio.get('out_of_range')))
    return dict(measuring=bool(measuring), label='MEASURING...' if measuring else bio['level'].upper(),
        color=MEASURING_COLOR if measuring else PALETTE[bio['level']],
        note=(f'Measuring baseline: {min(elapsed, baseline):.1f} / {baseline:g}s' if 'CALIBRATING' in reasons
              else 'Collecting sensor samples') if measuring else None,
        elapsed_seconds=elapsed, baseline_seconds=baseline)


def bpm_header_display(bio, recorded_vitals=None):
    """The visible top status follows the visible BPM band, not the control decision."""
    rate=select_display_bpm(bio,recorded_vitals)
    state=bpm_range(rate['bpm'])
    name={'recent_ecg_peaks':'ECG','recent_ppg_peaks':'PPG','recorded':'Recorded','analysis':'Analyzed'}.get(rate['source'],'')
    caption=f"{rate['bpm']:.0f} BPM / {name}" if rate['bpm'] is not None else 'No current BPM'
    return dict(label=state.upper(),color=PALETTE[state],caption=caption,
                bpm=rate['bpm'],state=state,used_for_control=False)


def draw_bpm_header(draw,bio,recorded_vitals=None,*,x=560,y=0,clear=False):
    """Shared by the live renderer and display-only review renderer."""
    header=bpm_header_display(bio,recorded_vitals)
    if clear:
        draw.rectangle((x-8,y,x+280,y+75),fill=(9,17,26))
    draw.text((x,y+13),'BIO STATE (BPM)',font=font(11),fill=(151,172,193))
    draw.text((x,y+34),header['label'],font=font(20,True),fill=header['color'])
    draw.text((x,y+61),header['caption'],font=font(10),fill=(151,172,193))
    return header


def display_waveforms(samples, now):
    """Same display filter as the supplied dashboard, limited to already received samples."""
    if len(samples) < 2:
        return np.empty((0, 3))
    past = samples[samples[:, 0] <= now + 1e-9]
    if len(past) < 2:
        return np.empty((0, 3))
    result = filtered_waves(past, window_s=5., margin_s=3.)
    return np.column_stack(result) if result is not None else np.empty((0, 3))


def channel_quality_label(bio, channel):
    c = bio.get('channels', {}).get(channel, {})
    # An uninitialized SQI=0 is not a measured quality score.
    return (f"Signal quality {c['sqi']:.2f}" if bio.get('result_fresh') and c.get('ready')
            and c.get('sqi') is not None else 'Signal quality --')

BIO_REASONS = dict(CALIBRATING='Collecting baseline', COLLECTING_SAMPLES='Collecting samples',
    SIGNAL_STALE='Signal result expired', OUTSIDE_RECORDING='Outside signal recording',
    LOW_SIGNAL_QUALITY='Signal quality too low', SENSOR_LOST='Sensor not readable',
    SENSOR_MISMATCH='ECG and PPG estimates disagree', HEART_PAUSE='No recent peaks in both channels',
    ABNORMAL_ECG_NO_PULSE='ECG model alert with missing PPG peaks', NO_PULSE_CONFIRMED='Missing peaks in both channels',
    NO_PULSE_UNCONFIRMED='Missing PPG peaks; confirmation needed',
    CHAOTIC_ECG_NO_PULSE='Unclear ECG and missing PPG peaks', HOLD_PREVIOUS_LEVEL='Waiting to confirm recovery')


def brief_bio_reason(bio):
    """Short display copy only. The original grade and reason codes are untouched."""
    display = measurement_display(bio)
    if display['measuring']:
        return ''
    if bio['level'] == 'normal':
        return ''
    names = dict(ABNORMAL_ECG_NO_PULSE='ECG alert / PPG pulse not detected',
        NO_PULSE_CONFIRMED='ECG / PPG pulse not detected',
        NO_PULSE_UNCONFIRMED='Pulse signal not detected',
        CHAOTIC_ECG_NO_PULSE='Signal unclear',
        HEART_PAUSE='Pulse signal interrupted',
        LOW_SIGNAL_QUALITY='Signal unclear', SENSOR_LOST='Signal lost',
        SIGNAL_STALE='Signal unavailable', OUTSIDE_RECORDING='Recording ended',
        SENSOR_MISMATCH='ECG / PPG rates differ', SENSOR_CONFLICT='ECG / PPG results differ',
        HR_HIGH='High heart rate', EXTREME_TACHY='High heart rate',
        HR_LOW='Low heart rate', EXTREME_BRADY='Low heart rate',
        HR_RISING='Heart rate rising', HR_FALLING='Heart rate falling',
        IRREGULAR_RHYTHM='Irregular rhythm', MODEL_CAUTION='Heart-rate alert',
        MODEL_DANGER='Heart-rate alert', HOLD_PREVIOUS_LEVEL='Checking recovery')
    return next((names[r] for r in bio.get('reasons', []) if r in names),
                'Signal unavailable' if bio['level'] == 'unavailable' else 'Heart-rate alert')


def draw_multimodal(vision_image, combined, vision, bio, samples, sync, border=8, recorded_vitals=None):
    # Existing annotation has a border; remove that border only, not any camera pixels.
    scene = vision_image[border:-border, border:-border] if border else vision_image
    h, w = scene.shape[:2]
    header, footer = 76, 222
    im = Image.new('RGB', (w, h + header + footer), (9, 17, 26))
    im.paste(Image.fromarray(cv2.cvtColor(scene, cv2.COLOR_BGR2RGB)), (0, header))
    p = ImageDraw.Draw(im)
    white, muted, accent = (236, 243, 250), (151, 172, 193), PALETTE[combined.state.value]
    display = measurement_display(bio)
    def text(s, x, y, size=14, color=white, bold=False, width=None):
        if width:
            while p.textlength(str(s), font=font(size, bold)) > width and size > 9:
                size -= 1
        p.text((x, y), str(s), font=font(size, bold), fill=color)
    text('MULTIMODAL DRIVER MONITOR', 20, 10, 12, muted, True)
    starting = (combined.state.value == 'unavailable' and display['measuring']
                and 'calibrating' in vision.reasons)
    text('PREPARING...' if starting else combined.state.value.upper(), 20, 30, 30,
         MEASURING_COLOR if starting else accent, True)
    text('VISION', 330, 13, 11, muted)
    text('PREPARING...' if vision.state.value == 'unavailable' and 'calibrating' in vision.reasons
         else vision.state.value.upper(), 330, 34, 20, PALETTE[vision.state.value], True)
    draw_bpm_header(p,bio,recorded_vitals)
    badge = 'SYNTHETIC SIGNAL' if bio['synthetic'] else 'RECORDED SIGNAL'
    text(badge, 850, 14, 14, (246, 189, 112) if bio['synthetic'] else (103, 224, 177), True)
    text(f'{combined.timestamp:.2f}s', 850, 39, 14, muted)
    y0 = header + h
    p.line((0, y0, w, y0), fill=(48, 67, 84), width=2)
    graph_w = round(w * .67)
    text('ECG / PPG', 20, y0 + 13, 12, muted, True)
    text('Last 5s', graph_w-75, y0 + 13, 11, muted)
    end = bio['timeline_seconds']
    filtered = display_waveforms(samples, end)
    for ch, label, col, color, row in [('ecg', 'ECG', 1, (255, 112, 132), 0), ('ppg', 'PPG', 2, (79, 192, 244), 1)]:
        yy = y0 + 39 + row * 97
        text(label, 20, yy, 13, color, True)
        left, right, top, bottom = 20, graph_w - 24, yy + 22, yy + 78
        p.rectangle((left, top, right, bottom), fill=(14, 26, 37))
        for tick in range(6):
            xx = left + tick / 5 * (right - left)
            p.line((xx, top, xx, bottom), fill=(29, 44, 57))
        if len(filtered) >= 2:
            values = filtered[:, col]
            lo, hi = float(values.min()), float(values.max())
            pad = max((hi - lo) * .12, 12 if col == 1 else 3)
            lo, hi = lo - pad, hi + pad
            coords = [(left + max(0, min(1, (t - end + 5) / 5)) * (right - left),
                       bottom - (v - lo) / (hi - lo) * (bottom - top)) for t, v in filtered[:, [0, col]]]
            p.line(coords, fill=color, width=2)
        else:
            text('No samples yet', left + 8, top + 12, 12, muted)
    x = graph_w + 18
    p.line((graph_w, y0 + 14, graph_w, y0 + 208), fill=(48, 67, 84))
    rate = select_display_bpm(bio, recorded_vitals)
    recent = str(rate['source']).startswith('recent_')
    text('RECENT PULSE RATE' if bio.get('display_pulse') else 'HEART RATE', x, y0 + 15, 12, muted, True)
    hr = rate['bpm']
    band = bpm_range(hr)
    rate_color = PALETTE[band]
    text(f'{hr:.0f}' if hr is not None else '--', x, y0 + 40, 48, rate_color, True)
    text('BPM', x + 120, y0 + 66, 16, muted)
    if rate['source'] == 'recorded':
        text('Recorded', x + 185, y0 + 68, 11, muted)
    elif recent:
        name = 'ECG' if rate['source']=='recent_ecg_peaks' else 'PPG'
        text(name+' estimate', x + 185, y0 + 68, 12, muted)
        text('BPM RANGE: '+band.upper(), x, y0 + 96, 12, rate_color, True)
    pulse = bio.get('display_pulse')
    if pulse:
        for i,name in enumerate(('ecg','ppg')):
            value = pulse.get(name,{}).get('bpm')
            label = f'{name.upper()} {value:.0f}' if value is not None else name.upper()+' --'
            text(label, x + i*140, y0 + 163, 13, muted)
        if recent and not rate.get('verified_by_monitor'):
            text('Unverified estimate', x, y0 + 187, 11, (246,189,112))
    reason = brief_bio_reason(bio)
    if reason:
        text(reason, x, y0 + 113, 17, display['color'], True, width=w - x - 20)
    out = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)
    return cv2.copyMakeBorder(out, border, border, border, border, cv2.BORDER_CONSTANT, value=accent[::-1])
