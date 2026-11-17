"""Fetch transcripts for downloaded videos and keep the search index current.

Captions are acquired once and stored next to the media.  A video with no
caption track is recorded as ``missing`` and is otherwise treated as a normal
library item; it never blocks the playlist or the web UI.  Partial runs merge
the previous summary instead of truncating it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .. import __version__
from ..config import Config
from ..index import refresh_video_quietly
from ..layout import LibraryLayout
from ..yt.metadata import (
    find_media,
    now_iso,
    read_json,
    write_json,
)
from ..yt.probe import extract_video_info
from .captions import (
    CaptionData,
    CaptionError,
    choose_caption,
    download_caption,
    fetch_panel_api,
    fetch_transcript_api,
    parse_caption,
)
from .store import (
    SUBTITLE_DIR,
    index_video_dir,
    read_transcript,
    write_transcript,
)

SUMMARY_FILE = "transcripts.json"

log = logging.getLogger(__name__)


@dataclass
class TranscriptResult:
    id: str
    title: str
    status: str  # fetched | skipped | missing | failed | no_media
    language: str | None = None
    kind: str | None = None
    segments: int = 0
    raw_file: str | None = None
    error: str | None = None


def _safe_language(language: str) -> str:
    keep = "".join(
        char if char.isalnum() or char in "-_" else "_" for char in language
    )
    return keep or "und"


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def _update_meta(video_dir: Path, *, doc: dict, raw_file: str) -> None:
    """Record the transcript in meta.json.

    The fetch ran a network call since meta.json was read, and a rename or
    transfer may have rewritten ``files.media`` meanwhile: re-read it under
    the publish lock and change only the ``transcript`` key."""
    # Imported here: ``ytop.publish`` imports the transcript store, which
    # loads this package.
    from ..publish import PUBLISH_LOCK

    transcript = {
        "language": doc.get("language"),
        "kind": doc.get("kind"),
        "segments": len(doc.get("segments") or []),
        "file": "transcript.json",
        "raw_file": raw_file,
        "fetched_at": doc.get("fetched_at"),
        "source": doc.get("source"),
    }
    with PUBLISH_LOCK:
        meta = read_json(video_dir / "meta.json")
        if meta is None:
            return
        meta["transcript"] = transcript
        write_json(video_dir / "meta.json", meta)


def _export_srt(layout: LibraryLayout, playlist_id: str, label: str) -> None:
    """Export transcripts as ``.srt`` files beside the media."""
    # Imported here: ``ytop.publish`` imports the transcript store, which
    # loads this package.
    from .. import publish

    try:
        report = publish.sync_playlist_files(layout, playlist_id)
        for problem in report.errors:
            log.warning("srt export for %s: %s", label, problem)
    except Exception as exc:  # noqa: BLE001 - the transcript is still stored
        log.warning("srt export for %s failed: %s", label, exc)


def fetch_video_transcript(
    video_dir: Path,
    *,
    playlist_id: str,
    config: Config,
    languages: list[str] | None = None,
    force: bool = False,
    layout: LibraryLayout,
    index: bool = True,
    retries: int | None = None,
    backoff_s: float | None = None,
    sync_files: bool = True,
) -> TranscriptResult:
    """Fetch one video's transcript.  ``sync_files=False`` skips the
    playlist-wide ``.srt`` export; a batch caller then syncs once."""
    video_dir = Path(video_dir)
    meta = read_json(video_dir / "meta.json") or {}
    video_id = str(meta.get("id") or video_dir.name)
    title = str(meta.get("title") or video_id)
    existing = read_transcript(video_dir)
    settings = config.transcripts
    if retries is not None or backoff_s is not None:
        changes: dict = {}
        if retries is not None:
            changes["retries"] = max(0, int(retries))
        if backoff_s is not None:
            changes["backoff_s"] = max(0.0, float(backoff_s))
        settings = replace(settings, **changes)

    if existing is not None and not force:
        segments = len(existing.get("segments") or [])
        if index:
            try:
                index_video_dir(
                    layout, video_dir,
                    playlist_id=playlist_id, title=title,
                )
            except Exception:  # noqa: BLE001 - the file is still usable
                pass
            refresh_video_quietly(layout, playlist_id, video_id)
        return TranscriptResult(
            video_id, title, "skipped",
            language=existing.get("language"),
            kind=existing.get("kind"),
            segments=segments,
            raw_file=existing.get("raw_file"),
        )

    if find_media(layout, video_dir) is None:
        return TranscriptResult(
            video_id, title, "no_media", error="no media file on disk"
        )

    wanted = languages or list(settings.languages)

    # Primary route: YouTube's InnerTube transcript endpoint, which is much
    # less likely to be rate-limited than the timedtext URL.  Transient
    # failures are retried with exponential backoff.
    fetched: CaptionData | None = None
    for attempt in range(settings.retries + 1):
        try:
            fetched = fetch_transcript_api(video_id, wanted)
            break
        except Exception:  # noqa: BLE001 - retry, then fall back
            if attempt >= settings.retries:
                fetched = None
                break
            delay = settings.backoff_s * (2 ** attempt)
            if delay > 0:
                time.sleep(min(delay, 60.0))

    if fetched is None:
        try:
            # The download already stored the extractor payload.  Reusing it
            # avoids a fresh network probe when the transcript API is blocked.
            info = read_json(video_dir / "raw.json") or extract_video_info(
                video_id, config, layout.root
            )
        except Exception as exc:  # noqa: BLE001 - per-video failure
            return TranscriptResult(
                video_id, title, "failed",
                error=f"{type(exc).__name__}: {exc}",
            )

        caption = choose_caption(info, wanted)
        panel_error: str | None = None
        if caption is not None:
            try:
                raw = download_caption(
                    caption,
                    timeout=settings.timeout_s,
                    retries=settings.retries,
                    backoff_s=settings.backoff_s,
                )
                fetched = CaptionData(
                    language=caption.language,
                    kind=caption.kind,
                    ext=caption.ext,
                    segments=parse_caption(raw, caption.ext),
                    raw=raw,
                )
            except CaptionError as exc:
                panel_error = str(exc)

        # The signed timedtext URLs are frequently rate-limited or PO-token
        # gated.  YouTube's current transcript panel endpoint returns the whole
        # default transcript without a token, so it is the robust fallback.
        if fetched is None:
            try:
                fetched = fetch_panel_api(
                    video_id,
                    language=(
                        caption.language if caption is not None
                        else (wanted[0] if wanted else "en")
                    ),
                    kind=caption.kind if caption is not None else "auto",
                    timeout=settings.timeout_s,
                )
            except Exception as exc:  # noqa: BLE001 - fall back to an error
                panel_error = f"{type(exc).__name__}: {exc}"

        if fetched is None:
            if caption is None:
                return TranscriptResult(
                    video_id, title, "missing",
                    error=(
                        panel_error
                        or "no caption track in the requested languages"
                    ),
                )
            return TranscriptResult(
                video_id, title, "failed",
                language=caption.language, kind=caption.kind,
                error=panel_error or "transcript panel returned no segments",
            )

    if fetched is None:
        return TranscriptResult(
            video_id, title, "missing",
            error="no caption track in the requested languages",
        )

    language = _safe_language(fetched.language)
    raw_name = f"{language}.{fetched.kind}.{fetched.ext}"
    raw_path = video_dir / SUBTITLE_DIR / raw_name
    _write_bytes(raw_path, fetched.raw)
    raw_file = f"{SUBTITLE_DIR}/{raw_name}"

    doc = write_transcript(
        video_dir,
        video_id=video_id,
        title=title,
        language=fetched.language,
        kind=fetched.kind,
        segments=fetched.segments,
        raw_file=raw_file,
    )
    if meta:
        _update_meta(video_dir, doc=doc, raw_file=raw_file)

    if sync_files:
        _export_srt(layout, playlist_id, video_id)

    if index:
        try:
            index_video_dir(
                layout, video_dir, playlist_id=playlist_id, title=title
            )
        except Exception:  # noqa: BLE001 - indexing is derived state
            pass
        refresh_video_quietly(layout, playlist_id, video_id)

    return TranscriptResult(
        video_id, title, "fetched",
        language=fetched.language,
        kind=fetched.kind,
        segments=len(fetched.segments),
        raw_file=raw_file,
    )


def fetch_playlist_transcripts(
    playlist_dir: Path,
    playlist: dict,
    *,
    config: Config,
    video_ids: list[str] | None = None,
    languages: list[str] | None = None,
    force: bool = False,
    delay_s: float | None = None,
    layout: LibraryLayout,
    index: bool | None = None,
    show_progress: bool = True,
    retries: int | None = None,
    backoff_s: float | None = None,
) -> list[TranscriptResult]:
    playlist_dir = Path(playlist_dir)
    playlist_id = str(playlist.get("id") or "")
    wanted = set(video_ids) if video_ids is not None else None
    settings = config.transcripts
    delay = settings.delay_s if delay_s is None else max(0.0, delay_s)
    do_index = settings.index if index is None else index

    results: list[TranscriptResult] = []
    entries = list(playlist.get("entries") or [])
    needed = [
        entry for entry in entries
        if not wanted or str(entry.get("id")) in wanted
    ]
    for position, entry in enumerate(needed, start=1):
        video_id = str(entry.get("id") or "")
        if not video_id:
            continue
        video_dir = layout.video_meta_dir(playlist_id, video_id)
        result = fetch_video_transcript(
            video_dir,
            playlist_id=playlist_id,
            config=config,
            languages=languages,
            force=force,
            layout=layout,
            index=do_index,
            retries=retries,
            backoff_s=backoff_s,
            # One sync for the whole batch, not one per video (O(n^2)).
            sync_files=False,
        )
        results.append(result)
        if show_progress:
            detail = ""
            if result.status == "fetched":
                detail = f" ({result.language}, {result.segments} segments)"
            elif result.error:
                detail = f" ({result.error})"
            print(
                f"  {position:02d}/{len(needed):02d} {video_id}: "
                f"{result.status}{detail}",
                flush=True,
            )
        if delay and position < len(needed):
            time.sleep(delay)

    if any(result.status == "fetched" for result in results):
        _export_srt(layout, playlist_id, playlist_id)
    write_playlist_summary(playlist_dir, playlist, results)
    return results


def write_playlist_summary(
    playlist_dir: Path,
    playlist: dict,
    results: list[TranscriptResult],
) -> dict:
    # playlist.json is read, changed and rewritten here; a concurrent publish
    # or folder rename must not interleave with it.
    from ..publish import PUBLISH_LOCK

    with PUBLISH_LOCK:
        return _write_playlist_summary(Path(playlist_dir), playlist, results)


def _write_playlist_summary(
    playlist_dir: Path,
    playlist: dict,
    results: list[TranscriptResult],
) -> dict:
    path = playlist_dir / SUMMARY_FILE
    previous = read_json(path) or {}
    rows = {
        str(row.get("id")): row
        for row in previous.get("videos") or []
        if row.get("id")
    }
    for result in results:
        rows[result.id] = asdict(result)

    order = {
        str(entry.get("id")): index
        for index, entry in enumerate(
            (read_json(playlist_dir / "playlist.json") or playlist).get("entries")
            or []
        )
    }
    merged = sorted(
        (row for key, row in rows.items() if key in order),
        key=lambda row: order[str(row["id"])],
    )
    totals = {
        "videos": len(merged),
        "available": sum(
            1 for row in merged if row.get("status") in {"fetched", "skipped"}
        ),
        "new": sum(1 for row in results if row.status == "fetched"),
        "missing": sum(1 for row in merged if row.get("status") == "missing"),
        "failed": sum(1 for row in merged if row.get("status") == "failed"),
        "segments": sum(int(row.get("segments") or 0) for row in merged),
    }
    doc = {
        "schema": 1,
        "playlist_id": str(playlist.get("id") or ""),
        "generated_at": now_iso(),
        "generator": f"ytop {__version__}",
        "videos": merged,
        "totals": totals,
    }
    write_json(path, doc)

    playlist = dict(read_json(playlist_dir / "playlist.json") or playlist)
    playlist["transcripts"] = {
        "updated_at": doc["generated_at"],
        "file": SUMMARY_FILE,
        **{key: totals[key] for key in (
            "videos", "available", "missing", "failed", "segments"
        )},
    }
    write_json(playlist_dir / "playlist.json", playlist)
    return doc

