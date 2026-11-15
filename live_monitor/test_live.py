"""실시간 모니터 시험 (Pi·카메라·모델 가중치 없이).  실행: <venv python> -B -m pytest -q live_monitor/test_live.py"""
import json
import struct
import sys
import unittest
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'driver-state-monitoring-system' / 'src'))

from live_bio import LiveBiosignalSource  # noqa: E402
from streams import StreamHandler, read_packet  # noqa: E402


class FakeStatus:
    def __init__(self, d):
        self.d = d

    def to_dict(self):
        return dict(self.d)


class FakeMonitor:
    def __init__(self):
        self.ecg, self.ppg, self.updates, self.closed = [], [], [], False

    def push_ecg(self, t, v):
        self.ecg.append(t)

    def push_ppg(self, t, v):
        self.ppg.append(t)

    def update(self, now):
        self.updates.append(now)
        return FakeStatus(dict(t=now, level='normal', reasons=[], hr=72.0, channels={}))

    def close(self):
        self.closed = True


def packet(info, payload=b''):
    info = dict(info, payload_bytes=len(payload))
    head = json.dumps(info).encode()
    return struct.pack('!I', len(head)) + head + payload


class Stream:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def read(self, n):
        out = self.data[self.pos:self.pos + n]
        self.pos += len(out)
        return out


class StreamTests(unittest.TestCase):
    def test_packets_to_callbacks_with_pi_clock(self):
        got_bio, got_frames = [], []
        h = StreamHandler(lambda *a: got_bio.append(a), lambda t, f: got_frames.append((t, f)),
                          lambda payload: np.zeros((2, 2, 3)) if payload == b'jpg' else None)
        data = (packet(dict(type='hello', origin_ns=1_000_000_000))
                + packet(dict(type='bio', index=0, ecg=500, ppg=600, ns=1_004_000_000))
                + packet(dict(type='frame', index=0, ns=1_033_000_000), b'jpg')
                + packet(dict(type='frame', index=3, ns=1_133_000_000), b'jpg')     # Pi 가 2장 버림
                + packet(dict(type='end', errors=[])))
        s = Stream(data)
        while h.handle(*read_packet(s)):
            pass
        self.assertEqual(got_bio, [(0.004, 500.0, 600.0)])
        self.assertAlmostEqual(got_frames[0][0], 0.033)
        self.assertEqual(h.dropped_frames, 2)

    def test_pi_frames_passed_as_jpeg_bytes_without_decoding(self):
        frames = []
        h = StreamHandler(lambda *a: None, lambda t, f: frames.append(f))      # decode 없음 = PiStream
        h.handle(dict(type='hello', origin_ns=0), b'')
        h.handle(dict(type='frame', index=0, ns=5), b'\xff\xd8jpeg')
        self.assertEqual(frames, [b'\xff\xd8jpeg'])
        with self.assertRaises(RuntimeError):
            h.handle(dict(type='frame', index=1, ns=6), b'')

    def test_bio_gap_and_pi_errors_stop_the_stream(self):
        h = StreamHandler(lambda *a: None, lambda *a: None, lambda p: None)
        h.handle(dict(type='hello', origin_ns=0), b'')
        h.handle(dict(type='bio', index=0, ecg=1, ppg=1, ns=1), b'')
        with self.assertRaises(RuntimeError):                       # ECG/PPG 샘플이 빠지면 멈춤
            h.handle(dict(type='bio', index=2, ecg=1, ppg=1, ns=2), b'')
        with self.assertRaisesRegex(RuntimeError, 'Serial'):
            h.handle(dict(type='end', errors=['Serial: unplugged']), b'')


