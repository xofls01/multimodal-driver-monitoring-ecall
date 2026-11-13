# Unity 연결 - 문헌 기준 개정본

이 복사본의 기본 모드는 literature다. 모든 주의를 합산하는 3초 정차는 기본 모드에서 제거했다. 기존 모드는 config/legacy에 보존한다. 실제 차량이나 119 신고용 시스템은 아니다.

## 현재 분석 전송

```bash
sh run.sh --input data/source_videos/D/D2.mp4 \
  --calibration-profile config/profiles/D2_eye_profile.json \
  --output outputs/unity_trial --unity
```

Python의 127.0.0.1:5001에 Unity가 접속한다. 같은 포트를 쓰는 다른 Python 서버는 종료한다. --unity-host와 --unity-port로 주소를 지정할 수 있다. 이 경로는 현재 추론의 Decision을 매 분석 프레임 publish()에 전달한다. 별도 --show로 현재 분석 화면도 띄울 수 있다. 영상 시각으로 판단하며 실시간 카메라 입력은 구현하지 않았다.

저장 로그의 1배속 재생은 serve_unity.py로 구분한다. 이전 로그를 재판단하려면 --rules config/legacy/decision_rules.json을 지정한다. 새 로그에는 literature_frame이 있어 새 모드로 재판단할 수 있다. 기록된 경고 확인 사건을 재생하는 것은 현재 Unity의 실제 경고 실행 확인과 다르다.

## Unity 파일과 상태

같은 파일을 중복 추가하지 말고 아래 두 파일을 원래 프로젝트의 동일 위치에서 교체한다.

- Unity/Assets/Stage1BasicVehicle/Scripts/UnityTcpVehicleBridge.cs
- Unity/Assets/Stage1BasicVehicle/Scripts/DriverStateWarningUI.cs

Normal은 기존 주행 제어 유지, Caution은 경고 UI와 소리, Danger는 EmergencyPullOverController.TriggerEmergency() 호출 경로다. 씬의 vehicle, emergencyController, warningPanel, warningText 참조가 필요하다. 갓길 경로와 차량 물리 코드는 이번 복사본에 포함되어 있지 않다.

정차 요청은 StopRequestLatched로 따로 유지한다. Normal이나 Unavailable을 받아도 비상 제어를 취소하지 않는다. 시뮬레이터에서 명시적으로 비상 상태를 해제하고 차량이 멈춘 뒤 ResetStopRequestLatch()를 호출할 수 있다. 현재 관측 상태와 정차 진행 상태를 혼동하지 않는다.

## 패킷과 경고 확인

기본 TCP 20Hz, UTF-8 JSON 한 줄씩을 사용한다. driver_state, vision_state, vision_available, 판단 이유, 원인별 지속 시간, 경고 요청 ID와 발행 시각, 기능 준비 상태를 전달한다. 조향, 가속, 브레이크나 제어 모드를 Python에서 덮어쓰지 않는다. 새 C# 패킷 형식은 reasons, durations, readiness도 읽는다.

0.75초보다 오래된 결과는 vision_available=false와 vision_state=unavailable, prediction_stale로 전달한다. 구버전 호환용 driver_state=Caution이 있어도 새 수신기는 일반 주의와 구분한다. Unity도 1초 예측 수신 watchdog을 유지한다. heartbeat는 행동 시간의 추가 관측이 아니다.

새 모드의 caution_seconds는 이전 호환 필드이며 3초 정차 타이머로 사용하지 않는다. caution_timer_policy=not_used와 judgment_mode를 확인하고 실제 원인별 시간은 durations에서 읽는다.

주의분산 경고를 요청한 경우에만 warning_request_id가 생긴다. Unity 경고 UI는 활성화된 렌더 프레임 이후 driver_warning_ack를 전송한다. Python은 해당 사건을 현재 영상 시각으로 대응시킨다. warning_issued_at은 warning_display_confirmed가 true일 때 해석한다. 요청 전송만으로 확인됐다고 처리하지 않는다. 다른 주의 이유나 관측 실패는 같은 미복귀 시간으로 합산하지 않는다.

