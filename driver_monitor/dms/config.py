"""모든 임계값/설정. config.json 으로 덮어쓸 수 있음."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path


@dataclass
class Config:
    # ---------------- 캘리브레이션 ----------------
    calib_seconds: float = 5.0          # 시작 시 정면 응시 + 눈 뜬 상태로 측정하는 시간

    # ---------------- 눈 감김 ----------------
    ear_ratio: float = 0.70             # EAR < 기준EAR * ear_ratio 이면 '감음'
    ear_min_thr: float = 0.12           # 임계값 하한 (캘리브레이션 실패 대비)
    ear_max_thr: float = 0.28           # 임계값 상한
    eye_method: str = "ear"             # "ear" | "blend" | "both"(둘 중 하나라도 감김이면 감김)
    blink_score_thr: float = 0.55       # blendshape eyeBlink 점수 임계값
    eye_closed_warn_s: float = 1.2      # 연속 감김 경고
    eye_closed_danger_s: float = 2.5    # 연속 감김 위험
    perclos_window_s: float = 60.0      # PERCLOS 계산 창
    perclos_warn: float = 0.15          # 창 내 감김 비율 경고
    perclos_danger: float = 0.30

    # ---------------- 머리 숙임 ----------------
    head_pitch_sign: float = 1.0        # 고개를 숙일 때 pitch 가 줄어들면 -1 로 변경
    head_down_deg: float = 15.0         # 기준 대비 이만큼 숙이면 '숙임'
    head_down_warn_s: float = 1.5
    head_down_danger_s: float = 3.0
    face_lost_warn_s: float = 2.0       # 얼굴이 안 보이는 시간 (고개 푹 숙임/이탈)
    nod_window_s: float = 60.0          # 끄덕임(졸음 헤드드롭) 횟수 집계 창
    nod_warn_count: int = 3

    # ---------------- 하품 ----------------
    mar_thr: float = 0.60
    yawn_min_s: float = 1.0

    # ---------------- 손 / 핸들 ----------------
    enable_hands: bool = True
    wheel_ring_inner: float = 0.55      # 타원 안쪽 반경 비율 (림만 잡은 것으로 인정)
    wheel_margin: float = 0.15          # 바깥 여유 (반경 비율)
    wheel_point_ratio: float = 0.35     # 손 key point 중 이 비율 이상이 영역 안이면 파지
    require_curl: bool = False          # True면 손가락이 말려 있어야(쥐고 있어야) 파지 인정
    curl_thr: float = 1.6               # 손끝-손바닥중심 거리 / 손바닥 크기 (작을수록 쥔 상태)
    hands_off_warn_s: float = 2.0       # 두 손 모두 이탈 경고 (손이 화면에 안 보여도 이탈로 봄)
    hands_off_danger_s: float = 5.0
    hands_grace_s: float = 0.3          # 손 검출이 이보다 짧게 끊기면 무시 (깜빡임 유예)
    one_hand_caution_s: float = 3.0     # 한 손 운전이 이만큼 이어지면 CAUTION (경고음 없음)

    # ---------------- rPPG (심박) ----------------
    enable_rppg: bool = True
    rppg_fs: float = 30.0               # 재샘플링 주파수
    rppg_hr_window_s: float = 10.0      # 심박 추정 창 (길수록 안정적, 반응은 느림)
    rppg_hrv_window_s: float = 30.0     # HRV 창
    rppg_update_s: float = 1.0          # 추정 주기
    rppg_min_hz: float = 0.7            # 42 bpm
    rppg_max_hz: float = 3.0            # 180 bpm
    rppg_snr_thr: float = -2.0          # 이 SNR(dB) 이상이면 유효 (잡음만 있을 때 90%가 -3dB 이하)
    rppg_min_fps: float = 15.0          # 실제 카메라 FPS 가 이보다 낮으면 추정 안 함
    rppg_gap_s: float = 0.5             # 얼굴이 이만큼 끊기면 버퍼 초기화
    rppg_roi_mode: str = "forehead_first"   # "forehead"(이마만) | "forehead_first"(이마 우선, 필요할 때 볼 추가) | "all"(이마+양볼)
    rppg_skin_mask: bool = True         # 볼 피부색과 다른 픽셀(앞머리, 눈썹, 모자, 짙은 그림자) 제외
    rppg_forehead_min_px: int = 600     # 이마 유효 픽셀이 이보다 적은 상태가 rppg_gap_s 이어지면 볼 추가
    rppg_roi_fallback_s: float = 5.0    # 이마 SNR < rppg_snr_thr 가 이만큼 이어지면 볼 추가
    rppg_roi_recover_s: float = 5.0     # 이마가 다시 좋아진 상태(픽셀 충분 + SNR 정상)가 이만큼 이어지면 이마로 복귀
    rppg_hrv: bool = True               # HRV(RMSSD) 계산 (실험적)
    rppg_hrv_snr_thr: float = 8.0       # HRV 는 이 SNR 이상일 때만
    rppg_baseline_n: int = 30           # 기준 심박: 캘리브레이션 후 유효 추정 n개의 중앙값
    rppg_alert: bool = False            # True면 기준 대비 심박 저하를 졸음 경고에 반영
    hr_drop_pct: float = 10.0           # 기준 대비 이 % 이상 낮으면 '심박 저하'
    hr_low_s: float = 30.0              # 저하가 이만큼 지속되면 WARN

    # ---------------- Unity 차량 연동 (--unity) ----------------
    # 팀원 Unity 씬(unity_stage1_basic_vehicle) 기준 좌표: 도로 x=-6~3(차선 -1.5), 갓길 x=3~6, 길이 z≈-20~380
    unity_host: str = "127.0.0.1"       # 같은 PC 의 Unity 만 접속. 다른 PC 에서 Unity 를 돌리면 "0.0.0.0" (방화벽 허용 필요)
    unity_port: int = 5001              # 팀원 vehicle_tcp_server.py 와 같은 포트 (둘을 동시에 켜면 안 됨)
    unity_send_hz: float = 20.0
    cruise_kph: float = 30.0            # 정속 주행 목표 속도 (도로가 400m 라 빠르면 금방 끝남)
    lane_center_x: float = 0.75         # 오른쪽 차로 중앙
    shoulder_center_x: float = 4.5      # 갓길 중앙
    road_end_z: float = 350.0           # 정속 주행 중 이 지점을 넘으면 차로 안에서 자동 정차
    takeover_reasons: list = field(default_factory=lambda: ["EYES CLOSED", "HEAD DOWN", "FACE NOT VISIBLE"])
    takeover_after_s: float = 2.0       # 위 원인의 DANGER 가 이만큼 이어지면 개입 (DANGER 자체도 2.5~3초 필요)
    alert_s: float = 2.0                # 개입 후 감속 전 비상등 + 경고 시간. 이 동안 깨어나면 취소
    decel_kph_per_s: float = 6.0        # 단계적 감속 비율 (약 1.7 m/s²)
    pull_over: bool = True              # 감속하면서 갓길로 이동
    vehicle_id: str = "VIN-TEST-0001"
    virtual_lat: float = 35.8468        # Unity 원점의 가상 위도/경도 (MSD 표시용, 실제 GPS 아님)
    virtual_lon: float = 127.1294
    ecall_dir: str = "ecall_log"        # MSD(JSON) 저장 폴더. 외부로 보내지 않음

    # ---------------- 기타 ----------------
    smooth_alpha: float = 0.5           # EAR/pitch 지수 평활 계수
    beep: bool = True
    wheel_file: str = "wheel_roi.json"

    @classmethod
    def load(cls, path: str | None) -> "Config":
        cfg = cls()
        if path and Path(path).exists():
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            for k, v in data.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
                elif k == "one_hand_warn_s":
                    print("[config] one_hand_warn_s 는 없어짐: 한 손 운전은 one_hand_caution_s 뒤 CAUTION")
                else:
                    print(f"[config] 알 수 없는 키 무시: {k}")
        return cfg

    def save(self, path: str) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
