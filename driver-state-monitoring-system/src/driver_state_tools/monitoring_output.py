"""Current observations and event history. No new diagnosis or vehicle-control rule."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime,timezone
import math
import uuid

from .bpm_display import select_display_bpm,bpm_range


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')


def get(obj,key,default=None):
    return obj.get(key,default) if isinstance(obj,dict) else getattr(obj,key,default)


def finite(value):
    return float(value) if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) else None


class MonitoringOutput:
    def __init__(self,clip_id,config=None,*,clock=utc_now):
        self.clip_id,self.config,self.clock=clip_id,config or {},clock
        self.session_id=uuid.uuid4().hex
        self.previous_time=None
        self.previous_vision_state=None
        self.head_start=None
        self.dangers={}
        self.warning=None
        self.warning_count=0
        self.last_visual_recovery=None
        self.last_response=None
        self.seen_responses=set()

    def _danger(self,kind,state,source_time,now):
        previous=self.dangers.get(kind,{})
        if state=='danger' and not previous.get('active'):
            previous=dict(first_source_seconds=previous.get('first_source_seconds',source_time),
                          source_seconds=source_time,detected_at_utc=now,
                          event_id=uuid.uuid4().hex,event_count=previous.get('event_count',0)+1)
        previous['active']=state=='danger'
        self.dangers[kind]=previous
        return dict(active=previous['active'],detected=previous.get('event_id') is not None,source_seconds=previous.get('source_seconds'),
                    first_source_seconds=previous.get('first_source_seconds'),
                    detected_at_utc=previous.get('detected_at_utc'),event_id=previous.get('event_id'),
                    event_count=previous.get('event_count',0),recording_event_utc=None)

    def update(self,vision,bio,decision,now,*,recorded_vitals=None,frame_shape=None,
               warning_events=(),response_events=(),response_input_connected=False,camera_read_ok=True):
        if not math.isfinite(now) or now<0 or self.previous_time is not None and now<=self.previous_time:
            raise ValueError('Monitoring output time must increase; create a tracker for a new clip')
        stamp=self.clock();v=vision.decision;raw=vision.raw;derived=vision.derived
        observed_time=float(v.timestamp)
        current=bool(camera_read_ok and 0<=now-observed_time<=float(self.config.get('max_observation_gap_seconds',.25))+1e-9)
        new_observation=self.previous_time is None or observed_time!=get(self,'last_observed_time')
        vstate=v.state.value if current else 'unavailable'
        warning_active=decision.state.value in ('caution','danger')
        if warning_active and (self.warning is None or not self.warning['active']):
            self.warning_count+=1
            self.warning=dict(active=True,request_id=decision.metadata.get('warning_request_id') or
                              f'observation:{self.session_id}:{self.warning_count}',
                              started_source_seconds=now,displayed_source_seconds=None,
                              visual_alert_seen=vstate in ('caution','danger'),recovery_recorded=False,
                              cancel_attempted=False)
        if self.warning:
            self.warning['active']=warning_active
            self.warning['visual_alert_seen']|=vstate in ('caution','danger')
            request=self.warning['request_id']
            for event in warning_events:
                if event.get('warning_request_id')==request and self.warning['displayed_source_seconds'] is None:
                    self.warning['displayed_source_seconds']=now
            for event in response_events:
                event_id=event.get('event_id')
                if (event.get('warning_request_id')!=request or event.get('clip_id')!=self.clip_id
                        or not event_id or event_id in self.seen_responses
                        or event.get('action') not in ('acknowledge','cancel_attempt')):
                    continue
                if len(self.seen_responses)>=256:self.seen_responses.clear()
                self.seen_responses.add(event_id)
                self.last_response=dict(event_id=event_id,action=event['action'],source=event.get('source','external_input'),
                    received_at_utc=event.get('received_at_utc') or stamp,applied_source_seconds=now,
                    warning_request_id=request,causes_stop_cancellation=False)
                self.warning['cancel_attempted']|=event['action']=='cancel_attempt'
            if (new_observation and current and vstate=='normal' and self.warning['visual_alert_seen']
                    and self.previous_vision_state in ('caution','danger','unavailable')
                    and not self.warning['recovery_recorded']):
                self.last_visual_recovery=dict(source_seconds=observed_time,observed_at_utc=stamp,
                    warning_request_id=request,after_confirmed_warning=self.warning['displayed_source_seconds'] is not None,
                    consciousness_confirmed=False)
                self.warning['recovery_recorded']=True
            if warning_active:
                if not decision.metadata.get('warning_request_id'):decision.metadata['warning_request_id']=request
                if self.warning['displayed_source_seconds'] is not None:
                    if decision.metadata.get('warning_issued_at') is None:
                        decision.metadata['warning_issued_at']=self.warning['displayed_source_seconds']
                    if not decision.metadata.get('warning_source'):decision.metadata['warning_source']='unity_ui_ack'

        values=v.metadata.get('evidence_values',{})
        head_known=current and values.get('head_drop_observed') is True
        drop=finite(get(derived,'relative_head_drop')) if head_known else None
        limit=finite(self.config.get('collapse_head_drop_delta'))
        lowered=drop>=limit if drop is not None and limit is not None else None
        if not current:self.head_start=None
        if new_observation:
            gap=get(self,'last_observed_time') is not None and observed_time-self.last_observed_time>.25+1e-9
            if lowered is not True or gap:self.head_start=None
            if lowered is True and self.head_start is None:self.head_start=observed_time
        head_duration=(observed_time-self.head_start if lowered else 0.) if lowered is not None else None
        eyes=get(derived,'eyes_closed') if current else None
        closure=float(v.durations.get('eyes_closed',0.)) if eyes is True else 0. if eyes is False else None
        quality=deepcopy(get(raw,'observation_quality',{}) or {})
        box=get(raw,'face_box')
        face_visible=(quality.get('face_detected',bool(box) or bool(get(raw,'face_readable',False)))) if current else None
        hand_points=get(raw,'hand_landmarks',[]) or []
        hand_flags=get(raw,'hand_on_wheel',[]) or []
        wrists=[get(raw,'left_wrist_on_wheel'),get(raw,'right_wrist_on_wheel')]
        wheel_configured=bool(self.config.get('wheel_roi'))
        if current and wheel_configured and hand_points and len(hand_flags)==len(hand_points):
            wheel=any(hand_flags);wheel_count=sum(hand_flags);wheel_method='hand_region_overlap'
        elif current and wheel_configured and any(x is not None for x in wrists):
            wheel=any(x is True for x in wrists);wheel_count=sum(x is True for x in wrists);wheel_method='wrist_region_overlap'
        else:wheel=wheel_count=None;wheel_method='unavailable'
        sensor_channels={}
        for name in ('ecg','ppg'):
            channel=bio.get('channels',{}).get(name,{})
            sensor_channels[name]=dict(ready=bool(channel.get('ready')),contact=channel.get('contact'),
                contact_known=channel.get('contact') is not None,
                signal_quality_available=bool(channel.get('ready') and bio.get('result_fresh') and channel.get('sqi') is not None),
                stale=channel.get('stale'),signal_quality=channel.get('sqi') if channel.get('ready') and bio.get('result_fresh') else None,
                reliable=bool(channel.get('reliable') and bio.get('result_fresh')))
        rate=select_display_bpm(bio,recorded_vitals)
        output=dict(schema='driver_monitoring_output.v1',clip_id=self.clip_id,session_id=self.session_id,
            source_time_seconds=now,processed_at_utc=stamp,state=decision.state.value,
            prediction_age_seconds=0.,is_current=True,
            danger=self._danger('final',decision.state.value,now,stamp),
            vision=dict(state=vstate,observation_source_seconds=observed_time,observation_age_seconds=max(0.,now-observed_time),
                current=current,max_observation_age_seconds=.25,danger=self._danger('vision',vstate,observed_time,stamp),
                eyes_observed=eyes is not None,eyes_closed=eyes,eyes_closed_seconds=closure,
                head_lowering_observed=lowered is not None,head_lowered=lowered,head_lowering_seconds=head_duration,
                head_lowering_angle_degrees=None,head_angle_calibrated=False,head_angle_status='calibration_required',
                head_pitch_proxy_observed=current and finite(get(derived,'relative_pitch')) is not None,
                head_pitch_proxy=finite(get(derived,'relative_pitch')) if current else None,
                head_pitch_proxy_units='relative_landmark_scaled_units_not_degrees',
                head_drop_ratio=drop,face_observed=current,face_visible=face_visible,
                face_readable=bool(get(raw,'face_readable')) if current else None,
                hands_observed=wheel is not None,hands_on_wheel=wheel,hands_on_wheel_count=wheel_count,hands_on_wheel_method=wheel_method,
                physical_grip_confirmed=None,physical_grip_confirmation_available=False),
            driver_response=dict(warning_active=warning_active,
                warning_request_id=self.warning['request_id'] if self.warning else None,
                warning_started_source_seconds=self.warning['started_source_seconds'] if self.warning else None,
                warning_displayed_source_seconds=self.warning['displayed_source_seconds'] if self.warning else None,
                warning_display_confirmed=bool(self.warning and self.warning['displayed_source_seconds'] is not None),
                visual_recovery=deepcopy(self.last_visual_recovery),
                explicit_input_connected=bool(response_input_connected),
                cancel_attempted=self.warning['cancel_attempted'] if self.warning and response_input_connected else None,
                last_confirmed_response=deepcopy(self.last_response),consciousness_confirmed=None),
            observation_quality=dict(judgment_confidence_score=None,judgment_confidence_calibrated=False,
                face_detection_confidence=None,face_detection_confidence_available=False,
                face=quality,quality_reasons=list(get(raw,'quality_reasons',[]) or []),
                camera=dict(input_kind='video_file',frame_read_ok=bool(camera_read_ok),
                    image_width=frame_shape[1] if frame_shape is not None and len(frame_shape)>=2 else None,
                    image_height=frame_shape[0] if frame_shape is not None and len(frame_shape)>=2 else None,
                    camera_hardware_ok=None,hardware_health_available=False),
                sensors=dict(input_kind='recorded_csv',sample_timestamp=bio.get('sample_timestamp'),
                    sample_available=bio.get('sample_timestamp') is not None,
                    sample_age_seconds=bio.get('sample_age_seconds'),result_fresh=bool(bio.get('result_fresh')),
                    hardware_ok=None,hardware_health_available=False,channels=sensor_channels)),
            biosignal=dict(bpm=rate['bpm'],bpm_state=bpm_range(rate['bpm']),bpm_source=rate['source'],
                bpm_verified_by_monitor=rate.get('verified_by_monitor'),
                model_state=bio.get('level','unavailable'),model_bpm=bio.get('hr'),model_bpm_available=bio.get('hr') is not None,
                synthetic=bool(bio.get('synthetic'))))
        if not current:
            output['vision']['head_pitch_proxy']=None
        self.previous_time=now;self.previous_vision_state=vstate
        if new_observation:self.last_observed_time=observed_time
        return output


def output_for_packet(output,age,stale_after):
    if output is None:return None
    result=deepcopy(output)
    total_age=max(0.,age)+output.get('prediction_age_seconds',0.)
    result['prediction_age_seconds']=total_age
    result['is_current']=bool(output.get('is_current',True) and total_age<=stale_after)
    result['vision']['observation_age_seconds']+=max(0.,age)
    vision_stale=result['vision']['observation_age_seconds']>result['vision']['max_observation_age_seconds']
    expired=not result['is_current']
    confirmation=result.get('vehicle_confirmation')
    if confirmation and confirmation.get('feedback_age_seconds') is not None:
        confirmation['feedback_age_seconds']+=max(0.,age)
        if confirmation['feedback_age_seconds']>stale_after:
            confirmation.update(stop_confirmed=False,status='stale',receipt_fresh=False)
    if expired:
        result['state']='unavailable';result['vision']['state']='unavailable'
        result['vision']['current']=False
        result['biosignal'].update(bpm=None,bpm_state='unavailable',model_state='unavailable',model_bpm=None)
        result['biosignal']['model_bpm_available']=False
        result['observation_quality']['sensors']['result_fresh']=False
        for channel in result['observation_quality']['sensors']['channels'].values():
            channel.update(reliable=False,signal_quality_available=False,signal_quality=None)
        result['observation_quality']['camera']['frame_read_ok']=None
        result['driver_response']['explicit_input_connected']=False
        result['danger']['active']=False
        if result.get('vehicle_confirmation'):
            result['vehicle_confirmation'].update(stop_confirmed=False,status='stale',receipt_fresh=False)
    if expired or vision_stale:
        result['vision']['state']='unavailable';result['vision']['current']=False
        for field in ('eyes_closed','eyes_closed_seconds','head_lowered','head_lowering_seconds',
                      'head_pitch_proxy','head_drop_ratio','face_visible','face_readable',
                      'hands_on_wheel','hands_on_wheel_count'):
            result['vision'][field]=None
        result['vision']['danger']['active']=False
        for field in ('eyes_observed','head_lowering_observed','head_pitch_proxy_observed','face_observed','hands_observed'):
            result['vision'][field]=False
    return result
