# 현재 통합 실행 구조

기본 명령은 `sh run_multimodal.sh`다. `scripts/run_multimodal_system.py`가 하나의 Python 프로세스에서 원본 영상과 ECG/PPG CSV를 처리한다. 저장된 비전 판단 파일을 기본 입력으로 사용하지 않는다.

## 한 프레임을 처리하는 순서

1. 원본 영상에서 프레임과 영상 시각을 읽는다.
2. 설정된 분석 간격마다 YOLO와 MediaPipe를 실제 실행하고 비전 판단 알고리즘을 갱신한다.
3. 동일한 영상 시각까지의 ECG/PPG 원시 샘플만 원본 `BioSignalMonitor`에 순서대로 넣는다. 생체 판정은 원본 재생 프로그램과 같은 1초 주기로 갱신한다.
4. 현재 비전 상태와 생체 상태를 `multimodal_fusion.combine()`에 전달한다.
5. 그 통합 결과를 화면과 로그에 표시하고, Unity 전송이 켜져 있으면 `UnityStateServer.publish()`에 직접 전달한다.

이 흐름은 `src/driver_state_tools/multimodal_runtime.py`의 `MultimodalProcessor.update()`에 있다. 비전의 낮은 분석 FPS 사이에는 마지막 관측값을 짧게 유지하고, 새 관측으로 시간을 바꿔 꾸미지 않는다. 생체신호에도 미래 샘플을 미리 넣지 않는다.

## 각각 무엇을 사용하나

비전은 기존 `VisionModels`, `RevisionSession`, 원래의 prototype 설정을 사용한다. 개인 기준 보정, 눈 감김, 전방 이탈, 자세, 손 위치 등의 판단 조건은 유지한다. 영상마다 모델 추적 상태와 비전 판단 세션을 초기화한다.

생체신호는 전달받은 `biosignal` 코드를 수정하지 않고 사용한다. ECG는 `hr_classifier_ft.pt`, PPG는 `papagei_s.pt`와 `ppg_head_ft.pt`를 사용한다. 원본 알고리즘의 최종 `level`, `reasons`, `hr`을 임의로 재분류하지 않는다. 현재는 초기 준비 설정 3개만 변경한 5초 실험 프로필을 적용한다. 원본 10초 설정은 별도 프로필로 보존한다. 학습 모델의 10초 입력 길이는 변경하지 않는다. 화면 파형은 전달받은 `filtered_waves` 함수를 직접 호출한다.

화면에 보이는 초기 `Recorded` BPM은 함께 저장된 `vitals.csv`의 현재 시각 이전 값이다. 이것은 화면 표시용이다. 생체 알고리즘의 입력이나 통합 판단에 사용하지 않는다. 분석기의 현재 HR이 있으면 그 값을 우선한다.

비전과 생체의 최종 상태를 합치는 정책은 `multimodal_fusion.py`에 별도로 있다. 원본 생체 알고리즘과 이 통합 정책은 다른 층이다. 전달받은 전체 `fusion/FusionEngine`의 CAN과 버튼 응답 경로를 활성화했다는 뜻은 아니다. 현재 CAN과 응답 버튼은 연결되어 있지 않다. rPPG는 이 통합 판단에 사용하지 않는다.

## 입력과 시간

`config/multimodal/system_pairs.json`에 원본 영상, ECG/PPG 폴더, 오프셋, 시간 연결의 근거를 지정한다. 이 설정에는 비전 캐시 파일 경로가 없다.

현재 N은 센서와 함께 촬영한 정상 기록이다. D3, E4, A는 제공받은 합성 생체신호를 영상 시각에 배치한 시험 자료다. 이 세 쌍을 실제 응급 상태에서 동시에 측정한 자료라고 해석하지 않는다. N도 ADC와 카메라 노출 지연을 보정한 정밀 하드웨어 동기화는 아니다.

## 실행 방법

