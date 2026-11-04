# 운전자 생체신호(ECG·PPG) + 카메라 모니터링

Arduino Nano로 ECG(심전도)와 PPG(맥파)를 측정해 심박을 계산하고, 라즈베리파이 카메라 영상과 함께
노트북 화면에 실시간으로 보여주는 프로그램입니다. 원할 때 15~30초 동안 영상과 신호를 같이 녹화합니다.

> 연구용 프로토타입이며 의료 진단용이 아닙니다.

```
 AD8232 (ECG) ─┐                     USB 시리얼            SSH                 
 SEN-11574 (PPG)┴─▶ Arduino Nano ──────────────▶ 라즈베리파이 ◀────────────▶ Windows 노트북
                   심박 계산·융합                 (카메라 연결)              화면 표시 + 녹화 저장
```

## 주요 기능

### Arduino Nano 펌웨어 (`firmware/ppg_ecg_nano/ppg_ecg_nano.ino`)

- **샘플링**: 타이머로 1kHz마다 ECG(A0)와 PPG(A3)를 읽습니다.
  - ECG·PPG 원신호는 4개씩 평균해 250Hz로 보냅니다.
  - PPG 심박 계산에는 10개씩 평균한 100Hz 신호를 씁니다.
- **PPG 심박**: 업체 보고서(ECG_PPG_DATA_PROCESSING_REPORT) 5장 순서를 따릅니다.
  1. 접촉 판별: 2초 동안 신호가 움직이는지, 0/1023으로 포화됐는지 봅니다.
  2. 기저선(DC) 제거
  3. 23탭 FIR 저역통과 필터 (업체 계수 그대로)
  4. 박동 검출: 진폭이 20보다 크고 1000보다 작을 때만 박동으로 인정합니다.
  5. 순시 BPM: 40~180 BPM만 씁니다.
  6. 첫 5박의 중앙값으로 기준값을 잡고, 이후 30 BPM 넘게 튀는 값은 버린 뒤 칼만 필터로 평활합니다.
  - 업체 방식에서 바꾼 점: SEN-11574 파형에서는 박동 하나를 두 번 세는 문제가 있어서, 4단계 박동 검출을 적응형 임계값 방식(PulseSensor 방식)으로 바꿨습니다.
- **ECG 심박**:
  - 60Hz 노치 → 5~15Hz 대역통과 → 미분·제곱·이동적분 → 적응형 임계값으로 R 피크를 찾습니다(Pan-Tompkins 축소판).
  - 최근 RR 간격 8개의 중앙값으로 BPM을 계산합니다.
  - RR 간격이 규칙적이고 QRS 신호가 잡음보다 충분히 클 때만 "측정 중"으로 봅니다. 그래서 전극이 떨어졌을 때 잡음을 박동으로 세지 않습니다.
- **융합 심박 (HR)**:
  | 상황 | 융합 심박 | `HR_SRC` |
  | --- | --- | --- |
  | 둘 다 측정 중, 차이 10 BPM 이하 | ECG 0.7 + PPG 0.3 가중평균 | 3 |
  | 둘 다 측정 중, 차이가 큼 | ECG 값 | 4 |
  | 한쪽만 측정 중 | 그쪽 값 | 1 (ECG) / 2 (PPG) |
  | 둘 다 아님 | 0 | 0 |
- **시리얼 출력** (115200 8N1):
  - `ECG:<0..1023>\tPPG:<0..1023>`: 250Hz
  - `PPG_BPM:..\tPPG_STATE:..\tPPG_PP:..\tECG_BPM:..\tECG_STATE:..\tHR_BPM:..\tHR_SRC:..\tDROP:..`: 1초마다
  - `STATE` 값: 0=신호 없음(손가락 없음 / 전극 확인), 1=찾는 중, 2=측정 중

### 노트북 프로그램

