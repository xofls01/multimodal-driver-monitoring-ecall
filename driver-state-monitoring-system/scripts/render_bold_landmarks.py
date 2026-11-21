#!/usr/bin/env python3
"""Redraw landmark colors on clean camera frames; retain saved decisions and BPM."""
import argparse
from bisect import bisect_right
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import subprocess
import sys


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--source', type=Path)
    parser.add_argument('--output', type=Path, required=True, help='New folder; existing results are never overwritten')
    parser.add_argument('--only', nargs='+', default=['N', 'D3', 'E4', 'A_new'])
    parser.add_argument('--geometry-cache', type=Path, help='Reuse coordinates from an earlier render verification folder')
    parser.add_argument('--code-root', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    root = args.project_root.resolve()
    source = args.source or root / 'outputs/multimodal_output_contract'
    output = args.output.resolve()
    if output.exists():
        raise FileExistsError('Choose a new output folder')
    output.mkdir(parents=True)
    for folder in ['gifs', 'clips', 'previews', 'verification', 'vision_only/gifs', 'vision_only/clips', 'vision_only/previews']:
        (output / folder).mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(root / 'src'))
    import driver_state_tools
    if args.code_root:
        driver_state_tools.__path__.insert(0, str(args.code_root / 'src/driver_state_tools'))
    import cv2
    import numpy as np
    import imageio_ffmpeg
    from PIL import Image
    from driver_state_tools.decision_engine import Decision, DriverState
    from driver_state_tools.vision_pipeline import VisionModels
    from driver_state_tools.vision_features import RawFeatures
    from driver_state_tools.landmark_overlay import draw_landmarks, EYE_RINGS, MOUTH_RING, FEATURE_COLORS
    from driver_state_tools.presentation_dashboard import draw_large_multimodal, draw_large_rail
    from driver_state_tools.evidence_dashboard import PALETTE
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    config = json.loads((source / 'vision_config_used.json').read_text())
    pairs = {r['id']: r for r in json.loads((root / 'config/multimodal/system_pairs.json').read_text())['pairs']}
    hashes = json.loads((root / 'models/manifest.json').read_text())['files']
    for key in ['pose_model', 'detect_model']:
        model_path = root / config[key]
        assert model_path.exists() and sha(model_path) == hashes[model_path.name]
        config[key] = str(model_path)
    models = None if args.geometry_cache else VisionModels(config, root)
    segments = {'N': (6, 13), 'D3': (5, 12), 'E4': (4, 15), 'A_new': (4, 11)}
    reports = []

    def decision(row):
        return Decision(row['timestamp'], DriverState(row['state']), row['reasons'], row['durations'], row['metadata'])

    def encoder(dest, size, fps):
        return subprocess.Popen([ffmpeg, '-v', 'error', '-nostdin', '-n', '-f', 'rawvideo', '-pix_fmt', 'bgr24',
            '-s', size, '-r', str(fps), '-i', '-', '-an', '-c:v', 'libx264', '-preset', 'fast', '-crf', '17',
            '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(dest)], stdin=subprocess.PIPE)

    def make_excerpt(original, dest, start, end, gif=False):
        command = [ffmpeg, '-v', 'error', '-nostdin', '-n', '-ss', str(start), '-i', str(original), '-t', str(end-start), '-an']
        if gif:
            command += ['-filter_complex', '[0:v]fps=10,split[a][b];[a]palettegen=max_colors=192:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle', '-loop', '0']
        else:
            command += ['-c:v', 'libx264', '-preset', 'fast', '-crf', '18', '-pix_fmt', 'yuv420p', '-movflags', '+faststart']
        subprocess.run(command + [str(dest)], check=True)

    def verify_video(path, size, count, fps):
        cap = cv2.VideoCapture(str(path))
        assert cap.isOpened()
        actual = (round(cap.get(3)), round(cap.get(4)))
        assert actual == size
        assert round(cap.get(cv2.CAP_PROP_FRAME_COUNT)) == count
        assert abs(cap.get(cv2.CAP_PROP_FPS) - fps) < .01
        cap.release()

    try:
        for name in args.only:
            folder = source / name
            filenames = ['fusion.jsonl', 'display_bpm.jsonl', 'vision_frames.jsonl', 'model_output.jsonl', 'unity_packets.jsonl', 'bio_recomputed.jsonl', 'preview.mp4']
            before = {f: sha(folder/f) for f in filenames}
            rows = [json.loads(line) for line in (folder/'fusion.jsonl').open()]
            rates = [json.loads(line) for line in (folder/'display_bpm.jsonl').open()]
            observations = [json.loads(line) for line in (folder/'vision_frames.jsonl').open()]
            meta = json.loads((folder/'summary.json').read_text())
            count, fps = meta['frames'], meta['fps']
            assert len(rows) == len(rates) == count and observations
            observation_times = [r["timestamp"] for r in observations]
            original = root / pairs[name]['video']
            assert sha(original) == pairs[name]['source_sha256']
            camera_cap, result_cap = cv2.VideoCapture(str(original)), cv2.VideoCapture(str(folder/'preview.mp4'))
            assert camera_cap.isOpened() and result_cap.isOpened()
            if models: models.start_clip()
            geometry_cache = [json.loads(line) for line in (args.geometry_cache/f"{name}_geometry.jsonl").open()] if args.geometry_cache else None
            if geometry_cache: assert len(geometry_cache) == count
            dest = output / f'{name}.mp4'
            vision_dest = output / 'vision_only' / f'{name}.mp4'
            combined_encoder = encoder(dest, '1920x1080', fps)
            vision_encoder = encoder(vision_dest, '1920x736', fps) if name != 'N' else None
            geometry_log = (output / 'verification' / f'{name}_geometry.jsonl').open('w')
            print(name, 'rendering', count, 'frames', flush=True)
            try:
                for i, (row, rate) in enumerate(zip(rows, rates)):
                    observation_index = bisect_right(observation_times, row["timestamp"] + 1e-7) - 1
                    assert observation_index >= 0
                    observation = observations[observation_index]
                    assert -.000001 <= row["timestamp"] - observation["timestamp"] < .12
                    ok, frame = camera_cap.read()
                    ok2, source_frame = result_cap.read()
                    assert ok and ok2 and frame.shape == (720, 1280, 3)
                    assert abs(row['timestamp'] - i/fps) < 1e-5
                    assert abs(rate['timestamp'] - row['timestamp']) < 1e-6
                    stored = observation['raw']
                    if geometry_cache:
                        g = geometry_cache[i]
                        assert abs(g['timestamp']-row['timestamp']) < 1e-6
                        assert g['vision_state'] == row['vision']['state']
                        assert g['fusion_state'] == row['fusion']['state']
                        face = {'box':g['face_box'], 'readable':g.get('face_readable', bool(g['face_box'])),
                                'eyes_readable':g.get('eyes_readable', stored['eyes_readable']),
                                'landmarks':{int(k):v for k,v in g['face_landmarks'].items()}}
                        pose = {'readable':g.get('pose_readable', stored['pose_readable']),
                                'keypoints':{int(k):v for k,v in g['pose_keypoints'].items()}}
                        hands = {'landmarks':g['hand_landmarks']}
                    else:
                        face = models._extract_face(frame)
                        pose = models._extract_pose(frame)
                        hands = models._extract_hands(frame)
                    raw = RawFeatures(timestamp=row['timestamp'], face_box=face.get('box'),
                        face_readable=bool(face.get('readable')), eyes_readable=bool(face.get('eyes_readable')),
                        pose_readable=bool(pose.get('readable')), face_landmarks=face.get('landmarks', {}),
                        pose_keypoints=pose.get('keypoints', {}), hand_landmarks=hands.get('landmarks', []),
                        hand_on_chest=VisionModels._hand_chest_context(hands.get('landmarks', []), pose.get('keypoints', {})),
                        phone_boxes=stored.get('phone_boxes', []), handheld_objects=stored.get('handheld_objects', []))
                    derived = SimpleNamespace(**observation['derived'])
                    camera = draw_landmarks(frame, raw, derived, config)
                    vision = decision(row['vision'])
                    combined = draw_large_multimodal(source_frame, decision(row['fusion']), vision, row['bio'], rate, config, camera_frame=camera)
                    combined_encoder.stdin.write(combined.tobytes())
                    if vision_encoder:
                        rail = cv2.cvtColor(np.asarray(draw_large_rail(vision, config)), cv2.COLOR_RGB2BGR)
                        vision_frame = cv2.copyMakeBorder(np.hstack([camera, rail]), 8, 8, 8, 8,
                            cv2.BORDER_CONSTANT, value=PALETTE[vision.state.value][::-1])
                        assert vision_frame.shape == (736, 1920, 3)
                        vision_encoder.stdin.write(vision_frame.tobytes())
                    if i == min(count-1, round(7.7*fps)):
                        cv2.imwrite(str(output/'previews'/f'{name}.jpg'), combined)
                        if vision_encoder:
                            cv2.imwrite(str(output/'vision_only/previews'/f'{name}.jpg'), vision_frame)
                    used = set(sum((list(ring) for ring in EYE_RINGS), [])) | set(MOUTH_RING)
                    geometry_log.write(json.dumps({'frame': i, 'timestamp': row['timestamp'], 'face_box': raw.face_box,
                        'face_readable':raw.face_readable, 'eyes_readable':raw.eyes_readable, 'pose_readable':raw.pose_readable,
                        'face_landmarks': {k:v for k,v in raw.face_landmarks.items() if k in used},
                        'pose_keypoints': raw.pose_keypoints, 'hand_landmarks': raw.hand_landmarks,
                        'vision_state': row['vision']['state'], 'fusion_state': row['fusion']['state']})+'\n')
                    if (i+1) % 150 == 0:
                        print(name, i+1, '/', count, flush=True)
            finally:
                camera_cap.release(); result_cap.release(); geometry_log.close()
                combined_encoder.stdin.close()
                if vision_encoder: vision_encoder.stdin.close()
            assert combined_encoder.wait() == 0
            if vision_encoder: assert vision_encoder.wait() == 0
            verify_video(dest, (1920,1080), count, fps)
            if vision_encoder: verify_video(vision_dest, (1920,736), count, fps)
            start, end = segments[name]
            variants = [(dest, output)] + ([(vision_dest, output/'vision_only')] if vision_encoder else [])
            for original_result, destination in variants:
                make_excerpt(original_result, destination/'clips'/f'{name}.mp4', start, end)
                gif = destination/'gifs'/f'{name}.gif'
                make_excerpt(original_result, gif, start, end, gif=True)
                with Image.open(gif) as image:
                    duration = 0
                    for j in range(image.n_frames):
                        image.seek(j); duration += image.info.get('duration', 0)
                    assert abs(duration/1000 - (end-start)) < .12
                    assert image.size == ((1920,736) if destination.name == 'vision_only' else (1920,1080))
            assert all(sha(folder/f) == digest for f,digest in before.items())
            assert sha(original) == pairs[name]['source_sha256']
            report = {'id': name, 'frames': count, 'fps': fps, 'seconds': count/fps,
                'source_files_unchanged': before, 'camera_source_unchanged': True,
                'vision_state_frames': dict(Counter(r['vision']['state'] for r in rows)),
                'fusion_state_frames': dict(Counter(r['fusion']['state'] for r in rows)),
                'decisions_recomputed': False, 'bpm_recomputed': False, 'signal_filter_rerun': False,
                'landmark_coordinates_recomputed': not bool(args.geometry_cache), 'geometry_inference_fps': fps,
                'geometry_cache':str(args.geometry_cache) if args.geometry_cache else None,
                'camera_cropped': False, 'ecg_ppg_chart_pixels_reused': True,
                'gif_segment': [start, end], 'multimodal_video_sha256': sha(dest),
                'vision_only_video_sha256': sha(vision_dest) if vision_encoder else None}
            assert report['vision_state_frames'] == meta['vision_state_frames']
            assert report['fusion_state_frames'] == meta['fused_state_frames']
            (output/'verification'/f'{name}.json').write_text(json.dumps(report, indent=2)+'\n')
            reports.append(report)
            print(name, 'videos and GIFs verified', flush=True)
    finally:
        if models: models.close()
    (output/'verification/renderer.json').write_text(json.dumps({'passed':True, 'clips':reports,
        'feature_colors_bgr':FEATURE_COLORS, 'line_width_pixels_at_720p': {'face':3,'eyes':3,'pose':5,'hands':3},
        'outline_extra_width_pixels':3, 'pose_joint_radius_pixels':5,
        'note':'Drawing coordinates were extracted from camera frames or reused from the explicitly supplied geometry cache. All decisions, thresholds, BPM and filtered waveform images were retained.'}, indent=2)+'\n')
    print('All outputs verified:', output, flush=True)


if __name__ == '__main__':
    main()
