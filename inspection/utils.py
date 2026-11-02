"""
Strict pre-inference validation.

Invalid files are recorded as failed attempts with a readable reason.
They must never be passed to Model.inference.predict.
"""

from __future__ import annotations

import logging
from io import BytesIO
from typing import Optional

from django.core.files.uploadedfile import UploadedFile
from PIL import Image, UnidentifiedImageError

logger = logging.getLogger("inspection")

MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MiB
MIN_DIMENSION = 64
MAX_DIMENSION = 4096
MIN_BATCH_SIZE = 1
MAX_BATCH_SIZE = 10
ALLOWED_FORMATS = frozenset({"JPEG", "PNG"})


class ImageValidationError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def validate_batch_size(file_count: int) -> Optional[str]:
    if MIN_BATCH_SIZE <= file_count <= MAX_BATCH_SIZE:
        return None
    return (
        f"Batch size must be between {MIN_BATCH_SIZE} and {MAX_BATCH_SIZE} "
        f"files inclusive; received {file_count}."
    )


def _readable_size(num_bytes: int) -> str:
    mib = num_bytes / (1024 * 1024)
    return f"{mib:.2f} MiB"


def validate_image_file(uploaded_file: UploadedFile) -> Optional[str]:
    """
    Return a human-readable failure reason, or None if the file may be
    sent to the frozen classifier. Decoding is required; extension and
    Content-Type are not trusted.
    """
    name = getattr(uploaded_file, "name", "unnamed") or "unnamed"

    size = getattr(uploaded_file, "size", None)
    if size is None:
        try:
            uploaded_file.seek(0, 2)
            size = uploaded_file.tell()
            uploaded_file.seek(0)
        except Exception:
            size = None

    if size is not None and size == 0:
        reason = "Empty file; decoded JPEG or PNG image data is required."
        logger.info("Rejected '%s': %s", name, reason)
        return reason

    if size is not None and size > MAX_FILE_SIZE_BYTES:
        reason = (
            f"File size {_readable_size(size)} exceeds the 5 MiB limit "
            f"({MAX_FILE_SIZE_BYTES} bytes)."
        )
        logger.info("Rejected '%s': %s", name, reason)
        return reason

    try:
        uploaded_file.seek(0)
        payload = uploaded_file.read()
        uploaded_file.seek(0)
    except Exception as exc:
        reason = f"Could not read upload bytes: {exc}"
        logger.info("Rejected '%s': %s", name, reason)
        return reason

    if not payload:
        reason = "Empty file; decoded JPEG or PNG image data is required."
        logger.info("Rejected '%s': %s", name, reason)
        return reason

    if len(payload) > MAX_FILE_SIZE_BYTES:
        reason = (
            f"File size {_readable_size(len(payload))} exceeds the 5 MiB limit "
            f"({MAX_FILE_SIZE_BYTES} bytes)."
        )
        logger.info("Rejected '%s': %s", name, reason)
        return reason

    try:
        with Image.open(BytesIO(payload)) as verified:
            verified.verify()
    except UnidentifiedImageError:
        reason = "File is not a valid decoded JPEG or PNG image."
        logger.info("Rejected '%s': %s", name, reason)
        return reason
    except Exception as exc:
        reason = f"Image could not be decoded ({exc}). JPEG and PNG are required."
        logger.info("Rejected '%s': %s", name, reason)
        return reason

    try:
        with Image.open(BytesIO(payload)) as image:
            fmt = (image.format or "").upper()
            if fmt == "JPG":
                fmt = "JPEG"
            if fmt not in ALLOWED_FORMATS:
                reason = (
                    f"Unsupported image format '{fmt or 'unknown'}'. "
                    "Only decoded JPEG and PNG are accepted."
                )
                logger.info("Rejected '%s': %s", name, reason)
                return reason

            width, height = image.size
            if not (
                MIN_DIMENSION <= width <= MAX_DIMENSION
                and MIN_DIMENSION <= height <= MAX_DIMENSION
            ):
                reason = (
                    f"Image dimensions {width}×{height} px are outside the "
                    f"allowed range {MIN_DIMENSION}–{MAX_DIMENSION} px inclusive."
                )
                logger.info("Rejected '%s': %s", name, reason)
                return reason

            image.load()
    except Exception as exc:
        reason = f"Decoded image failed integrity checks: {exc}"
        logger.info("Rejected '%s': %s", name, reason)
        return reason

    return None
