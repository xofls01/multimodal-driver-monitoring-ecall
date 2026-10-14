#!/usr/bin/env python3
"""Build a local review page grouped by scenario -> condition, without moving Drive files."""
from __future__ import annotations
import argparse
import csv
import html
import json
import os
from pathlib import Path
from collections import Counter

FAMILIES = {'N':'정상 운전','D':'졸음 행동 모사','A':'전방 주시 이탈','E':'응급 대응 행동 모사'}
CONDITIONS = {'N':'기본 조건','O':'한 손 운전','H':'모자','L':'저조도','S':'선글라스'}


def read_csv(path):
    return list(csv.DictReader(path.open(encoding='utf-8-sig'))) if path.exists() else []


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--output',type=Path,default=Path('outputs/driver_monitor_20'))
    ap.add_argument('--scenario-only',action='store_true',help='Group only by four scenario families; no capture-condition comparison')
    args=ap.parse_args()
    root=args.output.resolve()
    metadata=read_csv(root/'inspection/video_metadata.csv')
    if metadata and len({r['capture_condition'] for r in metadata})==1:
        args.scenario_only=True
    audit_path=root/'run/review_audit/audit.json'
    visual_audits={r['clip_id']:r for r in json.loads(audit_path.read_text()).get('clips',[])} if audit_path.exists() else {}
    summaries={r['clip_id']:r for r in read_csv(root/'run/video_summary.csv')}
    run_path=root/'run/run_summary.json'
    run_info=json.loads(run_path.read_text()) if run_path.exists() else {}
    completed=sum(bool(s.get('annotated_video')) for s in summaries.values())
    status_note=(f'완료 {completed}/{len(metadata)}개. 드라이브 재정리 중 입력 경로가 바뀌어 실행이 중단됐습니다. 남은 파일의 현재 위치를 확인해야 합니다.'
                 if run_info.get('status')=='interrupted_source_path_changed' else '')
    # Completed clip logs can be reviewed even while the full batch is running.
    for r in metadata:
        cid=r['clip_id']; record_file=root/'run/frame_records'/f'{cid}.jsonl'
        if cid in summaries or not record_file.exists(): continue
        records=[json.loads(line) for line in record_file.open(encoding='utf-8')]
        phone_counts=Counter(x['raw'].get('phone_track_mode') for x in records)
        summaries[cid]=dict(annotated_video=f'annotated_videos/{cid}.mp4',event_segments=f'event_segments/{cid}.csv',
                           output_width=int(r['width'])+round(int(r['height'])*420/720),output_height=r['height'],
                           output_fps=r['fps'],phone_detected_frames=phone_counts['detected'],
                           phone_tracked_frames=phone_counts['tracked'],phone_cached_frames=phone_counts['cached'],analyzed_frames=len(records))
    counts=Counter((r['scenario_family'],r['capture_condition']) for r in metadata)
    grouped=[]
    audit=[]
    display_conditions={'N':'시나리오별 시험 촬영'} if args.scenario_only else CONDITIONS
    for f in FAMILIES:
        for c in display_conditions:
            found=sum(n for (family,_),n in counts.items() if family==f) if args.scenario_only else counts[f,c]
            audit.append(dict(scenario_family=f,capture_condition='not_stratified' if args.scenario_only else c,found_videos=found,expected_videos=5,
                              status='available' if found else 'no_video_found_not_evaluated'))
    with (root/'condition_coverage.csv').open('w',encoding='utf-8-sig',newline='') as out:
        writer=csv.DictWriter(out,fieldnames=audit[0].keys()); writer.writeheader(); writer.writerows(audit)
    # Grouping here is a view of provenance, never training labels or predictions.
    with (root/'scenario_condition_manifest.csv').open('w',encoding='utf-8-sig',newline='') as out:
        keys=['scenario_family','capture_condition','scenario_id','clip_id','relative_path']
        writer=csv.DictWriter(out,fieldnames=keys); writer.writeheader()
        writer.writerows({k:r[k] for k in keys} for r in sorted(metadata,key=lambda r:(r['scenario_family'],r['capture_condition'],r['scenario_id'])))
    for f,name in FAMILIES.items():
        grouped.append(f'<section><h2>{f} · {name}</h2>')
        for c,condition in display_conditions.items():
            videos=[r for r in metadata if r['scenario_family']==f and (args.scenario_only or r['capture_condition']==c)]
            if not args.scenario_only:
                grouped.append(f'<details {"open" if videos else ""}><summary>{condition} <span>{len(videos)}개</span></summary>')
            if not videos:
                grouped.append('<p class="muted">현재 가져온 자료에 영상 없음 · 이 조건의 성능은 검증하지 않았습니다.</p>')
            grouped.append('<div class="grid">')
            for r in videos:
                cid=r['clip_id']; s=summaries.get(cid,{})
                snapshots=visual_audits.get(cid,{}).get('snapshots',{})
                selected=snapshots.get('caution') or snapshots.get('phone') or snapshots.get('late')
                preview=f'run/review_audit/{selected["image"]}' if selected else f'inspection/{r["contact_sheet"]}'
                grouped.append(f'<article><h3>{html.escape(cid)}</h3><a href="{html.escape(preview)}"><img loading="lazy" src="{html.escape(preview)}" alt="판단 결과 또는 원본 대표 프레임"></a>')
                if selected:
                    grouped.append(f'<p>판단 화면 · {selected["timestamp"]:.2f}초 · 클릭하면 원본 크기<br><a href="inspection/{html.escape(r["contact_sheet"])}">원본 시간 표시 프레임 모음</a></p>')
                # MP4 link always available once processing finishes. mp4v playback depends on browser codec support.
                if s.get('annotated_video'):
                    movie=f'browser_videos/{cid}.mp4' if (root/'run/browser_videos'/f'{cid}.mp4').exists() else s['annotated_video']
                    grouped.append(f'<a class="play" href="run/{movie}">▶ 결과 영상 열기</a> <a href="run/{s["event_segments"]}">판단 구간 CSV</a>')
                    grouped.append(f' · <a href="run/frame_records/{cid}.jsonl">프레임 로그</a>')
                    grouped.append(f'<p>원본 {r["width"]}×{r["height"]} · 결과 {s["output_width"]}×{s["output_height"]} · {float(s["output_fps"]):.2f} FPS</p>')
                    grouped.append(f'<p>phone 직접 {s["phone_detected_frames"]} / 누락 추적 {s["phone_tracked_frames"]} / 관측 사이 유지 {s.get("phone_cached_frames",0)} / 분석 {s["analyzed_frames"]} 프레임<br>직접 물체 검사 약 5FPS · 눈/자세 약 15FPS</p>')
                else:
                    grouped.append('<p class="muted">아직 분석하지 못함 · 현재 입력 위치 확인 필요</p>' if s.get('error') else '<p class="muted">분석 결과 없음 또는 실행 중</p>')
                grouped.append('</article>')
            grouped.append('</div>' if args.scenario_only else '</div></details>')
        grouped.append('</section>')
    css='''body{background:#10151c;color:#e8edf3;font:16px/1.6 -apple-system,BlinkMacSystemFont,sans-serif;margin:0}main{max-width:1400px;margin:auto;padding:42px 30px}h1{font-size:36px;margin:10px 0}h2{font-size:25px}a{color:#8bdbef}section{margin:36px 0}details{border:1px solid #303947;border-radius:12px;margin:10px 0;padding:18px}summary{cursor:pointer;font-weight:600}summary span{color:#93a0b4;float:right}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:18px}article{background:#1b232e;padding:18px;border-radius:9px;margin-top:16px}img{width:100%;border-radius:6px}article p{font-size:13px;color:#a9b6c7}.muted{color:#a7b4c6}.play{display:inline-block;padding:8px 14px;background:#304354;border-radius:7px;margin:8px}.note{border-left:3px solid #d6ad59;padding:12px 20px;background:#1a2029}.legend{display:flex;gap:18px;flex-wrap:wrap}.legend span:before{content:'● ';color:var(--c)}'''
    scope_note=f'현재는 시나리오별 {len(metadata)}개 시험입니다. 촬영 조건별 비교는 이번 실험에 포함하지 않았습니다.' if args.scenario_only else '모자·저조도·선글라스 자료가 없어 해당 조건의 실제 성능을 주장할 수 없습니다.'
    content=f'''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>캡스톤 · 비전 시험 결과</title><style>{css}</style><main><p class="muted">CAPSTONE / FEASIBILITY REVIEW / v2</p><h1>운전자 비전 시험 결과</h1><p>영상 → 행동 특징 → 시간·반복 판단. 시나리오 이름은 정리용이며 판단 입력으로 쓰지 않았습니다.</p><div class="note">검사 당시 {len(metadata)}개 자료, 참가자 한 명의 연기 영상입니다. 정확도 평가가 아니라 구현 가능성 확인입니다. 라벨을 임의로 만들거나 추가 학습하지 않았습니다.<br>모자·저조도·선글라스 자료가 없어 해당 조건의 실제 성능을 주장할 수 없습니다.<br>{html.escape(status_note)}</div><p class="legend"><span style="--c:#3ccff5">얼굴</span><span style="--c:#79ef95">눈</span><span style="--c:#b69bf5">자세</span><span style="--c:#fac344">손</span><span style="--c:#f46cd6">휴대전화</span></p><p><a href="inspection/video_metadata.csv">원본 검사 CSV</a> · <a href="condition_coverage.csv">조건별 검사 당시 현황</a> · <a href="run/video_summary.csv">전체 판단 요약</a> · <a href="../../docs/vision_v2_design.md">알고리즘·UI 설명</a></p>{''.join(grouped)}<p class="muted">휴대전화 검출 비율은 정답 라벨 기준 정확도가 아닙니다. 결과 MP4가 브라우저에서 재생되지 않으면 QuickTime/VLC로 여세요. 원본 화질은 720p이며 1080p 복원이 아닙니다.</p></main></html>'''
    content=content.replace('모자·저조도·선글라스 자료가 없어 해당 조건의 실제 성능을 주장할 수 없습니다.',scope_note)
    design = Path(__file__).resolve().parents[1] / 'docs/vision_v2_design.md'
    if not design.exists():
        design = Path(__file__).resolve().parents[1] / 'README.md'
    design_link = html.escape(Path(os.path.relpath(design, root)).as_posix(), quote=True)
    content = content.replace('../../docs/vision_v2_design.md', design_link)
    if args.scenario_only:
        content=content.replace('조건별 검사 당시 현황','시나리오별 자료 현황')
        if run_info.get('success_count')==len(metadata):
            content=content.replace('<br>'+html.escape(status_note)+'</div>',f'<br>모델 실행 {len(metadata)}/{len(metadata)}개 완료.</div>')
    (root/'index.html').write_text(content,encoding='utf-8')
    print(root/'index.html')


if __name__=='__main__': main()
