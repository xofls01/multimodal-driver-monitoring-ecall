"""rPPG: 얼굴 영상에서 심박수(BPM)와 HRV(RMSSD, 실험적)를 추정.

파이프라인
  1) 얼굴 랜드마크로 ROI(기본: 이마, 필요할 때 양 볼 추가)를 잡고 평균 RGB 기록
  2) 타임스탬프 기준으로 일정 fs(30Hz)로 재샘플링 (웹캠 FPS 흔들림 보정)
  3) POS 알고리즘 (Wang et al., 2017, IEEE TBME)으로 맥파 신호 추출
     * 흑백(IR) 영상이면 밝기 채널 기반으로 자동 전환
  4) FFT 로 0.7~3.0Hz(42~180bpm) 대역 최대 피크 → BPM, 피크/잡음 비율 → SNR(dB)
  5) HRV: 대역 필터된 신호를 업샘플링해 피크 검출 → 박동 간격(IBI) → RMSSD / SDNN

ROI 모드 (cfg.rppg_roi_mode)
  "forehead"       이마만
  "forehead_first" 이마 우선. 이마가 가려지거나(유효 픽셀 부족) SNR 이 계속 낮으면 볼을 추가
  "all"            이마 + 양 볼 (예전 방식)
  ROI 마다 버퍼를 따로 두므로, ROI 를 바꿔도 신호가 섞이거나 처음부터 다시 쌓지 않는다.

scipy 없이 numpy + OpenCV 만 사용.
"""
from __future__ import annotations

import dataclasses
import math
from collections import deque
from dataclasses import dataclass, field

import cv2
import numpy as np

# ---------------- ROI (MediaPipe Face Mesh 인덱스) ----------------
# 이마는 고정된 점들의 볼록 껍질 대신 랜드마크 세 줄 사이를 보간해서 만든다.
# (MediaPipe 기준 얼굴 메시에서 잰 높이, 눈썹 윗선 = 0)
#   FH_BROW 눈썹 윗선 105,107 / 336,334 + 미간 위 9      0 cm
#   FH_MID  이마 가운데 줄                               약 +1.5 cm
#   FH_TOP  얼굴 윤곽 맨 위 (머리선이라 앞머리가 걸림)   약 +3.2 cm
# 예전 ROI 는 MID~TOP 이라 머리선까지 들어갔다. 지금은 아래 변을 BROW→MID 의 40%(눈썹 위 약 0.6cm),
# 위 변을 MID→TOP 의 60%(머리선 아래 약 0.7cm)로 잡아 눈썹과 머리카락을 피하고, 높이는 비슷하게 유지.
FH_BROW = [105, 107, 9, 336, 334]
FH_MID = [69, 108, 151, 337, 299]
FH_TOP = [67, 109, 10, 338, 297]
FH_LOW_T, FH_HIGH_T = 0.4, 0.6
L_CHEEK = [116, 117, 118, 119, 100, 142, 203, 206, 207, 187, 147, 123, 50, 101, 36, 205]
R_CHEEK = [345, 346, 347, 348, 329, 371, 423, 426, 427, 411, 376, 352, 280, 330, 266, 425]

ROI_FOREHEAD, ROI_ALL = "forehead", "forehead+cheeks"
ROI_MODES = {"forehead": [ROI_FOREHEAD], "forehead_first": [ROI_FOREHEAD, ROI_ALL], "all": [ROI_ALL]}

MIN_PX = 50                 # 영역 유효 픽셀이 이보다 적으면 그 영역은 이번 프레임에 안 씀
SKIN_CHROMA_TOL = 12.0      # 기준 피부(볼)와 Cr/Cb 차이 허용치
SKIN_Y_MIN = 0.5            # 밝기가 기준 피부의 이 비율 미만이면 제외 (머리카락, 눈썹, 짙은 그림자)
SKIN_MIN_FRAC = 0.3         # 피부 판정을 통과한 비율이 이보다 낮으면 조명 탓으로 보고 피부 마스크를 끔


def forehead_points(pts: np.ndarray) -> np.ndarray:
    lo = pts[FH_BROW] + FH_LOW_T * (pts[FH_MID] - pts[FH_BROW])
    hi = pts[FH_MID] + FH_HIGH_T * (pts[FH_TOP] - pts[FH_MID])
    return np.vstack([lo, hi[::-1]])


def roi_polys(pts: np.ndarray) -> dict[str, list[np.ndarray]]:
    return {"forehead": [cv2.convexHull(forehead_points(pts).astype(np.int32))],
            "cheeks": [cv2.convexHull(pts[idx].astype(np.int32)) for idx in (L_CHEEK, R_CHEEK)]}


