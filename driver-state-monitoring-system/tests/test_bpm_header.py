import copy
from pathlib import Path
import sys
import unittest
from PIL import Image,ImageDraw
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from driver_state_tools.multimodal_dashboard import bpm_header_display,draw_bpm_header,PALETTE


class BPMHeaderTests(unittest.TestCase):
    def bio(self,rate,level='normal'):
        return dict(level=level,hr=74.5,result_fresh=True,timeline_seconds=7.7,reasons=[],
                    display_pulse=dict(timestamp=7.7,ecg=dict(bpm=rate,status='estimated' if rate else 'no_recent_peaks'),
                                       ppg=dict(bpm=None,status='no_recent_peaks')))

    def test_header_tracks_displayed_bpm_band_instead_of_the_other_assessed_rate(self):
        for rate,state in [(74.,'normal'),(120.,'caution'),(193.,'danger')]:
            bio=self.bio(rate);before=copy.deepcopy(bio);header=bpm_header_display(bio)
            self.assertEqual(header['state'],state)
            self.assertEqual(header['color'],PALETTE[state])
            self.assertIn(f'{rate:.0f} BPM',header['caption'])
            self.assertEqual(bio,before)
            self.assertFalse(header['used_for_control'])

    def test_missing_current_bpm_cannot_show_normal(self):
        header=bpm_header_display(self.bio(None))
        self.assertEqual(header['state'],'unavailable')
        self.assertEqual(header['caption'],'No current BPM')

    def test_initial_model_calibration_does_not_block_actual_bpm_display(self):
        bio=self.bio(74.,'unavailable');bio.update(timeline_seconds=3.,reasons=['CALIBRATING'],
            startup_settings={'calibration_s':5.})
        self.assertEqual(bpm_header_display(bio)['state'],'normal')
        self.assertNotIn('MEASURING',bpm_header_display(bio)['label'])

    def test_actual_header_pixels_are_red_at_193(self):
        image=Image.new('RGB',(850,76),(9,17,26));draw=ImageDraw.Draw(image)
        draw_bpm_header(draw,self.bio(193.),clear=True)
        pixels=np.array(image)[34:60,560:760]
        self.assertGreater(np.count_nonzero(np.all(pixels==PALETTE['danger'],axis=2)),100)
        self.assertEqual(np.count_nonzero(np.all(pixels==PALETTE['normal'],axis=2)),0)


if __name__=='__main__':unittest.main()
