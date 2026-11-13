"""Explicit startup-only experiment settings. Received analyzer code and clinical rules stay untouched."""
from dataclasses import replace
import json
import math
from pathlib import Path
from biosignal.config import Config
from biosignal.dsp import MODEL_WINDOW_S


def load_startup_profile(path=None):
    data=json.loads(Path(path).read_text()) if path else dict(name='original10',overrides={})
    allowed={'calibration_s','min_ready_s','calibration_min_readings'}
    overrides=data.get('overrides',{})
    if not isinstance(overrides,dict) or set(overrides)-allowed:
        raise ValueError('Only startup duration and minimum observation settings can be overridden')
    for key,value in overrides.items():
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0:
            raise ValueError('Startup settings must be finite and positive')
        if key=='calibration_min_readings' and not isinstance(value,int):
            raise ValueError('Minimum readings must be an integer')
    cfg=replace(Config(),**overrides)
    if cfg.min_ready_s>cfg.calibration_s:
        raise ValueError('Signal readiness cannot require longer than the startup duration')
    return cfg,dict(profile=data.get('name','custom_startup'),overrides=dict(overrides),
                    calibration_s=cfg.calibration_s,min_ready_s=cfg.min_ready_s,
                    calibration_min_readings=cfg.calibration_min_readings,
                    original_defaults=not bool(overrides),neural_window_seconds=MODEL_WINDOW_S)
