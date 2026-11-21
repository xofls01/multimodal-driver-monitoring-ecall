"""Original landmark colors, with one additional pixel at 720p. No state calculations."""
import cv2
import numpy as np

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

FEATURE_COLORS = {key:COLORS[key] for key in ('face','eyes','pose','hands','phone','muted')}
FEATURE_COLORS['closed'] = COLORS['danger']

def draw_landmarks(frame, raw, derived, config=None):
    cfg = config or {}
    scene = frame.copy()
    h, w = frame.shape[:2]
    scale = h / 720
    thick = max(1, round(scale*1.5)) + max(1,round(scale))
    muted = COLORS['muted']
    def brackets(box, color):
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
            cv2.circle(scene,tuple(map(int,p[:2])),max(2,round(3*scale))+max(1,round(scale)),COLORS["pose"],-1,cv2.LINE_AA)
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

    return scene
