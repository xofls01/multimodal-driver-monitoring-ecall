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
   py -m pip install -r requirements.txt
   ```

   | 패키지 | 용도 |
   | --- | --- |
   | `paramiko` | 라즈베리파이 SSH 연결 |
   | `matplotlib` | 실시간 파형·BPM 화면 표시 |
   | `opencv-python` | 영상 처리 및 저장 |
   | `numpy` | 생체신호·영상 배열 처리 |
   | `scipy` | ECG·PPG 필터링 및 BPM 계산 |
   | `torch` | ECG 등급 판정 모델 실행 |

   설치 확인:

   ```powershell
   py -c "import paramiko, matplotlib, cv2, numpy, scipy, torch; print('설치 완료')"
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
- Arduino에는 `firmware/ppg_ecg_nano/ppg_ecg_nano.ino`를 올립니다. 시리얼 포트는 `/dev/ttyUSB*`, `/dev/ttyACM*` 순서로 자동으로 찾습니다.
- Arduino 시리얼 모니터와 다른 카메라 프로그램은 종료합니다.

### 프로그램 실행

Python 파일이 있는 폴더의 **Windows 터미널**에서 실행합니다.

```powershell
py .\biosignal_camera_live_bpm.py --host ecall.local
```

비밀번호 요청 시 `ecall202609`를 입력합니다.

VS Code에서는 이 폴더를 연 뒤 터미널에 위 명령을 입력하거나, 실행 및 디버그(F5)에서 "생체신호 실시간 실행"을 고릅니다.

### 생체신호 판정

실행하면 첫 10초로 개인 기준 심박을 정한 뒤, 노트북에서 ECG·PPG를 1초마다 판정합니다. 화면 왼쪽 60%에 그래프, 오른쪽 40%에 정상·주의·위험과 이유를 표시하고, 근거를 측정 폴더의 `bio_status.jsonl`에 저장합니다. 라즈베리파이에는 추가 설치가 필요 없습니다. 자세한 내용은 [biosignal/README.md](biosignal/README.md)를 참고하세요.

### 통합 판단

생체신호, 비전, CAN(조향·가속·제동)을 합친 정상·주의·위험 기준과 그 이유는 [fusion/README.md](fusion/README.md)를 참고하세요.