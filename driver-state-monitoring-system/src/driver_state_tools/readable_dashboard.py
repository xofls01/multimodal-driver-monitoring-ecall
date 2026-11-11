"""Presentation only: readable labels without changing observations or decisions."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont


FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
    "C:/Windows/Fonts/malgun.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
)
OBJECT_NAMES = dict(zip(
    ("cell phone", "phone", "bottle", "wine glass", "cup", "fork", "spoon", "bowl",
     "banana", "apple", "sandwich", "orange", "broccoli", "carrot", "hot dog",
     "pizza", "donut", "cake", "book"),
    ("휴대전화", "휴대전화", "병", "와인잔", "컵", "포크", "숟가락", "그릇",
     "바나나", "사과", "샌드위치", "오렌지", "브로콜리", "당근", "핫도그",
     "피자", "도넛", "케이크", "책")))


@lru_cache(maxsize=16)
def resolve_font(configured: str | None = None) -> str | None:
    if configured:
        if not Path(configured).is_file():
            raise ValueError(f"Dashboard font does not exist: {configured}")
        return configured
    return next((p for p in FONT_CANDIDATES if Path(p).is_file()), None)


@lru_cache(maxsize=64)
def _font(path: str | None, size: int):
    return ImageFont.truetype(path, size) if path else ImageFont.load_default(size=size)


def build_panel_content(raw: Any, derived: Any, decision: Any,
                        rules: dict[str, Any], rppg: Any = None,
                        language: str = "ko") -> dict[str, Any]:
    """Return UI text. Unknown observations never become zero/negative evidence."""
    def tr(ko, en):
        return ko if language == "ko" else en
    def sec(value):
        return tr(f"{value:.2f}초", f"{value:.2f}s")
    m, d = decision.metadata, decision.durations
    statuses = m.get("feature_status", {})
    eye_known = (statuses.get("eyes") == "ready" and derived.eyes_readable
                 and derived.eyes_closed is not None)
    gaze_known = statuses.get("gaze") == "ready" and m.get("gaze") in ("forward", "away")
    nod_known = statuses.get("nod") == "ready"
    mouth_known = statuses.get("mouth") == "ready" and derived.mouth_open is not None
    eyes_limit = float(rules["eyes_closed_caution_seconds"])
    away_limit = float(rules["attention_away_caution_seconds"])
    window = float(rules["attention_window_seconds"])
    total_limit = float(rules["attention_cumulative_seconds"])
    ratio_window = float(rules["eye_ratio_window_seconds"])
    reasons = {
        "eyes_closed": tr(f"눈 감김이 {sec(d.get('eyes_closed', 0))} 이어졌어요",
                          f"Eyes closed for {sec(d.get('eyes_closed', 0))}"),
        "prolonged_eye_closure": tr("눈 감김이 길게 이어지고 있어요", "Eye closure continues"),
        "validated_eye_closure_6s": tr("검증된 눈 감김 정차 조건에 도달했어요", "Validated eye-closure stop condition"),
        "attention_away": tr(f"전방 이탈이 {sec(d.get('attention_away', 0))} 이어졌어요",
                             f"Looking away for {sec(d.get('attention_away', 0))}"),
        "attention_away_cumulative": tr("최근 구간의 전방 이탈 시간이 기준을 넘었어요", "Total time looking away exceeded the limit"),
        "away_after_displayed_warning": tr("경고가 표시된 뒤에도 전방 이탈이 이어졌어요", "Still looking away after displayed warning"),
        "nod_suspected": tr("꾸벅임으로 의심되는 머리 움직임이 보여요", "Possible head nod observed"),
        "nod_completed": tr("고개가 내려갔다 돌아오는 움직임이 확인됐어요", "Completed head nod observed"),
        "sustained_mouth_opening": tr("입을 크게 벌린 상태가 이어지고 있어요", "Mouth remains open"),
        "long_term_eye_closure_ratio": tr("최근 구간의 눈 감김 비율이 기준을 넘었어요", "Eye-closure ratio exceeded the limit"),
        "recovering": tr("경고 조건 해소와 정상 복귀를 확인하고 있어요", "Checking recovery from the warning"),
        "recovery_unobserved": tr("경고 원인이 해소됐는지 확인할 수 없어요", "Cannot verify recovery from the warning"),
        "required_observation_unavailable": tr(
            "전방 주시를 확인할 수 없어요" if eye_known else "눈 상태를 확인할 수 없어요" if gaze_known else "눈 상태와 전방 주시 확인이 필요해요",
            "Attention is unavailable" if eye_known else "Eye state is unavailable" if gaze_known else "Eye state and attention are unavailable"),
    }
    priority = ("away_after_displayed_warning", "validated_eye_closure_6s")
    chosen = next((r for r in priority if r in decision.reasons),
                  decision.reasons[0] if decision.reasons else None)
    reason = reasons.get(chosen, tr("판단 근거는 상세 로그에서 확인할 수 있어요", "See the decision log for details")) if chosen else tr(
        "현재 관측에서 경고 조건이 없어요", "No warning condition in current observations")
    state = decision.state.value
    state_label = {
        "normal": tr("정상", "NORMAL"), "caution": tr("주의", "CAUTION"),
        "danger": tr("위험 / 정차 요청", "DANGER / STOP REQUEST"),
        "unavailable": tr("판단 불가", "UNAVAILABLE"),
    }[state]
    eye_state = (tr("눈 감김", "Closed") if derived.eyes_closed else tr("눈 뜸", "Open")) if eye_known else (
        tr("기준 설정 필요", "Calibration needed") if statuses.get("eyes") == "needs_calibration" else tr("확인 불가", "Unavailable"))
    nod = m.get("nod_event", {})
    nod_label = tr("보정 필요", "Calibration needed")
    if nod_known:
        nod_label = (tr("꾸벅임 확인", "Nod completed") if nod.get("completed") else
                     {"descending": tr("고개 내려가는 중", "Head descending"),
                      "below_lower": tr("고개 숙임 유지", "Head remains lowered"),
                      "recovering": tr("고개 돌아오는 중", "Head returning")}.get(nod.get("phase"),
                         tr("꾸벅임 의심", "Possible nod") if nod.get("suspected") else tr("현재 꾸벅임 없음", "No current nod")))
    elif statuses.get("nod") == "unavailable":
        nod_label = tr("확인 불가", "Unavailable")
    mouth_label = (tr("입 벌림", "Open") if derived.mouth_open else tr("입 다묾", "Closed")) if mouth_known else (
        tr("확인 불가", "Unavailable") if statuses.get("mouth") == "unavailable" else tr("기준 설정 필요", "Calibration needed"))
    ratio = m.get("eye_ratio", {})
    if ratio.get("status") == "collecting":
        ratio_label = tr(f"{ratio_window:g}초 구간 수집 중 ({ratio.get('current_window_collected_seconds', 0):.1f}초)",
                         f"Collecting {ratio.get('current_window_collected_seconds', 0):.1f}/{ratio_window:g}s")
    elif ratio.get("status") in ("high", "low", "unknown"):
        # Bounds retain unknown time. Never report a single confident percentage.
        bounds = f"{ratio.get('lower_ratio', 0)*100:.0f}~{ratio.get('upper_ratio', 1)*100:.0f}%"
        label = {"high": tr("기준 이상", "High"), "low": tr("기준 미만", "Low"),
                 "unknown": tr("판단 보류", "Uncertain")}[ratio["status"]]
        ratio_label = f"{bounds} ({label})"
    else:
        ratio_label = tr("확인 불가", "Unavailable")
    objects = (["cell phone"] if raw.phone_confidence is not None else []) + [o["label"] for o in raw.handheld_objects]
    objects = list(dict.fromkeys(OBJECT_NAMES.get(o, "기타 물체") if language == "ko" else o.replace("_", " ") for o in objects))
    object_label = ", ".join(objects[:2]) + (tr(f" 외 {len(objects)-2}개", f" +{len(objects)-2}") if len(objects) > 2 else "")
    result = dict(
        state=state, state_label=state_label, reason=reason,
        title=tr("운전자 상태", "DRIVER STATE"), time=tr(f"영상 {decision.timestamp:.2f}초", f"Video {decision.timestamp:.2f}s"),
        setup=tr("전체 기능 연결 완료" if m.get("full_system_ready") else "개발 중 / 일부 기능 준비 필요",
                 "Setup complete" if m.get("full_system_ready") else "Development / partial setup"),
        eye_known=eye_known, eye_state=eye_state,
        eye_time=sec(d.get("eyes_closed", 0)) if eye_known else "--",
        eye_seconds=d.get("eyes_closed", 0) if eye_known else None,
        eye_limit=eyes_limit, eye_rule=tr(f"연속 {eyes_limit:g}초 이상이면 주의", f"Caution after {eyes_limit:g}s continuous closure"),
        eye_stop=tr("눈 감김 정차 조건: 이 영상에서 검증됨" if m.get("eye_danger_validated") else "눈 감김 정차 조건: 검증 전",
                    "Eye stop condition: validated for this clip" if m.get("eye_danger_validated") else "Eye stop condition: not validated"),
        gaze_known=gaze_known,
        gaze_state=tr("전방 주시" if m.get("gaze") == "forward" else "전방 이탈", "Forward" if m.get("gaze") == "forward" else "Away") if gaze_known else tr("확인 불가", "Unavailable"),
        away_time=sec(d.get("attention_away", 0)) if gaze_known else "--",
        away_total=sec(d.get("attention_window_seconds", 0)) if gaze_known else "--",
        away_seconds=d.get("attention_away", 0) if gaze_known else None, away_limit=away_limit,
        away_window_label=tr(f"최근 {window:g}초 누적 이탈", f"Total away / {window:g}s"),
        gaze_rule=tr(f"{away_limit:g}초 연속 또는 {window:g}초 중 {total_limit:g}초면 주의",
                     f"Caution: {away_limit:g}s continuous or {total_limit:g}s in {window:g}s"),
        gaze_note=tr("시선 모델 연결이 필요해요", "Gaze model needs connecting") if statuses.get("gaze") == "model_not_connected" else (
            tr("시선 모델과 입력 상태를 확인해주세요", "Check gaze model and input") if not gaze_known else
            tr("경고 표시 확인 대기", "Waiting for warning-display acknowledgement") if m.get("warning_request_id") and m.get("warning_issued_at") is None else
            tr(f"경고 후 이탈 {sec(d.get('after_displayed_warning', 0))}", f"Away after warning {sec(d.get('after_displayed_warning', 0))}") if m.get("warning_issued_at") is not None else ""),
        nod_state=nod_label, nod_count=str(derived.nod_count) if nod_known else "--",
        mouth_state=mouth_label, eye_ratio=ratio_label,
        objects=object_label or tr("현재 확인된 후보 없음", "No confirmed candidate"),
        chest=tr("확인 불가", "Unavailable") if derived.distress_gesture is None else tr("관측됨", "Observed") if derived.distress_gesture else tr("관측되지 않음", "Not observed"),
        rppg=None,
    )
    if rppg and rppg.status != "disabled":
        result["rppg"] = (f"{rppg.heart_rate_bpm:.0f} BPM" if rppg.status == "ready" and rppg.heart_rate_bpm is not None else
                          tr("계산 중", "Collecting") if rppg.status in ("calibrating", "collecting") else tr("신호 불안정", "Low signal"))
    return result


def draw_readable_panel(height: int, width: int, raw: Any, derived: Any, decision: Any,
                        rules: dict[str, Any], rppg: Any = None,
                        ui_config: dict[str, Any] | None = None) -> np.ndarray:
    """Return a BGR rail. Never touches source image or decision objects."""
    cfg = ui_config or {}
    font_path = resolve_font(cfg.get("ui_font_path"))
    ko = font_path is not None and cfg.get("ui_language", "ko") == "ko"
    c = build_panel_content(raw, derived, decision, rules, rppg, "ko" if ko else "en")
    def tr(k, e):
        return k if ko else e
    im = Image.new("RGB", (420, 720), (15, 23, 27))
    draw = ImageDraw.Draw(im)
    muted, white = (153, 170, 179), (239, 244, 246)
    accent = {"normal": (105, 226, 147), "caution": (253, 194, 62),
              "danger": (250, 87, 89), "unavailable": (183, 165, 157)}[c["state"]]

    def text(value, x, y, size=16, color=white, maximum=None):
        maximum = maximum or 394-x
        font = _font(font_path, size)
        while draw.textlength(value, font=font) > maximum and size > 11:
            size -= 1
            font = _font(font_path, size)
        # Ellipsis is preferable to drawing outside the rail for arbitrary labels.
        while value and draw.textlength(value, font=font) > maximum:
            value = value[:-2] + "…" if ko else value[:-4] + "..."
        draw.text((x, y), value, font=font, fill=color)

    def line(y):
        draw.line((24, y, 396, y), fill=(49, 64, 70))
    def row(label, value, y, color=white):
        text(label, 24, y, 14, muted, 175)
        text(value, 208, y-1, 16, color, 188)
    def meter(value, limit, y, color):
        draw.rounded_rectangle((24, y, 396, y+4), radius=2, fill=(43, 58, 65))
        if value is not None and value > 0:
            draw.rounded_rectangle((24, y, 24+max(3, 372*min(1, value/limit)), y+4), radius=2, fill=color)

    draw.rectangle((0, 0, 4, 720), fill=accent)
    text(c["title"], 24, 18, 17)
    text(c["time"], 270, 21, 13, muted)
    text(c["state_label"], 24, 52, 34, accent)
    text(c["reason"], 24, 101, 16, accent)
    text(c["setup"], 24, 128, 13, muted)
    line(157)

    text(tr("01  눈 상태", "01  EYE STATE"), 24, 174, 18, (149, 239, 121))
    row(tr("현재 눈 상태", "Current eyes"), c["eye_state"], 207)
    row(tr("연속 눈 감김", "Continuous closure"), c["eye_time"], 235)
    meter(c["eye_seconds"], c["eye_limit"], 264, (149, 239, 121))
    text(c["eye_rule"], 24, 277, 13, muted)
    text(c["eye_stop"], 24, 298, 13, muted)
    line(326)

    text(tr("02  전방 주시", "02  DRIVER ATTENTION"), 24, 342, 18, (60, 207, 245))
    row(tr("현재 주시 상태", "Current attention"), c["gaze_state"], 375)
    row(tr("연속 전방 이탈", "Continuous away"), c["away_time"], 403)
    row(c["away_window_label"], c["away_total"], 431)
    text(c["gaze_rule"], 24, 459, 13, muted)
    text(c["gaze_note"], 24, 480, 13, (60, 207, 245))
    line(508)

    text(tr("03  꾸벅임과 입 벌림", "03  NODS AND MOUTH"), 24, 524, 18, (182, 155, 245))
    row(tr("꾸벅임", "Head nod"), c["nod_state"], 557)
    row(tr("입 벌림", "Mouth"), c["mouth_state"], 585)
    line(613)
    # Keep auxiliary observations compact and separate from warning conditions.
    row(tr("물체 후보", "Object candidates"), c["objects"], 628)
    row(tr("가슴 부근 손", "Hand near chest"), c["chest"], 651)
    text(tr("눈 감김 비율  ", "Eye-closure ratio  ")+c["eye_ratio"], 24, 675, 12, muted)
    if c["rppg"] is not None:
        text(tr("영상 맥박 추정  ", "Camera pulse estimate  ")+c["rppg"]+tr(" / 판단에 미사용", " / separate"), 24, 695, 12, muted)
    else:
        text(tr("개발용 시뮬레이터 / 의료 진단용 아님", "Development simulator / not medical diagnosis"), 24, 695, 12, muted)
    if (width, height) != (420, 720):
        im = im.resize((width, height), Image.Resampling.LANCZOS)
    return np.asarray(im)[:, :, ::-1].copy()


def replace_panel(scene: np.ndarray, raw: Any, derived: Any, decision: Any,
                  rules: dict[str, Any], rppg: Any = None, width: int = 420,
                  ui_config: dict[str, Any] | None = None) -> np.ndarray:
    panel = draw_readable_panel(scene.shape[0], width, raw, derived, decision, rules, rppg, ui_config)
    return np.concatenate((scene, panel), axis=1)
