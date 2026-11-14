# e-Call MQTT 검증

비전(PC)이 e-Call을 요청했다고 가정하고, 라즈베리파이가 MSD를 만들어 폰의 MQTT 클라이언트 앱으로 보내는지 확인한다.
연구용 테스트 시스템이며 119·관제센터로는 보내지 않는다. 차량·차주 정보는 모두 가상 값이다.

```
PC (send_test_trigger.py)              Pi (Mosquitto + ecall_service.py)          폰 (MQTT 앱)
  ── ecall/VIN-TEST-0001/trigger ──▶   MSD 생성 · 검사 · SQLite 저장
                                       ── ecall/VIN-TEST-0001/msd ───────────▶   표시
  ◀── ecall/VIN-TEST-0001/result ───   SENT / INVALID
```

## 파일

| 파일 | 어디서 | 하는 일 |
| --- | --- | --- |
| `config.json` | 공통 | 브로커 주소, 차량 ID, 가상 차량·차주 정보 |
| `msd.py` | 공통 | MSD 생성(`build_msd`)과 검사(`validate_msd`) |
| `mqtt_compat.py` | 공통 | paho-mqtt 1.x·2.x 둘 다 동작하게 하는 도우미, 토픽 이름 |
| `ecall_service.py` | Pi | trigger를 받아 MSD를 만들어 전송. 실패 시 5초마다 재전송 |
| `pi_setup.sh`, `ecall-service.service` | Pi | 설치와 자동 실행 등록 (아래 1번) |
| `send_test_trigger.py` | PC | 비전 대신 trigger를 보내는 테스트 도구 |
| `ecall_link.py` | PC | Unity 정차(Stopped) → trigger 생성·발행 (Danger 사유·심박 포함, 한 번만) |
| `run_vehicle_with_ecall.py` | PC | 통합본 차량 서버를 **수정 없이** e-Call 과 함께 실행 |
| `test_ecall_link.py` | PC | Unity·Pi 없이 연결 시험 |

## 통합본과 연결 (비전 + 생체신호 → Unity 갓길 정차 → e-Call)

```
[PC] 모델 run_multimodal_system.py ──5002──▶ run_vehicle_with_ecall.py ◀──5001──▶ Unity
                                              (= 원본 vehicle_tcp_server + 알림)   갓길 정차 → emergency_phase "Stopped"
                                               │ Danger 순간의 사유·심박 기억, Stopped 순간 한 번 발행
                                               ▼  ecall/VIN-TEST-0001/trigger (QoS 1)
[Pi] Mosquitto + ecall_service.py ── MSD 생성·검사·SQLite 저장·재전송 ──▶ 폰
                                   └── ecall/VIN-TEST-0001/result (SENT) ──▶ PC 로그
```

- 통합본(`Unity/`, `driver-state-monitoring-system/`) 파일은 수정하지 않는다. `run_vehicle_with_ecall.py`가 실행 중에만 두 지점에 알림을 덧붙인다.
- trigger 형식은 `send_test_trigger.make_trigger()`와 같다. 추가로 `model_reasons`, `stop_basis`, `unity_position`을 근거로 넣는다(MSD에는 들어가지 않음).
- 모델 사유 → MSD 사유: 쓰러짐 자세 → `slumped_forward`, 몸 기울어짐 → `body_tilt`, 손 가슴 → `unwell_posture`, 눈 감김 → `eyes_closed`, 고개 끄덕임 → `head_down`, 빈맥 → `hr_spike`, 서맥 → `hr_drop`, 맥박 소실 → `hr_missing`. 무응답 확인 로직은 통합본에 없으므로 `no_response`는 넣지 않는다.
- 심박: 모든 모델 패킷에서 맥박 상태와 심박을 기록한다.
  - `hr_bpm`(현재 심박): 모델이 확인한 `bio_hr`. 맥박 소실 중이면 비운다(멈춘 심장을 뛰는 것처럼 보내지 않음).
  - `pulse_status`: `detected` / `not_detected_unconfirmed` / `not_detected_confirmed` / `unknown`. 알림 문장은 "맥박 미검출(센서 기준, 미확정)"처럼 센서 판단임을 밝히고 사망으로 단정하지 않는다.
  - `hr_last_bpm`, `hr_last_age_s`, `hr_last_source`, `hr_last_verified`: 맥박이 잡히던 동안의 마지막 심박. ECG 봉우리 심박(`bio_display_hr`, 모델 미검증)이 변화를 빨리 따라가므로 먼저 쓴다. 모델이 맥박 소실을 표시하기까지 약 0.6초 걸리므로(E4: 세동 7.5초 → `HEART_PAUSE` 8.11초) 소실 감지 0.5초 이전 값을 쓴다. E4 결과: 193 bpm(심실빈맥).
  - 의식이 없어도 맥박이 있으면 현재 심박을 그대로 보낸다(예: `35 bpm (심한 서맥)`). 빈맥·서맥 표시는 숫자 구간만 보고 붙이는 참고값이다.
