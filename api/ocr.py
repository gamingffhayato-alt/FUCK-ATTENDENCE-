"""
OCR module: timetable image -> raw text via the free OCR.space API.

Vercel serverless functions cannot install system packages (no tesseract),
so OCR is delegated to the hosted OCR.space API:

    image bytes -> base64 -> POST https://api.ocr.space/parse/image -> parsed text

Required environment variable:
    OCR_SPACE_API_KEY   free key from https://ocr.space/ocrapi
                        (get one at https://github.com/ocrspace/OCRSpace/#apikey)

No pytesseract, no Pillow, no numpy — only `requests`.
"""

from __future__ import annotations

import base64
import os

import requests

OCR_SPACE_ENDPOINT = "https://api.ocr.space/parse/image"

ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif", ".gif"}
# Vercel request bodies are capped around 4.5 MB — fail fast with a clear message
MAX_IMAGE_BYTES = 4 * 1024 * 1024
REQUEST_TIMEOUT = (10, 30)  # (connect, read) seconds


class OcrUnavailableError(RuntimeError):
    """The OCR.space API key is missing, or the API failed/unreachable."""


def extract_text(image_bytes: bytes) -> str:
    """
    Base64-encode the image, send it to OCR.space and return the parsed
    text as a single string ('' when nothing could be read).

    Raises OcrUnavailableError when the key is missing or the API fails.
    """
    api_key = os.getenv("OCR_SPACE_API_KEY", "").strip()
    if not api_key:
        raise OcrUnavailableError(
            "OCR_SPACE_API_KEY is not set — get a free key at https://ocr.space/ocrapi "
            "and add it to your .env (local) and Vercel environment variables."
        )

    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "apikey": api_key,
        "base64Image": f"data:{_sniff_media_type(image_bytes)};base64,{image_b64}",
        "language": "eng",
        "isTable": "true",           # timetables are grids -> table recognition
        "OCREngine": "2",            # engine 2 is more accurate on tables/screenshots
        "scale": "true",             # upscale small images before recognition
        "detectOrientation": "true",
    }

    try:
        response = requests.post(
            OCR_SPACE_ENDPOINT, data=payload, timeout=REQUEST_TIMEOUT
        )
    except requests.RequestException as exc:
        raise OcrUnavailableError(f"Could not reach the OCR.space API: {exc}") from exc

    if response.status_code != 200:
        raise OcrUnavailableError(
            f"OCR.space API returned HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise OcrUnavailableError("OCR.space API returned a non-JSON response") from exc

    # --- API-level errors -------------------------------------------------
    if body.get("IsErroredOnProcessing"):
        message = body.get("ErrorText") or "unknown processing error"
        results = body.get("ParsedResults") or []
        if results and isinstance(results[0], dict) and results[0].get("ErrorMessage"):
            message = results[0]["ErrorMessage"]
        raise OcrUnavailableError(f"OCR.space failed to process the image: {message}")

    # --- Success: join all parsed pages into one string --------------------
    results = body.get("ParsedResults") or []
    texts = [
        r.get("ParsedText", "")
        for r in results
        if isinstance(r, dict) and r.get("ParsedText")
    ]
    return "\n".join(texts).strip()


def ocr_available() -> bool:
    """True when an OCR.space API key is configured (used by /api/health)."""
    return bool(os.getenv("OCR_SPACE_API_KEY", "").strip())


def _sniff_media_type(data: bytes) -> str:
    """Detect the image format from magic bytes (Pillow-free)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"GIF8":
        return "image/gif"
    if data[:2] == b"BM":
        return "image/bmp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    return "image/png"
