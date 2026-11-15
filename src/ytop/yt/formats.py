"""Per-video format policy.

The policy is deliberately independent of yt-dlp's selector strings:

* take the **native maximum height**;
* within that height prefer AV1, then VP9, then H.264;
* pick the smallest original-language audio that fits the container.

That is what the P0 measurements support.  A blanket resolution cap would throw
away the two 720p items in the initial playlist, and preferring a codec before
resolution would silently downgrade those items to 240p AV1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

VIDEO_CODEC_ORDER = ("av01", "vp9", "avc1")


class FormatError(RuntimeError):
    """No usable format was offered by the extractor."""


def video_family(fmt: dict) -> str:
    codec = (fmt.get("vcodec") or "").lower()
    if codec.startswith("av01"):
        return "av01"
    if codec.startswith("vp9") or codec.startswith("vp09"):
        return "vp9"
    if codec.startswith("avc1"):
        return "avc1"
    return codec or "unknown"


def audio_family(fmt: dict | None) -> str:
    if not fmt:
        return ""
    codec = (fmt.get("acodec") or "").lower()
    if codec.startswith("opus"):
        return "opus"
    if codec.startswith("mp4a"):
        return "mp4a"
    return codec or "unknown"


def _filesize(fmt: dict) -> int:
    size = fmt.get("filesize") or fmt.get("filesize_approx")
    return int(size) if size else 0


def _number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _stream_bytes(fmt: dict, duration: float) -> int:
    """Estimate one stream on its own.

    Mixing known and unknown stream sizes used to understate the total: if the
    video advertised a filesize but an alternate audio track did not, the
    unknown track contributed nothing.  Each stream now falls back to its own
    duration/bitrate.
    """
    size = _filesize(fmt)
    if size:
        return size
    own_duration = _number(fmt.get("duration") or 0) or duration
    bitrate = _number(
        fmt.get("tbr") or fmt.get("vbr") or fmt.get("abr") or 0
    )
    if own_duration and bitrate:
        return int(bitrate * 1000 / 8 * own_duration)
    return 0


def _is_video(fmt: dict) -> bool:
    if (fmt.get("vcodec") or "none") == "none":
        return False
    if fmt.get("has_drm"):
        return False
    if not fmt.get("height"):
        return False
    if (fmt.get("ext") or "") == "mhtml":
        return False
    if (fmt.get("protocol") or "") == "mhtml":
        return False
    if "storyboard" in (fmt.get("format_note") or "").lower():
        return False
    return bool(fmt.get("format_id"))


def _is_audio(fmt: dict) -> bool:
    if (fmt.get("acodec") or "none") == "none":
        return False
    if (fmt.get("vcodec") or "none") != "none":
        return False
    if (fmt.get("ext") or "") == "mhtml":
        return False
    return bool(fmt.get("format_id"))


def _video_key(fmt: dict) -> tuple:
    dynamic_range = (fmt.get("dynamic_range") or "SDR").upper()
    protocol = (fmt.get("protocol") or "").lower()
    return (
        1 if dynamic_range == "SDR" else 0,
        _number(fmt.get("fps")),
        # Prefer a rendition whose size is known and that downloads over plain
        # HTTPS.  YouTube also lists the same stream as HLS with a bogus tbr and
        # no filesize; choosing that one both downloads slower and breaks the
        # pre-download size estimate.
        1 if _filesize(fmt) else 0,
        1 if protocol in ("https", "http") else 0,
        _number(fmt.get("tbr")),
        _filesize(fmt),
    )


def _audio_key(fmt: dict) -> tuple:
    note = (fmt.get("format_note") or "").lower()
    original = 1 if ("original" in note or "default" in note) else 0
    codec_rank = {"opus": 2, "mp4a": 1}.get(audio_family(fmt), 0)
    # Smaller bitrate wins; the key is sorted with max().
    return (original, codec_rank, -_number(fmt.get("abr")), -_filesize(fmt))


def _best_audio_key(fmt: dict) -> tuple:
    """Sort key for ``preserve_audio``: the highest-bitrate track wins."""
    codec_rank = {"opus": 2, "mp4a": 1}.get(audio_family(fmt), 0)
    return (_number(fmt.get("abr")), _filesize(fmt), codec_rank)


def _audio_xtags(fmt: dict | None) -> str:
    if not fmt:
        return ""
    url = str(fmt.get("url") or "")
    if not url:
        return ""
    try:
        return parse_qs(urlparse(url).query).get("xtags", [""])[0]
    except ValueError:
        return ""


def audio_is_original(fmt: dict | None) -> bool:
    """True when the extractor labels this as the video's original audio."""
    if not fmt:
        return False
    xtags = _audio_xtags(fmt)
    if "acont=original" in xtags:
        return True
    if "acont=dubbed-auto" in xtags:
        return False
    return "original" in (fmt.get("format_note") or "").lower()


