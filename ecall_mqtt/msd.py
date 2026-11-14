"""MSD(Minimum Set of Data) 생성과 검사.
연구용 테스트 메시지이며 test_call은 항상 true다. 119·관제센터로 보내지 않는다."""
import datetime
import uuid

MSD_VERSION = 1
STATES = {"NORMAL", "WARNING", "VERIFYING", "EMERGENCY", "SENSOR_ERROR"}


def build_msd(trigger, cfg):
    """trigger : 비전(PC)에서 보낸 요청 {position, direction_deg, state, reason, hr_bpm}
    cfg     : config.json (차량·차주 정보는 가상 값)"""
    pos = trigger.get("position") or {}
    unity = trigger.get("unity_position")
    position = {
        "lat": pos.get("lat"),
        "lon": pos.get("lon"),
        "confidence": pos.get("confidence", "simulated"),
    }
    if isinstance(unity, dict):                              # 시뮬레이터(Unity) 좌표(m)
        position["unity"] = {k: unity.get(k) for k in ("x", "y", "z")}
    return {
        "msd_version": MSD_VERSION,
        "msd_id": str(uuid.uuid4()),
        "test_call": True,                                   # 항상 테스트 표시
        "activation": trigger.get("activation", "automatic"),
        "time_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "time_sync": cfg.get("time_sync", "unknown"),
        "position": position,
        "direction_deg": trigger.get("direction_deg"),
        "vehicle": cfg["vehicle"],
        "owner": cfg["owner"],
        "driver_state": {
            "state": trigger.get("state", "EMERGENCY"),
            "reason": trigger.get("reason", []),
            "hr_bpm": trigger.get("hr_bpm"),                 # 현재 심박. 측정값 없으면 None 그대로
            # 아래는 trigger 에 있을 때만 (send_test_trigger.py 처럼 없으면 MSD 에도 없음)
            **{k: trigger[k] for k in VITAL_KEYS if k in trigger},
        },
    }


# 맥박 상태와 마지막 확인 심박 (PC ecall_link.VitalsTracker 가 만든다)
VITAL_KEYS = ("pulse_status", "hr_last_bpm", "hr_last_age_s", "hr_last_source", "hr_last_verified")
PULSE_STATUSES = {"detected", "not_detected_unconfirmed", "not_detected_confirmed", "unknown"}
PULSE_KO = {"not_detected_unconfirmed": "맥박 미검출(센서 기준, 미확정)",
            "not_detected_confirmed": "맥박 미검출(센서 기준)"}
HR_SOURCE_KO = {"ecg_peaks": "ECG", "ppg_peaks": "PPG", "model": "모델 확인값"}


def hr_band(bpm):
    """숫자만 보고 붙이는 참고 표시. 진단이 아니다."""
    if bpm >= 150:
        return "심한 빈맥"
    if bpm > 100:
        return "빈맥"
    if bpm <= 40:
        return "심한 서맥"
    if bpm < 60:
        return "서맥"
    return "정상 범위"


REASON_KO = {
    "head_down": "고개 숙임",
    "no_response": "경고 무응답",
    "no_button_response": "버튼 무응답",
    "unwell_posture": "몸이 불편한 모습",
    "slumped_forward": "핸들 앞으로 쓰러짐",
    "slumped_side": "측면으로 쓰러짐",
    "body_tilt": "몸 기울어짐",
    "hr_spike": "심박 급상승",
    "hr_drop": "심박 급감소",
    "hr_missing": "맥박 미검출(센서)",
    "eyes_closed": "눈 감김",
    "manual_sos": "운전자 SOS 버튼",
}
STATE_KO = {"NORMAL": "정상", "WARNING": "주의", "VERIFYING": "확인 중",
            "EMERGENCY": "응급", "SENSOR_ERROR": "센서 오류"}
DIRS = ["북", "북동", "동", "남동", "남", "남서", "서", "북서"]


