"""Playlist enumeration and per-video format probing."""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, ExtractorError

from ..config import Config
from ..timing import Phase, PhaseClock, human_seconds, percentile
from ..toolchain import deno_path, js_runtime
from . import runner as ytdlp_runner
from .formats import (
    FormatError,
    Selection,
    allocate_efficient,
    select_format,
)
from .pot import apply_auth_options
from .proxies import (
    REGION_BLOCKED,
    active_proxy,
    countries_of,
    is_region_blocked,
    order_proxies,
    proxy_url,
    redact,
    region_blocked_message,
    using_proxy,
)
from .signin import (
    AGE_GATED,
    AGE_RESTRICTED_NEEDS_SIGNIN,
    is_age_gated,
    using_signin,
)

log = logging.getLogger(__name__)


class ProbeError(RuntimeError):
    """The playlist or video could not be read.  ``countries``: where YouTube
    offers a region-blocked video, when it said."""

    def __init__(self, message: str = "", countries: list[str] | None = None) -> None:
        super().__init__(message)
        self.countries = countries


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


_BOT_CHECK = dict(YOUTUBE_CHANGED)["bot-check"]
BOT_CHECK_STOPPED = (
    "not read: YouTube asked to confirm this computer is not a bot, so "
    "YTOP stopped asking. Try again later, or sign in for this session."
)


def is_bot_check(text: str | None) -> bool:
    """YouTube's "Sign in to confirm you're not a bot" (not the age gate)."""
    return bool(text) and bool(_BOT_CHECK.search(text or ""))


def classify_failure(exc: BaseException | str | None) -> str:
    """``"region_blocked"`` for a video YouTube does not offer in this
    country, ``"age_restricted"`` for an age-gated one, ``"youtube_changed"``
    when yt-dlp's error looks like YouTube changed under it (see
    ``YOUTUBE_CHANGED``), else ``"other"``."""
    texts: list[str] = []
    seen: set[int] = set()
    current: object = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        texts.append(str(current))
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
    text = "\n".join(texts)
    if is_region_blocked(exc):
        return "region_blocked"
    if is_age_gated(exc):  # walks the same cause chain as ``text``
        return "age_restricted"
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
    # A region-blocked video read through a proxy: the proxy's full URL,
    # login included.  Kept for the download; never rendered.
    proxy: str | None = None
    # Where YouTube offers a region-blocked video, when it said.
    countries: list[str] | None = None
    # ``"region_blocked"`` or ``"age_restricted"`` when ``error`` is that;
    # else None.
    error_kind: str | None = None
    # An age-gated video read signed in with ``Config.age_gate_browser``:
    # the download must sign in the same way.
    signed_in: bool = False


@dataclass
class ProbeResult:
    url: str
    playlist: dict
    items: list[ProbedItem] = field(default_factory=list)
    # Wall-clock per step (listing, videos, allocating); see ytop.timing.
    timings: list[Phase] = field(default_factory=list)
    # YouTube started bot-checking: the videos after that were not read.
    bot_blocked: bool = False

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
    proxy = active_proxy()
    if proxy is not None:
        opts["proxy"] = proxy
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
        raise ProbeError(str(exc), countries_of(exc)) from exc
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


# Analysis needs each video's format list, not playable stream URLs, so it
# can skip YouTube's player JavaScript: no Deno run per video to solve the
# signature/"n" challenges (measured: ~6 s -> ~3.5 s a video, same picks).
# Only the legacy muxed 360p mp4 (format 18) goes missing.  Downloads always
# read the video fully again.
QUICK_EXTRACTOR_ARGS = {"youtube": {"player_skip": ["js"]}}