def audio_is_auto_dubbed(fmt: dict | None) -> bool:
    """True for YouTube's generated dub tracks."""
    if not fmt:
        return False
    xtags = _audio_xtags(fmt)
    if "acont=dubbed-auto" in xtags:
        return True
    if "acont=original" in xtags:
        return False
    note = (fmt.get("format_note") or "").lower()
    return "original" not in note and bool(note)


def audio_is_default(fmt: dict | None) -> bool:
    """True when YouTube marks this as the default audio track."""
    if not fmt:
        return False
    track = fmt.get("audio_track") or {}
    if isinstance(track, dict) and track.get("audio_is_default"):
        return True
    note = (fmt.get("format_note") or "").lower()
    return "default" in note


_QUALITY_SUFFIX = re.compile(
    r",\s*(?:low|medium|high|drc)(?:\s*,\s*drc)?\s*$", re.IGNORECASE
)


def audio_label(fmt: dict | None) -> str:
    """A readable audio-track label without the bitrate tier suffix."""
    if not fmt:
        return ""
    note = str(fmt.get("format_note") or "").strip()
    note = _QUALITY_SUFFIX.sub("", note)
    note = note.strip(" ,")
    if note.lower() in {"low", "medium", "high", "drc", "audio"}:
        note = ""
    if note:
        return note
    return str(fmt.get("language") or "unknown")


def audio_detail(fmt: dict | None) -> str:
    """Short codec + bitrate text such as ``Opus 160k``."""
    if not fmt:
        return ""
    family = audio_family(fmt)
    label = {"opus": "Opus", "mp4a": "AAC", "vorbis": "Vorbis",
             "flac": "FLAC"}.get(family, family.upper() if family else "")
    abr = _number(fmt.get("abr"))
    if label and abr:
        return f"{label} {abr:.0f}k"
    return label


@dataclass
class Selection:
    video: dict
    audio: dict | None = None
    # Additional audio tracks stored beside the media and offered in the
    # player.  The first/primary audio is merged into the video file.
    alternates: list[dict] = field(default_factory=list)

    @property
    def container(self) -> str | None:
        vfam = video_family(self.video)
        if self.audio is None:
            return None
        afam = audio_family(self.audio)
        if vfam in ("av01", "vp9") and afam == "opus":
            return "webm"
        if vfam == "avc1" and afam == "mp4a":
            return "mp4"
        if vfam in ("av01", "vp9") and afam == "mp4a":
            return "mp4"
        # H.264 + Opus is not a WebM/MP4 combination; Matroska keeps both
        # streams without a transcode.
        return "mkv"

    @property
    def format_spec(self) -> str:
        if self.audio is None:
            return str(self.video["format_id"])
        return f"{self.video['format_id']}+{self.audio['format_id']}"

    @property
    def video_family(self) -> str:
        return video_family(self.video)

    @property
    def audio_family(self) -> str:
        return audio_family(self.audio)

    @property
    def audio_detail(self) -> str:
        return audio_detail(self.audio)

    @property
    def audio_bitrate(self) -> int | None:
        if self.audio is None:
            return None
        abr = _number(self.audio.get("abr"))
        return int(round(abr)) if abr else None

    @property
    def audio_channels(self) -> int | None:
        if self.audio is None:
            return None
        try:
            channels = int(self.audio.get("audio_channels") or 0)
        except (TypeError, ValueError):
            return None
        return channels or None

    @property
    def audio_label(self) -> str:
        return audio_label(self.audio)

    @property
    def audio_original(self) -> bool:
        return audio_is_original(self.audio)

    @property
    def audio_auto_dubbed(self) -> bool:
        return audio_is_auto_dubbed(self.audio)

    @property
    def estimated_bytes(self) -> int:
        duration = _number(self.video.get("duration") or 0)
        total = _stream_bytes(self.video, duration)
        if self.audio is not None:
            total += _stream_bytes(self.audio, duration)
        total += sum(_stream_bytes(fmt, duration) for fmt in self.alternates)
        return total

    def summary(self) -> str:
        v = self.video
        height = v.get("height") or 0
        fps = _number(v.get("fps"))
        fps_text = f"{fps:g} fps " if fps else ""
        audio = self.audio
        if audio:
            lang = audio.get("language") or "?"
            abr = _number(audio.get("abr"))
            abr_text = f" {abr:.0f}k" if abr else ""
            audio_text = f" + {self.audio_family}{abr_text} ({lang})"
            if self.alternates:
                audio_text += f" + {len(self.alternates)} alt"
        else:
            audio_text = " (no audio)"
        container = f" [{self.container}]" if self.container else ""
        return f"{height}p {self.video_family} {fps_text}{audio_text}{container}"


