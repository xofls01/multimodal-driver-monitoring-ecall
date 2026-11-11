import sys
import unittest
import json
from pathlib import Path
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from driver_state_tools.vision_pipeline import VisionModels
from driver_state_tools.object_tracking import TemporalBoxTracker


class PhoneContextTest(unittest.TestCase):
    def extract(self,box,hand,keypoints=None,other_hand=None,verifier=None):
        model=object.__new__(VisionModels)
        model.config=json.loads((ROOT/'config/vision_baseline.json').read_text())
        model.phone_tracker=TemporalBoxTracker(min_confirmations=1)
        model._next_phone_at=0
        model._last_phone_mode=None
        # These tests isolate hand/chest geometry. Verification has its own
        # positive/negative image-coordinate fixtures below.
        model._verify_phone_candidates=verifier or (lambda frame,candidates:candidates)
        model._phone_predictions=lambda images,image_size,confidence: [[box] if image_size==960 else [] for _ in images]
        points=[(float(hand[0]+i%5*6),float(hand[1]+i//5*6),0) for i in range(21)]
        hands=[points]
        if other_hand is not None:
            hands.append([(float(other_hand[0]+i%5*6),float(other_hand[1]+i//5*6),0) for i in range(21)])
        return model._extract_objects(np.zeros((720,1280,3),np.uint8),{'keypoints':keypoints or {}},{'landmarks':hands},0)

    @staticmethod
    def torso():
        return {5:(800,250,.9),6:(1100,250,.9),11:(800,650,.9),12:(1100,650,.9)}

    def test_weak_phone_over_chest_hand_is_not_confirmed(self):
        boxes,mode=self.extract((900,340,1000,380,.274),(920,355),self.torso())
        self.assertEqual(boxes,[])
        self.assertIsNone(mode)

    def test_strong_real_phone_can_coexist_with_chest_hand(self):
        boxes,mode=self.extract((900,340,1000,380,.8),(920,355),self.torso())
        self.assertTrue(boxes)
        self.assertEqual(mode,'detected')

    def test_low_phone_score_remains_usable_away_from_chest(self):
        boxes,mode=self.extract((1100,525,1200,565,.15),(1120,535),self.torso())
        self.assertTrue(boxes)
        self.assertEqual(mode,'detected')

    def test_other_chest_hand_does_not_veto_phone_hand(self):
        boxes,mode=self.extract((1100,525,1200,565,.15),(920,355),self.torso(),(1120,535))
        self.assertTrue(boxes)
        self.assertEqual(mode,'detected')

    def test_wheel_bar_not_phone_even_one_hand_survives(self):
        boxes,mode=self.extract((900,525,1180,600,.8),(1050,530))
        self.assertEqual(boxes,[])
        self.assertIsNone(mode)

    def test_small_phone_with_hand_context_can_pass(self):
        boxes,mode=self.extract((900,525,1000,565,.8),(920,535))
        self.assertTrue(boxes)
        self.assertEqual(mode,'detected')

    def test_unmeasured_frames_do_not_confirm_or_break_pending(self):
        tracker=TemporalBoxTracker(min_confirmations=2)
        box=(10,10,70,40,.8)
        self.assertEqual(tracker.update([box],0).boxes,[])
        self.assertEqual(tracker.peek(.1).boxes,[])
        self.assertEqual(tracker.update([box],.2).mode,'detected')
        self.assertEqual(tracker.peek(.3).mode,'cached')
        self.assertEqual(tracker.peek(1.1).boxes,[])

    def test_context_alone_cannot_bypass_object_reverification(self):
        checked=[]
        def reject(frame,candidates):
            checked.extend(candidates)
            return []
        boxes,mode=self.extract((900,525,1000,565,.8),(920,535),verifier=reject)
        self.assertTrue(checked)
        self.assertEqual(boxes,[])
        self.assertIsNone(mode)


class PhoneVerificationTest(unittest.TestCase):
    def setUp(self):
        self.model=object.__new__(VisionModels)
        self.model.config=json.loads((ROOT/'config/prototype/vision_baseline.json').read_text())
        self.frame=np.zeros((720,1280,3),np.uint8)
        self.box=(200,200,300,240,.15)

    def test_weak_real_phone_can_pass_when_the_recheck_is_strong(self):
        # Padding .6 maps crop (60,24,160,64) to source (200,200,300,240).
        self.model._phone_predictions=lambda images,*args:[[(60,24,160,64,.9)] for _ in images]
        self.assertEqual(self.model._verify_phone_candidates(self.frame,[self.box]),[self.box])

    def test_two_weak_scores_do_not_establish_a_phone(self):
        self.model._phone_predictions=lambda images,*args:[[(60,24,160,64,.4)] for _ in images]
        self.assertEqual(self.model._verify_phone_candidates(self.frame,[self.box]),[])

    def test_different_phone_in_the_crop_cannot_validate_the_candidate(self):
        self.model._phone_predictions=lambda images,*args:[[(0,0,30,30,.99)] for _ in images]
        self.assertEqual(self.model._verify_phone_candidates(self.frame,[self.box]),[])

    def test_strong_initial_prediction_still_needs_local_image_support(self):
        self.model._phone_predictions=lambda images,*args:[[] for _ in images]
        self.assertEqual(self.model._verify_phone_candidates(self.frame,[self.box[:4]+(.95,)]),[])

    def test_invalid_crop_is_not_inferred_or_returned(self):
        self.model._phone_predictions=lambda images,*args:[]
        self.assertEqual(self.model._verify_phone_candidates(self.frame,[(1500,100,1600,150,.9)]),[])


if __name__=='__main__': unittest.main()
