#!/usr/bin/env python3
"""Build a per-video EAR reference from explicitly reviewed observation intervals.

No scenario-name labels or model training. Interval annotations must come from
visual review. A successful per-video check is not generalization validation.
"""
from pathlib import Path
from hashlib import sha256
import argparse
import json
import math
import statistics


def build_profile(video, records, annotations, config):
    if annotations.get("reviewed") is not True or not annotations.get("reviewer"):
        raise ValueError("A reviewer must explicitly confirm the eye-state annotations")
    digest = sha256(video.read_bytes()).hexdigest()
    if annotations.get("source_sha256") != digest:
        raise ValueError("Reviewed annotations do not match this video")
    def selected(t, intervals):
        return any(float(start) <= t <= float(end) for start,end in intervals)
    def ear(row):
        raw = row["raw"]
        values = raw.get("ear_left"),raw.get("ear_right")
        if not raw.get("eyes_readable") or any(v is None or not math.isfinite(v) for v in values):
            return None
        return sum(values)/2
    neutral = [v for r in records if selected(r["timestamp"],annotations["neutral_open_intervals"])
               and (v:=ear(r)) is not None]
    if len(neutral) < int(config["calibration_min_samples"]):
        raise ValueError("Not enough reviewed neutral-open eye samples")
    baseline = statistics.mean(neutral)
    requested = baseline*.75
    threshold = min(config["eye_threshold_max"],max(config["eye_threshold_min"],requested))
    checks={}
    for name,closed in (("validation_open_intervals",False),("validation_closed_intervals",True)):
        samples = [ear(r) for r in records if selected(r["timestamp"],annotations.get(name,[]))]
        valid = [v for v in samples if v is not None]
        # Open check also verifies the actual reopening hysteresis boundary.
        limit = threshold if closed else threshold+config["eye_hysteresis"]
        correct = sum(v <= limit if closed else v >= limit for v in valid)
        checks[name] = dict(total_samples=len(samples), valid_samples=len(valid), correct_samples=correct,
                            all_valid_and_correct=bool(samples) and len(valid)==len(samples) and correct==len(valid))
    validation = dict(passed=all(x["all_valid_and_correct"] for x in checks.values()), checks=checks,
                      applied_threshold=threshold, eye_combine_method="mean", baseline_method="mean",
                      eye_hysteresis=config["eye_hysteresis"],
                      scope="reviewed intervals of this source video only; not clinical or independent accuracy")
    return dict(schema="reviewed_video_calibration_v1",source_sha256=digest,
                neutral_open_reviewed=True,neutral_sample_count=len(neutral),eye_baseline_mean=baseline,
                requested_threshold=requested,applied_threshold=threshold,
                clamp_changed_threshold=abs(threshold-requested)>1e-9,
                eye_validation=validation,provenance=dict(reviewer=annotations["reviewer"],
                    annotations=annotations, records_note="raw left/right EAR observations"),
                nod=None,mouth=None)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--video",required=True,type=Path)
    p.add_argument("--records",required=True,type=Path)
    p.add_argument("--annotations",required=True,type=Path)
    p.add_argument("--config",type=Path,default=Path(__file__).resolve().parents[1]/"config/vision_baseline.json")
    p.add_argument("--output",required=True,type=Path)
    args=p.parse_args()
    rows=[json.loads(x) for x in args.records.read_text().splitlines() if x.strip()]
    annotations=json.loads(args.annotations.read_text())
    if annotations.get("records_sha256") != sha256(args.records.read_bytes()).hexdigest():
        raise ValueError("Review annotations must also identify the measured-record file SHA256")
    profile=build_profile(args.video,rows,annotations,json.loads(args.config.read_text()))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open("x") as stream:
        json.dump(profile,stream,ensure_ascii=False,indent=2)
    print(f"Profile saved; eye Danger path validated for reviewed intervals: {profile['eye_validation']['passed']}")


if __name__=="__main__": main()
