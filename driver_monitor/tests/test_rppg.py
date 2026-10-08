"""rPPG 합성 신호 테스트.  실행: python -m pytest tests -q"""
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dms.config import Config
from dms.monitor import DriverMonitor, WARN
from dms.perception import FaceObs
from dms.rppg import (RPPG, RPPGResult, roi_stats, roi_polys, FH_BROW, FH_MID, FH_TOP, L_CHEEK, R_CHEEK,
                      ROI_FOREHEAD, ROI_ALL)
from dms.ui import draw_hud, draw_rppg


def synth(bpm=72, dur=32, fps=30, amp=0.008, noise=0.3, mono=False, rsa=0.0, seed=0):
    """피부 반사 모델: 조명 변화 * (1 + 맥파 * 혈액량 색 서명) + 센서 잡음, FPS 흔들림 포함."""
    rng = np.random.default_rng(seed)
    t = np.cumsum(rng.uniform(0.8, 1.2, int(dur * fps)) / fps)
    tf = np.arange(0, t[-1] + 1, 1e-3)
    phf = 2 * np.pi * np.cumsum(bpm / 60 * (1 + rsa * np.sin(2 * np.pi * 0.25 * tf))) * 1e-3
    ph = np.interp(t, tf, phf)
    p = np.sin(ph) + 0.3 * np.sin(2 * ph + 0.5)
    illum = 1 + 0.05 * np.sin(2 * np.pi * 0.07 * t) + 0.02 * np.sin(2 * np.pi * 0.4 * t + 1)
    if mono:
        C = 120 * illum * (1 - amp * p) + rng.normal(0, noise, len(t))
        C = np.stack([C, C, C], 1)
    else:
        C = np.array([150, 110, 90.]) * illum[:, None] * (1 + amp * np.array([0.33, 0.77, 0.53]) * p[:, None])
        C += rng.normal(0, noise, C.shape)
    k = np.where(np.diff(np.floor((phf - np.pi / 2) / (2 * np.pi))) > 0)[0]
    ibi = np.diff(tf[k]) * 1000
    return t, C, float(np.sqrt(np.mean(np.diff(ibi) ** 2)))


def feed(cfg, t, C):
    r = RPPG(cfg)
    for ti, c in zip(t, C):
        r.push(float(ti), tuple(c))
    return r


def test_hr_accuracy_range():
    cfg = Config()
    for bpm in (50, 72, 95, 130):
        for seed in range(2):
            t, C, _ = synth(bpm=bpm, seed=seed)
            res = feed(cfg, t, C).res
            assert res.valid and abs(res.bpm - bpm) < 3, (bpm, seed, res.bpm, res.snr)


def test_mono_ir_mode():
    t, C, _ = synth(mono=True)
    res = feed(Config(), t, C).res
    assert res.mode == "mono" and abs(res.bpm - 72) < 3


def test_noise_only_mostly_invalid():
    cfg = Config()
    valid = total = 0
    for seed in range(6):
        t, C, _ = synth(amp=0.0, seed=seed)
        r = RPPG(cfg)
        seq = -1
        for ti, c in zip(t, C):
            r.push(float(ti), tuple(c))
            if r.res.seq != seq:
                seq = r.res.seq
                total += 1
                valid += r.res.valid
    assert valid / total < 0.15


def test_hrv_clean_signal():
    cfg = Config()
    t, C, true_rmssd = synth(rsa=0.05, noise=0.0)
    res = feed(cfg, t, C).res
    assert not math.isnan(res.rmssd) and abs(res.rmssd - true_rmssd) / true_rmssd < 0.2


def test_hrv_suppressed_when_noisy():
    t, C, _ = synth(amp=0.004, noise=0.3)
    res = feed(Config(), t, C).res
    assert math.isnan(res.rmssd)


def test_gap_resets_buffer():
    cfg = Config()
    t, C, _ = synth(dur=15)
    r = feed(cfg, t, C)
    assert r.res.progress == 1.0
    r.push(float(t[-1] + 2.0), tuple(C[-1]))
    assert r.res.progress == 0.0 and len(r.buf) == 1