```bash
# 네 입력을 모두 새로 분석
sh run_multimodal.sh

# E4의 현재 분석 결과가 로컬 TCP 수신기에 도착하는지 시험
sh run_multimodal.sh --only E4 --verify-tcp

# 현재 분석 결과를 실제 Unity 클라이언트로 전송
sh run_multimodal.sh --only E4 --unity

# 처리 중인 화면도 함께 보기
sh run_multimodal.sh --only E4 --show
```

`--unity`는 기본적으로 `127.0.0.1:5001`에서 Unity 클라이언트 접속을 기다린다. 접속 후 현재 분석에서 나온 통합 결과를 전송한다. 다른 호스트나 포트는 `--unity-host`, `--unity-port`로 지정한다. 실제 Unity의 긴급 정차 잠금은 Unity의 명시적인 초기화 기능으로 해제한다.

파일 재생의 시각보다 빠르게 처리하지 않으려면 `--pace`를 사용할 수 있다. Unity 전송에서는 이 제한을 적용한다. 다만 연산이 느리면 원본보다 늦게 처리될 수 있다. 이 옵션이 처리 속도를 보장하지는 않는다.

이전 기능도 명시적으로 남겨뒀다.

```bash
# 저장된 비전 결과를 재사용하는 이전 미리보기 경로
sh run_multimodal.sh --cached-preview

# 저장된 통합 판단을 1배속으로 전송하는 시험용 재생
sh run_multimodal.sh --unity-replay outputs/multimodal_system/E4/fusion.jsonl --allow-synthetic-control
```

이 두 경로는 기본 실행과 구분한다. 저장된 통합 결과를 재생하면 전송 메타데이터에도 `saved_decision_replay`라고 표시한다.

## 결과와 Unity 입력

현재 실험 결과 페이지는 `outputs/multimodal_output_contract/index.html`이다. 원본 10초 실행 결과는 `outputs/multimodal_system`에 보존한다. 각 입력의 하위 폴더에는 다음 파일을 저장한다.

- `preview.mp4`: 원본 장면을 유지한 결과 영상
- `vision_frames.jsonl`: 이번 실행에서 모델로 계산한 비전 관측과 판단
- `bio_recomputed.jsonl`: 이번 실행에서 원본 생체 알고리즘으로 계산한 결과
- `fusion.jsonl`: 매 프레임의 비전 상태, 생체 상태, 통합 결과
- `unity_packets.jsonl`: Unity로 전달하는 메시지 형식
- `display_bpm.jsonl`: 화면에 표시한 BPM과 출처
- `summary.json`: 실제 추론 횟수, 판정 갱신 횟수, 처리 속도와 검증 범위

Unity에는 Normal, Caution, Danger와 함께 `fusion_available`, `vision_available`, `bio_available`, 각 원본 상태, 결과의 시각과 근거를 전달한다. 판단 불가를 일반 Caution으로 해석하지 않도록 availability 필드를 함께 읽어야 한다. 오래된 결과는 유효한 새 관측으로 처리하지 않는다.

## 이번 검증

N, D3, E4, A 원본 영상에서 비전 추출을 총 1,063번 실제 실행했고 생체 판정을 69번 갱신했다. 생체 결과 69개는 전달받은 ZIP의 원본 재생 프로그램을 별도로 실행한 결과와 모든 출력 필드가 같았다. 유효 프레임 2,002개와 패킷의 생체 등급도 원본과 같았다.

통합 결과 2,123개가 현재 처리 루프에서 `UnityStateServer.publish()`를 거쳐 실제 로컬 TCP 시험 수신기에 전달되는 것을 확인했다. 회귀 테스트 243개가 통과했다. Unity Editor를 실행한 시험은 아니다.

이 PC의 측정 속도는 영상 생성과 TCP 검증을 포함해 약 6.5~7.4 FPS였다. 결과 영상의 프레임 수와 재생 FPS는 원본대로 저장했지만, 생성 속도가 실시간 30 FPS라는 뜻은 아니다. 실시간 카메라와 센서 수신, 전체 시스템의 지연, Unity의 실제 경고와 정차 및 e-Call 동작은 별도 검증이 필요하다.

