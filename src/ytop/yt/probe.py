"""Playlist enumeration and per-video format probing."""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, ExtractorError

from ..config import Config
from ..timing import Phase, PhaseClock, human_seconds, percentile
from ..toolchain import deno_path
from . import runner as ytdlp_runner
from .formats import (
    FormatError,
    Selection,
    allocate_efficient,
    select_format,
)
from .pot import apply_auth_options


class ProbeError(RuntimeError):
    """The playlist or video could not be read."""


class JobCancelled(RuntimeError):
    """The user cancelled a background probe or download."""


# yt-dlp failures that mean YouTube changed something a component update
# can fix (extractor, signature, n-challenge, PO token, bot check), as
# (what, pattern).  Matched case-insensitively against the error message
# and its causes; anything else is "other" (a private or removed video, an
# age gate, the network, the disk).
YOUTUBE_CHANGED: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (what, re.compile(pattern, re.IGNORECASE)) for what, pattern in (
        ("extractor", r"unable to extract|failed to extract any player response"
                      r"|failed to parse json|please report this issue on"
                      r"|precondition check failed"),
        ("signature", r"signature (?:extraction|solving|decipher\w*) failed"
                      r"|unable to decrypt signature|\bsig(?:nature)? function"),
        ("n-challenge", r"\bn[ -]challenge|\bnsig\b|n-?parameter|challenge solv"
                        r"|javascript runtime|js runtime"),
        ("po-token", r"\bpo[ _-]?token|botguard|forcing sabr|sabr streaming"),
        ("bot-check", r"sign in to confirm(?! your age)"),
        ("formats", r"no video formats found|http error 403"),
    )
)


def classify_failure(exc: BaseException | str | None) -> str:
    """``"youtube_changed"`` when yt-dlp's error looks like YouTube changed
    under it (see ``YOUTUBE_CHANGED``), else ``"other"``."""
    texts: list[str] = []
    seen: set[int] = set()
    current: object = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        texts.append(str(current))
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
    text = "\n".join(texts)
    if any(pattern.search(text) for _what, pattern in YOUTUBE_CHANGED):
        return "youtube_changed"
    return "other"


@dataclass
class ProbedItem:
    index: int
    id: str
    url: str
    title: str = ""
    duration: float | None = None
    info: dict | None = None
    selection: Selection | None = None
    error: str | None = None
    # Wall-clock of this video's extraction.
    seconds: float = 0.0
    # Already in the library with its media: not extracted (``info`` and
    # ``selection`` stay empty); the download keeps what is on disk.
    existing: bool = False


@dataclass
class ProbeResult:
    url: str
    playlist: dict
    items: list[ProbedItem] = field(default_factory=list)
    # Wall-clock per step (listing, videos, allocating); see ytop.timing.
    timings: list[Phase] = field(default_factory=list)

    @property
    def estimated_bytes(self) -> int:
        return sum(i.selection.estimated_bytes for i in self.items if i.selection)

    @property
    def failures(self) -> list[ProbedItem]:
        return [
            i for i in self.items
            if i.error or (i.selection is None and not i.existing)
        ]

    @property
    def existing(self) -> list[ProbedItem]:
        return [i for i in self.items if i.existing]


def deno_dir(library_root: Path) -> Path:
    """Deno's cache inside a library: ``<library>/.ytop/cache/deno``."""
    from ..layout import LibraryLayout  # layout imports ytop.yt

    return LibraryLayout(Path(library_root)).cache_dir / "deno"


def use_library_deno_dir(library_root: Path) -> Path:
    """Point ``DENO_DIR`` at the library's cache for this process.

    yt-dlp runs Deno for YouTube's JS challenges as a subprocess that
    inherits ``os.environ``; without this Deno writes to the host's
    ``~/.cache/deno`` (``~/Library/Caches/deno``, ``%LOCALAPPDATA%\\deno``).
    Portable serve calls it with the launch library at startup.

    It also sets ``XDG_CACHE_HOME`` to the library's ``.ytop/cache``: the
    bgutil PO-token script's cache (``<XDG_CACHE_HOME>/bgutil-ytdlp-pot-provider``,
    where the plugin reads and writes) has no extractor argument, only that
    variable, and would otherwise land in the host's ``~/.cache``.  Nothing
    else in YTOP reads ``XDG_CACHE_HOME``; yt-dlp's own cache dir is set
    explicitly per library.
    """
    path = deno_dir(library_root)
    os.environ["DENO_DIR"] = str(path)
    os.environ["XDG_CACHE_HOME"] = str(path.parent)
    return path


