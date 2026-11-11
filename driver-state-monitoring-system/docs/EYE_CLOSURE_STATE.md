# 눈 감김 대기 상태와 문헌 반영 범위

## 이번 변경

눈을 감았다는 사실을 유효하게 관측한 상태와 눈 상태 자체를 읽지 못한 상태를 구분한다. Normal은 경고 조건이 아직 충족되지 않았다는 운영 상태이며, 눈이 열려 있거나 안전 운전이 입증됐다는 뜻이 아니다.

현재 눈 감김이 유효하게 관측되고 다른 경고와 미해소 원인이 없으면, 1.5초 전에는 Normal과 EYE CLOSURE / EVALUATING을 표시한다. 눈 감김 시간 막대는 계속 진행한다. 1.5초에 도달하면 기존대로 Caution을 발생시킨다. 꾸벅임과 자세 경고가 먼저 성립하면 해당 경고를 우선한다.

눈 감김 중 시선은 정면이나 이탈로 채우지 않는다. attention_away는 None, 관측 가능 필드는 false로 유지하고 화면에 EYES CLOSED를 표시한다. 실제 눈이나 얼굴 관측 실패는 여전히 Unavailable이다.

이전에 전방 이탈 경고가 있었고 눈 감김 때문에 전방 복귀를 확인하지 못하면 Caution을 유지한다. 눈을 감았다는 이유로 이전 경고를 해제하거나 정상 복귀 시간을 채우지 않는다. 이전 자세 경고에서 실제 자세 관측이 사라지면 여전히 recovery_unobserved로 Unavailable이다.

경고 원인 해소가 관측되면 0.3초 안정화 후 Normal로 복귀한다. 이번 변경은 이 시간이나 눈 감김 경고 1.5초, 전방 이탈 기준, 자세 정차 기준을 변경하지 않았다.

Unity 예시 패킷과 실제 Python 전송 경로에는 decision_phase, pending_events, attention_status를 추가했다. 평가 중에는 driver_state=Normal, vision_available=true가 가능하지만 observation_status.attention=false와 attention_status=not_assessed_eyes_closed가 함께 전달된다. 이것은 시선을 정상이라고 추정한 결과가 아니다. 오래된 패킷은 여전히 vision_available=false이며 평가 중 사건도 비운다.

## 현재 문헌을 반영했다고 말할 수 있는가

일부 지표와 시간 기준을 참고해 구현했다고 말할 수 있다. 전체 모델을 재현했거나 현재 모든 경고와 정차 조건이 문헌으로 검증됐다고 말할 수는 없다. 기본 prototype 모드와 보존한 literature 모드를 구분해야 한다.

