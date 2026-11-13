import copy
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from biosignal.dashboard import filtered_waves
from driver_state_tools.multimodal_dashboard import display_waveforms, measurement_display, channel_quality_label, brief_bio_reason


def status(**overrides):
    result = dict(level='unavailable', reasons=['CALIBRATING'], t=3., result_fresh=True,
        sample_timestamp=4., sample_age_seconds=0., timeline_seconds=4., out_of_range=False,
        channels={'ecg':dict(ready=False, sqi=0.)})
    result.update(overrides)
    return result


class MeasuringDisplayTests(unittest.TestCase):
    def test_normal_has_no_extra_explanatory_paragraph(self):
        self.assertEqual(brief_bio_reason(status(level='normal', hr_class='danger', reasons=[])), '')

    def test_alert_reason_is_short_and_preserves_original_data(self):
        s = status(level='danger', reasons=['ABNORMAL_ECG_NO_PULSE'], hr=None)
        before=copy.deepcopy(s)
        self.assertEqual(brief_bio_reason(s), 'ECG alert / PPG pulse not detected')
        self.assertEqual(s, before)

    def test_unavailable_still_explains_actual_signal_problem(self):
        self.assertEqual(brief_bio_reason(status(reasons=['LOW_SIGNAL_QUALITY'])), 'Signal unclear')

    def test_no_artificial_baseline_waiting_caption(self):
        self.assertEqual(brief_bio_reason(status()), '')

    def test_baseline_is_measuring_and_does_not_mutate_analysis(self):
        s = status();before=copy.deepcopy(s)
        view = measurement_display(s)
        self.assertEqual(view['label'], 'MEASURING...')
        self.assertIn('4.0 / 10s', view['note'])
        self.assertEqual(s, before)

    def test_first_fresh_samples_without_result_are_measuring(self):
        s = status(t=None, reasons=['SIGNAL_STALE'])
        self.assertTrue(measurement_display(s)['measuring'])

    def test_first_video_frame_is_preparing(self):
        s = status(t=None, sample_timestamp=None, timeline_seconds=0., reasons=['OUTSIDE_RECORDING'], out_of_range=True)
        self.assertTrue(measurement_display(s)['measuring'])

    def test_stale_calibration_is_not_disguised_as_measuring(self):
        self.assertEqual(measurement_display(status(result_fresh=False))['label'], 'UNAVAILABLE')

    def test_bad_signal_and_eof_are_not_disguised_as_measuring(self):
        for reason in ['LOW_SIGNAL_QUALITY', 'SENSOR_LOST', 'OUTSIDE_RECORDING']:
            self.assertEqual(measurement_display(status(reasons=[reason]))['label'], 'UNAVAILABLE')

    def test_sensor_failure_on_first_frame_is_not_disguised_as_startup(self):
        s = status(t=None, timeline_seconds=0., reasons=['SENSOR_LOST'])
        self.assertEqual(measurement_display(s)['label'], 'UNAVAILABLE')
        s = status(reasons=['CALIBRATING', 'LOW_SIGNAL_QUALITY'])
        self.assertEqual(measurement_display(s)['label'], 'UNAVAILABLE')

    def test_real_grades_are_not_overridden_by_calibration_note(self):
        for level in ['normal', 'caution', 'danger']:
            self.assertEqual(measurement_display(status(level=level))['label'], level.upper())

    def test_uncomputed_quality_is_missing_but_measured_zero_is_preserved(self):
        s = status()
        self.assertEqual(channel_quality_label(s, 'ecg'), 'Signal quality --')
        s['channels']['ecg']['ready'] = True
        self.assertEqual(channel_quality_label(s, 'ecg'), 'Signal quality 0.00')
        s['result_fresh'] = False
        self.assertEqual(channel_quality_label(s, 'ecg'), 'Signal quality --')


class FilteredDisplayTests(unittest.TestCase):
    def setUp(self):
        t = np.arange(0, 10., 1/250.)
        self.samples = np.column_stack([t, 500+50*np.sin(2*np.pi*1.25*t)+30*np.sin(2*np.pi*60*t),
                                       520+20*np.sin(2*np.pi*1.25*t)+8*np.sin(2*np.pi*30*t)])

    def test_identical_to_supplied_display_filter(self):
        past = self.samples[self.samples[:, 0] <= 8.]
        expected = np.column_stack(filtered_waves(past, 5., 3.))
        np.testing.assert_allclose(display_waveforms(self.samples, 8.), expected)

    def test_future_samples_cannot_influence_current_waveform(self):
        a = display_waveforms(self.samples, 8.)
        changed = self.samples.copy()
        changed[changed[:, 0] > 8., 1:] = 1e8
        np.testing.assert_array_equal(a, display_waveforms(changed, 8.))
        self.assertLessEqual(a[-1, 0], 8.)
        self.assertGreaterEqual(a[0, 0], 3.)

    def test_raw_samples_not_modified(self):
        before = self.samples.copy()
        display_waveforms(self.samples, 8.)
        np.testing.assert_array_equal(self.samples, before)

    def test_short_history_waits_for_filter_instead_of_drawing_raw(self):
        self.assertEqual(display_waveforms(self.samples, 1.).shape, (0, 3))
        self.assertEqual(display_waveforms(self.samples[:0], 0.).shape, (0, 3))

    def test_powerline_noise_is_attenuated(self):
        cleaned = display_waveforms(self.samples, 8.)
        # Narrowband projection avoids counting the wanted 1.25 Hz component as noise.
        t = cleaned[:, 0]
        raw = self.samples[(self.samples[:, 0] >= t[0]) & (self.samples[:, 0] <= t[-1]), 1]
        project = lambda values: abs(np.mean((values-values.mean())*np.exp(-2j*np.pi*60*t)))
        self.assertLess(project(cleaned[:, 1]), project(raw)*.1)


if __name__ == '__main__': unittest.main()
