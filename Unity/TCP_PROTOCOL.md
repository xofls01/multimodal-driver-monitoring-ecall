# Python ↔ Unity TCP JSON 규격

## 전송 방식

- TCP 기본 포트: `5001`
- 인코딩/프레이밍: UTF-8 NDJSON, JSON마다 `\n`
- 기본 송수신률: 20Hz
- 연결 방향: Unity TCP Client → Python TCP Server

TCP는 패킷 경계를 보장하지 않으므로 양쪽 모두 줄바꿈까지 누적한 뒤 JSON 하나로 해석합니다.

## Unity → Python: vehicle_state

```json
{
  "type": "vehicle_state",
  "sequence": 24,
  "timestamp": 12.45,
  "speed_kph": 59.4,
  "position": {"x": 0.75, "y": 0.43, "z": 126.4},
  "forward": {"x": 0.02, "y": 0.0, "z": 0.99},
  "heading_deg": 1.2,
  "steering": 0.0,
  "throttle": 0.08,
  "brake": 0.0,
  "control_mode": "Cruise",
  "hazard": false,
  "ecall": false,
  "driver_state": "Normal",
  "emergency_phase": "Idle"
}
```

`control_mode`은 실제 적용 중인 `Emergency`, `Remote`, `Cruise`, `Manual` 중 하나입니다.

차선 보정 진단 필드는 다음과 같습니다. 차선 여유는 차량 중심이 아닌, 회전한 차체와 바퀴 외곽에서 차선 안쪽 경계까지의 최소 거리입니다.

| 필드 | 의미 |
|---|---|
| `lane_geometry_ready` | 실제 차선·차량 크기 확인 완료. Python의 중복 조향 보정을 끄는 기준 |
| `lane_assist_authorized`, `lane_assist_active` | Caution+CAN 제어권, 실제 개입 여부 |
| `lane_left_boundary_m`, `lane_right_boundary_m` | 도로 횡축 방향 차선 안쪽 경계 |
| `lane_clearance_m`, `lane_min_clearance_m` | 현재 여유, 현재 CAN 실행의 물리 프레임 전체 최소 여유 |
| `lane_predicted_clearance_m` | 원본 조향 적용 시 1.1초 예측 구간의 최소 여유(진단값). 실제 개입은 측정한 편향과 회전 관성을 포함한 횡방향 정지거리로 결정 |
| `lane_applied_steering`, `lane_assist_reason` | 보정 명령, 개입·해제 사유 |

## Python → Unity: vehicle_control

기본 정속주행/상태 보고 패킷:

```json
{
  "type": "vehicle_control",
  "sequence": 25,
  "throttle": 0.0,
  "brake": 0.0,
  "steering": 0.0,
  "hazard": false,
  "ecall": false,
  "remote_enabled": false,
  "control_mode": "cruise",
  "driver_state": "Caution"
}
```

| 필드 | 값 | 의미 |
|---|---|---|
| `throttle` | 0.0~1.0 | Remote 가속 입력 |
| `brake` | 0.0~1.0 | Remote 브레이크 입력 |
| `steering` | -1.0~1.0 | Remote 조향 요청. Caution+CAN에서는 Unity 물리 프레임의 차선 보정 필터를 거침. 음수 좌/양수 우 |
| `hazard` | Boolean | 비상등 점멸 상태(일반 모드와 Remote 모드 모두 반영) |
| `ecall` | Boolean | Remote e-Call 상태 |
| `control_mode` | `cruise`, `remote`, `manual` | 명시적인 일반 제어 모드 |
| `remote_enabled` | Boolean | `control_mode` 없는 구버전 패킷의 호환 필드 |
| `driver_state` | `Normal`, `Caution`, `Danger` | 운전자 분류 결과 (대소문자 무관) |
| `can_active`, `can_scenario`, `can_time_s` | Boolean, String, 초 | CAN 재생 상태와 CSV 시각 |
| `can_anomaly`, `can_risk_score` | Boolean, 정수 | 원본 CAN 조향·가속 이상 여부와 활성 채널 수. 운전자 상태를 변경하지 않음 |
| `steer_anomaly`, `accel_anomaly` | Boolean | 각 원본 CAN 이상 감지의 확정 상태 |
| `steer_correction_active`, `accel_limit_active` | Boolean | Caution에서 실제 명령에 적용 중인 보정 상태. 조향 복귀 중에는 `steer_anomaly=false`여도 보정이 유지될 수 있음 |
| `can_detect_reason`, `can_release_reason` | String | 확정·해제 사유. CSV의 `ground_truth_*`는 판단에 사용하지 않음 |
| `can_steering`, `can_throttle`, `can_brake` | 정규화된 입력 | 마지막 CAN 원본에서 변환한 값 (Danger 이후에도 진단값으로 남음) |
| `can_target_speed_kph` | km/h | 정속 구간에서 Unity 실제 속도와 비교하는 목표값; 급가속 구간에서는 진단값 |