R1의 EAR 보정 원리는 prototype 설정의 eye_close_ratio=0.75와 양쪽 EAR 평균, 검토한 눈 뜸 구간 평균에 반영했다. 실제 적용에는 임계값 제한과 히스테리시스가 있으므로 항상 원문의 수치와 완전히 같지는 않다. Face Mesh로 측정을 바꾼 조합의 정확도도 별도 검증이 필요하다. [원문 식(5)](https://www.mdpi.com/1424-8220/24/17/5683)

R2 Table 2의 눈 감김 1.5초를 Caution 시간으로 참고했다. 원문은 해당 시간만으로 모든 졸음이나 의식 상태를 확정하는 연구가 아니다. 우리 Caution 상태에 연결한 것은 적용 설계다. [R2 원문](https://www.mdpi.com/1424-8220/19/14/3200)

R6에서 평가한 DMS의 연속 이탈 3초 또는 30초 중 누적 이탈 10초를 시간 참고값으로 반영했다. 현재 입력은 상대 머리와 홍채 지표이며 R4의 시선 영역 모델이 아니다. 따라서 같은 숫자를 썼다는 사실과 동일한 시선 측정 성능을 구현했다는 주장은 구분한다. [R6 출판사 초록](https://www.sciencedirect.com/science/article/pii/S1369847824003498)

R1의 60초 구간과 20% 눈 감김 비율도 계산기에 반영했다. 다만 15초 영상에서는 구간이 완성되지 않아 이 경고가 활성화되지 않는다. 결측 시간에 따른 비율 범위를 쓰는 것은 별도의 구현 설계이며 표준 P80 측정을 주장하지 않는다.

R3의 보정된 꾸벅임 사건 검출기는 코드로 보존했고 개인과 카메라에 맞는 경계와 속도 보정이 필요하다. 현재 prototype의 NOD PROXY, 머리 내려감 0.12, 0.4초와 반복 횟수는 논문을 완전히 재현한 경고가 아니다. 기능을 삭제한 것은 아니지만 문헌식 검출을 검증 완료한 것으로 소개하면 안 된다.

R4 시선 영역 모델은 가중치와 전처리 확인이 끝나지 않아 미연결이다. 기존 상대 지표가 그 모델을 대체 구현한 것처럼 설명하지 않는다.

R1의 입 벌림 원문 식과 MediaPipe 점 대응, 최대 입 벌림 보정은 엄격한 문헌 모드에서 준비가 필요하다. 현재 prototype의 단일 세로 거리 MAR 경고는 그 원문 식과 별개다.

R7에서 참고한 눈 감김 6초 정차는 현재 검증되지 않아 prototype에서 비활성이다. 엄격한 문헌 모드에는 실제 주의분산 경고 표시 뒤 전방 미복귀 3초 경로가 있지만, 시선 모델이 미연결이라 현재 시연에서 그 경로가 완성됐다고 말할 수 없다.

현재 E4에서 작동하는 큰 자세 변화 0.8초 경고와 같은 자세가 경고 후 3초 더 지속될 때의 정차는 프로젝트의 시험용 규칙이다. R7의 경고 후 전방 미복귀 3초와 동일한 조건이 아니다. 가슴 부근 손 1초, 상체 기울기 수치, 반복 횟수, 복귀 0.3초와 이번 경고 전 상태 처리도 우리 설계 또는 구현 설정이다.

발표에서는 다음과 같이 설명할 수 있다.

> 사전학습 모델에서 추출한 행동 특징에 문헌의 일부 지표와 시간 기준을 적용했다. 시뮬레이터 대응과 관측 실패 처리에는 별도의 설계 규칙을 사용했으며, 사람과 촬영 조건이 달라질 때의 성능 검증은 후속 단계다.

## 코드 위치

- src/driver_state_tools/prototype_engine.py: 눈 감김 평가 중 상태와 이전 경고 유지
- src/driver_state_tools/evidence_dashboard.py: 평가 중 이유와 EYES CLOSED 표시
- src/driver_state_tools/unity_bridge.py: 평가 단계와 사건을 패킷에 포함
- tests/test_eye_closure_pending.py: 경계 시간, 관측 실패, 이전 경고와 TCP 모의 수신 확인
- scripts/rejudge_prototype_review.py: 기존 실제 관측 기록에 새 판단 적용
- config/prototype/decision_rules.json: 기존 시간 기준과 출처 설명, 이번에 수치 변경 없음

## 확인 결과와 실행

자동 테스트 171개를 통과했다. 합성 관측으로 만든 Normal 평가 중 상태와 Caution이 AnalysisPublisher 및 UnityStateServer를 거쳐 실제 로컬 TCP 모의 수신기에 도착하는 것도 확인했다. 실제 Unity Editor와 차량 물리는 실행하지 않았다.

기존 실제 영상의 관측 기록 1223개에 새 판단을 적용하고 영어 UI 영상 5개를 다시 만들었다. 새 검출 추론이나 영상 기반 새 실시간 전송을 실행한 것은 아니다. 새 폴더의 unity_packets는 오프라인 예시 패킷이다. 이전 원본 추론과 전송 기록은 그대로 보존했다.

D2는 Unavailable 80개 중 61개가 경고 전 눈 감김으로 인한 평가 중 Normal로 바뀌었다. Caution 21개와 첫 Caution 9.13초는 유지됐다. D3는 Caution 85개와 첫 Caution 3.83초가 유지됐다. A4에서는 기존 전방 이탈 경고가 눈 감김 중 임의 해제되지 않도록 유지되는 시점이 추가됐다. E4는 Normal 58개, Caution 89개, Danger 72개, Unavailable 16개와 첫 Danger 11.01초가 그대로다.

[이번 영상 모음](../outputs/eye_closure_pending/index.html)과 해당 폴더의 state_comparison.json, test_result.json, video_verification.json에서 확인할 수 있다.

현재 영상을 새로 분석할 때는 기존과 같이 실행한다.

```bash
sh run.sh --input data/source_videos/D/D2.mp4 --output outputs/new_d2 --unity
```

기존 측정에 판단만 다시 적용할 때는 다음을 사용한다. 새 출력 경로를 지정해야 한다.

```bash
python scripts/rejudge_prototype_review.py --run outputs/prototype_evidence_final --output outputs/new_eye_review
python scripts/build_prototype_review.py --run outputs/new_eye_review
```

눈 감김 검출 누락은 이번 상태 표시 변경으로 해결한 문제가 아니다. 추가 보정과 검증이 필요하다.
