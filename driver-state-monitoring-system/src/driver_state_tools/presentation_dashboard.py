"""Consistent three-section presentation of existing vision evidence."""
import cv2
import numpy as np
from PIL import Image, ImageDraw
from driver_state_tools.evidence_dashboard import evidence_content, font, PALETTE
from driver_state_tools.multimodal_dashboard import measurement_display, brief_bio_reason
from driver_state_tools.bpm_display import bpm_range

def presentation_sections(decision,config):
    """UI view only. Select existing observations; never change a decision or timer."""
    rules=config['decision_rules'];c=evidence_content(None,decision,rules,config)
    meters=c['meters'];f=decision.metadata.get('prototype_frame',{})
    preparing=not f.get('calibrated',False) and decision.metadata.get('calibration',{}).get('status')=='calibrating'
    missing='Preparing' if preparing else 'Not observed'
    def state(label,known):return label if known else missing
    eyes=dict(title='01 / EYE STATE',focus='Eye closure and head nods',color=(134,227,180),
              state=state(c['eye_label'],meters['eyes_closed']['known']),primary_label='Closed time',primary=meters['eyes_closed'])
    attention_state=c['attention_label']
    if decision.metadata.get('attention_status')=='not_assessed_eyes_closed':attention_state='Not assessed (eyes closed)'
    elif not meters['attention_away']['known']:attention_state=missing
    attention=dict(title='02 / DRIVER ATTENTION',focus='Time away and repeated glances',color=(92,199,246),
                   state=attention_state,primary_label='Away time',primary=meters['attention_away'])
    posture_meter=meters['collapse_posture'];posture_label=c['posture_label'];posture_primary='Posture change'
    # A supported body-lean warning can be present below the stronger collapse threshold.
    # Show that already-computed observation instead of calling the posture upright.
    if meters['torso_lean']['known'] and meters['torso_lean']['active'] and not meters['collapse_posture']['active']:
        posture_meter=meters['torso_lean'];posture_label='Body leaning';posture_primary='Body lean time'
    posture=dict(title='03 / POSTURE',focus='Head lowering and body leaning',color=(185,167,245),
                 state=state(posture_label,posture_meter['known']),primary_label=posture_primary,primary=posture_meter)
    return c,[eyes,attention,posture]

