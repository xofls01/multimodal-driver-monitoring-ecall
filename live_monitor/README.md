# 실시간 운전자 모니터 (카메라 + ECG/PPG → Unity → e-Call)

녹화 파일 대신 **Pi에 연결한 카메라와 아두이노(ECG/PPG)**를 실시간으로 받아 원본 비전·생체 모델로 판단하고,
Unity가 갓길에 정차하면 e-Call(MSD)을 보낸다. 연구용 프로토타입이며 진단용이 아니다.

```
[Pi] 카메라(/dev/video0) + 아두이노(/dev/ttyUSB*) ──SSH──▶ [PC] run_live.py ──5002──▶ run_vehicle_with_ecall.py ◀──5001──▶ Unity
                                                          원본 모델(비전·생체) 그대로              정차 → Pi(MQTT) → 폰 MSD
```

- `driver-state-monitoring-system/` 코드는 수정하지 않고 불러다 쓴다. 바뀐 건 입력(파일 → 실시간)과 화면뿐이다.
- 화면: 카메라 + 비전 표시(원본 `draw_overlay`) + **생체신호는 BPM 숫자 하나**(ECG/PPG 그래프·채널 정보 없음).
  BPM은 원본 규칙 그대로(ECG 우선, 없으면 PPG) → 위험 판정·MSD와 같은 기준.
- 기록(`live_monitor/sessions/LIVE_날짜시각/`, git 제외): `video.mjpeg`(받은 JPEG 그대로), `frames.csv`,
  `biosignals.csv`(ECG·PPG 원본 전 샘플), `unity_packets.jsonl`(판단), `bio_status.jsonl`, `session.json`.

## 준비 (처음 한 번)

1. **모델용 Python** — MediaPipe는 한글 경로에서 동작하지 않으므로 영문 경로에 만든다.
   ```bash
   py -3.11 -m venv %USERPROFILE%\.venvs\driver-live
   %USERPROFILE%\.venvs\driver-live\Scripts\python -m pip install -r driver-state-monitoring-system\requirements-lock.txt paramiko pytest
   ```
2. **모델 가중치** — `driver-state-monitoring-system/models/`에 `yolo26n.pt`, `yolo26n-pose.pt`,
   `biosignal/hr_classifier_ft.pt`, `papagei_s.pt`, `ppg_head_ft.pt` (해시는 `runtime_assets_manifest.json`).
3. **Pi** — 카메라와 아두이노(ECG/PPG 펌웨어 `ECG:<n> PPG:<n>` 250 Hz)를 Pi USB에 연결.
   Pi에 `python3-opencv`, `python3-serial`이 있어야 한다. PC에서 `ssh ecall@192.168.0.64`로 한 번 접속해 호스트 키를 등록해 둔다.

## 실행 (저장소 루트, 터미널 2개)

```bash
# 터미널 1: 차량 서버 + e-Call
py -B ecall_mqtt\run_vehicle_with_ecall.py --ecall-broker 192.168.0.64 --model-port 5002

# 터미널 2: 실시간 모니터 (Pi 비밀번호를 물어봄)
%USERPROFILE%\.venvs\driver-live\Scripts\python -B live_monitor\run_live.py --pi-host 192.168.0.64
```

그다음 Unity Play. 순서:
1. **워밍업 8초** (`WARMING UP`): 모델 초기화. 판단·송신 없음 (Unity에는 판단 불가 → 주의로 표시될 수 있음).
2. **판단 시작 후 0.5~5초**: 운전 자세로 **정면을 본다** (비전 보정. 이 구간에 최소 8장 필요).
   화면 아래에 `CALIBRATING … | body OK / NOT VISIBLE (show hips)`가 나온다.
   생체신호는 처음 약 5초 동안 측정 준비(`Measuring baseline`).
3. 콘솔에 `보정 완료: 자세 기준 OK`가 나오면 준비 끝. `⚠ 자세 기준 없음`이 나오면 쓰러짐을 판단할 수 없는 상태이니
   카메라를 조정하고 화면에서 **`c`(다시 보정)**를 누른다.
4. 운전하다가 쓰러지면(고개·몸이 앞으로 무너진 자세 지속) Danger → Unity 갓길 정차 → 완전 정차 후 e-Call.
5. 종료: 화면에서 `q` 또는 Ctrl+C. 다시 할 때는 Unity도 새로 시작.

### 정차 조건과 운전자 개입

| 상황 | 결과 |
| --- | --- |
| 쓰러진 자세 0.8초 → 주의, 3초 더 지속 | Danger (원본 규칙) |
| 눈 감김 1.5초 | 주의 + Unity 경고 화면·경고음 (원본 규칙) |
| ↳ **경고가 전달된 뒤 3초 동안 눈을 뜨지 않음** | **Danger** (실시간 전용 `--no-response-stop 3`, `live_rules.py`). MSD 사유: 눈 감김, 경고 무응답 |
| ↳ 그 사이 눈을 뜸 | 타이머 초기화. 얼굴이 잠깐 안 보이는 것은 뜸으로 보지 않음 |
| 정차 도중(완전 정차 전) 눈을 뜨고 + 핸들·페달(W/S/A/D, Space) 조작 | 비상 정차 취소, 운전자에게 주도권 (Unity). e-Call 보내지 않음 |
| 완전 정차 후 | e-Call 전송. 다시 출발은 R 리셋 |

원본 모델은 눈 감김만으로는 정차하지 않는다("Eye stop remains disabled until separately validated").
`--no-response-stop 0`이면 원본대로. 무응답 타이머는 차량 서버가 연결돼(경고가 Unity로 실제 전달될 때) 있을 때만 센다.

