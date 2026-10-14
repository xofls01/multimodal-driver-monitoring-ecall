"""Evidence-first rendering. The source image is never cropped or covered by cards."""
from __future__ import annotations

from typing import Any
import cv2
import numpy as np

COLORS = {
    "face": (245, 207, 60), "eyes": (149, 239, 121),
    "pose": (245, 155, 182), "hands": (68, 195, 250),
    "phone": (214, 108, 244), "muted": (150, 160, 177),
    "normal": (147, 226, 105), "caution": (62, 194, 253),
    "danger": (89, 87, 250), "unavailable": (157, 165, 183),
}
EYE_RINGS = ((33,160,158,133,153,144), (362,385,387,263,373,380))
MOUTH_RING = (78,81,13,311,308,402,14,178)


def fmt(value: float | None, digits: int = 1, suffix: str = "") -> str:
    return "--" if value is None else f"{value:.{digits}f}{suffix}"


def draw_overlay(frame: np.ndarray, raw: Any, derived: Any, decision: Any,
                 rppg: Any = None, panel_width: int | None = None,
                 ui_config: dict[str, Any] | None = None) -> np.ndarray:
    cfg = ui_config or {}
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
    for hand in raw.hand_landmarks:
        palm = np.asarray([hand[i][:2] for i in (0,5,9,13,17)],np.int32)
        cv2.polylines(scene,[palm],True,COLORS["hands"],thick,cv2.LINE_AA)
    for box in raw.phone_boxes:
        brackets(box,COLORS["phone"])
        label = "PHONE / DETECTED" if raw.phone_track_mode == "detected" else "PHONE / HELD" if raw.phone_track_mode == "cached" else "PHONE / TRACKED"
        xy = (max(0,box[0]),max(round(20*scale),box[1]-round(6*scale)))
        cv2.putText(scene,label,xy,cv2.FONT_HERSHEY_SIMPLEX,.35*scale,(20,20,20),max(2,thick+2),cv2.LINE_AA)
        cv2.putText(scene,label,xy,cv2.FONT_HERSHEY_SIMPLEX,.35*scale,COLORS["phone"],thick,cv2.LINE_AA)
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
        text(label,24,y,.34,muted)
        value_width = cv2.getTextSize(value,cv2.FONT_HERSHEY_SIMPLEX,.43*scale,max(1,round(scale)))[0][0]
        text(value,max(190,(rail-value_width)/scale-24),y,.43,color)
    def meter(value: float,threshold: float,y: float,color: Any) -> None:
        x1,x2 = round(24*scale),rail-round(24*scale)
        cv2.rectangle(panel,(x1,round(y*scale)),(x2,round((y+4)*scale)),(65,56,45),-1)
        if value > 0:
            cv2.rectangle(panel,(x1,round(y*scale)),(x1+round((x2-x1)*min(1,value/threshold)),round((y+4)*scale)),color,-1)

    text("DRIVER / OBSERVATION",24,32,.48)
    text(f"REPLAY   T+{derived.timestamp:05.2f}s",24,55,.33,muted)
    text(state.upper() if state != "unavailable" else "UNAVAILABLE",24,99,.9,accent)
    priority = ("collapse_posture","distress_gesture","eyes_closed","long_blinks_repeated","nods_repeated","attention_away","attention_away_repeated","attention_away_cumulative")
    reasons = sorted(decision.reasons,key=lambda s: priority.index(s) if s in priority else 99)
    text(" / ".join(s.replace("_"," ").upper() for s in reasons[:2]) or "NO ACTIVE ALERT",24,123,.31,accent)
    text(f"COVERAGE: {derived.measurement_coverage.upper()}  |  {derived.calibration_status.upper()}",24,145,.31,muted)
    line(160)

    for i,(label,ready,color) in enumerate((
        ("FACE",derived.face_readable,COLORS["face"]),("EYES",derived.eyes_readable,COLORS["eyes"]),
        ("POSE",derived.pose_readable,COLORS["pose"]),("HANDS",bool(raw.hand_landmarks),COLORS["hands"]))):
        x = 24+i*96
        cv2.circle(panel,(round(x*scale),round(185*scale)),max(2,round(3*scale)),color if ready else (75,75,75),-1)
        text(label,x+9,189,.29,color if ready else muted)

    text("01 / FORWARD ATTENTION",24,223,.36,COLORS["face"])
    attention = "UNKNOWN" if derived.attention_away is None else "AWAY" if derived.attention_away else "FORWARD PROXY"
    row("DIRECTION",attention,249,COLORS['caution'] if derived.attention_away else white)
    away = decision.durations.get("attention_away",0)
    row("CONTINUOUS AWAY",f"{away:.1f}s / 2.0s",274)
    meter(away,2,283,COLORS["caution"])
    row("AWAY IN LAST 10s",f"{decision.durations.get('attention_window_seconds',0):.1f}s",308)
    text(derived.attention_source,24,328,.29,muted)
    line(340)

    text("02 / EYE TIMING",24,364,.36,COLORS["eyes"])
    eye_state = "UNREADABLE" if derived.eyes_closed is None else "CLOSED" if derived.eyes_closed else "OPEN"
    row("APERTURE",eye_state,389,COLORS["eyes"] if derived.eyes_readable else muted)
    row("CONTINUOUS CLOSED",fmt(derived.eye_closed_seconds if derived.eyes_readable else None,1,"s"),414)
    meter(derived.eye_closed_seconds,2,423,COLORS["eyes"])
    row("PERCLOS / VALID TIME",f"{fmt(derived.perclos*100 if derived.perclos is not None else None,0,'%')} / {derived.eye_observed_seconds:.1f}s",448)
    row("BLINKS / MIN",fmt(derived.blink_rate_per_minute),473)
    row("LAST CLOSE / NO BLINK",f"{fmt(derived.last_blink_duration,2,'s')} / {fmt(derived.no_blink_seconds,1,'s')}",498)
    text("NO-BLINK TIME IS NOT A DROWSINESS VERDICT",24,518,.27,muted)
    line(530)

    text("03 / POSTURE & CONTEXT",24,553,.36,COLORS["pose"])
    posture = "UNKNOWN" if derived.collapse_posture is None else "SLUMP PROXY" if derived.collapse_posture else "LEAN" if derived.torso_lean else "STABLE"
    row("POSTURE / NODS",f"{posture} / {derived.nod_count}",578)
    phone = "DETECTED" if raw.phone_track_mode == "detected" else "SHORT TRACK" if raw.phone_track_mode == "tracked" else "BETWEEN SAMPLES" if raw.phone_track_mode == "cached" else "NOT CONFIRMED"
    row("PHONE",phone,601,COLORS["phone"] if raw.phone_boxes else muted)
    row("HAND NEAR CHEST","YES" if derived.distress_gesture else "--" if derived.distress_gesture is None else "NO",624,COLORS["hands"])
    line(635)
    if rppg and rppg.status != "disabled":
        text("rPPG / NORMAL DEMO ONLY",24,656,.29,muted)
        text(f"FOREHEAD {fmt(rppg.forehead_bpm,0)}    CHEEKS {fmt(rppg.cheeks_bpm,0)}",24,678,.33)
        text(f"{rppg.status.upper()}   CAMERA ESTIMATE / NOT MEDICAL",24,699,.27,muted)
    else:
        text("VISION ONLY / RESPONSE INPUT: NOT CONNECTED",24,657,.29,muted)
        text("NO INFERRED UNCONSCIOUSNESS OR CARDIAC ARREST",24,680,.26,muted)
        text("PHONE IS CONTEXT, NOT THE ATTENTION RULE",24,700,.27,muted)
    # A tiny external legend (not overlaid on source pixels) is available in HTML too.
    return np.concatenate((scene,panel),axis=1)