def draw_large_rail(decision,config,rppg=None):
    c,sections=presentation_sections(decision,config);meters=c['meters']
    im=Image.new('RGB',(624,720),(11,19,28));p=ImageDraw.Draw(im)
    muted,white=(142,163,183),(235,243,249);accent=PALETTE[decision.state.value]
    def text(s,x,y,size=18,color=white,bold=False,max_width=None):
        s=str(s);limit=max_width if max_width is not None else 600-x
        while p.textlength(s,font=font(size,bold))>limit and size>15:size-=1
        p.text((x,y),s,font=font(size,bold),fill=color)
    def right(s,y,size=19,color=white,bold=False):
        s=str(s);x=600-p.textlength(s,font=font(size,bold));text(s,x,y,size,color,bold)
    def track(m,y,color,left=24,right=600):
        p.rounded_rectangle((left,y,right,y+6),radius=3,fill=(33,47,64))
        if m['known'] and m['elapsed'] is not None and m['elapsed']>0:
            end=left+max(3,(right-left)*min(m['elapsed']/m['limit'],1))
            p.rounded_rectangle((left,y,end,y+6),radius=3,fill=color)
    def timing(label,m,y,color,compact=False):
        value=(f"{m['elapsed']:.1f} / {m['limit']:.1f} s" if m['known'] and m['elapsed'] is not None else f"-- / {m['limit']:.1f} s")
        text(label,24,y,16,muted,max_width=260)
        if compact:
            text(value,278,y,17,white,max_width=130);track(m,y+7,color,420,600)
        else:right(value,y-2,19,white,True);track(m,y+25,color)
    text('DRIVER MONITOR',24,18,20,white,True)
    right(f'Video {decision.timestamp:05.2f}s',20,17,muted)
    text(decision.state.value.upper(),24,46,44,accent,True)
    text(c['reason'],24,96,19,accent,True)
    for i,(name,section) in enumerate(zip(['Eyes','Attention','Posture'],sections)):
        x=24+i*190;known=section['primary']['known']
        p.ellipse((x,123,x+5,128),fill=(87,211,173) if known else (76,91,108))
        text(name+': '+('readable' if known else 'unavailable'),x+12,117,14,muted,max_width=172)
    for index,(section,y) in enumerate(zip(sections,[148,333,518])):
        color=section['color'];p.line((24,y-6,600,y-6),fill=(39,56,72))
        text(section['title'],24,y,21,color,True)
        text(section['focus'],24,y+29,16,muted)
        text('Current',24,y+56,16,muted)
        text(section['state'],300,y+51,22,white,True,max_width=300)
        timing(section['primary_label'],section['primary'],y+82,color)
        if index==0:
            text('Nods (30s)',24,y+121,16,muted)
            text(f"{c['dips']} / {config['decision_rules']['head_dip_repeat_count']}",176,y+119,19,white,True)
            text('Long closures (30s)',300,y+121,16,muted)
            right(f"{c['long_closures']} / {config['decision_rules']['long_closure_repeat_count']}",y+119,19,white,True)
            timing('Mouth open',meters['mouth_open'],y+157,color,True)
        elif index==1:
            total={'known':meters['attention_away']['known'],'elapsed':c['attention_total'],'limit':c['cumulative_limit']}
            timing(f"Total away ({c['window']:g}s)",total,y+121,color)
            text(f"Glances ({c['window']:g}s)",24,y+157,16,muted)
            right(f"{c['episodes']} / {c['episode_limit']}",y+155,19,white,True)
            for j in range(c['episode_limit']):
                x=304+j*24;p.rounded_rectangle((x,y+163,x+15,y+172),radius=2,fill=color if j<c['episodes'] else (45,63,81))
        else:
            # This is the existing same-posture danger timer, not a generic caution timer.
            timing('Danger timer (posture)',meters['collapse_stop'],y+121,accent)
            timing('Hand near chest',meters['hand_near_chest'],y+157,(245,186,108),True)
    text('PROTOTYPE / NOT A MEDICAL DIAGNOSIS',24,704,13,muted)
    return im