| 파일 | 하는 일 |
| --- | --- |
| `biosignal_camera_live_bpm.py` | 라즈베리파이에 SSH로 접속해 카메라 영상과 Nano 신호를 받습니다. 실시간으로 화면에 보여 주고, 원할 때 녹화합니다. |
| `firmware/ppg_ecg_nano/ecg_ppg_live_view.py` | 라즈베리파이 없이 Nano를 노트북에 직접 꽂고 ECG·PPG 파형과 심박을 확인합니다. |

**`biosignal_camera_live_bpm.py` 화면 구성**
- 카메라 영상
- ECG 파형 (60/120Hz 노치 + 0.5~40Hz, 화면 표시용)
- PPG 맥박 파형 (0.7~4Hz, 화면 표시용)
- 심박: 융합 HR, 그 값의 출처, ECG·PPG 각각의 BPM과 상태
- 녹화 길이 선택(15·20·25·30초)과 **[● 녹화 시작]** 버튼 (R 키로도 시작·중지)

**녹화**
- 정한 시간이 지나면 자동으로 멈춥니다. 중간에 멈출 수도 있습니다.
- 영상은 각 프레임의 라즈베리파이 시각에 맞춰 30fps MP4로 저장합니다. 라즈베리파이 카메라는 실제로 초당 약 14장만 보내기 때문에, 이렇게 해야 **재생 속도가 실제와 같습니다.**
- 같은 녹화를 두 가지 영상으로 저장합니다.
  - `video.mp4`: 카메라 영상만
  - `video_overlay.mp4`: 카메라 아래에 ECG·PPG 파형과 심박을 그린 영상

라즈베리파이에서 실행되는 코드는 노트북 프로그램 안에 들어 있습니다(`REMOTE`). 프로그램을 켤 때마다
SSH로 보내 메모리에서만 실행하므로, 라즈베리파이에 따로 파일을 설치하거나 업데이트할 필요가 없습니다.

## 폴더 구조

```
biosignal_camera_live_bpm.py        카메라 + 생체신호 실시간 화면·녹화 (노트북에서 실행)
firmware/
  ppg_ecg_nano/                     현재 쓰는 Nano 펌웨어 (ECG + PPG + 심박 계산)
    ppg_ecg_nano.ino
    ecg_ppg_live_view.py            Nano 단독 확인용 화면
  health_sio_uno/                   이전 버전: 업체 SCP 바이너리 규격 ECG 전용 펌웨어와 뷰어
recordings/                         녹화 결과 (영상 파일은 git 에 올리지 않음)
```

## 하드웨어 연결

| 부품 | 연결 |
| --- | --- |
| AD8232 ECG 모듈 | OUTPUT → Nano **A0**, 3.3V, GND. 전극 3개(빨강·노랑·초록) |
| SEN-11574 Pulse Sensor | 보라(S) → Nano **A3**, 빨강 → 5V, 검정 → GND |
| Arduino Nano | 라즈베리파이 USB (Nano 단독 확인 때는 노트북 USB) |
| 카메라 | 라즈베리파이 USB (`/dev/video0`) |

- PPG 핀을 바꾸면 `ppg_ecg_nano.ino`의 `PPG_PIN`도 같이 바꿉니다.
- ECG 전극은 새 젤 패드를 쓰고, 피부를 알코올로 닦은 뒤 붙입니다. 접촉이 약하면 "전극 확인"으로 표시되고 BPM이 나오지 않습니다.
- PPG는 손가락을 누르지 말고 가볍게 대서 테이프로 고정합니다. 손이 차가우면 신호가 약해 측정이 끊길 수 있습니다. 이때는 손을 데우거나 귓불에 대세요.

## 실행에 꼭 필요한 준비

### Windows 노트북

1. **Python 설치**
   - 설치할 때 `Add Python to PATH`와 Python 런처(`py`) 옵션을 선택합니다.
   - 설치 후 VS Code 터미널을 다시 열고 확인합니다.

   ```powershell
   py --version
   ```

