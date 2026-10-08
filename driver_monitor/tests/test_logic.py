"""모델 없이 판정 로직을 합성 데이터로 검증.  실행: python -m pytest tests -q"""
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dms import geometry as G
from dms.config import Config
from dms.monitor import DriverMonitor, OK, CAUTION, WARN, DANGER, LEVEL_NAME
from dms.perception import FaceObs, HandObs
from dms.wheel import WheelROI

W, H = 1280, 720


def fake_face(ear=0.30, pitch=0.0, mar=0.1):
    return FaceObs(pts=np.zeros((478, 2), np.float32), ear_l=ear, ear_r=ear, ear=ear, mar=mar,
                   pitch=pitch, yaw=0, roll=0, nose_ratio=0.5)


def fake_hand(cx, cy, r=25):
    ang = np.linspace(0, 2 * math.pi, 21, endpoint=False)
    pts = np.stack([cx + r * np.cos(ang), cy + r * np.sin(ang)], 1).astype(np.float32)
    return HandObs(pts=pts, label="Right", score=0.9)


def run(mon, seq, fps=30, t0=0.0, wheel=None):
    """seq: [(duration, face_fn, hands)] -> 마지막 state"""
    t = t0
    s = None
    for dur, face_fn, hands in seq:
        for _ in range(int(dur * fps)):
            s = mon.update(t, face_fn(), hands, wheel, (W, H))
            t += 1 / fps
    return s, t


def calibrated(cfg=None):
    cfg = cfg or Config(calib_seconds=2.0)
    m = DriverMonitor(cfg)
    _, t = run(m, [(2.5, lambda: fake_face(), [])])
    assert m.baseline.done and abs(m.baseline.ear - 0.30) < 1e-6
    return m, t


def test_ear_geometry():
    # 눈 폭 60, 높이 18 → EAR 0.3
    pts = np.zeros((478, 2))
    p = {33: (0, 0), 133: (60, 0), 160: (20, -9), 144: (20, 9), 158: (40, -9), 153: (40, 9)}
    for k, v in p.items():
        pts[k] = v
    assert abs(G.eye_aspect_ratio(pts, G.LEFT_EYE) - 0.3) < 1e-6


def test_euler_pitch_sign():
    th = math.radians(20)  # x축 +20도 회전 = 고개 숙임
    Rx = np.array([[1, 0, 0, 0], [0, math.cos(th), -math.sin(th), 0], [0, math.sin(th), math.cos(th), 0], [0, 0, 0, 1]])
    p, y, r = G.euler_from_matrix(Rx)
    assert abs(p - 20) < 1e-6 and abs(y) < 1e-6 and abs(r) < 1e-6


def test_eyes_closed_levels():
    m, t = calibrated()
    s, t = run(m, [(0.5, lambda: fake_face(0.10), [])], t0=t)
    assert s.eyes_closed and s.level == OK          # 짧은 감김 = 아직 정상
    s, t = run(m, [(1.0, lambda: fake_face(0.10), [])], t0=t)
    assert s.level >= WARN and any("EYES" in r for r in s.reasons)
    s, t = run(m, [(1.5, lambda: fake_face(0.10), [])], t0=t)
    assert s.level == DANGER
    s, t = run(m, [(0.2, lambda: fake_face(0.30), [])], t0=t)
    assert not s.eyes_closed and s.eyes_closed_s == 0


def test_blink_count():
    m, t = calibrated()
    for _ in range(5):
        _, t = run(m, [(0.15, lambda: fake_face(0.10), []), (1.0, lambda: fake_face(0.30), [])], t0=t)
    assert m.state.blinks_per_min == 5 and m.state.level == OK


def test_head_down():
    m, t = calibrated()
    s, t = run(m, [(2.0, lambda: fake_face(pitch=25), [])], t0=t)
    assert s.head_down and s.level >= WARN
    s, t = run(m, [(1.5, lambda: fake_face(pitch=25), [])], t0=t)
    assert s.level == DANGER


def test_face_lost_after_head_drop():
    m, t = calibrated()
    _, t = run(m, [(0.5, lambda: fake_face(pitch=25), [])], t0=t)
    s, t = run(m, [(3.5, lambda: None, [])], t0=t)
    assert s.level == DANGER and any("FACE" in r for r in s.reasons)


