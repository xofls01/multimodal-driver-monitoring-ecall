#!/usr/bin/env python3
"""Replace only the rail of a saved analysis video, using its unchanged records."""
import argparse
from bisect import bisect_right
from collections import Counter
from hashlib import sha256
import html
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import cv2
import imageio_ffmpeg
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from driver_state_tools.decision_engine import Decision, DriverState
from driver_state_tools.readable_dashboard import replace_panel


def digest(path):
    h = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024*1024), b""):
            h.update(block)
    return h.hexdigest()


def select_record_index(timestamps, timestamp):
    index = bisect_right(timestamps, timestamp+1e-8)-1
    if index < 0:
        raise ValueError("No observation at or before this frame; cannot use a future result")
    return index


def render_clip(run, target, cid, config, rules):
    records_path = run/cid/"frame_records"/f"{cid}.jsonl"
    source = run/cid/"annotated_videos"/f"{cid}.mp4"
    summary = json.loads((run/cid/"clip_summary.json").read_text())
    records = [json.loads(line) for line in records_path.read_text().splitlines() if line.strip()]
    timestamps = [r["timestamp"] for r in records]
    if not records or timestamps != sorted(set(timestamps)):
        raise ValueError("Observation timestamps must be nonempty and strictly increasing")
    if any(r["decision"]["metadata"].get("mode") != "literature" for r in records):
        raise ValueError("Readable revision review expects literature-mode records")
    before = {str(p.resolve()): digest(p) for p in (records_path, source)}
    capture = cv2.VideoCapture(str(source))
    fps = capture.get(cv2.CAP_PROP_FPS)
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    expected = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    scene_width = int(summary["source_width"])
    if not capture.isOpened() or fps <= 0 or width <= scene_width:
        raise ValueError(f"Cannot read annotated video: {source}")
    if height != summary["source_height"] or abs(fps-summary["source_fps"]) > .01:
        raise ValueError("Prior video is resized or retimed; cannot preserve source geometry")
    dest = target/cid/f"{cid}.mp4"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise FileExistsError(f"Use a new output folder: {dest}")
    ffmpeg = subprocess.Popen([
        imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-nostdin",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{width}x{height}",
        "-r", str(fps), "-i", "pipe:0", "-an", "-c:v", "libx264", "-crf", "18",
        "-preset", "fast", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(dest)
    ], stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    frame_index, previous = 0, None
    samples = {round(t*fps) for t in (1, 5, 8, 13, 15)}
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            index = select_record_index(timestamps, frame_index/fps)
            if index != previous:
                record = records[index]
                decision = record["decision"]
                obj = Decision(record["timestamp"], DriverState(decision["state"]),
                               decision["reasons"], decision["durations"], decision["metadata"])
                raw, derived, rppg = (SimpleNamespace(**record[k]) for k in ("raw", "derived", "rppg"))
                # Render the rail once per actual analysis result, matching sample-and-hold.
                rendered = replace_panel(frame[:, :scene_width], raw, derived, obj,
                                         rules, rppg, width-scene_width, config)
                panel = rendered[:, scene_width:].copy()
                previous = index
            output = np.concatenate((frame[:, :scene_width], panel), axis=1)
            if not np.array_equal(output[:, :scene_width], frame[:, :scene_width]):
                raise AssertionError("Source scene changed before encoding")
            if frame_index in samples:
                cv2.imwrite(str(dest.parent/f"preview_{frame_index:04d}.png"), output)
            ffmpeg.stdin.write(output.tobytes())
            frame_index += 1
    finally:
        capture.release()
        ffmpeg.stdin.close()
    error = ffmpeg.stderr.read().decode()
    if ffmpeg.wait() != 0:
        raise RuntimeError(error)
    check = cv2.VideoCapture(str(dest))
    output_fps = check.get(cv2.CAP_PROP_FPS)
    output_shape = (int(check.get(cv2.CAP_PROP_FRAME_WIDTH)), int(check.get(cv2.CAP_PROP_FRAME_HEIGHT)))
    decoded = 0
    while check.read()[0]:
        decoded += 1
    check.release()
    if frame_index != expected or decoded != expected or output_shape != (width, height) or abs(output_fps-fps) > .001:
        raise AssertionError("Output geometry, timing or frame count changed")
    if any(digest(Path(p)) != h for p, h in before.items()):
        raise AssertionError("Saved input changed")
    result = dict(clip_id=cid, output=dest.relative_to(target).as_posix(),
                  frames=decoded, fps=output_fps, width=width, height=height,
                  source_scene_width=scene_width, records=len(records),
                  state_counts=dict(Counter(r["decision"]["state"] for r in records)),
                  inputs_sha256=before, output_sha256=digest(dest),
                  source_pixels_preserved_before_encoding=True,
                  inference_rerun=False, judgments_recomputed=False,
                  note="UI-only rendering. H.264 is lossy; no audio. Prior logs remain authoritative.")
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", type=Path, default=ROOT/"outputs/revision_actual")
    ap.add_argument("--output", type=Path, default=ROOT/"outputs/revision_readable_ui")
    ap.add_argument("--clips", nargs="+", default=["D2", "E4", "N1"])
    ap.add_argument("--config", type=Path, default=ROOT/"config/vision_baseline.json")
    ap.add_argument("--rules", type=Path, default=ROOT/"config/decision_rules.json")
    args = ap.parse_args()
    config, rules = (json.loads(p.read_text()) for p in (args.config, args.rules))
    # Refuse to annotate the saved run with different thresholds from its package.
    package_manifest = args.run.parent.parent/"package_manifest.json"
    recorded_rules = json.loads(package_manifest.read_text())["config/decision_rules.json"]
    if digest(args.rules) != recorded_rules:
        raise ValueError("Rules differ from the saved run's package; do not relabel past judgments")
    clips = [render_clip(args.run, args.output, cid, config, rules) for cid in args.clips]
    report = dict(clips=clips, rules_sha256=digest(args.rules), ui_only=True,
                  unity_editor_executed=False, no_new_model_or_calibration=True)
    (args.output/"verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n")
    labels = {"D2": "눈 감김 관찰", "E4": "상체와 손동작 관찰", "N1": "정상 운전 모사와 별도 영상 맥박 추정"}
    cards = []
    for clip in clips:
        cid = clip["clip_id"]
        cards.append(f'<article><h2>{html.escape(cid)} / {html.escape(labels.get(cid,cid))}</h2>'
                     f'<video controls preload="metadata" src="{clip["output"]}"></video>'
                     f'<a href="{clip["output"]}" download>MP4 저장</a>'
                     f'<p>{clip["width"]} x {clip["height"]} / {clip["fps"]:.3f} FPS / {clip["frames"]}프레임</p></article>')
    page = '''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>운전자 상태 / 화면 설명 개선</title><style>
    body{background:#0f171b;color:#edf4f6;font:16px/1.8 -apple-system,BlinkMacSystemFont,sans-serif;margin:0}main{max-width:1320px;padding:32px;margin:auto}h1{font-size:30px}article,.note{background:#18252c;border:1px solid #334650;border-radius:16px;padding:24px;margin:24px 0}p{color:#b6c9d0}video{display:block;width:100%;height:auto;margin:16px 0}a{color:#71cbea}strong{color:#f8cd75}</style><main>
    <h1>운전자 상태 / 알아보기 쉬운 화면</h1><p>실제 영상의 기존 관측 기록으로 오른쪽 설명 패널만 다시 만들었습니다. 판단 결과와 기준은 변경하지 않았습니다.</p>
    <div class="note"><strong>결과와 기준을 구분해서 읽을 수 있습니다.</strong><p>현재 눈 상태와 연속 눈 감김 시간 아래에 주의 기준을 표시합니다. 측정할 수 없는 시간은 0초 대신 --로 표시합니다.</p>
    <p>시선 모델은 아직 연결되지 않았고, 꾸벅임과 입 벌림은 별도 보정이 필요합니다. D2의 눈 감김 대응 검사도 통과하지 않아 눈 감김 정차 조건은 비활성입니다. 화면을 정리하면서 미준비 기능을 작동 중으로 바꾸지 않았습니다.</p>
    <p>원본 장면의 위아래를 자르지 않았습니다. 장면 위 표시 색은 얼굴: 하늘색, 눈: 초록색, 자세: 보라색, 손: 주황색입니다. 물체는 후보로 표시하며, 손이 가슴 근처에 있다는 관측과 통증 판단을 구분합니다.</p>
    <p>영상 속 판단 불가는 아직 필요한 관측이 준비되지 않았다는 뜻입니다. 실제 Unity Editor와 차량 정차 동작은 이번 화면 수정에서 실행하지 않았습니다.</p></div>
    '''+"".join(cards)+'''<p><a href="verification.json">이번 영상 생성 확인 기록</a></p></main></html>'''
    assert "·" not in page
    (args.output/"index.html").write_text(page)


if __name__ == "__main__":
    main()
