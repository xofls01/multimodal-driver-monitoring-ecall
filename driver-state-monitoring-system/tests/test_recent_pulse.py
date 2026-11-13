import copy
from pathlib import Path
import sys
import unittest
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from driver_state_tools.recent_pulse import RecentPulseRates, bpm_at
from driver_state_tools.bpm_display import select_display_bpm
from driver_state_tools.decision_engine import Decision, DriverState
from driver_state_tools.multimodal_fusion import combine
from driver_state_tools.unity_bridge import vehicle_control_packet


class RecentPulseTests(unittest.TestCase):
    def test_received_recent_two_interval_formula(self):
        beats=np.array([1., 2., 2.32, 2.64])
        self.assertAlmostEqual(bpm_at(beats,2.7),187.5)
        self.assertEqual(bpm_at(beats,4.),0.)
        self.assertIsNone(bpm_at(beats,.5))

    def test_display_uses_ecg_estimate_without_regrading_or_replacing_assessed_rate(self):
        bio=dict(level='normal',hr=74.5,result_fresh=True,channels={'ecg':{'reliable':False}},
                 display_pulse=dict(timestamp=6.8,ecg=dict(bpm=188.5),ppg=dict(bpm=None)))
        before=copy.deepcopy(bio)
        rate=select_display_bpm(bio)
        self.assertEqual(rate['bpm'],188.5)
        self.assertEqual(rate['source'],'recent_ecg_peaks')
        self.assertFalse(rate['verified_by_monitor'])
        self.assertFalse(rate['used_for_grade'])
        self.assertEqual(bio,before)

    def test_stale_display_is_not_used(self):
        bio=dict(hr=None,result_fresh=False,display_pulse=dict(timestamp=1.,ecg=dict(bpm=200)))
        self.assertIsNone(select_display_bpm(bio)['bpm'])

    def test_channel_classification_reliability_does_not_confirm_a_missing_or_conflicting_rate(self):
        for hr in [None,74.5,float('nan')]:
            bio=dict(result_fresh=True,channels={'ecg':dict(reliable=True,hr=hr)},
                     display_pulse=dict(timestamp=10.5,ecg=dict(bpm=193.),ppg=dict(bpm=None)))
            self.assertFalse(select_display_bpm(bio)['verified_by_monitor'])

    def test_matching_valid_channel_rate_can_confirm_display_agreement(self):
        bio=dict(result_fresh=True,channels={'ecg':dict(reliable=True,hr=74.5)},
                 display_pulse=dict(timestamp=10.5,ecg=dict(bpm=75.),ppg=dict(bpm=None)))
        self.assertTrue(select_display_bpm(bio)['verified_by_monitor'])

    def test_no_current_peaks_cannot_keep_showing_an_old_assessed_rate(self):
        bio=dict(hr=74.5,result_fresh=True,display_pulse=dict(timestamp=8.,
                 ecg=dict(bpm=None,status='no_recent_peaks'),ppg=dict(bpm=None,status='no_recent_peaks')))
        self.assertIsNone(select_display_bpm(bio)['bpm'])

    def test_future_samples_do_not_change_display_estimate(self):
        t=np.arange(0.,10.,.004)
        ecg=500+100*np.sin(2*np.pi*1.25*t)+40*np.sin(2*np.pi*2.5*t)
        ppg=500+20*np.sin(2*np.pi*1.25*t)
        original=np.column_stack([t,ecg,ppg]);modified=original.copy()
        modified[t>5.1,1:]=np.random.default_rng(8).uniform(0,1000,size=modified[t>5.1,1:].shape)
        a=RecentPulseRates(original).at(5.1);b=RecentPulseRates(modified).at(5.1)
        self.assertEqual(a,b)
        self.assertLessEqual(a['last_sample_timestamp'],5.1+1e-9)
        self.assertFalse(a['used_for_grade'])

    def test_no_scenario_metadata_or_future_warmup_is_required(self):
        t=np.arange(0.,3.,.004);s=np.column_stack([t,500+np.sin(t*8)*50,500+np.sin(t*8)*10])
        rate=RecentPulseRates(s).at(1.9)
        self.assertLessEqual(rate['last_sample_timestamp'],1.9)
        self.assertIsNone(RecentPulseRates(s).at(0.)['ecg']['bpm'])

    def test_unity_keeps_assessed_hr_and_display_estimate_separate(self):
        v=Decision(6.8,DriverState.NORMAL,[],{},dict(prototype_frame={}))
        bio=dict(level='normal',hr=74.5,result_fresh=True,available=True,t=6.,
                 channels={'ecg':{'reliable':False}},display_pulse=dict(timestamp=6.8,
                 ecg=dict(bpm=188.5),ppg=dict(bpm=None)))
        combined=combine(v,bio,{},6.8)
        packet=vehicle_control_packet(combined,1)
        self.assertEqual(packet['driver_state'],'Normal')
        self.assertEqual(packet['bio_hr'],74.5)
        self.assertEqual(packet['bio_display_hr'],188.5)
        self.assertFalse(packet['bio_display_hr_used_for_grade'])
        self.assertFalse(packet['bio_display_hr_verified'])
        stale=vehicle_control_packet(combined,2,age=2.)
        self.assertIsNone(stale['bio_display_hr'])


if __name__=='__main__':unittest.main()