class LiveBioTests(unittest.TestCase):
    def make(self, **kw):
        m = FakeMonitor()
        src = LiveBiosignalSource(m, startup_settings=dict(calibration_s=5.0),
                                  pulse_rates_factory=lambda s: type('R', (), {'at': lambda self, now: dict(
                                      timestamp=now, ecg=dict(bpm=72.0), ppg=dict(bpm=71.0))})(), **kw)
        return m, src

    def test_uses_only_received_past_samples_and_updates_at_1hz(self):
        m, src = self.make()
        for i in range(1000):                                         # 4초 분량, 250 Hz
            src.feed(i / 250, 500, 600)
        st = src.advance(2.0)
        self.assertEqual(m.ecg[-1], 2.0)                              # 2초 뒤 샘플은 아직 안 씀
        self.assertEqual(m.updates, [1.0, 2.0])
        self.assertTrue(st['result_fresh'])
        self.assertEqual(st['level'], 'normal')
        self.assertEqual(st['display_pulse']['ecg']['bpm'], 72.0)
        src.advance(3.5)
        self.assertEqual(m.updates, [1.0, 2.0, 3.0])

    def test_sensor_stops_becomes_stale(self):
        m, src = self.make()
        for i in range(500):
            src.feed(i / 250, 500, 600)
        self.assertTrue(src.advance(1.99)['result_fresh'])
        st = src.advance(4.0)                                         # 2초째 샘플 없음
        self.assertFalse(st['result_fresh'])
        self.assertEqual((st['level'], st['reasons']), ('unavailable', ['SIGNAL_STALE']))
        self.assertNotIn('display_pulse', st)

    def test_before_any_sample(self):
        _, src = self.make()
        st = src.advance(0.5)
        self.assertEqual((st['level'], st['reasons']), ('unavailable', ['COLLECTING_SAMPLES']))

    def test_backwards_time_rejected(self):
        _, src = self.make()
        src.feed(1.0, 1, 1)
        src.feed(0.5, 1, 1)                                           # 버림
        self.assertEqual(src.samples_received, 1)
        src.advance(2.0)
        with self.assertRaises(ValueError):
            src.advance(1.0)

    def test_real_pulse_rates_on_live_window(self):
        """원본 RecentPulseRates 로 72 bpm ECG·PPG 를 20초 받으면 72 근처, 창 길이는 일정."""
        from driver_state_tools.recent_pulse import RecentPulseRates
        m = FakeMonitor()
        src = LiveBiosignalSource(m, startup_settings={},
                                  pulse_rates_factory=lambda s: RecentPulseRates(s, confirmation_s=.08))
        fs, bpm, t0 = 250.0, 72.0, 7.3                               # Pi 시계가 0 이 아니어도
        rng = np.random.default_rng(0)
        for i in range(int(20 * fs)):
            t = t0 + i / fs
            phase = ((t - t0) * bpm / 60) % 1
            ecg = 500 + 300 * np.exp(-((phase - 0.1) / 0.01) ** 2) + rng.normal(0, 3)
            ppg = 500 + 40 * np.exp(-((phase - 0.4) / 0.08) ** 2) + rng.normal(0, 0.5)
            src.feed(t, ecg, ppg)
        st = src.advance(t0 + 19.9)
        pulse = st['display_pulse']
        self.assertAlmostEqual(pulse['ecg']['bpm'], bpm, delta=2)
        self.assertAlmostEqual(pulse['ppg']['bpm'], bpm, delta=3)
        self.assertLessEqual(src._recent[-1][0] - src._recent[0][0], src.window + 0.01)


