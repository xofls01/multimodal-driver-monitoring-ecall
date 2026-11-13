import copy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from biosignal.monitor import BioStatus
from biosignal.dashboard import summarize
from driver_state_tools.bpm_display import RecordedVitals, select_display_bpm


class BPMDisplayTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'vitals.csv'
        self.path.write_text('clip_s,hr_bpm,hr_src\n0.57,68.5,4\n1.57,73,1\n2.57,0,0\n')
        self.vitals=RecordedVitals(self.path)
    def tearDown(self):self.temp.cleanup()

    def test_calibrating_bpm_matches_original_dashboard(self):
        original=BioStatus(t=6.,level='unavailable',reasons=['CALIBRATING'],hr=73.6)
        bio=dict(original.to_dict(),result_fresh=True,available=False)
        rate=select_display_bpm(bio)
        self.assertEqual(f"{rate['bpm']:.0f}",summarize(original).bpm)
        self.assertEqual(rate['source'],'analysis')

    def test_original_heart_rate_does_not_depend_on_final_grade(self):
        for level in ['normal','caution','danger','unavailable']:
            self.assertEqual(select_display_bpm(dict(hr=75.,level=level,result_fresh=True))['bpm'],75.)

    def test_first_recorded_value_is_not_used_before_its_timestamp(self):
        self.assertIsNone(select_display_bpm({'hr':None},self.vitals.at(.56))['bpm'])
        self.assertEqual(select_display_bpm({'hr':None},self.vitals.at(.57))['bpm'],68.5)

    def test_original_analyzed_rate_has_priority_when_ready(self):
        rate=select_display_bpm(dict(hr=74.,result_fresh=True),self.vitals.at(1.6))
        self.assertEqual(rate['bpm'],74.)
        self.assertEqual(rate['source'],'analysis')

    def test_latest_invalid_record_clears_old_rate_instead_of_displaying_zero(self):
        self.assertEqual(self.vitals.at(2.56)['bpm'],73.)
        rate=select_display_bpm({'hr':None},self.vitals.at(2.57))
        self.assertIsNone(rate['bpm'])

    def test_expired_recorded_or_analyzed_values_are_not_displayed(self):
        self.assertFalse(self.vitals.at(5.)['available'])
        self.assertIsNone(select_display_bpm(dict(hr=74.,result_fresh=False),self.vitals.at(5.))['bpm'])

    def test_recorded_bpm_does_not_change_algorithm_grade_or_fields(self):
        bio=dict(hr=None,level='unavailable',reasons=['CALIBRATING'],available=False)
        before=copy.deepcopy(bio)
        self.assertEqual(select_display_bpm(bio,self.vitals.at(1.))['bpm'],68.5)
        self.assertEqual(bio,before)

    def test_optional_recording_file_is_not_required_for_analyzed_rate(self):
        missing=RecordedVitals(Path(self.temp.name)/'missing.csv')
        self.assertEqual(select_display_bpm(dict(hr=75.,result_fresh=True),missing.at(1.))['bpm'],75.)

    def test_bad_record_does_not_resurrect_previous_value(self):
        self.path.write_text('clip_s,hr_bpm,hr_src\n0,75,1\n1,nan,1\n')
        self.assertIsNone(RecordedVitals(self.path).at(1.)['bpm'])

    def test_nonmonotonic_recording_times_are_rejected(self):
        self.path.write_text('clip_s,hr_bpm,hr_src\n1,75,1\n1,76,1\n')
        with self.assertRaises(ValueError):RecordedVitals(self.path)


if __name__=='__main__':unittest.main()
