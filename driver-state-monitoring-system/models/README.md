# Model files

코드 저장소에는 가중치 파일 대신 SHA256 목록을 둡니다. 별도 보관한 driver-runtime-assets.zip을 프로젝트 루트에 풀면 정확히 사용한 가중치가 복원됩니다.

- yolo26n.pt: 사전학습 물체 검출 모델
- yolo26n-pose.pt: 사전학습 자세 모델
- biosignal/hr_classifier_ft.pt: 전달받은 ECG 모델
- biosignal/papagei_s.pt: 전달받은 PPG 인코더
- biosignal/ppg_head_ft.pt: 전달받은 PPG 분류기

이 통합 작업에서 새로 학습하지 않았습니다. 미사용 기본 ECG와 PPG 가중치는 실행 묶음에서 제외했습니다. 전체 원본 가중치는 로컬 원본 프로젝트에 보존했습니다.