class EyeNoResponseTests(unittest.TestCase):
    """실시간 전용: 눈 감김 경고가 전달된 뒤 3초 동안 눈을 안 뜨면 Danger."""

    def decision(self, t, *, state='CAUTION', closed=True, face=True, reasons=('eyes_closed',)):
        from driver_state_tools.decision_engine import Decision, DriverState
        return Decision(t, getattr(DriverState, state), list(reasons), {},
                        dict(prototype_frame=dict(calibrated=True, face_readable=face, eyes_closed=closed)))

    def run_frames(self, rule, frames):
        return [rule.apply(self.decision(t, **kw), t).state.value for t, kw in frames]

    def test_no_response_after_warning_becomes_danger(self):
        from live_rules import EyeNoResponseRule
        rule = EyeNoResponseRule(3.0)
        states = self.run_frames(rule, [(t / 4, {}) for t in range(0, 14)])      # 0~3.25초 계속 감음
        self.assertEqual(states[:12], ['caution'] * 12)                          # 경고 후 3초 전까지는 주의
        self.assertEqual(states[12:], ['danger', 'danger'])
        d = rule.apply(self.decision(3.5), 3.5)
        self.assertIn('eye_closure_no_response', d.reasons)
        self.assertEqual(d.metadata['stop_basis'], 'live_no_response_after_eye_closure_warning')

    def test_opening_eyes_resets_and_hidden_face_holds(self):
        from live_rules import EyeNoResponseRule
        rule = EyeNoResponseRule(3.0)
        self.run_frames(rule, [(0.0, {}), (2.0, {})])
        for t in (2.2, 2.7, 3.3):                                               # 1초 넘게 눈을 뜸 → 초기화
            rule.apply(self.decision(t, state='NORMAL', closed=False, reasons=()), t)
        self.assertEqual(self.run_frames(rule, [(3.5, {}), (6.4, {})]), ['caution', 'caution'])
        rule2 = EyeNoResponseRule(3.0)
        self.run_frames(rule2, [(0.0, {})])
        rule2.apply(self.decision(1.0, closed=None, face=False), 1.0)          # 얼굴 잠깐 안 보임: 유지
        self.assertEqual(rule2.apply(self.decision(3.1), 3.1).state.value, 'danger')

    def test_one_frame_open_flicker_does_not_reset(self):
        """2026-11-15 실시간 시험: 감은 지 4초쯤 한 프레임 '뜸' 인식 → 원본이 회복 확인(recovery_pending)으로 바뀜.
        한 프레임 뜸은 반응이 아니므로, 경고 후 3초가 되면 그대로 Danger."""
        from live_rules import EyeNoResponseRule
        rule = EyeNoResponseRule(3.0)
        self.run_frames(rule, [(0.0, {}), (1.0, {}), (2.0, {})])
        rule.apply(self.decision(2.2, state='CAUTION', closed=False, reasons=('recovery_pending',)), 2.2)
        state = rule.apply(self.decision(3.1, reasons=('recovery_pending',)), 3.1).state.value
        self.assertEqual(state, 'danger')

    def test_waits_until_warning_is_delivered_and_can_be_disabled(self):
        from live_rules import EyeNoResponseRule
        delivered = [False]
        rule = EyeNoResponseRule(3.0, warning_delivered=lambda: delivered[0])
        self.assertEqual(self.run_frames(rule, [(t, {}) for t in (0, 2, 4)]), ['caution'] * 3)  # 차량 서버 미연결
        delivered[0] = True
        self.assertEqual(self.run_frames(rule, [(5.0, {}), (7.9, {}), (8.0, {})]), ['caution', 'caution', 'danger'])
        off = EyeNoResponseRule(0)
        self.assertEqual(self.run_frames(off, [(t, {}) for t in range(0, 10)])[-1], 'caution')


class UiTests(unittest.TestCase):
    def test_bio_panel_shows_bpm_only(self):
        from PIL import ImageDraw
        from driver_state_tools.decision_engine import Decision, DriverState
        import live_ui
        written = []
        original = ImageDraw.ImageDraw.text

        def spy(self, xy, text, *a, **k):
            written.append(str(text))
            return original(self, xy, text, *a, **k)
        ImageDraw.ImageDraw.text = spy
        try:
            bio = dict(level='normal', reasons=[], t=10.0, hr=74.5, result_fresh=True, timeline_seconds=10.0,
                       channels=dict(ecg=dict(hr=73.0, reliable=True), ppg=dict(hr=74.5, reliable=True)),
                       display_pulse=dict(timestamp=10.0, ecg=dict(bpm=73.0, status='estimated'),
                                          ppg=dict(bpm=74.0, status='estimated')),
                       startup_settings=dict(calibration_s=5.0))
            d = Decision(10.0, DriverState.NORMAL, [])
            img = live_ui.draw_live(np.zeros((200, 300, 3), np.uint8), d, d, bio, elapsed=10.0, fps=7.0)
        finally:
            ImageDraw.ImageDraw.text = original
        self.assertEqual(img.shape[2], 3)
        self.assertIn('73', written)                                  # 원본 규칙: ECG 우선
        self.assertIn('BPM', written)
        self.assertFalse([s for s in written if 'ECG' in s or 'PPG' in s], written)


if __name__ == '__main__':
    unittest.main()
