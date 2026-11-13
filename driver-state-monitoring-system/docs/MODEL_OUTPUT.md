# 최종 출력 형식

기본 실행은 원본 영상과 `recordings.zip`에서 추출한 ECG/PPG를 함께 처리한다.

```bash
sh run_multimodal.sh
sh run_multimodal.sh --only E4 --unity
```

영상별 결과는 `outputs/multimodal_output_contract/<영상 ID>/`에 저장한다.

- `model_output.jsonl`: 프레임별 최종 출력. 한 줄이 한 시점의 JSON이다.
- `latest_output.json`: 마지막으로 분석한 프레임의 출력이다. 파일 재생 종료 뒤의 실시간 상태를 뜻하지 않는다.
- `example_output.json`: 비전 Danger가 관측된 첫 프레임을 우선 선택한 실제 출력 예시다. Danger가 없으면 마지막 프레임을 사용한다.
- `danger_events.jsonl`: 통합 판단이 Danger로 전환된 사건의 출력이다.
- `vision_danger_events.jsonl`: 비전 자체가 Danger로 전환된 사건의 출력이다.
- `unity_packets.jsonl`: 기존 제어 필드와 새 `monitoring` 객체를 포함한 실제 송신 형식이다.
- `preview.mp4`: 전체 결과 영상. HTML 없이 업로드할 수 있는 H.264 MP4다.
- `incident_output.json`: 실제 Caution과 Danger 장면, 관측 출력과 미디어 파일 경로를 묶은 최종 파일이다.
- `evidence/`: 해당 상태가 실제로 관측됐을 때 저장하는 JPG와 구간 MP4다.

형식 정의는 `schemas/monitoring_output.schema.json`에 있다. 상태 문자열은 `normal`, `caution`, `danger`, `unavailable`이다. 기존 Unity 호환 필드 `driver_state`는 `Normal`, `Caution`, `Danger`를 유지한다. 판단 불가에서는 호환용 Caution과 `vision_available=false`를 함께 확인해야 한다.

## Danger 시각

`danger`는 비전과 생체신호를 합친 최종 판단의 Danger 이력이다. `vision.danger`는 비전 규칙 자체의 Danger 이력이다. 둘은 다른 시각일 수 있다.

- `active`: 현재 Danger인지 여부.
- `detected`: 이 영상에서 Danger를 관측한 적이 있는지 여부.
- `source_seconds`: 최근 Danger로 전환된 영상 시각. 같은 Danger가 유지되는 동안 다시 쓰지 않는다.
- `first_source_seconds`: 이 영상에서 처음 Danger가 된 시각.
- `detected_at_utc`: 프로그램이 해당 전환을 처리한 UTC 시각.
- `recording_event_utc`: 원본 촬영의 실제 사건 시각. 현재는 정확한 연결 근거가 없어 `null`이다.

영상을 분석한 UTC 시각과 과거 촬영 시각을 혼동하지 않는다. 파일을 느리게 처리하더라도 영상 기준 시각은 원본 FPS를 따른다.

## 비전 지표

`vision.eyes_closed`와 `eyes_closed_seconds`는 현재 읽을 수 있는 눈 감김과 연속 감김 시간이다. 눈을 읽지 못하면 값은 `null`이며 `eyes_observed=false`다. 이를 눈을 뜬 상태나 감김 0초로 취급하지 않는다.

`head_lowered`와 `head_lowering_seconds`는 현재 모델이 사용하는 머리 위치 기준으로 따로 계산한다. 몸 기울기와 합쳐진 기존 collapse 타이머를 고개 숙임 시간으로 복사하지 않는다. 관측이 끊기면 시간도 미확인으로 처리한다.

`head_lowering_angle_degrees`는 출력 필드에 포함했지만 현재 값은 `null`, `head_angle_calibrated=false`다. 현재 모델의 `head_pitch_proxy`는 상대적 얼굴 랜드마크 지표이며 실제 3차원 각도가 아니다. `head_drop_ratio`도 어깨 폭에 대한 상대 위치다. 실제 각도가 필요하면 카메라 보정과 검증된 머리 자세 모델을 연결해야 한다.

`face_visible`은 얼굴 영역을 관측했는지, `face_readable`은 해당 특징을 사용할 수 있는지다. 얼굴이 작거나 눈이 가려져도 두 값의 의미를 구분한다.

`hands_on_wheel`과 `hands_on_wheel_count`는 설정된 핸들 영역과 손 또는 손목 위치의 겹침을 뜻한다. 사용한 방법은 `hands_on_wheel_method`에 표시한다. 물리적으로 핸들을 움켜쥐었는지는 확인할 수 없어 `physical_grip_confirmed=null`이다. 손을 관측하지 못하면 `hands_observed=false`와 `null`을 내보낸다.

## 운전자 반응

