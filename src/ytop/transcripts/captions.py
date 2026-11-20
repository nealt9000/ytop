"""Caption selection, download and normalization.

YouTube exposes two caption collections: ``subtitles`` (creator-provided) and
``automatic_captions`` (ASR).  Both are keyed by language and carry a list of
alternate encodings of the same track.  We prefer creator captions, then an
automatic track, and within a track prefer ``json3`` (the most precise timing
and text), then WebVTT, SRT and finally TTML.

Caption endpoints are aggressively rate-limited (HTTP 429).  A missing track is
therefore a normal outcome, not a library failure: the caller records it and
moves on.  Every download is retried with exponential backoff and honours
``Retry-After`` when YouTube sends one.
"""

from __future__ import annotations

import base64
import gzip
import html
import json
import re
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlsplit

from ..yt.proxies import active_proxy

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
WATCH_URL = "https://www.youtube.com/watch?v={video_id}"
PANEL_URL = "https://www.youtube.com/youtubei/v1/get_panel?prettyPrint=false"
PANEL_ID = "PAmodern_transcript_view"
_PANEL_SESSION: tuple[str, str] | None = None
_PANEL_SESSION_AT = 0.0
_PANEL_SESSION_TTL = 600.0
_PANEL_LOCK = threading.Lock()
# Transcripts of feature-length videos are large; 64 MiB is generous.
MAX_CAPTION_BYTES = 64 * 1024 * 1024
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
EXT_PREFERENCE = ("json3", "vtt", "srt", "ttml")
SOURCE_PREFERENCE = ("manual", "auto")


class CaptionError(RuntimeError):
    """A caption track could not be selected, downloaded or parsed."""


@dataclass(frozen=True)
class Caption:
    language: str
    kind: str  # manual | auto
    ext: str
    url: str
    name: str | None = None

    @property
    def label(self) -> str:
        return self.name or self.language

    @property
    def is_automatic(self) -> bool:
        return self.kind == "auto"


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str

    def to_dict(self) -> dict:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "text": self.text,
        }


def _language_order(available: Iterable[str], preferred: Iterable[str]) -> list[str]:
    keys = list(available)

    def rank(key: str) -> tuple:
        lowered = key.lower()
        for index, lang in enumerate(preferred):
            lang = lang.lower()
            base = lang.split("-", 1)[0]
            if lowered == lang:
                return (index, 0, key)
            if lowered == f"{lang}-orig":
                return (index, 1, key)
            if lowered.startswith(f"{lang}-"):
                return (index, 2, key)
            if lowered == base:
                return (index, 3, key)
            if lowered == f"{base}-orig":
                return (index, 4, key)
            if lowered.startswith(f"{base}-"):
                return (index, 5, key)
        return (len(list(preferred)), 0, key)

    return sorted(keys, key=rank)


def choose_caption(info: dict, languages: Iterable[str]) -> Caption | None:
    """Pick the best available caption track for ``languages``.

    Language preference comes first, then creator captions over automatic ones
    within that language.  Picking *any* manual track before checking the
    preferred language would silently download a Spanish manual transcript when
    the caller asked for English.
    """
    preferred = [lang.strip() for lang in languages if lang and lang.strip()]
    if not preferred:
        preferred = ["en"]

    manual = info.get("subtitles") or {}
    automatic = info.get("automatic_captions") or {}
    manual = manual if isinstance(manual, dict) else {}
    automatic = automatic if isinstance(automatic, dict) else {}
    available = set(manual) | set(automatic)
    for language in _language_order(available, preferred):
        for kind, collection in (("manual", manual), ("auto", automatic)):
            tracks = collection.get(language) or []
            for ext in EXT_PREFERENCE:
                for track in tracks:
                    if not isinstance(track, dict):
                        continue
                    if track.get("ext") != ext or not track.get("url"):
                        continue
                    return Caption(
                        language=language,
                        kind=kind,
                        ext=ext,
                        url=str(track["url"]),
                        name=track.get("name"),
                    )
    return None


