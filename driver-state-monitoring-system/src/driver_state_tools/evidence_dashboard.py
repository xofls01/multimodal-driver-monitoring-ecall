"""English evidence dashboard with live timers and an OUTSIDE status border."""
from functools import lru_cache
from pathlib import Path
import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

PALETTE = dict(normal=(93,224,154), caution=(255,195,62), danger=(255,78,99), unavailable=(147,163,180))


@lru_cache(maxsize=32)
def font(size, bold=False):
    paths = (["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"] if bold else
             ["/System/Library/Fonts/Supplemental/Arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"])
    p = next((p for p in paths if Path(p).exists()), None)
    return ImageFont.truetype(p,size) if p else ImageFont.load_default(size=size)


def evidence_content(derived, decision, rules, config=None):
    config = config or {}
    f = dict(decision.metadata.get("prototype_frame", {}))
    evidence = decision.metadata.get("evidence_values", {})
    if f.get("collapse_posture") is False and evidence and not (
            evidence.get("head_drop_observed") and evidence.get("torso_observed")):
        f["collapse_posture"] = None
    d = decision.durations
    meters = {}
    for k in ("eyes_closed", "attention_away", "collapse_posture", "torso_lean", "mouth_open", "hand_near_chest", "nod_proxy"):
        known = f.get("calibrated", False) and f.get(k) is not None
        meters[k] = dict(known=known, elapsed=d.get(k,0) if known else None,
                        limit=float(rules[k+"_caution_seconds"]), active=f.get(k) is True)
    meters["collapse_stop"] = dict(known=meters["collapse_posture"]["known"],
        elapsed=d.get("collapse_after_caution",0) if meters["collapse_posture"]["known"] else None,
        limit=float(rules["collapse_after_caution_seconds"]), active="collapse_posture" in decision.reasons)
    # Name only the currently supported component. The combined posture timer
    # cannot establish how long each individual component has been present.
    lowered = (evidence.get("head_drop_observed") and evidence.get("relative_head_drop") is not None
               and "collapse_head_drop_delta" in config
               and evidence["relative_head_drop"] >= config["collapse_head_drop_delta"])
    leaning = (evidence.get("torso_observed") and evidence.get("relative_torso") is not None
               and "collapse_lean_degrees" in config
               and abs(evidence["relative_torso"]) >= config["collapse_lean_degrees"])
    posture = ("Unable to read" if not meters["collapse_posture"]["known"] else
               "Upright" if f.get("collapse_posture") is False else
               "Head lowered and body leaning" if lowered and leaning else
               "Head lowered" if lowered else "Body leaning" if leaning else "Posture changed")
    names = dict(sustained_collapse_stop="Stop requested",
        collapse_posture=posture, eyes_closed=f"Eyes closed for {d.get('eyes_closed',0):.2f} seconds",
        attention_away=f"Looking away for {d.get('attention_away',0):.2f} seconds",
        attention_repeated=f"Looked away {int(d.get('attention_episode_count',0))} times recently",
        attention_cumulative=f"Looking away: {d.get('attention_window_seconds',0):.2f}s in the last {rules['attention_window_seconds']:g}s",
        mouth_open=f"Mouth open for {d.get('mouth_open',0):.2f} seconds",
        hand_near_chest=f"Hand near chest for {d.get('hand_near_chest',0):.2f} seconds",
        torso_lean=f"Body leaning for {d.get('torso_lean',0):.2f} seconds",
        nod_proxy="Eyes closed with head lowered",
        repeated_head_dips="Repeated head nods",repeated_long_closures="Repeated long eye closures",
        eye_ratio_high="High eye closure time over 60 seconds",recovery_pending="Checking return to normal",
        attention_recovery_unconfirmed="Looking ahead not yet confirmed",
        recovery_unobserved="Unable to confirm recovery",calibrating="Getting ready to monitor",
        insufficient_observation="Unable to assess driver state")
    priorities = ("sustained_collapse_stop", "collapse_posture", "eyes_closed", "attention_away")
    reason = next((r for r in priorities if r in decision.reasons), decision.reasons[0] if decision.reasons else None)
    default_reason = ("Eyes closed - checking duration" if decision.metadata.get("decision_phase") == "evaluating" else
                      "No warning triggered" if decision.state.value == "normal" else
                      "Unable to assess driver state" if decision.state.value == "unavailable" else "Driver warning active")
    eyes_closed = decision.metadata.get("attention_status") == "not_assessed_eyes_closed"
    source = decision.metadata.get("attention_source", "unavailable").upper()
    attention_note = ("Attention not assessed while eyes are closed" if eyes_closed else
                      "Unable to assess where the driver is looking" if not meters["attention_away"]["known"] else
                      "Estimated from head position and eye landmarks" if "IRIS" in source else
                      "Estimated from head and shoulder position" if "POSE" in source else
                      "Estimated from head position")
    stop_meter = meters["collapse_stop"]
    remaining = None
    if decision.metadata.get("stop_requested") and decision.state.value == "danger":
        stop_text, stop_note = "Stop requested", "Simulator stop condition reached"
    elif stop_meter["known"] and stop_meter["active"] and decision.state.value == "caution":
        remaining = max(0.,stop_meter["limit"]-stop_meter["elapsed"])
        stop_text, stop_note = f"Stop request in {remaining:.2f}s", "If the same posture continues"
    elif not stop_meter["known"]:
        stop_text, stop_note = "Unable to assess stop countdown", "Posture must be readable to count time"
    else:
        stop_text, stop_note = "No stop countdown", "Other warnings do not add to this timer"
    return dict(meters=meters, reason=names.get(reason,default_reason),
        eye_label="Unable to read" if not meters["eyes_closed"]["known"] else "Closed" if f.get("eyes_closed") else "Open",
        attention_label="Eyes closed" if eyes_closed else
                        "Unable to assess" if not meters["attention_away"]["known"] else "Looking away" if f.get("attention_away") else "Looking ahead",
        attention_note=attention_note, posture_label=posture,
        stop_text=stop_text, stop_note=stop_note, stop_remaining_seconds=remaining,
        episodes=int(d.get("attention_episode_count",0)), episode_limit=int(rules["attention_repeat_count"]),
        attention_total=d.get("attention_window_seconds",0) if meters["attention_away"]["known"] else None,
        window=float(rules["attention_window_seconds"]), cumulative_limit=float(rules["attention_cumulative_seconds"]),
        dips=int(f.get("head_dip_count",0)), long_closures=int(f.get("long_closure_count",0)),
        attention_source=decision.metadata.get("attention_source", "unavailable"))


