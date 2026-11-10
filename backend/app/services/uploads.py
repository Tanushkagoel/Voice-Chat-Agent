from __future__ import annotations

import re
from pathlib import Path

_NAME = re.compile(r"[^A-Za-z0-9._ -]")


def sniff_image(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def safe_filename(filename: str | None) -> str:
    name = Path(filename or "photo").name
    cleaned = _NAME.sub("", name).strip() or "photo"
    return cleaned[:80]


def extension_for(content_type: str) -> str:
    return {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[content_type]