def _retry_after(error: urllib.error.HTTPError) -> float | None:
    value = error.headers.get("Retry-After") if error.headers else None
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def download_caption(
    caption: Caption,
    *,
    timeout: float = 30.0,
    retries: int = 4,
    backoff_s: float = 2.0,
) -> bytes:
    """Download a caption track, retrying transient failures.

    ``time.sleep`` is resolved at call time so tests can replace it.
    """
    last: Exception | None = None
    for attempt in range(retries + 1):
        request = urllib.request.Request(
            caption.url,
            headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read(MAX_CAPTION_BYTES)
            if not data:
                raise CaptionError("caption endpoint returned an empty body")
            return data
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in RETRY_STATUS or attempt >= retries:
                break
            delay = _retry_after(exc)
            if delay is None:
                delay = backoff_s * (2 ** attempt)
            if delay > 0:
                time.sleep(min(delay, 120.0))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            if attempt >= retries:
                break
            delay = backoff_s * (2 ** attempt)
            if delay > 0:
                time.sleep(min(delay, 120.0))
    raise CaptionError(f"caption download failed: {last}") from last


_VTT_TIMESTAMP = re.compile(
    r"(?P<h>\d{1,2}):(?P<m>\d{2}):(?P<s>\d{2})[.,](?P<ms>\d{3})"
)
_SRT_TIMESTAMP = re.compile(
    r"(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2}),(?P<ms>\d{3})"
)
_CUE_LINE = re.compile(
    r"^\s*(?P<start>\d{1,3}:\d{2}:\d{2}[.,]\d{1,3})\s*-->\s*"
    r"(?P<end>\d{1,3}:\d{2}:\d{2}[.,]\d{1,3})"
)
_TAG = re.compile(r"<[^>]+>")


def _timestamp(value: str) -> float:
    match = _VTT_TIMESTAMP.search(value) or _SRT_TIMESTAMP.search(value)
    if not match:
        raise CaptionError(f"bad timestamp {value!r}")
    return (
        int(match.group("h")) * 3600
        + int(match.group("m")) * 60
        + int(match.group("s"))
        + int(match.group("ms")) / 1000.0
    )


def _clean(text: str) -> str:
    text = html.unescape(text or "")
    text = _TAG.sub("", text)
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _segments_from_cues(cues: list[tuple[float, float, str]]) -> list[Segment]:
    segments: list[Segment] = []
    for start, end, text in cues:
        text = _clean(text)
        if not text:
            continue
        if end <= start:
            continue
        segments.append(Segment(start=start, end=end, text=text))
    return _normalize(segments)


def _normalize(segments: list[Segment]) -> list[Segment]:
    """Sort, drop duplicates and clamp overlapping timings."""
    segments.sort(key=lambda s: (s.start, s.end))
    out: list[Segment] = []
    for segment in segments:
        if out and segment.text == out[-1].text and segment.start == out[-1].start:
            continue
        if out and segment.start < out[-1].end:
            previous = out[-1]
            # Keep the later segment's start; do not silently drop content.
            if segment.start <= previous.start:
                continue
            out[-1] = Segment(previous.start, min(previous.end, segment.start),
                              previous.text)
        out.append(segment)
    return [s for s in out if s.text and s.end > s.start]


def parse_json3(data: bytes) -> list[Segment]:
    try:
        doc = json.loads(data.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise CaptionError(f"invalid json3 caption: {exc}") from exc

    events = doc.get("events") or []
    raw: list[tuple[float, float | None, str]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        start_ms = event.get("tStartMs")
        if start_ms is None:
            continue
        segs = event.get("segs") or []
        text = "".join(str(seg.get("utf8") or "") for seg in segs if isinstance(seg, dict))
        if not text.strip():
            continue
        duration = event.get("dDurationMs")
        raw.append((
            float(start_ms) / 1000.0,
            float(duration) / 1000.0 if duration is not None else None,
            text,
        ))

    cues: list[tuple[float, float, str]] = []
    for index, (start, duration, text) in enumerate(raw):
        if duration is None or duration <= 0:
            next_start = raw[index + 1][0] if index + 1 < len(raw) else start + 2.0
            duration = max(0.2, next_start - start)
        cues.append((start, start + duration, text))
    return _segments_from_cues(cues)


def parse_vtt(data: bytes) -> list[Segment]:
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    lines = text.split("\n")
    cues: list[tuple[float, float, str]] = []
    index = 0
    while index < len(lines):
        match = _CUE_LINE.match(lines[index])
        if not match:
            index += 1
            continue
        start = _timestamp(match.group("start"))
        end = _timestamp(match.group("end"))
        index += 1
        body: list[str] = []
        while index < len(lines) and lines[index].strip():
            body.append(lines[index])
            index += 1
        cues.append((start, end, " ".join(body)))
    return _segments_from_cues(cues)


def parse_srt(data: bytes) -> list[Segment]:
    return parse_vtt(data)


def parse_ttml(data: bytes) -> list[Segment]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise CaptionError(f"invalid ttml caption: {exc}") from exc
    cues: list[tuple[float, float, str]] = []
    for element in root.iter():
        if not element.tag.endswith("p"):
            continue
        begin = element.attrib.get("begin")
        end = element.attrib.get("end")
        if not begin or not end:
            continue
        try:
            start = _clock(begin)
            finish = _clock(end)
        except ValueError:
            continue
        cues.append((start, finish, "".join(element.itertext())))
    return _segments_from_cues(cues)


def _clock(value: str) -> float:
    value = value.strip()
    if value.endswith("ms"):
        return float(value[:-2]) / 1000.0
    if value.endswith("s"):
        return float(value[:-1])
    parts = value.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    return float(value)



@dataclass(frozen=True)
class CaptionData:
    """A caption track that has already been downloaded and parsed."""

    language: str
    kind: str  # manual | auto
    ext: str
    segments: list[Segment]
    raw: bytes


def fetch_transcript_api(
    video_id: str,
    languages: Iterable[str],
) -> CaptionData | None:
    """Fetch captions through YouTube's player API.

    ``youtube-transcript-api`` uses the InnerTube transcript endpoint rather
    than the ``timedtext`` URL that yt-dlp exposes.  In practice that endpoint
    is far less likely to return HTTP 429, so it is the primary route; the
    yt-dlp URL remains a fallback.  The import is lazy so a missing optional
    dependency degrades to the fallback instead of breaking the CLI.
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        from youtube_transcript_api._errors import (
            AgeRestricted,
            InvalidVideoId,
            IpBlocked,
            NoTranscriptFound,
            RequestBlocked,
            TranscriptsDisabled,
            VideoUnavailable,
            VideoUnplayable,
        )
    except Exception:  # noqa: BLE001 - optional at runtime
        return None

    permanent = (
        AgeRestricted,
        InvalidVideoId,
        IpBlocked,
        NoTranscriptFound,
        TranscriptsDisabled,
        VideoUnavailable,
        VideoUnplayable,
    )

    preferred = [lang.strip() for lang in languages if lang and lang.strip()]
    if not preferred:
        preferred = ["en"]

    proxy_config = None
    proxy = active_proxy()
    if proxy:
        if urlsplit(proxy).scheme not in ("http", "https"):
            # requests has no SOCKS support here; the yt-dlp fallback copes.
            return None
        from youtube_transcript_api.proxies import GenericProxyConfig

        proxy_config = GenericProxyConfig(http_url=proxy, https_url=proxy)

    try:
        api = YouTubeTranscriptApi(proxy_config=proxy_config)
        tracks = list(api.list(video_id))
    except permanent:
        return None

    for kind, group in (
        ("manual", [t for t in tracks if not t.is_generated]),
        ("auto", [t for t in tracks if t.is_generated]),
    ):
        if not group:
            continue
        by_code = {track.language_code: track for track in group}
        for code in _language_order(by_code, preferred):
            track = by_code[code]
            # Transient failures propagate so the caller can retry with
            # backoff; a genuinely absent track returns None instead.
            try:
                fetched = track.fetch()
            except (IpBlocked, RequestBlocked):
                # The HTML/API is reachable but the timedtext host is blocking
                # this IP.  The panel/yt-dlp fallbacks may still work; do not
                # retry the same blocked endpoint for every video.
                return None
            snippets = list(fetched)
            segments = _segments_from_cues([
                (
                    float(snippet.start),
                    float(snippet.start) + float(snippet.duration),
                    str(snippet.text),
                )
                for snippet in snippets
            ])
            if not segments:
                continue
            raw = json.dumps(
                fetched.to_raw_data(), ensure_ascii=False
            ).encode("utf-8")
            return CaptionData(
                language=track.language_code,
                kind=kind,
                ext="json",
                segments=segments,
                raw=raw,
            )
    return None


def _panel_params(video_id: str) -> str:
    """Build the deterministic protobuf params for the transcript panel."""
    inner = b"\x0a\x0b" + video_id.encode("ascii")
    payload = (
        b"\xaa\x09" + bytes([len(inner) + 2]) + inner + b"\x18\x01"
    )
    return base64.b64encode(payload).decode("ascii")


def _panel_seconds(value: str) -> float | None:
    parts = str(value or "").strip().split(":")
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return None
    if len(numbers) == 2:
        return numbers[0] * 60 + numbers[1]
    if len(numbers) == 3:
        return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    return None


def _panel_segments(data: dict) -> list[Segment]:
    rows: list[tuple[float, str]] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            segment = node.get("transcriptSegmentViewModel")
            if isinstance(segment, dict):
                start = _panel_seconds(segment.get("timestamp"))
                text = _clean(str(segment.get("simpleText") or ""))
                if start is not None and text:
                    rows.append((start, text))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    rows.sort(key=lambda row: row[0])
    deduped: list[tuple[float, str]] = []
    for start, text in rows:
        if deduped and start == deduped[-1][0] and text == deduped[-1][1]:
            continue
        deduped.append((start, text))
    segments: list[Segment] = []
    for index, (start, text) in enumerate(deduped):
        if index + 1 < len(deduped):
            end = deduped[index + 1][0]
        else:
            end = start + max(2.0, min(8.0, len(text) / 12.0))
        if end <= start:
            end = start + 1.0
        segments.append(Segment(start=float(start), end=float(end), text=text))
    return segments


def _panel_session(video_id: str, timeout: float) -> tuple[str, str] | None:
    """Return ``(client_version, visitor_data)`` from a watch page.

    The values are cached for a few minutes: one playlist download would
    otherwise fetch a 1.5 MiB watch page for every video just to build the same
    InnerTube context.
    """
    global _PANEL_SESSION, _PANEL_SESSION_AT
    now = time.monotonic()
    with _PANEL_LOCK:
        if _PANEL_SESSION is not None and now - _PANEL_SESSION_AT < _PANEL_SESSION_TTL:
            return _PANEL_SESSION
    try:
        request = urllib.request.Request(
            WATCH_URL.format(video_id=video_id),
            headers={"User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            page = response.read(4 * 1024 * 1024).decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    version = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', page)
    visitor = re.search(r'"VISITOR_DATA":"([^"]+)"', page)
    if version is None or visitor is None:
        return None
    session = (version.group(1), visitor.group(1))
    with _PANEL_LOCK:
        _PANEL_SESSION = session
        _PANEL_SESSION_AT = now
    return session


def _fetch_panel_once(
    video_id: str, session: tuple[str, str], timeout: float
) -> dict | None:
    version, visitor = session
    context = {
        "client": {
            "clientName": "WEB",
            "clientVersion": version,
            "hl": "en",
            "gl": "US",
            "visitorData": visitor,
            "userAgent": USER_AGENT,
            "osName": "X11",
            "osVersion": "",
            "platform": "DESKTOP",
        }
    }
    body = gzip.compress(json.dumps({
        "context": context,
        "panelId": PANEL_ID,
        "params": _panel_params(video_id),
    }).encode("utf-8"))
    request = urllib.request.Request(
        PANEL_URL,
        data=body,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
            "X-Youtube-Client-Name": "1",
            "X-Youtube-Client-Version": version,
            "X-Goog-Visitor-Id": visitor,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read(64 * 1024 * 1024))
    except (
        urllib.error.URLError, TimeoutError, OSError, ValueError,
    ):
        return None


def fetch_panel_api(
    video_id: str,
    *,
    language: str = "en",
    kind: str = "auto",
    timeout: float = 30.0,
) -> CaptionData | None:
    """Fetch the whole transcript through YouTube's current panel API.

    The legacy timedtext endpoint is frequently blocked or PO-token-gated.  The
    modern ``get_panel`` endpoint returns the same transcript text without a PO
    token and is what the current YouTube UI uses.  It selects the video's
    default caption language, so callers pass the language they already chose
    from the extractor metadata.
    """
    global _PANEL_SESSION
    session = _panel_session(video_id, timeout)
    if session is None:
        return None
    data = _fetch_panel_once(video_id, session, timeout)
    if data is None:
        # A stale visitor can be rejected; refresh once and retry.
        with _PANEL_LOCK:
            _PANEL_SESSION = None
        session = _panel_session(video_id, timeout)
        if session is None:
            return None
        data = _fetch_panel_once(video_id, session, timeout)
    if data is None:
        return None
    segments = _panel_segments(data)
    if not segments:
        return None
    return CaptionData(
        language=language or "und",
        kind=kind or "auto",
        ext="panel",
        segments=segments,
        raw=json.dumps(data, ensure_ascii=False).encode("utf-8"),
    )


PARSERS = {
    "json3": parse_json3,
    "vtt": parse_vtt,
    "srt": parse_srt,
    "ttml": parse_ttml,
}


def parse_caption(data: bytes, ext: str) -> list[Segment]:
    parser = PARSERS.get(ext)
    if parser is None:
        raise CaptionError(f"unsupported caption format {ext!r}")
    segments = parser(data)
    if not segments:
        raise CaptionError("caption track contained no timed text")
    return segments


def transcript_text(segments: Iterable[Segment]) -> str:
    """Plain text used for FTS indexing, with sentence-ish segmentation."""
    return "\n".join(segment.text for segment in segments)
