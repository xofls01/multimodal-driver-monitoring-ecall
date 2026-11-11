#!/usr/bin/env python3
"""Run real video inference + TCP mock receiver. Does NOT launch Unity Editor."""
import argparse
from pathlib import Path
from hashlib import sha256
import json
import socket
import threading
import time

import run_vision_baseline as runner


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input",type=Path,required=True,help="Source-video root; original files are read only")
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--calibration-profile",type=Path)
    p.add_argument("--config",type=Path)
    p.add_argument("--rules",type=Path)
    p.add_argument("--clips",nargs="+",default=["D2","E4","N1"])
    args=p.parse_args()
    root=runner.PROJECT_ROOT
    config=runner.load_json(args.config or root/"config/vision_baseline.json")
    rules=runner.load_json(args.rules or root/"config/decision_rules.json")
    if args.calibration_profile is None and config.get("calibration_profile"):
        args.calibration_profile=root/config["calibration_profile"]
    config["decision_rules"]=rules
    args.output.mkdir(parents=True,exist_ok=False)
    models=runner.VisionModels(config,root)
    with runner.UnityStateServer(port=0,log_path=args.output/"transport.jsonl") as server:
        received=[]
        done=threading.Event()
        errors=[]
        def receive():
            try:
                with socket.create_connection(("127.0.0.1",server.port),timeout=3) as client:
                    client.settimeout(3)
                    with client.makefile("rb") as stream:
                        while not done.is_set():
                            line=stream.readline()
                            if not line: break
                            received.append(json.loads(line))
            except Exception as error:
                errors.append(str(error))
        thread=threading.Thread(target=receive,daemon=True)
        thread.start()
        assert server.connected.wait(3)
        summaries=[]
        try:
            for cid in args.clips:
                family=cid[0]
                source=args.input/family/f"{cid}.mp4"
                digest=sha256(source.read_bytes()).hexdigest()
                output=args.output/cid
                output.mkdir()
                profile=runner.load_profile(args.calibration_profile,source) if args.calibration_profile else {}
                print(f"Actual inference + TCP mock: {cid}",flush=True)
                summary=runner.analyze_video(source,Path(source.name),output,config,rules,models,
                                              state_server=server,calibration_profile=profile)
                assert summary["current_decisions_published"]==summary["analyzed_frames"]
                assert sha256(source.read_bytes()).hexdigest()==digest
                summaries.append(summary)
                (output/"clip_summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2))
                print(f"{cid}: {summary['analyzed_frames']} current decisions published",flush=True)
            time.sleep(.15)
        finally:
            models.close()
            done.set()
            thread.join(4)
        assert not errors,errors
        assert not thread.is_alive()
        with (args.output/"mock_received_packets.jsonl").open("x") as stream:
            for packet in received:
                stream.write(json.dumps(packet,ensure_ascii=False)+"\n")
        report=dict(unity_editor_executed=False,physics_or_vehicle_stopping_verified=False,
                    receiver="Python TCP mock, no warning-display acknowledgement generated",clips=summaries,
                    received_packets=len(received),received_available_states=sorted({p["driver_state"] for p in received if p["vision_available"]}),
                    unavailable_packets=sum(not p["vision_available"] for p in received),
                    note="Real source-video inference and current publish() verified; no generalization or medical accuracy claim")
        (args.output/"verification.json").write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps({k:v for k,v in report.items() if k!="clips"},ensure_ascii=False,indent=2))


if __name__=="__main__": main()
