"""Resumable, idempotent playlist downloads into a portable library.

yt-dlp writes into ``.ytop/cache/partial/<video-id>/``, so an interrupted
download resumes from its ``.part`` file.  A finished file is published under
its readable name in ``YTOP Library/<folder>/`` and the partial dir removed.
"""

from __future__ import annotations

import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from ..config import Config
from ..fsinfo import fits_on
from ..index import refresh_video_quietly
from ..layout import LibraryLayout, read_library
from ..publish import PUBLISH_LOCK, playlist_folder, publish_download
from ..toolchain import ffmpeg_path
from ..transcripts.fetch import (
    TranscriptResult,
    fetch_video_transcript,
    write_playlist_summary,
)
from . import runner as ytdlp_runner
from .formats import (
    allocate_efficient,
    audio_family,
    audio_is_auto_dubbed,
    audio_is_default,
    audio_is_original,
    audio_label,
    select_format,
)
from .metadata import (
    MEDIA_EXTENSIONS,
    PLAYLIST_THUMB_NAME,
    THUMB_NAME,
    fetch_thumbnail,
    find_media,
    human_bytes,
    normalize_playlist,
    normalize_video,
    playlist_directory,
    read_json,
    sanitize_component,
    video_directory,
    write_json,
)
from .probe import (
    JobCancelled,
    ProbedItem,
    ProbeResult,
    analyse_with_fallback,
    base_options,
    classify_failure,
    extract_video_info,
    probe_playlist,
    record_failure,
)
from .proxies import covering_countries, redact, using_proxy
from .rawinfo import trim_raw_info
from .signin import using_signin


@dataclass
class ItemResult:
    index: int
    id: str
    title: str
    status: str  # downloaded | skipped | failed
    path: str | None = None
    size: int = 0
    error: str | None = None
    transcript: TranscriptResult | None = None
    error_kind: str | None = None  # e.g. "fat32_limit", "region_blocked"
    countries: list[str] | None = None  # where a region-blocked video is offered


def _failure_kind(exc: BaseException, item: ProbedItem) -> str | None:
    """``"region_blocked"``, ``"age_restricted"`` or ``"youtube_changed"`` for
    a failed download.  A video read through a proxy or the age-gate sign-in is
    never ``"youtube_changed"``: a flaky proxy or session must not trigger a
    component update."""
    kind = classify_failure(exc)
    if kind in ("region_blocked", "age_restricted"):
        return kind
    if kind == "youtube_changed" and not item.proxy and not item.signed_in:
        return kind
    return None


def _ytdlp_download(url: str, opts: dict) -> None:
    """yt-dlp downloads ``url``: here, or in the retry's child Python."""
    runner = ytdlp_runner.active()
    if runner is not None:
        runner.download(url, opts)
        return
    with YoutubeDL(opts) as ydl:
        ydl.download([url])


class FatLimitError(DownloadError):
    """The selected rendition cannot be stored on a FAT32 destination."""


def media_destination(layout: LibraryLayout, playlist_id: str) -> Path:
    """The media folder a playlist's downloads land in, or the media root
    while that folder does not exist yet (used for filesystem checks)."""
    doc = read_json(layout.playlist_meta_dir(playlist_id) / "playlist.json") or {}
    folder = doc.get("folder")
    if folder:
        path = layout.media_root / str(folder)
        if path.is_dir():
            return path
    return layout.media_root


def fat32_message(layout: LibraryLayout, estimated_bytes: int) -> str:
    info = read_library(layout.root)
    label = (info.label if info else "") or layout.root.name
    return (
        f"This video (~{human_bytes(estimated_bytes)}) is larger than FAT32's "
        f"4 GB file limit on {label}. Choose a smaller quality or an "
        "exFAT/other drive."
    )


@dataclass
class FetchResult:
    playlist_dir: Path
    playlist: dict
    items: list[ItemResult] = field(default_factory=list)
    total_bytes: int = 0

    @property
    def downloaded(self) -> list[ItemResult]:
        return [i for i in self.items if i.status == "downloaded"]

    @property
    def skipped(self) -> list[ItemResult]:
        return [i for i in self.items if i.status == "skipped"]

    @property
    def failed(self) -> list[ItemResult]:
        return [i for i in self.items if i.status == "failed"]


BytesCallback = Callable[[int, int], None]
ItemCallback = Callable[[ItemResult], None]
StartCallback = Callable[[ProbedItem], None]


def _progress_hook(
    label: str,
    lock: threading.Lock,
    *,
    quiet: bool,
    callback: BytesCallback | None = None,
    cancel: Callable[[], bool] | None = None,
):
    seen: set[int] = set()

    def hook(data: dict) -> None:
        if cancel is not None and cancel():
            raise JobCancelled("cancelled by the user")
        if data.get("status") != "downloading":
            return
        total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
        done = data.get("downloaded_bytes") or 0
        if not total:
            return
        if callback is not None:
            callback(int(done), int(total))
        if quiet:
            return
        percent = int(done * 100 / total)
        bucket = percent // 25 * 25
        if bucket in seen or bucket >= 100:
            return
        seen.add(bucket)
        with lock:
            print(f"    {label} {bucket:>3d}%", flush=True)

    return hook


