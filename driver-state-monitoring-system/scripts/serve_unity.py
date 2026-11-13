#!/usr/bin/env python3
"""Serve recorded vision decisions to Unity in real time, not at inference speed."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from driver_state_tools.unity_bridge import UnityStateServer, rejudge_recording, replay_decisions


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--records", type=Path, required=True, help="One source video's frame_records JSONL")
    ap.add_argument("--rules", type=Path, default=ROOT / "config/decision_rules.json")
    ap.add_argument("--host", default="127.0.0.1", help="Use this PC's LAN address or 0.0.0.0 only for a trusted simulator network")
    ap.add_argument("--port", type=int, default=5001)
    ap.add_argument("--send-rate", type=float, default=20)
    ap.add_argument("--output", type=Path, default=None, help="New session directory; existing sessions are not overwritten")
    args = ap.parse_args()
    server = None
    try:
        records = args.records.expanduser().resolve()
        rules = json.loads(args.rules.read_text(encoding="utf-8"))
        decisions = rejudge_recording(records, rules)
        output = (args.output or ROOT / "outputs/unity_sessions" / datetime.now().strftime("session_%Y%m%d_%H%M%S_%f")).expanduser().resolve()
        if output == records or output in records.parents:
            raise ValueError("Output must not contain or replace the input recording")
        output.mkdir(parents=True, exist_ok=False)
        metadata = dict(input_records=str(records), input_sha256=hashlib.sha256(records.read_bytes()).hexdigest(),
                        mode="recorded_features_rejudged_with_current_rules_then_replayed_at_1x", rules=rules,
                        note="Unity performs simulator stopping and mock e-Call. Python sends no vehicle actuator commands.")
        (output / "session.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        with (output / "decisions.jsonl").open("x", encoding="utf-8") as stream:
            for clip_id, decision in decisions:
                stream.write(json.dumps(dict(clip_id=clip_id, timestamp=decision.timestamp, state=decision.state.value,
                                             reasons=decision.reasons, durations=decision.durations,
                                             metadata=decision.metadata), ensure_ascii=False) + "\n")
        server = UnityStateServer(args.host, args.port, args.send_rate, log_path=output / "tcp_events.jsonl").start()
        print(f"Listening {args.host}:{server.port}; Unity connects as TCP client. Output: {output}", flush=True)
        print("Waiting for Unity. Replaying precomputed decisions at 1x; this is not live camera inference.", flush=True)
        previous = [None]
        def show(decision):
            if decision.state != previous[0]:
                print(f"T+{decision.timestamp:.2f}s {decision.state.value.upper()}: {', '.join(decision.reasons)}", flush=True)
                previous[0] = decision.state
        replay_decisions(server, decisions, on_decision=show)
        print("Recording finished: vision is unavailable. Server stays on to receive stopping/e-Call telemetry. Ctrl+C to exit.", flush=True)
        while True:
            threading.Event().wait(.2)
    except KeyboardInterrupt:
        return 0
    except (ValueError, OSError, TypeError, KeyError) as error:
        print(f"Stopped: {error}", file=sys.stderr)
        return 1
    finally:
        if server is not None:
            server.stop()


if __name__ == "__main__":
    raise SystemExit(main())
