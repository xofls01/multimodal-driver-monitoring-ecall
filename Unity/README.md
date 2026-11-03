# Unity 차량 정속주행 · 운전자 경고 · 비상 갓길 정차

기존 WheelCollider 차량과 Python TCP 통신을 유지하면서 다음 기능을 제공합니다.

- Play 즉시 시작하는 기본 60km/h P 제어 정속주행
- 100m 도로 세그먼트 4개의 재활용식 직선 도로
- Python의 `Normal/Caution/Danger` 상태 및 명시적 원격 운전 명령
- 별도 스레드에서 실행되는 caution/danger 시나리오
- Caution 진입 시 확인 버튼 없는 주의 메시지와 1회 경고음
- 밝은 웜 화이트 차체와 전면 다크/후면 레드 램프 베이스
- 램프 베이스 위 전·후방 좌우 주황색 비상등 4개의 실시간 점멸 표시
- Danger 진입 시 비상 정차 상태 머신, 비상 UI, 비상등 점멸, e-Call 상태

## 처음 적용하는 순서

1. 이 폴더가 Unity 프로젝트의 `Assets/Stage1BasicVehicle` 아래에 있는지 확인합니다.
2. 스크립트 컴파일이 끝나면 `Tools > Emergency Driving Demo > Create Stage 1 Scene`을 실행합니다.
3. 생성된 `Assets/Stage1BasicVehicle/Scenes/BasicDrivingStage1.unity`를 엽니다.
4. Play를 누릅니다. TCP 연결이 없어도 차량이 자동으로 출발합니다.
5. 별도 터미널에서 아래 Python 서버를 실행해 상태 시나리오를 시험합니다.

> Scene Builder를 다시 실행하면 같은 경로의 데모 Scene을 새 구성으로 다시 저장합니다.

## 제어 우선순위

최종 WheelCollider 토크, 브레이크, 조향은 `BasicWheelVehicleController`만 적용합니다.

```text
Emergency > 명시적 Remote > Cruise > Manual
```

- `Emergency`: Danger가 한 번 감지되면 Reset 전까지 잠깁니다.
- `Remote`: Python 패킷의 `control_mode="remote"`일 때만 활성화됩니다.
- `Cruise`: 기본 ON이며 TCP 연결 여부와 무관하게 동작합니다.
- `Manual`: Cruise를 끄고 Remote가 아닌 경우 키보드 입력을 사용합니다.

`Normal`/`Caution` 수신 자체는 제어 모드를 바꾸지 않습니다. 따라서 Python이 이 상태를 20Hz로 전송해도 정속주행은 유지됩니다. `Danger`는 비상 정차를 시작합니다. `control_mode`이 없는 예전 송신기는 `remote_enabled`를 사용하므로 제어 모드는 하위 호환됩니다. 이전 `N/D/A/E` 분류 코드는 더 이상 받지 않습니다.

## 정속주행 제어식

`CruiseControlController`가 다음 P 제어 입력을 계산하고 차량 컨트롤러에 전달합니다.

```text
error = targetSpeedKph - currentSpeedKph

error > deadband   : throttle = clamp(error * throttleKp, 0, maximumThrottle)
error < -deadband  : brake    = clamp(-error * brakeKp, 0, maximumBrake)
deadband 내부      : throttle = 0, brake = 0
```

가속과 브레이크는 같은 프레임에 동시에 요청되지 않습니다. 기본값은 목표 60km/h, deadband 1.5km/h, `throttleKp=0.08`, `brakeKp=0.10`입니다.

## 도로 세그먼트 재활용

Scene Builder는 `RoadSegment_0`~`RoadSegment_3` 네 개만 만듭니다. 각 세그먼트에는 일반 도로 `X=-6~3`, 넓어진 갓길 `X=3~7`, 실선과 점선이 들어 있습니다. 정차 목표는 도로·갓길 콜라이더의 경계와 차량 차체·바퀴 폭을 읽어 양쪽 여유가 같도록 계산합니다. 기본 구성의 목표는 `X=5.0`입니다.

차량보다 충분히 뒤로 간 가장 뒤 세그먼트를 현재 가장 앞 세그먼트 다음 위치로 옮깁니다. 런타임 `Instantiate`/`Destroy`가 없고 GameObject 수가 늘지 않습니다. R로 차량을 시작점에 되돌리면 도로도 초기 위치로 복귀합니다.

## Python 실행과 명령

프로젝트 루트에서:

```bash
python3 Unity/python/vehicle_tcp_server.py
```

외부 패키지는 필요하지 않습니다.

```text
scenario danger       # Normal 10초 -> Danger 유지
scenario caution      # Normal 5초 -> Caution 8초 -> Normal
scenario stop         # 실행 중인 상태 시나리오 중단

state normal
state caution
state danger

cruise                # Unity Cruise 활성화
manual                # Cruise OFF, 키보드 제어
remote                # 명시적 Python Remote 모드
set 0.4 0 0.15
forward
left
right
center
coast
stop
hazard on             # 현재 일반 제어 모드에서 비상등 점멸
hazard off            # 비상등 끄기
status
help
quit
```

시나리오는 별도 스레드에서 상태를 바꾸므로 20Hz TCP 송수신과 콘솔 입력은 계속 동작합니다. `scenario stop`은 현재 상태를 유지한 채 타이머만 중단합니다. `state ...` 명령은 실행 중 시나리오를 중단한 뒤 상태만 변경합니다.

## 기능별 시험 절차와 예상 결과

### 정속주행과 도로

