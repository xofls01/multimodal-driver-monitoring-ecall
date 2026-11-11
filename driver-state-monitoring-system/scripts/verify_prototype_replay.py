#!/usr/bin/env python3
"""Check final engine states against saved actual-run decisions. Not new inference."""
import argparse
from pathlib import Path
import sys
import json
from hashlib import sha256

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from driver_state_tools.prototype_engine import PrototypeDecisionEngine,PrototypeFrame


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run",type=Path,required=True)
    args=p.parse_args()
    rules=json.loads((args.run/"rules_used.json").read_text())
    config=json.loads((args.run/"config_used.json").read_text())
    report=[]
    for path in sorted(args.run.glob("*/frame_records/*.jsonl")):
        engine=PrototypeDecisionEngine(rules);count=0
        for line in path.read_text().splitlines():
            row=json.loads(line);stored=row["decision"];values=dict(row["prototype_frame"])
            e=stored["metadata"]["evidence_values"]
            head=e["head_drop_observed"];body=e["torso_observed"]
            positive=(head and e["relative_head_drop"]>=config["collapse_head_drop_delta"] or
                      body and abs(e["relative_torso"])>=config["collapse_lean_degrees"])
            values["collapse_posture"]=True if positive else False if head and body else None
            current=engine.update(PrototypeFrame(**values))
            if current.state.value!=stored["state"] or current.reasons!=stored["reasons"]:
                raise AssertionError(f"Current state/reason differs: {path}:{row['timestamp']}")
            if any(abs(current.durations[k]-v)>1e-5 for k,v in stored["durations"].items()):
                raise AssertionError(f"Current duration differs: {path}:{row['timestamp']}")
            count+=1
        report.append(dict(clip_id=path.stem,matched_states_reasons_durations=count,
                           records_sha256=sha256(path.read_bytes()).hexdigest()))
    result=dict(clips=report,total_matched=sum(r["matched_states_reasons_durations"] for r in report),
                final_missing_component_mask_checked=True,new_inference=False,
                actual_unity_execution=False)
    (args.run/"final_engine_replay_verification.json").write_text(json.dumps(result,indent=2)+"\n")
    print(json.dumps(result,indent=2))


if __name__=="__main__": main()