def base_options(config: Config, cache_root: Path | None = None) -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": config.retries,
        "extractor_retries": config.retries,
        "fragment_retries": config.retries,
        "sleep_interval_requests": config.request_interval,
        "socket_timeout": config.socket_timeout,
    }
    deno = deno_path()
    if deno is not None:
        opts["js_runtimes"] = {"deno": {"path": str(deno)}}
    if cache_root is not None:
        from ..layout import LibraryLayout  # layout imports ytop.yt

        # Keep yt-dlp's cache with the library, never in the host's ~/.cache.
        opts["cachedir"] = str(LibraryLayout(Path(cache_root)).cache_dir / "yt-dlp")
        # yt-dlp's Deno subprocess inherits the environment; the first
        # library used keeps Deno's cache too (portable serve pins the
        # launch library's at startup, see ``use_library_deno_dir``).
        os.environ.setdefault("DENO_DIR", str(deno_dir(cache_root)))
        os.environ.setdefault("XDG_CACHE_HOME", str(deno_dir(cache_root).parent))
    apply_auth_options(opts, config)
    return opts


def normalize_playlist_url(value: str) -> str:
    """Accept a playlist id, a playlist URL, or a watch URL carrying ``list=``."""
    value = value.strip()
    if not value:
        raise ProbeError("empty playlist id/URL")

    if value.startswith(("http://", "https://")):
        parsed = urlparse(value)
        list_ids = parse_qs(parsed.query).get("list")
        if list_ids and list_ids[0]:
            return f"https://www.youtube.com/playlist?list={list_ids[0]}"
        if parsed.path.rstrip("/") == "/playlist":
            return value
        raise ProbeError("that URL has no playlist id; pass a playlist URL or list id")

    if not re.fullmatch(r"[A-Za-z0-9_-]{10,}", value):
        raise ProbeError(f"not a playlist id or URL: {value!r}")
    return f"https://www.youtube.com/playlist?list={value}"


def _extract(url: str, opts: dict) -> dict:
    runner = ytdlp_runner.active()
    try:
        if runner is None:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        else:  # a retry with other components, in a child Python
            info = runner.extract(url, opts)
    except (DownloadError, ExtractorError) as exc:
        raise ProbeError(str(exc)) from exc
    if not info:
        raise ProbeError(f"no metadata returned for {url}")
    return info


def extract_playlist_info(
    value: str, config: Config, limit: int | None = None,
    cache_root: Path | None = None,
) -> tuple[str, dict]:
    url = normalize_playlist_url(value)
    opts = base_options(config, cache_root) | {"extract_flat": "in_playlist", "skip_download": True}
    info = _extract(url, opts)

    entries = info.get("entries")
    if not entries:
        raise ProbeError("not a playlist (no entries); pass a playlist URL or list id")

    if limit is not None:
        info = dict(info)
        info["entries"] = list(entries)[:limit]
    return url, info


def extract_video_info(
    video_id: str, config: Config, cache_root: Path | None = None
) -> dict:
    url = f"https://www.youtube.com/watch?v={video_id}"
    info = _extract(url, base_options(config, cache_root) | {"skip_download": True})
    if info.get("_type") == "playlist":
        nested = (info.get("entries") or [None])[0]
        if not nested:
            raise ProbeError(f"video {video_id} resolved to an empty playlist")
        info = nested
    return info


def probe_video(
    video_id: str,
    config: Config,
    *,
    playlist: dict | None = None,
    index: int = 1,
    on_item=None,
    on_item_start=None,
    max_height: int | None = None,
    preserve_audio: bool = False,
    cancel=None,
    cache_root: Path | None = None,
) -> ProbeResult:
    """Probe one video, optionally as an entry of an existing playlist.

    The returned ``ProbeResult`` carries the destination playlist document (or
    a minimal ``Un-grouped`` one) so ``download_probe`` writes the video into
    the right folder.
    """
    if cancel is not None and cancel():
        raise JobCancelled("cancelled by the user")
    target = dict(playlist or {
        "id": "un-grouped",
        "title": "Un-grouped",
        "channel": "",
        "description": "",
    })
    item = ProbedItem(
        index=index,
        id=video_id,
        url=f"https://www.youtube.com/watch?v={video_id}",
    )
    if cancel is not None and cancel():
        raise JobCancelled("cancelled by the user")
    if on_item_start is not None:
        on_item_start(item)
    try:
        if cancel is not None and cancel():
            raise JobCancelled("cancelled by the user")
        info = extract_video_info(video_id, config, cache_root)
        item.info = info
        item.title = str(info.get("title") or video_id)
        item.duration = info.get("duration")
        item.selection = select_format(
            info, config.audio_language, max_height, preserve_audio
        )
    except JobCancelled:
        raise
    except (ProbeError, FormatError) as exc:
        item.error = str(exc)
    except Exception as exc:  # noqa: BLE001 - the job page shows the reason
        item.error = f"{type(exc).__name__}: {exc}"
    if on_item is not None:
        on_item(item)
    return ProbeResult(
        url=item.url,
        playlist=target,
        items=[item],
    )


