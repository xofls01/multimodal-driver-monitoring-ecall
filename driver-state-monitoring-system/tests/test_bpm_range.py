from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from driver_state_tools.bpm_display import bpm_range,assessed_rate_caption
from driver_state_tools.multimodal_dashboard import draw_multimodal
from driver_state_tools.evidence_dashboard import PALETTE
from driver_state_tools.decision_engine import Decision,DriverState
import numpy as np


class BPMRangeTests(unittest.TestCase):
    def test_instantaneous_color_bands_match_received_overlay(self):
        for value,expected in [(40,'danger'),(41,'caution'),(50,'caution'),(51,'normal'),
                               (100,'normal'),(101,'caution'),(130,'caution'),(131,'danger'),(193,'danger')]:
            self.assertEqual(bpm_range(value),expected)

    def test_missing_rate_is_gray_not_green(self):
        for value in [None,float('nan'),float('inf')]:
            self.assertEqual(bpm_range(value),'unavailable')

    def test_ppg_assessed_rate_and_ecg_display_range_are_explicitly_different(self):
        bio=dict(level='normal',hr=74.5,result_fresh=True,channels={
            'ecg':dict(hr=176.5,reliable=False),'ppg':dict(hr=74.5,reliable=True)})
        self.assertEqual(assessed_rate_caption(bio),'Assessed HR: 74 (PPG)')
        self.assertEqual(bpm_range(193.),'danger')
        self.assertEqual(bio['level'],'normal')

    def test_stale_assessed_rate_is_not_shown(self):
        self.assertEqual(assessed_rate_caption(dict(hr=74.,result_fresh=False)),'Assessed HR: --')

    def test_rendered_193_uses_red_even_when_assessed_bio_grade_is_normal(self):
        bio=dict(level='normal',hr=74.5,result_fresh=True,synthetic=True,timeline_seconds=7.7,
                 channels={'ppg':dict(hr=74.5,reliable=True)},display_pulse=dict(timestamp=7.7,
                 ecg=dict(bpm=193.),ppg=dict(bpm=None)))
        v=Decision(7.7,DriverState.CAUTION,[],{},{});image=np.zeros((736,1756,3),dtype=np.uint8)
        output=draw_multimodal(image,v,v,bio,np.empty((0,3)),{})
        # Actual numeric region, excluding the separate green final-grade header.
        numeric=output[840:900,1190:1300]
        self.assertGreater(np.count_nonzero(np.all(numeric==PALETTE['danger'][::-1],axis=2)),50)
        self.assertEqual(bio['level'],'normal')


if __name__=='__main__':unittest.main()
