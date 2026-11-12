"""녹화 파일 예열(biosignal.offline) 검사. 합성 신호를 측정 폴더 형식으로 저장해 넣는다."""
import csv
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from biosignal import simulate
from biosignal.offline import RecordingAssessor, assess_recording, read_recording, self_warmup

FS = 250.0


def write_folder(folder, streams, dur):
    """simulate.Streams 를 REC_ 폴더와 같은 biosignals.csv 로 저장한다 (PPG 는 250 Hz 로 보간)."""
    t = np.arange(0, dur, 1 / FS) + 0.004
    ecg = np.interp(t, streams.ecg_t, streams.ecg)
    ppg = np.interp(t, streams.ppg_t, streams.ppg)
    ppg = 512 + (ppg - np.median(ppg)) / 25          # Nano 의 0~1023 범위로 옮긴다
    with open(os.path.join(folder, "biosignals.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["index", "clip_s", "ecg_raw", "ppg_raw"])
        for i, (ti, ev, pv) in enumerate(zip(t, ecg, ppg)):
            w.writerow([i, f"{ti:.6f}", int(round(ev)), int(round(pv))])


class OfflineWarmupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        # 15초 영상: 처음 9초는 정상, 9초에 심정지. 파일 끝에 사건이 있다
        cls.arrest = os.path.join(cls.tmp.name, "arrest")
        os.makedirs(cls.arrest)
        write_folder(cls.arrest, simulate.cardiac_arrest(seed=1, onset=9.0), 15.0)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_self_warmup_judges_from_first_second(self):
        st = assess_recording(self.arrest, warmup="self")
        self.assertEqual(st[0].level, "normal")
        self.assertIsNotNone(st[0].hr_baseline)
        self.assertNotIn("CALIBRATING", st[0].reasons)
        self.assertLess(st[0].t, 1.5)

    def test_event_at_end_does_not_leak_into_start(self):
        st = assess_recording(self.arrest, warmup="self")
        before = [s.level for s in st if s.t < 9.0]
        self.assertTrue(all(lv == "normal" for lv in before), before)
        after = [s.level for s in st if s.t >= 12.0]
        self.assertTrue(any(lv in ("caution", "danger") for lv in after), after)

    def test_warmup_uses_only_the_first_seconds(self):
        sig = read_recording(self.arrest)
        warm = self_warmup(sig)
        self.assertIsNotNone(warm)
        head = sig[1][sig[0] < sig[0][0] + 3.0]
        self.assertTrue(set(np.unique(warm[1])) <= set(np.unique(head)))
        self.assertGreaterEqual(warm[0][-1], 19.0)

    def test_frame_by_frame_matches_whole_file(self):
        whole = assess_recording(self.arrest, warmup="self")
        bio = RecordingAssessor(self.arrest, warmup="self", models=False)
        seen = []
        for frame in range(int(15.0 * 30)):           # 30 fps 영상의 프레임 시각
            now = frame / 30.0
            st = bio.at(now)
            if now < 0.99:
                self.assertIsNone(st)                 # 첫 판정 전
            if st is not None:
                self.assertLessEqual(st.t, now)       # 그 시각 뒤의 신호로 판정하지 않는다
                if not seen or seen[-1] is not st:
                    seen.append(st)
        bio.at(bio.end_t)
        bio.close()
        self.assertEqual([(s.t, s.level, s.reasons) for s in bio.history],
                         [(s.t, s.level, s.reasons) for s in whole])

    def test_without_warmup_measures_baseline_first(self):
        st = assess_recording(self.arrest, warmup=None)
        self.assertEqual(st[0].level, "unavailable")
        self.assertIsNone(st[0].hr_baseline)


if __name__ == "__main__":
    unittest.main()
