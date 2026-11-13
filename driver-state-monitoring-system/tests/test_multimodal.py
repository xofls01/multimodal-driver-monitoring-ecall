import csv
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from driver_state_tools.biosignal_replay import BiosignalReplay, validate_frame_clock
from driver_state_tools.multimodal_fusion import combine
from driver_state_tools.decision_engine import Decision, DriverState
from driver_state_tools.unity_bridge import UnityStateServer, vehicle_control_packet
from driver_state_tools.analysis_publisher import AnalysisPublisher


def bio(level='normal', **extra):
    return dict(level=level, available=level != 'unavailable', synthetic=True, t=1.,
        age_seconds=0., sample_age_seconds=0., reasons=[], hr=75., channels={}, **extra)


def vision(state='normal', **features):
    return Decision(1., DriverState(state), ['test_warning'] if state != 'normal' else [],
        {'eyes_closed': 3.}, {'mode': 'prototype', 'prototype_frame': features})


class FusionPolicyTests(unittest.TestCase):
    def test_replay_uses_supplied_live_model_defaults(self):
        root = Path(__file__).resolve().parents[1]
        cfg = json.loads((root/'config/multimodal/replay_pairs.json').read_text())
        self.assertEqual(Path(cfg['models']['ecg']).name, 'hr_classifier_ft.pt')
        self.assertEqual(Path(cfg['models']['ppg_encoder']).name, 'papagei_s.pt')
        self.assertEqual(Path(cfg['models']['ppg_head']).name, 'ppg_head_ft.pt')

    def test_active_finetuned_weights_are_not_excluded_from_package(self):
        root = Path(__file__).resolve().parents[1]
        self.assertNotIn('models/biosignal/*_ft.pt', (root/'.gitignore').read_text())

    def test_normal_pulse_does_not_clear_vision_caution_or_danger(self):
        for state in ['caution', 'danger']:
            self.assertEqual(combine(vision(state), bio(), {}).state.value, state)

    def test_normal_requires_vision_but_not_finished_bio_baseline(self):
        self.assertEqual(combine(vision(), bio('unavailable'), {}).state, DriverState.NORMAL)
        self.assertEqual(combine(vision('unavailable'), bio(), {}).state, DriverState.UNAVAILABLE)

    def test_bad_signal_is_not_bio_danger(self):
        b = bio('unavailable'); b['reasons'] = ['NOISY_ECG']; b['hr_class'] = 'danger'
        self.assertEqual(combine(vision(), b, {}).state, DriverState.NORMAL)

    def test_bio_hr_class_danger_alone_is_not_final_bio_danger(self):
        self.assertEqual(combine(vision(), bio(hr_class='danger'), {}).state, DriverState.NORMAL)

    def test_bio_danger_with_normal_vision_requests_confirmation(self):
        d = combine(vision(), bio('danger'), {})
        self.assertEqual(d.state, DriverState.CAUTION)
        self.assertFalse(d.metadata['stop_requested'])

    def test_caution_alone_is_not_posture_corroboration(self):
        d = combine(vision('caution', collapse_posture=None), bio('danger'), {})
        self.assertEqual(d.state, DriverState.CAUTION)

    def test_bio_danger_and_current_observed_collapse_requests_simulator_stop(self):
        d = combine(vision('caution', collapse_posture=True), bio('danger'), {})
        self.assertEqual(d.state, DriverState.DANGER)
        self.assertTrue(d.metadata['stop_requested'])
        self.assertTrue(d.metadata['multimodal']['synthetic'])

    def test_eye_closure_corroboration_threshold(self):
        v = vision('caution', eyes_closed=True)
        v.durations['eyes_closed'] = 2.99
        self.assertEqual(combine(v, bio('danger'), {}).state, DriverState.CAUTION)
        v.durations['eyes_closed'] = 3.
        self.assertEqual(combine(v, bio('danger'), {}).state, DriverState.DANGER)

    def test_stale_vision_is_not_corroboration(self):
        d = combine(vision('caution', collapse_posture=True), bio('danger'), {}, 2.)
        self.assertEqual(d.state, DriverState.CAUTION)
        self.assertFalse(d.metadata['multimodal']['vision_available'])

    def test_future_vision_rejected(self):
        with self.assertRaises(ValueError):
            combine(vision(), bio(), {}, .5)

    def test_packet_preserves_both_states(self):
        d = combine(vision('unavailable'), bio('caution'), {})
        p = vehicle_control_packet(d, 1)
        self.assertEqual(p['driver_state'], 'Caution')
        self.assertEqual(p['fusion_state'], 'caution')
        self.assertEqual(p['vision_state'], 'unavailable')
        self.assertFalse(p['vision_available'])
        self.assertTrue(p['bio_available'])
        self.assertTrue(p['fusion_available'])

    def test_packet_expiration_invalidates_all_sources_and_stop(self):
        d = combine(vision('danger'), bio(), {})
        p = vehicle_control_packet(d, 1, age=1.)
        self.assertEqual(p['fusion_state'], 'unavailable')
        self.assertFalse(p['vision_available'])
        self.assertFalse(p['bio_available'])
        self.assertFalse(p['fusion_available'])
        self.assertFalse(p['stop_requested'])
        self.assertIsNone(p['bio_hr'])

    def test_actual_current_decision_reaches_server_publish(self):
        clock = [0.]
        server = UnityStateServer(clock=lambda: clock[0])
        pub = AnalysisPublisher(server)
        for i,state in enumerate(['normal', 'caution', 'danger']):
            d = combine(vision(state), bio(), {})
            pub.publish(d, 'trial')
            p = server.next_packet()
            self.assertEqual(p['driver_state'], state.capitalize())
            self.assertEqual(p['source_timestamp'], d.timestamp)
        self.assertEqual(pub.published_count, 3)
        clock[0] = 2
        self.assertFalse(server.next_packet()['fusion_available'])


