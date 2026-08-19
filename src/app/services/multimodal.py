# Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
# SPDX-License-Identifier: Apache-2.0

"""Multimodal attachments for binary workspace files (images / PDFs).

Tool results stay small JSON markers. Before the next LiteLLM call, the hub
appends a user message with provider-friendly content parts (data URIs).
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.config import Config
from app.utils.logger import logger

# Extension → (kind, mime)
_IMAGE_EXTS = {
    ".png": ("image", "image/png"),
    ".jpg": ("image", "image/jpeg"),
    ".jpeg": ("image", "image/jpeg"),
    ".gif": ("image", "image/gif"),
    ".webp": ("image", "image/webp"),
    ".bmp": ("image", "image/bmp"),
}
_PDF_EXTS = {
    ".pdf": ("pdf", "application/pdf"),
}

# Magic-byte sniffers (kind, mime)
_MAGIC: List[Tuple[bytes, str, str]] = [
    (b"\x89PNG\r\n\x1a\n", "image", "image/png"),
    (b"\xff\xd8\xff", "image", "image/jpeg"),
    (b"GIF87a", "image", "image/gif"),
    (b"GIF89a", "image", "image/gif"),
    (b"RIFF", "image", "image/webp"),  # refined below
    (b"%PDF", "pdf", "application/pdf"),
]


def detect_media(path: Path, head: bytes) -> Optional[Tuple[str, str]]:
    """Return ``(kind, mime)`` for a supported binary file, else None."""
    ext = path.suffix.lower()
    if ext in _IMAGE_EXTS:
        return _IMAGE_EXTS[ext]
    if ext in _PDF_EXTS:
        return _PDF_EXTS[ext]

    for magic, kind, mime in _MAGIC:
        if head.startswith(magic):
            if magic == b"RIFF" and not (len(head) >= 12 and head[8:12] == b"WEBP"):
                continue
            return kind, mime

    guessed, _ = mimetypes.guess_type(str(path))
    if guessed and guessed.startswith("image/"):
        return "image", guessed
    if guessed == "application/pdf":
        return "pdf", guessed
    return None


def model_supports_multimodal(model: Optional[str]) -> bool:
    """Heuristic: does this LiteLLM model id likely accept image/file parts?"""
    if not Config.MULTIMODAL_ENABLED:
        return False
    name = (model or "").strip().lower()
    if not name:
        return False
    # Explicit allow/deny overrides
    deny = [p.strip().lower() for p in Config.MULTIMODAL_MODEL_DENYLIST.split(",") if p.strip()]
    if any(p in name for p in deny):
        return False
    allow = [p.strip().lower() for p in Config.MULTIMODAL_MODEL_ALLOWLIST.split(",") if p.strip()]
    if allow:
        return any(p in name for p in allow)
    markers = [p.strip().lower() for p in Config.MULTIMODAL_VISION_MARKERS.split(",") if p.strip()]
    if not markers:
        return False
    return any(m in name for m in markers)


def build_media_marker(
    *,
    rel_path: str,
    abs_path: str,
    kind: str,
    mime: str,
    size: int,
) -> Dict[str, Any]:
    """Small tool-result payload (no base64)."""
    max_bytes = Config.MULTIMODAL_MAX_BYTES
    too_large = size > max_bytes
    return {
        "path": rel_path,
        "kind": kind,
        "mimeType": mime,
        "bytes": size,
        "multimodal": True,
        "absolutePath": abs_path,
        "tooLarge": too_large,
        "maxBytes": max_bytes,
        "note": (
            f"Binary {kind} exceeds MULTIMODAL_MAX_BYTES ({max_bytes}); not attached."
            if too_large
            else f"Binary {kind} will be attached for vision-capable models on the next LLM turn."
        ),
    }


def _data_uri(mime: str, raw: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def content_parts_for_marker(marker: Dict[str, Any], *, model: Optional[str]) -> List[dict]:
    """Build LiteLLM content parts for one media marker (may be empty)."""
    if not marker.get("multimodal") or marker.get("tooLarge"):
        return []
    if not model_supports_multimodal(model):
        return [
            {
                "type": "text",
                "text": (
                    f"[multimodal skipped] Model '{model}' is not treated as vision-capable; "
                    f"file {marker.get('path')} ({marker.get('mimeType')}) was not attached. "
                    f"Set MULTIMODAL_MODEL_ALLOWLIST or use a vision model."
                ),
            }
        ]

    abs_path = marker.get("absolutePath")
    if not abs_path:
        return []
    path = Path(abs_path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        logger.warning("Failed to load multimodal file %s: %s", abs_path, exc)
        return [{"type": "text", "text": f"[multimodal error] Could not read {marker.get('path')}: {exc}"}]

    if len(raw) > Config.MULTIMODAL_MAX_BYTES:
        return [
            {
                "type": "text",
                "text": f"[multimodal skipped] {marker.get('path')} exceeds size cap after read.",
            }
        ]

    mime = str(marker.get("mimeType") or "application/octet-stream")
    kind = str(marker.get("kind") or "")
    rel = marker.get("path") or path.name
    uri = _data_uri(mime, raw)
    parts: List[dict] = [
        {"type": "text", "text": f"Attached workspace file `{rel}` ({mime}, {len(raw)} bytes)."}
    ]

    if kind == "image" or mime.startswith("image/"):
        parts.append({"type": "image_url", "image_url": {"url": uri}})
        return parts

    if kind == "pdf" or mime == "application/pdf":
        # LiteLLM / OpenAI-compatible document part (Anthropic + some others).
        parts.append(
            {
                "type": "file",
                "file": {
                    "filename": path.name,
                    "file_data": uri,
                },
            }
        )
        return parts

    return [
        {
            "type": "text",
            "text": f"[multimodal skipped] Unsupported kind for {rel}: {kind}/{mime}",
        }
    ]


def build_multimodal_user_message(
    markers: List[Dict[str, Any]],
    *,
    model: Optional[str],
) -> Optional[dict]:
    """Assemble a single user message carrying all pending media attachments."""
    if not markers or not Config.MULTIMODAL_ENABLED:
        return None
    content: List[dict] = []
    for marker in markers:
        content.extend(content_parts_for_marker(marker, model=model))
    if not content:
        return None
    return {"role": "user", "content": content}