def select_video(info: dict, max_height: int | None = None) -> dict:
    """Pick the best video at or below ``max_height``.

    ``None`` or ``0`` means the native maximum.  When a playlist has no
    rendition at or below the cap, the smallest available height is used rather
    than silently downloading a much larger file.
    """
    videos = [f for f in info.get("formats") or [] if _is_video(f)]
    if not videos:
        raise FormatError("no video formats available (private, live, or DRM-only?)")

    if max_height and max_height > 0:
        allowed = [f for f in videos if int(f["height"]) <= int(max_height)]
        if allowed:
            videos = allowed
        else:
            lowest = min(int(f["height"]) for f in videos)
            videos = [f for f in videos if int(f["height"]) == lowest]

    height = max(int(f["height"]) for f in videos)
    at_max = [f for f in videos if int(f["height"]) == height]

    for family in VIDEO_CODEC_ORDER:
        group = [f for f in at_max if video_family(f) == family]
        if group:
            return max(group, key=_video_key)
    return max(at_max, key=_video_key)


def select_audio(
    info: dict,
    video: dict,
    audio_language: str = "en",
    preserve_audio: bool = False,
) -> dict | None:
    audios = [f for f in info.get("formats") or [] if _is_audio(f)]
    if not audios:
        return None

    language = (audio_language or "").lower()

    def language_matches(fmt: dict) -> bool:
        lang = (fmt.get("language") or "").lower()
        if not language:
            return True
        # Untagged audio is assumed to be the original track.
        return not lang or lang.startswith(language) or lang == "und"

    # The original performance always wins over the preferred UI language.
    # YouTube can expose auto-dubbed English tracks on videos whose original
    # language is Spanish (or another language); downloading the dub by
    # default is wrong even when the user reads English.
    originals = [f for f in audios if audio_is_original(f)]
    if originals:
        pool = originals
    else:
        defaults = [f for f in audios if audio_is_default(f)]
        pool = defaults or [f for f in audios if language_matches(f)] or audios

    preferred = "opus" if video_family(video) in ("av01", "vp9") else (
        "mp4a" if video_family(video) == "avc1" else None
    )
    if preserve_audio:
        # Music playlists care more about the audio than the frame budget, but
        # the browser still has to play the result: keep the container-native
        # audio family (Opus for AV1/VP9, AAC for H.264) and only then take the
        # highest bitrate.  Otherwise H.264 + Opus forces a Matroska file that
        # many browsers refuse.
        if preferred:
            matching = [f for f in pool if audio_family(f) == preferred]
            if matching:
                pool = matching
        return max(pool, key=_best_audio_key)

    if preferred:
        matching = [f for f in pool if audio_family(f) == preferred]
        if matching:
            pool = matching

    return max(pool, key=_audio_key)


