# multimodal-driver-monitoring-ecall
A multimodal driver safety system that detects emergencies using physiological signals and camera-based behavior analysis, controls the vehicle, and initiates an automated e-Call.

## 실행 방법

현재 사용 중인 **저장된 E4 가시화·모델 판단과 CAN danger 동시 재생**은 프로젝트 루트에서 다음 명령으로 시작합니다.

```bash
python3 -B Unity/python/play_can_model.py
```

기존 Python 차량 서버와 모델 실행을 종료한 뒤 위 명령을 실행하고, Unity에서 **Play**를 누릅니다. Unity의 TCP 설정은 `127.0.0.1:5001`입니다. 별도로 모델이나 `can danger`를 실행할 필요가 없습니다. 저장된 분석 결과와 Python 실행 환경이 필요합니다.

| 실행 방식 | 상세 안내 |
|---|---|
| 저장된 가시화·판단 결과와 CAN danger 동시 재생 | [동시 재생 방법](Unity/README.md#저장된-e4-분석과-can-danger-동시-재생) |
| 실제 모델을 추론하면서 가시화와 Unity 연결 | [실제 모델 연결 방법](Unity/README.md#실제-모델-연결-위치) |
| Unity 프로젝트 적용, CAN 시나리오, 경고 및 비상 정차 | [Unity 사용 설명서](Unity/README.md) |

처음 준비하는 경우 [모델 실행 환경 및 런타임 자료 안내](driver-state-monitoring-system/README.md)를 먼저 확인합니다. 가중치·입력 영상·생체신호와 생성된 분석 결과는 Git에 포함되지 않습니다.
