"""
image_validation.py — Validate uploaded JPEG before spending money on Vertex AI.

All checks are cheap (microseconds to milliseconds). They run before any API call.
A request that fails here costs ~$0.00. A request that reaches Vertex AI costs ~$0.02–0.05.

Validation order (mirrors request pipeline steps 6–10):
  1. File size from Content-Length header (fast reject before reading body)
  2. Magic bytes — first 3 bytes must be FF D8 FF (real JPEG signature)
  3. Actual byte count after full read
  4. Image dimensions via Pillow
"""

import io
import logging

from fastapi import HTTPException, UploadFile
from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------
MAX_FILE_BYTES = 10 * 1024 * 1024   # 10 MB
MIN_DIMENSION = 100                  # pixels — real form photos won't be smaller
MAX_DIMENSION = 8000                 # pixels — prevents absurdly large buffers

JPEG_MAGIC = b"\xff\xd8\xff"


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def validate_jpeg_bytes(raw_bytes: bytes) -> bytes:
    """
    Validate raw bytes as a JPEG image and return them if valid.
    Raises HTTPException (400 or 413) on any failure.

    Called directly when the caller has already read the bytes (e.g. to
    detect file type before routing). validate_jpeg_upload() is a thin
    async wrapper for callers that have an UploadFile instead.
    """
    if len(raw_bytes) == 0:
        raise HTTPException(status_code=400, detail="Empty file")

    if len(raw_bytes) > MAX_FILE_BYTES:
        logger.warning("Rejected upload: size=%d bytes exceeds limit", len(raw_bytes))
        raise HTTPException(
            status_code=413,
            detail=f"File too large (max {MAX_FILE_BYTES // 1_048_576} MB)",
        )

    # Magic bytes: real JPEG signature is FF D8 FF
    if raw_bytes[:3] != JPEG_MAGIC:
        logger.warning(
            "Rejected upload: bad magic bytes=%s", raw_bytes[:3].hex()
        )
        raise HTTPException(
            status_code=400,
            detail="File does not appear to be a valid JPEG image",
        )

    # Pillow: open and check dimensions
    try:
        img = Image.open(io.BytesIO(raw_bytes))
        img.verify()  # detects truncated / corrupt files
    except UnidentifiedImageError:
        logger.warning("Rejected upload: Pillow could not identify image format")
        raise HTTPException(status_code=400, detail="Could not read image file")
    except Exception as exc:
        logger.warning("Rejected upload: image verification failed: %s", exc)
        raise HTTPException(status_code=400, detail="Image file appears to be corrupt")

    # Re-open after verify() (verify() exhausts the file object)
    img = Image.open(io.BytesIO(raw_bytes))
    width, height = img.size

    if width < MIN_DIMENSION or height < MIN_DIMENSION:
        logger.warning("Rejected upload: dimensions too small (%dx%d)", width, height)
        raise HTTPException(
            status_code=400,
            detail=f"Image too small (minimum {MIN_DIMENSION}x{MIN_DIMENSION} pixels)",
        )

    if width > MAX_DIMENSION or height > MAX_DIMENSION:
        logger.warning("Rejected upload: dimensions too large (%dx%d)", width, height)
        raise HTTPException(
            status_code=400,
            detail=f"Image too large (maximum {MAX_DIMENSION}x{MAX_DIMENSION} pixels)",
        )

    logger.info(
        "Image validated: size=%d bytes, dimensions=%dx%d", len(raw_bytes), width, height
    )
    return raw_bytes


async def validate_jpeg_upload(file: UploadFile) -> bytes:
    """
    Validate the uploaded UploadFile as a JPEG and return its raw bytes.
    Raises HTTPException (400 or 413) on any validation failure.

    Thin async wrapper around validate_jpeg_bytes() for callers that have
    an UploadFile rather than raw bytes. Checks MIME type first (cheap
    client-declared filter), then delegates to validate_jpeg_bytes().
    """
    # MIME type declared by the client (untrusted, but cheap first filter)
    if file.content_type not in ("image/jpeg", "image/jpg"):
        logger.warning("Rejected upload: wrong content_type=%s", file.content_type)
        raise HTTPException(status_code=400, detail="File must be a JPEG image")

    raw_bytes = await file.read()
    return validate_jpeg_bytes(raw_bytes)