## 약 5초 시작 실험 프로필

사용자 요청으로 `config/multimodal/bio_startup_fast5.json`을 기본으로 선택한다. 모델과 원본 분석 코드 및 필터는 보존하고 다음 초기 준비 설정만 변경한다.

- `calibration_s`:10초에서5초
- `min_ready_s`:6초에서3초
- `calibration_min_readings`:5회에서3회

현재 네 시험 파일에서는 실제 기준값이 약 5.03~5.04초에 잡혔다. 신호가 나쁘거나 유효 측정이 부족하면5초가 지났다고 정상으로 강제 전환하지 않는다. 초기 자료가 줄어드는 실험 설정이며 검증된 의학적 기준이 아니다.

학습 모델은 여전히 10초 신호를 요구한다. 따라서5초부터 10초 사이의 판정에는 기존 맥박 기반 규칙을 먼저 사용하고, 학습 모델은 원래 조건을 채운 후 사용한다. 단순히 화면의 Measuring 문구만5초에 없애는 변경은 아니다. 시작 조건을 바꾸면 초기 기준값과 이후 판정 시각에도 영향이 있을 수 있다.

원본 10초 설정으로 돌아가려면 다음 명령을 실행한다.

```bash
sh run_multimodal.sh --bio-startup-profile config/multimodal/bio_startup_original.json
```

현재 실험값은 `biosignal_startup_used.json`과 `biosignal_config_used.json`에 저장한다. 전달받은 원본 분석 코드에도 동일한 시작 설정을 적용해 별도로 실행했으며, 분석 결과 69개가 모든 필드에서 일치했다. 이 비교는 원본 10초 기본값과 같다는 의미가 아니다.

원본 프로그램과 대조할 때도 동일한 프로필을 선택해야 한다. 기본은 현재 입력 설정의 프로필을 사용한다. 예를 들어 원본 10초 결과를 대조하려면 다음과 같이 지정한다.

```bash
python scripts/verify_biosignal_reference.py --archive <전달받은 ZIP> --results outputs/multimodal_system --bio-startup-profile config/multimodal/bio_startup_original.json --compare-only
```


## 원본 UI와 BPM 표시 차이 확인 (2026-11-12)

새로 받은 `recordings.zip`의 `E4/_overlay.py`를 실행하지 않고 동일 계산부를 재현하면 6.8초의 ECG 표시는 188.495 BPM이다. 기존 통합 화면의 74.5는 `BioSignalMonitor.hr`였으며, 그 시점에 모니터가 선택한 PPG 측정값이다. 두 숫자는 서로 다른 계산 경로의 결과다.

원본 UI는 최근 두 RR 간격의 평균으로 ECG 표시값을 만들고 ECG를 우선 표시한다. 모니터는 최대 12초 분석 창의 중앙 RR과 신호 품질, 학습 모델의 판단을 거쳐 사용할 채널을 선택한다. 원본 UI의 높은 숫자를 모니터의 최종 출력과 동일하다고 해석하면 안 된다.

현재 기본 실행은 `recent_pulse.py`에서 원본 UI의 250 Hz 기준 필터와 피크 설정, 최근 두 간격 공식을 사용한다. 다만 파일 전체를 먼저 필터링하는 원본 UI와 달리 현재 시각까지 도착한 표본만 사용한다. 필터 끝부분의 피크는 설정 가능한 0.08초 동안 확인하므로 값의 전환 시각은 원본 오프라인 영상과 조금 다를 수 있다. 이 간격은 표시 안정화를 위한 구현 설정이며 의학적 판정 기준이 아니다.

화면에는 `ECG estimate` 또는 `PPG estimate`를 표시하며, 해당 채널을 모니터가 신뢰하지 않는 시점에는 `Unverified estimate`도 표시한다. 이 숫자는 상태 판단에 사용하지 않는다. 채널별 값도 함께 표시하고, 최근 피크가 사라지면 과거 74 BPM을 계속 유지하지 않는다. 피크를 찾지 못했다는 이유만으로 심정지를 판정하거나 측정값을 0 BPM으로 만들지 않는다.