def _staged_media(partial: Path) -> Path | None:
    """The merged ``video.<ext>`` yt-dlp left in a partial dir."""
    if not partial.is_dir():
        return None
    files = [
        p for p in partial.iterdir()
        if p.stem == "video"
        and p.suffix.lower() in MEDIA_EXTENSIONS
        and p.is_file()
        and p.stat().st_size > 0
    ]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def _download_media(
    item: ProbedItem,
    layout: LibraryLayout,
    playlist_id: str,
    config: Config,
    force: bool,
    lock: threading.Lock,
    *,
    quiet: bool = False,
    progress: BytesCallback | None = None,
    cancel: Callable[[], bool] | None = None,
    preserve_audio: bool = False,
    max_height: int | None = None,
) -> tuple[Path, str]:
    video_dir = video_directory(layout, playlist_id, item.id)
    if not force:
        existing = find_media(layout, video_dir)
        if existing is not None:
            # A partial left by an earlier interrupted attempt is now moot.
            shutil.rmtree(layout.partial_dir(item.id), ignore_errors=True)
            return existing, "skipped"

    assert item.selection is not None
    # A forced re-download keeps the old file until the new one is published;
    # publish_download then replaces it.
    partial = layout.partial_dir(item.id)
    partial.mkdir(parents=True, exist_ok=True)

    label = f"{item.index:02d} {item.id}"
    last_error: Exception | None = None
    for attempt in range(2):
        if cancel is not None and cancel():
            raise JobCancelled("cancelled by the user")
        if attempt:
            # Formats can disappear between probing and downloading.  Re-probe
            # once, reselect, and resume; never discard a partial file.
            try:
                # Never above the height chosen for this run (cap or budget).
                cap = item.selection.video.get("height") or max_height
                info = extract_video_info(item.id, config, layout.root)
                item.info = info
                item.selection = select_format(
                    info, config.audio_language, cap, preserve_audio=preserve_audio
                )
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                break
            time.sleep(2)

        opts = base_options(config, layout.root) | {
            "format": item.selection.format_spec,
            "outtmpl": {"default": str(partial / "video.%(ext)s")},
            "ffmpeg_location": str(ffmpeg_path().parent),
            "continuedl": True,
            "overwrites": force,
            "concurrent_fragment_downloads": 1,
            "progress_hooks": [
                _progress_hook(
                    label, lock, quiet=quiet, callback=progress,
                    cancel=cancel,
                )
            ],
        }
        if item.selection.container:
            opts["merge_output_format"] = item.selection.container

        try:
            _ytdlp_download(item.url, opts)
            last_error = None
            break
        except DownloadError as exc:
            if cancel is not None and cancel():
                raise JobCancelled("cancelled by the user") from exc
            last_error = exc
            if attempt == 0:
                time.sleep(3)

    if last_error is not None:
        raise RuntimeError(str(last_error)) from last_error

    staged = _staged_media(partial)
    if staged is None:
        raise RuntimeError("yt-dlp reported success but produced no media file")
    media = publish_download(layout, playlist_id, item.id, staged)
    shutil.rmtree(partial, ignore_errors=True)
    return media, "downloaded"


def _kbps(fmt: dict | None) -> int | None:
    try:
        abr = float((fmt or {}).get("abr") or 0)
    except (TypeError, ValueError):
        return None
    return int(round(abr)) if abr else None


def _existing_audio_file(audio_dir: Path, format_id: str) -> Path | None:
    if not audio_dir.is_dir():
        return None
    for path in audio_dir.glob(f"{format_id}.*"):
        if path.is_file() and path.suffix != ".part" and path.stat().st_size > 0:
            return path
    return None