### 자세 기준과 자동 재보정

원본 모델은 보정할 때 **어깨와 엉덩이가 모두 보이는 장면**에서만 고개 높이·몸통 각도 기준을 모은다.
엎드린 뒤에는 엉덩이가 가려져도 괜찮다(고개 높이는 코·어깨로 계산. E4도 쓰러진 동안 엉덩이 0.24~0.51).
엉덩이가 꼭 보여야 하는 건 **보정할 때뿐**이다. 기준이 없으면 엎드렸을 때 쓰러짐도 회복도 판단할 수 없어
원본 모델이 '측정 불가(회색, `recovery_unobserved`)'로 표시하고 정차하지 않는다.

- 자동 재보정(`--auto-recalibrate 2`): 자세 기준이 없을 때 **정상 상태로 얼굴·어깨·엉덩이가 2초 이상** 보이면
  그 자리에서 다시 보정한다(10초 간격 제한). 콘솔에 `자동으로 다시 보정합니다`가 나오면 5초 동안 정면을 본다.
  다시 보정하면 생체신호 측정 준비(약 5초)도 처음부터 한다.
- 2026-11-15 실시간 기록으로 확인: 시작 보정 0/34 실패 → 고쳐 앉음 → 자동 재보정 OK(22/22) →
  그 뒤 엎드림이 회색이 아니라 쓰러짐 주의 → Danger.

### 카메라·센서 배치 (중요)

- **어깨와 엉덩이가 화면에 보여야 한다.** 원본 모델은 자세(몸통 각도, 고개 높이)의 기준을 어깨+엉덩이 관절로 잡는다
  (관절 신뢰도 0.30 이상). 엉덩이가 책상·칸막이에 가리면 자세 기준이 생기지 않아 **쓰러짐을 판단하지 못한다**.
  E4 녹화처럼 의자에 앉은 허리·엉덩이까지 보이게 카메라를 조금 뒤·위로 둔다.
  2026-11-14 첫 실시간 시험: 엉덩이 신뢰도 0.1 → 자세 기준 없음 → 쓰러져도 Danger 없음(눈 감김·시선 이탈 주의만).
- **PPG(손가락 센서)는 움직이면 신호가 깨진다.** 첫 시험에서 ECG는 안정적(신호 품질 0.9~1.0)이었지만 PPG는 품질 0~0.2로
  한 번도 쓸 수 없었다. 이때 원본 생체 모델은 'ECG는 있는데 PPG 맥박 없음'(`NO_PULSE_UNCONFIRMED`)으로 계속 주의를 낸다.
  PPG를 끼운 손은 핸들 조작에 쓰지 말고 가만히 두거나, 센서를 테이프로 고정한다.

콘솔에 5초마다 `처리 fps / 모델 ms / 최대지연`이 나온다. 처리 fps가 3 아래로 오래 떨어지면 판단이 늦어진다.

## 실시간 전용 설정 (원본 파일은 그대로, 이 실행에서만)

이 PC(i3-14100, GPU 없음)에서 원본 모델은 프레임당 약 0.1초, 휴대폰·물건 검출이 도는 프레임은 약 0.4초 걸린다.
원본 설정은 녹화 재생(초당 15장, 영상 시간 기준)에 맞춰져 있어서 실시간에서는 다음을 바꾼다. 실행할 때 콘솔과 `session.json`에 남는다.

| 옵션 | 원본 | 실시간 기본 | 이유 |
| --- | --- | --- | --- |
| `--max-gap` | 0.25 s | 0.6 s | 관찰 간격이 이보다 길면 쓰러짐·눈감김 지속 시간이 0으로 리셋됨. 판정 기준 초는 그대로 |
| `--phone-fps` | 5 /s | 1 /s | 휴대폰 검출(960 px, 약 0.4초)이 매 프레임 돌아 처리 속도가 무너짐. 검출 결과 유지(0.8초)는 그대로 |
| `--handheld-fps` | 3 /s | 2 /s | 손에 든 물건 검출도 같은 이유 |
| `--fps` (카메라) | 30 | 15 | 판단은 최신 프레임만 쓰므로(초당 약 5~9장) 네트워크·CPU 여유. 실제 Pi는 약 7 fps로 들어왔다 |
| `calibration_max_seconds` | 3 s | 보정 구간 끝(5 s) | 원본은 보정 표본을 3초까지만 모은다. 초당 판단 수가 적어 구간 끝까지 모은다 |

휴대폰·물건 검출 빈도를 낮추면 주의분산(휴대폰 사용) 판단이 조금 늦어질 수 있다. 쓰러짐·눈감김·생체 판단에는 영향이 없다.

## Pi 없이 시험

녹화 폴더(`biosignals.csv`, `frames.csv` + 영상)를 실제 시간 속도로 흘려보낸다.
E4 녹화 zip을 푼 폴더라면:

```bash
%USERPROFILE%\.venvs\driver-live\Scripts\python -B live_monitor\run_live.py --folder <E4 폴더> --video <E4 폴더>\video_overlay.mp4 --crop-height 720
```

2026-11-14 확인(E4, 이 PC, Unity·차량 서버·Pi 함께): 처리 4~7 fps, 4.1초 비전 보정 완료, 7.9초 Caution(가슴에 손),
11.7초 Danger(녹화 분석 11.0초) → Unity 정차 → e-Call `SENT`(사유: 쓰러짐·맥박 미검출, 마지막 심박 188 bpm).

시험: `%USERPROFILE%\.venvs\driver-live\Scripts\python -B -m pytest -q live_monitor\test_live.py`
