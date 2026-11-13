from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from driver_state_tools.analysis_publisher import AnalysisPublisher
from driver_state_tools.decision_engine import Decision,DriverState
from driver_state_tools.multimodal_runtime import MultimodalProcessor,VisionObservation
from driver_state_tools.unity_bridge import UnityStateServer


class Vision:
    def __init__(self,states=None):self.calls=[];self.states=states or ['normal']
    def update(self,frame,t):
        self.calls.append((frame,t));state=self.states[min(len(self.calls)-1,len(self.states)-1)]
        d=Decision(t,DriverState(state),[],{},dict(mode='prototype',prototype_frame={'collapse_posture':state=='caution'}))
        return VisionObservation(frame,None,d)


class Signal:
    def __init__(self,levels=None):self.calls=[];self.levels=levels or ['normal'];self.returned=[]
    def advance(self,t):
        self.calls.append(t);level=self.levels[min(len(self.calls)-1,len(self.levels)-1)]
        b=dict(t=t,level=level,reasons=[],hr=None,channels={},available=level!='unavailable',
               result_fresh=True,age_seconds=0.,sample_age_seconds=0.,synthetic=True)
        self.returned.append(dict(b));return b


class RuntimeTests(unittest.TestCase):
    def test_raw_frames_and_signals_advance_on_the_same_clock(self):
        v=Vision();b=Signal();p=MultimodalProcessor(v,b,{},'clip',vision_every_frames=2)
        for i in range(5):p.update('raw-'+str(i),i/30)
        self.assertEqual([x[0] for x in v.calls],['raw-0','raw-2','raw-4'])
        self.assertEqual(b.calls,[i/30 for i in range(5)])
        self.assertEqual(p.vision_count,3)
        self.assertEqual(p.frame_count,5)

    def test_current_fused_decision_not_vision_alone_reaches_actual_publish(self):
        v=Vision();b=Signal(['normal','caution']);server=UnityStateServer(clock=lambda:0.)
        publisher=AnalysisPublisher(server);p=MultimodalProcessor(v,b,{},'clip',publisher=publisher)
        p.update('first',0.);result=p.update('second',.1)
        self.assertEqual(result.vision.decision.state,DriverState.NORMAL)
        self.assertEqual(result.decision.state,DriverState.CAUTION)
        packet=server.next_packet()
        self.assertEqual(packet['driver_state'],'Caution')
        self.assertEqual(packet['bio_state'],'caution')
        self.assertEqual(packet['vision_state'],'normal')
        self.assertEqual(packet['pipeline'],'multimodal_analysis')
        self.assertEqual(packet['analysis_origin'],'current_video_and_ecg_ppg_inference')
        self.assertEqual(packet['source_timestamp'],.1)
        self.assertEqual(publisher.published_count,2)

    def test_each_current_bio_grade_is_preserved_including_missing_bpm(self):
        b=Signal(['normal','caution','danger','unavailable']);p=MultimodalProcessor(Vision(),b,{},'clip')
        for i,level in enumerate(b.levels):
            result=p.update('frame',i*.1)
            self.assertEqual(result.bio['level'],level)
            self.assertIsNone(result.bio['hr'])
            self.assertEqual(result.bio,b.returned[-1])

    def test_current_collapse_and_bio_danger_can_produce_danger(self):
        p=MultimodalProcessor(Vision(['caution']),Signal(['danger']),{},'clip')
        self.assertEqual(p.update('current',0.).decision.state,DriverState.DANGER)

    def test_new_session_has_no_prior_features_or_calibration_state(self):
        first=MultimodalProcessor(Vision(),Signal(),{},'one');first.update('one',0.)
        second_v=Vision();second_b=Signal();second=MultimodalProcessor(second_v,second_b,{},'two')
        second.update('two',0.)
        self.assertEqual(second.frame_count,1)
        self.assertEqual(second_v.calls,[('two',0.)])
        self.assertEqual(second_b.calls,[0.])

    def test_invalid_or_duplicate_video_time_requires_a_new_session(self):
        p=MultimodalProcessor(Vision(),Signal(),{},'clip');p.update('frame',1.)
        for t in [0.,1.,float('nan'),float('inf')]:
            with self.assertRaises(ValueError):p.update('bad',t)


if __name__=='__main__':unittest.main()
