"""실시간 연결부와, 실측에서 확인한 전극 탈락 파형을 검사한다."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np

from biosignal import BioSignalMonitor, Config
from biosignal import simulate as sim
from biosignal.live import LiveAssessor

MODELS = dict(model_path=os.environ.get("BIOSIGNAL_MODEL"),
              ppg_encoder_path=os.environ.get("BIOSIGNAL_PPG_ENCODER"),
              ppg_head_path=os.environ.get("BIOSIGNAL_PPG_HEAD"))


def replay_without_flags(mon, ecg, ppg, seconds, fs=250.0):
    """실제 측정처럼 접촉 정보 없이 ECG·PPG 원값만 넣는다."""
    t = np.arange(0, seconds, 1 / fs)
    rows, next_update = [], 1.0
    for i, ti in enumerate(t):
        mon.push_ecg(ti, ecg[i])
        mon.push_ppg(ti, ppg[i])
        if ti >= next_update:
            rows.append(mon.update(ti))
            next_update += 1.0
    return rows


def healthy(seconds, seed=0):
    s = sim.build(dur=seconds, seed=seed)
    ppg = np.interp(s.ecg_t, s.ppg_t, s.ppg)
    return s.ecg, ppg


class ElectrodeOffPatterns(unittest.TestCase):
    """2026-10-27 측정 기록에서 본 탈락 파형: 레일 고정, 레일 사이를 오가는 전원 잡음."""

    def check(self, broken):
        ecg, ppg = healthy(90)
        ecg = ecg.copy()
        ppg = ppg.copy()
        ecg[len(ecg) // 2:] = broken(len(ecg) - len(ecg) // 2)
        ppg[len(ppg) // 2:] = 970.0
        rows = replay_without_flags(BioSignalMonitor(Config(), **MODELS), ecg, ppg, 90)
        after = [st for st in rows if st.t > 60]
        self.assertTrue(all(st.level != "danger" for st in rows))
        self.assertEqual(after[-1].level, "unavailable")
        self.assertIn("SENSOR_LOST", after[-1].reasons)
        self.assertIsNone(after[-1].hr)

    def test_stuck_at_upper_rail(self):
        self.check(lambda n: np.full(n, 760.0))

    def test_stuck_at_zero(self):
        self.check(lambda n: np.zeros(n))

    def test_mains_hum_between_rails(self):
        self.check(lambda n: np.where(np.sin(2 * np.pi * 60 * np.arange(n) / 250.0) > 0, 760.0, 0.0))


class NormalNeverHeldWithoutSignal(unittest.TestCase):
    def test_goes_unavailable_immediately(self):
        s = sim.all_contact_lost()
        rows = list(sim.replay(BioSignalMonitor(Config(), **MODELS), s))
        lost = [st for st in rows if st.t >= s.onset + 13]
        self.assertTrue(lost)
        self.assertTrue(all(st.level != "normal" for st in lost))


class Display(unittest.TestCase):
    def test_describes_each_state(self):
        from biosignal.dashboard import COLORS, summarize
        from biosignal.monitor import BioStatus

        self.assertEqual(summarize(None).title, "연결 중")
        self.assertEqual(summarize(None, "boom").reasons, ["boom"])

        danger = summarize(BioStatus(t=1.0, level="danger", reasons=["EXTREME_BRADY"], hr=35.0, hr_baseline=72.0,
                                     hr_delta=-37.0,
                                     channels={"ecg": {"hr": 35.0, "reliable": True}, "ppg": {"hr": 36.0, "reliable": True}}))
        self.assertEqual((danger.title, danger.color, danger.reasons), ("위험", COLORS["danger"], ["VERY LOW BPM"]))
        self.assertEqual(danger.bpm, "35")
        self.assertIn("-37", danger.trend)
        self.assertIn("PPG 36", danger.sensors)

        high = summarize(BioStatus(t=1.0, level="caution", reasons=["HR_HIGH", "HR_RISING"], hr=125.0))
        self.assertEqual((high.title, high.reasons), ("주의", ["HIGH BPM", "BPM RISING"]))

        normal = summarize(BioStatus(t=1.0, level="normal", reasons=["DROWSY_HINT"], hr=70.0))
        self.assertEqual((normal.title, normal.reasons), ("정상", []))

        lost = summarize(BioStatus(t=1.0, level="unavailable", reasons=["SENSOR_LOST"],
                                   channels={"ecg": {"hr": 205.0, "reliable": False}}))
        self.assertEqual((lost.title, lost.reasons, lost.bpm), ("측정 불가", ["SENSOR OFF"], "--"))
        self.assertIn("ECG --", lost.sensors)

        calibrating = summarize(BioStatus(t=1.0, level="unavailable", reasons=["CALIBRATING"], hr=70.0))
        self.assertEqual((calibrating.title, calibrating.reasons), ("기준 심박 측정 중", []))

    def test_every_reason_code_has_screen_text(self):
        import re
        from biosignal import monitor as m
        from biosignal.dashboard import REASON_TEXT

        with open(m.__file__, encoding="utf-8") as f:
            source = f.read()
        codes = set(re.findall(r'"([A-Z]+(?:_[A-Z]+)+)"', source)) - {"DROWSY_HINT"}
        self.assertTrue(codes)
        self.assertEqual(codes - set(REASON_TEXT) - {"CALIBRATING"}, set())

    def test_right_panel_is_40_percent(self):
        import matplotlib
        matplotlib.use("Agg")
        from biosignal.dashboard import BioDashboard

        dash = BioDashboard()
        dash.fig.canvas.draw()
        panel = dash.title.axes.get_window_extent()
        self.assertAlmostEqual(panel.width / dash.fig.bbox.width, 0.4, places=2)


class Assessor(unittest.TestCase):
    def test_step_produces_status(self):
        ecg, ppg = healthy(20)
        live = LiveAssessor(**MODELS)
        self.assertIsNone(live.step())
        for i, (e, p) in enumerate(zip(ecg, ppg)):
            live.push(i / 250.0, e, p)
        status = live.step()
        self.assertIsNotNone(status)
        self.assertIs(live.latest(), status)
        self.assertEqual(status.level, "normal")
        self.assertLess(abs(status.hr - 72.0), 5.0)
        live.stop()

    def test_thread_runs_and_stops(self):
        live = LiveAssessor(period_s=0.01, **MODELS)
        live.start()
        live.push(0.0, 500.0, 500.0)
        for _ in range(200):
            if live.latest() is not None:
                break
            import time
            time.sleep(0.01)
        live.stop()
        self.assertIsNotNone(live.latest())
        self.assertIsNone(live.error)
        self.assertFalse(live._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