원본 `_overlay.py`는 파일 전체의 신호를 필터링하고 `session.json`에 기록된 합성 시나리오 시간을 읽어 일부 VF 라벨과 0 표시를 강제로 만든다. 이 부분은 모델 감지가 아니므로 통합 판단에는 연결하지 않았다. 원본 코드 사본은 `reference/supplied_overlay`에 비교용으로 보관했다.

`BioSignalMonitor`의 판정 코드, 모델 가중치와 필터는 유지했다. 앞서 요청한 약 5초 초기 측정 설정도 유지했다. 새 ZIP의 config와 monitor는 줄바꿈을 정규화하면 기존 수신본과 같다. 새로 포함된 `biosignal.offline.RecordingAssessor`의 자기 예열(앞 3초를 반복해 영상 시작 전 20초를 생성)은 이번 기본 실행에 추가하지 않았다. 따라서 첫 프레임부터 예열된 결과라고 주장하지 않는다.

Unity 패킷의 `bio_hr`는 기존 판단용 심박을 유지한다. `bio_display_hr`, `bio_display_hr_source`, `bio_display_hr_verified`, `bio_display_hr_used_for_grade=false`는 화면용 추정값을 구별한다. 오래된 패킷에는 표시값도 전달하지 않는다. 실제 Unity 차량 동작은 실행하지 않았다.


## recordings.zip 재실행과 숫자색

사용자가 다시 지정한 `recordings.zip`에서 `biosignals.csv`, `frames.csv`, `vitals.csv`, `session.json`을 네 영상별로 새로 추출했다. 입력은 `data/recordings_zip_20261112`에 원본 바이트 그대로 보관하고, ZIP 해시와 각 내부 경로 및 파일 해시를 `source_archive.json`에 기록했다. `config/multimodal/system_pairs.json`이 이 경로를 가리킨다. 새 실행 폴더는 `outputs/multimodal_recordings`다.

원본 `_overlay.py`의 BPM 숫자색은 즉시 구간 표시다. 51~100은 Normal 색, 41~50과 101~130은 Caution 색, 40 이하 또는 131 이상은 Danger 색을 사용한다. 이 구간은 전달받은 Config 값과 동일하다. 심박의 신뢰도나 지속 시간을 반영한 `BioSignalMonitor.level`과는 의미가 다르다.

통합 UI의 숫자색과 상단 `BIO STATE (BPM)`은 같은 구간을 사용한다. 원본 알고리즘의 최종 등급과 판단용 심박값은 로그와 Unity 출력에 별도로 보존한다. 예를 들어 193이라는 ECG 피크 추정값은 빨간색 Danger 구간이지만, 모니터가 신뢰한 PPG 74.5를 선택하고 경고의 지속 조건이 아직 충족되지 않았다면 최종 등급은 Normal일 수 있다. 이 화면용 구간만으로 최종 등급이나 Unity 정차를 강제로 바꾸지 않는다.

Unity의 `bio_display_bpm_range`는 숫자색에 대응하는 구간이다. 최종 판정은 기존 `bio_state`, `bio_available`, 통합 `driver_state`로 구분한다. `bio_display_hr_used_for_grade=false`와 신뢰 여부 필드를 유지한다. 출력 숫자의 색이 빨갛다는 것만으로 Unity가 정차하게 해서는 안 된다.


## 상단 BPM 상태 동기화

사용자 요청에 따라 상단은 `BIO STATE (BPM)`으로 표시하며, 하단 BPM 숫자와 동일한 `select_display_bpm` 및 `bpm_range` 결과를 사용한다. 193 BPM이면 위 상태도 빨간 DANGER이고, 작은 설명에도 같은 193 BPM과 ECG 출처를 표시한다. 이전의 Assessed HR 74 문구는 상단에서 제거했다.