def test_nodding():
    m, t = calibrated()
    for _ in range(3):
        _, t = run(m, [(0.6, lambda: fake_face(pitch=25), []), (2.0, lambda: fake_face(pitch=0), [])], t0=t)
    assert m.state.nods == 3 and any("NOD" in r for r in m.state.reasons)


def test_perclos():
    m, t = calibrated(Config(calib_seconds=2.0, perclos_window_s=30))
    # 1초 주기로 0.4초 감김 → PERCLOS ≈ 40% (연속 감김 경고는 안 걸림)
    for _ in range(30):
        _, t = run(m, [(0.4, lambda: fake_face(0.10), []), (0.6, lambda: fake_face(0.30), [])], t0=t)
    assert 0.3 < m.state.perclos < 0.5 and m.state.level == DANGER


def test_wheel_grip():
    cfg = Config(calib_seconds=2.0)
    m, t = calibrated(cfg)
    wheel = WheelROI.from_drag((440, 300), (840, 700), W, H, "ellipse")  # 중심 (640,500), 반축 200
    left, right = fake_hand(440, 500), fake_hand(840, 500)          # 림 위
    center = fake_hand(640, 500)                                    # 핸들 중앙(림 아님)
    away = fake_hand(100, 100)
    s, t = run(m, [(0.5, lambda: fake_face(), [left, right])], t0=t, wheel=wheel)
    assert s.hands_on_wheel == 2 and s.level == OK
    s, t = run(m, [(0.5, lambda: fake_face(), [left, away])], t0=t, wheel=wheel)
    assert s.hands_on_wheel == 1
    s, t = run(m, [(2.5, lambda: fake_face(), [center, away])], t0=t, wheel=wheel)
    assert s.hands_on_wheel == 0 and s.level >= WARN
    s, t = run(m, [(3.0, lambda: fake_face(), [])], t0=t, wheel=wheel)
    assert s.level == DANGER


def test_rect_roi_and_save(tmp_path):
    r = WheelROI.from_drag((100, 400), (500, 700), W, H, "rect")
    f = tmp_path / "w.json"
    r.save(str(f))
    r2 = WheelROI.load(str(f))
    assert r2 == r
    assert r2.grip_ratio(fake_hand(300, 550).pts, W, H, 0.55, 0.1) == 1.0
    assert r2.grip_ratio(fake_hand(900, 100).pts, W, H, 0.55, 0.1) == 0.0


def test_no_wheel_no_hand_alert():
    m, t = calibrated()
    s, t = run(m, [(6.0, lambda: fake_face(), [])], t0=t, wheel=None)
    assert s.hands_on_wheel == -1 and s.level == OK


# ---------------- 손 이탈 / 한 손 운전 ----------------
WHEEL = WheelROI.from_drag((440, 300), (840, 700), W, H, "ellipse")
LEFT, RIGHT, AWAY = fake_hand(440, 500), fake_hand(840, 500), fake_hand(100, 100)


def run_frames(m, n, hands_fn, t, fps=30, face_fn=lambda: fake_face()):
    s = None
    for i in range(n):
        s = m.update(t, face_fn(), hands_fn(i), WHEEL, (W, H))
        t += 1 / fps
    return s, t


def test_level_order():
    assert OK < CAUTION < WARN < DANGER == 3
    assert LEVEL_NAME[CAUTION] == "CAUTION"


def test_hands_not_visible_warn_then_danger():
    m, t = calibrated()
    s, t = run_frames(m, 30, lambda i: [LEFT, RIGHT], t)
    assert s.level == OK and s.hands_on_wheel == 2
    s, t = run_frames(m, 57, lambda i: [], t)                  # 손 검출 0개 1.9초
    assert s.level == OK and s.hands_on_wheel == 0 and 1.8 < s.hands_off_s < 2.0
    s, t = run_frames(m, 6, lambda i: [], t)                   # 2.1초
    assert s.level == WARN and any("HANDS OFF" in r and "DANGER in" in r for r in s.reasons)
    s, t = run_frames(m, 93, lambda i: [], t)                  # 5.2초
    assert s.level == DANGER and s.hands_off_s >= 5.0


def test_hands_flicker_ignored():
    m, t = calibrated()
    # 10프레임마다 2프레임(0.07초)씩 검출이 끊김: 두 손 → 0개, 또는 한 손만
    s, t = run_frames(m, 300, lambda i: [] if i % 10 < 2 else [LEFT, RIGHT], t)
    assert s.level == OK and s.hands_off_s == 0 and s.hands_on_wheel == 2
    s, t = run_frames(m, 300, lambda i: [LEFT] if i % 10 < 2 else [LEFT, RIGHT], t)
    assert s.level == OK and s.one_hand_s == 0 and s.hands_on_wheel == 2