- 전송 시점: Unity는 0.8 km/h 이하에서 `Stopped`를 보내고 그때부터 제동한다. 그래서 `Stopped` 직후가 아니라 **속도 0이 0.5초 이어진 뒤**(최대 3초 대기) 최종 정차 위치로 보낸다.
- 위치: MSD `position.unity`에 Unity 좌표 `{x, y, z}`(m, 소수 둘째 자리)를 넣고, 알림 문장에 `위치(Unity) : X …, Y …, Z … (m)`로 표시한다. MSD 형식상 필요한 위경도는 Unity 좌표를 기준 위경도(`--ecall-lat/--ecall-lon`)에 더한 **가상 값**(`confidence: simulated`)으로 함께 넣는다. (Unity 좌표 표시는 Pi의 `msd.py`가 하므로 Pi에도 새 `msd.py`가 있어야 한다.)
- PC 쪽 기록: `ecall_mqtt/ecall_requests.jsonl` (보낸 trigger, Pi 결과).

PC 실행 (모두 `ecall_mqtt` 폴더에서):

```bash
# 1) Pi 연결 확인 → [결과] ... 'status': 'SENT'
py send_test_trigger.py --broker 192.168.0.64

# 2) 수동 시연: Unity Play → 콘솔에 state danger → 갓길 정차 → e-Call → 폰 알림
#    다시 하려면 state normal → Unity 창에서 R → 차가 달리면 state danger
py run_vehicle_with_ecall.py --ecall-broker 192.168.0.64

# 3) 실시간 모델과 함께 (모델 서버를 먼저 --unity --unity-port 5002 로 켠 뒤)
#    모델 실행기: python3 -B Unity/python/run_driver_model.py --only E4 --unity --show (Linux/mac 전용, fcntl 사용)
py run_vehicle_with_ecall.py --ecall-broker 192.168.0.64 --model-port 5002

# 4) 저장된 모델 결과 + CAN 시나리오 재생 (Unity/python/play_can_model.py, opencv 필요)
py run_vehicle_with_ecall.py --ecall-broker 192.168.0.64 --ecall-entry play_can danger --recording <분석결과폴더>
```

- `--ecall-*` 외의 옵션은 진입점(`--ecall-entry vehicle` = `Unity/python/vehicle_tcp_server.py`, 기본값 / `play_can` = `Unity/python/play_can_model.py`)에 그대로 전달된다.
- `play_can_model.py`는 Linux/mac에서 `driver-state-monitoring-system/.venv/bin/python`이 있으면 그 파이썬으로 다시 실행(execve)되어 e-Call 알림이 빠진다. Windows에서는 해당되지 않는다. Linux/mac이면 그 `.venv` 파이썬으로 `run_vehicle_with_ecall.py`를 직접 실행할 것.
- 모델 분석 결과(`outputs/.../unity_packets.jsonl`, `preview.mp4`)와 녹화 자료(`driver-runtime-assets.zip`)는 GitHub에 없다. 없으면 2)의 수동 시연만 가능.