class MonitorStub:
    def __init__(self):
        self.times = []; self.updates = []
    def push_ecg(self, t, v): self.times.append(t)
    def push_ppg(self, t, v): pass
    def update(self, t):
        self.updates.append(t)
        return SimpleNamespace(to_dict=lambda:dict(t=t, level='normal', reasons=[], hr=75., channels={'ecg':dict(reliable=True, hr=75.)}))
    def close(self): pass


class CausalReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        (self.folder / 'session.json').write_text(json.dumps({'status':'synthetic'}))
        with (self.folder / 'biosignals.csv').open('w') as f:
            f.write('clip_s,ecg_raw,ppg_raw\n')
            for t in [0,.5,1,1.5,2,2.5,3]: f.write(f'{t},500,520\n')
        self.mon = MonitorStub()
    def tearDown(self): self.temp.cleanup()
    def test_no_future_samples_or_nearest_future_status(self):
        r = BiosignalReplay(self.folder, self.mon)
        r.advance(.99)
        self.assertEqual(self.mon.times, [0,.5])
        self.assertEqual(self.mon.updates, [])
        status = r.advance(1.)
        self.assertEqual(status['t'], 1.)
        self.assertTrue((r.waveform(1.)[:,0] <= 1.).all())
    def test_eof_not_extrapolated_as_normal(self):
        r = BiosignalReplay(self.folder, self.mon)
        self.assertTrue(r.advance(3.)['available'])
        st = r.advance(3.01)
        self.assertFalse(st['available'])
        self.assertIsNone(st['hr'])
    def test_original_normal_is_preserved_even_when_bpm_is_missing(self):
        original = dict(t=1., level='normal', reasons=[], hr=None, channels={}, hr_class='danger')
        self.mon.update = lambda t: SimpleNamespace(to_dict=lambda:dict(original))
        st = BiosignalReplay(self.folder, self.mon).advance(1.)
        for k,v in original.items(): self.assertEqual(st[k], v)
        self.assertTrue(st['available'])
    def test_all_original_levels_and_reasons_are_forwarded_without_regrading(self):
        for level in ['normal', 'caution', 'danger', 'unavailable']:
            original = dict(t=1., level=level, reasons=['ORIGINAL_REASON'], hr=None, channels={})
            mon = MonitorStub()
            mon.update = lambda t,original=original: SimpleNamespace(to_dict=lambda:dict(original))
            st = BiosignalReplay(self.folder, mon).advance(1.)
            for k,v in original.items(): self.assertEqual(st[k], v)
    def test_update_cadence_matches_supplied_replay_instead_of_accumulating_sample_jitter(self):
        (self.folder/'biosignals.csv').write_text('clip_s,ecg_raw,ppg_raw\n0,500,520\n1.1,500,520\n2.01,500,520\n3.01,500,520\n')
        mon = MonitorStub()
        BiosignalReplay(self.folder, mon).advance(3.01)
        self.assertEqual(mon.updates,[1.1,2.01,3.01])
    def test_backward_seek_rejected_and_new_clip_independent(self):
        r = BiosignalReplay(self.folder, self.mon);r.advance(2.)
        with self.assertRaises(ValueError): r.advance(1.)
        self.assertFalse(BiosignalReplay(self.folder, MonitorStub()).advance(0)['available'])
    def test_explicit_offset(self):
        r = BiosignalReplay(self.folder, self.mon, offset_seconds=-1.)
        self.assertFalse(r.advance(0.)['available'])
        self.assertEqual(r.advance(2.)['timeline_seconds'], 1.)
    def test_signal_gap_is_unknown(self):
        (self.folder/'biosignals.csv').write_text('clip_s,ecg_raw,ppg_raw\n0,500,520\n1,500,520\n8,500,520\n')
        r = BiosignalReplay(self.folder, self.mon);r.advance(1.)
        self.assertFalse(r.advance(4.)['available'])
    def test_nan_and_duplicate_sample_times_rejected(self):
        for values in ['0,500,520\n0,500,520\n', '0,nan,520\n']:
            (self.folder/'biosignals.csv').write_text('clip_s,ecg_raw,ppg_raw\n'+values)
            with self.assertRaises(ValueError): BiosignalReplay(self.folder, self.mon)
    def test_frame_count_or_sync_basis_mismatch_rejected(self):
        (self.folder/'frames.csv').write_text('clip_s\n0\n0.5\n')
        validate_frame_clock(self.folder, 2, 2., 'synthetic_clip_clock')
        with self.assertRaises(ValueError): validate_frame_clock(self.folder, 3, 2., 'synthetic_clip_clock')
        with self.assertRaises(ValueError): validate_frame_clock(self.folder, 2, 2., 'unknown')


if __name__ == '__main__': unittest.main()