초기 기준 측정 중에는 MEASURING을 유지한다. 측정이 끝난 뒤 사용할 BPM이 없으면 UNAVAILABLE을 표시한다. 숫자 없는 상태를 Normal로 바꾸거나 0 BPM으로 만들지 않는다.

이 상단 상태는 BPM 구간에 따른 화면 표시이며, 원본 모델의 최종 생체 등급이라고 이름 붙이지 않는다. 원본 `BioSignalMonitor.level`, 통합 판단, Unity 정차 조건과 패킷은 변경하지 않았다. 화면 표시를 제어 판단에도 사용하려면 신호 품질, 지속 조건과 함께 별도로 연결해야 하므로 이번에는 연결하지 않았다.

이번 영상은 이미 검증한 recordings.zip 실행 결과의 상단 영역만 다시 렌더링했다. 모델 추론과 Unity 실행을 새로 했다고 주장하지 않는다. 원래 분석 로그와 제어 패킷은 바이트 단위로 동일하게 보존했으며, 영상의 프레임 수, FPS와 카메라 영역을 유지했다. `scripts/render_bpm_header.py`가 이 표시 전용 재생성을 수행한다. 기본 `run_multimodal.sh`로 새 분석을 실행해도 같은 상단 표시 함수를 사용한다.


## 최종 출력 계약 추가

현재 기본 실행은 `MonitoringOutput`에서 비전의 Danger 시각과 행동 지표, 경고 후 시각적 복귀 및 별도 버튼 응답, 관측 품질을 수집한다. 결과는 `model_output.jsonl`과 `VehicleControlPacket.monitoring`에 동일한 형식으로 저장 및 전송한다. 상세 필드와 미준비 항목은 `MODEL_OUTPUT.md`를 따른다.

사용자가 현재 BPM 구간 등급 유지를 선택했으므로 생체신호 상단의 기준은 유지하되 MEASURING 표시와 화면 대기 바만 제거했다. 이전 절의 초기 MEASURING 표시 보존 설명은 현재 버전에 적용하지 않는다. 원래 생체신호 모듈의 기준선 계산과 입력 길이는 임의로 없애지 않았으며, 실제 값이 없으면 판단 불가다.

`head_lowering_angle_degrees`는 실제 각도 보정이 준비되지 않아 null이다. 얼굴 검출 확률과 최종 판단 확률도 임의로 만들지 않는다. 실제 얼굴 크기와 영상 명도, 대비 및 YOLO 검출과 키포인트 점수를 품질 정보로 제공한다.

반응 기록에는 경고 화면 표시 확인과 운전자 버튼 응답을 별도로 사용한다. 버튼 연결 전에는 명시적 응답과 취소 시도를 null로 표시한다. Unity C# 수신 자료형과 버튼 이벤트 전송 API는 작성했으나 Unity Editor 컴파일 및 실제 차량 동작을 실행하지 않았다.


## 사진과 MP4 파일 출력

`export_incident_media.py`로 현재 네 결과 영상에서 최초 비전 Caution 사진과 실제 Danger 사진, 첫 경고부터 녹화 끝까지의 MP4를 내보냈다. 전체 MP4는 원래 결과와 바이트가 동일하다. 구간 영상은 원래 프레임 수와 FPS를 기준으로 자르고 H.264로 저장했다. HTML은 필요하지 않다.

현재 네 영상에는 실제 Unity 정차 완료 응답이 없다. 정차 후 사진은 생성하지 않았고 구간 영상도 정차 완료로 표시하지 않는다. 새 실행에서는 현재 영상과 세션, 원본 시각에 대응하는 신선한 Unity Stopped 응답과 속도, 앞선 정차 요청을 확인해야 정차 사진을 저장한다.

Python 테스트 293개 통과. 실제 파일에서는 네 전체 영상의 해시, 구간 영상의 프레임 수와 FPS, 전체 화면 크기 1756x1034, 사진의 사건 시각과 ZIP 무결성을 확인했다. Unity는 Python TCP 모의 클라이언트로 메시지만 검사했다. 실제 Unity Editor 컴파일과 차량 정차는 실행하지 않았다.
