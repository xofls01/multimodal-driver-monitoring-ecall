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
- 5Hz 가상 CAN CSV 재생과 차량 조작 이상 감시 (운전자 상태 분류와는 분리)

## 처음 적용하는 순서

1. 저장소의 `Unity/Assets/Stage1BasicVehicle` 소스가 실제 Unity 프로젝트의 `Assets/Stage1BasicVehicle`에 반영됐는지 확인합니다. 저장소의 `Unity` 폴더 자체는 Unity 프로젝트가 아닙니다. 현재 시연용 에디터는 별도의 `/Users/jihoo/My project`를 열고 있으므로 두 위치의 파일이 자동으로 동기화되지는 않습니다.
2. 스크립트 컴파일이 끝나면 `Tools > Emergency Driving Demo > Create Stage 1 Scene`을 실행합니다.
3. 생성된 `Assets/Stage1BasicVehicle/Scenes/BasicDrivingStage1.unity`를 엽니다.
4. Play를 누릅니다. TCP 연결이 없어도 차량이 자동으로 출발합니다.
5. 별도 터미널에서 아래 Python 서버를 실행해 상태 시나리오를 시험합니다.

> Scene Builder를 다시 실행하면 같은 경로의 데모 Scene을 새 구성으로 다시 저장합니다.
> 이미 수정 중인 Scene은 `Tools > Emergency Driving Demo > Add Grass To Current Scene`을 실행하고 저장하면 기존 오브젝트를 유지한 채 잔디 지면만 추가할 수 있습니다.

## 제어 우선순위

최종 WheelCollider 토크, 브레이크, 조향은 `BasicWheelVehicleController`만 적용합니다.

```text
Emergency > CAN 재생(Remote) > 일반 Remote > Cruise > Manual
```

- `Emergency`: Danger가 한 번 감지되면 Reset 전까지 잠깁니다.
- `Remote`: Python 패킷의 `control_mode="remote"`일 때만 활성화됩니다.
- `CAN 재생`: 기존 Remote 패킷을 이용하며, 재생 중 일반 제어 명령은 받지 않습니다. 최종 Danger 이후 CAN 입력은 Python에서 잠기고 Unity 비상제어가 Remote 입력을 무시합니다.
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

Scene Builder는 `RoadSegment_0`~`RoadSegment_3` 네 개만 만듭니다. 각 세그먼트에는 일반 도로 `X=-6~3`, 갓길 `X=3~7`, 양쪽 100m 폭의 잔디 지면, 실선과 점선이 들어 있습니다. 잔디에도 콜라이더가 있어 도로를 벗어난 차량이 곧바로 추락하지 않으며, 도로 세그먼트와 함께 계속 이어집니다. 정차 목표는 도로·갓길 콜라이더의 경계와 차량 차체·바퀴 폭을 읽어 양쪽 여유가 같도록 계산합니다. 기본 구성의 목표는 `X=5.0`입니다.

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

## CAN CSV 재생 및 이상 거동 판단

CAN 시연용 CSV는 아래 네 개입니다. 실제 차량 CAN/DBC 데이터가 아니라 시연 상황을 가정해 생성한 값입니다. `ground_truth_state`와 `ground_truth_event`는 시나리오의 설정과 검증용 정답이며 CAN으로 심정지·졸음·전방주시 태만의 원인을 판별했다는 의미가 아닙니다.

| 명령 | CSV | 전개 |
|---|---|---|
| `can normal` | `can_normal_60kmh_15s.csv` | 60 km/h 직선 정속주행 |
| `can danger` | `can_danger_steer_accel_15s.csv` | 심정지 시연: 7초부터 조향이 작게 흔들리고, 8초에 급가속과 큰 조향 편향 시작 |
| `can caution` | `can_caution_drowsy_drift_15s.csv` | 기존 파일명을 유지한 18초 졸음 시연: 3~5초, 8~11초, 13~16초에 좌측으로 치우치고 깬 뒤 원래 주행 위치로 복귀 |
| `can distraction` | `can_distraction_held_steer_15s.csv` | 바닥 물건을 집는 전방주시 태만 시연: 5초 정상주행 뒤 조향 편향 유지, 늦은 복귀 |

