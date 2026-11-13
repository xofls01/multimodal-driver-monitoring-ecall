#!/usr/bin/env python3
"""Export actual result videos and event photos as portable MP4/JPG/JSON files."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from driver_state_tools.evidence_export import export_evidence


def export_package(source,output,only=None):
    source=Path(source).resolve();output=Path(output).resolve()
    archive=output.with_suffix('.zip')
    if output==source or source in output.parents or output in source.parents:
        raise ValueError('Share folder must be separate from analysis results')
    if (output.exists() and any(output.iterdir())) or archive.exists():
        raise ValueError('Use a new share folder and ZIP name to preserve previous exports')
    clips=sorted(p for p in source.iterdir() if p.is_dir() and (p/'model_output.jsonl').is_file()
                 and (only is None or p.name in only))
    if not clips:raise ValueError('No per-frame analysis results were found')
    if only and set(only)-{p.name for p in clips}:raise ValueError('Some requested clips have no analysis results')
    output.mkdir(parents=True,exist_ok=True)
    manifest=dict(schema='driver_media_package.v1',clips=[],files={},unity_editor_executed=False)
    for folder in clips:
        result=export_evidence(folder,folder.name)
        for key,item in result['media'].items():
            if item is None:continue
            original=folder/item['path']
            name=f'{folder.name}_{"full.mp4" if key=="full_video" else original.name}'
            target=output/name;shutil.copy2(original,target)
            item['path']=name
            manifest['files'][name]=hashlib.sha256(target.read_bytes()).hexdigest()
        record=output/f'{folder.name}_output.json'
        record.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
        manifest['files'][record.name]=hashlib.sha256(record.read_bytes()).hexdigest()
        manifest['clips'].append(dict(clip_id=folder.name,output=record.name,
            full_video=result['media']['full_video']['path'],
            caution_source_seconds=(result['media']['caution_photo'] or {}).get('source_time_seconds'),
            danger_source_seconds=(result['media']['danger_photo'] or {}).get('source_time_seconds'),
            stop_confirmed=result['stop_confirmation']['confirmed'],
            clip_coverage=(result['media']['incident_video'] or {}).get('coverage')))
    (output/'README.txt').write_text(
        '업로드용 결과 파일\n\n'
        '*_full.mp4: 전체 분석 영상. HTML 없이 바로 재생하거나 업로드할 수 있습니다.\n'
        '*_caution.jpg / *_danger.jpg: 비전에서 해당 상태를 처음 관측한 실제 프레임입니다.\n'
        '해당 상태가 없으면 사진도 생성하지 않습니다.\n'
        '*_to_recording_end.mp4: 첫 경고부터 녹화 끝까지의 구간입니다. 정차 완료 영상이 아닙니다.\n'
        '*_to_stop.mp4 / *_after_stop.jpg: 현재 영상과 세션에 맞는 Unity 정차 완료 응답이 확인될 때만 생성합니다.\n'
        '*_output.json: 영상 시각, 지표, 관측 품질, 반응 입력 여부와 각 파일의 경로입니다.\n\n'
        '현재 제공 영상에는 실제 Unity 정차 응답과 버튼 반응이 없습니다. Unity Editor 실행은 검증하지 않았습니다.\n'
        '실제 고개 각도와 보정된 전체 판정 확률, 물리적인 핸들 쥠, 카메라와 센서의 하드웨어 정상 여부는 미확인 값입니다.\n'
        'null은 정상이나 0이 아니라 미확인입니다. 얼굴과 눈을 읽지 못한 시점의 지표도 null입니다.\n'
        'processed_at_utc는 분석 시각이며 과거 촬영의 실제 사건 시각이 아닙니다.\n'
        '비전 Danger, 통합 Danger, 화면용 BPM 등급은 별도 필드입니다. 사진 선택은 비전 상태를 기준으로 합니다.\n'
        'N 이외의 생체신호는 합성 자료입니다. 의료 진단이나 실제 응급상황 검증 자료가 아닙니다.\n',encoding='utf-8')
    (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_DEFLATED) as z:
        for path in sorted(output.iterdir()):z.write(path,arcname=f'{output.name}/{path.name}')
    return manifest,archive


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,default=ROOT/'outputs/multimodal_output_contract')
    parser.add_argument('--output',type=Path,default=ROOT/'outputs/share_media')
    parser.add_argument('--only',nargs='+')
    args=parser.parse_args()
    manifest,archive=export_package(args.input,args.output,args.only)
    print(json.dumps(dict(folder=str(args.output.resolve()),zip=str(archive),clips=manifest['clips']),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