1. Python을 실행하지 않고 Unity Play를 누릅니다.
2. HUD에서 `Cruise ON / 목표 60 km/h`를 확인합니다.
3. 차량이 자동 출발해 목표 속도 부근을 유지하는지 봅니다.
4. 오래 주행해도 Hierarchy의 RoadSegment가 4개인지 확인합니다.

### Caution 경고

1. Python 서버와 Unity를 실행합니다.
2. `scenario caution`을 입력합니다.
3. 5초 후 `주의: 운전자 상태를 확인하세요.` 메시지와 경고음이 한 번 발생합니다.
4. Caution 동안 메시지는 버튼 없이 유지되고, 8초 후 Normal이 되면 자동으로 사라집니다.
5. 다시 `state caution`을 입력하면 새 진입이므로 메시지와 경고음이 다시 발생합니다.

### 비상 정차

1. `cruise`를 입력하고 `scenario danger`를 실행합니다.
2. Normal로 10초 주행 후 Danger가 20Hz 패킷에 계속 포함됩니다.
3. Caution 메시지가 닫히고 비상 UI가 나타나며 차량 전·후방 비상등이 즉시 깜빡입니다.
4. 차량이 최소 약 55m 길이의 완만한 S자 경로를 따라 감속과 조향을 동시에 진행합니다. 진입 속도가 높으면 경로도 자동으로 길어집니다.
5. 실제 횡속도를 반영해 갓길 중앙에 진입하고, 차체가 전방과 평행해진 뒤 정차합니다.
6. HUD의 단계가 아래 순서로 바뀝니다.

```text
SignalAndSlow -> MoveToShoulder -> StraightenAndBrake -> Stopped
```

`Stopped`에서는 브레이크, `hazard=true`, 비상등 점멸, `ecall=true`가 유지됩니다. 다시 시험하려면 Python에서 `state normal`을 보낸 뒤 Unity에서 R을 누릅니다.

비상 상황과 별개로 표시만 확인하려면 Python 콘솔에서 `hazard on`과 `hazard off`를 입력합니다. Cruise/Manual/Remote 어느 일반 제어 모드에서도 동작하며, Danger 비상 제어 중에는 안전상 외부 `hazard off`보다 비상 점멸이 우선합니다.

## 키보드 조작

| 키 | 기능 |
|---|---|
| `C` | Cruise ON/OFF |
| `M` | 로컬 Manual/Remote 플래그 전환 |
| `W/S` | Manual에서 가속/브레이크·후진 |
| `A/D` | Manual에서 좌우 조향 |
| `Space` | Manual에서 강한 제동 |
| `R` | 차량과 비상 상태 초기화 |

Manual 운전을 하려면 `C`로 Cruise를 끄고 Manual 모드를 사용합니다.

## Inspector 주요 값

- `CruiseControlController`: 목표 속도, deadband, throttle/brake Kp, 최대 입력
- `BasicWheelVehicleController`: 모터/브레이크 토크, 최대 속도, 조향각, Remote timeout
- `EmergencyPullOverController`: 도로·갓길·차체 콜라이더 참조, 좌우 여유 거리, 전방 주시 거리, 제동/조향 값과 입력 변화율
- `EndlessRoadController`: 세그먼트 길이, 후방 재활용 거리, 차량/세그먼트 참조
- `DriverStateWarningUI`: 경고음 볼륨, 주파수, 길이와 Canvas 참조
- `HazardLightController`: 전·후방 램프 Renderer 참조, 점멸 간격(기본 0.45초)
- `UnityTcpVehicleBridge`: 서버 Host/Port, 상태 송신률

## 데이터 전체 흐름

```text
모델/시나리오
  -> shared.set_driver_state() / publish_prediction()
  -> Python ControlCommand.driver_state
  -> 20Hz NDJSON TCP
  -> UnityTcpVehicleBridge.CurrentDriverState
  -> Caution: DriverStateWarningUI
  -> Danger: EmergencyPullOverController -> HazardLightController
  -> BasicWheelVehicleController
  -> WheelCollider
```

Caution 메시지는 상태가 유지되는 동안 표시되고 Normal로 돌아가면 자동으로 사라집니다. 경고음은 Caution으로 진입할 때만 발생하며 같은 상태의 반복 패킷에서는 다시 울리지 않습니다. 비상 정차 중에는 분류가 흔들려도 Emergency 컨트롤과 비상 UI가 우선합니다.

## 실제 모델 연결 위치

`Unity/python/vehicle_tcp_server.py`에서 모델 추론 결과를 얻은 위치에 한 줄을 연결합니다.

```python
publish_prediction(shared, predicted_label)  # predicted_label: Normal, Caution, Danger
```

또는 동일한 동작의 `shared.set_driver_state(predicted_label)`을 호출해도 됩니다. 이 함수는 운전자 상태만 변경하며 Cruise/Remote/Manual 모드는 변경하지 않습니다.

## 문제 해결

- 차량이 움직이지 않으면 WheelCollider 4개 참조, Rigidbody `Is Kinematic`, 바퀴와 노면 접촉을 확인합니다.
- 경고음이 안 들리면 Main Camera의 `AudioListener`와 Inspector의 `warningVolume`을 확인합니다.
- Python이 다른 컴퓨터라면 `PythonTcpBridge > Host`를 Python 컴퓨터 IP로 바꿉니다.
- 재질이 분홍색이면 생성된 Material의 Shader를 프로젝트 렌더 파이프라인에 맞는 Lit/Standard로 변경합니다.