Python은 재생 시작 시 CSV를 한 번 읽고 `time_s`에 맞춰 5Hz로 적용합니다. 기존 NDJSON은 20Hz를 유지하며 그 사이에는 마지막 CAN 입력을 반복 전송합니다.

```bash
python3 Unity/python/vehicle_tcp_server.py
```

Unity 차량이 Cruise로 약 60km/h에 도달한 뒤 Python 콘솔에서 실행합니다.

```text
can normal     # 정상 CSV 15초 재생
can caution    # 졸음·복귀 3회 CSV 18초 재생
can distraction # 전방주시 태만 시연 CSV 15초 재생
can danger     # 심정지 상황의 급가속·급조향 시연 CSV 15초 재생
can status     # 재생 시간, CAN 이상 감시 상태, Unity 비상 단계
can stop       # 재생 중지 및 Cruise 복귀 (Danger는 잠금 유지)
can restart    # 이미 로드한 CSV를 처음부터 재생
state caution  # 외부 분류 결과를 Caution으로 입력해 보정 시연
state danger   # 기존 비상 정차 시연
```

기본 CSV 경로는 `--can-normal-csv`, `--can-caution-csv`, `--can-distraction-csv`, `--can-danger-csv`로 바꿀 수 있습니다. `--can-max-steering-angle` 기본값 30도는 Unity 차량의 `maxSteeringAngle`과 맞춰야 합니다. CSV를 다시 생성하려면 `python3 Unity/python/generate_can_demo_csvs.py`를 실행합니다. 기준 정상주행 CSV에서 나머지 세 파일을 생성하며, 졸음 CSV만 18초로 확장합니다.

졸음 CSV의 Caution 구간은 실제로 조는 시간(3~5초, 8~11초, 13~16초)입니다. 깬 뒤에는 조향을 중립으로만 놓지 않고 반대 조향으로 원래 횡위치에 복귀합니다. 복귀 완료 시점은 각각 7.8초, 12.8초, 17.8초입니다. 이 복귀 경로는 60km/h와 현재 차량 조향 설정을 사용해 계산했으므로 Unity 물리 주행에서의 실제 복귀 오차는 별도로 확인해야 합니다.
CSV를 수정한 뒤에는 `can caution`을 다시 입력해야 파일을 새로 읽습니다. `can restart`는 이전에 메모리에 읽어둔 내용을 재생합니다.

CAN은 원본 조향·페달·종가속도의 차량 조작 이상만 감시합니다. 네 CSV의 `ground_truth_*`는 판단에 사용하지 않고 Unity 경고·비상 상태도 직접 바꾸지 않습니다. Normal은 이상 기록만, Caution은 경고 UI·소리와 조향/가속 보정, Danger는 기존 비상 정차를 수행합니다. 외부 모델 대신 콘솔 `state caution`/`state danger`로 상태를 지정할 수 있습니다. 과거의 `--can-demo-mode` 옵션과 `can demo on|off` 명령은 없습니다.

재생 완료·중지·TCP 단절 때 CAN 입력을 해제하고 Cruise로 복귀합니다. 외부에서 정한 운전자 상태는 바꾸지 않습니다. Danger가 확정되면 CAN 명령을 차단하고 기존 비상 정차가 제어권을 가집니다.

CAN 조향은 조향각 ÷ 최대 조향각으로 정규화해 Remote 입력으로 보냅니다. 정상 주행 구간은 Unity가 보고한 실제 `speed_kph`와 CSV의 목표 속도(기본 60km/h)를 비교해 기존 Cruise와 같은 P 제어 값으로 가속·제동합니다. 따라서 CSV의 약 22% 기본 페달 값을 계속 적용해 가속하지 않습니다. 초기 페달 값보다 20%p 이상 급증하거나 브레이크가 입력된 구간은 CSV 페달을 그대로 적용해 `can danger`의 8초 이후 급가속을 유지합니다. `can_throttle`과 `can_brake`에는 제어에 적용한 값과 별도로 CSV 원본 페달 값이 남습니다. Unity 속도 보고가 아직 없으면 정속 구간은 가속·제동을 요청하지 않습니다. 위치·속도 Transform은 덮어쓰지 않으며, 실제 속도와 차선 이탈 폭은 Unity 주행에서 확인해야 합니다.

