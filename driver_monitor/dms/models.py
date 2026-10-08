"""MediaPipe 모델 파일(.task) 자동 다운로드."""
from __future__ import annotations

import urllib.request
from pathlib import Path

MODEL_DIR = Path(__file__).resolve().parent.parent / "models"
URLS = {
    "face_landmarker.task": "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task",
    "hand_landmarker.task": "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task",
}


def ensure_model(name: str) -> str:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    path = MODEL_DIR / name
    if not path.exists() or path.stat().st_size < 1000:
        print(f"[models] {name} 다운로드 중...")
        urllib.request.urlretrieve(URLS[name], path)
        print(f"[models] 저장: {path}")
    return str(path)