### 모드 해석 규칙

1. Danger로 잠긴 Emergency 제어가 언제나 최우선입니다.
   Caution+CAN에서만 Unity 차선 침범 예측/조향 이상 보정 및 Python 가속 이상 제한이 적용됩니다.
2. `control_mode`이 있으면 해당 값을 사용합니다.
3. `control_mode` 없이 `remote_enabled` 필드가 있으면 구버전 호환을 위해 `true`를 Remote, `false`를 Manual로 해석합니다.
4. 두 모드 필드 없이 `driver_state`만 온 패킷은 현재 제어 모드를 그대로 유지합니다.

Python 기본 패킷은 `control_mode="cruise"`, `remote_enabled=false`입니다. `set`, `forward`, `left`, `right`, `stop`, `remote` 명령은 두 필드를 각각 `remote`, `true`로 바꿉니다.

CAN CSV 재생은 같은 `vehicle_control` 패킷을 사용하며 `control_mode="remote"`로 5Hz의 마지막 입력을 20Hz에 실어 보냅니다. 정속 구간의 `throttle`·`brake`는 Unity의 실제 속도 피드백으로 계산하고, 큰 페달 변화나 브레이크 입력 구간은 CSV 원본 페달을 적용합니다. Caution에서 가속 이상이 확정되면 실제 속도에 따라 가속을 해제하거나 제동합니다. `can_throttle`·`can_brake`는 항상 CSV 원본 페달 값입니다. 원본 CAN 차량 조작 이상 감지는 활성화되어 있지만 CSV 정답 라벨이나 CAN 이상으로 `driver_state`를 바꾸지 않습니다. 외부 모델 또는 `state danger` 명령으로 Danger가 전송되면 Python의 원격 조향·가속·브레이크 값이 0으로 잠기고, Unity는 차선 보정을 포함한 일반 Remote 입력을 적용하지 않습니다.

## 운전자 상태와 Unity 동작

| 코드 | 의미 | Unity 동작 |
|---|---|---|
| `Normal` | 정상 | 주의 메시지 숨김, 선택된 일반 제어 유지 |
| `Caution` | 주의 | 메시지·경고음, CAN 재생 시 차선 침범 예측 보정과 가속 이상 제한 |
| `Danger` | 위험 | 비상 UI, 비상등 점멸, 감속, 갓길 이동, 정렬, 정차, e-Call |

`Caution` 메시지는 확인 버튼 없이 `Normal`로 돌아가면 자동으로 사라집니다. 20Hz 반복 패킷에는 경고음이 다시 울리지 않습니다. `Danger`는 Unity에서 한 번 감지되면 다음 단계가 잠기며 이후 분류가 흔들려도 취소되거나 처음부터 재시작되지 않습니다.

```text
Idle
-> SignalAndSlow
-> MoveToShoulder
-> StraightenAndBrake
-> Stopped
```

`Danger`가 감지되는 즉시 `hazard=true`가 잠기고 차량 전·후방 좌우 램프가 점멸합니다. `Stopped`에서도 `hazard=true`, `ecall=true`, 브레이크가 유지됩니다. 재시험은 `state normal`을 전송한 뒤 Unity에서 R을 누릅니다.

일반 상태에서는 Python 콘솔의 `hazard on`/`hazard off`로 점멸을 독립 시험할 수 있습니다. Danger 비상 제어가 활성화된 동안에는 수신 패킷의 `hazard=false`가 비상 점멸을 해제하지 않습니다.

## 연결 안전 동작

- Remote에서 새 명령이 0.75초 이상 없으면 가속을 해제하고 설정된 약한 브레이크를 적용합니다.
- Cruise는 Python 연결 없이도 Unity 내부에서 계속 동작합니다.
- Unity는 연결 실패/종료 후 기본 1초 간격으로 재접속합니다.
- Python 서버는 Unity가 끊겨도 실행을 유지하고 다음 연결을 기다립니다.
