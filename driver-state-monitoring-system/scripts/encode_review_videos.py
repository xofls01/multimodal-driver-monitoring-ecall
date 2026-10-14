#!/usr/bin/env python3
"""Create browser-compatible H.264 copies; never replace source or analysis videos."""
import argparse
from pathlib import Path
import cv2
import subprocess


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--run',type=Path,default=Path('outputs/driver_monitor_20/run'))
    args=ap.parse_args()
    target=args.run/'browser_videos'; target.mkdir(parents=True,exist_ok=True)
    for source in sorted((args.run/'annotated_videos').glob('*.mp4')):
        if not (args.run/'frame_records'/f'{source.stem}.jsonl').exists():
            print(f'{source.stem}: still processing, skipped',flush=True)
            continue
        destination=target/source.name
        capture=cv2.VideoCapture(str(source))
        fps=capture.get(cv2.CAP_PROP_FPS)
        width=int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)); height=int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        expected=int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        if not capture.isOpened() or fps<=0 or expected<=0:
            capture.release(); raise RuntimeError(f'unreadable source: {source}')
        if destination.exists() and destination.stat().st_mtime >= source.stat().st_mtime:
            check=cv2.VideoCapture(str(destination))
            same=(int(check.get(cv2.CAP_PROP_FRAME_WIDTH))==width and
                  int(check.get(cv2.CAP_PROP_FRAME_HEIGHT))==height and
                  abs(check.get(cv2.CAP_PROP_FPS)-fps)<0.01)
            decoded=0
            while check.read()[0]: decoded+=1
            check.release()
            if same and decoded==expected:
                capture.release()
                print(f'{source.stem}: existing H.264 copy verified, skipped',flush=True)
                continue
        writer=cv2.VideoWriter(str(destination),cv2.VideoWriter_fourcc(*'avc1'),fps,(width,height))
        if not writer.isOpened():
            capture.release(); writer.release()
            import imageio_ffmpeg
            subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-nostdin', '-y', '-i', str(source),
                            '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
                            str(destination)], check=True, capture_output=True)
            count=expected
        else:
            count=0
            try:
                while True:
                    ok,frame=capture.read()
                    if not ok: break
                    writer.write(frame); count+=1
            finally:
                capture.release(); writer.release()
        check=cv2.VideoCapture(str(destination)); decoded=0
        while check.read()[0]: decoded+=1
        check.release()
        if count != expected or decoded != expected:
            raise RuntimeError(f'frame count mismatch: {source}: {expected} vs {count} vs {decoded}')
        print(f'{source.stem}: H.264 {width}x{height} {fps:.3f}fps {count} frames',flush=True)


if __name__=='__main__': main()