def _download_alternate_audio(
    item: ProbedItem,
    video_dir: Path,
    config: Config,
    force: bool,
    lock: threading.Lock,
    *,
    quiet: bool = False,
    cancel: Callable[[], bool] | None = None,
    cache_root: Path | None = None,
) -> list[tuple[dict, Path]]:
    """Download the extra audio tracks selected for this video.

    The primary audio is merged into the video file.  Alternates are kept as
    standalone files so the browser can switch tracks without a transcode.
    A failure here must not lose the video that already downloaded.
    """
    audio_dir = Path(video_dir) / "audio"
    if force and audio_dir.is_dir():
        # A forced re-download replaces the tracks; drop ones no longer selected.
        keep = {
            str(fmt.get("format_id") or "")
            for fmt in (item.selection.alternates if item.selection else [])
        }
        for path in audio_dir.iterdir():
            if path.is_file() and path.name.partition(".")[0] not in keep:
                path.unlink(missing_ok=True)
    if item.selection is None or not item.selection.alternates:
        return []
    audio_dir.mkdir(parents=True, exist_ok=True)
    saved: list[tuple[dict, Path]] = []
    for fmt in item.selection.alternates:
        format_id = str(fmt.get("format_id") or "")
        if not format_id:
            continue
        existing = _existing_audio_file(audio_dir, format_id)
        if existing is not None and not force:
            saved.append((fmt, existing))
            continue
        try:
            if existing is not None:
                existing.unlink(missing_ok=True)
            opts = base_options(config, cache_root) | {
                "format": format_id,
                "outtmpl": str(audio_dir / f"{format_id}.%(ext)s"),
                "ffmpeg_location": str(ffmpeg_path().parent),
                "continuedl": True,
                "overwrites": True,
                "concurrent_fragment_downloads": 1,
                "progress_hooks": [
                    _progress_hook(
                        f"audio {format_id}", lock, quiet=True, cancel=cancel
                    )
                ],
            }
            _ytdlp_download(item.url, opts)
            path = _existing_audio_file(audio_dir, format_id)
            if path is None:
                raise RuntimeError(
                    f"audio track {format_id} downloaded no file"
                )
            saved.append((fmt, path))
            if not quiet:
                with lock:
                    print(
                        f"    audio {audio_label(fmt)}: "
                        f"{human_bytes(path.stat().st_size)}",
                        flush=True,
                    )
        except JobCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - keep the video
            if not quiet:
                with lock:
                    print(
                        f"    audio {format_id} failed: {exc}", flush=True
                    )
    return saved


def _audio_track_rows(
    item: ProbedItem,
    video_dir: Path,
    alternates: list[tuple[dict, Path]],
) -> list[dict]:
    rows: list[dict] = []
    selection = item.selection
    if selection is not None and selection.audio is not None:
        primary = selection.audio
        rows.append({
            "id": str(primary.get("format_id") or "primary"),
            "label": audio_label(primary),
            "language": primary.get("language"),
            "codec": audio_family(primary),
            "bitrate": _kbps(primary),
            "channels": primary.get("audio_channels"),
            "embedded": True,
            "file": None,
            "original": audio_is_original(primary),
            "auto_dubbed": audio_is_auto_dubbed(primary),
            "default": audio_is_default(primary),
            "size": 0,
        })
    for fmt, path in alternates:
        try:
            relative = path.relative_to(video_dir).as_posix()
            size = path.stat().st_size
        except (ValueError, OSError):
            continue
        rows.append({
            "id": str(fmt.get("format_id") or path.stem),
            "label": audio_label(fmt),
            "language": fmt.get("language"),
            "codec": audio_family(fmt),
            "bitrate": _kbps(fmt),
            "channels": fmt.get("audio_channels"),
            "embedded": False,
            "file": relative,
            "original": audio_is_original(fmt),
            "auto_dubbed": audio_is_auto_dubbed(fmt),
            "default": audio_is_default(fmt),
            "size": size,
        })
    return rows


def _entry(
    item: ProbedItem,
    *,
    available: bool,
    error: str | None,
) -> dict:
    # Media location lives only in each video's meta.json (files.media).
    return {
        "id": item.id,
        "title": item.title,
        "duration": item.duration,
        "index": item.index,
        "available": available,
        "metadata_ok": bool(item.info),
        "error": error,
    }


def _keep_existing(
    item: ProbedItem,
    layout: LibraryLayout,
    playlist_id: str,
    vdir: Path,
    media: Path,
    lock: threading.Lock,
    *,
    quiet: bool = False,
) -> tuple[ItemResult, dict]:
    """A video already in the library, not re-read from YouTube: keep its
    stored details, taking only a changed title from the playlist listing."""
    listed = (item.title or "").strip()
    # The listing names unavailable videos "[Private video]", "[Deleted video]".
    real_title = bool(listed) and not (listed.startswith("[") and listed.endswith("]"))
    stored = read_json(vdir / "meta.json")
    if real_title and stored is not None and stored.get("title") != listed:
        with PUBLISH_LOCK:
            stored = read_json(vdir / "meta.json")
            if stored is not None:
                stored["title"] = listed
                write_json(vdir / "meta.json", stored)
        refresh_video_quietly(layout, playlist_id, item.id)
    title = (
        listed if real_title
        else str((stored or {}).get("title") or listed or item.id)
    )
    item.title = title
    if not quiet:
        with lock:
            print(f"  {item.index:02d} {title}: already downloaded", flush=True)
    entry = _entry(item, available=True, error=None)
    entry["metadata_ok"] = stored is not None
    return (
        ItemResult(item.index, item.id, title, "skipped", path=str(vdir),
                   size=media.stat().st_size),
        entry,
    )


