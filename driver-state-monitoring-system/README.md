# Driver State Monitoring System

YOLO와 MediaPipe로 영상의 눈, 머리, 자세와 손을 관측하고 ECG/PPG를 같은 시간에 맞춰 분석합니다. 지속 시간과 반복 조건으로 상태를 계산하며 Unity에 현재 판단을 TCP로 전달합니다.

결과는 전체 MP4, Caution/Danger 사진, 경고 구간 MP4와 JSON입니다. 정차 후 사진은 Unity 정차 응답이 확인된 경우만 저장합니다. 생체신호 화면은 BPM 구간을 표시하며 원본 생체 판정 결과는 별도 필드로 보존합니다.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-lock.txt
# 별도로 받은 driver-runtime-assets.zip의 경로를 지정합니다.
python -m zipfile -e /path/to/driver-runtime-assets.zip .
sh run_multimodal.sh
```

원본 영상, 생체신호, 모델 가중치와 생성 결과는 Git에 포함하지 않습니다. 실행에는 별도로 보관한 런타임 자료가 필요합니다. `config/multimodal/system_pairs.json`에서 입력을 지정합니다.

Unity 송신: `sh run_multimodal.sh --only E4 --unity`

업로드 파일 묶음: `python scripts/export_incident_media.py --output outputs/share_media`

[출력 형식](docs/MODEL_OUTPUT.md) / [Unity 연결](integration/UNITY_INTEGRATION.md) / [검증 범위](docs/SYSTEM_RUNTIME.md)

현재는 녹화 파일로 동작을 확인한 프로토타입입니다. 실제 고개 각도와 버튼 입력, 장비 상태 확인은 추가 연결이 필요합니다. Unity Editor의 실제 차량 동작은 검증하지 않았습니다.
