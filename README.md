# capstone_personal

캡스톤 "핸들 꽉 잡아": 운전자 생체신호·영상 기반 긴급 대응 안전 시스템(e-Call) 개인 작업 저장소.
연구용 위험 감지 프로토타입이며 의료 진단 시스템이 아닙니다.

## 폴더

| 폴더 | 내용 |
|---|---|
| `driver_monitor/` | 웹캠 운전자 모니터링: 눈 감김, PERCLOS, 고개 숙임, 하품, 핸들 파지, 카메라 심박(rPPG). `--unity`로 Unity 차량 연동(정속 주행 → 의식 저하 시 비상등 → 감속·갓길 정차 → e-Call MSD 파일 생성) |
| `HKMC_CAN_LOG/unity_stage1_basic_vehicle/` | 팀원이 만든 Unity 차량 씬 + Python TCP 서버 (원본 그대로) |
| `unity_project/` | 위 팀원 `Assets/Stage1BasicVehicle`을 넣고 씬을 생성해 둔 Unity 6000.6 프로젝트 |

`unity_project/Assets/Stage1BasicVehicle`은 팀원 원본을 복사한 것입니다. 팀원 코드가 바뀌면 다시 복사해야 합니다.

## 실행 (Windows)

```bash
cd driver_monitor
py -m pip install -r requirements.txt
py main.py --unity
```

그다음 Unity Hub에서 `unity_project`를 6000.6 버전으로 열고 `BasicDrivingStage1` 씬에서 Play를 누릅니다.
자세한 내용은 [driver_monitor/README.md](driver_monitor/README.md)를 참고하세요. 테스트는 `py -m pytest tests -q`입니다(driver_monitor 폴더에서).

## 아직 안 된 것

- 심박 센서(아두이노) 연동과 카메라+심박 융합 판정
- e-Call 실제 전송 (지금은 파일 저장만 함), Unity 화면의 경고·비상등·e-Call 표시
- 라즈베리파이 이전, 성능 측정(심박 오차, SNR 개선)
