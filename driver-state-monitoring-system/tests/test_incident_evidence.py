"""Software fixtures test exports, not medical or actual Unity vehicle behavior."""
from pathlib import Path
from copy import deepcopy
import json
import sys
import tempfile
import unittest
import cv2
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from driver_state_tools.evidence_export import confirmed_stop,plan_evidence,export_evidence
from driver_state_tools.unity_bridge import UnityStateServer
from export_incident_media import export_package


def report(state,t):
    return dict(is_current=True,source_time_seconds=t,vision=dict(state=state,current=True),
                driver_response=dict(explicit_input_connected=False,last_confirmed_response=None),
                observation_quality=dict(camera=dict(frame_read_ok=True)))


def stopped_report(t):
    r=report('danger',t)
    r['vehicle_confirmation']=dict(stop_confirmed=True,status='confirmed_stopped',
        stop_request_observed=True,receipt_fresh=True,source_time_matches=True,clip_matches=True,session_matches=True)
    return r


class IncidentEvidenceTests(unittest.TestCase):
    def test_recording_end_and_danger_are_not_stop_confirmations(self):
        rows=[report('normal',0),report('caution',1),report('danger',2),report('danger',3)]
        plan=plan_evidence(rows)
        self.assertEqual((plan['caution_frame'],plan['danger_frame']),(1,2))
        self.assertIsNone(plan['after_stop_frame'])
        self.assertEqual(plan['clip_coverage'],'caution_to_recording_end')

    def test_stale_or_camera_failure_cannot_be_selected_as_current_evidence(self):
        stale=report('danger',0);stale['is_current']=False
        rows=[stale,report('normal',1),report('caution',2)]
        self.assertIsNone(plan_evidence(rows)['danger_frame'])
        for key in ('receipt_fresh','source_time_matches','clip_matches','session_matches','stop_request_observed'):
            r=stopped_report(3);r['vehicle_confirmation'][key]=False
            self.assertFalse(confirmed_stop(r),key)
        r=stopped_report(3);r['observation_quality']['camera']['frame_read_ok']=False
        self.assertFalse(confirmed_stop(r))

    def test_first_danger_can_precede_first_caution(self):
        p=plan_evidence([report('danger',0),report('caution',1)])
        self.assertEqual(p['start_frame'],0)
        self.assertEqual(p['clip_coverage'],'danger_to_recording_end')

    def test_only_confirmed_stop_ends_incident_early(self):
        p=plan_evidence([report('normal',0),report('caution',1),stopped_report(2),report('normal',3)])
        self.assertEqual(p['end_frame'],2)
        self.assertTrue(p['stop_confirmed'])
        self.assertEqual(p['clip_coverage'],'caution_to_confirmed_unity_stop')

    def test_unmapped_wrong_session_disconnected_or_old_unity_feedback_cannot_confirm_stop(self):
        now=[1.];server=UnityStateServer(clock=lambda:now[0]);server.connected.set()
        payload=dict(clip_id='E4',session_id='current',has_source_timestamp=True,
                     source_timestamp=10.,speed_kph=0.,emergency_phase='Stopped')
        server._latest_vehicle_state=payload;server._latest_vehicle_state_received_at=1.
        def check():return server.vehicle_confirmation('E4',10.1,True,session_id='current')['stop_confirmed']
        self.assertTrue(check())
        for key,value in [('clip_id','other'),('session_id','previous-run'),('has_source_timestamp',False),
                          ('source_timestamp',11.),('source_timestamp',9.),('speed_kph',5.),
                          ('speed_kph',True),('emergency_phase','PullingOver')]:
            server._latest_vehicle_state=dict(payload,**{key:value})
            self.assertFalse(check(),key)
        server._latest_vehicle_state=payload
        self.assertFalse(server.vehicle_confirmation('E4',10.1,False,session_id='current')['stop_confirmed'])
        now[0]=2.;self.assertFalse(check())
        now[0]=1.;server.connected.clear();self.assertFalse(check())

    def make_fixture(self,folder,rows):
        folder.mkdir(parents=True)
        out=cv2.VideoWriter(str(folder/'preview.mp4'),cv2.VideoWriter_fourcc(*'mp4v'),10.,(160,96))
        self.assertTrue(out.isOpened())
        for i in range(len(rows)):out.write(np.full((96,160,3),i*20,np.uint8))
        out.release()
        (folder/'model_output.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))

    def test_real_mp4_export_matches_frame_range_and_portable_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);folder=base/'analysis/E4'
            rows=[report('normal',0),report('caution',.1),report('danger',.2),report('danger',.3)]
            self.make_fixture(folder,rows)
            result=export_evidence(folder)
            self.assertIsNone(result['media']['after_stop_photo'])
            self.assertIsNone(result['monitoring']['driver_response']['last_confirmed_response'])
            photo=cv2.imread(str(folder/result['media']['danger_photo']['path']))
            self.assertEqual(photo.shape,(96,160,3))
            self.assertAlmostEqual(float(photo.mean()),40.,delta=6.)
            self.assertEqual(result['media']['incident_video']['frames'],3)
            self.assertAlmostEqual(result['media']['incident_video']['fps'],10.)
            manifest,archive=export_package(base/'analysis',base/'share')
            self.assertTrue(archive.is_file())
            portable=json.loads((base/'share/E4_output.json').read_text())
            for item in portable['media'].values():
                if item:self.assertTrue((base/'share'/item['path']).is_file())
            self.assertFalse(manifest['clips'][0]['stop_confirmed'])
            with self.assertRaises(ValueError):export_package(base/'analysis',base/'share')

    def test_no_danger_means_no_danger_photo_and_confirmed_stop_has_real_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            self.make_fixture(base/'N',[report('normal',0),report('caution',.1)])
            result=export_evidence(base/'N')
            self.assertIsNone(result['media']['danger_photo'])
            self.assertFalse((base/'N/evidence/danger.jpg').exists())
            self.make_fixture(base/'stop_fixture',[report('caution',0),stopped_report(.1),report('normal',.2)])
            stopped=export_evidence(base/'stop_fixture')
            self.assertTrue(stopped['stop_confirmation']['confirmed'])
            self.assertEqual(stopped['media']['incident_video']['frames'],2)
            self.assertTrue((base/'stop_fixture/evidence/after_stop.jpg').is_file())

    def test_wrong_video_frame_count_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/'N';self.make_fixture(folder,[report('normal',0)])
            (folder/'model_output.jsonl').write_text(json.dumps(report('normal',0))+'\n'+json.dumps(report('normal',.1)))
            with self.assertRaises(ValueError):export_evidence(folder)


if __name__=='__main__':unittest.main()