def _process_item(
    item: ProbedItem,
    playlist: dict,
    layout: LibraryLayout,
    config: Config,
    force: bool,
    lock: threading.Lock,
    **kwargs,
) -> tuple[ItemResult, dict]:
    """Process one item; a video read through a proxy or the age-gate sign-in
    is fetched through it too, because YouTube's stream URLs are bound to the
    IP and session that read them.  Only ``config.age_gate_browser`` is ever
    used for the sign-in, never the session browser."""
    if item.existing:
        kept = _keep_or_reread_existing(item, playlist, layout, config, force, lock, **kwargs)
        if kept is not None:
            return kept
    browser = config.age_gate_browser if item.signed_in and config.age_gate_browser else None
    with using_proxy(item.proxy), using_signin(browser):
        return _process_item_body(item, playlist, layout, config, force, lock, **kwargs)


def _keep_or_reread_existing(
    item: ProbedItem,
    playlist: dict,
    layout: LibraryLayout,
    config: Config,
    force: bool,
    lock: threading.Lock,
    *,
    quiet: bool = False,
    cancel: Callable[[], bool] | None = None,
    preserve_audio: bool = False,
    refresh_existing: bool = False,
    max_height: int | None = None,
    **_ignored,
) -> tuple[ItemResult, dict] | None:
    """A video already downloaded: its kept result, or None after reading it
    now (a refresh, a forced download, or media gone since the review), as a
    full analysis would have: through the proxy or sign-in it needs, which
    ``item`` then records for the download.  A failed read is recorded on
    ``item`` the way the probe records one."""
    playlist_id = str(playlist.get("id") or "")
    vdir = video_directory(layout, playlist_id, item.id)
    media = None if force else find_media(layout, vdir)
    if media is not None and not refresh_existing:
        return _keep_existing(item, layout, playlist_id, vdir, media, lock, quiet=quiet)
    if cancel is not None and cancel():
        raise JobCancelled("cancelled by the user")
    try:
        item.info, item.selection, item.proxy, item.signed_in = analyse_with_fallback(
            item.id, config, layout.root,
            max_height=max_height, preserve_audio=preserve_audio,
        )
        item.title = str(item.info.get("title") or item.title)
        item.duration = item.info.get("duration") or item.duration
    except JobCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 - reported like a failed probe
        record_failure(item, exc)
    item.existing = False
    return None


