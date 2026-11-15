"""Small formatting helpers shared by the web templates."""

from __future__ import annotations

import html
import re
from datetime import datetime
from urllib.parse import quote

_URL = re.compile(r"https?://[^\s<>\"']+")
_TIMESTAMP = re.compile(r"(?<![\w:])(?:\d{1,2}:)?\d{1,2}:\d{2}(?![\w:])")
_TRAILING = ".,;:!?)]}>"


def format_duration(value) -> str:
    try:
        total = int(float(value))
    except (TypeError, ValueError):
        return "—"
    if total < 0:
        return "—"
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def format_hours(value) -> str:
    """Hours as a short reading: "45 min", "3.5 h", "120 h"."""
    try:
        hours = max(0.0, float(value))
    except (TypeError, ValueError):
        return "—"
    if hours < 1:
        return f"{round(hours * 60)} min"
    if hours < 100:
        return f"{hours:.1f} h"
    return f"{round(hours)} h"


def format_timestamp(value) -> str:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return "0:00"
    return format_duration(seconds)


def format_date(value) -> str:
    """Render a YouTube ``YYYYMMDD`` upload date without any locale dependency."""
    if not value:
        return ""
    text = str(value)
    try:
        if len(text) == 8 and text.isdigit():
            date = datetime(int(text[:4]), int(text[4:6]), int(text[6:]))
        else:
            date = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return text
    return f"{date.day} {date:%b %Y}"


def format_finished(value) -> str:
    """Render a job's ISO finish time as ``14 Nov 2026, 02:09`` in local time."""
    if not value:
        return ""
    text = str(value)
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return text
    local = moment.astimezone()
    return f"{local.day} {local:%b %Y, %H:%M}"


def parse_timestamp(text: str) -> int:
    parts = [int(p) for p in text.split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return 0


def _anchor(href: str, label: str, *, css: str, extra: str = "") -> str:
    return (
        f'<a class="{css}" href="{html.escape(href, quote=True)}"{extra}>'
        f"{html.escape(label)}</a>"
    )


def description_html(text: str) -> str:
    """Escape a description, then linkify URLs and timestamp markers.

    YouTube descriptions are plain text.  Rendering them through this function
    keeps them readable offline and makes ``12:34`` markers seek the player,
    without ever trusting the stored text as HTML.
    """
    text = (text or "").strip()
    if not text:
        return '<p class="muted">No description was provided for this video.</p>'

    out: list[str] = []
    index = 0
    pattern = re.compile(f"({_URL.pattern})|({_TIMESTAMP.pattern})")
    for match in pattern.finditer(text):
        out.append(html.escape(text[index:match.start()]))
        url = match.group(1)
        stamp = match.group(2)
        if url:
            clean = url
            suffix = ""
            while clean and clean[-1] in _TRAILING:
                suffix = clean[-1] + suffix
                clean = clean[:-1]
            out.append(_anchor(clean, clean, css="ext",
                              extra=' target="_blank" rel="noreferrer"'))
            out.append(html.escape(suffix))
        elif stamp:
            seconds = parse_timestamp(stamp)
            out.append(_anchor(
                f"#t={seconds}", stamp, css="ts-link",
                extra=f' data-seek="{seconds}"',
            ))
        index = match.end()
    out.append(html.escape(text[index:]))
    return "".join(out).replace("\n", "<br>")


def snippet(text: str, limit: int = 180) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def media_kind(path) -> str:
    suffix = getattr(path, "suffix", "") or ""
    return {
        ".webm": "video/webm",
        ".mp4": "video/mp4",
        ".m4v": "video/mp4",
        ".mkv": "video/x-matroska",
        ".mov": "video/quicktime",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".ogg": "audio/ogg",
        ".opus": "audio/ogg",
    }.get(suffix.lower(), "application/octet-stream")


def stream_url(playlist_id: str, video_id: str) -> str:
    return f"/media/{quote(playlist_id)}/{quote(video_id)}"


def thumb_url(playlist_id: str, video_id: str) -> str:
    return f"/media/{quote(playlist_id)}/{quote(video_id)}/thumb"
