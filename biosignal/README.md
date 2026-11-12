# 생체신호 판단 알고리즘 (ECG·PPG)

ECG·PPG 파형에서 심박수(bpm)를 구하고, 첫 10초의 개인 기준선과 비교해 정상·주의·위험·측정 불가로 판정한다.
연구용 프로토타입이며 의료 진단용이 아니다. 임계값은 `biosignal/config.py`에 있다.

## 동작 구조

| 단계 | ECG | PPG |
|---|---|---|
| 전처리 | 60 Hz 노치 + 대역통과 + 이동평균 | 0.5–4 Hz 대역통과 + 이동평균 |
| bpm | R 피크 간격의 중앙값 | 맥박 피크 간격의 중앙값 |
| 등급 | 직접 학습한 1D-CNN (`models/hr_classifier.pt`) | 사전학습 PaPaGei-S + 직접 학습한 판정부 (`models/papagei_s.pt`, `models/ppg_head.pt`) |

- **bpm은 피크 간격으로 직접 잰 값만 표시한다.** 파형 품질이 낮으면 값을 내지 않고 `--`로 표시한다.
- 등급은 ECG 모델 → PPG 모델 → 측정한 심박수의 임계값 순으로 낸다. 모델 신뢰도가 0.7 미만이면 다음 순서로 넘어간다. `hr_class_source`에 `ecg_model`, `ppg_model`, `rule` 중 무엇을 썼는지 남긴다.
- 직접 잰 bpm이 40 이하 또는 150 이상이면 모델 판정과 상관없이 위험으로 본다.
- PPG 모델이 측정 불가로 판단하면 PPG bpm도 쓰지 않는다.
- ECG가 포화되면(샘플의 5% 이상이 0 근처 또는 약 742 이상) 전극 탈락으로 본다. PPG는 0 근처 또는 약 890 이상이 기준이다. 실측 기록으로 정한 값이라 공급 전압이나 장비가 바뀌면 다시 확인해야 한다.

## 판정 기준과 근거

판단 기준은 `생체신호 판단 기준.pptx`를 따른다.

| 등급 | 기준 | 화면 문구 | 근거 |
|---|---|---|---|
| 위험 | ECG·PPG 모두 박동 소실 8초 이상 | `NO PULSE` | 맥박 확인 상한 10초[4] 안에 들도록 정함 |
| 위험 | ECG 모델이 깨끗한 ECG를 이상 리듬으로 판정 + PPG 맥박 소실 | `NO PULSE + ABNORMAL ECG` | 학습 라벨 |
| 위험 | 40 bpm 이하 또는 131 bpm 이상을 두 센서가 5초 이상 확인 | `VERY LOW BPM`, `VERY HIGH BPM` | 제안서 요구값, 국내 심폐소생술 가이드라인 인용 연구, 조기 경고 점수 3점 구간(≥131) |
| 주의 | 41–50 bpm 10초 지속 | `LOW BPM` | 서맥 정의 50 미만[1], 조기 경고 점수 1점 구간 |
| 주의 | 101–130 bpm 10초 지속 (한 센서만 잰 131–149 포함) | `HIGH BPM` | 정상 범위 50–100[2], 조기 경고 점수 1–2점 구간 |
| 주의 | 기준선 대비 ±25 bpm 또는 ±30% 중 큰 값 이상, 7초 지속 | `BPM RISING`, `BPM FALLING` | 설계값. 공개 임계값을 찾지 못함 |
| 주의 | ECG는 정상인데 PPG 맥박만 소실, 두 센서 모두 박동이 3초 넘게 끊김 | `NO PULSE? (1 SENSOR)`, `HEARTBEAT PAUSE` | 설계값. 비전과 합쳐 판단 |
| 주의 | 모델 주의 판정 10초, RR 변동계수 0.2 초과 20초 | `ABNORMAL RHYTHM`, `IRREGULAR RHYTHM` | 정상 조건 "박동이 규칙적" |
| 정상 | 51–100 bpm, 기준선과 변화가 작고 박동이 규칙적 | 이유 없음 | |
| 판단 불가 | 기준선 측정 중(처음 10초) | `MEASURING BASELINE` | |
| 판단 불가 | 두 센서 포화(최근 12초의 5% 넘게 ECG 8 이하·742 이상, PPG 8 이하·890 이상) 또는 1.5초 넘게 수신 끊김 | `SENSOR OFF` | 실측 기록 |
| 판단 불가 | 기준선 측정 뒤에도 두 센서 신호 품질 0.5 미만 | `POOR SIGNAL` | |
| 판단 불가 | 두 센서 모두 깨끗한데 심박수가 10 bpm 넘게 다름 | `ECG/PPG BPM MISMATCH` | Nano 펌웨어의 일치 기준 |
| 판단 불가 | 한 센서만 위험 범위(40 이하·150 이상) 또는 박동 소실, 다른 센서는 잡음 | `... (1 SENSOR)` | 위험은 두 센서 확인이 필요. 150은 빈맥성 부정맥 기준[3] |
| 판단 불가 | ECG에 리듬 없는 잡음만 있고 PPG 맥박 소실 | `NOISY ECG + NO PULSE` | 심실세동은 ECG 모델이 따로 판정 |