def probe_playlist(
    value: str,
    config: Config,
    limit: int | None = None,
    on_item=None,
    on_total=None,
    on_item_start=None,
    max_height: int | None = None,
    target_mb_per_hour: float | None = None,
    min_height: int = 240,
    probe_jobs: int | None = None,
    cancel=None,
    preserve_audio: bool = False,
    cache_root: Path | None = None,
    have: Callable[[str, str], bool] | None = None,
) -> ProbeResult:
    """Read the playlist, then extract each video's formats.

    ``have(playlist_id, video_id)`` names videos already in the library with
    their media: they are not extracted (a re-sync then costs only the new
    videos) and come back with ``existing`` set."""
    clock = PhaseClock(unit="videos")
    clock.start("listing")
    url, playlist = extract_playlist_info(value, config, limit, cache_root)
    entries = list(playlist.get("entries") or [])
    if limit is not None:
        entries = entries[:limit]
    clock.stop()
    clock.phases[-1].count = len(entries)
    if on_total is not None:
        on_total(len(entries))
    if cancel is not None and cancel():
        raise JobCancelled("cancelled by the user")

    result = ProbeResult(url=url, playlist=playlist)
    workers = max(1, probe_jobs if probe_jobs is not None else config.probe_jobs)
    playlist_id = str(playlist.get("id") or "")

    def probe_one(index: int, entry: dict) -> ProbedItem | None:
        if cancel is not None and cancel():
            raise JobCancelled("cancelled by the user")
        if not entry or not entry.get("id"):
            return None
        item = ProbedItem(
            index=index,
            id=str(entry["id"]),
            url=f"https://www.youtube.com/watch?v={entry['id']}",
            title=str(entry.get("title") or ""),
            duration=entry.get("duration"),
        )
        if on_item_start is not None:
            on_item_start(item)  # every item, so started - done stays in flight
        if have is not None and playlist_id and have(playlist_id, item.id):
            item.existing = True
            return item
        started = time.monotonic()
        try:
            info = extract_video_info(item.id, config, cache_root)
            item.info = info
            item.title = str(info.get("title") or item.title)
            item.duration = info.get("duration") or item.duration
            # Provisional selection; the playlist-wide budget is allocated
            # after every item has been probed.
            item.selection = select_format(
                info, config.audio_language, max_height, preserve_audio
            )
        except JobCancelled:
            raise
        except (ProbeError, FormatError) as exc:
            item.error = str(exc)
        except Exception as exc:  # noqa: BLE001 - record and continue the playlist
            item.error = f"{type(exc).__name__}: {exc}"
        item.seconds = time.monotonic() - started
        return item

    indexed = [
        (index, entry)
        for index, entry in enumerate(entries, start=1)
    ]
    clock.start("videos")
    if workers == 1:
        for index, entry in indexed:
            item = probe_one(index, entry)
            if item is None:
                continue
            result.items.append(item)
            if on_item is not None:
                on_item(item)
    else:
        pool = ThreadPoolExecutor(max_workers=workers)
        futures = {
            ytdlp_runner.spawn(pool, probe_one, index, entry): index
            for index, entry in indexed
        }
        try:
            for future in as_completed(futures):
                try:
                    item = future.result()
                except JobCancelled:
                    # Queued fetches never start.  In-flight fetches are joined
                    # below; the socket timeout is their outer bound, so the
                    # job only becomes terminal once no worker can still call
                    # back or write.
                    for pending in futures:
                        pending.cancel()
                    raise
                if item is None:
                    continue
                result.items.append(item)
                if on_item is not None:
                    on_item(item)
        finally:
            # Joining is the completion barrier: when this returns, no item
            # worker can still invoke a callback.
            pool.shutdown(wait=True, cancel_futures=True)
    result.items.sort(key=lambda item: item.index)
    clock.stop()
    clock.phases[-1].count = len(result.items)
    clock.phases[-1].note = _per_video_note(result.items, workers)
    if target_mb_per_hour and target_mb_per_hour > 0:
        clock.start("allocating")
        allocate_efficient(
            result.items,
            target_mb_per_hour=target_mb_per_hour,
            audio_language=config.audio_language,
            min_height=min_height,
            max_height=max_height or 0,
            preserve_audio=preserve_audio,
        )
        clock.stop()
    result.timings = clock.snapshot()
    return result


def _per_video_note(items: list[ProbedItem], workers: int) -> str:
    """``each 3.2 s mean, 5.1 s p90, 9.0 s max; 4 at a time; 2 failed;
    40 already in the library``."""
    kept = sum(1 for item in items if item.existing)
    seconds = [item.seconds for item in items if not item.existing]
    if not seconds:
        return f"{kept} already in the library" if kept else ""
    note = (
        f"each {human_seconds(sum(seconds) / len(seconds))} mean, "
        f"{human_seconds(percentile(seconds, 0.9))} p90, "
        f"{human_seconds(max(seconds))} max; {workers} at a time"
    )
    failed = sum(1 for item in items if item.error)
    return (
        note + (f"; {failed} failed" if failed else "")
        + (f"; {kept} already in the library" if kept else "")
    )
