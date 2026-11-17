"""Slim the yt-dlp info dict before it is kept as ``raw.json``.

yt-dlp's info dict averages megabytes per video, almost all of it signed
stream URLs (``formats``), fragment lists, and one caption URL per format for
every translated language (``automatic_captions``).  Nothing reads those back
from disk.  The saved snapshot is read only for:

* ``categories`` / ``tags`` -- the index backfill (``ytop.index``);
* ``subtitles`` / ``automatic_captions`` -- the transcript fallback, through
  ``choose_caption`` (language, ``ext``, ``url``, ``name`` of a track).

Everything else at the top level is kept as is (it is small); the bulky parts
are slimmed so they still describe what was offered.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlencode, urlparse

from ..transcripts.captions import EXT_PREFERENCE

# Top-level keys that are pure bulk: per-request URLs, headers, fragment lists,
# the thumbnail candidate list (``thumbnail`` itself is kept) and the replay
# heatmap.  ``requested_downloads`` repeats ``requested_formats``.
_DROP_KEYS = frozenset({
    "thumbnails",
    "heatmap",
    "http_headers",
    "url",
    "manifest_url",
    "fragment_base_url",
    "fragments",
    "downloader_options",
    "requested_downloads",
})

# What a format entry keeps: enough to show or re-rank the quality ladder
# (the keys ``ytop.yt.formats`` reads), without URLs or fragments.
_FORMAT_KEYS = (
    "format_id", "format", "format_note", "ext", "protocol", "container",
    "vcodec", "acodec", "width", "height", "resolution", "fps",
    "dynamic_range", "tbr", "vbr", "abr", "asr", "audio_channels",
    "filesize", "filesize_approx", "language", "language_preference",
    "quality", "source_preference", "has_drm", "audio_track",
)

_CAPTION_KEYS = ("ext", "url", "name")


def _slim_format(fmt: object) -> object:
    if not isinstance(fmt, dict):
        return fmt
    slim = {key: fmt[key] for key in _FORMAT_KEYS if key in fmt}
    # The audio-track labels read ``xtags`` from the stream URL's query
    # (``acont=original`` / ``dubbed-auto``); keep just that parameter.
    url = fmt.get("url")
    if isinstance(url, str) and url:
        try:
            xtags = parse_qs(urlparse(url).query).get("xtags")
        except ValueError:
            xtags = None
        if xtags:
            slim["url"] = "?" + urlencode({"xtags": xtags[0]})
    return slim


def _slim_formats(value: object) -> object:
    if not isinstance(value, list):
        return value
    return [_slim_format(fmt) for fmt in value]


def _slim_captions(value: object) -> object:
    """Keep, per language, only the track ``choose_caption`` would pick."""
    if not isinstance(value, dict):
        return value
    slim: dict = {}
    for language, tracks in value.items():
        if not isinstance(tracks, list):
            continue
        best = None
        for ext in EXT_PREFERENCE:
            best = next(
                (t for t in tracks
                 if isinstance(t, dict) and t.get("ext") == ext and t.get("url")),
                None,
            )
            if best is not None:
                break
        if best is not None:
            slim[language] = [
                {key: best[key] for key in _CAPTION_KEYS if key in best}
            ]
    return slim


def trim_raw_info(info: dict) -> dict:
    """Return a slimmed copy of a yt-dlp info dict for ``raw.json``.

    ``info`` itself is not changed: the download still needs its URLs.
    Idempotent, so an already trimmed document comes back the same.
    """
    trimmed: dict = {}
    for key, value in info.items():
        if key in _DROP_KEYS:
            continue
        if key in ("formats", "requested_formats"):
            value = _slim_formats(value)
        elif key in ("subtitles", "automatic_captions"):
            value = _slim_captions(value)
        trimmed[key] = value
    return trimmed
