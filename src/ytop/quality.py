"""How a stored video's rendition reads in the UI (pure; no web imports, so
the library index can store it too)."""

from __future__ import annotations


def number(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def codec_label(value) -> str:
    """Short, readable codec names for the library UI."""
    if not value:
        return ""
    codec = str(value).strip().lower()
    if codec.startswith(("av01", "av1")):
        return "AV1"
    if codec.startswith(("vp09", "vp9")):
        return "VP9"
    if codec.startswith(("avc1", "h264", "h.264")):
        return "H.264"
    if codec.startswith(("hev1", "hvc1", "hevc")):
        return "HEVC"
    if codec.startswith("vp8"):
        return "VP8"
    return codec.upper()


def audio_codec_label(value) -> str:
    """Short, readable audio codec names for the library UI."""
    if not value:
        return ""
    codec = str(value).strip().lower()
    if codec.startswith("opus"):
        return "Opus"
    if codec.startswith(("mp4a", "aac")):
        return "AAC"
    if codec.startswith("vorbis"):
        return "Vorbis"
    if codec.startswith("flac"):
        return "FLAC"
    return codec.upper()


def audio_quality(meta: dict) -> dict:
    """Describe the primary audio track stored with one video."""
    fmt = meta.get("format") if isinstance(meta.get("format"), dict) else {}
    tracks = [
        track for track in meta.get("audio_tracks") or []
        if isinstance(track, dict)
    ]
    primary = next(
        (track for track in tracks if track.get("embedded")),
        tracks[0] if tracks else None,
    )
    codec = audio_codec_label(
        fmt.get("audio_codec") or (primary or {}).get("codec")
    )
    bitrate = number(fmt.get("audio_bitrate"))
    if bitrate is None and primary is not None:
        bitrate = number(primary.get("bitrate"))
    label = str(
        fmt.get("audio_label") or (primary or {}).get("label") or ""
    )
    original = fmt.get("audio_original")
    if original is None and primary is not None:
        original = bool(primary.get("original"))
    detail = " ".join(
        part for part in (
            codec,
            f"{int(round(bitrate))}k" if bitrate else "",
        ) if part
    )
    return {
        "audio_codec": codec,
        "audio_bitrate": int(round(bitrate)) if bitrate else None,
        "audio_detail": detail,
        "audio_label": label,
        "audio_original": bool(original),
    }


def container_label(value) -> str:
    container = str(value or "").strip().lower().lstrip(".")
    return {
        "webm": "WebM", "mp4": "MP4", "m4v": "M4V",
        "mkv": "MKV", "mov": "MOV",
    }.get(container, container.upper())