def _process_item_body(
    item: ProbedItem,
    playlist: dict,
    layout: LibraryLayout,
    config: Config,
    force: bool,
    lock: threading.Lock,
    *,
    quiet: bool = False,
    progress: BytesCallback | None = None,
    cancel: Callable[[], bool] | None = None,
    fetch_transcripts: bool = True,
    preserve_audio: bool = False,
    refresh_existing: bool = False,
    upgrade: bool = False,
    max_height: int | None = None,
) -> tuple[ItemResult, dict]:
    title = item.title or item.id
    playlist_id = str(playlist.get("id") or "")
    vdir = video_directory(layout, playlist_id, item.id)
    if upgrade and not force and not item.error and item.selection is not None:
        media = find_media(layout, vdir)
        stored = _stored_height(vdir) if media is not None else None
        if media is not None:
            offered = item.selection.video.get("height") or 0
            if stored is not None and offered <= stored:
                return _keep_existing(item, layout, playlist_id, vdir, media, lock,
                                      quiet=quiet)
            # Taller than what is on disk: replace this video's media only.
            force = True
    if item.error or item.info is None or item.selection is None:
        media = find_media(layout, vdir)
        if not quiet:
            with lock:
                print(f"  {item.index:02d} {title}: SKIP "
                      f"({item.error or 'no format'})", flush=True)
        return (
            ItemResult(item.index, item.id, title, "failed",
                       path=str(vdir) if media else None,
                       size=media.stat().st_size if media else 0,
                       error=item.error or "no usable format",
                       error_kind=item.error_kind, countries=item.countries),
            _entry(item, available=media is not None,
                   error=item.error or "no usable format"),
        )

    # Refuse before writing anything: a FAT32 volume cannot hold a file of
    # 4 GB or more, and yt-dlp would fail only after hours of downloading.
    if force or find_media(layout, vdir) is None:
        estimate = item.selection.estimated_bytes
        if not fits_on(media_destination(layout, playlist_id), estimate):
            raise FatLimitError(fat32_message(layout, estimate))

    vdir.mkdir(parents=True, exist_ok=True)
    existing_meta = read_json(vdir / "meta.json") or {}

    # Keep the raw extractor snapshot before doing anything expensive,
    # without the stream URLs and caption bulk nothing reads back (#5).
    write_json(vdir / "raw.json", trim_raw_info(item.info))

    if not quiet:
        with lock:
            print(f"  {item.index:02d} {title}: {item.selection.summary()}",
                  flush=True)

    thumb_path = vdir / THUMB_NAME
    old_thumb_url = (existing_meta.get("files") or {}).get("thumbnail_url")
    if force or not thumb_path.exists() or old_thumb_url != item.info.get("thumbnail"):
        fetch_thumbnail(item.info, thumb_path)
    thumb_name = THUMB_NAME if thumb_path.exists() else None

    media: Path | None = None
    status = "skipped"
    error: str | None = None
    error_kind: str | None = None
    try:
        media, status = _download_media(
            item, layout, playlist_id, config, force, lock,
            quiet=quiet, progress=progress, cancel=cancel,
            preserve_audio=preserve_audio, max_height=max_height,
        )
    except JobCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 - keep metadata and other items
        error = redact(str(exc))
        error_kind = _failure_kind(exc, item)
        media = find_media(layout, vdir)

    alternates: list[tuple[dict, Path]] = []
    audio_mismatch = bool(
        status == "skipped"
        and (existing_meta.get("format") or {}).get("audio_id")
        and item.selection is not None
        and item.selection.audio is not None
        and str((existing_meta.get("format") or {}).get("audio_id"))
        != str(item.selection.audio.get("format_id"))
    )
    if media is not None and item.selection is not None and not audio_mismatch:
        try:
            alternates = _download_alternate_audio(
                item, vdir, config, force, lock,
                quiet=quiet, cancel=cancel,
                cache_root=layout.root,
            )
        except JobCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - alternate audio is optional
            if not quiet:
                with lock:
                    print(f"    alternate audio failed: {redact(str(exc))}",
                          flush=True)

    tracks = _audio_track_rows(item, vdir, alternates)
    if audio_mismatch:
        # The media on disk was produced by an older policy and contains a
        # different audio track.  Do not relabel it as the newly selected one;
        # a forced re-download will replace it.
        tracks = existing_meta.get("audio_tracks") or []
    elif not tracks and existing_meta.get("audio_tracks"):
        tracks = existing_meta["audio_tracks"]

    meta = normalize_video(
        item.info,
        index=item.index,
        playlist=playlist,
        video_dir=vdir,
        thumbnail_file=thumb_name,
        media_file=layout.relative_media(media) if media else None,
        selection=item.selection,
        audio_tracks=tracks,
        existing=existing_meta,
    )
    # On a sync we do not re-download existing media.  Keep the format that is
    # actually on disk even if YouTube's available renditions changed since.
    if status == "skipped" and existing_meta.get("format"):
        meta["format"] = existing_meta["format"]
    with PUBLISH_LOCK:
        # Another worker's publish may have renamed this file meanwhile; take
        # the current reference rather than the path seen above.
        current = find_media(layout, vdir) if media is not None else None
        if current is not None:
            media = current
        meta["files"]["media"] = layout.relative_media(media) if media else None
        write_json(vdir / "meta.json", meta)
    refresh_video_quietly(layout, playlist_id, item.id)

    transcript: TranscriptResult | None = None
    if fetch_transcripts and media is not None and not error:
        try:
            transcript = fetch_video_transcript(
                vdir,
                playlist_id=playlist_id,
                config=config,
                layout=layout,
                # A blocked/rate-limited transcript endpoint must not make a
                # 121-video download crawl.  The explicit `ytop transcripts`
                # command still uses the configured retry policy.
                retries=0,
                backoff_s=0.0,
            )
        except Exception as exc:  # noqa: BLE001 - media stays usable
            transcript = TranscriptResult(
                id=item.id, title=title, status="failed",
                error=redact(f"{type(exc).__name__}: {exc}"),
            )

    entry = _entry(item, available=media is not None, error=error)
    if error:
        if not quiet:
            with lock:
                print(f"  {item.index:02d} {title}: FAILED ({error})", flush=True)
        return (
            ItemResult(item.index, item.id, title, "failed", path=str(vdir),
                       size=media.stat().st_size if media else 0, error=error,
                       transcript=transcript, error_kind=error_kind),
            entry,
        )

    if not quiet:
        with lock:
            print(f"  {item.index:02d} {title}: {status}"
                  + (f" ({human_bytes(media.stat().st_size)})" if media else ""),
                  flush=True)
    return (
        ItemResult(item.index, item.id, title, status, path=str(vdir),
                   size=media.stat().st_size if media else 0,
                   transcript=transcript),
        entry,
    )


