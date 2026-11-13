"""Small, explicit replay policy. Supplied fusion response/CAN assumptions are NOT silently enabled."""
from __future__ import annotations

from copy import deepcopy
from .decision_engine import Decision, DriverState
from .bpm_display import select_display_bpm, bpm_range


def combine(vision: Decision, bio: dict, sync: dict, now=None) -> Decision:
    now = vision.timestamp if now is None else now
    vision_age = now - vision.timestamp
    if vision_age < -1e-6:
        raise ValueError('Future vision observation cannot be used')
    if vision_age > .25:
        vision = Decision(vision.timestamp, DriverState.UNAVAILABLE, ['vision_observation_stale'])
    state = vision.state
    reasons = ['VISION:' + r for r in vision.reasons]
    v_ok = state != DriverState.UNAVAILABLE
    b_ok = bool(bio.get('available'))
    b_level = bio.get('level') if b_ok else 'unavailable'
    vf = vision.metadata.get('prototype_frame', {})
    # Different sources corroborate only CURRENT, observed evidence; a generic Caution is insufficient.
    corroboration = (v_ok and (vf.get('collapse_posture') is True or
                     (vf.get('eyes_closed') is True and vision.durations.get('eyes_closed', 0.) >= 3.)))
    basis = 'vision_only'
    if state == DriverState.DANGER:
        basis = vision.metadata.get('stop_basis') or 'vision_prototype_stop'
    elif b_level == 'danger' and corroboration:
        state = DriverState.DANGER
        basis = 'biosignal_alert_and_observed_posture_or_eye_closure'
    elif state == DriverState.CAUTION or b_level in ('caution', 'danger'):
        state = DriverState.CAUTION
        basis = 'biosignal_needs_confirmation' if b_level == 'danger' else 'warning_from_available_source'
    # A normal pulse cannot certify road attention when the camera is unavailable.
    elif not v_ok:
        state, basis = DriverState.UNAVAILABLE, 'vision_observation_missing'
    if b_ok:
        reasons += ['BIO:' + r for r in bio.get('reasons', [])]
    if state == DriverState.DANGER and vision.state != DriverState.DANGER:
        reasons.append('FUSION:CORROBORATED_TEST_ALERT')
    metadata = deepcopy(vision.metadata)
    metadata.update(pipeline='multimodal_replay', stop_requested=state == DriverState.DANGER,
                    stop_basis=basis, decision_phase='multimodal_replay')
    metadata['multimodal'] = dict(vision_state=vision.state.value, vision_available=v_ok,
        bio_state=bio.get('level', 'unavailable'), bio_available=b_ok,
        bio_timestamp=bio.get('t'), bio_age_seconds=bio.get('age_seconds'),
        bio_sample_age_seconds=bio.get('sample_age_seconds'),
        bio_reasons=bio.get('reasons', []), bio_hr=bio.get('hr'),
        bio_hr_source=bio.get('hr_class_source'), bio_channels=bio.get('channels', {}),
        synthetic=bool(bio.get('synthetic')), synchronization=deepcopy(sync),
        response_input_connected=False, can_connected=False, rppg_used_for_fusion=False,
        vision_source_timestamp=vision.timestamp, vision_age_seconds=vision_age,
        startup_settings=bio.get('startup_settings',{}),
        display_pulse=deepcopy(bio.get('display_pulse')),
        display_rate=select_display_bpm(bio) if bio.get('display_pulse') else None,
        fusion_policy='prototype_replay_v1', basis=basis)
    return Decision(now, state, reasons, dict(vision.durations), metadata)


def add_multimodal_packet(packet, decision, age, stale_after):
    info = decision.metadata.get('multimodal')
    if info is None:
        return packet
    fresh = age <= stale_after
    packet.update(pipeline=decision.metadata.get('pipeline','multimodal_replay'), fusion_state=packet['vision_state'],
                  fusion_available=bool(fresh and decision.state != DriverState.UNAVAILABLE),
                  vision_state=info['vision_state'] if fresh else 'unavailable',
                  vision_available=bool(fresh and info['vision_available']),
                  bio_state=info['bio_state'] if fresh else 'unavailable',
                  bio_available=bool(fresh and info['bio_available']),
                  synthetic_biosignal=info['synthetic'], synchronization=info['synchronization'],
                  response_input_connected=bool(info.get('response_input_connected')), can_connected=False,
                  bio_hr=info['bio_hr'] if fresh and info['bio_available'] else None,
                  bio_age_seconds=None if info['bio_age_seconds'] is None else info['bio_age_seconds'] + age,
                  bio_sample_age_seconds=None if info['bio_sample_age_seconds'] is None else info['bio_sample_age_seconds'] + age,
                  bio_source_timestamp=info['bio_timestamp'], bio_reasons=info['bio_reasons'],
                  bio_model_source=info['bio_hr_source'], fusion_policy=info['fusion_policy'])
    packet['vision_source_timestamp'] = info['vision_source_timestamp']
    packet['vision_age_seconds'] = info['vision_age_seconds'] + age
    packet['bio_signal_quality'] = {k: v.get('sqi') for k, v in info['bio_channels'].items()} if fresh else {}
    packet['analysis_origin'] = decision.metadata.get('analysis_origin','saved_or_prior_vision_analysis')
    packet['input_mode'] = decision.metadata.get('input_mode','paired_files')
    packet['bio_startup_settings'] = info.get('startup_settings',{})
    display = info.get('display_rate')
    if display is not None:
        packet['bio_display_hr'] = display.get('bpm') if fresh else None
        packet['bio_display_hr_source'] = display.get('source') if fresh else None
        packet['bio_display_hr_verified'] = bool(fresh and display.get('verified_by_monitor'))
        packet['bio_display_hr_used_for_grade'] = False
        packet['bio_display_bpm_range'] = bpm_range(packet['bio_display_hr'])
    return packet
