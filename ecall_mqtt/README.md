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
| `ecall_screen.py`, `ecall_screen.html` | PC (Pi도 가능) | 받은 MSD를 터미널 대신 브라우저 화면으로 표시 (시연용, 아래 4번) |
| `screen_window.py` | PC (Windows) | 수신 화면을 앱 창으로 화면 가로 절반 크기·가운데·항상 위에 띄움 |
| `test_ecall_screen.py` | PC | 브로커·브라우저 없이 화면 시험 |
| `run_e4_demo.ps1`, `run_e4_demo_mac.sh` | PC / Mac | 아래 실행 과정을 명령어 하나로 (바로 아래 'Windows·macOS 한 번에 실행') |

## Windows·macOS 한 번에 실행 (Pi 없이 PC 한 대로 비전 + Unity + e-Call)

명령어 하나로 Unity 에디터, e-Call 서비스(Pi 역할), e-Call 수신 화면(폰 역할), 차량 서버를 모두 켠다.
비전·생체신호는 저장해 둔 E4 분석 결과(영상 포함)를 CAN danger 시나리오와 함께 재생한다.
Unity에서 Play를 누르면 위험 판단 → 비상등·갓길 정차 → e-Call 전송이 진행되고, **e-Call이 도착하는 순간 브라우저에 수신 화면(http://localhost:8765)이 열린다.**

차량 실행 래퍼는 현재 정차 사건의 `SENT` 결과를 Unity에도 전달한다. Unity 왼쪽 아래에 체크 표시와 **e-Call 전송 완료**가 소리 없이 3초간 나타난다. 같은 결과의 재수신은 표시를 반복하지 않는다. `SENT`는 서비스의 MQTT 브로커 발행 확인이며, 폰의 열람 확인은 아니다. 기존 전송·결과 로그·웹 수신 화면 동작은 그대로 유지한다.

**Windows** (PowerShell, 저장소 루트에서) — `ecall_mqtt/run_e4_demo.ps1`

```powershell
powershell -ExecutionPolicy Bypass -File .\ecall_mqtt\run_e4_demo.ps1
```

**macOS** (터미널, 저장소 루트에서) — `ecall_mqtt/run_e4_demo_mac.sh` (Windows 와 명령어가 다르다)

```bash
bash ecall_mqtt/run_e4_demo_mac.sh
```

- 스크립트는 자기 위치(`ecall_mqtt`) 기준으로 경로를 찾으므로 어느 폴더에서 실행해도 된다(`ecall_mqtt` 안이면 `.\run_e4_demo.ps1`, `bash run_e4_demo_mac.sh`).
- `-ExecutionPolicy Bypass`(Windows)는 이번 실행에만 스크립트 실행 제한을 푼다(시스템 설정은 그대로).
- **끝내기**: 스크립트를 실행한 창에서 Ctrl+C(또는 창 닫기). 같이 띄운 e-Call 서비스·수신 화면도 꺼진다. 다시 실행하면 이전에 남은 서버를 먼저 정리한다.
- **다시 시연**: 수신 화면에서 R(대기 화면으로) → Unity Play 다시.

### 처음 한 번 준비

1. **Unity 프로젝트**: 통합본 Unity 프로젝트, 씬 `Assets/Stage1BasicVehicle/Scenes/BasicDrivingStage1`. 기본 위치 `~/My project`(Windows `%USERPROFILE%\My project`), 다르면 아래 옵션으로 지정.
2. **MQTT 브로커(Mosquitto)를 이 PC에**
   - Windows: [mosquitto.org](https://mosquitto.org/download/)에서 설치 → 서비스 시작(관리자 PowerShell `Start-Service mosquitto`)
   - macOS: `brew install mosquitto && brew services start mosquitto`
3. **모델 가상환경** `driver-state-monitoring-system/.venv` (모델 README 참고) + `paho-mqtt`, `opencv-python`. 스크립트는 PATH의 python이 아니라 이 `.venv` 파이썬을 쓴다.
4. **저장된 E4 분석 결과** (`driver-state-monitoring-system/outputs/`는 git에 없으므로 따로 받거나 만든다)
   - `preview.mp4`, `unity_packets.jsonl`이 있는 폴더. `outputs/local_e4_test/E4` → `outputs/e4_overlay_crop/E4` 순서로 자동으로 찾는다.
   - 만드는 법(`driver-state-monitoring-system`에서, 입력 영상·생체신호 필요):
     `.venv\Scripts\python -B scripts\run_multimodal_system.py --pairs <입력 목록> --only E4 --verify-tcp --output outputs\local_e4_test`

### PC마다 다른 값 (옵션·환경 변수)

| 값 | Windows 옵션 (또는 환경 변수) | macOS 환경 변수 | 기본값 |
| --- | --- | --- | --- |
| Unity 프로젝트 폴더 | `-UnityProject` (`UNITY_PROJECT`) | `UNITY_PROJECT` | `~/My project` |
| Unity 에디터 | `-UnityExe` (`UNITY_EXE`) | `UNITY_APP` | 프로젝트 버전과 같은 Unity Hub 에디터 자동 선택 |
| 재생할 분석 결과 폴더 | `-Recording` (`E4_RECORDING`) | `E4_RECORDING` | 위 4번 자동 탐색 |
| MQTT 브로커 | `-Broker` (`BROKER`) | `BROKER` | `localhost` (Pi 를 쓰면 Pi IP. 이때 PC 에서 `ecall_service.py` 는 띄우지 않음) |

예: `powershell -ExecutionPolicy Bypass -File .\ecall_mqtt\run_e4_demo.ps1 -UnityProject "D:\Unity\DriverSim" -Broker 192.168.0.64`
예: `UNITY_PROJECT="$HOME/Unity/DriverSim" bash ecall_mqtt/run_e4_demo_mac.sh`

### 알아둘 점

- Windows 스크립트는 PowerShell 5.1이 한글을 읽도록 UTF-8(BOM)으로 저장되어 있다. 고칠 때 인코딩을 유지할 것.
- macOS 스크립트는 Windows와 같은 순서로 만들었지만 Mac에서 실제로 돌려 보지는 않았다.

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

## 4. e-Call 수신 화면 (터미널 대신 화면으로 시연)

MSD 토픽(`ecall/VIN-TEST-0001/msd`)을 구독해 받은 내용을 브라우저 화면으로 보여 준다. 추가 설치는 없다(paho-mqtt만 필요).
처음엔 "e-Call 대기 중" 화면이고, MSD가 오면 화면 테두리가 한 번 빨갛게 깜빡이며 MSD 내용이 뜬다.
표시 문구는 폰 알림(`…/text`)과 같은 `msd.msd_sections()`를 쓴다.

```bash
# PC 에서 (Pi 브로커 구독). 브라우저가 http://localhost:8765 로 자동으로 열린다
py -B ecall_mqtt\ecall_screen.py --broker 192.168.0.64

# 다른 창에서 확인용 e-Call 보내기 (또는 위 '통합본과 연결'처럼 실제 시연)
py ecall_mqtt\send_test_trigger.py --broker 192.168.0.64
```

- 화면에서 **R 키**(또는 오른쪽 아래 `초기화`)를 누르면 대기 화면으로 돌아간다. 연속 시연할 때 매번 누른다.
- **연습용** `--demo`: 브로커·Pi 없이 4초 뒤 예시 MSD가 뜬다(R을 누르면 다시). 발표 리허설이나 화면 확인용.
  ```bash
  py -B ecall_mqtt\ecall_screen.py --demo
  ```
- `--open-on-ecall`: 시작할 때가 아니라 **e-Call(MSD)이 도착할 때** 화면을 연다(차량이 정차한 뒤 화면이 뜨는 연출). 화면이 이미 열려 있으면 새로 열지 않고 맨 앞으로 가져온다.
- **Windows 창 모양**: Edge(없으면 Chrome)의 주소창 없는 앱 창으로, 작업 영역 가로 절반 × 세로 거의 전체 크기로 가운데에 띄우고 **다른 창(카톡·메모 등)보다 위(항상 위)**에 둔다. 글자 크기는 창 크기에 맞춰 커지고 작아진다. 닫기는 창의 X 또는 Alt+F4. 크기 비율은 `screen_window.py`의 `WIDTH_RATIO`, `HEIGHT_RATIO`. macOS 등은 기본 브라우저로 연다.
- 같은 공유기의 폰·노트북에서 보려면 `--host 0.0.0.0`으로 켜고 `http://<이 PC IP>:8765`를 연다.
- 위쪽 표시: 브로커 연결 상태, Pi 온라인/오프라인(`…/status`), `TEST CALL`.
- MSD에 사진 링크가 있으면 오른쪽 아래 **확장 데이터**(향후 활용 예정, 표준 MSD 항목 아님) 칸에 작게 보여 준다. 사진은 Pi 웹 서버에서 열리므로 화면을 띄운 기기가 Pi와 같은 Wi-Fi여야 한다.
- 화면 시험: `py -B -m pytest -q -p no:cacheprovider ecall_mqtt/test_ecall_screen.py`

## 주의·위험 사진 (MSD 링크)

정차 후 e-Call 을 보낼 때 PC 가 사진을 먼저 Pi 로 올리고(`ecall/<VIN>/media/<event_id>/<caution|danger>`, JPEG),
Pi 의 `ecall_service.py` 가 `media/<event_id>/` 에 저장한 뒤 **같은 공유기 안에서만 열리는 웹 서버(8080)**로 보여 준다.
MSD 에는 `media.caution_photo_url`, `media.danger_photo_url`, 알림 문장에는 `사진(주의) : http://<Pi>:8080/...` 가 들어간다.

- 사진: 모델 분석 결과의 `evidence/caution.jpg`, `danger.jpg`. 녹화 재생에서 `--recording <분석결과폴더>` 를 주면 자동으로 쓰고,
  다른 폴더는 `--ecall-media <폴더>` 로 지정한다.
  ```bash
  py -B ecall_mqtt\run_vehicle_with_ecall.py --ecall-broker 192.168.0.64 --ecall-entry play_can danger --recording driver-state-monitoring-system\outputs\<분석결과>\E4
  ```
- 사진이 없으면 지금처럼 글자 정보만 보낸다. 폰은 Pi 와 같은 Wi-Fi 여야 링크가 열린다.
- 비밀번호 없는 시연용 링크다(폴더 목록은 막음). 다른 사람이 쓰는 공유기에서는 쓰지 말 것.
- Pi 에는 `ecall_service.py`, `msd.py`, `mqtt_compat.py` 를 새로 올리고 `sudo systemctl restart ecall-service`.
  Pi 설정(`config.json`, 선택): `media_http_port`(기본 8080), `media_base_url`(기본: Pi 의 LAN 주소 자동), `media_dir`(기본 `media`)

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