`can_anomaly.py`는 Unity와 같은 속도별 앞바퀴 조향 축소율, 축간거리 2.7m를 사용해 `v² tan(delta)/L`의 절댓값을 계산합니다. CSV 조향값은 핸들각이 아니라 저속 기준 앞바퀴 조향 명령으로 생성되었으므로 조향비 기본값은 1입니다. 5도/초 이상 변화와 예상 횡가속도 2m/s² 이상이 함께 발생하거나, 6m/s² 이상이 0.4초 지속되면 조향 이상을 확정합니다. 페달 증가 80%p/초와 원본 종가속도 0.05g 이상이 함께 발생한 뒤 높은 페달·종가속도가 0.4초 유지되면 가속 이상을 확정합니다. 각 채널은 원본 입력이 별도 정상 범위로 돌아와 1초 지속되어야 해제됩니다. 이 수치는 시뮬레이터 초기 설정이며 실제 차량 안전 기준이 아닙니다.

Caution의 CAN 조향은 Unity `CautionLaneAssistController`가 물리 프레임마다 보정합니다. Bridge가 기존 Scene의 차량에도 컴포넌트를 자동으로 추가하므로 Scene을 다시 만들 필요가 없습니다. 실제 왼쪽 `LaneDash`와 오른쪽 `ShoulderBoundary`의 안쪽 경계, 차체 크기, 바퀴 바깥쪽 폭을 읽고 차체가 돌아간 경우 앞뒤 모서리까지 포함합니다. 현재 Scene의 경계는 X=-1.44~2.93m, 차량의 보수적 반폭은 1.0m입니다.

평상시에는 CAN 조향을 그대로 적용해 실제 편향이 발생하게 합니다. 바깥쪽으로 이동하면서 중앙 오차가 0.45m를 넘거나, 남은 차선 여유가 현재 횡이동을 멈추는 데 필요한 거리+0.25m보다 작아지면 복귀를 시작합니다. 필요한 거리는 실제 횡속도뿐 아니라 차체 회전속도, 현재 전진 속도, 조향 반응 지연 0.35초를 반영합니다. CAN 조향만으로 계산한 1.1초 예상 경로는 진단값이며, 그것만으로 차를 미리 중앙에 묶어두지 않습니다. CAN 이상 분류 결과는 그대로 유지합니다.

복귀 중에는 중앙 오차와 횡속도로 반대 조향을 계산하고, 중앙에 가까워지면 다시 조향을 풀어 전방을 향하게 합니다. 중앙·횡속도·방향이 안정된 상태가 1초 지속되고 원본 조향이 중립 근처(기준 조향각 ±0.45도)에서 0.1초 유지되면 제어권을 반환합니다. 물리적 복귀와 원본 입력 복귀의 타이머를 분리해 다음 졸음까지 계속 개입하지 않도록 했습니다. 조향 최대 입력은 ±0.14, 변화율은 초당 0.8, 횡가속도 목표 제한은 3.5m/s²이며 모두 컴포넌트 설정입니다. Stage 1의 알려진 직선 도로를 사용하는 제어이며 영상 차선 인식이나 경로 계획은 추가하지 않았습니다.

Unity가 `lane_geometry_ready=true`를 보고하면 Python은 원본 조향 명령을 전달해 중복 보정을 피하고, 실제 적용 조향은 Unity `vehicle_state.steering`에 남습니다. 이전 Unity 스크립트에는 기존 Python 위치·방향 보정만 적용되므로 관련 Unity 스크립트 3개도 동기화해야 합니다. 가속 이상에서는 Unity 실제 속도를 재생 시작 목표 속도(기본 60km/h)와 비교해 가속을 해제하거나 제동합니다. 원본 진단값은 `can_steering`/`can_throttle`/`can_brake`에 남으며 감지 입력으로 Unity 보정값을 사용하지 않습니다. 운전자 최종 상태는 계속 `publish_prediction(shared, label)`로 연결합니다.

