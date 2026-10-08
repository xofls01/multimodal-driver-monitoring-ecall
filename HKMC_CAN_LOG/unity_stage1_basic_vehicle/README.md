# Unity 차량 환경 및 Python 양방향 TCP 제어 · 1~2단계

이 묶음은 다음 환경을 자동으로 생성합니다.

- 2차선 직선 도로
- 우측 갓길
- `WheelCollider` 기반 기본 차량
- 키보드 가속·브레이크·조향
- 차량 추적 카메라
- 속도와 위치 HUD
- Python과 Unity의 양방향 TCP JSON 통신
- 연결 종료 후 Unity 자동 재접속
- Python에서 가속·브레이크·조향·비상등·e-Call 명령 전송

## 설치

1. 압축 파일을 해제합니다.
2. 안에 있는 `Assets/Stage1BasicVehicle` 폴더를 Unity 프로젝트의 `Assets` 폴더 안으로 복사합니다.
3. Unity로 돌아가 스크립트 컴파일이 끝날 때까지 기다립니다.
4. 상단 메뉴에서 `Tools > Emergency Driving Demo > Create Stage 1 Scene`을 선택합니다.
5. 자동 생성된 `BasicDrivingStage1` Scene에서 Play를 누릅니다.

> 메뉴를 실행하면 현재 Scene을 저장할 것인지 먼저 확인한 뒤 새로운 기본 Scene을 생성합니다.

## 조작법

| 키 | 기능 |
|---|---|
| `W` 또는 `↑` | 가속 |
| `S` 또는 `↓` | 브레이크, 정차 후 후진 |
| `A/D` 또는 `←/→` | 좌우 조향 |
| `Space` | 강한 제동 |
| `M` | Manual/Remote 모드 전환 |
| `R` | 시작 위치로 복귀 |

## Python TCP 제어 실행

Unity Play를 누르기 전이나 누른 후 별도 터미널에서 실행합니다.

```bash
cd unity_stage1_basic_vehicle/python
python3 vehicle_tcp_server.py
```

외부 패키지는 필요하지 않습니다. Python 표준 라이브러리만 사용합니다.

서버가 실행되면 Unity HUD의 TCP 상태가 `Connected 127.0.0.1:5001`로 변경됩니다.

다음 명령으로 Python에서 차량을 제어할 수 있습니다.

```text
forward
left
right
center
stop
coast
set 0.4 0 0.15
hazard on
ecall on
status
demo
manual
remote
```

빠른 동작 확인은 `demo`를 입력하면 됩니다.

```text
3초 가속 → 2초 우측 조향 → 비상등 작동 → 감속·정차
```

Python이 다른 컴퓨터에서 실행될 경우 Unity의 `PythonTcpBridge` 오브젝트를 선택하고 `Host`를 Python 컴퓨터의 IP 주소로 변경합니다.

## 생성되는 주요 오브젝트

```text
Environment
├─ MainRoad
├─ Shoulder
├─ LeftEdge
├─ ShoulderBoundary
├─ RightEdge
└─ LaneDash ...

PlayerVehicle
├─ Body
├─ Cabin
├─ WheelCollider_FL/FR/RL/RR
└─ WheelVisual_FL/FR/RL/RR

Main Camera
VehicleHUD
PythonTcpBridge
Directional Light
```

## 정상 동작 확인

- Play 후 W를 누르면 차량이 전진합니다.
- A/D를 누르면 앞바퀴가 회전하며 차량 방향이 변합니다.
- S 또는 Space를 누르면 차량이 감속합니다.
- 화면 왼쪽 위에 현재 속도와 X/Z 위치가 표시됩니다.
- 차량이 넘어지거나 도로 밖으로 나갔을 때 R을 누르면 시작 위치로 돌아옵니다.

## 문제가 발생할 때

### 키 입력이 동작하지 않는 경우

`Edit > Project Settings > Player > Active Input Handling`을 `Both` 또는 현재 프로젝트에서 사용하는 입력 시스템으로 설정한 뒤 Unity를 재시작합니다. 제공 스크립트는 새 Input System과 기존 Input Manager를 모두 지원합니다.

### 차량이 움직이지 않는 경우

`PlayerVehicle`을 선택하고 다음 항목을 확인합니다.

- `BasicWheelVehicleController`의 Wheel Collider 4개가 연결되어 있는지
- Rigidbody의 `Is Kinematic`이 꺼져 있는지
- Play 중 차량 바퀴가 도로 표면에 닿아 있는지

### 재질이 분홍색으로 보이는 경우

프로젝트의 렌더 파이프라인 설정이 다를 수 있습니다. 자동 생성된 Materials에서 Shader를 현재 프로젝트의 Lit 또는 Standard 계열 Shader로 변경합니다.

## 2단계 완료 기준

- Python 터미널에서 `status` 입력 시 Unity 속도와 위치가 표시됩니다.
- Python 터미널에서 `forward` 입력 시 Unity 차량이 전진합니다.
- `left`, `right`, `stop` 명령이 Unity 차량에 반영됩니다.
- Python 서버를 종료했다가 다시 실행하면 Unity가 자동으로 재접속합니다.

## 다음 단계

2단계가 정상 동작하면 목표 속도를 유지하는 PID 기반 정속 주행 제어를 구현합니다.
