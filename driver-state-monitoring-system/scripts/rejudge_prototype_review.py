#!/usr/bin/env python3
"""Apply current rules to saved measured features. No new detector inference/TCP."""
import argparse
from collections import Counter
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from driver_state_tools.prototype_engine import PrototypeFrame,PrototypeDecisionEngine
from driver_state_tools.unity_bridge import vehicle_control_packet
from run_vision_baseline import create_segments
import csv


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    args=p.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    rules=json.loads((ROOT/"config/prototype/decision_rules.json").read_text())
    config=json.loads((args.run/"config_used.json").read_text())
    source_report=json.loads((args.run/"verification.json").read_text())
    summaries=[];comparisons=[]
    for summary in source_report["clips"]:
        summary=dict(summary);cid=summary["clip_id"]
        source=args.run/cid/"frame_records"/f"{cid}.jsonl"
        digest=sha256(source.read_bytes()).hexdigest()
        target=args.output/cid
        (target/"frame_records").mkdir(parents=True)
        (target/"annotated_videos").mkdir()
        shutil.copy2(args.run/cid/"annotated_videos"/f"{cid}.mp4",target/"annotated_videos"/f"{cid}.mp4")
        engine=PrototypeDecisionEngine(rules);records=[];transitions=Counter()
        for line in source.read_text().splitlines():
            row=json.loads(line);old=row["decision"]
            f=dict(row["prototype_frame"]);e=old["metadata"]["evidence_values"]
            head=e["head_drop_observed"];body=e["torso_observed"]
            positive=(head and e["relative_head_drop"]>=config["collapse_head_drop_delta"] or
                      body and abs(e["relative_torso"])>=config["collapse_lean_degrees"])
            f["collapse_posture"]=True if positive else False if head and body else None
            decision=engine.update(PrototypeFrame(**f))
            metadata={**old["metadata"],**decision.metadata}
            row["previous_decision_summary"]=dict(state=old["state"],reasons=old["reasons"])
            row["decision"]=dict(state=decision.state.value,reasons=decision.reasons,
                                 durations=decision.durations,metadata=metadata)
            row["prototype_frame"]=asdict(PrototypeFrame(**f))
            row["derived"]["eye_closed_seconds"]=decision.durations["eyes_closed"]
            row["derived"]["collapse_posture"]=f["collapse_posture"]
            transitions[(old["state"],decision.state.value)]+=1
            records.append(row)
        with (target/"frame_records"/f"{cid}.jsonl").open("x") as stream:
            for r in records:stream.write(json.dumps(r,ensure_ascii=False)+"\n")
        (target/"unity_packets").mkdir()
        with (target/"unity_packets"/f"{cid}.jsonl").open("x") as stream:
            from driver_state_tools.decision_engine import Decision,DriverState
            for i,r in enumerate(records,1):
                d=r["decision"]
                obj=Decision(r["timestamp"],DriverState(d["state"]),d["reasons"],d["durations"],d["metadata"])
                stream.write(json.dumps(vehicle_control_packet(obj,i,clip_id=cid),ensure_ascii=False)+"\n")
        (target/"event_segments").mkdir()
        segments=create_segments(records,1/summary["analysis_fps"])
        with (target/"event_segments"/f"{cid}.csv").open("w",encoding="utf-8-sig",newline="") as stream:
            writer=csv.DictWriter(stream,fieldnames=list(segments[0]));writer.writeheader();writer.writerows(segments)
        counts=Counter(r["decision"]["state"] for r in records)
        for state in ("normal","caution","danger","unavailable"):summary[state+"_frames"]=counts[state]
        summary.update(analysis_execution="saved_feature_replay",source_records_sha256=digest,
            source_current_decisions_published=summary["current_decisions_published"],current_decisions_published=0,
            processing_seconds=None)
        (target/"clip_summary.json").write_text(json.dumps(summary,indent=2)+"\n")
        assert sha256(source.read_bytes()).hexdigest()==digest
        comparisons.append(dict(clip_id=cid,state_transitions=[dict(before=a,after=b,count=n) for (a,b),n in transitions.items()]))
        summaries.append(summary)
        print(cid,dict(counts),flush=True)
    report=dict(clips=summaries,analysis_execution="saved_feature_replay",current_analysis_and_publish_verified=False,
        unity_editor_executed=False,physics_or_vehicle_stopping_verified=False,
        source_run=str(args.run.resolve()),note="Original measured features preserved. States re-evaluated, not new inference. Offline Unity packet examples only.")
    for filename,value in (("verification.json",report),("state_comparison.json",comparisons),
                            ("rules_used.json",rules),("config_used.json",config)):
        (args.output/filename).write_text(json.dumps(value,indent=2)+"\n")


if __name__=="__main__":main()
