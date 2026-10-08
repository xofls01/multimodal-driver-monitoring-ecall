# Python ↔ Unity TCP JSON 규격

## 전송 방식

- TCP 포트 기본값: `5001`
- 문자 인코딩: UTF-8
- 메시지 구분: JSON 한 개 뒤에 줄바꿈 `\n`
- 권장 송수신 주기: 20Hz
- 연결 방향: Unity가 TCP Client, Python이 TCP Server

TCP는 메시지 경계를 보장하지 않으므로 수신 측에서는 줄바꿈이 나올 때까지 데이터를 모은 뒤 JSON 하나로 해석합니다.

## Unity → Python

```json
{
  "type": "vehicle_state",
  "sequence": 24,
  "timestamp": 12.45,
  "speed_kph": 38.2,
  "position": {"x": 0.75, "y": 0.43, "z": 26.4},
  "forward": {"x": 0.02, "y": 0.0, "z": 0.99},
  "heading_deg": 1.2,
  "steering": 0.0,
  "throttle": 0.4,
  "brake": 0.0,
  "control_mode": "Remote",
  "hazard": false,
  "ecall": false
}
```

## Python → Unity

```json
{
  "type": "vehicle_control",
  "sequence": 25,
  "throttle": 0.4,
  "brake": 0.0,
  "steering": 0.15,
  "hazard": false,
  "ecall": false,
  "remote_enabled": true
}
```

| 필드 | 범위 | 의미 |
|---|---:|---|
| `throttle` | 0.0~1.0 | 가속 입력 |
| `brake` | 0.0~1.0 | 브레이크 입력 |
| `steering` | -1.0~1.0 | 음수는 좌회전, 양수는 우회전 |
| `hazard` | Boolean | 비상등 상태 |
| `ecall` | Boolean | e-Call 상태 |
| `remote_enabled` | Boolean | `false`이면 Unity 키보드 제어로 복귀 |

## 안전 동작

- Unity는 Python 명령을 `0.75초` 이상 받지 못하면 가속을 해제합니다.
- 연결이 끊긴 상태에서는 설정된 약한 브레이크를 적용합니다.
- Unity TCP Client는 기본적으로 1초마다 Python Server에 재접속합니다.
- Python Server는 Unity 연결이 종료되어도 계속 실행되며 다음 연결을 기다립니다.
