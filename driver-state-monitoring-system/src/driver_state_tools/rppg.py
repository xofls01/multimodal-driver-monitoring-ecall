from __future__ import annotations

import math
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from .vision_features import RawFeatures


@dataclass
class RPPGResult:
    timestamp: float
    status: str
    source: str | None = None
    heart_rate_bpm: float | None = None
    snr_db: float | None = None
    quality: float | None = None
    observed_seconds: float = 0.0
    minimum_seconds: float = 8.0
    forehead_skin_ratio: float | None = None
    cheeks_skin_ratio: float | None = None
    forehead_status: str = "unavailable"
    forehead_bpm: float | None = None
    forehead_snr_db: float | None = None
    cheeks_status: str = "unavailable"
    cheeks_bpm: float | None = None
    cheeks_snr_db: float | None = None
    agreement_bpm: float | None = None
    waveform: list[float] = field(default_factory=list)
    note: str = "prototype camera estimate; not a medical measurement"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # The compact waveform is for the on-screen sparkline, not long-term storage.
        data.pop("waveform", None)
        return data


class RPPGTracker:
    """POS-based rPPG prototype that measures forehead and cheeks in parallel.

    RGB traces remain independent. When both regions pass quality and temporal
    gates, their BPM estimates are fused only if they agree closely enough.
    """

    def __init__(self, config: dict[str, Any], sample_rate_hint: float | None = None) -> None:
        self.config = config
        self.window_seconds = float(config.get("rppg_window_seconds", 12.0))
        self.min_seconds = float(config.get("rppg_min_seconds", 8.0))
        self.nominal_fps = float(sample_rate_hint or config.get("analysis_fps", 8.0))
        self.buffers: dict[str, deque[tuple[float, np.ndarray]]] = {
            "forehead": deque(),
            "cheeks": deque(),
        }
        self.estimate_history: dict[str, deque[tuple[float, float]]] = {
            "forehead": deque(),
            "cheeks": deque(),
        }
        self.started_at: float | None = None

    def _append(self, source: str, timestamp: float, value: tuple[float, float, float] | None) -> None:
        if value is None:
            return
        array = np.asarray(value, dtype=np.float64)
        if array.shape != (3,) or not np.all(np.isfinite(array)) or np.any(array <= 1.0):
            return
        buffer = self.buffers[source]
        buffer.append((timestamp, array))

    def _trim(self, timestamp: float) -> None:
        cutoff = timestamp - self.window_seconds
        for buffer in self.buffers.values():
            while buffer and buffer[0][0] < cutoff:
                buffer.popleft()

    def _candidate(self, source: str) -> dict[str, Any] | None:
        samples = list(self.buffers[source])
        if len(samples) < 3:
            return None
        times = np.asarray([sample[0] for sample in samples], dtype=np.float64)
        duration = float(times[-1] - times[0])
        if duration < self.min_seconds:
            return None
        minimum_samples = max(16, int(self.min_seconds * self.nominal_fps * float(self.config.get("rppg_min_coverage", 0.65))))
        if len(samples) < minimum_samples:
            return None
        diffs = np.diff(times)
        positive = diffs[diffs > 1e-4]
        if positive.size == 0:
            return None
        sample_rate = float(1.0 / np.median(positive))
        if sample_rate < 4.0:
            return None
        uniform_times = np.arange(times[0], times[-1] + 1e-9, 1.0 / sample_rate)
        if uniform_times.size < 24:
            return None
        colors = np.asarray([sample[1] for sample in samples], dtype=np.float64)
        rgb = np.column_stack([np.interp(uniform_times, times, colors[:, channel]) for channel in range(3)])
        means = np.mean(rgb, axis=0)
        if np.any(means <= 1e-6):
            return None
        normalized = rgb / means - 1.0
        # Plane-Orthogonal-to-Skin (POS): S1=G-B, S2=G+B-2R.
        s1 = normalized[:, 1] - normalized[:, 2]
        s2 = normalized[:, 1] + normalized[:, 2] - 2.0 * normalized[:, 0]
        s2_std = float(np.std(s2))
        if s2_std <= 1e-9:
            return None
        pulse = s1 + (float(np.std(s1)) / s2_std) * s2
        pulse -= np.mean(pulse)
        pulse *= np.hanning(len(pulse))
        spectrum = np.fft.rfft(pulse)
        frequencies = np.fft.rfftfreq(len(pulse), d=1.0 / sample_rate)
        power = np.abs(spectrum) ** 2
        low = float(self.config.get("rppg_min_bpm", 42.0)) / 60.0
        high = min(float(self.config.get("rppg_max_bpm", 180.0)) / 60.0, sample_rate / 2.0 - 1e-6)
        band = (frequencies >= low) & (frequencies <= high)
        if not np.any(band) or float(np.sum(power[band])) <= 1e-12:
            return None
        band_indices = np.where(band)[0]
        peak_index = int(band_indices[np.argmax(power[band])])
        peak_frequency = float(frequencies[peak_index])
        signal_band = band & (np.abs(frequencies - peak_frequency) <= float(self.config.get("rppg_peak_band_hz", 0.12)))
        noise_band = band & ~signal_band
        # Peak SNR compares the local fundamental peak with the robust median
        # spectral floor. Integrating the entire 42-180 BPM band unfairly
        # penalizes short windows that necessarily have broad leakage.
        signal_level = float(np.mean(power[signal_band]))
        noise_level = float(np.median(power[noise_band])) if np.any(noise_band) else 0.0
        snr_db = 10.0 * math.log10((signal_level + 1e-12) / (noise_level + 1e-12))
        normalized_wave = pulse / (float(np.max(np.abs(pulse))) + 1e-9)
        sparkline = normalized_wave[-min(len(normalized_wave), 48):].astype(float).tolist()
        return {
            "source": source,
            "heart_rate_bpm": peak_frequency * 60.0,
            "snr_db": snr_db,
            "quality": float(np.clip((snr_db + 3.0) / 12.0, 0.0, 1.0)),
            "observed_seconds": duration,
            "waveform": sparkline,
        }

    def _stable(self, candidate: dict[str, Any] | None) -> dict[str, Any] | None:
        if candidate is None:
            return None
        source = str(candidate["source"])
        buffer = self.estimate_history[source]
        timestamp = self.buffers[source][-1][0]
        if not buffer or timestamp > buffer[-1][0] + 1e-6:
            buffer.append((timestamp, float(candidate["heart_rate_bpm"])))
        stability_seconds = float(self.config.get("rppg_stability_seconds", 3.0))
        while buffer and buffer[0][0] < timestamp - stability_seconds:
            buffer.popleft()
        minimum_snr = float(self.config.get("rppg_min_snr_db", 6.0))
        if candidate["snr_db"] < minimum_snr or len(buffer) < 5:
            return None
        if buffer[-1][0] - buffer[0][0] < stability_seconds * 0.75:
            return None
        values = np.asarray([item[1] for item in buffer], dtype=float)
        center = float(np.median(values))
        mad = float(np.median(np.abs(values - center)))
        if mad > float(self.config.get("rppg_max_bpm_mad", 8.0)):
            return None
        if float(np.ptp(values)) > float(self.config.get("rppg_max_bpm_range", 12.0)):
            return None
        stable = dict(candidate)
        stable["heart_rate_bpm"] = center
        return stable

    def update(self, raw: RawFeatures) -> RPPGResult:
        if self.started_at is None:
            self.started_at = raw.timestamp
        self._append("forehead", raw.timestamp, raw.rppg_forehead_rgb)
        self._append("cheeks", raw.timestamp, raw.rppg_cheeks_rgb)
        self._trim(raw.timestamp)
        forehead = self._candidate("forehead") if raw.rppg_forehead_rgb is not None else None
        cheeks = self._candidate("cheeks") if raw.rppg_cheeks_rgb is not None else None
        stable_forehead = self._stable(forehead)
        stable_cheeks = self._stable(cheeks)
        region_fields = {
            "forehead_status": "ready" if stable_forehead is not None else ("unstable" if forehead is not None else "unavailable"),
            "forehead_bpm": stable_forehead["heart_rate_bpm"] if stable_forehead is not None else None,
            "forehead_snr_db": forehead["snr_db"] if forehead is not None else None,
            "cheeks_status": "ready" if stable_cheeks is not None else ("unstable" if cheeks is not None else "unavailable"),
            "cheeks_bpm": stable_cheeks["heart_rate_bpm"] if stable_cheeks is not None else None,
            "cheeks_snr_db": cheeks["snr_db"] if cheeks is not None else None,
        }
        common = {
            "timestamp": raw.timestamp,
            "minimum_seconds": self.min_seconds,
            "forehead_skin_ratio": raw.rppg_forehead_skin_ratio,
            "cheeks_skin_ratio": raw.rppg_cheeks_skin_ratio,
            **region_fields,
        }
        if raw.rppg_forehead_rgb is None and raw.rppg_cheeks_rgb is None:
            elapsed = max(0.0, raw.timestamp - float(self.started_at))
            return RPPGResult(
                status="calibrating" if elapsed < self.min_seconds else "unavailable",
                observed_seconds=0.0,
                **common,
            )
        if stable_forehead is not None and stable_cheeks is not None:
            difference = abs(stable_forehead["heart_rate_bpm"] - stable_cheeks["heart_rate_bpm"])
            if difference <= float(self.config.get("rppg_fusion_max_delta_bpm", 10.0)):
                weights = np.asarray(
                    [max(0.05, stable_forehead["quality"]), max(0.05, stable_cheeks["quality"])], dtype=float
                )
                bpms = np.asarray([stable_forehead["heart_rate_bpm"], stable_cheeks["heart_rate_bpm"]], dtype=float)
                snrs = np.asarray([stable_forehead["snr_db"], stable_cheeks["snr_db"]], dtype=float)
                return RPPGResult(
                    status="ready",
                    source="fusion",
                    heart_rate_bpm=float(np.average(bpms, weights=weights)),
                    snr_db=float(np.average(snrs, weights=weights)),
                    quality=float(np.average([stable_forehead["quality"], stable_cheeks["quality"]], weights=weights)),
                    observed_seconds=min(stable_forehead["observed_seconds"], stable_cheeks["observed_seconds"]),
                    waveform=stable_forehead["waveform"] if stable_forehead["quality"] >= stable_cheeks["quality"] else stable_cheeks["waveform"],
                    agreement_bpm=difference,
                    **common,
                )
            return RPPGResult(
                status="unavailable",
                source="disagreement",
                observed_seconds=min(stable_forehead["observed_seconds"], stable_cheeks["observed_seconds"]),
                agreement_bpm=difference,
                **common,
            )
        chosen = stable_forehead or stable_cheeks
        if chosen is not None:
            return RPPGResult(status="ready", **common, **chosen)
        elapsed = max(0.0, raw.timestamp - float(self.started_at))
        best_observed = max(
            (buffer[-1][0] - buffer[0][0] for buffer in self.buffers.values() if len(buffer) >= 2),
            default=0.0,
        )
        if elapsed < self.min_seconds:
            return RPPGResult(status="calibrating", observed_seconds=best_observed, **common)
        best = max((candidate for candidate in (forehead, cheeks) if candidate is not None), key=lambda item: item["snr_db"], default=None)
        return RPPGResult(
            status="unavailable",
            source=best["source"] if best else None,
            snr_db=best["snr_db"] if best else None,
            quality=best["quality"] if best else None,
            observed_seconds=best["observed_seconds"] if best else best_observed,
            waveform=best["waveform"] if best else [],
            **common,
        )