2. **필수 Python 패키지 설치**

   ```powershell
   py -m pip install paramiko pyserial matplotlib opencv-python numpy scipy
   ```

   | 패키지 | 용도 |
   | --- | --- |
   | `paramiko` | 라즈베리파이 SSH 연결 |
   | `pyserial` | Nano 직접 연결 (`--local`, `ecg_ppg_live_view.py`) |
   | `matplotlib` | 실시간 파형·BPM 화면 표시 |
   | `opencv-python` | 영상 처리 및 저장 |
   | `numpy` | 생체신호·영상 배열 처리 |
   | `scipy` | 화면 표시용 필터 |

   설치 확인:

   ```powershell
   py -c "import paramiko, serial, matplotlib, cv2, numpy, scipy; print('설치 완료')"
   ```

3. **SSH 최초 접속**

   ```powershell
   ssh ecall@ecall.local
   ```

   - 사용자: `ecall`
   - 비밀번호: `ecall202609`
   - 최초 접속 시 본인 라즈베리파이의 호스트 키인지 확인하고 `yes` 입력
   - 접속 후 `exit` 입력

### 라즈베리파이

라즈베리파이 터미널에서 최초 1회 실행합니다.

```bash
sudo apt update
sudo apt install -y python3 python3-opencv python3-serial
sudo systemctl enable --now ssh
sudo usermod -aG dialout,video ecall
sudo reboot
```

### Arduino Nano 펌웨어 업로드

1. Arduino IDE에서 `firmware/ppg_ecg_nano/ppg_ecg_nano.ino`를 엽니다.
2. 보드는 **Arduino Nano**, 프로세서는 **ATmega328P (Old Bootloader)**를 고릅니다. CH340 칩을 쓰는 호환 보드는 이 설정이어야 업로드됩니다.
3. 포트를 고르고 업로드합니다.
4. 시리얼 플로터로 PPG 파형을 보려면 스케치 맨 위의 `PLOT_MODE`를 `1`로 바꿔 올립니다. 라즈베리파이 연동에는 `0`이어야 합니다.

## 실행 방법

### 1. 카메라 + 생체신호 (라즈베리파이)

- 노트북과 라즈베리파이를 같은 네트워크에 연결합니다.
- 라즈베리파이에 Nano와 카메라를 연결합니다.
- Arduino 시리얼 모니터와 다른 카메라 프로그램은 종료합니다.

이 폴더의 **Windows 터미널**에서 실행합니다.

```powershell
cd multimodal-driver-monitoring-ecall
py .\biosignal_camera_live_bpm.py --host ecall.local
```

비밀번호 요청 시 `ecall202609`를 입력합니다. 창을 닫으면 종료됩니다.

| 옵션 | 기본값 | 설명 |
| --- | --- | --- |
| `--host` | `ecall.local` | 라즈베리파이 주소 |
| `--port` | `/dev/ttyUSB0` | 라즈베리파이에서 Nano 시리얼 포트 (`ls /dev/ttyUSB*`로 확인) |
| `--camera` | `/dev/video0` | 라즈베리파이 카메라 |
| `--width` `--height` `--fps` | 1280 720 30 | 카메라 요청 해상도·fps |
| `--record-seconds` | 15 | 녹화 길이 기본값 (15, 20, 25, 30). 화면에서도 바꿀 수 있음 |
| `--output` | `recordings/` | 녹화 저장 폴더 |
| `--local` | | 라즈베리파이 없이 이 PC에 연결한 Nano·카메라로 실행 |

### 2. 녹화

1. 화면 왼쪽 아래에서 **녹화 길이**(15·20·25·30초)를 고릅니다.
2. **[● 녹화 시작]** 버튼 또는 **R** 키를 누릅니다. 카메라 왼쪽 위에 `● REC 경과/길이`가 표시됩니다.
3. 정한 시간이 지나면 자동으로 멈춥니다. 중간에 멈추려면 같은 버튼(또는 R)을 다시 누릅니다.
4. 멈춘 뒤 몇 초 동안 영상을 만듭니다(화면에 "영상 저장 중…"). 이때 창을 닫아도 저장이 끝난 뒤 종료됩니다.

