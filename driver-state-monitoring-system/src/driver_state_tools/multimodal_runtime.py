"""One current-observation loop: raw camera frame + raw signal source -> fused decision -> publisher."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from .analysis_publisher import AnalysisPublisher
from .multimodal_fusion import combine
from .revision_session import RevisionSession
from .monitoring_output import MonitoringOutput


@dataclass
class VisionObservation:
    raw: Any
    derived: Any
    decision: Any


class CurrentVisionSource:
    """Runs YOLO and MediaPipe on the frame passed in, with an independent per-clip decision session."""
    def __init__(self, models, config, rules, profile, *, eyes_occluded=False):
        self.models=models
        self.session=RevisionSession(config,rules,profile)
        self.models.start_clip(eyes_occluded=eyes_occluded)
        self.inference_calls=0

    def update(self, frame, timestamp):
        raw=self.models.extract(frame,timestamp,collect_rppg=False)
        derived,decision=self.session.update(raw)
        self.inference_calls+=1
        return VisionObservation(raw,derived,decision)


@dataclass
class MultimodalStep:
    timestamp: float
    vision: VisionObservation
    bio: dict
    decision: Any
    vision_inferred: bool
    publish_revision: Any


class MultimodalProcessor:
    """No saved vision decisions are accepted. Both sources are advanced on the current media time.

    The signal source is BiosignalReplay backed by the original BioSignalMonitor.
    Dependencies may be replaced by test doubles; the application supplies actual models.
    """
    def __init__(self, vision_source, signal_source, sync, clip_id, *, vision_every_frames=1, publisher=None):
        if vision_every_frames<1:raise ValueError('Vision frame interval must be positive')
        self.vision_source=vision_source
        self.signal_source=signal_source
        self.sync=sync
        self.clip_id=clip_id
        self.vision_every_frames=int(vision_every_frames)
        self.publisher=publisher or AnalysisPublisher()
        self.frame_count=0
        self.vision_count=0
        self.last_time=None
        self.latest_vision=None
        self.output_tracker=MonitoringOutput(clip_id,getattr(getattr(vision_source,'session',None),'config',{}))
        self.stop_request_observed=False

    def update(self, frame, timestamp):
        if not math.isfinite(timestamp) or timestamp<0 or (self.last_time is not None and timestamp<=self.last_time):
            raise ValueError('Video time must increase; create a new processor for a new clip')
        self.last_time=timestamp
        server=self.publisher.server
        warning_events=server.warning_events(self.clip_id) if server is not None else []
        response_events=server.driver_response_events(self.clip_id) if server is not None else []
        for event in warning_events:
            session=getattr(self.vision_source,'session',None)
            if session is not None:session.confirm_warning(event['warning_request_id'],timestamp,'unity_ui_ack')
        infer=self.latest_vision is None or self.frame_count%self.vision_every_frames==0
        if infer:
            self.latest_vision=self.vision_source.update(frame,timestamp)
            self.vision_count+=1
        bio=self.signal_source.advance(timestamp)
        current=combine(self.latest_vision.decision,bio,self.sync,timestamp)
        current.metadata.update(pipeline='multimodal_analysis',input_mode='paired_files',
            analysis_origin='current_video_and_ecg_ppg_inference',vision_inference_count=self.vision_count)
        recorded=self.signal_source.recorded_vitals.at(bio.get('timeline_seconds',timestamp)) if getattr(self.signal_source,'recorded_vitals',None) else None
        report=self.output_tracker.update(self.latest_vision,bio,current,timestamp,recorded_vitals=recorded,
            frame_shape=getattr(frame,'shape',None),warning_events=warning_events,response_events=response_events,
            response_input_connected=bool(server is not None and server.driver_response_input_connected))
        self.stop_request_observed|=bool(current.metadata.get('stop_requested'))
        report['vehicle_confirmation']=(server.vehicle_confirmation(self.clip_id,timestamp,self.stop_request_observed,
                session_id=report['session_id'])
            if server is not None else dict(stop_confirmed=False,status='not_received',
                stop_request_observed=self.stop_request_observed,receipt_fresh=False,
                source_time_matches=False,clip_matches=False,session_matches=False,feedback_source_seconds=None,
                feedback_age_seconds=None,
                speed_kph=None,emergency_phase=None,confirmation_source='unity_vehicle_feedback'))
        current.metadata['monitoring_output']=report
        current.metadata['multimodal']['response_input_connected']=report['driver_response']['explicit_input_connected']
        revision=self.publisher.publish(current,self.clip_id)
        self.frame_count+=1
        return MultimodalStep(timestamp,self.latest_vision,bio,current,infer,revision)

    def publish_camera_failure(self,timestamp,reason='camera_frame_decode_failed'):
        from .decision_engine import Decision,DriverState
        unavailable=Decision(timestamp,DriverState.UNAVAILABLE,[reason],{},dict(stop_requested=False))
        observation=self.latest_vision or VisionObservation(None,None,unavailable)
        bio=dict(level='unavailable',hr=None,result_fresh=False,channels={},synthetic=False)
        unavailable.metadata['monitoring_output']=self.output_tracker.update(observation,bio,
            unavailable,timestamp,camera_read_ok=False)
        self.last_time=timestamp
        return self.publisher.publish(unavailable,self.clip_id)
