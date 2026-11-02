"""Adapter around the frozen Model.inference.predict entry point."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from django.conf import settings

_root = str(settings.BASE_DIR)
if _root not in sys.path:
    sys.path.insert(0, _root)

from Model.inference import predict as frozen_predict  # noqa: E402

METADATA_PATH = Path(settings.BASE_DIR) / "Model" / "model_metadata.json"


def _metadata_version() -> str:
    try:
        data = json.loads(METADATA_PATH.read_text(encoding="utf-8"))
        return str(data.get("model_version") or settings.MODEL_VERSION)
    except OSError:
        return settings.MODEL_VERSION


def run_predict(image_path: str) -> dict[str, Any]:
    """
    Call the frozen classifier and normalize the result.

    The 0.5 decision threshold lives inside Model/inference.py. This
    adapter never re-thresholds scores.
    """
    raw = frozen_predict(image_path)
    version = _metadata_version()

    if isinstance(raw, dict):
        predicted_class = (
            raw.get("predicted_class")
            or raw.get("label")
            or raw.get("class_name")
            or raw.get("class")
            or ""
        )
        confidence = raw.get("confidence")
        defect_probability = raw.get("defect_probability", raw.get("probability"))
        version = raw.get("model_version") or version
    elif isinstance(raw, (tuple, list)) and len(raw) >= 2:
        predicted_class, defect_probability = raw[0], raw[1]
        confidence = raw[2] if len(raw) > 2 else None
    else:
        raise TypeError(
            "Model.inference.predict must return a dict or (class, score) tuple."
        )

    predicted_class = str(predicted_class).strip()
    if predicted_class.lower() == "normal":
        predicted_class = "Normal"
    elif predicted_class.lower() in {"defective", "defect"}:
        predicted_class = "Defective"

    if predicted_class not in {"Normal", "Defective"}:
        raise ValueError(f"Unexpected predicted_class from model: {predicted_class!r}")

    if defect_probability is not None:
        defect_probability = float(defect_probability)
    if confidence is None and defect_probability is not None:
        confidence = (
            1.0 - defect_probability
            if predicted_class == "Normal"
            else defect_probability
        )
    if confidence is not None:
        confidence = float(confidence)

    return {
        "predicted_class": predicted_class,
        "confidence": confidence,
        "defect_probability": defect_probability,
        "model_version": version,
    }