def draw_evidence_overlay(frame, raw, derived, decision, rppg, width, config):
    from .dashboard import _draw_baseline_overlay
    rules = config["decision_rules"]
    # Existing landmarks retain their per-feature colors. Only the rail is new.
    base_cfg = dict(config, decision_rules={**rules,"danger_policy":"external_response"})
    existing = _draw_baseline_overlay(frame,raw,derived,decision,rppg,width,base_cfg)
    h,w = frame.shape[:2]
    c = evidence_content(derived,decision,rules,config)
    f = decision.metadata.get("prototype_frame", {})
    accent = PALETTE[decision.state.value]
    im = Image.new("RGB",(460,720),(11,19,28));p=ImageDraw.Draw(im)
    muted,white = (142,163,183),(235,243,249)
    def text(s,x,y,size=14,color=white,bold=False,max_width=None):
        s=str(s);max_width=max_width or 440-x
        while p.textlength(s,font=font(size,bold))>max_width and size>10: size-=1
        p.text((x,y),s,font=font(size,bold),fill=color)
    def row(label,value,y,color=white):
        text(label,24,y,11,muted)
        text(value,245,y-2,15,color,True,max_width=190)
    def section(label,y,color):
        p.line((24,y-12,436,y-12),fill=(39,56,72))
        text(label,24,y,13,color,True)
    def track(value,limit,y,color,known=True):
        p.rounded_rectangle((24,y,436,y+5),radius=2,fill=(33,47,64))
        if known and value is not None and value>0:
            p.rounded_rectangle((24,y,24+max(3,412*min(value/limit,1)),y+5),radius=2,fill=color)
    def bar(label,value,limit,y,color,known=True):
        text(f"{label} {value:.2f}s" if known and value is not None else "Unable to read duration",24,y,12,white,max_width=250)
        text(f"Warning at {limit:g}s",290,y,11,muted,max_width=146)
        track(value,limit,y+21,color,known)
    def meter(key,label,y,color):
        m=c["meters"][key];bar(label,m["elapsed"],m["limit"],y,color,m["known"])
    text("DRIVER MONITOR",24,18,14,white,True)
    text(f"Video {decision.timestamp:05.2f}s",330,18,12,muted)
    text(decision.state.value.upper(),24,46,36,accent,True)
    text(c["reason"],24,94,14,accent,True)
    # A dark badge distinguishes state from sensor coverage.
    ready=c["meters"]
    labels=[("Eyes",ready["eyes_closed"]["known"]),("Attention",ready["attention_away"]["known"]),("Posture",ready["collapse_posture"]["known"])]
    for i,(label,known) in enumerate(labels):
        x=24+i*140;p.ellipse((x,122,x+5,127),fill=(87,211,173) if known else (76,91,108));text(label+": "+("readable" if known else "unavailable"),x+12,117,10,muted,max_width=125)

    section("01 / EYES",153,(134,227,180))
    closed=f.get("eyes_closed") if ready["eyes_closed"]["known"] else None
    row("Eye state", c["eye_label"],180,
        (255,195,62) if closed else white)
    meter("eyes_closed","Eyes closed for",205,(134,227,180))
    text("Head nods (last 30s)",24,241,11,muted)
    text(f"{c['dips']} "+("time" if c['dips']==1 else "times")+f"; repeat at {rules['head_dip_repeat_count']}",245,240,12,white)
    text("Long eye closures (last 30s)",24,258,11,muted)
    text(f"{c['long_closures']} "+("time" if c['long_closures']==1 else "times")+f"; repeat at {rules['long_closure_repeat_count']}",245,257,12,white)

    section("02 / DRIVER ATTENTION",282,(92,199,246))
    away=f.get("attention_away") if ready["attention_away"]["known"] else None
    row("Attention", c["attention_label"],308)
    meter("attention_away","Looking away for",333,(92,199,246))
    text(f"Total away (last {c['window']:g}s): "+(f"{c['attention_total']:.2f}s" if c['attention_total'] is not None else "unavailable"),24,371,11,white,max_width=255)
    text(f"Warning at {c['cumulative_limit']:g}s",290,371,11,muted)
    track(c["attention_total"],c["cumulative_limit"],392,(92,199,246),away is not None)
    text("Times looking away",24,411,10,muted)
    for i in range(c["episode_limit"]):
        x=156+i*18;p.rounded_rectangle((x,411,x+11,420),radius=2,fill=(92,199,246) if i<c["episodes"] else (45,63,81))
    text(f"{c['episodes']} "+("time" if c['episodes']==1 else "times")+f"; repeat at {c['episode_limit']}",230,408,12)
    text(c["attention_note"],24,426,10,muted)

    section("03 / POSTURE",451,(185,167,245))
    collapse=f.get("collapse_posture") if ready["collapse_posture"]["known"] else None
    row("Posture", c["posture_label"],477)
    meter("collapse_posture","Posture change for",502,(185,167,245))
    text(c["stop_text"],24,540,16,accent,True,max_width=300)
    text(f"After {ready['collapse_stop']['limit']:g}s",350,543,10,muted,max_width=86)
    m=ready["collapse_stop"]
    track(m["elapsed"],m["limit"],561,accent,m["known"])
    text(c["stop_note"],24,577,10,muted)

    section("04 / OTHER OBSERVATIONS",612,(245,186,108))
    for key,label,y in (("mouth_open","Mouth open",638),("hand_near_chest","Hand near chest",658),("torso_lean","Body leaning",678)):
        m=ready[key];text(label,24,y,10,muted)
        value=f"{m['elapsed']:.2f}s; alert at {m['limit']:g}s" if m["known"] else "Unable to read"
        text(value,146,y-2,11,max_width=125)
        p.rounded_rectangle((278,y+2,353,y+6),radius=2,fill=(38,54,69))
        if m["known"] and m["elapsed"]>0:
            p.rounded_rectangle((278,y+2,278+max(3,75*min(1,m['elapsed']/m['limit'])),y+6),radius=2,fill=(245,186,108))
    pulse=(f"{rppg.heart_rate_bpm:.0f} BPM" if rppg and rppg.status=="ready" and rppg.heart_rate_bpm else
           "Collecting" if rppg and rppg.status in ("collecting","calibrating") else "Low signal")
    text("Estimated",370,633,9,muted)
    text("pulse",370,645,10,muted)
    text(pulse if rppg and rppg.status!="disabled" else "Not used",370,661,11,max_width=66)
    text("PROTOTYPE / NOT A MEDICAL DIAGNOSIS",24,703,9,muted)
    rail=np.asarray(im.resize((existing.shape[1]-w,h),Image.Resampling.LANCZOS))[:,:,::-1].copy()
    output=np.concatenate((existing[:,:w],rail),axis=1)
    border=round(int(config.get("output_border_pixels",8))*h/720)
    return cv2.copyMakeBorder(output,border,border,border,border,cv2.BORDER_CONSTANT,value=accent[::-1])
