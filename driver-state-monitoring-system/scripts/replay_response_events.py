#!/usr/bin/env python3
"""Replay authoritative warning/button events against recorded visual features.

Never derives a timeout or consciousness label from an E scenario filename.
"""
import argparse
import json
import sys
from pathlib import Path
from dataclasses import fields
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from driver_state_tools.decision_engine import FeatureFrame,TemporalDecisionEngine


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--records',type=Path,required=True)
    ap.add_argument('--events',type=Path,required=True,help='JSON list of timestamp/kind warning_started, button_response, response_timeout, warning_ended')
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--rules',type=Path,default=ROOT/'config/decision_rules.json')
    args=ap.parse_args()
    if args.output.resolve() in (args.records.resolve(),args.events.resolve()):
        raise SystemExit('output must not replace inputs')
    events=json.loads(args.events.read_text())
    if not isinstance(events,list): raise SystemExit('events must be a list')
    if any(e.get('kind') not in ('warning_started','button_response','response_timeout','warning_ended') or not isinstance(e.get('timestamp'),(int,float)) or e['timestamp']<0 for e in events):
        raise SystemExit('invalid event')
    events=sorted(events,key=lambda e:e['timestamp'])
    engine=TemporalDecisionEngine(json.loads(args.rules.read_text()))
    keys={f.name for f in fields(FeatureFrame)}
    active=False; response=None; pos=0; outputs=[]
    for line in args.records.open():
        record=json.loads(line); t=record['timestamp']
        while pos<len(events) and events[pos]['timestamp']<=t:
            event=events[pos]; pos+=1
            if event['kind']=='warning_started': active=True; response=None
            elif event['kind']=='warning_ended': active=False; response=None
            elif not active: raise SystemExit('response event needs an active warning')
            elif event['kind']=='button_response': response=True
            elif event['kind']=='response_timeout': response=False
        data={k:v for k,v in record['derived'].items() if k in keys}
        data.update(timestamp=t,yawning=record['derived']['mouth_open'],warning_active=active,response_received=response)
        decision=engine.update(FeatureFrame(**data))
        outputs.append(dict(timestamp=t,state=decision.state.value,reasons=decision.reasons,
                            warning_active=active,response_received=response,note='external event replay, not a medical label'))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text('\n'.join(json.dumps(r,ensure_ascii=False) for r in outputs)+'\n')
    print(f'{len(outputs)} decisions -> {args.output}')


if __name__=='__main__': main()