`driver_response`는 다음을 분리한다.

- `warning_active`, `warning_request_id`: 현재 경고 상태와 경고 사건 ID.
- `warning_display_confirmed`, `warning_displayed_source_seconds`: Unity가 실제 경고 화면을 그렸다고 응답한 경우만 기록한다. 경고를 요청하거나 결과 영상에 주의를 그린 것만으로 확인 처리하지 않는다.
- `visual_recovery`: 경고 상태 이후 비전이 정상 복귀를 관측한 시각. `after_confirmed_warning`으로 실제 경고 표시 확인 뒤의 관측인지 구분한다. 눈을 뜨고 자세를 회복한 관측을 의식 회복으로 단정하지 않는다.
- `cancel_attempted`: 명시적 입력의 취소 시도. 입력 미연결이면 `null`이다.
- `last_confirmed_response`: 실제 응답 이벤트를 수신한 UTC 시각, 적용한 영상 시각, 입력 종류와 사건 ID. 영상만으로 생성하지 않는다.

Unity의 경고 표시 확인 메시지 `driver_warning_ack`는 운전자가 누른 응답이 아니다. 명시적인 버튼 입력은 아래처럼 별도로 보낸다.

```json
{
  "type": "driver_response",
  "clip_id": "E4",
  "warning_request_id": "현재 패킷의 warning_request_id",
  "event_id": "버튼을 누를 때마다 만드는 고유 ID",
  "action": "cancel_attempt"
}
```

`action`은 `acknowledge` 또는 `cancel_attempt`다. 다른 영상이나 지나간 경고, 중복 ID, 오래된 판단에 대한 응답은 거부한다. 응답이 들어왔다고 현재 Danger나 Unity 정차 상태를 자동 해제하지 않는다. 이 변경은 반응을 기록하는 기능이다.

Unity 쪽 `UnityTcpVehicleBridge`에는 `ReportDriverAcknowledgement()`와 `ReportDriverCancelAttempt()`를 추가했다. 실제 버튼 OnClick에 연결한 다음 `driverResponseInputEnabled`를 켠다. 기본값은 꺼져 있으므로 현재 녹화 영상에는 명시적 버튼 반응이 `null`로 나온다. 경고 화면 표시 확인과 버튼 입력을 합치지 않는다.

## 관측 품질과 상태

`observation_quality.face`에는 얼굴 크기, 명도, 대비, 눈 사이 거리와 실제 YOLO 사람 검출 신뢰도 및 키포인트 신뢰도를 기록한다. Face Mesh가 제공하지 않는 얼굴 검출 확률이나 검증하지 않은 전체 판정 확률을 임의로 만들지 않는다. `judgment_confidence_score`와 `face_detection_confidence`는 현재 `null`이며 관련 확인 플래그도 false다.

`camera.frame_read_ok`는 해당 영상 프레임을 읽었는지 여부다. 녹화 파일이 정상적으로 읽힌다고 실제 카메라 하드웨어가 정상이라고 단정하지 않아 `camera_hardware_ok=null`이다.

`sensors`에는 원신호 시각, 자료의 경과 시간, ECG/PPG 채널의 준비 상태, 접촉 정보, SQI와 신뢰 가능 여부를 기록한다. CSV 재생이므로 센서 하드웨어 정상 여부는 `null`이다. 아직 계산하지 않은 SQI는 0으로 꾸미지 않고 `null`로 표시한다.

전송이 오래되면 `is_current=false`로 바꾸고 현재 행동값을 `null`로 처리한다. 과거 Danger 감지 시각과 버튼 응답 이력은 과거 기록으로 유지한다. 카메라 관측 자체의 나이도 전송 나이와 따로 확인한다.

## 생체신호 화면과 제어 출력

사용자가 선택한 기준에 따라 상단과 하단은 현재 BPM 구간 등급을 바로 표시한다. `MEASURING` 문구와 화면 대기 바는 제거했다. 실제 BPM이 없을 때는 판단 불가를 표시하며 정상값을 만들지 않는다. 생체신호 모듈의 입력 길이와 기준선 계산 자체를 없앤 것은 아니다.

`biosignal.bpm_state`는 이 화면용 구간이고, `biosignal.model_state`는 받은 생체신호 알고리즘의 최종 등급이다. Unity의 실제 동작은 기존 통합 `driver_state`와 신선도 필드를 따른다. 이번 작업은 이 제어 규칙을 임의로 변경하지 않았다.

`bpm_verified_by_monitor`는 같은 채널에서 실제 BPM이 측정됐고, 표시 추정값과 기존 Config의 심박 차이 허용값 이내로 일치하는 경우만 true다. 모델이 채널의 등급을 판정할 수 있다는 이유만으로 BPM 숫자까지 신뢰한다고 표시하지 않는다. 이 값은 표시 추정치의 일치 여부이며 의학적으로 보정된 판정 확률이 아니다.

