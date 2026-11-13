"""Evidence-first rendering. The source image is never cropped or covered by cards."""
from __future__ import annotations

from typing import Any
import cv2
import numpy as np
from .decision_engine import DEFAULT_RULES

COLORS = {
    "face": (245, 207, 60), "eyes": (149, 239, 121),
    "pose": (245, 155, 182), "hands": (68, 195, 250),
    "phone": (214, 108, 244), "muted": (150, 160, 177),
    "drink": (202, 210, 85), "food": (86, 180, 255),
    "utensil": (170, 199, 235), "other": (209, 177, 191),
    "normal": (147, 226, 105), "caution": (62, 194, 253),
    "danger": (89, 87, 250), "unavailable": (157, 165, 183),
}
EYE_RINGS = ((33,160,158,133,153,144), (362,385,387,263,373,380))
MOUTH_RING = (78,81,13,311,308,402,14,178)


def fmt(value: float | None, digits: int = 1, suffix: str = "") -> str:
    return "--" if value is None else f"{value:.{digits}f}{suffix}"


def _draw_baseline_overlay(frame: np.ndarray, raw: Any, derived: Any, decision: Any,
                 rppg: Any = None, panel_width: int | None = None,
                 ui_config: dict[str, Any] | None = None) -> np.ndarray:
    cfg = ui_config or {}
    rules = {**DEFAULT_RULES, **cfg.get("decision_rules", {})}
    h, w = frame.shape[:2]
    scale = h / 720
    rail = panel_width or round(420*scale)
    scene = frame.copy()
    thick = max(1, round(scale*1.5))
    white, muted = (242, 245, 248), COLORS["muted"]
    state = decision.state.value
    accent = COLORS[state]

    def brackets(box: Any, color: Any) -> None:
        x1,y1,x2,y2 = map(int, box[:4])
        size = max(5,min(round(20*scale), (x2-x1)//4, (y2-y1)//4))
        for x,y,dx,dy in ((x1,y1,1,1),(x2,y1,-1,1),(x1,y2,1,-1),(x2,y2,-1,-1)):
            cv2.line(scene,(x,y),(x+dx*size,y),color,thick,cv2.LINE_AA)
            cv2.line(scene,(x,y),(x,y+dy*size),color,thick,cv2.LINE_AA)

    if raw.face_box:
        brackets(raw.face_box, COLORS["face"] if raw.face_readable else muted)
    if derived.eyes_readable:
        for ring in EYE_RINGS:
            if all(i in raw.face_landmarks for i in ring):
                pts = np.asarray([raw.face_landmarks[i] for i in ring], np.int32)
                cv2.polylines(scene,[pts],True,COLORS["danger"] if derived.eyes_closed else COLORS["eyes"],thick,cv2.LINE_AA)
    if derived.mouth_open and all(i in raw.face_landmarks for i in MOUTH_RING):
        cv2.polylines(scene,[np.asarray([raw.face_landmarks[i] for i in MOUTH_RING],np.int32)],True,COLORS["hands"],thick,cv2.LINE_AA)
    for a,b in ((5,6),(5,11),(6,12),(11,12),(5,7),(7,9),(6,8),(8,10)):
        p,q = raw.pose_keypoints.get(a),raw.pose_keypoints.get(b)
        if p and q and min(p[2],q[2]) >= .3:
            cv2.line(scene,tuple(map(int,p[:2])),tuple(map(int,q[:2])),COLORS["pose"],thick,cv2.LINE_AA)
    for i in (5,6,7,8,9,10,11,12):
        p = raw.pose_keypoints.get(i)
        if p and p[2] >= .3:
            cv2.circle(scene,tuple(map(int,p[:2])),max(2,round(3*scale)),COLORS["pose"],-1,cv2.LINE_AA)
    for index, hand in enumerate(raw.hand_landmarks):
        palm = np.asarray([hand[i][:2] for i in (0,5,9,13,17)],np.int32)
        on_chest = index < len(raw.hand_on_chest) and raw.hand_on_chest[index]
        cv2.polylines(scene,[palm],True,COLORS["hands"],thick + int(on_chest),cv2.LINE_AA)
        if on_chest:
            label = "HAND NEAR CHEST"
            xy = (max(0, int(palm[:,0].min())), min(h-round(8*scale),int(palm[:,1].max())+round(20*scale)))
            cv2.putText(scene,label,xy,cv2.FONT_HERSHEY_SIMPLEX,.4*scale,(20,20,20),max(2,thick+2),cv2.LINE_AA)
            cv2.putText(scene,label,xy,cv2.FONT_HERSHEY_SIMPLEX,.4*scale,COLORS["hands"],thick,cv2.LINE_AA)
    for box in raw.phone_boxes:
        brackets(box,COLORS["phone"])
        label = "PHONE CANDIDATE"
        xy = (max(0,box[0]),max(round(20*scale),box[1]-round(6*scale)))
        cv2.putText(scene,label,xy,cv2.FONT_HERSHEY_SIMPLEX,.35*scale,(20,20,20),max(2,thick+2),cv2.LINE_AA)
        cv2.putText(scene,label,xy,cv2.FONT_HERSHEY_SIMPLEX,.35*scale,COLORS["phone"],thick,cv2.LINE_AA)
    for item in getattr(raw, "handheld_objects", []):
        box = item["box"]
        color = COLORS.get(item["category"], COLORS["other"])
        brackets(box, color)
        label = item["label"].upper() + (" CANDIDATE" if item["mode"] == "detected" else " TRACK")
        xy = (max(0, round(box[0])), max(round(20*scale), round(box[1])-round(6*scale)))
        cv2.putText(scene,label,xy,cv2.FONT_HERSHEY_SIMPLEX,.35*scale,(20,20,20),max(2,thick+2),cv2.LINE_AA)
        cv2.putText(scene,label,xy,cv2.FONT_HERSHEY_SIMPLEX,.35*scale,color,thick,cv2.LINE_AA)
    if cfg.get("overlay_show_rppg_rois",False):
        boxes = [raw.rppg_forehead_box] + raw.rppg_cheek_boxes
        for box in boxes:
            if box:
                cv2.rectangle(scene,box[:2],box[2:],(193,180,130),1,cv2.LINE_AA)

    panel = np.zeros((h,rail,3),np.uint8)
    for y in range(h):
        panel[y,:] = (np.array((30,24,20))*(1-y/h)+np.array((18,17,19))*y/h).astype(np.uint8)
    def text(value: str,x: float,y: float,size: float=.43,color: Any=white) -> None:
        # Fit long reasons and metrics into the rail instead of spilling across the face.
        px,py = round(x*scale),round(y*scale)
        actual = size*scale
        text_thick = max(1,round(scale)) if size < .6 else thick
        width = cv2.getTextSize(value,cv2.FONT_HERSHEY_SIMPLEX,actual,text_thick)[0][0]
        if width > rail-px-round(22*scale):
            actual *= (rail-px-round(22*scale))/max(1,width)
        cv2.putText(panel,value,(px,py),cv2.FONT_HERSHEY_SIMPLEX,actual,color,text_thick,cv2.LINE_AA)
    def line(y: float) -> None:
        cv2.line(panel,(round(24*scale),round(y*scale)),(rail-round(24*scale),round(y*scale)),(62,59,54),max(1,round(scale)),cv2.LINE_AA)
    def row(label: str,value: str,y: float,color: Any=white) -> None:
        text(label,24,y,.37,muted)
        label_width = cv2.getTextSize(label,cv2.FONT_HERSHEY_SIMPLEX,.37*scale,max(1,round(scale)))[0][0]
        value_size = .48
        value_width = cv2.getTextSize(value,cv2.FONT_HERSHEY_SIMPLEX,value_size*scale,max(1,round(scale)))[0][0]
        available = rail - round(68*scale) - label_width
        if value_width > available:
            value_size *= available/max(1,value_width)
            value_width = cv2.getTextSize(value,cv2.FONT_HERSHEY_SIMPLEX,value_size*scale,max(1,round(scale)))[0][0]
        text(value,(rail-value_width)/scale-24,y,value_size,color)
    def meter(value: float,threshold: float,y: float,color: Any) -> None:
        x1,x2 = round(24*scale),rail-round(24*scale)
        cv2.rectangle(panel,(x1,round(y*scale)),(x2,round((y+4)*scale)),(65,56,45),-1)
        if value > 0:
            cv2.rectangle(panel,(x1,round(y*scale)),(x1+round((x2-x1)*min(1,value/threshold)),round((y+4)*scale)),color,-1)

    text("DRIVER MONITOR",24,32,.48)
    text(f"REPLAY   T+{derived.timestamp:05.2f}s",24,55,.33,muted)
    text(state.upper() if state != "unavailable" else "UNAVAILABLE",24,99,.9,accent)
    priority = ("collapse_posture","distress_gesture","eyes_closed_with_posture_change","eyes_closed","long_blinks_repeated","nods_repeated","attention_away","attention_away_repeated","attention_away_cumulative")
    reasons = sorted(decision.reasons,key=lambda s: priority.index(s) if s in priority else 99)
    reason_labels = {
        "collapse_posture": "BODY POSTURE CHANGE",
        "distress_gesture": "HAND NEAR CHEST",
        "eyes_closed_with_posture_change": "EYES CLOSED + HEAD LOWERED",
        "eyes_closed": "EYES CLOSED",
        "long_blinks_repeated": "REPEATED LONG EYE CLOSURES",
        "nods_repeated": "REPEATED HEAD NODS",
        "attention_away": "LOOKING AWAY",
        "attention_away_repeated": "LOOKING AWAY",
        "attention_away_cumulative": "LOOKING AWAY",
        "head_abnormal": "HEAD TURNED OR LOWERED",
        "yawning": "MOUTH OPEN",
        "perclos_high": "EXTENDED EYE CLOSURE",
        "torso_lean": "BODY LEANING",
        "recovering": "RETURNING TO NORMAL",
        "caution_persisted": "CAUTION CONTINUES",
        "face_unreadable": "FACE TRACKING UNAVAILABLE",
        "eyes_unreadable": "EYE TRACKING UNAVAILABLE",
        "pose_unreadable": "BODY TRACKING UNAVAILABLE",
        "calibrating": "INITIALIZING",
    }
    labels = list(dict.fromkeys(reason_labels.get(s,s.replace("_"," ").upper()) for s in reasons if s != "caution_persisted"))
    text(" / ".join(labels[:2]) or "NO ACTIVE ALERT",24,123,.34,accent)
    if rules["danger_policy"] == "sustained_caution" and state in ("caution", "danger"):
        limit = float(rules['caution_to_danger_seconds'])
        label = f"CAUTION {decision.durations.get('caution_continuous',0):.1f}s / {limit:.1f}s TO DANGER" if state == "caution" else f"CAUTION EXCEEDED {limit:.1f}s"
        text(label,24,145,.33,accent)
    else:
        status = "INITIALIZING" if derived.calibration_status != "ready" else "MONITORING" if derived.measurement_coverage == "full" else "LIMITED VISIBILITY"
        text(status,24,145,.33,muted)
    line(160)

    for i,(label,ready,color) in enumerate((
        ("FACE",derived.face_readable,COLORS["face"]),("EYES",derived.eyes_readable,COLORS["eyes"]),
        ("POSE",derived.pose_readable,COLORS["pose"]),("HANDS",bool(raw.hand_landmarks),COLORS["hands"]))):
        x = 24+i*96
        cv2.circle(panel,(round(x*scale),round(185*scale)),max(2,round(3*scale)),color if ready else (75,75,75),-1)
        text(label,x+9,189,.29,color if ready else muted)

    text("01 / DRIVER ATTENTION",24,223,.43,COLORS["face"])
    attention = "UNKNOWN" if derived.attention_away is None else "AWAY" if derived.attention_away else "FORWARD"
    row("DIRECTION",attention,249,COLORS['caution'] if derived.attention_away else white)
    away = decision.durations.get("attention_away",0)
    away_threshold = float(rules["attention_away_caution_seconds"])
    row("TIME LOOKING AWAY",f"{away:.1f}s / {away_threshold:.1f}s",274)
    meter(away,away_threshold,283,COLORS["caution"])
    row("TOTAL IN LAST 10s",f"{decision.durations.get('attention_window_seconds',0):.1f}s",308)
    line(328)

    text("02 / EYE STATE",24,357,.43,COLORS["eyes"])
    eye_state = "UNAVAILABLE" if derived.eyes_closed is None else "CLOSED" if derived.eyes_closed else "OPEN"
    eye_color = muted if derived.eyes_closed is None else COLORS["caution"] if derived.eyes_closed else COLORS["eyes"]
    row("EYES",eye_state,384,eye_color)
    closed_limit = float(rules["eyes_closed_caution_seconds"])
    closed_value = f"{derived.eye_closed_seconds:.1f}s / {closed_limit:.1f}s" if derived.eyes_readable else "--"
    row("CLOSED FOR",closed_value,412)
    meter(derived.eye_closed_seconds if derived.eyes_readable else 0,closed_limit,421,eye_color)
    closure = fmt(derived.perclos*100,0,'%') if derived.perclos is not None else "COLLECTING" if derived.eyes_readable else "UNAVAILABLE"
    row("EYE CLOSURE (%)",closure,451)
    row("BLINK COUNT",str(derived.blink_count) if derived.eye_observed_seconds > 0 else "--",479)
    line(496)

    text("03 / POSTURE & HANDS",24,526,.43,COLORS["pose"])
    if not derived.pose_readable or derived.collapse_posture is None:
        posture = "UNKNOWN"
    elif derived.collapse_posture:
        posture = "LEANING" if abs(derived.relative_torso or 0) >= float(cfg.get("collapse_lean_degrees",34)) else "LOWERED"
    else:
        posture = "LEANING" if derived.torso_lean else "UPRIGHT"
    row("POSTURE",posture,553)
    row("HEAD NODS",str(derived.nod_count),577)
    objects = (["PHONE"] if raw.phone_confidence is not None else []) + [
        item["label"].upper() for item in getattr(raw, "handheld_objects", [])]
    objects = list(dict.fromkeys(objects))
    object_label = " / ".join(objects[:2]) + (f" +{len(objects)-2}" if len(objects)>2 else "")
    row("OBJECT CANDIDATES",object_label or "NOT CONFIRMED",601,COLORS["phone"] if objects else muted)
    chest_value = f"YES ({decision.durations.get('distress_gesture',0):.1f}s)" if derived.distress_gesture else "UNKNOWN" if derived.distress_gesture is None else "NO"
    row("HAND NEAR CHEST",chest_value,625,COLORS["hands"] if derived.distress_gesture else muted if derived.distress_gesture is None else white)
    line(638)
    if rppg and rppg.status != "disabled":
        text("rPPG / CAMERA PULSE ESTIMATE",24,660,.34,muted)
        ready = rppg.status == "ready" and rppg.heart_rate_bpm is not None
        value = f"{rppg.heart_rate_bpm:.0f} BPM" if ready else "COLLECTING" if rppg.status == "calibrating" else "LOW SIGNAL"
        text(value,24,690,.73 if ready else .48,white if ready else muted)
        text(f"FOREHEAD {fmt(rppg.forehead_bpm,0)}",235,676,.29,muted)
        text(f"CHEEKS {fmt(rppg.cheeks_bpm,0)}",235,695,.29,muted)
    else:
        text("VISION-BASED BEHAVIOR MONITORING",24,666,.32,muted)
    text("PROTOTYPE / NOT A MEDICAL DIAGNOSIS",24,715,.26,muted)
    # A tiny external legend (not overlaid on source pixels) is available in HTML too.
    return np.concatenate((scene,panel),axis=1)


def draw_overlay(frame, raw, derived, decision, rppg=None, panel_width=None, ui_config=None):
    if decision.metadata.get("mode") == "prototype":
        from .evidence_dashboard import draw_evidence_overlay
        return draw_evidence_overlay(frame,raw,derived,decision,rppg,panel_width,ui_config or {})
    if decision.metadata.get("mode") != "literature":
        return _draw_baseline_overlay(frame,raw,derived,decision,rppg,panel_width,ui_config)
    from .readable_dashboard import replace_panel
    cfg = dict(ui_config or {})
    cfg["decision_rules"] = {**cfg.get("decision_rules", {}), "danger_policy": "literature"}
    original = _draw_baseline_overlay(frame,raw,derived,decision,rppg,panel_width,cfg)
    width = frame.shape[1]
    return replace_panel(original[:, :width], raw, derived, decision,
                         cfg["decision_rules"], rppg, original.shape[1]-width, cfg)
