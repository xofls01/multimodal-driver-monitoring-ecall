"""High-contrast camera annotations. This module never changes driver decisions."""
import cv2
import numpy as np

# OpenCV BGR. Keep feature colors separate from the state-colored outer border.
FEATURE_COLORS = {
    "face": (245, 232, 210),
    "eyes": (160, 220, 80),
    "pose": (255, 155, 65),
    "hands": (90, 190, 255),
    "closed": (100, 95, 245),
    "phone": (255, 110, 80),
    "muted": (150, 160, 177),
}
EYE_RINGS = ((33, 160, 158, 133, 153, 144), (362, 385, 387, 263, 373, 380))
MOUTH_RING = (78, 81, 13, 311, 308, 402, 14, 178)
POSE_EDGES = ((5, 6), (5, 11), (6, 12), (11, 12), (5, 7), (7, 9), (6, 8), (8, 10))


def draw_landmarks(frame, raw, derived, config=None):
    """Draw only available coordinates, with a dark outline on every bright line."""
    config = config or {}
    scene = frame.copy()
    height = frame.shape[0]
    scale = height / 720
    dark = (14, 12, 16)
    face_width = max(2, round(config.get("overlay_face_line_pixels", 3) * scale))
    eye_width = max(2, round(config.get("overlay_eye_line_pixels", 3) * scale))
    pose_width = max(3, round(config.get("overlay_pose_line_pixels", 5) * scale))
    hand_width = max(2, round(config.get("overlay_hand_line_pixels", 3) * scale))
    outline = max(2, round(config.get("overlay_outline_pixels", 3) * scale))

    def line(a, b, color, width):
        a, b = tuple(map(round, a[:2])), tuple(map(round, b[:2]))
        cv2.line(scene, a, b, dark, width + outline, cv2.LINE_AA)
        cv2.line(scene, a, b, color, width, cv2.LINE_AA)

    def ring(points, color, width):
        points = np.asarray(points, np.int32)
        cv2.polylines(scene, [points], True, dark, width + outline, cv2.LINE_AA)
        cv2.polylines(scene, [points], True, color, width, cv2.LINE_AA)

    def label(value, xy, color):
        xy = (max(4, round(xy[0])), max(22, min(height - 8, round(xy[1]))))
        cv2.putText(scene, value, xy, cv2.FONT_HERSHEY_SIMPLEX, .48 * scale,
                    dark, max(3, round(5 * scale)), cv2.LINE_AA)
        cv2.putText(scene, value, xy, cv2.FONT_HERSHEY_SIMPLEX, .48 * scale,
                    color, max(1, round(1.5 * scale)), cv2.LINE_AA)

    def brackets(box, color):
        x1, y1, x2, y2 = map(round, box[:4])
        size = max(5, min(round(22 * scale), (x2 - x1) // 4, (y2 - y1) // 4))
        for x, y, dx, dy in ((x1, y1, 1, 1), (x2, y1, -1, 1),
                              (x1, y2, 1, -1), (x2, y2, -1, -1)):
            line((x, y), (x + dx * size, y), color, face_width)
            line((x, y), (x, y + dy * size), color, face_width)

    if raw.face_box:
        brackets(raw.face_box, FEATURE_COLORS["face"] if raw.face_readable else FEATURE_COLORS["muted"])
    if derived.eyes_readable:
        color = FEATURE_COLORS["closed"] if derived.eyes_closed else FEATURE_COLORS["eyes"]
        for indices in EYE_RINGS:
            if all(i in raw.face_landmarks for i in indices):
                ring([raw.face_landmarks[i] for i in indices], color, eye_width)
    if derived.mouth_open and all(i in raw.face_landmarks for i in MOUTH_RING):
        ring([raw.face_landmarks[i] for i in MOUTH_RING], FEATURE_COLORS["face"], eye_width)
    for a, b in POSE_EDGES:
        p, q = raw.pose_keypoints.get(a), raw.pose_keypoints.get(b)
        if p and q and min(p[2], q[2]) >= .3:
            line(p, q, FEATURE_COLORS["pose"], pose_width)
    radius = max(3, round(config.get("overlay_joint_radius_pixels", 5) * scale))
    for i in (5, 6, 7, 8, 9, 10, 11, 12):
        point = raw.pose_keypoints.get(i)
        if point and point[2] >= .3:
            xy = tuple(map(round, point[:2]))
            cv2.circle(scene, xy, radius + max(2, round(3 * scale)), dark, -1, cv2.LINE_AA)
            cv2.circle(scene, xy, radius, FEATURE_COLORS["pose"], -1, cv2.LINE_AA)
    for index, hand in enumerate(raw.hand_landmarks):
        if len(hand) < 18:
            continue
        palm = [hand[i][:2] for i in (0, 5, 9, 13, 17)]
        ring(palm, FEATURE_COLORS["hands"], hand_width)
        on_chest = index < len(raw.hand_on_chest) and raw.hand_on_chest[index]
        if on_chest:
            label("HAND NEAR CHEST", (min(p[0] for p in palm), max(p[1] for p in palm) + 22 * scale),
                  FEATURE_COLORS["hands"])
    for box in raw.phone_boxes:
        brackets(box, FEATURE_COLORS["phone"])
        label("PHONE CANDIDATE", (box[0], box[1] - 8 * scale), FEATURE_COLORS["phone"])
    object_colors = {"drink": (255, 220, 40), "food": (40, 180, 255),
                     "utensil": (90, 230, 255), "other": (200, 170, 240)}
    for item in getattr(raw, "handheld_objects", []):
        color = object_colors.get(item["category"], FEATURE_COLORS["phone"])
        brackets(item["box"], color)
        suffix = " CANDIDATE" if item["mode"] == "detected" else " TRACK"
        label(item["label"].upper() + suffix, (item["box"][0], item["box"][1] - 8 * scale), color)
    if config.get("overlay_show_rppg_rois", False):
        for box in [raw.rppg_forehead_box] + raw.rppg_cheek_boxes:
            if box:
                cv2.rectangle(scene, box[:2], box[2:], (193, 180, 130), 1, cv2.LINE_AA)
    return scene
