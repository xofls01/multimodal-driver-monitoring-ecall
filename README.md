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
   py -m pip install paramiko matplotlib opencv-python numpy scipy
   ```

   | 패키지 | 용도 |
   | --- | --- |
   | `paramiko` | 라즈베리파이 SSH 연결 |
   | `matplotlib` | 실시간 파형·BPM 화면 표시 |
   | `opencv-python` | 영상 처리 및 저장 |
   | `numpy` | 생체신호·영상 배열 처리 |
   | `scipy` | PPG 필터링 및 BPM 계산 |

   설치 확인:

   ```powershell
   py -c "import paramiko, matplotlib, cv2, numpy, scipy; print('설치 완료')"
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

### 실행 전 확인

- 노트북과 라즈베리파이를 같은 네트워크에 연결합니다.
- 라즈베리파이에 Arduino와 카메라를 연결합니다.
- Arduino에는 ECG·PPG 데이터를 출력하는 펌웨어가 있어야 합니다.
- Arduino 시리얼 모니터와 다른 카메라 프로그램은 종료합니다.

### 프로그램 실행

Python 파일이 있는 폴더의 **Windows 터미널**에서 실행합니다.

```powershell
py .\biosignal_camera_live_bpm.py --host ecall.local
```

비밀번호 요청 시 `ecall202609`를 입력합니다.

연결되면 카메라와 ECG·PPG는 먼저 미리보기로 표시되며, 이때는 파일에 데이터가 기록되지 않습니다.

화면의 ECG에는 `0.5–40 Hz` band-pass와 `60 Hz` notch 필터가 적용됩니다. 분석 재현성을 위해 `biosignals.csv`에는 필터링하지 않은 원본 ADC 값이 저장됩니다.

1. 대시보드 아래의 `Start recording` 버튼을 누릅니다.
2. 화면에 표시되는 3초 카운트다운이 끝나면 CSV와 영상 기록이 시작됩니다.
3. 기록 시작 후 15초가 지나면 자동으로 기록이 종료되고 미리보기 상태로 돌아갑니다.
4. 15초 전이라도 `Stop recording` 버튼을 누르면 즉시 기록을 마무리할 수 있습니다. 카운트다운 중 누르면 기록 시작이 취소됩니다.
5. `Start next recording` 버튼을 누르면 다시 3초 후 다음 기록을 시작할 수 있습니다.
6. 프로그램을 끝내려면 대시보드 창을 닫고, 터미널에 `Recording closed`가 표시될 때까지 기다립니다.

각 기록은 같은 세션 폴더 아래에 덮어쓰기 없이 따로 저장됩니다.

```text
recordings/SESSION_날짜_시간/
├── session.json
├── TAKE_001/
│   ├── biosignals.csv
│   ├── frames.csv
│   ├── video_frames.csv
│   └── video.avi
└── TAKE_002/
    ├── biosignals.csv
    ├── frames.csv
    ├── video_frames.csv
    └── video.avi
```

`video.avi`는 30 FPS 형식을 유지하면서 실제 카메라 타임스탬프에 맞게 프레임을 반복하거나 생략하므로 배속 없이 재생됩니다. `frames.csv`에는 원본 수신 프레임 시간이, `video_frames.csv`에는 AVI의 각 프레임과 원본 프레임의 대응 관계가 저장됩니다.

> VS Code의 Python 확장은 터미널 명령으로 실행할 때 필수는 아닙니다.
