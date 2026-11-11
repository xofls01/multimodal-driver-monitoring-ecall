# Model weights

기존 사전학습 가중치 yolo26n.pt와 yolo26n-pose.pt를 동일한 SHA256으로 보존했다. 추가 학습하지 않았다. 모델 체크섬은 manifest.json에 있다.

YOLO Detection은 휴대전화, 음료 용기, 식기, 음식과 책 후보를, Pose는 신체 키포인트를 추출한다. 얼굴, 눈, 입과 손은 고정한 MediaPipe 패키지의 모델을 사용한다. 의료 상태 분류 모델이 아니다.

문헌 R4의 시선 모델은 미연결이다. 가중치와 전처리, 클래스 순서를 검토하기 전 기존 YOLO나 얼굴 상대 지표를 그 모델의 결과로 사용하지 않는다. 자세한 조건은 docs/LITERATURE_MAPPING.md에 있다.