시선 모델이 없으면 UNKNOWN이다. D2 프로필은 눈 감김 검토에 실패해 눈 감김 정차 경로가 비활성이다. 이 상태에서 정차를 보이기 위해 임의의 Danger 규칙을 추가하지 않는다.

## 검증 범위

Python 모의 TCP 수신기를 이용해 Normal, Caution, Danger와 Unavailable의 전달 및 현재 분석에서 publish() 호출을 테스트했다. 실제 영상에서도 현재 판단의 전송을 확인했다. Unity Editor를 직접 실행하거나 C#을 컴파일하지 않았으며, 실제 UI와 소리, 차량 감속과 갓길 정차, e-Call은 검증하지 않았다.

실제 Unity 확인은 씬 참조와 두 파일 교체 후 수행한다. 각 상태의 동작과 경고 ACK, Danger 이후 Normal/Unavailable 및 통신 끊김에서도 정차가 유지되는지 확인한다. e-Call 서버 전송 완료를 이 코드만으로 주장하지 않는다.


## 최종 관측 출력과 운전자 응답

`VehicleControlPacket.monitoring`에 `MonitoringOutputPacket.cs` 형식의 관측 출력이 추가됐다. Danger 영상 시각, 지표, 반응 기록, 관측 품질을 전달한다. 상태 문자열과 세부 필드 설명은 `../docs/MODEL_OUTPUT.md`를 따른다.

Unity 프로젝트에 `UnityTcpVehicleBridge.cs`와 `MonitoringOutputPacket.cs`를 함께 반영한다. 기존 조향과 감속 제어기는 변경하지 않았다. 명시적 응답 버튼을 연결했다면 Bridge의 `driverResponseInputEnabled`를 켜고, 버튼 OnClick에서 `ReportDriverAcknowledgement()` 또는 `ReportDriverCancelAttempt()`를 호출한다. 이 함수는 응답을 보고할 뿐 정차를 취소하지 않는다.

기존 `ReportDisplayedWarning()`은 경고 UI가 렌더링됐다는 확인이다. 운전자 응답으로 사용하지 않는다. `JsonUtility`에서 null 값은 기본 0 또는 false가 될 수 있으므로 observed/calibrated/connected 플래그 확인이 필요하다.

추가된 C# 코드의 Unity Editor 컴파일과 버튼 동작은 직접 실행하지 않았다. Python 테스트와 TCP 모의 클라이언트에서 응답 프로토콜을 확인했다.

## 정차 완료 사진을 위한 응답

새 Bridge는 `vehicle_state`에 현재 명령의 `clip_id`, `monitoring.session_id`, `monitoring.source_time_seconds`를 각각 `clip_id`, `session_id`, `source_timestamp`로 돌려준다. 현재 판단이면 `has_source_timestamp=true`다. Unity의 자체 실행 시간인 `timestamp`를 영상 시각으로 사용하지 않는다.

Python은 연결과 수신 신선도, 영상 및 세션 대응, 앞선 정차 요청, `Stopped` 단계와 속도 0~0.1 kph를 모두 확인한 경우만 `vehicle_confirmation.stop_confirmed=true`로 기록한다. 사진은 이 응답을 받은 영상 프레임에서 저장한다. 다른 영상이나 재실행 전의 응답, 시간이 대응하지 않는 구버전 응답은 사용하지 않는다.

이 프로토콜은 Python TCP 모의 클라이언트로 검사한다. 실제 Unity Editor 실행, 차량 정차, 갓길 도착이나 e-Call 완료는 검증하지 않았다. 현재 제공한 네 결과 영상에는 정차 완료 응답이 없으며 정차 후 사진도 없다.