녹화마다 `recordings/REC_날짜_시간/` 폴더가 생깁니다.

| 파일 | 내용 |
| --- | --- |
| `video.mp4` | 카메라 영상만 (30fps, 실제 속도). MP4 인코더가 없는 PC면 `video.avi` |
| `video_overlay.mp4` | 카메라 + 아래쪽에 ECG·PPG 파형(최근 5초)과 Nano 심박(HR, ECG, PPG, 출처) |
| `frames.csv` | 프레임별 라즈베리파이 시각 (`clip_s` = 녹화 시작 기준 초) |
| `biosignals.csv` | ECG(raw, mV), PPG(raw) 250Hz |
| `vitals.csv` | 1초마다 Nano 심박: `ppg_bpm`, `ecg_bpm`, `hr_bpm`(융합), 상태, `hr_src` |
| `session.json` | 녹화 설정, 실제 길이, 개수, 완료/중지 여부, 카메라 실제 fps(`camera_measured_fps`) |

모든 CSV와 영상은 라즈베리파이 시각 기준이라 `clip_s`로 서로 맞춰 볼 수 있습니다.
MP4는 반복 프레임을 거의 공간 없이 저장해서, 15초 녹화가 영상 두 개 합쳐 약 17MB입니다.

### 3. 라즈베리파이 없이 시험

Nano와 카메라를 노트북에 직접 꽂고 실행합니다. 화면과 녹화 기능은 같습니다.

```powershell
py .\biosignal_camera_live_bpm.py --local --port COM6 --camera 0
```

### 4. Nano만 확인 (카메라 없이)

```powershell
cd firmware\ppg_ecg_nano
py .\ecg_ppg_live_view.py COM6
```

- ECG·PPG 파형, 융합 심박, ECG·PPG 각각의 BPM과 상태를 보여 줍니다.
- 기록은 같은 폴더에 `vitals_log_날짜_시간.csv`로 저장됩니다.
- COM 번호는 장치 관리자 → 포트(COM & LPT)의 **USB-SERIAL CH340**에서 확인합니다.

## 문제 해결

| 증상 | 확인할 것 |
| --- | --- |
| `could not open port` | Arduino 시리얼 모니터나 다른 측정 앱이 켜져 있지 않은지, COM 번호가 맞는지 |
| Nano 업로드가 멈춤 | 프로세서를 **ATmega328P (Old Bootloader)**로 |
| ECG "전극 확인" | 전극 패드 접촉, 케이블 연결, AD8232 3.3V 전원 |
| PPG가 자주 끊김 | 박동 진폭이 20 근처면 신호가 약한 것. 손가락 고정, 빛 가리기, 손 데우기 |
| 녹화 영상이 빨리 재생됨 | 예전 버전 프로그램. 창을 닫고 다시 실행 (폴더에 `video_overlay.mp4`가 생기면 새 버전) |

## 알려진 한계

- 손가락이 움직이거나 전극이 붙었다 떨어지는 잡음 구간에서는 짧게 잘못된 BPM이 나올 수 있습니다. 둘이 다를 때 ECG를 우선하는 융합 규칙 때문에, ECG 오측정이 융합 값에 그대로 나타나기도 합니다.
- PPG는 손이 차가우면 진폭이 작아져서 측정이 자주 끊깁니다.
- 시리얼 줄에 일련번호가 없어서, 라즈베리파이까지 가는 동안 데이터가 빠져도 알 수 없습니다. `DROP`은 Nano 안에서 생긴 손실만 셉니다.
- `ecg_mv`는 USB 전원을 5V로 가정합니다. 라즈베리파이 USB에서는 실제로 4.3~4.7V일 수 있습니다.