def _skin_mask(crop: np.ndarray, good: np.ndarray, masks: dict) -> np.ndarray:
    """볼 피부색(중앙값)과 비슷한 픽셀만 True. 볼이 안 잡히거나 대부분 탈락하면 good 그대로."""
    ycc = cv2.cvtColor(crop, cv2.COLOR_BGR2YCrCb).astype(np.float32)
    ref_m = (masks["cheeks"] > 0) & good
    if np.count_nonzero(ref_m) < MIN_PX:
        return good
    ref = np.median(ycc[ref_m], axis=0)
    skin = ((ycc[..., 0] >= SKIN_Y_MIN * ref[0])
            & (np.abs(ycc[..., 1] - ref[1]) <= SKIN_CHROMA_TOL)
            & (np.abs(ycc[..., 2] - ref[2]) <= SKIN_CHROMA_TOL))
    roi = ((masks["forehead"] > 0) | (masks["cheeks"] > 0)) & good
    if np.count_nonzero(skin & roi) < SKIN_MIN_FRAC * np.count_nonzero(roi):
        return good
    return good & skin


def roi_stats(frame_bgr: np.ndarray, pts: np.ndarray, skin: bool = True):
    """영역별 {"forehead"|"cheeks": (평균 (R,G,B) 또는 None, 유효 픽셀 수)} 와 영역 다각형."""
    polys = roi_polys(pts)
    h, w = frame_bgr.shape[:2]
    allp = np.concatenate([hh for hs in polys.values() for hh in hs]).reshape(-1, 2)
    x0, y0 = np.clip(allp.min(0), 0, [w - 1, h - 1])
    x1, y1 = np.clip(allp.max(0) + 1, 1, [w, h])
    if x1 - x0 < 4 or y1 - y0 < 4:
        return {k: (None, 0) for k in polys}, polys
    crop = frame_bgr[y0:y1, x0:x1]
    off = np.array([x0, y0], np.int32)
    masks = {}
    for k, hs in polys.items():
        m = np.zeros(crop.shape[:2], np.uint8)
        for hh in hs:
            cv2.fillPoly(m, [hh.reshape(-1, 2) - off], 255)
        masks[k] = m
    # 과노출/암부 픽셀 제외 (반사광, 그림자)
    mx, mn = crop.max(axis=2), crop.min(axis=2)
    good = (mx < 250) & (mn > 5)
    if skin:
        good = _skin_mask(crop, good, masks)
    out = {}
    for k, m in masks.items():
        m[~good] = 0
        n = cv2.countNonZero(m)
        if n < MIN_PX:
            out[k] = (None, n)
        else:
            b, g, r, _ = cv2.mean(crop, mask=m)
            out[k] = ((r, g, b), n)
    return out, polys


def _union_rgb(stats: dict):
    """이마+볼 전체 평균 (픽셀 수 가중). 예전 방식(마스크 하나로 평균)과 같은 값."""
    parts = [(np.asarray(rgb, float), n) for rgb, n in stats.values() if rgb is not None]
    if not parts:
        return None
    tot = sum(n for _, n in parts)
    return tuple(sum(rgb * n for rgb, n in parts) / tot)


# ---------------- 신호 처리 ----------------
_P = np.array([[0.0, 1.0, -1.0], [-2.0, 1.0, 1.0]])


def pos(C: np.ndarray, fs: float, win_s: float = 1.6) -> np.ndarray:
    """POS 알고리즘. C: (N,3) RGB → (N,) 맥파 신호."""
    N = len(C)
    l = max(2, int(round(win_s * fs)))
    H = np.zeros(N)
    if N < l:
        return H
    win = np.lib.stride_tricks.sliding_window_view(C, l, axis=0)      # (K, 3, l)
    mu = win.mean(axis=2, keepdims=True)
    ok = np.all(mu[:, :, 0] > 1e-6, axis=1)
    Cn = win / np.where(mu > 1e-6, mu, 1.0)
    S = np.einsum("ij,kjl->kil", _P, Cn)                               # (K, 2, l)
    s1, s2 = S[:, 0], S[:, 1]
    sd2 = s2.std(axis=1, keepdims=True)
    alpha = np.where(sd2 > 1e-12, s1.std(axis=1, keepdims=True) / np.where(sd2 > 1e-12, sd2, 1), 0.0)
    h = s1 + alpha * s2
    h = (h - h.mean(axis=1, keepdims=True)) * ok[:, None]
    for k in range(l):                                                 # overlap-add
        H[k:k + len(h)] += h[:, k]
    return H


