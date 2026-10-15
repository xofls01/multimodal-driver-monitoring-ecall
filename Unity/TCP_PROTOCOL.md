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
  "driver_state": "N",
  "emergency_phase": "Idle"
}
```

`control_mode`은 실제 적용 중인 `Emergency`, `Remote`, `Cruise`, `Manual` 중 하나입니다.

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
  "driver_state": "D"
}
```

| 필드 | 값 | 의미 |
|---|---|---|
| `throttle` | 0.0~1.0 | Remote 가속 입력 |
| `brake` | 0.0~1.0 | Remote 브레이크 입력 |
| `steering` | -1.0~1.0 | Remote 조향, 음수 좌/양수 우 |
| `hazard` | Boolean | 비상등 점멸 상태(일반 모드와 Remote 모드 모두 반영) |
| `ecall` | Boolean | Remote e-Call 상태 |
| `control_mode` | `cruise`, `remote`, `manual` | 명시적인 일반 제어 모드 |
| `remote_enabled` | Boolean | `control_mode` 없는 구버전 패킷의 호환 필드 |
| `driver_state` | `N`, `D`, `A`, `E` | 운전자 분류 결과 |

### 모드 해석 규칙

1. E로 잠긴 Emergency 제어가 언제나 최우선입니다.
2. `control_mode`이 있으면 해당 값을 사용합니다.
3. `control_mode` 없이 `remote_enabled` 필드가 있으면 구버전 호환을 위해 `true`를 Remote, `false`를 Manual로 해석합니다.
4. 두 모드 필드 없이 `driver_state`만 온 패킷은 현재 제어 모드를 그대로 유지합니다.

Python 기본 패킷은 `control_mode="cruise"`, `remote_enabled=false`입니다. `set`, `forward`, `left`, `right`, `stop`, `remote` 명령은 두 필드를 각각 `remote`, `true`로 바꿉니다.

## 운전자 상태와 Unity 동작

| 코드 | 의미 | Unity 동작 |
|---|---|---|
| `N` | Normal | 경고 없음, 선택된 일반 제어 유지 |
| `D` | Drowsiness | D 최초 진입 시 경고창/비프 1회 |
| `A` | Attention | A 최초 진입 시 경고창/비프 1회 |
| `E` | Emergency | 비상 UI, 비상등 점멸, 감속, 갓길 이동, 정렬, 정차, e-Call |

E는 Unity에서 한 번 감지되면 다음 단계가 잠기며 이후 분류가 흔들려도 취소되거나 처음부터 재시작되지 않습니다.

```text
Idle
-> SignalAndSlow
-> MoveToShoulder
-> StraightenAndBrake
-> Stopped
```

E가 감지되는 즉시 `hazard=true`가 잠기고 차량 전·후방 좌우 램프가 점멸합니다. `Stopped`에서도 `hazard=true`, `ecall=true`, 브레이크가 유지됩니다. 재시험은 `state N`을 전송한 뒤 Unity에서 R을 누릅니다.

일반 상태에서는 Python 콘솔의 `hazard on`/`hazard off`로 점멸을 독립 시험할 수 있습니다. E 비상 제어가 활성화된 동안에는 수신 패킷의 `hazard=false`가 비상 점멸을 해제하지 않습니다.

## 연결 안전 동작

- Remote에서 새 명령이 0.75초 이상 없으면 가속을 해제하고 설정된 약한 브레이크를 적용합니다.
- Cruise는 Python 연결 없이도 Unity 내부에서 계속 동작합니다.
- Unity는 연결 실패/종료 후 기본 1초 간격으로 재접속합니다.
- Python 서버는 Unity가 끊겨도 실행을 유지하고 다음 연결을 기다립니다.