- 주의 상태에서 신호를 믿을 수 없게 되면 바로 판단 불가로 바꾼다. 위험이었다면 위험을 유지한다.
- 판단 불가 동안은 영상·CAN 데이터로 판단한다(`needs_second_opinion=True`).

- [1] Kusumoto et al., [2018 ACC/AHA/HRS Bradycardia Guideline](https://doi.org/10.1161/CIR.0000000000000627), Circulation 2019: 동서맥 50 bpm 미만, 동정지 3초 초과. 원문 확인.
- [2] [ACC 보도자료](https://www.acc.org/about-acc/press-releases/2018/11/06/13/31/societies-publish-new-guidance-for-the-treatment-of-slow-irregular-heartbeats): 정상 심박 50–100 bpm. 원문 확인.
- [3] AHA ACLS 성인 빈맥 알고리즘: 빈맥성 부정맥은 대개 150/min 이상. [교육 사이트의 사본](https://acls-algorithms.com/wp-content/uploads/2021/02/Website-Tachycardia-Algorithm-Diagram.pdf)으로 확인. AHA 공식 페이지는 접근하지 못했다.
- [4] AHA BLS의 맥박 확인 10초 이내. 2차 요약 자료로만 확인했다.
- [5] Shah et al., [Automated loss of pulse detection on a consumer smartwatch](https://doi.org/10.1038/s41586-025-08810-9), Nature 2025: 착용 확인을 통과해야 맥박 소실을 판정하고, 판정 뒤 사용자 응답을 확인한 다음 신고한다. 맥박 소실 시뮬레이션 민감도 67.23%, 약 57초 소요. 접촉 확인과 단계적 경보 구조를 참고했다. 수치는 스마트워치 결과이며 우리 장비의 성능이 아니다.
- [6] Mehler, Reimer & Coughlin, Human Factors 54(3), 396–412, 2012: 주행 중 인지 부하에 따라 심박수가 오른다. 심박 상승만으로 위험을 판정하지 않는 이유다. 초록만 확인했고 bpm 수치는 쓰지 않았다.

이 임계값은 일반 성인의 정의이고, 운전 중 응급 판단 기준으로 검증된 값은 아니다. 가이드라인도 증상 없는 동서맥은 나쁜 결과와 관련이 없다고 본다.

### 개인 기준선

- 첫 10초의 신뢰 측정값 중앙값으로 정하고, 이후 고정한다. 측정 중에는 "기준 심박 측정 중"으로 표시하고 정상이라고 하지 않는다. 위험 근거가 나오면 측정 중에도 바로 표시한다.
- 40 bpm 이하, 100 bpm 초과, 위험 소견이 있으면 기준선으로 쓰지 않고 안정될 때까지 다시 잰다. 그동안은 절대 임계값으로 판정한다.
- 기준선이 50 미만인 사람은 `기준선 - 10`(최소 40) 미만부터 `LOW BPM`으로 본다. 기준선 근처의 규칙적인 심박이면 모델의 주의 판정도 쓰지 않는다.
- 기준선은 다시 잡지 않으므로, 시동 직전 활동으로 높게 잡히거나 장시간 주행으로 심박이 서서히 변하면 `BPM FALLING`이 날 수 있다.

### 급성 심정지 흐름

합성 신호에서 정상 → 박동 소실 후 약 6초에 주의(`HEARTBEAT PAUSE`) → 약 11초에 위험(`NO PULSE`)으로 올라간다. VF 파형은 약 8초에 위험이 된다.
ECG는 그대로인데 맥박만 사라지면(무맥성 전기활동) 손가락이 떨어졌을 수도 있으므로 주의에 머물고 `needs_second_opinion=True`를 낸다. 비전 결과와 합칠 때 최종 판단해야 한다.

현재 장비에는 접촉 신호가 없다. 맥박을 잡은 적이 있고 PPG가 포화되지 않았으면 접촉이 유지된 것으로 본다. 실측에서 손가락을 떼면 PPG가 약 966에 포화됐기 때문이다. 다만 손가락을 댄 채 맥박이 없는 실측은 없어서, 이 가정은 검증되지 않았다.

**`hr_class`와 `level`은 다르다.**
- `hr_class`: 심박 등급 (모델 또는 임계값 판정)
- `level`: 교차 확인을 마친 최종 등급

`level`은 ECG와 PPG의 근거가 일치해야 `danger`가 된다. 근거가 하나면 `caution`과
`needs_second_opinion=True`를 내서 이후 비전과 합칠 때 판단하게 한다.

## 실시간 실행

```bash
py .\biosignal_camera_live_bpm.py --host ecall.local
```

노트북에서 판정해 화면에 표시하고, 측정 폴더의 `bio_status.jsonl`에 1초마다 근거를 저장한다.
화면은 왼쪽 60%에 ECG·PPG·심박수 그래프, 오른쪽 40%에 등급, 이유, 현재 bpm, 기준선 대비 변화를 보여준다. 카메라는 화면에 표시하지 않지만 `video.avi`로 계속 저장한다.

장비 없이 화면을 볼 때:

```bash
python tools/demo_dashboard.py cardiac_arrest
python tools/demo_dashboard.py tachycardia_125 --speed 3
```

다른 실시간 프로그램에 붙일 때는 `LiveAssessor`를 쓴다. 수신과 화면을 막지 않도록 판정은 별도 스레드에서 돈다.

```python
from biosignal.live import LiveAssessor

assessor = LiveAssessor(model_path="models/hr_classifier.pt", log_path="bio_status.jsonl",
                        ppg_encoder_path="models/papagei_s.pt", ppg_head_path="models/ppg_head.pt")
assessor.start()
assessor.push(t, ecg_raw, ppg_raw)  # 샘플을 받을 때마다. t는 라즈베리파이 시각(초)
status = assessor.latest()          # 화면을 그릴 때. 아직 결과가 없으면 None
assessor.stop()                     # 종료할 때
```

- 처음 10초는 기준선 측정이다. 분석은 6초부터, 모델은 신호가 10초 쌓인 뒤부터 동작한다.
- 위험 판정 뒤 센서가 떨어져도 경보는 유지한다. 신뢰할 수 있는 정상 신호를 확인해야 풀린다.

## 측정 파일로 확인

```bash
python tools/replay_recording.py "<측정 폴더>" --model models/hr_classifier.pt --ppg-encoder models/papagei_s.pt --ppg-head models/ppg_head.pt --expect normal --out bio_status.jsonl
```

`REC_`와 `SESSION_` 폴더를 모두 읽고, 측정 폴더는 수정하지 않는다.

### 녹화 파일을 비전과 합칠 때 (예열)

| | 실시간 | 녹화 파일 |
|---|---|---|
| 기준선 재료 | 들어오는 신호의 처음 10초 | 파일 앞 3초 |
| 방법 | 실제로 10초 기다림 | 앞 3초를 박동 경계에서 잘라 20초로 이어 붙여 영상 시작 전에 먼저 넣음 |
| 판정 시작 | 약 10초 | 영상 1초 |

녹화 파일만 넣으면 처음 10초를 기준선 측정에 쓰고, 분석 창(최소 6초, 모델 10초)도 비어 있어 15초 안팎의 시연 영상에서는 판정이 너무 늦다.
실제 차에서는 사건 전부터 측정하고 있으므로, 영상 시작 시점에 기준선과 분석 창이 차 있는 상태를 재현한다. 판정 알고리즘과 10초 측정 규칙은 바꾸지 않는다.

**비전과 합칠 때는 미리 만든 결과 파일 대신 모델을 영상과 함께 돌린다.** 비전이 프레임을 처리할 때마다 그 프레임 시각을 넣으면, 그 시각까지의 신호만으로 판정한 가장 최근 결과가 나온다.

```python
from biosignal.offline import RecordingAssessor

bio = RecordingAssessor("recordings/E4")        # 기본 가중치(미세조정 버전), 파일 앞 3초 예열
for frame_index, clip_s in frames:               # frames.csv 의 영상 시각(초)
    vision = vision_engine.update(frame)
    status = bio.at(clip_s)                      # 첫 판정(약 1초) 전에는 None
    bio_level = status.level if status else "unavailable"
    bio_reasons = status.reasons if status else []
bio.close()
```

- `status.level`: `normal`·`caution`·`danger`·`unavailable`. `status.reasons`: 판정 이유. `status.needs_second_opinion`이 `True`면 생체신호만으로는 확실하지 않으니 비전 판단을 따른다.
- 판정은 1초마다 새로 나오고, 그 사이 프레임에는 직전 판정이 그대로 나온다. 그 시각 뒤의 신호는 쓰지 않는다.
- 처음 만들 때 가중치를 읽느라 몇 초 걸린다. 그 뒤 프레임당 최대 약 25 ms (E4·D3·TheNewA·N 측정).
- 시각은 앞으로만 간다. 영상을 처음부터 다시 돌리려면 새로 만든다.

결과를 파일로 확인할 때:

```bash
python tools/replay_recording.py recordings/E4 --model models/hr_classifier_ft.pt --ppg-encoder models/papagei_s.pt --ppg-head models/ppg_head_ft.pt --out bio_status.jsonl
```

- 기본이 파일 앞 3초 예열(`--warmup self`)이다. `--warmup none`은 실시간과 같이 처음 10초를 측정하고, `--warmup <폴더>`는 그 정상 측정 전체로 예열한다.
- 파일 시각(`clip_s`, 영상 첫 프레임 = 0초)부터의 판정만 기록한다. 비전과 같은 시각끼리 합치면 된다.
- **파일 전체로 예열하면 안 된다.** 끝에 있는 사건이 예열에 들어가 E4는 1초부터 위험이 나왔다.
- 앞 3초가 정상 상태라고 가정한다. 비전의 중립 구간 기준선과 같은 가정이다. 앞 3초에서 박동을 3개 이상 찾지 못하면 예열 없이 판정한다.
- 시작 공백만 없앤다. 사건 뒤 판정까지의 지연(위험 5초 확인, 맥박 소실 8초, 12초 분석 창의 중앙값)은 알고리즘의 실제 반응 시간이라 그대로 남는다.
- 이어 붙인 예열 신호는 박동 간격 변동이 실제보다 작다. RMSSD 기준값(졸음 참고 지표)은 예열 값에서 시작한다.

## 주요 사유 코드

| 코드 | 의미 |
|---|---|
| `NO_PULSE_CONFIRMED` | ECG와 PPG 모두 박동 소실 (`danger`) |
| `ABNORMAL_ECG_NO_PULSE` | ECG 모델 이상 판정 + PPG 맥박 소실 (`danger`) |
| `CHAOTIC_ECG_NO_PULSE` | 리듬 없는 ECG 잡음 + PPG 맥박 소실 (`unavailable`) |
| `MODEL_DANGER`, `EXTREME_BRADY`, `EXTREME_TACHY` | 위험 등급 심박을 두 센서가 확인 (`danger`) |
| `*_UNCONFIRMED` | 한 센서만 위험 범위, 다른 센서는 잡음 (`unavailable`) |
| `NO_PULSE_UNCONFIRMED` | ECG 정상 + PPG 맥박만 소실이면 `caution`, 다른 센서가 잡음이면 `unavailable` |
| `SENSOR_MISMATCH` | 두 센서 심박수 10 bpm 넘게 차이 (`unavailable`) |
| `SENSOR_CONFLICT` | 한 센서만 위험 (`caution`) |
| `HEART_PAUSE` | 두 센서 모두에서 박동이 끊기고 신호가 평탄, 위험 확정 전 단계 (`caution`) |
| `HR_HIGH`, `HR_LOW`, `MODEL_CAUTION` | 주의 등급 심박 지속 (`caution`) |
| `HR_RISING`, `HR_FALLING` | 개인 기준선 대비 상승·하강 (`caution`) |
| `IRREGULAR_RHYTHM` | 불규칙 리듬 지속 (`caution`) |
| `SENSOR_LOST`, `LOW_SIGNAL_QUALITY`, `CALIBRATING` | 측정 불가 (`unavailable`) |
| `HOLD_PREVIOUS_LEVEL` | 경고 해제 대기(10초), `danger`는 정상 신호 확인 전까지 유지. 주의 중 판단 불가가 30초 안에 끝나면 주의에서 다시 시작해 해제 대기를 거친다 |
| `DROWSY_HINT` | 졸음 참고 지표, 등급에 영향 없음 |

## PPG 사전학습 모델

- 모델: [PaPaGei-S](https://github.com/Nokia-Bell-Labs/papagei-foundation-model), 코드 BSD-3-Clause-Clear (`third_party/PAPAGEI_LICENSE`)
- 가중치: [Zenodo 13983110](https://zenodo.org/records/13983110)의 `papagei_s.pt`. 페이지에 라이선스가 따로 적혀 있지 않아 상용화 전에는 확인이 필요하다.
- 전처리: 공개 예제의 pyPPG 필터를 scipy로 옮겼으므로 원본과 완전히 같다고 보장하지 않는다.
- 판정부 학습: 인코더는 고정하고, [BIDMC](https://physionet.org/content/bidmc/1.0.0/) 53개 기록(ODC-BY)으로 환자 단위로 나눠 학습했다.

## 모델 재학습과 평가

```bash
pip install -r requirements.txt
python tools/build_dataset.py --out <데이터 폴더>
python tools/train_hr_model.py --data <데이터 폴더> --out models/hr_classifier.pt --epochs 20
python tools/eval_hr_model.py --data <데이터 폴더> --model models/hr_classifier.pt

python tools/build_ppg_dataset.py --out <PPG 데이터 폴더> --ecg-data <데이터 폴더>
python tools/train_ppg_head.py --data <PPG 데이터 폴더> --encoder models/papagei_s.pt --out models/ppg_head.pt
```

학습 데이터는 PhysioNet MIT-BIH Arrhythmia, Normal Sinus Rhythm, Noise Stress Test, VFDB, CUDB이며
환자 단위로 학습·검증·테스트를 나눈다. 서맥·빈맥 일부는 시간축을 변환해 만들었다.

## 테스트

```bash
python -m unittest discover -s tests
```

환경변수 `BIOSIGNAL_MODEL`, `BIOSIGNAL_PPG_ENCODER`, `BIOSIGNAL_PPG_HEAD`에 가중치 경로를 주면 모델 경로로 검사한다.
`tools/run_scenarios.py`는 합성 시나리오별 판정과 지연 시간을 표로 출력한다.

## 평가 결과와 한계

학습에 쓰지 않은 35개 기록의 실제 심박 창 기준(모델 단독, 창 하나 단위):

| 항목 | 값 |
|---|---|
| 등급 정확도 평균 | 86.8% (기록별 20.5–100%) |
| 정상 심전도 기록 4개 | 99.9% |
| 부정맥 기록 21개 평균 | 82.1% (최저 20.5%) |
| 신뢰도 0.7 기준 위험 재현율 / 정밀도 | 0.845 / 0.986 |
| 신뢰도 0.7 기준 정상을 주의 / 위험으로 본 비율 | 9.7% / 0.13% |

피크 간격 bpm은 같은 데이터에서 평균 오차 1.06 bpm이고, 측정값을 낸 비율은 약 86%다.

PPG는 BIDMC 테스트 10개 기록 기준(시간축 변환 포함, 모델 단독, 창 하나 단위):

| 항목 | PaPaGei + 판정부 | 피크 + 임계값 |
|---|---|---|
| 전체 정확도 | 92.5% | 86.5% |
| 위험 재현율 / 정밀도 | 0.860 / 0.923 | 0.564 / 0.981 |
| 정상을 위험 / 주의로 본 비율 | 0.32% / 3.67% | 0.00% / 0.21% |

테스트 기록의 실제 심박은 모두 정상 범위라, PPG의 주의·위험 성능은 시간축 변환으로 만든 파형으로만 평가했다.
PPG가 망가진 창은 라벨을 믿을 수 없어 데이터에서 뺐으므로 실제 사용보다 쉬운 평가일 수 있다.

실측 재생 결과:

| 기록 | 길이 | 결과 |
|---|---|---|
| 정상 상태 `REC_` | 20초 | 정상 14회, 불일치 0회 |
| 전극 탈락 파형이 많은 `SESSION_` 2개 | 734초 | 위험·주의·정상 0회, 전부 측정 불가 |

- 실제 PPG에서 PPG 모델의 신뢰도는 0.39–0.99로 들쭉날쭉해, 핸들 파지를 가정한 재생에서는 등급 대부분이 임계값 규칙으로 나왔다.
- 공개 데이터 대부분이 병원 기록이라 주행 중 성능을 뜻하지 않는다.
- 실측 기록은 입력 대역, 포화 기준, 신뢰도 기준을 정할 때 참고했으므로 독립 검증이 아니다.
- 우리 센서로 여러 사람을 측정한 검증, 의료용 심박계와의 비교(오차 5 bpm 요구), SNR 10 dB 요구 검증은 아직 하지 않았다.
- 실제 심정지와 실제 PPG 맥박 소실은 합성 신호로만 확인했다.
- 실제 라즈베리파이 연결로는 실행해 보지 않았다. 측정 파일을 흘려보내는 가짜 SSH로 전체 흐름만 확인했다.

## ECGFounder와 비교

사전학습 모델 [ECGFounder](https://github.com/PKUDigitalHealth/ECGFounder)(MIT, 단일 리드)를 같은 테스트 기록 35개, 창 2,618개로 비교한 뒤 현재 모델을 유지하기로 했다. 판정층은 현재 모델과 같은 학습 기록으로 새로 학습했다.

| 항목 | 현재 모델 (`hr_classifier_ft`) | 라벨 직접 변환 | 특징 + 판정층 |
|---|---|---|---|
| 3등급 정확도 | 87.1% | 73.2% | 90.7% |
| 위험 재현율 / 정밀도 | 99.2% / 99.4% | 92.3% / 77.6% | 95.3% / 94.2% |
| 정상을 위험으로 본 비율 | 0% | 2.09% | 0.47% |
| CPU 1스레드 처리 시간 | 11.6 ms | 약 198 ms | 약 198 ms |

판정층의 정확도 우위는 환자 단위 95% 신뢰구간이 -1.0~+9.5%로 확실하지 않고, `mitdb_231`의 위험 창 22개를 모두 놓쳤다. 라벨에는 측정 불가와 심실세동이 없다. 현재 모델은 같은 테스트셋을 이전 평가에도 썼으므로 수치가 약간 유리할 수 있다.

## 측정 기록으로 미세조정

실시간 코드의 기본 모델은 미세조정 버전(`models/hr_classifier_ft.pt`, `models/ppg_head_ft.pt`)이다. 기존 모델(`hr_classifier.pt`, `ppg_head.pt`)도 남겨 두었다.

```bash
python tools/finetune_on_recording.py "<측정 폴더>" --ecg-data <ECG 데이터> --ppg-data <PPG 데이터> --out-dir models
```

특징 추출 부분은 고정하고, 공개 데이터와 측정 파형을 섞어 마지막 판정층만 다시 학습한다. 주의·위험 예시는 측정 파형의 시간축을 변환해 만들었다.

| 공개 테스트셋 | ECG 기존 | ECG 조정 | PPG 기존 | PPG 조정 |
|---|---|---|---|---|
| 전체 정확도 | 88.5% | 89.0% | 92.5% | 92.4% |
| 위험 재현율 | 0.904 | 0.952 | 0.860 | 0.875 |
| 주의 재현율 | 0.882 | 0.852 | 0.908 | 0.912 |
| 정상을 위험으로 본 비율 | 0.17% | 0.24% | 0.32% | 0.41% |

학습 기록은 한 사람의 정상 상태 20초뿐이다. 앞 12초로 학습해 뒤 10초를 확인했을 때 PPG의 `normal` 확률은 0.14에서 0.94로 올랐지만, 이 확인은 창 1개이고 2초가 겹친다. 최종 모델은 20초 전체로 학습했으므로 이 기록의 재생 결과는 성능 근거가 아니다. 다른 사람과 다른 측정 환경에서의 효과는 확인하지 못했다.