오프라인 검증: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s Unity/python -p 'test_can_integration.py'`

외부 상태 입력으로 Danger를 시험했다면 `can stop`, `state normal`, Unity `R`, `can restart` 순서로 다시 시험합니다.

실제 Unity 검증 결과는 `validation/can_lane_validation.json`, 첫 졸음의 위치·조향 기록은 `validation/can_lane_recovery.png`에 저장했습니다. 검증은 차선 안에 머무르는 것과 함께 실제 0.25m 이상 편향, 양방향 조향, 최종 중앙 오차 0.15m 이하를 요구합니다. 조향을 고정한 채로는 검증을 통과할 수 없습니다.

2026-11-12 Unity 6.3 LTS의 Caution 시험에서 졸음은 0.58~0.62m, 급조향은 0.60m, 전방주시 태만은 0.79m 편향된 뒤 중앙으로 복귀했습니다. 최종 중앙 오차는 모두 4cm 이내였고, 차량 외곽의 최소 차선 여유는 각각 약 0.54m, 0.55m, 0.37m였습니다. 첫 졸음은 7.8초에 개입이 해제된 것도 추가 확인했습니다. 정상에서는 조향 개입이 없었습니다. 발열 제한 때문에 졸음 18초는 구간별로 실행했으며, 중간 연결이 끊긴 연속 실행 시도는 통과 기록에 포함하지 않았습니다.

실제 Unity 측정을 다시 하려면 Python 서버를 끈 상태에서 다음 명령을 실행하고 Unity Play를 누릅니다. `RESULT`가 출력되면 Play를 종료합니다. 이 검증기는 구간 완료와 차선 여유 0.15m 초과를 모두 확인하며 실패하면 종료 코드 1을 반환합니다.

```bash
python3 Unity/python/verify_can_lane.py caution --start 2 --end 8 --output /tmp/can-lane-check.json
```

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
4. Danger 직후 가속을 끊고 감속하며 차체 방향과 횡속도를 안정화합니다.
5. 현재 위치·진행 방향·횡속도를 매 물리 프레임 반영해 갓길 중앙으로 이동합니다. 갓길 중앙에서 방향과 횡속도가 안정되면 제동해 정차합니다.
6. HUD의 단계가 아래 순서로 바뀝니다.

```text
SignalAndSlow -> MoveToShoulder -> StraightenAndBrake -> Stopped
```

급조향으로 횡속도가 생기면 예상 정지 위치를 반영해 목표 지점 전에 반대 조향을 시작합니다. 도로 경계를 넘을 것으로 예측되더라도 잔디 지면 위에서 갓길 복귀를 계속 시도합니다. 지면에서 크게 이탈하거나 진행 방향을 잃으면 `StopInPlace -> Stopped`로 전환해 조향을 유지하며 현재 위치에서 제동합니다. 정차 시 차량 위치·방향을 강제로 옮기지 않습니다. `Stopped`에서는 브레이크, `hazard=true`, 비상등 점멸, `ecall=true`가 유지됩니다. 다시 시험하려면 Python에서 `state normal`을 보낸 뒤 Unity에서 R을 누릅니다.

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
- `EmergencyPullOverController`: 도로·갓길·차체 콜라이더 참조, 좌우 여유 거리, 안정화·갓길 도착 속도, 제동/조향 값과 입력 변화율
- `EndlessRoadController`: 세그먼트 길이, 후방 재활용 거리, 차량/세그먼트 참조
- `DriverStateWarningUI`: 경고음 볼륨, 주파수, 길이와 Canvas 참조
- `HazardLightController`: 전·후방 램프 Renderer 참조, 점멸 간격(기본 0.45초)
- `UnityTcpVehicleBridge`: 서버 Host/Port, 상태 송신률

## 데이터 전체 흐름

```text
모델의 최종 분류 TCP 출력 (5002)
  -> model_state_client.py (Normal / Caution / Danger만 수신)
  -> SharedData.receive_model_result()
  -> Python ControlCommand.driver_state
  -> 20Hz NDJSON TCP (5001, 기존 CAN/차량 명령과 합침)
  -> UnityTcpVehicleBridge.CurrentDriverState
  -> Caution: DriverStateWarningUI
  -> Danger: EmergencyPullOverController -> HazardLightController
  -> BasicWheelVehicleController
  -> WheelCollider
