"""Connect measured observations, per-video calibration and selected rule mode."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from hashlib import sha256
import json
import math
import uuid
from typing import Any

from .decision_engine import FeatureFrame, TemporalDecisionEngine
from .literature_engine import LiteratureFrame, LiteratureDecisionEngine
from .nod_events import NodEventDetector
from .vision_features import FeatureTracker
from .prototype_engine import PrototypeFrame, PrototypeDecisionEngine


def load_profile(path: Path | None, source: Path) -> dict:
    if path is None:
        return {}
    data = json.loads(path.read_text())
    digest = sha256(source.read_bytes()).hexdigest()
    entries = data.get("profiles", [data])
    matches = [p for p in entries if p.get("source_sha256") == digest]
    if len(matches) != 1:
        raise ValueError("Calibration profile must match exactly one source-video SHA256")
    profile = matches[0]
    if profile.get("schema") == "reviewed_neutral_window_v1":
        window = profile.get("neutral_window")
        if (profile.get("reviewed") is not True or not profile.get("provenance")
                or not isinstance(window, list) or len(window) != 2
                or any(not isinstance(v, (int,float)) or not math.isfinite(v) for v in window)
                or not 0 <= window[0] < window[1] <= 3):
            raise ValueError("Invalid reviewed neutral window")
        return profile
    if profile.get("schema") != "reviewed_video_calibration_v1":
        raise ValueError("Unsupported calibration profile schema")
    mean = profile.get("eye_baseline_mean")
    if (profile.get("neutral_open_reviewed") is not True or not profile.get("provenance")
            or not isinstance(mean, (int,float)) or not math.isfinite(mean) or mean <= 0
            or profile.get("neutral_sample_count", 0) < 8):
        raise ValueError("Neutral eye-open calibration must be reviewed, valid and sufficiently sampled")
    return profile


class RevisionSession:
    def __init__(self, config: dict[str, Any], rules: dict[str, Any], profile: dict | None = None):
        self.config, self.rules = dict(config), dict(rules)
        self.profile = profile or {}
        mode = rules.get("mode", "legacy")
        if mode not in ("literature", "legacy", "prototype") or config.get("judgment_mode", "legacy") != mode:
            raise ValueError("Use matching literature, prototype or legacy configurations; modes must not be mixed")
        self.literature = rules.get("mode") == "literature"
        self.prototype = mode == "prototype"
        self.config["judgment_mode"] = mode
        self.config["calibration_reference"] = self.profile
        self.tracker = FeatureTracker(self.config)
        self.engine = PrototypeDecisionEngine(rules) if self.prototype else LiteratureDecisionEngine(rules) if self.literature else TemporalDecisionEngine(rules)
        self.nods = NodEventDetector(self.profile.get("nod"), float(rules.get("max_observation_gap_seconds", .25)))
        self.session_id = uuid.uuid4().hex
        self.last_frame = None
        self.last_nod = None
        self.last_timestamp = None

    def _mouth(self, raw) -> tuple[bool | None, str]:
        p = self.profile.get("mouth") or {}
        # The published Eq.1 is deliberately not replaced by a symmetric formula.
        required = (p.get("formula") == "r1_eq1_literal" and p.get("landmark_mapping_reviewed") is True
                    and p.get("maximum_open_reviewed") is True and p.get("provenance")
                    and float(p.get("maximum_open_seconds", 0)) >= 5
                    and isinstance(p.get("maximum_open_mean_mar"), (int,float))
                    and p["maximum_open_mean_mar"] > 0)
        if not required:
            return None, "mapping_and_calibration_required"
        indices = p.get("mediapipe_points_p1_to_p8", [])
        if len(indices) != 8 or len(set(indices)) != 8:
            return None, "mapping_and_calibration_required"
        if not raw.face_readable or any(i not in raw.face_landmarks for i in indices):
            return None, "unavailable"
        import numpy as np
        points = [np.asarray(raw.face_landmarks[i]) for i in indices]
        distance = lambda a,b: float(np.linalg.norm(points[a-1]-points[b-1]))
        width = 2*distance(1,5)
        if width <= 1e-6:
            return None, "unavailable"
        mar = (distance(2,8)+distance(3,7)+distance(4,5))/width
        return mar >= p["maximum_open_mean_mar"]*.5, "ready"

    def update(self, raw):
        if not math.isfinite(raw.timestamp) or raw.timestamp < 0:
            raise ValueError("Observation time must be finite and nonnegative")
        if self.last_timestamp is not None and raw.timestamp < self.last_timestamp:
            self.tracker = FeatureTracker(self.config)
            self.nods = NodEventDetector(self.profile.get("nod"), float(self.rules.get("max_observation_gap_seconds", .25)))
            self.session_id = uuid.uuid4().hex
            self.engine.reset()
        self.last_timestamp = raw.timestamp
        derived = self.tracker.update(raw)
        if self.prototype:
            # A missing hip invalidates torso angle, not a separately visible
            # nose/shoulder head-drop observation. No missing value becomes true.
            head_known = raw.head_drop_ratio is not None and derived.relative_head_drop is not None
            torso_known = raw.torso_angle is not None and derived.relative_torso is not None
            head_low = head_known and derived.relative_head_drop >= float(self.config["collapse_head_drop_delta"])
            body_lean = torso_known and abs(derived.relative_torso) >= float(self.config["collapse_lean_degrees"])
            collapse = True if head_low or body_lean else False if head_known and torso_known else None
            attention = derived.attention_away if derived.eyes_closed is not True else None
            nod_proxy = (derived.eyes_closed and derived.relative_head_drop >= float(self.config["nod_drop_ratio"])) if head_known and derived.eyes_closed is not None else None
            self.last_frame = PrototypeFrame(raw.timestamp, derived.calibration_status == "ready",
                raw.face_readable, derived.eyes_closed, attention, collapse,
                derived.torso_lean if torso_known else None, derived.mouth_open,
                derived.distress_gesture, nod_proxy, derived.nod_count,
                derived.long_blink_count, derived.attention_source)
            decision = self.engine.update(self.last_frame)
            decision.metadata.update(session_id=self.session_id,
                feature_status=dict(eyes="ready" if derived.eyes_readable else "unavailable",
                    attention="relative_proxy" if attention is not None else "unavailable",
                    posture="ready" if collapse is not None else "unavailable",
                    nod="geometry_proxy", mouth="relative_proxy", gaze="model_not_connected"),
                calibration=dict(status=derived.calibration_status, samples=self.tracker.profile.sample_count,
                    eye_samples=len(self.tracker._samples["ear"]), baseline_ear=self.tracker.profile.neutral_ear,
                    neutral_window=self.profile.get("neutral_window"), provenance=self.profile.get("provenance")),
                evidence_values=dict(ear=derived.ear,relative_head_drop=derived.relative_head_drop,
                    relative_torso=derived.relative_torso,head_drop_observed=head_known,
                    torso_observed=torso_known))
            derived.collapse_posture = collapse
            derived.attention_away = attention
            derived.eye_closed_seconds = decision.durations["eyes_closed"]
            ratio = decision.metadata["eye_ratio"]
            derived.perclos = ratio.get("lower_ratio") if ratio["status"] in ("high", "low") else None
            derived.perclos_high = ratio["status"] == "high" if ratio["status"] in ("high", "low") else None
            return derived, decision
        if not self.literature:
            self.last_frame = FeatureFrame(
                timestamp=raw.timestamp, face_readable=derived.face_readable, eyes_readable=derived.eyes_readable,
                pose_readable=derived.pose_readable, eyes_closed=derived.eyes_closed, attention_away=derived.attention_away,
                head_abnormal=derived.head_abnormal, yawning=derived.mouth_open, perclos_high=derived.perclos_high,
                torso_lean=derived.torso_lean, collapse_posture=derived.collapse_posture,
                distress_gesture=derived.distress_gesture, long_blinks_repeated=derived.long_blinks_repeated,
                nods_repeated=derived.nods_repeated)
            return derived, self.engine.update(self.last_frame)
        eye_y = None
        if raw.face_readable and raw.image_height > 0 and all(i in raw.face_landmarks for i in (33,263)):
            eye_y = (raw.face_landmarks[33][1]+raw.face_landmarks[263][1])/(2*raw.image_height)
        nod = self.nods.update(raw.timestamp, eye_y, derived.eyes_closed)
        self.last_nod = nod.to_dict()
        mouth, mouth_status = self._mouth(raw)
        gaze = raw.gaze_observation or {}
        gaze_state = gaze.get("state", "unknown") if gaze.get("status") == "ready" else "unknown"
        statuses = dict(eyes="ready" if derived.eyes_readable else "needs_calibration" if not self.profile else "unavailable",
                        gaze=gaze.get("status", "model_not_connected"), nod=nod.status, mouth=mouth_status)
        baseline = self.tracker.profile.neutral_ear
        coefficient = float(self.config["eye_close_ratio"])
        requested = baseline*coefficient if baseline is not None else None
        applied = min(float(self.config["eye_threshold_max"]), max(float(self.config["eye_threshold_min"]), requested)) if requested is not None else None
        validation = self.profile.get("eye_validation", {})
        eye_validated = (validation.get("passed") is True and applied is not None
                         and validation.get("eye_combine_method") == "mean"
                         and validation.get("baseline_method") == "mean"
                         and validation.get("eye_hysteresis") == float(self.config["eye_hysteresis"])
                         and isinstance(validation.get("applied_threshold"), (int,float))
                         and abs(validation["applied_threshold"]-applied) <= 1e-9)
        statuses["eye_stop"] = "ready" if eye_validated else "needs_validation"
        self.last_frame = LiteratureFrame(raw.timestamp, self.session_id,
                            eyes_closed=derived.eyes_closed,
                            eye_danger_validated=eye_validated,
                            gaze=gaze_state, gaze_zone=gaze.get("zone", "unknown"),
                            mouth_open=mouth, nod_suspected=nod.suspected, nod_completed=nod.completed,
                            feature_status=statuses)
        decision = self.engine.update(self.last_frame)
        decision.metadata["eye_calibration"] = dict(baseline_mean=baseline, coefficient=coefficient,
                    requested_threshold=requested, applied_threshold=applied,
                    clamp_changed_threshold=requested is not None and abs(requested-applied)>1e-9,
                    hysteresis=float(self.config["eye_hysteresis"]),
                    provenance=self.profile.get("provenance"), eye_validation=self.profile.get("eye_validation"))
        decision.metadata["nod_event"] = self.last_nod
        decision.metadata["relative_proxy_observations"] = dict(
            attention_away=derived.attention_away,head_abnormal=derived.head_abnormal,
            iris_gaze_away=derived.gaze_away,mouth_open=derived.mouth_open,
            legacy_nod_count=derived.nod_count,legacy_nods_repeated=derived.nods_repeated)
        derived.attention_away = (gaze_state == "away" if gaze_state in ("forward","away")
                                 and derived.eyes_closed is False else None)
        derived.attention_source = "ZONE MODEL" if gaze.get("status") == "ready" else "UNAVAILABLE"
        derived.gaze_away = derived.attention_away
        derived.mouth_open = mouth
        derived.nods_repeated = None
        derived.eye_closed_seconds = decision.durations["eyes_closed"]
        # Keep legacy diagnostic values in raw/derived logs, but display the new window.
        ratio = decision.metadata["eye_ratio"]
        derived.perclos = ratio.get("lower_ratio") if ratio["status"] in ("high", "low") else None
        derived.perclos_high = ratio["status"] == "high" if ratio["status"] in ("high", "low") else None
        derived.nod_count = nod.count
        return derived, decision

    def record_frame(self):
        return asdict(self.last_frame) if self.last_frame is not None else None

    def confirm_warning(self, request_id, timestamp, source):
        return self.literature and self.engine.confirm_warning(request_id, timestamp, source)