Unity 수신 형식은 `VehicleControlPacket.monitoring`으로 제공한다. `MonitoringOutputPacket.cs`에 자료형을 추가했다. Unity `JsonUtility`는 null 숫자를 0으로 읽을 수 있으므로 반드시 `eyes_observed`, `head_angle_calibrated`, `hands_observed`, `detected`, `explicit_input_connected` 등의 확인 플래그를 먼저 본다.

BPM은 `bpm_state`가 unavailable이면 숫자를 사용하지 않고, 모듈의 판단용 심박은 `model_bpm_available`을 먼저 확인한다. null이 변환된 0을 실제 0 BPM이나 0도 측정으로 해석하지 않는다.

Python 소켓 수신과 테스트용 TCP 클라이언트로 메시지를 검증한다. Unity Editor에서의 컴파일, 버튼 연결, 실제 차량 정차와 e-Call 실행은 별도 검증이 필요하다.

## 업로드할 사진과 영상

기본 분석 실행이 완료되면 프레임별 출력과 같은 길이의 결과 영상으로부터 사진과 구간 영상을 내보낸다.
기존 분석 결과에서도 모델을 다시 실행하지 않고 내보낼 수 있다.

```bash
.venv/bin/python scripts/export_incident_media.py --input outputs/multimodal_output_contract --output outputs/share_media_new
```

프로젝트 안에 가상환경이 없으면 `../driver-state-monitoring-system/.venv/bin/python`을 사용할 수 있다.
이 명령은 새 공유 폴더와 같은 이름의 ZIP을 만든다. 원본 영상과 생체신호, 기존 분석 로그는 수정하지 않는다.
공유 폴더의 JSON에는 같은 폴더 안의 파일명으로 경로를 기록하므로 다른 컴퓨터에서도 함께 옮겨 쓸 수 있다.

`driver_incident_output.v1`의 `monitoring`에는 첫 비전 Danger 시점의 실제 출력이 들어간다. 비전 Danger가 없으면 첫 Caution, 둘 다 없으면 마지막 분석 프레임을 사용한다.
`latest_driver_response`는 구간 끝에서 확인한 반응 이력이다. 사진 선정 시각과 구분한다.

`media`의 항목은 다음과 같다.

- `full_video`: 전체 MP4.
- `caution_photo`: 비전이 처음 Caution인 실제 프레임.
- `danger_photo`: 비전이 처음 Danger인 실제 프레임.
- `after_stop_photo`: 현재 분석 세션에 대응하는 Unity 정차 완료 응답을 처음 확인한 프레임.
- `incident_video`: 첫 Caution 또는 먼저 발생한 Danger부터 확인된 정차까지의 구간. 정차 응답이 없으면 녹화 끝까지다.

해당 상태를 관측하지 못하면 사진 필드는 `null`이며 사진을 만들지 않는다.
현재 제공하는 네 영상에는 실제 Unity 정차 응답이 없으므로 `after_stop_photo=null`이고,
구간 파일은 `*_to_recording_end.mp4`다. 녹화 종료나 Danger 요청을 정차 완료로 처리하지 않는다.
`coverage`와 `includes_confirmed_stop`으로 이 차이를 확인한다. 사진은 비전 상태를 기준으로 고르며 통합 판단이나 BPM 등급을 대신 사용하지 않는다.

`vehicle_confirmation`은 새 실행에서 전송하는 선택 필드다. 기존 녹화 출력에 없으면 정차 미확인으로 처리한다.
Unity의 `vehicle_state`가 현재 `clip_id`, `session_id`와 대응하고, 명시적인 `source_timestamp`가 현재 영상 시간과 0.75초 이내로 대응해야 한다.
수신한 지 0.75초 이내이며 연결이 살아 있고, 앞서 정차 요청이 있었으며 `emergency_phase=Stopped`, `speed_kph`가 0~0.1이어야 정차를 확인한다.
0.1 kph는 기존 Unity 정차 상태 초기화 검사에 사용하던 허용값으로, 문헌에서 가져온 기준이 아니다.
시간 대응이 없는 구버전 응답과 다른 세션, 오래된 응답은 정차 증거로 사용하지 않는다.
실제 갓길 위치나 e-Call 완료 여부까지 검증하는 필드는 아니다.

현재 실제 고개 각도, 명시적인 버튼 반응, 보정된 전체 판정 확률과 장비 하드웨어 상태는 아직 준비되지 않았다.
필드가 있다는 사실과 실제 측정값이 준비됐다는 사실을 구분한다. 실시간 TCP에는 상태와 관측 정보가 전달되고,
미디어 파일 경로를 포함한 `incident_output.json`은 분석 영상 인코딩 후 만들어지는 파일 출력이다.