def extract_video_info(
    video_id: str, config: Config, cache_root: Path | None = None,
    *, quick: bool = False,
) -> dict:
    """``quick``: formats only, without solving YouTube's JS challenges (the
    stream URLs it returns are not meant for downloading)."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    opts = base_options(config, cache_root) | {"skip_download": True}
    if quick:
        args = dict(opts.get("extractor_args") or {})
        for key, value in QUICK_EXTRACTOR_ARGS.items():
            args[key] = {**args.get(key, {}), **value}
        opts["extractor_args"] = args
    info = _extract(url, opts)
    if info.get("_type") == "playlist":
        nested = (info.get("entries") or [None])[0]
        if not nested:
            raise ProbeError(f"video {video_id} resolved to an empty playlist")
        info = nested
    return info


def analyse_video(
    video_id: str,
    config: Config,
    cache_root: Path | None = None,
    *,
    max_height: int | None = None,
    preserve_audio: bool = False,
) -> tuple[dict, Selection]:
    """Read one video and choose its format.  With ``config.quick_analysis``
    the quick read is tried first; a video it leaves without a usable format
    is read fully."""
    if config.quick_analysis:
        try:
            info = extract_video_info(video_id, config, cache_root, quick=True)
            return info, select_format(
                info, config.audio_language, max_height, preserve_audio
            )
        except FormatError:
            pass
    info = extract_video_info(video_id, config, cache_root)
    return info, select_format(info, config.audio_language, max_height, preserve_audio)


def _proxy_failure_reason(redacted: str) -> str:
    """A short, password-free reason a proxy could not read the video."""
    low = redacted.lower()
    if "407" in low or "proxy authentication" in low:
        return "proxy login refused"
    if is_bot_check(redacted):
        return "proxy was asked to confirm it is not a bot"
    if any(w in low for w in ("connection refused", "timed out", "unreachable",
                              "name resolution", "name or service not known")):
        return "proxy unreachable"
    return redacted[:80]


# yt-dlp could not load the age-gate profile's cookies (it fails before any
# request is made).
_COOKIES_UNREADABLE = re.compile(
    r"could not find .* cookies database|failed to decrypt|keyring|secretstorage"
    r"|permission denied.*cookie|cookies\.sqlite|cookies database",
    re.IGNORECASE,
)
# File paths in an error (a browser profile directory, say): quoted (may
# hold spaces), Windows ``X:\...``, or POSIX absolute (not a URL's path).
_PATHS = re.compile(
    r"""(['"])(?:[A-Za-z]:[\\/]|[/\\~])[^'"]*\1"""
    # Unquoted: words after a space that carry on the path ("Secret Profile/x.db")
    # are part of it.
    r"""|\b[A-Za-z]:\\[^\s'"]*(?:(?: [^\s'"\\]+)+\\[^\s'"]*)*"""
    r"""|(?<![\w:/.~-])~?/[^\s'"]+(?:(?: [^\s'"/]+)+/[^\s'"]*)*"""
)
# A signed-in read gets only images or no usable format without a JS runtime
# (no usable format can have other causes too, hence "may need").
_NEEDS_DENO = re.compile(
    r"only images are available|n challenge solving failed|requested format is not available",
    re.IGNORECASE,
)
_SIGN_IN_TO_CONFIRM = re.compile(r"sign in to confirm", re.IGNORECASE)
AGE_RESTRICTED_ALSO_REGION = (
    AGE_RESTRICTED_NEEDS_SIGNIN + " (also region-blocked; a proxy cleared that)"
)


def _signin_failure_reason(text: str, browser: str) -> str:
    """A short reason the signed-in retry failed.  Names only the browser,
    never the profile, and never reads as a bot check."""
    name, _, profile = browser.partition(":")
    if _COOKIES_UNREADABLE.search(text):
        return f"the YTOP sign-in ({name}) could not be read on this computer"
    if is_bot_check(text):
        return "YouTube asked to confirm it is not a bot"
    if _NEEDS_DENO.search(text):
        if js_runtime() is None:
            return "signed-in reads need Deno, which this computer doesn't have"
        return "signed-in reads may need a JavaScript runtime (Deno)"
    text = _PATHS.sub("<path>", redact(text))
    # A bare profile name; a short one ("1") would mask unrelated text.
    if len(profile) >= 4:
        text = text.replace(profile, "***")
    return " ".join(_SIGN_IN_TO_CONFIRM.sub("", text).split())[:80]


def _analyse_signed_in(
    video_id: str,
    config: Config,
    cache_root: Path | None,
    *,
    max_height: int | None,
    preserve_audio: bool,
    countries: list[str] | None,
    cause: BaseException,
) -> tuple[dict, Selection]:
    """One read signed in with ``config.age_gate_browser`` (through the
    context's proxy, if any).  A failure raises an ``"Age-restricted"``
    ``ProbeError`` chained to ``cause``, not to the failure itself, whose
    text may carry the profile path."""
    browser = config.age_gate_browser
    try:
        with using_signin(browser):
            return analyse_video(
                # Signed-in reads need the JS challenge solved, which the
                # quick analysis skips.
                video_id, replace(config, retries=1, quick_analysis=False), cache_root,
                max_height=max_height, preserve_audio=preserve_audio,
            )
    except JobCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 - reported as the item's reason
        reason = _signin_failure_reason(str(exc), browser)
        log.warning("signed-in retry failed for %s: %s", video_id, reason)
        failure = ProbeError(f"Age-restricted — signed-in retry failed: {reason}", countries)
    raise failure from cause


def analyse_with_fallback(
    video_id: str,
    config: Config,
    cache_root: Path | None = None,
    *,
    max_height: int | None = None,
    preserve_audio: bool = False,
) -> tuple[dict, Selection, str | None, bool]:
    """``analyse_video`` direct; when YouTube says the video is region-blocked,
    again through each configured proxy that fits, in order; when it is
    age-gated (directly or through the proxy that cleared its region block),
    once more signed in with ``config.age_gate_browser``.  Returns the proxy
    URL that worked (None: direct) and whether the read was signed in.  Any
    other direct failure is raised unchanged; a video no proxy opens raises
    a region-blocked ``ProbeError`` carrying the allowed countries; an
    age-gated one that could not be read raises a ``ProbeError`` starting
    ``"Age-restricted"``."""
    try:
        info, selection = analyse_video(
            video_id, config, cache_root,
            max_height=max_height, preserve_audio=preserve_audio,
        )
        return info, selection, None, False
    except (ProbeError, FormatError) as direct:
        if not is_region_blocked(direct):
            if not is_age_gated(direct):
                raise
            if not config.age_gate_browser:
                raise ProbeError(AGE_RESTRICTED_NEEDS_SIGNIN) from direct
            info, selection = _analyse_signed_in(
                video_id, config, cache_root,
                max_height=max_height, preserve_audio=preserve_audio,
                countries=None, cause=direct,
            )
            return info, selection, None, True
        countries = countries_of(direct)
        servers = order_proxies(config.proxies, countries)
        tried = len(servers) if config.proxies else -1
        # A dead proxy should cost one socket timeout, not ``retries`` of them.
        proxied = replace(config, retries=1)
        last: str | None = None
        for server in servers:
            url = proxy_url(server, config)
            with using_proxy(url):
                try:
                    info, selection = analyse_video(
                        video_id, proxied, cache_root,
                        max_height=max_height, preserve_audio=preserve_audio,
                    )
                    return info, selection, url, False
                except JobCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001 - this proxy failed; try the next
                    text = redact(str(exc))
                    # Its own text only: the cause chain reaches the direct failure.
                    age_gated = bool(AGE_GATED.search(text)) and not REGION_BLOCKED.search(text)
                    if not age_gated:
                        log.warning("proxy failed for %s: %s", video_id, text)
                        last = None if REGION_BLOCKED.search(text) else _proxy_failure_reason(text)
                        continue
                if not config.age_gate_browser:
                    raise ProbeError(AGE_RESTRICTED_ALSO_REGION, countries) from direct
                # This proxy cleared the region block: sign in through it.
                info, selection = _analyse_signed_in(
                    video_id, config, cache_root,
                    max_height=max_height, preserve_audio=preserve_audio,
                    countries=countries, cause=direct,
                )
                return info, selection, url, True
        raise ProbeError(region_blocked_message(countries, tried, last), countries) from direct


def record_failure(item: ProbedItem, exc: BaseException) -> None:
    item.error = redact(str(exc))
    # Before the region check: an age-gated video read through a proxy
    # carries the region-blocked direct failure as its cause.
    if item.error.startswith("Age-restricted"):
        item.error_kind = "age_restricted"
        item.countries = countries_of(exc)
    elif is_region_blocked(exc):
        item.error_kind = "region_blocked"
        item.countries = countries_of(exc)


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
        info, item.selection, item.proxy, item.signed_in = analyse_with_fallback(
            video_id, config, cache_root,
            max_height=max_height, preserve_audio=preserve_audio,
        )
        item.info = info
        item.title = str(info.get("title") or video_id)
        item.duration = info.get("duration")
    except JobCancelled:
        raise
    except (ProbeError, FormatError) as exc:
        record_failure(item, exc)
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
    # Every request after YouTube starts bot-checking only deepens the
    # block: stop reading once it does.
    blocked = threading.Event()

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
        if blocked.is_set():
            item.error = BOT_CHECK_STOPPED
            return item
        started = time.monotonic()
        try:
            # Provisional selection; the playlist-wide budget is allocated
            # after every item has been probed.
            info, item.selection, item.proxy, item.signed_in = analyse_with_fallback(
                item.id, config, cache_root,
                max_height=max_height, preserve_audio=preserve_audio,
            )
            item.info = info
            item.title = str(info.get("title") or item.title)
            item.duration = info.get("duration") or item.duration
        except JobCancelled:
            raise
        except (ProbeError, FormatError) as exc:
            record_failure(item, exc)
        except Exception as exc:  # noqa: BLE001 - record and continue the playlist
            item.error = f"{type(exc).__name__}: {exc}"
        item.seconds = time.monotonic() - started
        if is_bot_check(item.error):
            blocked.set()
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
    result.bot_blocked = blocked.is_set()
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
