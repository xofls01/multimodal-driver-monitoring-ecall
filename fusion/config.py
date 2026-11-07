"""통합 판단의 모든 임계값.

[근거] 공개 기준에서 가져온 값. 출처는 fusion/README.md의 "참고 자료"
[비전] 비전 쪽 decision_rules.json과 맞춘 값
그 외는 설계값이다. 실주행 기록으로 조정해야 한다.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FusionConfig:
    # --- 입력 끊김 판정 (받은 시각 기준) ---
    bio_stale_s: float = 3.0        # 생체신호는 약 1초마다 온다
    vision_stale_s: float = 2.0
    can_stale_s: float = 1.0        # Unity는 20 Hz로 보낸다

    # --- CAN: 조작 감지. 조향은 -1~1, 페달은 0~1로 정규화해서 넣는다 ---
    # 조향 기본값은 Unity(최대 30도) 기준이다. 실차는 핸들 각도 범위에 맞춰 다시 정해야 한다
    steering_delta: float = 0.02
    pedal_delta: float = 0.05
    gesture_gap_s: float = 0.5      # 이 시간 이상 떨어진 조작을 별개의 동작으로 센다

    # --- CAN: 무조작 ---
    no_input_support_s: float = 5.0  # 다른 근거가 있을 때 보조 근거로 쓰는 시간
    no_input_alone_s: float = 15.0   # [근거] UN R79 핸즈오프 첫 경고 시점. 비전이 없을 때의 단독 기준

    # --- CAN: 정지 추정 (속도 신호가 없을 때) ---
    stopped_speed_kph: float = 5.0   # 속도 신호가 있을 때만 쓴다
    stopped_brake: float = 0.1
    stopped_brake_hold_s: float = 2.0

    # --- CAN: 급조작 ---
    abrupt_window_s: float = 0.5
    abrupt_steer_delta: float = 0.5
    abrupt_quiet_before_s: float = 5.0  # 무수정 뒤 급수정 패턴에서 '무수정'으로 볼 시간. 패턴만 문헌에 있고 수치는 설계값
    hard_brake_delta: float = 0.6
    abrupt_repeat_count: int = 2     # 한 번은 회피 조작일 수 있어 반복될 때만 주의로 본다
    abrupt_repeat_window_s: float = 60.0

    # --- 응답 확인 창: 경고 후 이 시간 안에 조작이 없으면 위험 ---
    window_corroborated_s: float = 5.0   # [비전] danger_after_warning_seconds. 근거가 둘 이상일 때
    window_single_s: float = 10.0        # [근거] 눈 감김 2초 + 10초 = 12초. Euro NCAP 13초 안
    window_attention_s: float = 12.0     # [근거] 전방 이탈 2초 + 12초 = 14초. Euro NCAP 14초
    window_bio_unconfirmed_s: float = 30.0  # 응답 수단이 없고 비전은 정상일 때의 생체 위험
    response_gestures: int = 2           # 쓰러지며 핸들을 건드린 한 번은 응답으로 보지 않는다

    # --- 응답 확인 후 다시 확인할 때까지의 시간 ---
    regrace_behavior_s: float = 10.0
    regrace_bio_s: float = 60.0

    # --- 판단 ---
    sleep_s: float = 3.0             # [근거] Euro NCAP Tsleep. 즉시 위험에 쓰는 눈 감김 시간
    bio_danger_immediate: bool = False  # True면 생체 위험을 응답 확인 없이 바로 위험으로 올린다
    startup_s: float = 20.0          # 생체 기준선(10초 이상)과 비전 보정(최대 6초)을 기다린다
    clear_hold_s: float = 5.0        # 주의 해제 전 유지 시간. Unity 경고창이 반복해서 뜨는 것을 막는다
    hard_brake_note_s: float = 5.0   # 급제동을 사유에 남기는 시간
