# Biosignal weights

사용자에게 전달받은 압축 파일의 가중치를 그대로 보존했다.
현재 ECG는 hr_classifier_ft.pt, PPG는 papagei_s.pt와 ppg_head_ft.pt를 사용한다.
전달받은 biosignal_camera_live_bpm.py의 기본값과 같다. 추가 학습은 하지 않았다.

ECG의 1D CNN과 PPG의 PaPaGei-S 인코더 및 분류기는 src/biosignal에서 읽는다.
실행에 사용한 파일의 SHA256은 manifest.json과 결과 summary.json에 남긴다.
코드 라이선스는 src/biosignal/third_party/PAPAGEI_LICENSE를 확인한다.
공급 코드의 학습 성능과 의학적 유효성은 이번 연결에서 별도로 검증하지 않았다.