```

Caution 메시지는 상태가 유지되는 동안 표시되고 Normal로 돌아가면 자동으로 사라집니다. 경고음은 Caution으로 진입할 때만 발생하며 같은 상태의 반복 패킷에서는 다시 울리지 않습니다. 비상 정차 중에는 분류가 흔들려도 Emergency 컨트롤과 비상 UI가 우선합니다.

## 실제 모델 연결 위치

`driver-state-monitoring-system`의 소스·설정·가중치는 수정하지 않습니다. 모델의 기존 TCP 서버를 **5002**로 실행하고, 기존 차량 서버가 그 결과를 받아 Unity의 기존 **5001** 연결로 전달합니다. 모델에 포함된 Unity 스크립트를 현재 프로젝트에 덮어쓸 필요가 없습니다.

저장소 루트에서 터미널 두 개를 사용합니다. 먼저 모델 README에 따라 별도 런타임 자료와 모델 실행용 Python 환경을 준비해야 합니다. 현재 Git에 포함된 코드만으로는 가중치·원본 영상·ECG/PPG 자료가 없어 추론을 시작할 수 없습니다.

터미널 1 — 기존 모델 실행 옵션으로 출력 포트만 지정합니다. `E4`는 심정지 시연 입력이며 다른 입력은 `--only N`, `--only D3`, `--only A_new`로 선택할 수 있습니다.

```bash
sh driver-state-monitoring-system/run_multimodal.sh --only E4 --unity --unity-port 5002
```

터미널 2 — 차량 서버를 모델 연결 모드로 실행합니다. 이 서버는 Python 표준 라이브러리만 사용하며 모델용 torch/MediaPipe를 불러오지 않습니다.

```bash
python3 -B Unity/python/vehicle_tcp_server.py --model-port 5002
```

이후 Unity Play를 누릅니다. `UnityTcpVehicleBridge`의 Host는 `127.0.0.1`, Port는 기존 `5001`입니다. Unity가 연결돼야 모델 쪽 TCP에 접속하므로 녹화 시나리오가 Unity Play보다 먼저 진행되지 않습니다. Unity 연결이 끊기면 모델 TCP도 닫아 모델의 기존 재접속 대기 동작을 이용합니다. 모델 파일의 분석·초기화 자체는 Unity 연결 전에 시작될 수 있습니다.

연결 후에는 콘솔에 `[MODEL INPUT] active`, 상태가 바뀔 때 `[MODEL] final driver state ...`가 표시됩니다. 모델 재생은 기존 녹화 영상과 저장된 ECG/PPG 입력을 사용합니다. 이 연결 작업에서 실시간 카메라·센서 입력은 추가하지 않았습니다.

- 최종 `fusion_state`를 우선 읽고 `driver_state`와 일치하는 세 분류만 적용합니다. 비전·생체신호 개별 판단, 조향·가속·제동 값은 적용하지 않습니다.
- Normal은 기존 주행을 유지하고, Caution은 기존 UI·경고음과 CAN 보정을 사용하며, Danger는 기존 비상등·갓길 정차·e-Call 시연으로 이어집니다.
- 모델 준비 중, 판단 불가, 오래된 결과, 통신 단절은 Caution 경고로 처리합니다. 이미 수신한 Danger는 입력 장애로 해제하지 않습니다. 판단 불가 상태를 모델의 실제 Caution 결과와 구분한 이유는 `status`에 표시합니다.
- 짧은 Danger 뒤 바로 Normal이 와도 Unity가 `emergency_phase`로 비상 제어 시작을 보고할 때까지 Danger 전달을 유지합니다. 이후 분류가 Normal로 바뀌어도 Unity 비상 정차는 기존처럼 R 초기화 전까지 유지됩니다.
- 모델 연결 모드에서는 `state ...`와 `scenario caution/danger`를 차단해 실제 분류 결과와 충돌하지 않게 합니다. `can normal/caution/distraction/danger`, `can stop`, `status`, `quit`는 계속 사용할 수 있습니다. CAN 시나리오 이름은 모델 분류를 바꾸지 않습니다.
- 비상 정차 후 다시 시험하려면 `can stop`, 모델의 실제 Normal 복귀, Unity R 초기화 순서로 진행하고 CAN을 다시 시작합니다. 모델은 해당 터미널에서 종료·재실행해 다시 재생할 수 있습니다.

콘솔 없이 자동 수신만 하려면 `--no-console`을 추가하고 Ctrl+C로 종료합니다. `--model-port`를 생략하면 기존 수동 상태/CAN 시연 모드입니다.

| 설정 | 기본값 | 의미 |
|---|---|---|
| `--port` | `5001` | Unity 연결 포트 |
| `--model-port` | 미지정 | 지정하면 모델 수신 활성화, 로컬에서는 `5002` 권장 |
| `--model-host` | `127.0.0.1` | 모델 서버 주소 |
| `--model-timeout` | `1.0`초 | 유효한 모델 형식의 패킷이 안 들어올 때 경고 전환 |
| `--model-max-age` | `0.75`초 | 수신 결과의 허용 분석 나이, 모델 자체의 판단 불가 처리는 그대로 존중 |

모델 추론과 Unity Editor를 실행하지 않고 Python 연결을 검증하려면 다음을 실행합니다. 실제 루프백 TCP로 패킷 분할·합쳐짐, 상태 전달, 통신 끊김·재접속, Danger 유지와 CAN 우선순위를 확인합니다.

```bash
python3 -B -m unittest discover -s Unity/python -p 'test_*integration.py' -v
```

2026-11-13에는 제공받은 `driver-runtime-assets.zip`을 복원하고 Python 3.11.15 전용 `.venv`에 원본 `requirements-lock.txt`를 설치했습니다. 모델 소스 132개 파일과 가중치·원본 입력 26개의 해시를 확인했고, 모델 자체 검증 293개와 CAN/모델 연결 검증 41개가 통과했습니다.

원본 N·D3·E4·A_new 전체 2,123프레임의 실제 추론을 기존 차량 서버와 화면 없는 TCP 수신기로 확인했습니다. 첫 검증의 4분 시간 상한 이후 A_new를 별도로 완료했으며, Unity Editor와 차량 물리는 실행하지 않았습니다. E4는 영상 시간 5.00초에 Caution, 11.01초에 Danger가 처음 출력됐습니다. 정상용 N에서도 `eyes_closed`와 `recovery_pending`에 의한 Caution이 148프레임 발생했으며 원본 모델 판단을 그대로 전달했습니다.

이 환경의 전체 녹화 처리 속도는 7.3~9.2FPS였습니다. 이 수치는 추론·화면 생성·결과 저장을 포함하며 원본 영상 재생보다 느립니다. 실시간 카메라 처리 성능과 실제 Unity 주행은 별도 검증이 필요합니다. 상세 결과는 로컬 `driver-state-monitoring-system/outputs/runtime_validation_20261113/summary.json`, 분석 영상은 해당 결과의 `preview_path`에 있습니다.

## 문제 해결

- 차량이 움직이지 않으면 WheelCollider 4개 참조, Rigidbody `Is Kinematic`, 바퀴와 노면 접촉을 확인합니다.
- 경고음이 안 들리면 Main Camera의 `AudioListener`와 Inspector의 `warningVolume`을 확인합니다.
- Python이 다른 컴퓨터라면 `PythonTcpBridge > Host`를 Python 컴퓨터 IP로 바꿉니다.
- 재질이 분홍색이면 생성된 Material의 Shader를 프로젝트 렌더 파이프라인에 맞는 Lit/Standard로 변경합니다.