def draw_large_multimodal(source_frame,combined,vision,bio,rate,config,camera_frame=None):
    """Reuse the camera and filtered-waveform pixels from a verified old result.

    No model, signal filter, fusion rule, or BPM estimator executes here.
    Original result geometry is 1756x1034, with 8px border, 76px header,
    1280x720 camera, 460px evidence rail and 222px footer.
    """
    if source_frame.shape[:2]!=(1034,1756):raise ValueError('Unexpected source video geometry')
    border=8;h=720;w=1904;header=92;footer=252
    camera=source_frame[84:804,8:1288] if camera_frame is None else camera_frame
    if camera.shape!=(720,1280,3):raise ValueError("Expected full 1280x720 camera frame")
    rail=draw_large_rail(vision,config)
    im=Image.new('RGB',(w,h+header+footer),(9,17,26))
    im.paste(Image.fromarray(cv2.cvtColor(camera,cv2.COLOR_BGR2RGB)),(0,header))
    im.paste(rail,(1280,header));p=ImageDraw.Draw(im)
    white,muted,accent=(236,243,250),(151,172,193),PALETTE[combined.state.value]
    def text(s,x,y,size=18,color=white,bold=False,width=None):
        s=str(s)
        if width:
            while p.textlength(s,font=font(size,bold))>width and size>14:size-=1
        p.text((x,y),s,font=font(size,bold),fill=color)
    display=measurement_display(bio)
    starting=(combined.state.value=='unavailable' and display['measuring'] and 'calibrating' in vision.reasons)
    text('MULTIMODAL DRIVER MONITOR',20,10,17,muted,True)
    text('PREPARING...' if starting else combined.state.value.upper(),20,34,38,(92,199,246) if starting else accent,True)
    text('VISION',380,12,17,muted)
    text('PREPARING...' if vision.state.value=='unavailable' and 'calibrating' in vision.reasons else vision.state.value.upper(),380,36,28,PALETTE[vision.state.value],True)
    band=bpm_range(rate['bpm']);assert band==rate.get('bpm_range',band)
    text('BIO STATE (BPM)',650,10,17,muted)
    text(band.upper(),650,33,28,PALETTE[band],True)
    name={'recent_ecg_peaks':'ECG','recent_ppg_peaks':'PPG','recorded':'Recorded','analysis':'Analyzed'}.get(rate['source'],'')
    caption=f"{rate['bpm']:.0f} BPM / {name}" if rate['bpm'] is not None else 'No current BPM'
    text(caption,650,65,15,muted)
    badge='SYNTHETIC SIGNAL' if bio['synthetic'] else 'RECORDED SIGNAL'
    text(badge,1000,14,20,(246,189,112) if bio['synthetic'] else (103,224,177),True)
    text(f'{combined.timestamp:.2f}s',1000,45,19,muted)
    y0=header+h;p.line((0,y0,w,y0),fill=(48,67,84),width=2)
    graph_w=round(w*.67)
    text('ECG / PPG',20,y0+12,18,muted,True)
    text('Last 5s',graph_w-110,y0+12,17,muted)
    # Extract the already-filtered chart images. Resizing changes their display
    # area only; no signal is processed a second time.
    for label,color,row in [('ECG',(255,112,132),0),('PPG',(79,192,244),1)]:
        yy=y0+42+row*105;text(label,20,yy,20,color,True)
        old_top=865+row*97
        pixels=source_frame[old_top:old_top+57,28:1151]
        plot=Image.fromarray(cv2.cvtColor(pixels,cv2.COLOR_BGR2RGB))
        im.paste(plot.resize((graph_w-44,65),Image.Resampling.LANCZOS),(20,y0+68+row*105))
    x=graph_w+18;p.line((graph_w,y0+14,graph_w,y0+238),fill=(48,67,84))
    recent=str(rate['source']).startswith('recent_')
    text('RECENT PULSE RATE' if bio.get('display_pulse') else 'HEART RATE',x,y0+15,18,muted,True)
    hr=rate['bpm'];rate_color=PALETTE[band]
    text(f'{hr:.0f}' if hr is not None else '--',x,y0+42,68,rate_color,True)
    text('BPM',x+156,y0+82,22,muted)
    if rate['source']=='recorded':text('Recorded',x+264,y0+84,17,muted)
    elif recent:
        text(('ECG' if rate['source']=='recent_ecg_peaks' else 'PPG')+' estimate',x+264,y0+84,18,muted)
        text('BPM RANGE: '+band.upper(),x,y0+125,18,rate_color,True)
    pulse=bio.get('display_pulse')
    if pulse:
        for i,name in enumerate(('ecg','ppg')):
            value=pulse.get(name,{}).get('bpm');label=f'{name.upper()} {value:.0f}' if value is not None else name.upper()+' --'
            text(label,x+i*190,y0+204,19,muted)
        if recent and not rate.get('verified_by_monitor'):text('Unverified estimate',x,y0+229,16,(246,189,112))
    reason=brief_bio_reason(bio)
    if reason:text(reason,x,y0+155,20,display['color'],True,width=w-x-20)
    out=cv2.cvtColor(np.asarray(im),cv2.COLOR_RGB2BGR)
    out=cv2.copyMakeBorder(out,border,border,border,border,cv2.BORDER_CONSTANT,value=accent[::-1])
    assert out.shape==(1080,1920,3)
    assert np.array_equal(out[100:820,8:1288],camera),'Camera image changed'
    return out