def test_low_fps_rejected():
    t, C, _ = synth(fps=10, dur=15)
    res = feed(Config(), t, C).res
    assert not res.valid and "fps" in res.msg


# ---------------- ROI ----------------
SKIN, HAIR = (90, 130, 190), (25, 30, 35)                # BGR


def face_pts():
    """이마 세 줄(y=100 머리선, 150 가운데, 200 눈썹)과 양 볼(원)만 배치한 합성 랜드마크."""
    pts = np.tile(np.array([[320.0, 300.0]]), (478, 1))
    xs = 320 + np.array([-80.0, -40, 0, 40, 80])
    for idx, y in ((FH_TOP, 100), (FH_MID, 150), (FH_BROW, 200)):
        pts[idx] = np.stack([xs, np.full(5, float(y))], 1)
    ang = np.linspace(0, 2 * math.pi, 16, endpoint=False)
    for idx, cx in ((L_CHEEK, 230), (R_CHEEK, 410)):
        pts[idx] = np.stack([cx + 35 * np.cos(ang), 300 + 35 * np.sin(ang)], 1)
    return pts


def face_img(bangs_to=None):
    """피부색 바탕에 머리카락(y<110)과 눈썹(y 188~200). bangs_to 를 주면 앞머리가 그 y 까지 내려옴."""
    img = np.zeros((480, 640, 3), np.uint8)
    img[:] = SKIN
    img[:110] = HAIR
    img[188:200, 240:400] = HAIR
    if bangs_to:
        img[:bangs_to] = HAIR
    return img


def fake_face_obs():
    return FaceObs(pts=face_pts().astype(np.float32), ear_l=.3, ear_r=.3, ear=.3, mar=.1,
                   pitch=0, yaw=0, roll=0, nose_ratio=.5)


def test_forehead_roi_avoids_hairline_and_brows():
    hull = roi_polys(face_pts())["forehead"][0].reshape(-1, 2)
    assert hull[:, 1].min() >= 115 and hull[:, 1].max() <= 185     # 머리선(100)과 눈썹(200)에서 떨어짐
    stats, polys = roi_stats(face_img(), face_pts())
    (rgb, n), (crgb, cn) = stats["forehead"], stats["cheeks"]
    assert np.allclose(rgb, SKIN[::-1]) and np.allclose(crgb, SKIN[::-1])
    assert n > 5000 and cn > 5000 and len(polys["cheeks"]) == 2


def test_skin_mask_drops_bangs():
    img = face_img(bangs_to=160)                                    # 이마 위쪽 2/3 를 앞머리가 덮음
    full = roi_stats(face_img(), face_pts())[0]["forehead"][1]
    (rgb, n), _ = roi_stats(img, face_pts(), skin=True)[0].values()
    assert np.allclose(rgb, SKIN[::-1]) and n < 0.5 * full
    (rgb_raw, _), _ = roi_stats(img, face_pts(), skin=False)[0].values()
    assert not np.allclose(rgb_raw, SKIN[::-1])                     # 피부 마스크가 없으면 머리카락이 섞임


def test_forehead_first_covered_falls_back_to_cheeks():
    r = RPPG(Config())
    face = fake_face_obs()
    for i in range(10):
        r.update(i / 30, face_img(), face)
    assert r.res.roi == ROI_FOREHEAD and len(r.res.hulls) == 1      # 이마만 그림
    for i in range(10, 40):                                         # 앞머리로 이마가 다 가려짐
        r.update(i / 30, face_img(bangs_to=200), face)
    assert r.res.roi == ROI_ALL and r.res.roi_reason == "forehead covered"
    assert len(r.res.hulls) == 3 and r.res.fh_px < Config().rppg_forehead_min_px


