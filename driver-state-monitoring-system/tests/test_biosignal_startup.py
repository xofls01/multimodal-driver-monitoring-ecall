from dataclasses import asdict
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from biosignal import Config, BioSignalMonitor
from biosignal.dsp import MODEL_WINDOW_S
from driver_state_tools.biosignal_startup import load_startup_profile
from driver_state_tools.multimodal_dashboard import measurement_display


class StartupProfileTests(unittest.TestCase):
    def test_fast5_changes_exactly_the_three_startup_settings(self):
        root=Path(__file__).resolve().parents[1]
        cfg,profile=load_startup_profile(root/'config/multimodal/bio_startup_fast5.json')
        original=asdict(Config());new=asdict(cfg)
        self.assertEqual({k for k in original if original[k]!=new[k]},
                         {'calibration_s','min_ready_s','calibration_min_readings'})
        self.assertEqual((cfg.calibration_s,cfg.min_ready_s,cfg.calibration_min_readings),(5.,3.,3))
        self.assertEqual(profile['neural_window_seconds'],MODEL_WINDOW_S)
        self.assertEqual(MODEL_WINDOW_S,10.)

    def test_original_profile_is_exactly_the_received_defaults(self):
        root=Path(__file__).resolve().parents[1]
        cfg,profile=load_startup_profile(root/'config/multimodal/bio_startup_original.json')
        self.assertEqual(asdict(cfg),asdict(Config()))
        self.assertTrue(profile['original_defaults'])

    def test_display_uses_actual_five_second_config_and_does_not_regrade(self):
        bio=dict(level='unavailable',reasons=['CALIBRATING'],result_fresh=True,t=4.,timeline_seconds=4.,
                 startup_settings={'calibration_s':5.})
        view=measurement_display(bio)
        self.assertEqual(view['baseline_seconds'],5.)
        self.assertIn('4.0 / 5s',view['note'])
        self.assertEqual(bio['level'],'unavailable')

    def test_five_seconds_without_valid_signal_is_not_forced_to_normal(self):
        root=Path(__file__).resolve().parents[1]
        cfg,_=load_startup_profile(root/'config/multimodal/bio_startup_fast5.json')
        monitor=BioSignalMonitor(cfg)
        for i in range(1251):
            t=i/250.
            monitor.push_ecg(t,0.,leads_off=True)
            monitor.push_ppg(t,0.,contact=False)
            if i and i%250==0:status=monitor.update(t)
        self.assertEqual(status.level,'unavailable')
        self.assertIsNone(status.hr)
        monitor.close()

    def test_other_threshold_overrides_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'bad.json';p.write_text(json.dumps({'overrides':{'hr_danger_high':100}}))
            with self.assertRaises(ValueError):load_startup_profile(p)

    def test_invalid_startup_values_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'bad.json'
            for overrides in [{'calibration_s':0},{'calibration_s':float('nan')},
                              {'min_ready_s':True},{'calibration_min_readings':2.5},
                              {'calibration_s':2.,'min_ready_s':3.}]:
                p.write_text(json.dumps({'overrides':overrides}))
                with self.assertRaises(ValueError):load_startup_profile(p)


if __name__=='__main__':unittest.main()