def test_hands_gap_longer_than_grace_counts_from_release():
    m, t = calibrated()
    _, t = run_frames(m, 30, lambda i: [LEFT, RIGHT], t)
    s, t = run_frames(m, 15, lambda i: [], t)                  # 0.5초 > 유예 0.3초
    assert s.hands_on_wheel == 0 and 0.45 < s.hands_off_s < 0.55
    s, t = run_frames(m, 1, lambda i: [LEFT, RIGHT], t)        # 다시 잡으면 바로 0
    assert s.hands_on_wheel == 2 and s.hands_off_s == 0


def test_one_hand_caution():
    m, t = calibrated()
    _, t = run_frames(m, 30, lambda i: [LEFT, RIGHT], t)
    s, t = run_frames(m, 84, lambda i: [LEFT, AWAY], t)        # 2.8초
    assert s.hands_on_wheel == 1 and s.level == OK
    s, t = run_frames(m, 12, lambda i: [LEFT, AWAY], t)        # 3.2초
    assert s.level == CAUTION and s.reasons == ["ONE HAND 3s"]
    s, t = run_frames(m, 300, lambda i: [LEFT], t)             # 10초가 넘어도 CAUTION 유지 (예전엔 WARN)
    assert s.level == CAUTION


def test_one_hand_with_eyes_closed_takes_higher_level():
    m, t = calibrated()
    _, t = run_frames(m, 120, lambda i: [LEFT], t)
    assert m.state.level == CAUTION
    s, t = run_frames(m, 45, lambda i: [LEFT], t, face_fn=lambda: fake_face(0.10))   # 눈 감김 1.5초
    assert s.level == WARN and any("EYES" in r for r in s.reasons) and any("ONE HAND" in r for r in s.reasons)
    s, t = run_frames(m, 45, lambda i: [LEFT], t, face_fn=lambda: fake_face(0.10))   # 3초
    assert s.level == DANGER


def test_one_hand_then_both_off_escalates():
    m, t = calibrated()
    _, t = run_frames(m, 120, lambda i: [LEFT], t)
    assert m.state.level == CAUTION
    s, t = run_frames(m, 66, lambda i: [], t)                  # 남은 손도 뗌, 2.2초
    assert s.level == WARN and s.one_hand_s == 0 and 2.1 < s.hands_off_s < 2.3
    s, t = run_frames(m, 90, lambda i: [], t)
    assert s.level == DANGER


def test_wheel_cleared_and_redrawn_restarts_timer():
    m, t = calibrated()
    s, t = run_frames(m, 90, lambda i: [], t)
    assert s.level == WARN
    for _ in range(30):                                         # X 로 영역 삭제
        s = m.update(t, fake_face(), [], None, (W, H))
        t += 1 / 30
    assert s.level == OK and s.hands_on_wheel == -1
    s, t = run_frames(m, 3, lambda i: [], t)                    # 다시 지정하면 0초부터
    assert s.level == OK and s.hands_off_s < 0.2


def test_hud_caution_bar_no_border():
    from dms.ui import COL, draw_hud
    m, t = calibrated()
    _, t = run_frames(m, 120, lambda i: [LEFT], t)
    s = m.state
    assert s.level == CAUTION
    img = np.zeros((H, W, 3), np.uint8)
    draw_hud(img, s, 30.0, False, m.cfg)
    assert tuple(img[33, W // 2]) == COL[CAUTION]               # 상단 바 = 연한 노랑
    assert not img[H - 3, W - 3].any()                          # 테두리 없음
    s.level = DANGER
    img[:] = 0
    draw_hud(img, s, 30.0, False, m.cfg)
    assert tuple(img[H - 3, W - 3]) == COL[DANGER]


def test_beeper_silent_on_caution(monkeypatch):
    import main
    started = []
    monkeypatch.setattr(main.threading, "Thread", lambda *a, **k: started.append(k) or type("T", (), {"start": lambda self: None})())
    b = main.Beeper(True)
    b(OK)
    b(CAUTION)
    assert not started
    b(WARN)
    assert len(started) == 1 and started[0]["args"] == (WARN,)
