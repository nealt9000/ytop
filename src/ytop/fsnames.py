"""FAT-safe file and folder names that keep Unicode."""

from __future__ import annotations

import re
import unicodedata

_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_WS = re.compile(r"\s+")
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {
    f"LPT{i}" for i in range(1, 10)
}
_MAX_STEM = 100


def safe_name(value: str, limit: int = 100, fallback: str = "Untitled") -> str:
    text = unicodedata.normalize("NFC", value)
    text = _FORBIDDEN.sub(" ", text)
    text = _WS.sub(" ", text).strip(" .")
    if not text:
        return fallback
    head, dot, tail = text.partition(".")
    if head.strip().upper() in _RESERVED:
        text = f"{head}_{dot}{tail}"
    text = text[:limit].rstrip(" .")
    return text or fallback


def numbered_stem(index: int, total: int, title: str) -> str:
    width = max(2, len(str(total)))
    prefix = f"{index:0{width}d} - "
    name = safe_name(title, limit=max(1, _MAX_STEM - len(prefix)))
    return f"{prefix}{name}"


def plan_names(rows: list[tuple[str, str]], numbered: bool) -> dict[str, str]:
    total = len(rows)
    result: dict[str, str] = {}
    seen: set[str] = set()
    for index, (video_id, title) in enumerate(rows, start=1):
        stem = numbered_stem(index, total, title) if numbered else safe_name(title)
        if stem.casefold() in seen:
            stem = f"{stem} [{video_id}]"
        seen.add(stem.casefold())
        result[video_id] = stem
    return result