def download_probe(
    probe: ProbeResult,
    config: Config,
    *,
    limit: int | None = None,
    jobs: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    quiet: bool = False,
    max_height: int | None = None,
    target_mb_per_hour: float | None = None,
    min_height: int = 240,
    fetch_transcripts: bool = True,
    preserve_audio: bool | None = None,
    quality: str | None = None,
    refresh_existing: bool = False,
    upgrade: bool = False,
    library_root: Path | None = None,
    cancel: Callable[[], bool] | None = None,
    on_item_start: StartCallback | None = None,
    on_item_done: ItemCallback | None = None,
    on_progress: Callable[[ProbedItem, int, int], None] | None = None,
) -> FetchResult:
    """Download a previously probed playlist, reporting per-item progress.

    ``quality`` (a review choice such as ``"720p"``) is remembered on the
    playlist so the next sync's review starts from it; ``None`` keeps the
    stored choice.  Videos the probe found already downloaded (``existing``)
    keep their stored details unless ``refresh_existing`` re-reads them.
    """
    lock = threading.Lock()
    if library_root is None:
        raise ValueError("library_root is required")
    root = Path(library_root).expanduser()
    layout = LibraryLayout(root)
    pdir = playlist_directory(layout, probe.playlist)
    playlist_id = str(probe.playlist.get("id") or "")

    if not quiet:
        print()
        print(f"playlist: {probe.playlist.get('title') or probe.playlist.get('id')}")
        print(f"target:   {pdir}")
        print(f"videos:   {len(probe.items)}"
              + (f" (limited to {limit})" if limit is not None else ""))
        print(f"estimate: {human_bytes(probe.estimated_bytes)}"
              + (" (formats changed since last run?)" if probe.failures else ""))

    if dry_run:
        items = [
            ItemResult(i.index, i.id, i.title,
                       "failed" if i.error or not (i.selection or i.existing)
                       else "skipped",
                       error=i.error)
            for i in probe.items
        ]
        return FetchResult(pdir, probe.playlist, items, probe.estimated_bytes)

    pdir.mkdir(parents=True, exist_ok=True)
    existing_doc = read_json(pdir / "playlist.json") or {}
    playlist_options = dict(existing_doc.get("options") or {})
    if preserve_audio is None:
        preserve_audio = bool(playlist_options.get("preserve_audio"))
    playlist_options["preserve_audio"] = bool(preserve_audio)
    if quality is not None:
        playlist_options["quality"] = str(quality)

    # Apply the requested policy to the whole playlist before writing bytes.
    if target_mb_per_hour and target_mb_per_hour > 0:
        allocate_efficient(
            probe.items,
            target_mb_per_hour=target_mb_per_hour,
            audio_language=config.audio_language,
            min_height=min_height,
            max_height=max_height or 0,
            preserve_audio=bool(preserve_audio),
        )
    elif target_mb_per_hour is not None or max_height is not None:
        for item in probe.items:
            if item.info is None:
                continue
            try:
                item.selection = select_format(
                    item.info, config.audio_language, max_height,
                    bool(preserve_audio),
                )
                item.error = None
            except Exception as exc:  # noqa: BLE001
                item.error = str(exc)
                item.selection = None

    workers = max(1, jobs if jobs is not None else config.jobs)
    results: dict[int, tuple[ItemResult, dict]] = {}

    # Publish the playlist before the first byte is written.  A long download
    # then appears in the library immediately: finished entries are playable,
    # pending entries show as not-yet-available, and each entry flips to
    # available as its media lands.
    playlist_thumb = pdir / PLAYLIST_THUMB_NAME
    thumb_name = PLAYLIST_THUMB_NAME if playlist_thumb.exists() else None

    entries_by_id: dict[str, dict] = {}
    for row in existing_doc.get("entries") or []:
        if isinstance(row, dict) and row.get("id"):
            entries_by_id[str(row["id"])] = row

    # User order wins.  Entries already in the library stay exactly where they
    # are (including videos added by hand or reordered), and videos that are
    # new to this playlist are appended in the order the source returns them.
    # A sync therefore never drops or shuffles local entries.
    existing_order = [
        str(row.get("id"))
        for row in existing_doc.get("entries") or []
        if isinstance(row, dict) and row.get("id")
    ]
    order_ids = list(dict.fromkeys(
        existing_order + [item.id for item in probe.items]
    ))

    for item in probe.items:
        media = find_media(layout, video_directory(layout, playlist_id, item.id))
        row = dict(entries_by_id.get(item.id) or {})
        # Older entries carried the media location; it now lives in meta.json.
        row.pop("path", None)
        row.pop("video", None)
        row.update({
            "id": item.id,
            "title": item.title or row.get("title") or item.id,
            "duration": item.duration
            if item.duration is not None else row.get("duration"),
            "index": item.index,
            "available": media is not None,
            "metadata_ok": bool(item.info) or bool(row.get("metadata_ok")),
            "error": item.error,
        })
        entries_by_id[item.id] = row

    state_lock = threading.Lock()

    def ordered_entries() -> list[dict]:
        rows = [
            entries_by_id[video_id]
            for video_id in order_ids
            if video_id in entries_by_id
        ]
        for position, row in enumerate(rows, start=1):
            row["index"] = position
        return rows

    def current_folder() -> None:
        # The folder may be renamed while we download (rename_folder holds
        # PUBLISH_LOCK); take the one on disk, never our cached copy.
        on_disk = read_json(pdir / "playlist.json") or {}
        if on_disk.get("folder"):
            existing_doc["folder"] = on_disk["folder"]

    def write_partial() -> None:
        with PUBLISH_LOCK:
            current_folder()
            doc = normalize_playlist(
                probe.playlist,
                entries=ordered_entries(),
                thumbnail_file=thumb_name,
                existing=existing_doc,
                options=playlist_options,
            )
            if limit is not None:
                doc["partial"] = True
                doc["limit"] = limit
            write_json(pdir / "playlist.json", doc)

    if cancel is not None and cancel():
        raise JobCancelled("cancelled by the user")
    write_partial()
    # Fix the media folder name now, so later playlist.json rewrites keep it.
    published = read_json(pdir / "playlist.json") or {}
    playlist_folder(layout, published)
    existing_doc["folder"] = published["folder"]

    # Fetch the playlist artwork after publishing the entries, so a slow
    # thumbnail request can never delay the playlist appearing in the library.
    if force or not playlist_thumb.exists():
        thumb_source = probe.playlist
        if not thumb_source.get("thumbnails"):
            first_info = next((i.info for i in probe.items if i.info), None)
            if first_info:
                thumb_source = first_info
        fetch_thumbnail(thumb_source, playlist_thumb)
        if playlist_thumb.exists():
            thumb_name = PLAYLIST_THUMB_NAME
            write_partial()

    def run(item: ProbedItem) -> tuple[ItemResult, dict]:
        if cancel is not None and cancel():
            raise JobCancelled("cancelled by the user")
        if on_item_start is not None:
            on_item_start(item)
        callback = None
        if on_progress is not None:
            callback = lambda done, total: on_progress(item, done, total)
        try:
            result, entry = _process_item(
                item, probe.playlist, layout, config, force, lock,
                quiet=quiet, progress=callback, cancel=cancel,
                fetch_transcripts=fetch_transcripts,
                preserve_audio=bool(preserve_audio),
                refresh_existing=refresh_existing,
                upgrade=upgrade,
                max_height=max_height or None,
            )
        except JobCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad item must not stop the batch
            fat = isinstance(exc, FatLimitError)
            error = redact(str(exc) if fat else f"{type(exc).__name__}: {exc}")
            if not quiet:
                with lock:
                    print(f"  {item.index:02d} {item.title or item.id}: "
                          f"FAILED ({error})", flush=True)
            result = ItemResult(
                item.index, item.id, item.title or item.id, "failed",
                error=error,
                error_kind="fat32_limit" if fat else _failure_kind(exc, item),
            )
            media = find_media(
                layout, video_directory(layout, playlist_id, item.id)
            )
            entry = _entry(item, available=media is not None, error=error)
        # A sync may replace the media bytes; any cached scrubber frames for
        # the old file are now wrong.
        if result.status == "downloaded":
            shutil.rmtree(
                layout.cache_dir / "previews"
                / sanitize_component(playlist_id, 40)
                / sanitize_component(item.id, 40),
                ignore_errors=True,
            )
        with state_lock:
            entries_by_id[item.id] = entry
            write_partial()
        if on_item_done is not None:
            on_item_done(result)
        return result, entry

    cancelled = False
    if workers == 1:
        for item in probe.items:
            results[item.index] = run(item)
    else:
        pool = ThreadPoolExecutor(max_workers=workers)
        futures = {ytdlp_runner.spawn(pool, run, item): item for item in probe.items}
        try:
            for future in as_completed(futures):
                item = futures[future]
                try:
                    results[item.index] = future.result()
                except JobCancelled:
                    cancelled = True
                    for pending in futures:
                        pending.cancel()
                    break
        finally:
            # Wait for running items so no worker can keep writing into the
            # playlist after the caller has reported the job as terminal.
            pool.shutdown(wait=True, cancel_futures=True)
    if cancelled:
        raise JobCancelled("cancelled by the user")

    ordered = [results[i] for i in sorted(results)]
    items = [r[0] for r in ordered]
    entries = ordered_entries()

    # Playlist artwork: prefer the playlist's own, else the first video's.
    playlist_thumb = pdir / PLAYLIST_THUMB_NAME
    if not thumb_name:
        thumb_source = probe.playlist
        if not thumb_source.get("thumbnails"):
            first_info = next((i.info for i in probe.items if i.info), None)
            if first_info:
                thumb_source = first_info
        if force or not playlist_thumb.exists():
            fetch_thumbnail(thumb_source, playlist_thumb)
        thumb_name = PLAYLIST_THUMB_NAME if playlist_thumb.exists() else None

    with PUBLISH_LOCK:
        current_folder()
        playlist_doc = normalize_playlist(
            probe.playlist,
            entries=entries,
            thumbnail_file=thumb_name,
            existing=existing_doc,
            options=playlist_options,
        )
        if limit is not None:
            playlist_doc["partial"] = True
            playlist_doc["limit"] = limit
        write_json(pdir / "playlist.json", playlist_doc)

    transcript_results = [
        item.transcript for item in items
        if getattr(item, "transcript", None) is not None
    ]
    if fetch_transcripts and transcript_results:
        try:
            write_playlist_summary(pdir, playlist_doc, transcript_results)
        except Exception as exc:  # noqa: BLE001 - transcripts are optional
            if not quiet:
                print(f"transcript summary failed: {exc}", flush=True)

    total = sum(i.size for i in items)
    downloaded = len([i for i in items if i.status == "downloaded"])
    skipped = len([i for i in items if i.status == "skipped"])
    failed = len([i for i in items if i.status == "failed"])
    if not quiet:
        print()
        print(f"done: {downloaded} downloaded, {skipped} skipped, "
              f"{failed} failed; {human_bytes(total)} on disk")
        blocked = [i for i in items if i.error_kind == "region_blocked"]
        if blocked:
            where = ", ".join(covering_countries([i.countries for i in blocked]))
            print(f"{len(blocked)} region-blocked; countries that would cover "
                  f"them: {where or 'unknown'}")
    return FetchResult(pdir, playlist_doc, items, total)