def mono_signal(x: np.ndarray, fs: float, win_s: float = 1.6) -> np.ndarray:
    """흑백 영상용: 구간별 정규화한 밝기 변화 (혈액량이 늘면 어두워지므로 부호 반전)."""
    N = len(x)
    l = max(2, int(round(win_s * fs)))
    H = np.zeros(N)
    for n in range(l, N + 1):
        seg = x[n - l:n]
        mu = seg.mean()
        if mu <= 1e-6:
            continue
        h = -(seg / mu - 1.0)
        H[n - l:n] += h - h.mean()
    return H


def detrend(x: np.ndarray) -> np.ndarray:
    n = np.arange(len(x))
    a, b = np.polyfit(n, x, 1)
    return x - (a * n + b)


def spectrum_hr(x: np.ndarray, fs: float, lo: float, hi: float):
    """(bpm, snr_dB, peak_hz). SNR = (기본파±0.1Hz + 2차 고조파±0.2Hz) / 나머지 대역 파워."""
    x = detrend(x) * np.hanning(len(x))
    nfft = max(8192, 1 << (int(math.ceil(math.log2(len(x)))) + 2))
    f = np.fft.rfftfreq(nfft, 1.0 / fs)
    P = np.abs(np.fft.rfft(x, nfft)) ** 2
    band = (f >= lo) & (f <= hi)
    if not band.any() or P[band].sum() <= 0:
        return 0.0, -99.0, 0.0
    fp = f[band][np.argmax(P[band])]
    ev = (f >= 0.5) & (f <= 4.0)
    sig = ev & ((np.abs(f - fp) <= 0.1) | (np.abs(f - 2 * fp) <= 0.2))
    noise = ev & ~sig
    ps, pn = P[sig].sum(), P[noise].sum()
    snr = 10 * math.log10(ps / pn) if pn > 0 and ps > 0 else -99.0
    return fp * 60.0, snr, fp


def bandpass_fft(x: np.ndarray, fs: float, lo: float, hi: float, up: int = 1) -> np.ndarray:
    """FFT 마스킹 대역통과. up>1 이면 대역 제한 보간으로 up 배 업샘플링."""
    x = detrend(x)
    n = len(x)
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(n, 1.0 / fs)
    X[(f < lo) | (f > hi)] = 0
    return np.fft.irfft(X, n * up) * up


def find_peaks(y: np.ndarray, min_dist: int) -> np.ndarray:
    """양수 극대값 중 min_dist 이상 떨어진 것만 (높은 것 우선)."""
    if len(y) < 3:
        return np.array([], int)
    c = np.where((y[1:-1] > y[:-2]) & (y[1:-1] >= y[2:]) & (y[1:-1] > 0))[0] + 1
    keep: list[int] = []
    for i in c[np.argsort(-y[c])]:
        if all(abs(i - k) >= min_dist for k in keep):
            keep.append(int(i))
    return np.array(sorted(keep), int)


def refine_peaks(y: np.ndarray, pk: np.ndarray) -> np.ndarray:
    """포물선 보간으로 서브샘플 피크 위치."""
    out = pk.astype(float)
    for j, i in enumerate(pk):
        if 0 < i < len(y) - 1:
            a, b, c = y[i - 1], y[i], y[i + 1]
            d = a - 2 * b + c
            if abs(d) > 1e-12:
                out[j] = i + 0.5 * (a - c) / d
    return out


def hrv_from_signal(x: np.ndarray, fs: float, hr_hz: float, lo: float, hi: float):
    """(rmssd_ms, sdnn_ms, n_ibi). 박동 수가 부족하면 nan."""
    up = max(1, int(math.ceil(250.0 / fs)))
    blo, bhi = max(lo, hr_hz - 0.6), min(hi + 0.5, hr_hz + 0.6)
    y = bandpass_fft(x, fs, blo, bhi, up)
    fsu = fs * up
    pk = find_peaks(y, int(0.6 * fsu / hr_hz))
    if len(pk) < 6:
        return math.nan, math.nan, 0
    ibi = np.diff(refine_peaks(y, pk)) / fsu * 1000.0
    med = np.median(ibi)
    ibi = ibi[(ibi > 0.7 * med) & (ibi < 1.3 * med)]  # 이상 박동 제거
    if len(ibi) < 8:
        return math.nan, math.nan, len(ibi)
    rmssd = float(np.sqrt(np.mean(np.diff(ibi) ** 2)))
    return rmssd, float(np.std(ibi)), len(ibi)


