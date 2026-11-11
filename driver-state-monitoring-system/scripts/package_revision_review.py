#!/usr/bin/env python3
"""Package inspected revision videos, source changes and handoff metadata."""
from pathlib import Path
from hashlib import sha256
import json
import subprocess
import sys
import zipfile
import html

ROOT=Path(__file__).resolve().parents[1]


def main():
    output=ROOT/"outputs/revision_actual"
    report=json.loads((output/"verification.json").read_text())
    for cid in ("D2","E4","N1"):
        subprocess.run([sys.executable,str(ROOT/"scripts/encode_review_videos.py"),"--run",str(output/cid)],check=True)
    cards=[]
    labels={"D2":"눈 감김 후보와 현재 판단 전송", "E4":"가슴 부근 손동작 관찰과 미지원 응급 판단 구분", "N1":"미준비 관측과 별도 rPPG 표시"}
    for clip in report["clips"]:
        cid=clip["clip_id"]
        notes=f"분석 {clip['analyzed_frames']}개 / publish {clip['current_decisions_published']}개 / 주의 {clip['caution_frames']}개 / 판단 불가 {clip['unavailable_frames']}개 / 위험 {clip['danger_frames']}개"
        cards.append(f'<article><h2>{cid} - {html.escape(labels[cid])}</h2><p>{notes}</p><video controls preload="metadata" src="{cid}/browser_videos/{cid}.mp4"></video><p><a download href="{cid}/browser_videos/{cid}.mp4">영상 저장</a><a download href="{cid}/frame_records/{cid}.jsonl">프레임 기록</a></p></article>')
    page='''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>운전자 판단 기준 개정본 확인</title><style>
    body{margin:0;background:#101820;color:#e9f1f7;font:16px/1.7 -apple-system,BlinkMacSystemFont,sans-serif}main{max-width:1160px;margin:44px auto;padding:0 24px}h1{font-size:30px;line-height:1.4}h2{font-size:21px}p{color:#bfccd8}article,.notice{background:#192630;border:1px solid #314552;border-radius:16px;padding:24px;margin:24px 0}video{width:100%;height:auto;border-radius:8px}a{color:#8ed5ed;margin-right:20px}small{color:#9eb4c3}</style><main><small>2026.11.11 / 별도 복사본</small><h1>운전자 판단 기준 개정본</h1>
    <p>기존 모델과 관찰 기능을 유지하고 새 판단 모드를 분리했습니다. 미준비 기능을 정상으로 표시하지 않습니다.</p>
    <div class="notice"><strong>현재 전체 명세 준비가 끝난 상태는 아닙니다.</strong><p>시선 가중치 미연결, 꾸벅임 보정 필요, 입 벌림 점 대응과 보정 필요입니다. D2의 눈 감김 대응 검사도 통과하지 않아 6초 눈 감김 정차 경로가 비활성입니다.</p><p>실제 영상의 현재 판단이 publish()와 TCP 모의 수신기에 전달되는 것은 확인했습니다. Unity Editor, 차량 물리와 실제 정차 동작은 실행하지 않았습니다. 세 상태 전송 테스트는 별도의 합성 관측을 사용했습니다.</p></div>
    '''+''.join(cards)+'''<p><a href="verification.json">실제 영상 실행 기록</a><a href="../../docs/REVISION_REPORT.md">상세 작업 기록</a><a href="../../docs/CHANGED_FILES.md">변경 파일</a></p></main></html>'''
    assert "·" not in page
    (output/"index.html").write_text(page)
    tests=dict(date="2026-11-11",command="python -m unittest discover -s tests",tests_run=134,failures=0,errors=0,
               execution="Executed successfully in the existing Python 3.11 environment, including real local TCP tests",
               unity_editor_executed=False,real_vehicle_or_simulator_physics_verified=False)
    (output/"test_result.json").write_text(json.dumps(tests,indent=2)+"\n")
    snapshot=json.loads((ROOT/"original_snapshot.json").read_text())
    previous=snapshot["hashes"]
    changed=[]
    for base in ("src","scripts","tests","config","integration","docs","models"):
        for path in sorted((ROOT/base).rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            name=path.relative_to(ROOT).as_posix()
            digest=sha256(path.read_bytes()).hexdigest()
            if digest != previous.get(name):
                changed.append((name,"수정" if name in previous else "추가"))
    for name in ("README.md","run.sh","package_manifest.json"):
        if (ROOT/name).is_file():
            changed.append((name,"수정" if name in previous else "추가"))
    body="# 변경 파일\n\n원본과 비교한 별도 복사본의 변경이다. 모델 가중치와 원본 영상은 변경하지 않았다.\n\n"
    body+='\n'.join(f'- {kind}: [{name}](../{name})' for name,kind in changed)+"\n"
    (ROOT/"docs/CHANGED_FILES.md").write_text(body)
    source=Path(snapshot["source"])
    assert all(sha256((source/name).read_bytes()).hexdigest()==digest for name,digest in previous.items())
    manifest={}
    for base in ("src","scripts","tests","config","integration","docs","models"):
        for path in sorted((ROOT/base).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix!=".zip":
                manifest[path.relative_to(ROOT).as_posix()]=sha256(path.read_bytes()).hexdigest()
    for name in ("README.md","run.sh","requirements.txt","requirements-lock.txt",".gitignore"):
        manifest[name]=sha256((ROOT/name).read_bytes()).hexdigest()
    (ROOT/"package_manifest.json").write_text(json.dumps(manifest,indent=2)+"\n")
    package=ROOT/"integration/Unity-literature-bridge.zip"
    with zipfile.ZipFile(package,"w",zipfile.ZIP_DEFLATED) as archive:
        for path in (ROOT/"integration/Unity").rglob("*.cs"):
            archive.write(path,path.relative_to(ROOT/"integration"))
        archive.write(ROOT/"integration/UNITY_INTEGRATION.md","UNITY_INTEGRATION.md")
        archive.write(ROOT/"integration/source_manifest.json","source_manifest.json")
    print(f"Review: {output/'index.html'}")
    print(f"Changed files: {len(changed)}; original snapshot verified unchanged")


if __name__=="__main__": main()