def media_lookup(layout: LibraryLayout) -> Callable[[str, str], bool]:
    """``have(playlist_id, video_id)`` for :func:`probe_playlist`: whether
    the video's media is already in ``layout``'s library."""

    def have(playlist_id: str, video_id: str) -> bool:
        try:
            return find_media(layout, video_directory(layout, playlist_id, video_id)) is not None
        except (OSError, ValueError):
            return False

    return have


def _stored_height(vdir: Path) -> int | None:
    """Height of the media on disk, as recorded in ``meta.json``."""
    meta = read_json(vdir / "meta.json") or {}
    height = (meta.get("format") or {}).get("height")
    return height if isinstance(height, int) and height > 0 else None


def upgrade_lookup(layout: LibraryLayout, max_height: int | None) -> Callable[[str, str], bool]:
    """``have`` for an upgrade: videos already at or above ``max_height``
    count as done and are not read again.  Without a cap every downloaded
    video is read, since only YouTube knows whether more is on offer."""
    present = media_lookup(layout)

    def have(playlist_id: str, video_id: str) -> bool:
        if not max_height or not present(playlist_id, video_id):
            return False
        height = _stored_height(video_directory(layout, playlist_id, video_id))
        return height is not None and height >= max_height

    return have


def fetch_playlist(
    value: str,
    config: Config,
    *,
    limit: int | None = None,
    jobs: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    quiet: bool = False,
    max_height: int | None = None,
    target_mb_per_hour: float | None = None,
    min_height: int = 240,
    fetch_transcripts: bool = True,
    preserve_audio: bool | None = None,
    refresh_existing: bool = False,
    upgrade: bool = False,
    library_root: Path | None = None,
    cancel: Callable[[], bool] | None = None,
    on_item_start: StartCallback | None = None,
    on_item_done: ItemCallback | None = None,
    on_progress: Callable[[ProbedItem, int, int], None] | None = None,
) -> FetchResult:
    """Probe and download a playlist.  Videos already downloaded are not
    re-read from YouTube unless ``refresh_existing`` (or ``force``).

    ``upgrade`` re-reads downloaded videos stored below ``max_height`` (all
    of them without a cap) and replaces only those YouTube now offers taller;
    videos at or above the cap are kept untouched."""
    lock = threading.Lock()

    def probe_status(item: ProbedItem) -> None:
        if quiet:
            return
        if item.existing:
            detail = "already downloaded"
        elif item.error:
            detail = f"ERROR: {item.error}"
        elif target_mb_per_hour and target_mb_per_hour > 0:
            detail = "probed (efficient policy applied after the playlist)"
        elif item.selection:
            detail = item.selection.summary()
        else:
            detail = "no usable format"
        with lock:
            print(f"  probe {item.index:02d}/{item.id}: {item.title or item.id} "
                  f"— {detail}", flush=True)

    probe = probe_playlist(
        value, config, limit=limit, on_item=probe_status,
        max_height=max_height,
        target_mb_per_hour=target_mb_per_hour,
        min_height=min_height,
        probe_jobs=config.probe_jobs,
        cancel=cancel,
        preserve_audio=bool(preserve_audio),
        cache_root=library_root,
        have=(
            None if library_root is None or force or refresh_existing
            else upgrade_lookup(LibraryLayout(Path(library_root).expanduser()), max_height)
            if upgrade
            else media_lookup(LibraryLayout(Path(library_root).expanduser()))
        ),
    )
    return download_probe(
        probe, config,
        limit=limit,
        jobs=jobs,
        force=force,
        dry_run=dry_run,
        quiet=quiet,
        max_height=max_height,
        target_mb_per_hour=target_mb_per_hour,
        min_height=min_height,
        fetch_transcripts=fetch_transcripts,
        preserve_audio=preserve_audio,
        refresh_existing=refresh_existing,
        upgrade=upgrade,
        library_root=library_root,
        cancel=cancel,
        on_item_start=on_item_start,
        on_item_done=on_item_done,
        on_progress=on_progress,
    )
