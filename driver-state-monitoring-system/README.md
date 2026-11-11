# Driver State Monitoring System

YOLO와 MediaPipe로 눈, 머리, 자세, 손과 물체 정보를 추출하고 지속 시간과 반복으로 Normal / Caution / Danger / Unavailable을 계산한다. 결과 영상과 판단 로그를 저장하고 --unity 옵션으로 현재 판단을 TCP로 전달한다.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-lock.txt
sh run.sh --input /path/to/video.mp4 --neutral-window 0.3 1.3 --output outputs/my_run
```

--neutral-window에는 사용자가 확인한 눈 뜸과 정면 자세 구간을 지정한다. 처음 3초 안에서 유효 표본 8개 이상이 필요하다. 실제 해당 구간을 읽은 뒤 기준을 적용한다. 준비된 프로필은 원본 파일 해시가 일치할 때만 사용할 수 있다.

기본 실행은 prototype 모드다. 눈 감김이 관측되면 경고 전에는 Normal에서 시간을 확인하고, 같은 큰 자세 변화가 경고 후 3초 더 지속되면 시험용 정차를 요청한다. 전체 판단이 문헌으로 검증된 것은 아니다. [현재 구현과 문헌 반영 범위](docs/EYE_CLOSURE_STATE.md)에 구분했다.

Unity 수신 설정과 C# 코드는 [integration/UNITY_INTEGRATION.md](integration/UNITY_INTEGRATION.md)에 있다. 실제 Unity Editor의 차량 물리 동작은 이번 Python 검증 범위에 포함하지 않았다.

원본 영상, 생성 영상과 로그, 가상환경은 저장소에 포함하지 않는다. 사전학습 가중치 두 개와 버전을 고정한 의존성 파일을 포함한다. 실행 결과는 outputs에 생성된다.