def test_forehead_first_low_snr_fallback_and_recover():
    cfg = Config()
    t, Cp, _ = synth(dur=60, seed=0)
    _, Cn, _ = synth(dur=60, amp=0.0, seed=0)                     # 같은 시간축, 맥파 없음
    fore = np.where((t > 30)[:, None], Cp, Cn)                     # 이마: 30초까지 잡음만, 이후 맥파
    r = RPPG(cfg)
    switched = None
    for ti, f, c in zip(t, fore, Cp):
        res = r.push_stats(float(ti), {"forehead": (tuple(f), 3000), "cheeks": (tuple(c), 3000)})
        if switched is None and res.roi == ROI_ALL:
            switched = (ti, res.progress, len(r.buf), res.roi_reason)
        if ti < 30 and switched:
            assert res.roi == ROI_ALL                               # 이마가 나쁜 동안은 볼 포함 유지
    ts, prog, nbuf, why = switched
    assert why == "forehead low SNR"
    assert cfg.rppg_hr_window_s + cfg.rppg_roi_fallback_s - 1 < ts < 20
    assert prog == 1.0 and nbuf > 250                               # 볼 포함 버퍼는 따로 쌓여 있어 바로 사용
    assert r.res.roi == ROI_FOREHEAD and r.res.roi_reason == ""     # 이마가 좋아지면 복귀
    assert r.res.valid and abs(r.res.bpm - 72) < 3


def test_roi_mode_forehead_only_never_adds_cheeks():
    r = RPPG(Config(rppg_roi_mode="forehead"))
    assert list(r.est) == [ROI_FOREHEAD]
    face = fake_face_obs()
    for i in range(60):
        r.update(i / 30, face_img(bangs_to=200), face)
    assert r.res.roi == ROI_FOREHEAD and len(r.res.hulls) == 1


def test_roi_mode_all_uses_union_mean():
    r = RPPG(Config(rppg_roi_mode="all"))
    assert list(r.est) == [ROI_ALL]
    res = r.push_stats(0.0, {"forehead": ((100.0,) * 3, 1000), "cheeks": ((200.0,) * 3, 3000)})
    assert res.roi == ROI_ALL and np.allclose(r.buf[-1][1:], 175.0)  # 픽셀 수 가중 평균
    res = r.update(0.03, face_img(), fake_face_obs())
    assert len(res.hulls) == 3


def test_monitor_shows_roi():
    m = DriverMonitor(Config(calib_seconds=0.1))
    face = fake_face_obs()
    r = RPPGResult(roi=ROI_ALL, roi_reason="forehead covered")
    s = m.update(0.0, face, [], None, (640, 480), rppg=r)
    assert s.hr_roi == ROI_ALL and s.hr_roi_reason == "forehead covered"

def test_monitor_hr_baseline_and_alert():
    cfg = Config(calib_seconds=1.0, rppg_baseline_n=5, rppg_alert=True, hr_low_s=5.0)
    m = DriverMonitor(cfg)
    face = FaceObs(pts=np.zeros((478, 2), np.float32), ear_l=.3, ear_r=.3, ear=.3, mar=.1,
                   pitch=0, yaw=0, roll=0, nose_ratio=.5)
    t, seq = 0.0, 0
    for bpm, dur in ((70, 12), (58, 8)):                  # 기준 70 → 58 (-17%)
        for _ in range(int(dur * 30)):
            if int(t * 30) % 30 == 0:
                seq += 1
            r = RPPGResult(bpm=bpm, raw_bpm=bpm, snr=3, valid=True, progress=1, seq=seq)
            s = m.update(t, face, [], None, (640, 480), rppg=r)
            t += 1 / 30
    assert abs(s.hr_base - 70) < 1e-6 and s.hr_delta_pct < -15
    assert s.level >= WARN and any("HR LOW" in x for x in s.reasons)


def test_hud_renders():
    cfg = Config(calib_seconds=0.1)
    m = DriverMonitor(cfg)
    img = np.zeros((720, 1280, 3), np.uint8)
    t, C, _ = synth(dur=12)
    r = feed(cfg, t, C)
    r.res.hulls = []
    face = FaceObs(pts=np.zeros((478, 2), np.float32), ear_l=.3, ear_r=.3, ear=.3, mar=.1,
                   pitch=0, yaw=0, roll=0, nose_ratio=.5)
    for i in range(10):
        s = m.update(i * 0.05, face, [], None, (1280, 720), rppg=r.res)
    draw_hud(img, s, 30.0, True)
    draw_rppg(img, r.res)
    assert img.any()
