"""모든 임계값을 한곳에 모은 설정.

[요구] 산학협력 문제 제안서에 명시된 값
[근거] 공개 가이드라인에서 가져온 값. 출처는 biosignal/README.md의 "판정 기준과 근거"
그 외는 설계값이다. 임상 검증을 거친 수치가 아니므로 실측 데이터로 조정해야 한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Config:
    # --- 샘플링 (입력이 불규칙해도 이 주기로 재샘플링한다) ---
    ecg_fs: float = 250.0
    ppg_fs: float = 100.0

    # --- 분석 창 ---
    window_s: float = 12.0
    min_ready_s: float = 6.0        # 이만큼 쌓여야 분석 시작
    stale_s: float = 1.5            # 이 시간 동안 샘플이 없으면 센서 끊김으로 처리

    # --- 학습 모델 ---
    model_smooth_n: int = 3         # 최근 N회 추론의 확률을 평균해 순간 오판을 줄인다
    model_min_confidence: float = 0.7  # 미만이면 모델 판정을 버리고 임계값 규칙으로 판정

    # --- 전처리 ---
    mains_hz: float = 60.0          # 한국 상용전원
    # 포화(전극·센서 탈락) 판별 범위. 실측 기록에서 ECG는 0과 약 760에, PPG는 0과 약 900 이상에 붙었다.
    # 공급 전압이 달라지면 값이 바뀌므로 장비를 바꾸면 다시 확인해야 한다.
    ecg_rail_low: float = 8.0
    ecg_rail_high: float = 742.0
    ppg_rail_low: float = 8.0
    ppg_rail_high: float = 890.0
    rail_fraction: float = 0.05     # 분석 창에서 포화 샘플이 이 비율을 넘으면 탈락으로 판단
    ppg_contact_dc_min: Optional[float] = None  # PPG 접촉 판별 DC 하한. None이면 판별 안 함

    # --- 신호 품질 ---
    sqi_reliable: float = 0.5       # 이 값 이상이어야 채널을 신뢰
    amp_floor_ratio: float = 0.2    # 평소 진폭의 이 비율 미만인 피크는 잡음으로 버림
    quiet_ratio: float = 0.3        # 최근 신호 진폭이 평소의 이 비율 미만이면 '평탄'

    # --- 심박 임계값 (판단 기준 PPT) ---
    # 정상 51~100 / 주의 41~50, 101~130 (10초) / 위험 40 이하, 131 이상 (두 센서가 5초)
    hr_extreme_low: float = 40.0    # [요구] 이 값 이하는 위험
    hr_danger_high: float = 131.0   # [요구][근거] 이 값 이상은 위험. 제안서 요구값, 조기 경고 점수 3점(≥131)
    hr_extreme_high: float = 150.0  # 한 센서만 이 범위(40 이하·150 이상)를 재고 다른 센서를 못 믿으면 판단 불가
    hr_caution_low: float = 50.0    # [근거] 이 값 이하면 주의 (41~50)
    hr_caution_high: float = 100.0  # [근거] 이 값 초과면 주의 (101~130)
    hr_mismatch_bpm: float = 10.0   # 두 센서가 모두 깨끗한데 심박수가 이만큼 넘게 다르면 판단 불가. Nano 펌웨어 기준

    # --- 개인 기준선: 시작 후 첫 10초의 심박수 ---
    calibration_s: float = 10.0
    calibration_min_readings: int = 5  # 이 구간에서 필요한 신뢰 측정 횟수
    baseline_min_hr: float = 40.0   # 이 범위 밖의 심박수는 평소 값으로 보지 않고 다시 측정한다
    baseline_max_hr: float = 100.0
    personal_low_margin: float = 10.0  # 평소 심박이 낮은 사람은 '기준선 - 이 값' 미만부터 저심박으로 본다
    baseline_tau_s: float = 600.0   # RMSSD 기준값의 적응 시간. 심박 기준선은 고정한다

    # --- 기준선 대비 상승·하강 ---
    abrupt_delta_bpm: float = 25.0
    abrupt_delta_ratio: float = 0.30

    # --- 지속 시간 (순간적인 오탐 방지) ---
    pause_s: float = 3.0            # [근거] 동정지 정의. 모든 센서에서 박동이 끊기면 주의
    no_pulse_s: float = 8.0         # 맥박 확인 상한 10초([근거]) 안에 들도록 정한 값
    extreme_hold_s: float = 5.0
    caution_hold_s: float = 10.0
    abrupt_hold_s: float = 7.0
    irregular_hold_s: float = 20.0
    clear_hold_s: float = 10.0      # 이 시간 동안 정상이어야 경고 해제
    # 측정이 이 시간 이하로 끊겼다 돌아오면 지속 시간 타이머를 이어서 잰다.
    # 진동·움직임 잡음 1~2초 때문에 10초 판정을 처음부터 다시 하지 않도록 한다
    gap_tolerance_s: float = 3.0
    # 판단 불가가 이 시간 안에 끝나면 직전 등급(주의)에서 다시 시작해 clear_hold_s를 거쳐 해제한다.
    # 잡음 한 번으로 주의가 곧바로 정상이 되지 않도록 한다
    resume_memory_s: float = 30.0

    # --- HRV / 불규칙 / 졸음 참고 지표 ---
    hrv_window_s: float = 60.0
    hrv_min_rr: int = 30
    irregular_rr_cv: float = 0.20
    drowsy_hr_drop_ratio: float = 0.08
    drowsy_rmssd_rise_ratio: float = 0.20
    drowsy_hold_s: float = 30.0
