# Driver State Monitoring System

YOLO와 MediaPipe로 운전자 영상의 행동 특징을 추출하고, 지속 시간·반복으로 상태를 판단하는 비전 프로토타입.

- 영상 목록·길이·FPS·해상도·읽기 검사와 시간 표시 대표 프레임
- 눈 감김·깜빡임·PERCLOS, 전방 이탈, 상체 자세, 손-가슴 근접, 휴대전화 후보
- 정상·주의·위험·판단 불가와 근거 로그, 결과 MP4·HTML 모음
- 정상 N 영상에서만 이마·볼 rPPG 추출 실험

현재 사용한 사전학습 YOLO 가중치·설정·테스트를 포함한다. 20개 영상으로 추가 학습한 모델은 아니다.
영상만으로 의식·심정지를 판단하지 않으며, 위험 전환에는 별도 경고·버튼 무응답 입력이 필요하다. 센서·Unity 연동은 포함하지 않는다.

## 실행

프로젝트 폴더에서 Python 3.11로 실행한다. 설치에는 인터넷이 필요하다. 검증 환경은 macOS이며 다른 운영체제는 아직 확인하지 않았다.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-lock.txt
python scripts/run_project.py --check
python scripts/run_project.py --input "/path/to/videos" --prepare-frames
```

완료 후 표시되는 `outputs/experiment_날짜_시간/index.html`을 열면 된다. MP4·로그·무라벨 이미지도 같은 실험 폴더에 저장된다. 원본은 수정하지 않는다.
`--interval 3`은 대표 프레임 간격, `--annotation-interval 1`은 학습 준비 이미지 간격이다. 눈이 가려진 자료에는 `--eyes-occluded`를 사용한다.
설정은 `config/`, 테스트는 `python -m unittest discover -s tests -v`.

촬영 영상·기존 결과·가상환경은 로컬에 별도 보관한다. 같은 영상으로 재실행할 때는 그 영상 폴더를 입력한다. 운영체제·장치에 따른 차이가 있어 결과의 비트 단위 동일성은 보장하지 않는다.