def select_alternate_audio(
    info: dict,
    video: dict,
    primary: dict | None,
    audio_language: str = "en",
    preserve_audio: bool = False,
) -> dict | None:
    """Pick the preferred-language track when it differs from the original.

    This is the track the previous language-only policy would have selected,
    usually the YouTube-UI default language.  It is stored separately so the
    player can offer a switch without re-encoding the media.
    """
    if primary is None:
        return None
    audios = [f for f in info.get("formats") or [] if _is_audio(f)]
    if not audios:
        return None
    primary_id = str(primary.get("format_id") or "")
    primary_lang = str(primary.get("language") or "").lower().split("-", 1)[0]
    language = (audio_language or "").lower()

    def language_matches(fmt: dict) -> bool:
        lang = (fmt.get("language") or "").lower()
        if not language:
            return True
        return not lang or lang.startswith(language) or lang == "und"

    candidates = [
        f for f in audios
        if str(f.get("format_id") or "") != primary_id
        and language_matches(f)
        and not audio_is_original(f)
        and str(f.get("language") or "").lower().split("-", 1)[0]
        != primary_lang
    ]
    if not candidates:
        return None
    preferred = "opus" if video_family(video) in ("av01", "vp9") else (
        "mp4a" if video_family(video) == "avc1" else None
    )
    if preferred:
        matching = [f for f in candidates if audio_family(f) == preferred]
        if matching:
            candidates = matching
    if preserve_audio:
        return max(candidates, key=_best_audio_key)
    return max(candidates, key=_audio_key)


def select_format(
    info: dict,
    audio_language: str = "en",
    max_height: int | None = None,
    preserve_audio: bool = False,
) -> Selection:
    video = select_video(info, max_height)
    audio = select_audio(info, video, audio_language, preserve_audio)
    alternate = select_alternate_audio(
        info, video, audio, audio_language, preserve_audio
    )
    return Selection(
        video=video,
        audio=audio,
        alternates=[alternate] if alternate is not None else [],
    )


def allocate_efficient(
    items,
    *,
    target_mb_per_hour: float,
    audio_language: str = "en",
    min_height: int = 240,
    max_height: int = 0,
    preserve_audio: bool = False,
) -> int:
    """Allocate a playlist-wide size budget across all probed videos.

    Each video starts at the smallest legible rendition (at least
    ``min_height`` when one exists).  The remaining budget is spent on the
    upgrades that buy the most height per extra byte.  This avoids per-video
    budgets that push long videos to 144p while leaving the playlist far below
    its target.
    """
    items = list(items)
    if not items or target_mb_per_hour <= 0:
        return 0
    total_duration = sum(float(getattr(item, "duration", 0) or 0)
                         for item in items)
    budget = int(float(target_mb_per_hour) * 1_000_000 * total_duration / 3600)

    plans: list[dict | None] = []
    for item in items:
        info = getattr(item, "info", None)
        if not info:
            plans.append(None)
            continue
        heights = sorted({
            int(fmt["height"])
            for fmt in info.get("formats") or []
            if _is_video(fmt)
        })
        candidates: list[tuple[int, Selection]] = []
        for height in heights:
            if max_height and height > int(max_height):
                continue
            try:
                selection = select_format(
                    info, audio_language, height, preserve_audio
                )
            except FormatError:
                continue
            if selection.estimated_bytes > 0:
                candidates.append((height, selection))
        if not candidates:
            try:
                selection = select_format(
                    info, audio_language, max_height, preserve_audio
                )
            except FormatError:
                plans.append(None)
                continue
            candidates = [(int(selection.video.get("height") or 0), selection)]
        above = [c for c in candidates if c[0] >= int(min_height)]
        pick = above[0] if above else candidates[0]
        plans.append({
            "candidates": candidates,
            "index": candidates.index(pick),
            "pick": pick,
        })

    total = sum(plan["pick"][1].estimated_bytes for plan in plans if plan)
    while total < budget:
        best: tuple[float, int, tuple] | None = None
        for position, plan in enumerate(plans):
            if plan is None:
                continue
            candidates = plan["candidates"]
            index = plan["index"]
            if index + 1 >= len(candidates):
                continue
            nxt = candidates[index + 1]
            extra = nxt[1].estimated_bytes - plan["pick"][1].estimated_bytes
            if extra <= 0:
                continue
            gain = nxt[0] - plan["pick"][0]
            score = gain / extra
            if total + extra <= budget and (best is None or score > best[0]):
                best = (score, position, nxt)
        if best is None:
            break
        _, position, nxt = best
        plan = plans[position]
        assert plan is not None
        plan["index"] += 1
        plan["pick"] = nxt
        total += nxt[1].estimated_bytes - (
            plan["candidates"][plan["index"] - 1][1].estimated_bytes
        )

    for item, plan in zip(items, plans):
        if plan is not None:
            item.selection = plan["pick"][1]
    return total
