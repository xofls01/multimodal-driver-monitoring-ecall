# 변경 파일

원본과 비교한 별도 복사본의 변경이다. 모델 가중치와 원본 영상은 변경하지 않았다.

- 추가: [src/driver_state_tools/analysis_publisher.py](../src/driver_state_tools/analysis_publisher.py)
- 수정: [src/driver_state_tools/dashboard.py](../src/driver_state_tools/dashboard.py)
- 수정: [src/driver_state_tools/decision_engine.py](../src/driver_state_tools/decision_engine.py)
- 추가: [src/driver_state_tools/gaze_zones.py](../src/driver_state_tools/gaze_zones.py)
- 추가: [src/driver_state_tools/literature_engine.py](../src/driver_state_tools/literature_engine.py)
- 추가: [src/driver_state_tools/nod_events.py](../src/driver_state_tools/nod_events.py)
- 추가: [src/driver_state_tools/revision_session.py](../src/driver_state_tools/revision_session.py)
- 수정: [src/driver_state_tools/unity_bridge.py](../src/driver_state_tools/unity_bridge.py)
- 수정: [src/driver_state_tools/vision_features.py](../src/driver_state_tools/vision_features.py)
- 수정: [src/driver_state_tools/vision_pipeline.py](../src/driver_state_tools/vision_pipeline.py)
- 추가: [scripts/build_eye_calibration.py](../scripts/build_eye_calibration.py)
- 추가: [scripts/package_revision_review.py](../scripts/package_revision_review.py)
- 수정: [scripts/run_project.py](../scripts/run_project.py)
- 수정: [scripts/run_vision_baseline.py](../scripts/run_vision_baseline.py)
- 수정: [scripts/serve_unity.py](../scripts/serve_unity.py)
- 추가: [scripts/verify_actual_video_transport.py](../scripts/verify_actual_video_transport.py)
- 추가: [tests/test_current_analysis_publish.py](../tests/test_current_analysis_publish.py)
- 수정: [tests/test_feature_quality.py](../tests/test_feature_quality.py)
- 추가: [tests/test_literature_engine.py](../tests/test_literature_engine.py)
- 수정: [tests/test_responsive_decisions.py](../tests/test_responsive_decisions.py)
- 추가: [tests/test_revision_features.py](../tests/test_revision_features.py)
- 수정: [config/decision_rules.json](../config/decision_rules.json)
- 추가: [config/gaze_model_metadata.example.json](../config/gaze_model_metadata.example.json)
- 추가: [config/legacy/decision_rules.json](../config/legacy/decision_rules.json)
- 추가: [config/legacy/vision_baseline.json](../config/legacy/vision_baseline.json)
- 추가: [config/profiles/D2_eye_profile.json](../config/profiles/D2_eye_profile.json)
- 추가: [config/profiles/D2_reviewed_intervals.json](../config/profiles/D2_reviewed_intervals.json)
- 추가: [config/profiles/template.json](../config/profiles/template.json)
- 수정: [config/vision_baseline.json](../config/vision_baseline.json)
- 수정: [integration/UNITY_INTEGRATION.md](../integration/UNITY_INTEGRATION.md)
- 수정: [integration/Unity/Assets/Stage1BasicVehicle/Scripts/DriverStateWarningUI.cs](../integration/Unity/Assets/Stage1BasicVehicle/Scripts/DriverStateWarningUI.cs)
- 수정: [integration/Unity/Assets/Stage1BasicVehicle/Scripts/UnityTcpVehicleBridge.cs](../integration/Unity/Assets/Stage1BasicVehicle/Scripts/UnityTcpVehicleBridge.cs)
- 추가: [integration/Unity-literature-bridge.zip](../integration/Unity-literature-bridge.zip)
- 수정: [integration/source_manifest.json](../integration/source_manifest.json)
- 추가: [docs/CHANGED_FILES.md](../docs/CHANGED_FILES.md)
- 추가: [docs/LITERATURE_MAPPING.md](../docs/LITERATURE_MAPPING.md)
- 추가: [docs/REVISION_REPORT.md](../docs/REVISION_REPORT.md)
- 추가: [docs/revision_spec.md](../docs/revision_spec.md)
- 수정: [models/README.md](../models/README.md)
- 수정: [README.md](../README.md)
- 추가: [run.sh](../run.sh)
- 수정: [package_manifest.json](../package_manifest.json)

## 화면 표시 보완 파일

- 추가: src/driver_state_tools/readable_dashboard.py
- 수정: src/driver_state_tools/dashboard.py
- 추가: scripts/rerender_readable_review.py
- 추가: tests/test_readable_dashboard.py
- 수정: requirements.txt (기존 환경의 Pillow 버전을 직접 의존성으로 명시)
- 추가: docs/UI_READABILITY.md
- 수정: README.md, docs/REVISION_REPORT.md, package_manifest.json
- 추가 결과: outputs/revision_readable_ui/

## 판단 경로와 영어 증거 화면

상세 변경과 실제 결과는 [PROTOTYPE_RESTORE.md](PROTOTYPE_RESTORE.md)에 있다. 원본 프로젝트와 엄격한 문헌 엔진 및 설정은 보존했다.

- src/driver_state_tools/dashboard.py
- src/driver_state_tools/evidence_dashboard.py
- src/driver_state_tools/prototype_engine.py
- src/driver_state_tools/revision_session.py
- src/driver_state_tools/unity_bridge.py
- src/driver_state_tools/vision_features.py
- scripts/build_prototype_review.py
- scripts/run_vision_baseline.py
- scripts/verify_actual_video_transport.py
- scripts/verify_prototype_replay.py
- tests/test_prototype_evidence.py
- config/profiles/prototype_neutral_windows.json
- config/prototype/decision_rules.json
- config/prototype/vision_baseline.json
- docs/PROTOTYPE_RESTORE.md
- README.md
- run.sh
- results.html

## 눈 감김 경고 전 상태와 문헌 반영 범위

- src/driver_state_tools/evidence_dashboard.py
- src/driver_state_tools/prototype_engine.py
- src/driver_state_tools/unity_bridge.py
- scripts/build_prototype_review.py
- scripts/rejudge_prototype_review.py
- tests/test_eye_closure_pending.py
- docs/EYE_CLOSURE_STATE.md
- docs/LITERATURE_MAPPING.md
- docs/PROTOTYPE_RESTORE.md
- README.md
- results.html
- docs/CHANGED_FILES.md
- package_manifest.json

새 결과: outputs/eye_closure_pending/

## 영어 UI 문구 정리

- src/driver_state_tools/evidence_dashboard.py
- scripts/build_prototype_review.py
- tests/test_prototype_evidence.py
- tests/test_eye_closure_pending.py
- docs/UI_WORDING.md
- README.md, results.html, package_manifest.json

새 결과: outputs/ui_plain_english/

## 휴대전화 오검출 수정

- src/driver_state_tools/object_tracking.py
- src/driver_state_tools/vision_pipeline.py
- config/vision_baseline.json, config/prototype/vision_baseline.json, config/legacy/vision_baseline.json
- tests/test_object_tracking.py, tests/test_phone_context.py
- docs/PHONE_FALSE_POSITIVES.md

새 결과: outputs/phone_verified/
