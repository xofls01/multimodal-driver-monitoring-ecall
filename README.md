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

> VS Code의 Python 확장은 터미널 명령으로 실행할 때 필수는 아닙니다.