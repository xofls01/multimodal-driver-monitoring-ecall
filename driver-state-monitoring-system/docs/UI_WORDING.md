# 보기 쉬운 영어 UI 문구

현재 상태, 관측된 행동, 지속 시간과 기준, 다음 대응 순서로 읽을 수 있도록 문구를 정리했다. 판단 엔진과 모델, 설정값, 관측 기록과 Unity 예시 패킷은 변경하지 않았다.

## 화면 표현

맨 위는 DRIVER MONITOR와 영상 시간, Normal / Caution / Danger / Unavailable이다. Normal에서는 No warning triggered, 경고 전 눈 감김에는 Eyes closed - checking duration을 표시한다. Caution은 실제 관측된 행동과 이유를 표시하고 Danger는 Stop requested로 표시한다.

Eyes의 현재 상태는 Open, Closed, Unable to read다. 눈 감김 막대 위에 Eyes closed for 0.70s와 Warning at 1.5s를 분리해 표시한다. 관측할 수 없을 때는 0초로 채우지 않는다.

Driver attention은 Looking ahead, Looking away, Eyes closed, Unable to assess로 표시한다. 시선 영역 모델의 결과처럼 보이지 않도록 Estimated from head position 또는 Estimated from head and shoulder position처럼 추정에 사용한 정보를 작은 설명으로 붙인다.

연속 이탈은 Looking away for 4.19s, 누적 이탈은 Total away (last 30s): 5.61s로 표시한다. 경고 기준은 오른쪽에 Warning at 3s, Warning at 10s로 따로 표시한다.

반복 이탈과 고개 움직임, 긴 눈 감김의 횟수는 Times looking away, Head nods (last 30s), Long eye closures (last 30s)로 표시한다. 현재 횟수와 반복 기준을 글자로 구분한다. 반복 횟수만으로 모든 경고가 발생하는 것은 아니며 엔진의 현재 행동 조건은 유지한다. Head nods는 기존 머리 하강과 복귀의 관측 횟수이며 졸음이 검증됐다는 뜻은 아니다.

Posture에서는 실제 근거가 확인되는 항목에 따라 Head lowered 또는 Body leaning을 선택한다. 두 항목이 모두 확인됐을 때만 함께 표시한다. 특정 성분을 확인할 수 없는데 숙임과 기울기가 모두 확인된 것처럼 표시하지 않는다.

복합 자세 타이머는 Posture change for 3.31s로 표시한다. 현재의 개별 행동과 여러 자세 조건을 묶은 타이머를 구분해, 현재 머리가 내려갔다는 이유로 전체 복합 타이머가 모두 머리 숙임 시간이라고 단정하지 않는다.

정차 막대는 기존처럼 경고 후 같은 자세가 이어진 시간 / 설정 시간을 채운다. 그 위 문구는 남은 시간인 Stop request in 0.50s로 표시하고 If the same posture continues를 붙인다. 조건에 도달하면 Stop requested로 바꾼다. 실제 차량이 정차했다는 뜻의 Vehicle stopped는 사용하지 않는다. 자세가 해소되면 No stop countdown, 관측이 안 되면 Unable to assess stop countdown이다.

보조 항목은 Mouth open, Hand near chest, Body leaning이다. rPPG는 Estimated pulse로 표시하며 신호 수집과 낮은 품질, 해당 영상에서 사용하지 않는 경우를 구분한다.

## 유지한 표시

눈 감김, 연속 전방 이탈, 누적 전방 이탈, 자세 경고, 정차 타이머의 진행 막대 5개와 보조 관측 막대 3개를 유지했다. 실제 값과 기준에 따른 채움 비율, 색상, 반복 사건 표시와 초록색 / 노란색 / 빨간색 / 회색 바깥 테두리도 유지했다.

원본 장면 1280 x 720과 패널, 바깥 테두리를 포함한 출력은 1756 x 736이다. 원본 FPS와 프레임 수를 유지했고 인코딩 전 장면 픽셀의 동일성을 확인했다. 재인코딩에 따른 미세한 화질 차이는 있을 수 있다.

## 결과와 검증

[현재 영상 모음](../outputs/ui_plain_english/index.html)에 N1, D2, D3, A4, E4를 반영했다. 새 검출이나 판단 재계산을 수행한 작업은 아니다. 기존 기록 1223개의 해시와 Unity 예시 패킷, 사건 구간 CSV의 동일성을 확인했다. 이전 결과와 원본은 보존했다.

자동 테스트 174개가 통과했다. 실제 근거에 맞는 자세 이름, 조건부 정차 카운트다운, 정차 요청과 차량 정차 완료의 구분, 미관측일 때 남은 시간을 만들지 않는 처리를 확인했다. 기존 판단과 TCP 모의 전송 테스트도 통과했다. 실제 Unity Editor는 실행하지 않았다.

새 영상을 기존 run.sh 명령으로 분석하면 새 UI를 사용한다. 문헌 반영 범위와 눈 감김 대기 상태는 [EYE_CLOSURE_STATE.md](EYE_CLOSURE_STATE.md)에 있다.
