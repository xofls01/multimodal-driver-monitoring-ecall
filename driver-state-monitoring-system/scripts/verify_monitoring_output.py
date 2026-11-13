#!/usr/bin/env python3
"""Validate this project's declared output schema, observed flags and current TCP delivery."""
from pathlib import Path
import argparse
from datetime import datetime
import json,math

ROOT=Path(__file__).resolve().parents[1]


def validate(value,spec,root,path='$'):
    # Only the keywords used by our schema. This is not a general JSON Schema engine.
    if '$ref' in spec:
        target=root
        for key in spec['$ref'].removeprefix('#/').split('/'):target=target[key]
        return validate(value,target,root,path)
    if 'const' in spec:assert value==spec['const'],path
    if 'enum' in spec:assert value in spec['enum'],path
    if 'type' in spec:
        kinds=spec['type'] if isinstance(spec['type'],list) else [spec['type']]
        checks={'null':value is None,'boolean':isinstance(value,bool),'string':isinstance(value,str),
                'number':isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value),
                'integer':isinstance(value,int) and not isinstance(value,bool),
                'array':isinstance(value,list),'object':isinstance(value,dict)}
        assert any(checks.get(kind,False) for kind in kinds),path
    if 'minimum' in spec:assert value>=spec['minimum'],path
    if spec.get('format')=='date-time':datetime.fromisoformat(value.replace('Z','+00:00'))
    if isinstance(value,dict):
        assert set(spec.get('required',[]))<=set(value),path
        for key,item in spec.get('properties',{}).items():
            if key in value:validate(value[key],item,root,path+'.'+key)
    if isinstance(value,list) and 'items' in spec:
        for i,item in enumerate(value):validate(item,spec['items'],root,f'{path}[{i}]')


def lines(path):return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--results',type=Path,default=ROOT/'outputs/multimodal_output_contract')
    args=ap.parse_args();schema=json.loads((ROOT/'schemas/monitoring_output.schema.json').read_text())
    summaries=json.loads((args.results/'summary.json').read_text());clips=[]
    agreement_limit=json.loads((args.results/'biosignal_config_used.json').read_text())['hr_mismatch_bpm']
    for summary in summaries:
        folder=args.results/summary['id'];reports=lines(folder/'model_output.jsonl')
        packets=lines(folder/'unity_packets.jsonl');rates=lines(folder/'display_bpm.jsonl')
        inference=lines(folder/'fusion.jsonl')
        assert len(reports)==len(packets)==len(rates)==summary['frames']
        vision_dangers=[];final_dangers=[]
        for i,(report,packet,rate) in enumerate(zip(reports,packets,rates)):
            validate(report,schema,schema)
            assert packet['monitoring']==report
            assert report['source_time_seconds']==packet['source_timestamp']==rate['timestamp']
            assert report['biosignal']['bpm']==rate['bpm']
            assert report['biosignal']['bpm_state']==rate['bpm_range']
            if str(rate['source']).startswith('recent_'):
                channel=inference[i]['bio']['channels'].get('ecg' if rate['source']=='recent_ecg_peaks' else 'ppg',{})
                hr=channel.get('hr')
                confirmed=bool(channel.get('reliable') and hr is not None and math.isfinite(hr)
                               and abs(hr-rate['bpm'])<=agreement_limit)
                assert report['biosignal']['bpm_verified_by_monitor']==confirmed
            assert report['vision']['head_lowering_angle_degrees'] is None
            assert report['vision']['head_angle_calibrated'] is False
            assert report['observation_quality']['judgment_confidence_score'] is None
            assert report['observation_quality']['face_detection_confidence'] is None
            assert report['observation_quality']['camera']['frame_read_ok'] is True
            assert report['observation_quality']['camera']['camera_hardware_ok'] is None
            assert report['observation_quality']['sensors']['hardware_ok'] is None
            assert report['driver_response']['last_confirmed_response'] is None
            assert report['driver_response']['explicit_input_connected'] is False
            if not report['vision']['eyes_observed']:
                assert report['vision']['eyes_closed_seconds'] is None
            if not report['vision']['head_lowering_observed']:
                assert report['vision']['head_lowering_seconds'] is None
            if not report['vision']['hands_observed']:
                assert report['vision']['hands_on_wheel'] is None
            if report['vision']['danger']['active'] and (i==0 or
                report['vision']['danger']['event_id']!=reports[i-1]['vision']['danger']['event_id']):
                vision_dangers.append(report['vision']['danger']['source_seconds'])
            if report['danger']['active'] and (i==0 or report['danger']['event_id']!=reports[i-1]['danger']['event_id']):
                final_dangers.append(report['danger']['source_seconds'])
        delivered=[p for p in lines(folder/'received_packets.jsonl') if p.get('pipeline')=='multimodal_analysis'
                   and p.get('clip_id')==summary['id'] and p.get('monitoring')]
        sent_times={p['source_timestamp'] for p in delivered}
        assert {r['source_time_seconds'] for r in reports}<=sent_times
        clips.append(dict(id=summary['id'],validated_frames=len(reports),all_current_reports_reached_tcp_mock=True,
                          vision_danger_source_seconds=vision_dangers,final_danger_source_seconds=final_dangers))
    output=dict(schema='driver_monitoring_output.v1',schema_and_semantics_checked=True,
                total_frames=sum(c['validated_frames'] for c in clips),clips=clips,
                no_synthetic_driver_responses_added=True,actual_head_angle_unavailable=True,
                unity_editor_executed=False)
    (args.results/'output_contract_verification.json').write_text(json.dumps(output,indent=2))
    print(json.dumps(output,indent=2))


if __name__=='__main__':main()
