"""Explicit gaze-model adapter. Missing weights never fall back to head proxies."""
from __future__ import annotations

from pathlib import Path
from hashlib import sha256
import json
import math
from typing import Any

import cv2
import numpy as np


class GazeZoneEstimator:
    def __init__(self, config: dict[str, Any], root: Path):
        self.model = None
        self.status = "model_not_connected"
        self.error = None
        self.meta = None
        path = config.get("gaze_model_metadata")
        if not path:
            return
        try:
            path = Path(path)
            path = path if path.is_absolute() else root/path
            m = json.loads(path.read_text())
            required = ("weights", "sha256", "class_names", "forward_zones", "eyes_closed_zone",
                        "mean", "std", "image_size", "crop", "input_modality", "license", "provenance",
                        "camera_forward_confirmed", "deployment_validated", "minimum_confidence", "output_kind")
            if any(k not in m for k in required):
                raise ValueError("Gaze metadata incomplete; class order and preprocessing must be explicit")
            if m["deployment_validated"] is not True or m["camera_forward_confirmed"] is not True:
                self.status = "needs_validation"
                return
            if m["input_modality"] != config.get("gaze_input_modality", "RGB") or m["input_modality"] != "RGB":
                self.status = "input_modality_mismatch"
                return
            if not m["license"] or not m["provenance"]:
                raise ValueError("Missing model provenance or license")
            names = m["class_names"]
            if (len(names) != 7 or len(set(names)) != 7 or not set(m["forward_zones"]).issubset(names)
                    or not m["forward_zones"] or m["eyes_closed_zone"] not in names
                    or m["eyes_closed_zone"] in m["forward_zones"]):
                raise ValueError("Invalid gaze class order/mapping")
            if m["crop"] not in ("face", "upper_face", "full_frame") or m["output_kind"] not in ("logits", "probabilities"):
                raise ValueError("Unsupported preprocessing/output contract")
            if len(m["mean"]) != 3 or len(m["std"]) != 3 or any(float(v) <= 0 for v in m["std"]):
                raise ValueError("Invalid explicit model normalization")
            if len(m["image_size"]) != 2 or min(m["image_size"]) < 1 or not 0 < float(m["minimum_confidence"]) <= 1:
                raise ValueError("Invalid model input dimensions or quality gate")
            weights = Path(m["weights"])
            weights = weights if weights.is_absolute() else path.parent/weights
            if sha256(weights.read_bytes()).hexdigest() != m["sha256"]:
                raise ValueError("Gaze weights checksum mismatch")
            import torch
            self.model = torch.jit.load(str(weights), map_location="cpu").eval()
            self.meta, self.status = m, "ready"
        except Exception as error:
            self.error = f"{type(error).__name__}: {error}"
            self.status = "model_load_error"

    def predict(self, frame: np.ndarray, face: dict) -> dict:
        result = dict(state="unknown", zone="unknown", confidence=None, status=self.status, error=self.error)
        if self.model is None:
            return result
        if not face.get("readable"):
            return dict(result, status="unavailable")
        m = self.meta
        image = frame
        if m["crop"] != "full_frame":
            box = face.get("box")
            if box is None:
                return dict(result, status="unavailable")
            x1,y1,x2,y2 = map(int, box)
            x1,y1,x2,y2 = max(0,x1),max(0,y1),min(frame.shape[1],x2),min(frame.shape[0],y2)
            if m["crop"] == "upper_face":
                y2 = (y1+y2)//2
            image = frame[y1:y2,x1:x2]
        if image.size == 0:
            return dict(result, status="unavailable")
        import torch
        rgb = cv2.cvtColor(cv2.resize(image, tuple(map(int,m["image_size"]))), cv2.COLOR_BGR2RGB).astype(np.float32)/255.0
        tensor = (rgb-np.asarray(m["mean"],dtype=np.float32))/np.asarray(m["std"],dtype=np.float32)
        try:
            with torch.inference_mode():
                output = self.model(torch.from_numpy(tensor.transpose(2,0,1).copy()).unsqueeze(0))
                if tuple(output.shape) != (1,7) or not torch.isfinite(output).all():
                    raise ValueError("Expected seven finite gaze outputs")
                probabilities = torch.softmax(output[0],dim=0) if m["output_kind"] == "logits" else output[0]
                if (probabilities < 0).any() or abs(float(probabilities.sum())-1) > .01:
                    raise ValueError("Invalid class probabilities")
                confidence,index = probabilities.max(dim=0)
            confidence = float(confidence)
            if not math.isfinite(confidence) or confidence < float(m["minimum_confidence"]):
                return dict(result,status="low_confidence",confidence=confidence)
            zone = m["class_names"][int(index)]
            state = "eyes_closed" if zone == m["eyes_closed_zone"] else "forward" if zone in m["forward_zones"] else "away"
            return dict(state=state,zone=zone,confidence=confidence,status="ready",error=None)
        except Exception as error:
            return dict(result,status="inference_error",error=str(error))