def format_msd_text(m):
    """MSD를 사람이 읽기 좋은 한국어 알림 문장으로 바꾼다."""
    t = datetime.datetime.strptime(m["time_utc"], "%Y-%m-%dT%H:%M:%SZ")
    kst = t + datetime.timedelta(hours=9)
    ds = m["driver_state"]
    pos = m["position"]
    v, o = m["vehicle"], m["owner"]
    reasons = ", ".join(REASON_KO.get(r, r) for r in ds.get("reason", [])) or "없음"
    pulse = ds.get("pulse_status")
    if ds.get("hr_bpm") is not None:
        hr = f"{ds['hr_bpm']:.0f} bpm ({hr_band(ds['hr_bpm'])})"
    elif pulse in PULSE_KO:
        hr = f"현재 측정 안 됨 — {PULSE_KO[pulse]}"
    else:
        hr = "측정값 없음"
    hr_lines = [f"심박 : {hr}"]
    last = ds.get("hr_last_bpm")
    if last is not None and ds.get("hr_bpm") is None:
        age = ds.get("hr_last_age_s")
        when = f"정차 약 {age:.0f}초 전" if isinstance(age, (int, float)) else "시각 미상"
        src = HR_SOURCE_KO.get(ds.get("hr_last_source"), ds.get("hr_last_source") or "")
        check = "" if ds.get("hr_last_verified") else "·미검증"
        hr_lines.append(f"마지막 확인 심박 : {last:.0f} bpm ({hr_band(last)}, {when}, {src}{check})")
    d = m.get("direction_deg")
    direction = f"{DIRS[int((d + 22.5) // 45) % 8]} ({d:.0f}°)" if d is not None else "알 수 없음"
    conf = "시뮬레이션" if pos.get("confidence") == "simulated" else pos.get("confidence", "")
    how = "자동 발신" if m["activation"] == "automatic" else "수동 발신"
    head = "[테스트] e-Call 긴급 알림" if m.get("test_call") else "e-Call 긴급 알림"
    unity = pos.get("unity") or {}
    if unity:
        def c(v):
            return f"{v:.2f}" if isinstance(v, (int, float)) else "-"
        place = [f"위치(Unity) : X {c(unity.get('x'))}, Y {c(unity.get('y'))}, Z {c(unity.get('z'))} (m)",
                 f"가상 위경도 : {pos['lat']:.5f}, {pos['lon']:.5f} ({conf})"]
    else:
        place = [f"위치 : {pos['lat']:.5f}, {pos['lon']:.5f} ({conf})"]
    return "\n".join([
        head,
        "",
        f"시각 : {kst:%Y-%m-%d %H:%M:%S} (한국 시간)",
        f"상태 : {STATE_KO.get(ds['state'], ds['state'])} ({how})",
        f"사유 : {reasons}",
        *hr_lines,
        "",
        *place,
        f"지도 : https://www.google.com/maps?q={pos['lat']},{pos['lon']}",
        f"진행 방향 : {direction}",
        "",
        f"차량 : {v.get('model')} / {v.get('plate')}",
        f"차주 : {o.get('name')} ({o.get('phone')})",
        f"MSD ID : {m['msd_id']}",
    ])


def validate_msd(m):
    """문제 목록을 돌려준다. 빈 리스트면 통과."""
    errs = []
    for k in ("msd_version", "msd_id", "test_call", "activation", "time_utc",
              "position", "vehicle", "owner", "driver_state"):
        if k not in m:
            errs.append(f"필수 필드 없음: {k}")
    if m.get("test_call") is not True:
        errs.append("test_call은 반드시 true")
    if m.get("activation") not in ("automatic", "manual"):
        errs.append("activation은 automatic 또는 manual")
    pos = m.get("position") or {}
    lat, lon = pos.get("lat"), pos.get("lon")
    if not isinstance(lat, (int, float)) or not -90 <= lat <= 90:
        errs.append(f"위도 범위 오류: {lat}")
    if not isinstance(lon, (int, float)) or not -180 <= lon <= 180:
        errs.append(f"경도 범위 오류: {lon}")
    unity = pos.get("unity")
    if unity is not None:
        if not isinstance(unity, dict):
            errs.append(f"Unity 좌표 형식 오류: {unity}")
        else:
            for k in ("x", "y", "z"):
                v = unity.get(k)
                if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
                    errs.append(f"Unity {k} 좌표 오류: {v}")
    d = m.get("direction_deg")
    if d is not None and not (isinstance(d, (int, float)) and 0 <= d < 360):
        errs.append(f"방향 범위 오류(0~359): {d}")
    st = (m.get("driver_state") or {}).get("state")
    if st not in STATES:
        errs.append(f"알 수 없는 상태: {st}")
    ds = m.get("driver_state") or {}
    for key in ("hr_bpm", "hr_last_bpm"):
        hr = ds.get(key)
        if hr is not None and not (isinstance(hr, (int, float)) and not isinstance(hr, bool) and 0 < hr < 300):
            errs.append(f"심박 범위 오류({key}): {hr}")
    if "pulse_status" in ds and ds["pulse_status"] not in PULSE_STATUSES:
        errs.append(f"알 수 없는 맥박 상태: {ds['pulse_status']}")
    age = ds.get("hr_last_age_s")
    if age is not None and not (isinstance(age, (int, float)) and age >= 0):
        errs.append(f"마지막 심박 시각 오류: {age}")
    return errs
