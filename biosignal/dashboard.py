"""생체신호 화면. 왼쪽 60%는 그래프, 오른쪽 40%는 판정 결과와 이유를 보여준다."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

from . import dsp

COLORS = {"normal": "#1a7f37", "caution": "#c77700", "danger": "#c62828", "unavailable": "#666666"}
TITLES = {"normal": "정상", "caution": "주의", "danger": "위험", "unavailable": "측정 불가"}

# 판정 사유 코드를 화면에 표시할 짧은 문구로 바꾼다
REASON_TEXT = {
    "NO_PULSE_CONFIRMED": "NO PULSE",
    "CHAOTIC_ECG_NO_PULSE": "NO PULSE + CHAOTIC ECG",
    "ABNORMAL_ECG_NO_PULSE": "NO PULSE + ABNORMAL ECG",
    "NO_PULSE_UNCONFIRMED": "NO PULSE? (1 SENSOR)",
    "HEART_PAUSE": "HEARTBEAT PAUSE",
    "EXTREME_TACHY": "VERY HIGH BPM",
    "EXTREME_BRADY": "VERY LOW BPM",
    "MODEL_DANGER": "DANGEROUS RHYTHM",
    "EXTREME_TACHY_UNCONFIRMED": "VERY HIGH BPM (1 SENSOR)",
    "EXTREME_BRADY_UNCONFIRMED": "VERY LOW BPM (1 SENSOR)",
    "MODEL_DANGER_UNCONFIRMED": "DANGEROUS RHYTHM (1 SENSOR)",
    "SENSOR_CONFLICT": "SENSOR MISMATCH",
    "HR_HIGH": "HIGH BPM",
    "HR_LOW": "LOW BPM",
    "HR_RISING": "BPM RISING",
    "HR_FALLING": "BPM FALLING",
    "MODEL_CAUTION": "ABNORMAL RHYTHM",
    "IRREGULAR_RHYTHM": "IRREGULAR RHYTHM",
    "HOLD_PREVIOUS_LEVEL": "WAITING FOR RECOVERY",
    "SENSOR_LOST": "SENSOR OFF",
    "LOW_SIGNAL_QUALITY": "POOR SIGNAL",
}


@dataclass
class PanelText:
    title: str
    color: str
    reasons: List[str] = field(default_factory=list)
    bpm: str = "--"
    trend: str = ""
    sensors: str = ""


def _fmt(value: Optional[float]) -> str:
    return "--" if value is None else f"{value:.0f}"


def summarize(status, error: Optional[str] = None) -> PanelText:
    """판정 결과를 화면 문구로 바꾼다. status는 BioStatus 또는 None이다."""
    if error:
        return PanelText("판정 오류", COLORS["danger"], reasons=[str(error)])
    if status is None:
        return PanelText("연결 중", COLORS["unavailable"])

    calibrating = "CALIBRATING" in status.reasons
    title = "기준 심박 측정 중" if calibrating else TITLES[status.level]
    # 정상일 때는 이유를 적지 않는다. 졸음 참고 지표는 등급이 아니므로 화면에 올리지 않는다
    reasons: List[str] = []
    if status.level != "normal" and not calibrating:
        for code in status.reasons:
            text = REASON_TEXT.get(code)
            if text and text not in reasons:
                reasons.append(text)

    if calibrating:
        trend = "첫 10초 측정값이 기준이 됩니다"
    elif status.hr_baseline is None:
        trend = "기준 --"
    elif status.hr_delta is None:
        trend = f"기준 {status.hr_baseline:.0f}"
    else:
        trend = f"기준 {status.hr_baseline:.0f}  ({status.hr_delta:+.0f})"

    rates = [f"{name.upper()} {_fmt(c['hr']) if c.get('reliable') else '--'}"
             for name, c in status.channels.items()]
    return PanelText(title, COLORS[status.level], reasons[:3], _fmt(status.hr), trend, "  |  ".join(rates))


def _autoscale(ax, values) -> None:
    low, high = float(min(values)), float(max(values))
    pad = max(2.0, (high - low) * 0.12)
    ax.set_ylim(low - pad, high + pad)


def filtered_waves(samples, window_s: float, margin_s: float = 3.0):
    """화면 표시용 필터 파형. 저장 CSV와 판정에는 영향을 주지 않는다.

    필터가 창 앞쪽에서 튀는 부분이 보이지 않도록 margin_s만큼 더 계산하고 잘라낸다.
    Returns: (시각, ECG, PPG) 또는 신호가 짧으면 None
    """
    now = float(samples[-1, 0])
    part = samples[samples[:, 0] >= now - window_s - margin_s]
    span = float(part[-1, 0] - part[0, 0])
    if len(part) < 2 or span < 1.5:
        return None
    fs = (len(part) - 1) / span
    grid = np.linspace(part[0, 0], part[-1, 0], len(part))
    ecg = dsp.clean_ecg(np.interp(grid, part[:, 0], part[:, 1]), fs)
    ppg = dsp.clean_pulse(np.interp(grid, part[:, 0], part[:, 2]), fs)
    keep = grid >= now - window_s
    return grid[keep], ecg[keep], ppg[keep]


class BioDashboard:
    def __init__(self, window_s: float = 5.0, trend_s: float = 60.0, figsize=(14, 8)):
        from matplotlib import font_manager, pyplot as plt

        installed = {f.name for f in font_manager.fontManager.ttflist}
        plt.rcParams["font.family"] = [f for f in ("Malgun Gothic", "AppleGothic", "NanumGothic") if f in installed] + ["DejaVu Sans"]
        plt.rcParams["axes.unicode_minus"] = False
        self.window_s, self.trend_s = window_s, trend_s
        self.fig = plt.figure(figsize=figsize)
        left, right = self.fig.subfigures(1, 2, width_ratios=[6, 4], wspace=0)

        self.ax_ecg, self.ax_ppg, self.ax_hr = left.subplots(3, 1)
        left.subplots_adjust(left=0.09, right=0.97, top=0.95, bottom=0.08, hspace=0.45)
        self.ecg_line, = self.ax_ecg.plot([], [], color="#168b65", linewidth=1)
        self.ppg_line, = self.ax_ppg.plot([], [], color="#9b4ed0", linewidth=1)
        self.hr_line, = self.ax_hr.plot([], [], color="#d25c25", linewidth=1.6, label="BPM")
        self.base_line = self.ax_hr.axhline(0, color="#555555", linestyle="--", linewidth=1, label="기준", visible=False)
        self.ax_ecg.set(title="ECG (필터 후: 60 Hz 노치 + 0.5–40 Hz)", ylabel="필터 출력")
        self.ax_ppg.set(title="PPG (필터 후: 0.5–4 Hz)", ylabel="필터 출력")
        self.ax_hr.set(title="심박수", xlabel="시간 (초)", ylabel="BPM", ylim=(40, 130))
        self.ax_hr.legend(loc="upper left", fontsize=8)
        for ax in (self.ax_ecg, self.ax_ppg, self.ax_hr):
            ax.grid(alpha=0.25)

        ax = right.add_axes((0, 0, 1, 1))
        ax.set(xlim=(0, 1), ylim=(0, 1))
        ax.axis("off")
        self.banner = ax.add_patch(self._rect(COLORS["unavailable"]))
        self.title = ax.text(0.5, 0.86, "", ha="center", va="center", color="white", fontsize=44, fontweight="bold")
        self.reason = ax.text(0.5, 0.60, "", ha="center", va="center", fontsize=23, fontweight="bold", linespacing=1.5)
        self.bpm = ax.text(0.5, 0.34, "", ha="center", va="center", fontsize=40, fontweight="bold", color="#222222")
        self.trend = ax.text(0.5, 0.24, "", ha="center", va="center", fontsize=16, color="#444444")
        self.sensors = ax.text(0.5, 0.17, "", ha="center", va="center", fontsize=13, color="#666666")
        self.footer = ax.text(0.5, 0.05, "", ha="center", va="center", fontsize=10, color="#777777")

        self._trend = deque(maxlen=int(trend_s * 2))
        self._last_trend_t = -float("inf")
        self._last_scale_t = -float("inf")

    @staticmethod
    def _rect(color):
        from matplotlib.patches import Rectangle
        return Rectangle((0, 0.72), 1, 0.28, facecolor=color, edgecolor="none")

    def update(self, samples, status, error: Optional[str] = None, footer: str = "") -> None:
        """samples: (시각, ECG, PPG) 행렬. 시각은 초 단위."""
        panel = summarize(status, error)
        self.banner.set_facecolor(panel.color)
        self.title.set_text(panel.title)
        self.title.set_fontsize(44 if len(panel.title) <= 5 else 30)
        self.reason.set_text("\n".join(panel.reasons))
        self.reason.set_color(panel.color)
        self.bpm.set_text(f"{panel.bpm} BPM")
        self.trend.set_text(panel.trend)
        self.sensors.set_text(panel.sensors)
        self.footer.set_text(footer)
        if samples is None or len(samples) == 0:
            return

        now = float(samples[-1, 0])
        waves = filtered_waves(samples, self.window_s)
        if waves is not None:
            t, ecg, ppg = waves
            self.ecg_line.set_data(t, ecg)
            self.ppg_line.set_data(t, ppg)
            x_range = (max(0.0, now - self.window_s), max(self.window_s, now))
            self.ax_ecg.set_xlim(*x_range)
            self.ax_ppg.set_xlim(*x_range)
            if now - self._last_scale_t >= 1.0:
                _autoscale(self.ax_ecg, ecg)
                _autoscale(self.ax_ppg, ppg)
                self._last_scale_t = now

        if now - self._last_trend_t >= 1.0:
            self._last_trend_t = now
            hr = None if status is None else status.hr
            self._trend.append((now, float("nan") if hr is None else hr))
            xs, ys = zip(*self._trend)
            self.hr_line.set_data(xs, ys)
            self.ax_hr.set_xlim(max(0.0, now - self.trend_s), max(self.trend_s, now))
            baseline = None if status is None else status.hr_baseline
            if baseline is not None:
                self.base_line.set_ydata([baseline, baseline])
                self.base_line.set_visible(True)
            valid = [y for y in ys if y == y] + ([baseline] if baseline is not None else [])
            if valid:
                self.ax_hr.set_ylim(min(40.0, min(valid) - 10), max(130.0, max(valid) + 10))

    def artists(self):
        return (self.ecg_line, self.ppg_line, self.hr_line, self.base_line, self.banner, self.title,
                self.reason, self.bpm, self.trend, self.sensors, self.footer)
