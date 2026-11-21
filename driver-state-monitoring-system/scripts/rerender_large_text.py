#!/usr/bin/env python3
"""Render three consistent vision sections from existing recorded results without rerunning inference."""
from pathlib import Path
import argparse,hashlib,json,sys,importlib.util,subprocess
from collections import Counter

def sha(path):
 h=hashlib.sha256()
 with Path(path).open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 return h.hexdigest()
def main():
 package=Path(__file__).resolve().parents[1]
 ap=argparse.ArgumentParser(description=__doc__)
 ap.add_argument('--project-root',type=Path,default=package)
 ap.add_argument('--source',type=Path)
 ap.add_argument('--output',type=Path)
 ap.add_argument('--only',nargs='+',default=['N','D3','E4','A_new'])
 ap.add_argument('--preview-only',action='store_true')
 args=ap.parse_args();project=args.project_root.resolve();source=args.source or project/'outputs/multimodal_output_contract'
 output=args.output or project/'outputs/presentation_final';output.mkdir(parents=True,exist_ok=True)
 (output/'previews').mkdir(exist_ok=True);(output/'verification').mkdir(exist_ok=True)
 sys.path.insert(0,str(project/'src'))
 import cv2,numpy as np,imageio_ffmpeg
 from driver_state_tools.decision_engine import Decision,DriverState
 ui_path=package/'src/driver_state_tools/presentation_dashboard.py'
 spec=importlib.util.spec_from_file_location('large_text_ui',ui_path);ui=importlib.util.module_from_spec(spec);spec.loader.exec_module(ui)
 def decision(r):return Decision(r['timestamp'],DriverState(r['state']),r['reasons'],r['durations'],r['metadata'])
 config=json.loads((source/'vision_config_used.json').read_text());summaries=[]
 for name in args.only:
  folder=source/name;meta=json.loads((folder/'summary.json').read_text());rows=[json.loads(l) for l in (folder/'fusion.jsonl').open()];rates=[json.loads(l) for l in (folder/'display_bpm.jsonl').open()]
  video=folder/'preview.mp4';assert len(rows)==len(rates)==meta['frames']
  checked_files={f:sha(folder/f) for f in ['fusion.jsonl','display_bpm.jsonl','model_output.jsonl','vision_frames.jsonl','unity_packets.jsonl','bio_recomputed.jsonl','preview.mp4']}
  cap=cv2.VideoCapture(str(video));assert cap.isOpened();fps=meta['fps'];assert abs(cap.get(cv2.CAP_PROP_FPS)-fps)<.01
  def draw(i,frame):
   r=rows[i];rate=rates[i];assert abs(r['timestamp']-i/fps)<1e-5;assert abs(rate['timestamp']-r['timestamp'])<1e-6
   return ui.draw_large_multimodal(frame,decision(r['fusion']),decision(r['vision']),r['bio'],rate,config)
  if args.preview_only:
   from PIL import ImageDraw
   original=ImageDraw.ImageDraw.text;bounds=[]
   def audited(self,xy,text,*a,**k):
    box=self.textbbox(xy,str(text),font=k.get('font'));w,h=self._image.size
    if box[0]<0 or box[1]<0 or box[2]>w or box[3]>h:bounds.append({'text':str(text),'box':box,'image':[w,h]})
    return original(self,xy,text,*a,**k)
   ImageDraw.ImageDraw.text=audited
   for second in [0,4,7.7,12.5,(len(rows)-1)/fps]:
    i=min(len(rows)-1,round(second*fps));cap.set(cv2.CAP_PROP_POS_FRAMES,i);ok,frame=cap.read();assert ok
    result=draw(i,frame);cv2.imwrite(str(output/f'{name}_{second:.1f}s.jpg'),result)
   cap.release();ImageDraw.ImageDraw.text=original
   (output/f'{name}_text_bounds.json').write_text(json.dumps(bounds,indent=2));print(name,'text bounds issues',len(bounds),flush=True);continue
  dest=output/f'{name}.mp4'
  if dest.exists():raise FileExistsError('Use a new output folder to preserve previous results')
  ff=imageio_ffmpeg.get_ffmpeg_exe();proc=subprocess.Popen([ff,'-y','-v','error','-f','rawvideo','-pix_fmt','bgr24','-s','1920x1080','-r',str(fps),'-i','-','-an','-c:v','libx264','-preset','fast','-crf','17','-pix_fmt','yuv420p','-movflags','+faststart',str(dest)],stdin=subprocess.PIPE)
  i=0
  try:
   while True:
    ok,frame=cap.read()
    if not ok:break
    result=draw(i,frame);proc.stdin.write(result.tobytes())
    if i==min(len(rows)-1,round(12.5*fps)):cv2.imwrite(str(output/'previews'/f'{name}.jpg'),result)
    i+=1
  finally:cap.release();proc.stdin.close()
  if proc.wait()!=0:raise RuntimeError('H264 encoding failed')
  assert i==len(rows)
  check=cv2.VideoCapture(str(dest));assert int(check.get(cv2.CAP_PROP_FRAME_COUNT))==i and abs(check.get(cv2.CAP_PROP_FPS)-fps)<.01;assert (round(check.get(3)),round(check.get(4)))==(1920,1080);check.release()
  assert all(sha(folder/f)==s for f,s in checked_files.items())
  summary={'id':name,'output_video':dest.name,'frames':i,'fps':fps,'duration_seconds':i/fps,'output_size':[1920,1080],
    'operation':'presentation_only_rerender','source':str(folder),'source_file_hashes_unchanged':checked_files,
    'states':dict(Counter(r['fusion']['state'] for r in rows)),
    'vision_states':dict(Counter(r['vision']['state'] for r in rows)),
    'models_rerun':False,'decisions_recomputed':False,'bpm_recomputed':False,'signal_filter_rerun':False,
    'camera_image_pixels_unchanged_before_encoding':True,'ecg_ppg_filtered_images_reused':True,
    'real_time_or_unity_test':False,'bpm_records':len(rates),'video_sha256':sha(dest)}
  assert summary['states']==meta['fused_state_frames'];assert summary['vision_states']==meta['vision_state_frames']
  (output/'verification'/f'{name}.json').write_text(json.dumps(summary,indent=2)+'\n');summaries.append(summary)
  print(name,'rendered',i,'frames',flush=True)
 if summaries:
  (output/'verification/renderer.json').write_text(json.dumps({'display_only':True,'clips':summaries},indent=2)+'\n')
if __name__=='__main__':main()