시험 (저장소 루트에서, 통합본 `__pycache__`가 git에 올라가 있으므로 `-B`로 .pyc를 만들지 않는다):

```bash
py -B -m pytest -q -p no:cacheprovider ecall_mqtt/test_ecall_link.py
```

## 1. Pi 준비 (처음 한 번)

PC에서 이 폴더를 Pi로 복사한 뒤 설치 스크립트를 실행한다. sudo 비밀번호를 한 번 묻는다.

```bash
scp -r ecall_mqtt ecall@192.168.0.64:~/
ssh -t ecall@192.168.0.64 "bash ~/ecall_mqtt/pi_setup.sh"
```

Mosquitto(1883, LAN 허용)와 paho-mqtt를 설치하고, `ecall_service.py`를 systemd 서비스(`ecall-service`)로 등록해 **Pi를 켜면 자동 실행**된다.
상태 확인: `systemctl status ecall-service`, 로그: `journalctl -u ecall-service -f`.
브로커는 아이디·비밀번호가 없는 시연용 설정이므로 같은 공유기 안에서만 쓴다.

## 2. 폰 앱 설정

1. 폰을 Pi와 **같은 공유기 WiFi**에 연결
2. 브로커 추가 : Host = Pi IP, Port = 1883, 아이디·비밀번호·TLS 없음
3. 구독 토픽 : `ecall/VIN-TEST-0001/text`(읽기 쉬운 알림), `ecall/VIN-TEST-0001/msd`(JSON)

## 3. 실행

**Pi**

```bash
cd ~/ecall_mqtt
python3 ecall_service.py
```

**PC** (`py -m pip install paho-mqtt` 후, Pi IP는 `192.168.0.64`. 이 PC에서는 `ecall.local` 이름이 안 풀린다)

```bash
py send_test_trigger.py --broker 192.168.0.64                  # 기본 시나리오 E4
py send_test_trigger.py --broker 192.168.0.64 --scenario E10   # 심박 급상승 예시
py send_test_trigger.py --broker 192.168.0.64 --scenario MANUAL
```

성공하면 PC에 `[결과] ... 'status': 'SENT'`와 지연 시간(ms)이 찍히고, 폰 앱에 MSD JSON이 뜬다.

**Pi 서비스 없이 폰으로 바로 보내기** (통신만 확인할 때)

```bash
py send_test_trigger.py --broker 192.168.0.64 --direct
```

**Pi 혼자서 확인하기** (PC 없이)

```bash
python3 send_test_trigger.py --broker localhost
```

## 시나리오 예시

| 코드 | 사유(reason) | 심박 |
| --- | --- | --- |
| E2 | unwell_posture, no_response | 없음 |
| E4 | slumped_forward, no_response | 없음 |
| E8 | hr_spike, slumped_forward, no_response | 없음 |
| E10 | hr_spike, unwell_posture | 135 (주입 예시 값) |
| MANUAL | manual_sos (activation = manual) | 없음 |

## 알아둘 점

- `SENT`는 **브로커(Pi)가 받았다**는 뜻이다. 폰이 실제로 받았는지는 MQTT 앱으로는 확인할 수 없다. 나중에 우리 앱에서 ACK를 돌려보내게 만들면 된다.
- 같은 `event_id`는 한 번만 처리한다. 폰 쪽에서는 `msd_id`로 중복을 거른다.
- 전송 기록은 Pi의 `ecall_log.db`(SQLite)에 남는다.
- 비전 코드에 붙일 때는 `send_test_trigger.py`의 `make_trigger()`와 `request_ecall()`을 가져다 쓰고, Unity에서 `STOPPED`(정차 위치)를 받았을 때 호출하면 된다.
