"""Shared utility functions."""

from __future__ import annotations

import hashlib
import re


def safe_skill_slug(name: str) -> str:
    raw = str(name or "").strip()
    slug = re.sub(r"[^A-Za-z0-9_.\-\u4e00-\u9fff]+", "-", raw)
    slug = re.sub(r"-{2,}", "-", slug).strip("-.")
    if slug:
        return slug[:120].rstrip("-.") or slug[:120]
    if raw:
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
        return f"skill-{digest}"
    return "unknown"