# ---------------- 실시간 추정기 ----------------
@dataclass
class RPPGResult:
    bpm: float = 0.0            # 최근 유효 추정값의 중앙값
    raw_bpm: float = 0.0        # 이번 창의 추정값
    snr: float = -99.0
    valid: bool = False
    progress: float = 0.0       # 버퍼 채움 정도 (0~1)
    rmssd: float = math.nan
    sdnn: float = math.nan
    n_ibi: int = 0
    mode: str = "rgb"           # "rgb" | "mono"
    fps: float = 0.0
    msg: str = ""
    seq: int = 0                # 새 추정이 나올 때마다 +1
    wave: np.ndarray | None = None
    hulls: list = field(default_factory=list)   # 지금 쓰는 ROI 의 다각형만
    roi: str = ROI_FOREHEAD     # 지금 쓰는 ROI
    roi_reason: str = ""        # 볼을 추가한 이유 ("forehead covered" | "forehead low SNR")
    fh_px: int = 0              # 이마 유효 픽셀 수


class _Estimator:
    """ROI 하나의 평균 RGB 시계열 → 심박/HRV."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.buf: deque[tuple[float, float, float, float]] = deque()
        self.hist: deque[float] = deque(maxlen=5)
        self.res = RPPGResult()
        self._last_calc = -1e9
        self._last_t = None

    def reset(self, msg=""):
        self.buf.clear()
        self.hist.clear()
        seq = self.res.seq
        self.res = RPPGResult(msg=msg, seq=seq)

    def push(self, t: float, rgb) -> RPPGResult:
        c = self.cfg
        if self._last_t is not None and t - self._last_t > c.rppg_gap_s:
            self.reset("gap")
        self._last_t = t
        self.buf.append((t, *rgb))
        keep = max(c.rppg_hr_window_s, c.rppg_hrv_window_s)
        while self.buf and t - self.buf[0][0] > keep:
            self.buf.popleft()
        span = t - self.buf[0][0]
        self.res.progress = min(1.0, span / c.rppg_hr_window_s)
        if span >= c.rppg_hr_window_s and t - self._last_calc >= c.rppg_update_s:
            self._last_calc = t
            self._compute()
        return self.res

    def _compute(self):
        c, r = self.cfg, self.res
        a = np.array(self.buf, dtype=float)
        ts, C = a[:, 0], a[:, 1:]
        span = ts[-1] - ts[0]
        r.fps = (len(ts) - 1) / span if span > 0 else 0.0
        if r.fps < c.rppg_min_fps:
            r.valid, r.msg = False, f"fps too low ({r.fps:.0f})"
            return
        fs = c.rppg_fs
        tu = np.arange(ts[0], ts[-1], 1.0 / fs)
        Cu = np.stack([np.interp(tu, ts, C[:, k]) for k in range(3)], 1)

        chroma = np.abs(Cu[:, 0] - Cu[:, 1]).mean() + np.abs(Cu[:, 1] - Cu[:, 2]).mean()
        r.mode = "mono" if chroma < 1.0 else "rgb"
        x = mono_signal(Cu.mean(1), fs) if r.mode == "mono" else pos(Cu, fs)

        n_hr = int(c.rppg_hr_window_s * fs)
        bpm, snr, fp = spectrum_hr(x[-n_hr:], fs, c.rppg_min_hz, c.rppg_max_hz)
        r.raw_bpm, r.snr, r.seq = bpm, snr, r.seq + 1
        r.valid = snr >= c.rppg_snr_thr
        if r.valid:
            self.hist.append(bpm)
            r.msg = ""
        else:
            r.msg = "low signal quality"
        r.bpm = float(np.median(self.hist)) if self.hist else 0.0

        wav = bandpass_fft(x[-n_hr:], fs, c.rppg_min_hz, c.rppg_max_hz)
        n5 = int(5 * fs)
        w = wav[-n5:]
        r.wave = w / (np.abs(w).max() + 1e-12)

        r.rmssd, r.sdnn, r.n_ibi = math.nan, math.nan, 0
        # HRV 는 잡음에 매우 민감(잡음이 RMSSD 를 부풀림) → 신호가 아주 깨끗할 때만 계산
        if c.rppg_hrv and snr >= c.rppg_hrv_snr_thr and span >= 0.9 * c.rppg_hrv_window_s:
            r.rmssd, r.sdnn, r.n_ibi = hrv_from_signal(x, fs, fp, c.rppg_min_hz, c.rppg_max_hz)


class _Since:
    """조건이 연속으로 참이 된 시각."""

    def __init__(self):
        self.t: float | None = None

    def set(self, cond: bool, t: float):
        if not cond:
            self.t = None
        elif self.t is None:
            self.t = t

    def dur(self, t: float) -> float:
        return 0.0 if self.t is None else t - self.t


class RPPG:
    def __init__(self, cfg):
        self.cfg = cfg
        mode = cfg.rppg_roi_mode
        if mode not in ROI_MODES:
            print(f"[rppg] 알 수 없는 rppg_roi_mode={mode!r} → forehead_first")
            mode = "forehead_first"
        self.mode = mode
        self.est = {name: _Estimator(cfg) for name in ROI_MODES[mode]}
        self.res = RPPGResult()
        self._seq = 0
        self._last_t = None
        self._reset_select()
        self._publish()

    def _reset_select(self):
        self.active = ROI_MODES[self.mode][0]
        self.reason = ""
        self.fh_px = 0
        self._seen = None
        self._f_seq = -1
        self._px_low, self._px_ok = _Since(), _Since()
        self._snr_low, self._snr_ok = _Since(), _Since()

    @property
    def buf(self):
        """지금 쓰는 ROI 의 버퍼."""
        return self.est[self.active].buf

    def reset(self, msg=""):
        for e in self.est.values():
            e.reset(msg)
        self._reset_select()
        self._publish()

    def update(self, t: float, frame_bgr, face) -> RPPGResult:
        if face is None:
            if self._last_t is not None and t - self._last_t > self.cfg.rppg_gap_s and self.buf:
                self.reset("face lost")
            self.res.hulls = []
            return self.res
        stats, polys = roi_stats(frame_bgr, face.pts, self.cfg.rppg_skin_mask)
        self.push_stats(t, stats)
        self.res.hulls = polys["forehead"] + (polys["cheeks"] if self.active == ROI_ALL else [])
        return self.res

    def push(self, t: float, rgb) -> RPPGResult:
        """평균 RGB 한 샘플 추가 (이마/볼 구분이 없는 테스트·녹화 데이터용)."""
        big = 1 << 30
        return self.push_stats(t, {"forehead": (rgb, big), "cheeks": (rgb, big)})

    def push_stats(self, t: float, stats: dict) -> RPPGResult:
        """영역별 (평균 RGB|None, 유효 픽셀 수) 한 프레임 추가. roi_stats() 의 출력 형식."""
        fh_rgb, self.fh_px = stats.get("forehead", (None, 0))
        for name, e in self.est.items():
            rgb = fh_rgb if name == ROI_FOREHEAD else _union_rgb(stats)
            if rgb is not None:
                e.push(t, rgb)
                self._last_t = t
        if self.mode == "forehead_first":
            self._select(t)
        self._publish()
        return self.res

    def _select(self, t: float):
        """forehead_first: 이마 상태를 보고 이마 / 이마+볼 중 하나를 고른다."""
        c = self.cfg
        f = self.est[ROI_FOREHEAD].res
        px_low = self.fh_px < c.rppg_forehead_min_px
        self._px_low.set(px_low, t)
        self._px_ok.set(not px_low, t)
        if f.progress < 1.0:                    # 이마 버퍼가 다시 쌓이는 중이면 SNR 판단 보류
            self._snr_low.set(False, t)
            self._snr_ok.set(False, t)
        elif f.seq != self._f_seq:              # 이마 추정이 새로 나왔을 때만 갱신
            self._f_seq = f.seq
            self._snr_low.set(f.snr < c.rppg_snr_thr, t)
            self._snr_ok.set(f.snr >= c.rppg_snr_thr, t)
        if self.active == ROI_FOREHEAD:
            if self._px_low.dur(t) >= c.rppg_gap_s:
                self._switch(ROI_ALL, "forehead covered")
            elif self._snr_low.dur(t) >= c.rppg_roi_fallback_s:
                self._switch(ROI_ALL, "forehead low SNR")
        elif self._px_ok.dur(t) >= c.rppg_roi_recover_s and self._snr_ok.dur(t) >= c.rppg_roi_recover_s:
            self._switch(ROI_FOREHEAD, "")

    def _switch(self, roi: str, reason: str):
        if (roi, reason) != getattr(self, "_last_print", None):     # 같은 전환이 반복되면 한 번만 출력
            print(f"[rppg] ROI -> {roi}" + (f" ({reason})" if reason else ""))
            self._last_print = (roi, reason)
        self.active, self.reason = roi, reason

    def _publish(self):
        e = self.est[self.active]
        key = (self.active, e.res.seq)
        if key != self._seen:                   # 지금 ROI 에서 새 추정이 나왔거나 ROI 가 바뀜
            self._seen = key
            self._seq += 1
        self.res = dataclasses.replace(e.res, seq=self._seq, hulls=self.res.hulls, roi=self.active,
                                       roi_reason=self.reason, fh_px=int(self.fh_px))
