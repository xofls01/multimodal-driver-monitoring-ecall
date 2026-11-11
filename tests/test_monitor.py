"""판정 로직 회귀 테스트. 실행: python -m unittest discover -s tests -v

모델 없이 규칙 경로를 검사한다. BIOSIGNAL_MODEL(ECG)과 BIOSIGNAL_PPG_ENCODER·BIOSIGNAL_PPG_HEAD(PPG)
환경변수에 가중치 경로를 주면 모델 경로로 검사한다.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np

from biosignal import BioSignalMonitor, Config
from biosignal import simulate as sim
from biosignal.dsp import detect_pulse_peaks

MODELS = dict(model_path=os.environ.get("BIOSIGNAL_MODEL"),
              ppg_encoder_path=os.environ.get("BIOSIGNAL_PPG_ENCODER"),
              ppg_head_path=os.environ.get("BIOSIGNAL_PPG_HEAD"))


def run(name, seed=0):
    s = sim.SCENARIOS[name](seed=seed)
    return s, list(sim.replay(BioSignalMonitor(Config(), **MODELS), s))


def levels_after(rows, t):
    return {st.level for st in rows if st.t > t}


class Scenarios(unittest.TestCase):
    def test_normal_never_alarms(self):
        for name in ("normal", "driving_vibration"):
            for seed in range(3):
                _, rows = run(name, seed)
                self.assertNotIn("danger", levels_after(rows, 15), f"{name} seed {seed}")
                self.assertEqual(rows[-1].level, "normal", f"{name} seed {seed}")

    def test_hr_within_5_bpm(self):
        s, rows = run("normal")
        errs = [abs(st.hr - s.true_hr(st.t)) for st in rows if st.t > 15 and st.hr]
        self.assertLessEqual(max(errs), 5.0)

    def test_arrest_is_danger_when_both_sensors_agree(self):
        # 심실세동은 ECG 모델이 판정한다. 규칙만으로는 리듬 없는 잡음과 구분할 수 없어 판단 불가다
        names = ["cardiac_arrest"] + (["ventricular_fibrillation"] if MODELS["model_path"] else [])
        for name in names:
            s, rows = run(name)
            self.assertEqual(rows[-1].level, "danger", name)
            self.assertNotIn("danger", {st.level for st in rows if st.t < s.onset}, name)
            first = next(st.t for st in rows if st.level == "danger")
            self.assertLessEqual(first - s.onset, 20.0, name)

    def test_extreme_hr_is_danger_when_both_sensors_agree(self):
        for name in ("bradycardia_35", "tachycardia_165"):
            _, rows = run(name)
            self.assertEqual(rows[-1].level, "danger", name)
            self.assertEqual(rows[-1].hr_class, "danger", name)

    def test_chaotic_ecg_without_model_is_unavailable(self):
        if MODELS["model_path"]:
            self.skipTest("모델 경로에서는 위험으로 판정한다")
        _, rows = run("ventricular_fibrillation")
        self.assertEqual(rows[-1].level, "unavailable")
        self.assertIn("CHAOTIC_ECG_NO_PULSE", rows[-1].reasons)

    def test_single_sensor_danger_is_unavailable(self):
        """판단 불가 4·5번: 위험 근거가 한 센서뿐이고 다른 센서를 믿을 수 없다."""
        for name, reason in (("ecg_only_arrest", "NO_PULSE_UNCONFIRMED"),
                             ("ecg_only_tachycardia", "EXTREME_TACHY_UNCONFIRMED")):
            _, rows = run(name)
            self.assertEqual(rows[-1].level, "unavailable", name)
            self.assertIn(reason, rows[-1].reasons, name)
            self.assertTrue(rows[-1].needs_second_opinion, name)

    def test_single_sensor_140_is_caution(self):
        """한 센서만 131~149회면 위험도 판단 불가도 아니고 주의다."""
        s = sim.ecg_only_tachycardia()
        s2 = sim.build(seed=0, hr_fn=lambda t: 72.0 if t < 60 else 140.0)
        s.ecg = s2.ecg
        rows = list(sim.replay(BioSignalMonitor(Config()), s))
        self.assertEqual(rows[-1].level, "caution")
        self.assertIn("HR_HIGH", rows[-1].reasons)

    def test_sensor_mismatch_is_unavailable(self):
        """판단 불가 3번: 두 센서가 깨끗한데 심박수가 10회 넘게 다르다."""
        s = sim.build(seed=0, hr_fn=lambda t: 72.0)
        p = sim.build(seed=1, hr_fn=lambda t: 72.0 if t < 40 else 95.0)
        s.ppg = p.ppg
        rows = list(sim.replay(BioSignalMonitor(Config()), s))
        self.assertEqual(rows[-1].level, "unavailable")
        self.assertIn("SENSOR_MISMATCH", rows[-1].reasons)

    def test_electrode_off_is_not_cardiac_arrest(self):
        _, rows = run("electrode_off")
        self.assertNotIn("danger", levels_after(rows, 15))
        self.assertEqual(rows[-1].level, "normal")
        self.assertIn(rows[-1].hr_class_source, ("rule", "ppg_model"))  # ECG 없이 PPG로 판정

    def test_all_contact_lost_is_unavailable(self):
        _, rows = run("all_contact_lost")
        self.assertEqual(rows[-1].level, "unavailable")
        self.assertIn("SENSOR_LOST", rows[-1].reasons)

    def test_ecg_artifact_does_not_alarm(self):
        _, rows = run("ecg_motion_artifact")
        self.assertNotIn("danger", levels_after(rows, 15))

    def test_danger_survives_sensor_loss(self):
        """위험 판정 뒤 센서가 떨어져도 경보가 풀리면 안 된다."""
        s = sim.cardiac_arrest()
        cut = 90.0
        s.ecg_lo[s.ecg_t >= cut] = True
        s.ppg_contact[s.ppg_t >= cut] = False
        rows = list(sim.replay(BioSignalMonitor(Config(), **MODELS), s))
        self.assertEqual(rows[-1].level, "danger")

    def test_short_sensor_gap_does_not_clear_caution(self):
        """주의 중 2초 센서 끊김: 그동안은 판단 불가, 돌아오면 바로 주의. 정상으로 새지 않는다."""
        s = sim.tachycardia_mild()
        a, b = 100.0, 102.0
        s.ecg_lo[(s.ecg_t >= a) & (s.ecg_t < b)] = True
        s.ppg_contact[(s.ppg_t >= a) & (s.ppg_t < b)] = False
        rows = list(sim.replay(BioSignalMonitor(Config(), **MODELS), s))
        before = [st.level for st in rows if 90 <= st.t < a]
        gap = [st.level for st in rows if a <= st.t < b]
        after = [st.level for st in rows if st.t >= b]
        self.assertEqual(set(before), {"caution"})
        self.assertEqual(set(gap), {"unavailable"})  # 판단 불가 7번은 그대로
        self.assertEqual(set(after), {"caution"})

    def test_long_sensor_gap_still_needs_clear_hold(self):
        """주의 중 15초 끊긴 뒤 정상 심박으로 돌아와도 clear_hold_s 동안은 주의를 유지한다."""
        s = sim.build(dur=150.0, seed=0, hr_fn=lambda t: 72.0 if t < 60 or t >= 100 else 125.0,
                      onset=60.0)
        a, b = 100.0, 115.0
        s.ecg_lo[(s.ecg_t >= a) & (s.ecg_t < b)] = True
        s.ppg_contact[(s.ppg_t >= a) & (s.ppg_t < b)] = False
        cfg = Config()
        rows = list(sim.replay(BioSignalMonitor(cfg, **MODELS), s))
        normal_t = next(st.t for st in rows if st.t >= b and st.level == "normal")
        self.assertGreaterEqual(normal_t - b, cfg.clear_hold_s - 3.0)
        self.assertEqual(rows[-1].level, "normal")

    def test_stale_stream_is_not_danger(self):
        s = sim.normal()
        mon = BioSignalMonitor(Config(), **MODELS)
        rows = list(sim.replay(mon, s))
        later = mon.update(float(s.ecg_t[-1]) + 30.0)
        self.assertEqual(rows[-1].level, "normal")
        self.assertIn("SENSOR_LOST", later.reasons)
        self.assertNotEqual(later.level, "danger")


class Baseline(unittest.TestCase):
    def test_measured_in_first_10_seconds_then_fixed(self):
        _, rows = run("normal")
        early = [st for st in rows if st.t < 10]
        self.assertTrue(all(st.level == "unavailable" for st in early))
        self.assertTrue(all(st.hr_baseline is None for st in early))
        ready = next(st for st in rows if st.hr_baseline is not None)
        self.assertLessEqual(ready.t, 12.0)
        self.assertLess(abs(ready.hr_baseline - 72.0), 3.0)
        self.assertEqual({st.hr_baseline for st in rows if st.t >= ready.t}, {ready.hr_baseline})

    def test_delta_tracks_rise_and_reports_direction(self):
        s, rows = run("abrupt_rise_70_110")
        last = rows[-1]
        self.assertGreater(last.hr_delta, 30.0)
        self.assertEqual(last.level, "caution")
        self.assertIn("HR_RISING", last.reasons)
        self.assertIn("HR_HIGH", last.reasons)

    def test_fall_within_normal_range_is_caution(self):
        _, rows = run("abrupt_fall_95_62")
        self.assertEqual(rows[-1].level, "caution")
        self.assertIn("HR_FALLING", rows[-1].reasons)
        self.assertNotIn("HR_LOW", rows[-1].reasons)

    def test_excited_start_is_not_used_as_baseline(self):
        _, rows = run("high_start_settles")
        self.assertTrue(all(st.hr_baseline is None for st in rows if st.t < 40))
        self.assertLess(abs(rows[-1].hr_baseline - 74.0), 4.0)
        self.assertEqual(rows[-1].level, "normal")

    def test_naturally_low_heart_rate_is_not_flagged(self):
        _, rows = run("athlete_low_baseline")
        self.assertLess(abs(rows[-1].hr_baseline - 46.0), 3.0)
        self.assertEqual(rows[-1].level, "normal")


class HeartRateBands(unittest.TestCase):
    def test_mild_out_of_range_is_caution_with_reason(self):
        for name, reason in (("tachycardia_125", "HR_HIGH"), ("bradycardia_45", "HR_LOW"),
                             ("gradual_rise_72_112", "HR_HIGH")):
            _, rows = run(name)
            self.assertEqual(rows[-1].level, "caution", name)
            self.assertIn(reason, rows[-1].reasons, name)

    def test_boundaries_follow_guideline_definitions(self):
        # 정상 51~100 / 주의 41~50, 101~130 / 위험 40 이하, 131 이상
        for hr, level in ((100.0, "normal"), (104.0, "caution"), (54.0, "normal"), (46.0, "caution"),
                          (128.0, "caution"), (136.0, "danger"), (38.0, "danger")):
            s = sim.build(dur=45.0, seed=0, hr_fn=lambda t, hr=hr: 72.0 if t < 20 else hr)
            rows = list(sim.replay(BioSignalMonitor(Config()), s))
            # 72에서 잰 기준선이 있으므로 상승·하강 사유는 빼고 절대값 사유만 본다
            if level == "danger":
                self.assertEqual(rows[-1].level, "danger", f"hr={hr}: {rows[-1].reasons}")
                continue
            flagged = bool({"HR_HIGH", "HR_LOW"} & set(rows[-1].reasons))
            self.assertEqual(flagged, level == "caution", f"hr={hr}: {rows[-1].reasons} {rows[-1].hr}")

    def test_exact_boundaries(self):
        mon = BioSignalMonitor(Config())
        for hr, cls in ((40, "danger"), (41, "caution"), (50, "caution"), (51, "normal"),
                        (100, "normal"), (101, "caution"), (130, "caution"), (131, "danger")):
            self.assertEqual(mon._rule_class(float(hr)), cls, hr)


class SuddenArrest(unittest.TestCase):
    def no_flag_arrest(self):
        """실제 장비처럼 접촉 플래그 없이 ADC 범위 값만 넣는다."""
        s = sim.cardiac_arrest(onset=60.0)
        ppg = 500.0 + (np.interp(s.ecg_t, s.ppg_t, s.ppg) - sim.PPG_DC) * 0.25
        mon = BioSignalMonitor(Config(), **MODELS)
        rows, next_update = [], 1.0
        for t, e, p in zip(s.ecg_t, s.ecg, ppg):
            mon.push_ecg(t, e)
            mon.push_ppg(t, p)
            if t >= next_update:
                rows.append(mon.update(t))
                next_update += 1.0
        return rows

    def test_escalates_from_normal_to_caution_to_danger(self):
        s, rows = run("cardiac_arrest")
        self.assertEqual(next(st for st in rows if st.t >= s.onset - 1).level, "normal")
        caution = next(st.t for st in rows if st.t >= s.onset and st.level == "caution")
        danger = next(st.t for st in rows if st.t >= s.onset and st.level == "danger")
        self.assertLess(caution, danger)
        self.assertLessEqual(caution - s.onset, 8.0)
        self.assertLessEqual(danger - s.onset, 20.0)

    def test_detected_without_contact_flags(self):
        rows = self.no_flag_arrest()
        before = [st for st in rows if 15 < st.t < 60]
        self.assertEqual(before[-1].level, "normal")
        self.assertNotIn("danger", {st.level for st in before})
        self.assertEqual(rows[-1].level, "danger")

    def test_ecg_alive_without_pulse_is_caution(self):
        s, rows = run("pulseless_electrical_activity")
        self.assertEqual(rows[-1].level, "caution")
        self.assertIn("NO_PULSE_UNCONFIRMED", rows[-1].reasons)
        self.assertTrue(rows[-1].needs_second_opinion)

    def test_slow_decline_reaches_danger(self):
        s, rows = run("bradycardia_to_arrest")
        levels = [st.level for st in rows if st.t >= s.onset]
        self.assertIn("caution", levels)
        self.assertEqual(rows[-1].level, "danger")

    def test_finger_off_with_normal_ecg_is_not_danger(self):
        ecg, ppg = _ecg_ppg_adc(90)
        ppg[len(ppg) // 2:] = 966.0  # 실측에서 손가락을 뗐을 때의 값
        mon = BioSignalMonitor(Config(), **MODELS)
        rows, next_update = [], 1.0
        for i, (e, p) in enumerate(zip(ecg, ppg)):
            t = i / 250.0
            mon.push_ecg(t, e)
            mon.push_ppg(t, p)
            if t >= next_update:
                rows.append(mon.update(t))
                next_update += 1.0
        self.assertNotIn("danger", {st.level for st in rows})
        self.assertEqual(rows[-1].level, "normal")


def _ecg_ppg_adc(seconds, seed=0):
    s = sim.build(dur=seconds, seed=seed)
    return s.ecg.copy(), 500.0 + (np.interp(s.ecg_t, s.ppg_t, s.ppg) - sim.PPG_DC) * 0.25


class PpgPeaks(unittest.TestCase):
    def test_small_bumps_are_not_counted(self):
        fs = 100.0
        t = np.arange(0, 12, 1 / fs)
        beats = np.arange(0.5, 12, 0.8)
        y = sum(np.exp(-((t - b) ** 2) / (2 * 0.08 ** 2)) for b in beats)
        y = y + sum(0.28 * np.exp(-((t - b - 0.4) ** 2) / (2 * 0.05 ** 2)) for b in beats[::4])
        peaks, _, _ = detect_pulse_peaks(y, fs)
        self.assertAlmostEqual(len(peaks), len(beats), delta=1)


if __name__ == "__main__":
    unittest.main()
