"""Background probe/download/move/make-stick jobs for the local web UI.

A job is persisted to its destination library's ``jobs.json`` so the
downloads page shows recent activity after a reload, and a stick carries its
own job history.  Download defaults come from the destination library's
``config.toml``.  Raw extractor payloads are never
persisted: only the compact review needed to show a playlist before download.

Lifecycle invariant: a terminal status (``ready``, ``done``, ``failed``,
``cancelled``) means no worker can still mutate the job or the library.  A
cancel requested while a worker is active moves the job to ``cancelling``; it
becomes ``cancelled`` only after that worker has exited.

A job whose library is unplugged becomes ``paused_offline``: it is never
failed for that, and Resume restarts it once the library is back.
"""

from __future__ import annotations

import contextlib
import copy
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import makestick
from ..autoupdate import AutoUpdate
from ..config import SESSION_AUTH, Config, load_config
from ..fsinfo import fits_on, free_bytes
from ..libraries import READ_ONLY_MESSAGE, LibraryRef, LibrarySet
from ..timing import Phase, summary_lines
from ..yt.download import (
    ItemResult,
    download_probe,
    media_destination,
    media_lookup,
    upgrade_lookup,
)
from ..yt.formats import allocate_efficient, audio_detail, select_format
from ..yt.metadata import (
    CorruptDocumentError,
    human_bytes,
    now_iso,
    playlist_directory,
    quarantine_document,
    read_json,
    read_json_document,
    sanitize_component,
    write_json,
)
from ..yt.probe import (
    JobCancelled,
    ProbedItem,
    ProbeResult,
    classify_failure,
    normalize_playlist_url,
    probe_playlist,
    probe_video,
    reusing_analyses,
)
from . import fsops

# Kept for callers that imported the old private helper; the implementation
# lives in ``fsops`` so it can be tested independently of job state.
_copy_tree = fsops.copy_tree
from .library import UNGROUPED_ID, UNGROUPED_TITLE, Library

log = logging.getLogger(__name__)

MAX_JOBS = 50
ACTIVE = {"probing", "cancelling", "queued", "running"}
TERMINAL = {"ready", "done", "failed", "cancelled"}
# Not active (no worker) and not terminal: Resume once the library is back.
PAUSED = "paused_offline"
ALREADY_DOWNLOADING = "already downloading in another job"
# Rows written before libraries replaced storage locations.
LEGACY_KEYS = {
    "location_id": "library_id",
    "location_label": "library_label",
    "location_explicit": "library_explicit",
    "source_location_id": "source_library_id",
    "source_location_label": "source_library_label",
}
ConfigFor = Callable[[Path | None], Config]
# Jobs that copy files and report move_* progress instead of downloading.
COPY_KINDS = ("move", "make_stick")
DOWNLOAD_KINDS = ("playlist", "video")
UPDATE_TITLE = "Update YouTube parts"
INTERRUPTED_MESSAGE = (
    "interrupted before it finished (YTOP stopped or the drive was "
    "unplugged); click Resume to continue"
)
# Nonterminal progress is coalesced; a terminal transition always flushes.
SAVE_INTERVAL_S = 0.75


# Heights a web upgrade pass can raise a playlist to (#54).
UPGRADE_HEIGHTS = (720, 1080, 1440, 2160)


class JobDestinationError(RuntimeError):
    """A configured destination is gone or offline; never silently replace it."""


class LibraryOfflineError(JobDestinationError):
    """The job's library is unplugged; the job pauses until it returns."""


def default_config_for(root: Path | None) -> Config:
    """A library's download config plus this session's browser login."""
    return SESSION_AUTH.apply(load_config(root))


BOT_BLOCKED_NOTICE = (
    "YouTube started asking whether this computer is a bot, so YTOP stopped "
    "reading the rest of the playlist (more requests make the block last "
    "longer). Wait a while and review it again, or sign in with your browser "
    "for this session."
)


def _job_from_row(row: dict, library_id: str) -> Job:
    """Build a job from a stored row, mapping pre-library keys."""
    values = dict(row)
    for old, new in LEGACY_KEYS.items():
        if old in values:
            legacy = values.pop(old)
            values.setdefault(new, legacy)
    if not values.get("library_id"):
        values["library_id"] = library_id
    return Job(**{
        key: value for key, value in values.items()
        if key in Job.__dataclass_fields__
    })


# Fixed resolution caps offered between Efficient and Native.
QUALITY_TIERS = (480, 720, 1080, 1440, 2160)
TIER_POLICIES = tuple(f"{height}p" for height in QUALITY_TIERS)


def _timed_out(error: object) -> bool:
    """A video failed because a helper process ran out of time (the
    PO-token script's Deno on a busy stick): worth one more try."""
    return "TimeoutExpired" in str(error or "")


def policy_settings(policy: str, config: Config) -> tuple[float, int]:
    """``(target_mb_per_hour, max_height)`` for a review quality choice."""
    if policy == "efficient":
        return float(config.target_mb_per_hour), int(config.max_height)
    if policy == "native":
        return 0.0, 0
    if policy in TIER_POLICIES:
        return 0.0, int(policy[:-1])
    raise ValueError(f"unknown download quality: {policy!r}")


def _suggested_policy(stored, review: dict) -> str:
    """The review choice to preselect from a playlist's stored quality.

    A remembered tier that is no longer smaller than Native (the videos above
    it went away) means Native now; anything unknown means Efficient.
    """
    if not isinstance(stored, str):
        return "efficient"
    if stored in {"efficient", "native", *review["estimate_tiers"]}:
        return stored
    if stored in TIER_POLICIES:
        tallest = max(
            (int(row.get("native_height") or 0) for row in review["items"]),
            default=0,
        )
        if tallest and int(stored[:-1]) >= tallest:
            return "native"
    return "efficient"


@dataclass
class Job:
    id: str
    status: str = "queued"  # probing | cancelling | ready | queued | running | done | failed | cancelled | paused_offline
    url: str = ""
    playlist_id: str = ""
    title: str = ""
    channel: str = ""
    description: str = ""
    video_count: int = 0
    kind: str = "playlist"  # playlist | video | move | make_stick | update
    video_id: str = ""
    target_playlist_id: str = ""
    target_playlist_title: str = ""
    preserve_audio: bool = False
    audio_policy: str = "inherit"  # inherit | enabled | disabled
    library_id: str = ""
    library_label: str = ""
    library_explicit: bool = False
    source_library_id: str = ""
    source_library_label: str = ""
    move_bytes: int = 0
    move_total_bytes: int = 0
    move_files: int = 0
    move_total_files: int = 0
    move_phase: str = ""  # staging | copying | published | cleaning_source | done
    source_path: str = ""
    target_path: str = ""
    temp_path: str = ""
    source_cleanup_complete: bool = True
    # make_stick: new | upgrade, and the platforms the stick gets.
    stick_mode: str = ""
    stick_platforms: list[str] = field(default_factory=list)
    probe_done: int = 0
    probe_started: int = 0
    total_duration: float = 0.0
    estimate_bytes: int = 0
    estimate_native_bytes: int = 0
    # Fixed resolution caps offered on the review: {"720p": bytes, ...}.
    estimate_tiers: dict = field(default_factory=dict)
    # Free space on the destination drive when the review was built.
    free_bytes: int | None = None
    # The quality the review preselects (the playlist's last choice).
    suggested_policy: str = "efficient"
    # A sync: videos already downloaded (not re-read for the review), and
    # whether the download re-reads their details anyway.
    existing_count: int = 0
    refresh_existing: bool = False
    # An upgrade pass (``ytop fetch --upgrade``): only videos stored below
    # this height (or missing) are read again; 0 is an ordinary sync.
    upgrade_height: int = 0
    # Videos whose selected rendition exceeds FAT32's 4 GB file limit on the
    # destination drive (the download refuses them).
    fat32_warning: bool = False
    fat32_count: int = 0
    policy: str = "efficient"  # efficient | native | 480p..2160p
    target_mb_per_hour: float = 100.0
    max_height: int = 0
    cancel_requested: bool = False
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    current_id: str | None = None
    current_title: str | None = None
    current_bytes: int = 0
    current_total: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_downloaded: int = 0
    in_library: bool = False
    error: str | None = None
    # What the component updater did for this job (see ytop.autoupdate).
    notice: str = ""
    # The status a paused job had: a paused review ("ready") goes back to
    # its review on Resume instead of downloading unconfirmed.
    paused_from: str = ""
    # Wall-clock per step of the last make-stick or analysis (see
    # ytop.timing): [{"name", "seconds", "count", "unit", "bytes", "note"}].
    timings: list[dict] = field(default_factory=list)
    items: list[dict] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.status in ACTIVE

    @property
    def timing_lines(self) -> list[str]:
        """The stored timings as aligned lines (empty if none)."""
        phases = []
        for row in self.timings:
            try:
                phases.append(Phase(**{
                    key: row[key] for key in Phase.__dataclass_fields__ if key in row
                }))
            except (TypeError, KeyError):
                continue
        return summary_lines(phases)

    @property
    def estimate_label(self) -> str:
        return human_bytes(self.estimate_bytes)

    @property
    def estimate_native_label(self) -> str:
        return human_bytes(self.estimate_native_bytes)

    @property
    def quality_options(self) -> list[dict]:
        """Download choices for the review: Efficient, tiers, Native."""
        options = [("efficient", "Efficient", self.estimate_bytes)]
        for height in QUALITY_TIERS:
            key = f"{height}p"
            if key in self.estimate_tiers:
                options.append(
                    (key, f"{key} max", int(self.estimate_tiers[key]))
                )
        options.append(("native", "Native", self.estimate_native_bytes))
        # The totals count only videos not already downloaded (a sync's
        # existing videos are left out of the analysis).
        show_fit = self.free_bytes is not None
        return [
            {
                "policy": policy,
                "label": label,
                "bytes": size,
                "size_label": human_bytes(size),
                "fits": (size <= self.free_bytes) if show_fit else None,
            }
            for policy, label, size in options
        ]

    @property
    def policy_label(self) -> str:
        if self.policy == "native":
            return "native maximum"
        if self.policy in TIER_POLICIES:
            return f"{self.policy} max"
        return f"efficient, {self.target_mb_per_hour:g} MB/h budget"

    @property
    def bytes_label(self) -> str:
        return human_bytes(self.bytes_downloaded)

    @property
    def percent(self) -> float:
        if self.kind in COPY_KINDS:
            if self.move_total_bytes:
                return max(
                    0.0, min(1.0, self.move_bytes / self.move_total_bytes)
                )
            return 1.0 if self.status == "done" else 0.0
        if not self.video_count:
            return 0.0
        done = self.downloaded + self.skipped + self.failed
        return max(0.0, min(1.0, done / self.video_count))

    @property
    def moved_label(self) -> str:
        return human_bytes(self.move_bytes)

    @property
    def move_total_label(self) -> str:
        return human_bytes(self.move_total_bytes)

    @property
    def copy_progress_label(self) -> str:
        """Progress of an active move/make-stick job.  Phases that count
        files rather than bytes (checking, switching) report total_bytes 0,
        so the "X of Y" byte pair is left out instead of showing "0 B of 0 B"."""
        parts = []
        if self.move_total_bytes:
            parts.append(f"{self.moved_label} of {self.move_total_label}")
        if self.move_total_files:
            parts.append(f"{self.move_files}/{self.move_total_files} files")
        if self.current_title:
            parts.append(self.current_title)
        return " · ".join(parts)

    @property
    def current_percent(self) -> float:
        if not self.current_total:
            return 0.0
        return max(0.0, min(1.0, self.current_bytes / self.current_total))

    @property
    def probe_in_flight(self) -> int:
        return max(0, self.probe_started - self.probe_done)

    @property
    def status_label(self) -> str:
        labels = {
            "probing": "Reading metadata",
            "cancelling": "Cancelling",
            "ready": "Ready to download",
            "queued": "Queued",
            "running": "Running",
            "done": "Complete",
            "failed": "Failed",
            "cancelled": "Cancelled",
            PAUSED: "Paused",
        }
        return labels.get(self.status, self.status)


def _compact_probe(
    probe: ProbeResult,
    config: Config,
    preserve_audio: bool = False,
    destination: Path | None = None,
) -> dict:
    items = []
    total_duration = 0.0
    efficient_total = 0
    native_total = 0
    tier_totals = {height: 0 for height in QUALITY_TIERS}
    fat32_count = 0
    existing_count = 0
    for item in probe.items:
        if item.existing:
            existing_count += 1
            duration = item.duration or 0
            total_duration += duration
            items.append({
                "index": item.index,
                "id": item.id,
                "title": item.title or item.id,
                "duration": duration,
                "duration_label": _duration_label(duration),
                "error": None,
                "status": "pending",
                "existing": True,
                "size": 0,
                "native_size": 0,
                "native_height": None,
                "native_codec": None,
                "fat32_warning": False,
            })
            continue
        selection = item.selection
        native = None
        if item.info is not None:
            try:
                native = select_format(
                    item.info, config.audio_language, None, preserve_audio
                )
            except Exception:  # noqa: BLE001 - already recorded on the item
                native = None
            if native is not None:
                for height in QUALITY_TIERS:
                    try:
                        tier_totals[height] += select_format(
                            item.info, config.audio_language, height,
                            preserve_audio,
                        ).estimated_bytes
                    except Exception:  # noqa: BLE001 - same as native
                        continue
        duration = item.duration or 0
        total_duration += duration
        efficient_size = selection.estimated_bytes if selection else 0
        native_size = native.estimated_bytes if native else 0
        efficient_total += efficient_size
        native_total += native_size
        fat32_warning = bool(
            destination is not None
            and selection is not None
            and not fits_on(destination, efficient_size)
        )
        fat32_count += fat32_warning
        item_row = {
            "index": item.index,
            "id": item.id,
            "title": item.title or item.id,
            "duration": duration,
            "duration_label": _duration_label(duration),
            "error": item.error,
            "error_kind": item.error_kind,
            "status": "failed" if item.error or selection is None else "pending",
            "size": efficient_size,
            "native_size": native_size,
            "native_height": native.video.get("height") if native else None,
            "native_codec": native.video_family if native else None,
            "fat32_warning": fat32_warning,
        }
        if selection is not None:
            item_row.update({
                "height": selection.video.get("height"),
                "fps": selection.video.get("fps"),
                "video_codec": selection.video_family,
                "audio_codec": selection.audio_family,
                "audio_detail": audio_detail(selection.audio),
                "container": selection.container,
                "audio_alternates": len(selection.alternates),
            })
        items.append(item_row)
    return {
        "id": str(probe.playlist.get("id") or ""),
        "title": str(probe.playlist.get("title") or probe.playlist.get("id") or ""),
        "channel": str(
            probe.playlist.get("channel") or probe.playlist.get("uploader") or ""
        ),
        "description": str(probe.playlist.get("description") or ""),
        "video_count": len(probe.items),
        "total_duration": total_duration,
        "estimate_bytes": efficient_total,
        "estimate_native_bytes": native_total,
        "estimate_tiers": {
            f"{height}p": total
            for height, total in tier_totals.items()
            if total < native_total
        },
        "free_bytes": free_bytes(destination) if destination is not None else None,
        "fat32_warning": fat32_count > 0,
        "fat32_count": fat32_count,
        "existing_count": existing_count,
        "items": items,
    }


def _duration_label(value: float) -> str:
    seconds = int(value or 0)
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


class _OwnedRLock:
    """A re-entrant lock that can tell whether the calling thread holds it.

    Use it only as a context manager: a bare ``acquire()`` or a
    ``threading.Condition`` around it would bypass the ownership count."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._local = threading.local()

    def __enter__(self) -> _OwnedRLock:
        self._lock.acquire()
        self._local.depth = getattr(self._local, "depth", 0) + 1
        return self

    def __exit__(self, *_exc: object) -> None:
        self._local.depth -= 1
        self._lock.release()

    def held(self) -> bool:
        return getattr(self._local, "depth", 0) > 0


class JobManager:
    """Background workers, a durable job list, and compact probe reviews."""

    def __init__(
        self,
        library: Path | Library,
        *,
        config_for: ConfigFor | None = None,
        start_workers: bool = True,
        autoupdate: AutoUpdate | None = None,
    ):
        if isinstance(library, Library):
            self._lib = library
        else:
            self._lib = Library(LibrarySet(Path(library).expanduser(), detect=False))
        # Every job reads the config of its own destination library.
        self._config_for = config_for or default_config_for
        self._start_workers = start_workers
        # Updates the stick's YouTube parts when YouTube changed (None: never).
        self._autoupdate = autoupdate
        # Lock order: PUBLISH_LOCK before this one (a move reports progress
        # while it publishes).  Code holding this lock may list libraries,
        # which can recover a re-seen stick under PUBLISH_LOCK; that hook
        # must not wait for PUBLISH_LOCK when holds_lock() is true.
        self._lock = _OwnedRLock()
        # A probe must never wait for a long download, and a download must not
        # be blocked by a slow playlist probe.
        self._probe_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="ytop-probe"
        )
        self._download_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="ytop-download"
        )
        self._update_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="ytop-update"
        )
        self._running: set[str] = set()
        self._futures: dict[str, Future] = {}
        self._probes: dict[str, ProbeResult] = {}
        self._base_bytes: dict[str, int] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._jobs: dict[str, Job] = {}
        # make_stick jobs waiting for their worker: (plan, source image).
        self._stick_plans: dict[str, tuple[makestick.MakePlan, Path]] = {}
        # Online writable libraries whose jobs.json has been read.
        self._refs: dict[str, LibraryRef] = {}
        # The library whose jobs.json a job was read from: a job whose own
        # library is unplugged is written back there rather than dropped.
        self._origin: dict[str, str] = {}
        # Libraries whose jobs.json this manager keeps up to date, and what
        # was last written to each (unchanged files are not rewritten).
        self._files: set[str] = set()
        self._written: dict[str, list[dict]] = {}
        # Libraries whose last jobs.json write failed (warned about once).
        self._unsaved: set[str] = set()
        self._save_pending = False
        self._last_save = 0.0
        self._closed = False
        self._load()
        self._reconcile_moves()

    # -- config ------------------------------------------------------------
    def _config_for_ref(self, ref: LibraryRef | None) -> Config:
        return self._config_for(ref.root if ref is not None else None)

    def _job_config(self, job: Job) -> Config:
        """The config of the job's destination library (defaults if it is
        not connected)."""
        ref = self._lib.libraries.get(job.library_id) if job.library_id else None
        return self._config_for_ref(ref)

    def _media_lookup(self, job: Job) -> Callable[[str, str], bool] | None:
        """Videos already downloaded into the job's library, which a probe
        does not re-read (``None``: the library is not known or online).  For
        an upgrade, only those stored at or above its height."""
        ref = self._lib.libraries.get(job.library_id) if job.library_id else None
        if ref is None:
            return None
        if job.upgrade_height:
            return upgrade_lookup(ref.layout, job.upgrade_height)
        return media_lookup(ref.layout)

    def _job_cache_root(self, job: Job) -> Path | None:
        """Where yt-dlp's cache lives: the job's destination library, else
        the default library, so nothing lands in the host's ~/.cache."""
        ref = self._lib.libraries.get(job.library_id) if job.library_id else None
        if ref is None:
            try:
                ref = self._lib.libraries.default()
            except LookupError:
                return None
        return ref.root

    @property
    def config(self) -> Config:
        """The default library's config (what a new job would start with)."""
        try:
            ref: LibraryRef | None = self._lib.libraries.default()
        except LookupError:
            ref = None
        return self._config_for_ref(ref)

    # -- persistence -------------------------------------------------------
    def _online_writable(self) -> list[LibraryRef]:
        return [ref for ref in self._lib.libraries.libraries() if not ref.read_only]

    def _library_online(self, library_id: str) -> bool:
        ref = self._lib.libraries.get(library_id)
        if ref is None:
            return False
        try:
            return ref.layout.marker.is_file()
        except OSError:
            return False

    def _load(self) -> None:
        with self._lock:
            self._jobs = {}
            self._refs = {}
            self._origin = {}
            self._files = set()
            self._written = {}
            self._merge_libraries(self._online_writable())
            self._save(force=True)

    def _merge_libraries(self, refs: list[LibraryRef]) -> bool:
        """Read the jobs.json of newly seen libraries; jobs already in memory
        win.  Returns whether anything was added."""
        added = False
        for ref in refs:
            self._refs[ref.id] = ref
            path = ref.layout.jobs_file
            document = read_json_document(path)
            if document.corrupt or document.status == "unreadable":
                # Preserve the user's job history rather than resetting it.
                quarantine_document(path, document)
            if document.ok:
                self._files.add(ref.id)
            doc = document.data if document.ok else {}
            rows = (doc or {}).get("jobs") or []
            if document.ok and isinstance(rows, list):
                # What the file holds: an unchanged file is never rewritten
                # (a locked stick or read-only mount is only listed).
                self._written[ref.id] = copy.deepcopy(rows)
            broken = 0
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict) or not row.get("id"):
                    continue
                if str(row["id"]) in self._jobs:
                    continue
                try:
                    job = _job_from_row(row, ref.id)
                except (TypeError, ValueError):
                    broken += 1
                    continue
                if job.kind in DOWNLOAD_KINDS and job.status in {
                    "queued", "running",
                }:
                    # Its worker is gone (YTOP stopped, or the stick was
                    # pulled before the pause could be saved to it).
                    self._set_paused(job, INTERRUPTED_MESSAGE)
                elif job.status in ACTIVE:
                    job.status = "failed"
                    job.error = "interrupted by a server restart"
                    job.finished_at = now_iso()
                    job.current_id = None
                    job.current_title = None
                    job.cancel_requested = False
                self._jobs[job.id] = job
                self._origin[job.id] = ref.id
                added = True
            if broken:
                log.warning("%s: ignored %d malformed job rows", path, broken)
        return added

    def _load_new_libraries(self) -> None:
        """Pick up libraries plugged in since the last look, and pause the
        waiting jobs of libraries that were unplugged."""
        try:
            # Taken before the listing too: a library that finishes its
            # recovery in between is hidden from the listing and gone from
            # the later snapshot, yet online.
            was_recovering = self._lib.libraries.recovering()
            every = self._lib.libraries.libraries()
        except Exception:  # noqa: BLE001 - discovery must never break a page
            log.exception("library discovery failed")
            return
        refs = [ref for ref in every if not ref.read_only]
        online = {ref.id for ref in refs}
        with self._lock:
            if self._closed:
                return
            # Hidden while recover_library runs for it, but not unplugged.
            recovering = was_recovering | self._lib.libraries.recovering()
            online |= recovering
            for library_id in [key for key in self._refs if key not in online]:
                self._refs.pop(library_id, None)
                # It may have changed elsewhere; rewrite it when it returns.
                self._written.pop(library_id, None)
                self._unsaved.discard(library_id)
            new = [ref for ref in refs if ref.id not in self._refs]
            added = self._merge_libraries(new) if new else False
            paused = self._pause_offline({ref.id for ref in every} | recovering)
            if added or paused or new:
                self._save(force=True)

    def _offline_message(self, job: Job) -> str:
        return (
            f"{job.library_label or 'its library'} is not connected; plug it "
            "in and click Resume"
        )

    def _pause_offline(self, online: set[str]) -> bool:
        """Jobs waiting on an unplugged library pause; running ones pause
        from their own worker."""
        changed = False
        for job in self._jobs.values():
            if (
                job.kind in COPY_KINDS
                or not job.library_id
                or job.library_id in online
                or job.status not in {"ready", "queued"}
                or job.id in self._running
            ):
                continue
            future = self._futures.pop(job.id, None)
            if future is not None:
                future.cancel()
            self._set_paused(job, self._offline_message(job))
            changed = True
        return changed

    @staticmethod
    def _set_paused(job: Job, message: str) -> None:
        if job.status != PAUSED:
            job.paused_from = job.status
        job.status = PAUSED
        job.error = message
        job.cancel_requested = False
        job.current_id = None
        job.current_title = None
        job.current_bytes = 0
        job.current_total = 0
        for row in job.items:
            if row.get("status") == "downloading":
                row["status"] = "pending"
        job.updated_at = now_iso()

    def _pause(self, job_id: str, message: str | None = None) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            self._set_paused(job, message or self._offline_message(job))
            self._save(force=True)

    def holds_lock(self) -> bool:
        """The calling thread holds this manager's lock."""
        return self._lock.held()

    def busy_with(self, library_id: str) -> bool:
        """An active job reads or writes the library ``library_id``."""
        with self._lock:
            return any(
                job.active
                and library_id in (job.library_id, job.source_library_id)
                for job in self._jobs.values()
            )

    def downloading(self, library_id: str) -> bool:
        """A download job is queued or running into ``library_id``."""
        with self._lock:
            return any(
                job.kind in DOWNLOAD_KINDS and job.status in {"queued", "running"}
                and job.library_id == library_id
                for job in self._jobs.values()
            )

    def recover_library(self, ref: LibraryRef) -> None:
        """Adopt a library first seen after startup (a stick plugged in
        mid-session): read its jobs and finish or report the moves that
        were interrupted in it, before any job or page uses it."""
        if ref.read_only:
            return
        with self._lock:
            if self._closed:
                return
            # The library set saw it go away and come back, even if this
            # manager missed the unplug: it may have changed elsewhere.
            self._refs.pop(ref.id, None)
            self._written.pop(ref.id, None)
            self._unsaved.discard(ref.id)
            before = set(self._jobs)
            self._merge_libraries([ref])
            touching = {
                job.id for job in self._jobs.values()
                if job.id not in before or (
                    job.kind == "move" and not job.active
                    and ref.id in (job.library_id, job.source_library_id)
                )
            }
            self._reconcile_moves(touching)
            self._save(force=True)

    def _move_paths(self, job: Job) -> tuple[Path, Path, Path | None]:
        """A move's recorded folders, re-rooted where each library is
        mounted now: a stick that comes back under another mount point or
        drive letter still reconciles.  Libraries this manager does not know
        keep the recorded path."""

        def name(raw: str) -> str:
            return re.split(r"[\\/]", raw.rstrip("\\/"))[-1]

        source = Path(job.source_path)
        target = Path(job.target_path)
        staging = Path(job.temp_path) if job.temp_path else None
        # The manager's own map: listing libraries here could run a new
        # library's recovery hook while this thread holds the job lock.
        source_ref = self._refs.get(job.source_library_id)
        dest_ref = self._refs.get(job.library_id)
        if source_ref is not None:
            source = source_ref.layout.meta_root / name(job.source_path)
        if dest_ref is not None:
            target = dest_ref.layout.meta_root / name(job.target_path)
            if job.temp_path:
                staging = dest_ref.layout.meta_root / name(job.temp_path)
        return source, target, staging

    def _reconcile_moves(self, only: set[str] | None = None) -> None:
        """Finish or honestly report moves interrupted by a restart."""
        changed = False
        for job in list(self._jobs.values()):
            if only is not None and job.id not in only:
                continue
            if job.kind != "move" or job.status in {"done", "cancelled"}:
                continue
            if not (job.source_path and job.target_path):
                job.status = "failed"
                job.error = (
                    "the move was interrupted before its paths were recorded; "
                    "check both storage locations"
                )
                job.finished_at = job.finished_at or now_iso()
                changed = True
                continue
            source, target, staging = self._move_paths(job)
            result = fsops.reconcile_move(
                source=source, target=target, staging=staging,
            )
            job.move_phase = result.phase
            job.source_cleanup_complete = result.cleanup_complete
            if result.status == "done":
                job.status = "done"
                job.finished_at = job.finished_at or now_iso()
                job.error = None
                job.move_bytes = job.move_total_bytes
                job.move_files = job.move_total_files
            else:
                job.status = "failed"
                job.error = result.error
                job.finished_at = job.finished_at or now_iso()
            changed = True
        if changed:
            self._save(force=True)

    def _serialise(self) -> list[Job]:
        jobs = sorted(
            self._jobs.values(), key=lambda job: job.created_at, reverse=True
        )
        active = [job for job in jobs if job.active]
        inactive = [job for job in jobs if not job.active]
        room = max(0, MAX_JOBS - len(active))
        keep = active + inactive[:room]
        # Drop auxiliary state for evicted jobs so it cannot leak forever.
        kept_ids = {job.id for job in keep}
        for job_id in list(self._jobs):
            if job_id not in kept_ids:
                self._probes.pop(job_id, None)
                self._cancel.pop(job_id, None)
                self._base_bytes.pop(job_id, None)
                self._futures.pop(job_id, None)
                self._stick_plans.pop(job_id, None)
        self._jobs = {job.id: job for job in keep}
        return keep

    def _save_target(self, job: Job) -> str | None:
        """The library whose jobs.json holds ``job``: its own when online,
        else the one it was read from (so an unplugged library's job is not
        lost), else none (its own file still holds it)."""
        if job.library_id in self._refs:
            return job.library_id
        origin = self._origin.get(job.id)
        if origin in self._refs:
            return origin
        return None

    def _save(self, *, force: bool = True) -> None:
        """Persist each job to its own library's jobs.json.  Nonterminal
        progress is coalesced; terminal state is always flushed
        synchronously."""
        with self._lock:
            now = time.monotonic()
            if not force and now - self._last_save < SAVE_INTERVAL_S:
                self._save_pending = True
                return
            keep = self._serialise()
            # Files that lost their last job are rewritten empty.
            groups: dict[str, list[Job]] = {
                library_id: [] for library_id in self._files
                if library_id in self._refs
            }
            for job in keep:
                target = self._save_target(job)
                if target is not None:
                    groups.setdefault(target, []).append(job)
            for library_id, jobs in groups.items():
                ref = self._refs[library_id]
                rows = [asdict(job) for job in jobs]
                if self._written.get(library_id) == rows:
                    continue
                try:
                    # Never recreate the folders of a stick unplugged since
                    # the last scan.
                    if not ref.layout.marker.is_file():
                        continue
                    write_json(ref.layout.jobs_file, {
                        "schema": 1,
                        "jobs": rows,
                    })
                except OSError as exc:
                    # A locked stick or read-only mount: say so once, not on
                    # every progress save.
                    if library_id not in self._unsaved:
                        self._unsaved.add(library_id)
                        log.warning(
                            "could not save jobs to %s: %s", ref.root, exc
                        )
                    continue
                self._unsaved.discard(library_id)
                self._files.add(library_id)
                self._written[library_id] = rows
                for job in jobs:
                    self._origin[job.id] = library_id
            self._last_save = now
            self._save_pending = False

    def _flush_save(self) -> None:
        with self._lock:
            if self._save_pending:
                self._save(force=True)

    def set_notice(self, job_id: str, text: str) -> None:
        self._update(job_id, notice=text)

    def _update(self, job_id: str, **fields) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            previous_status = job.status
            for key, value in fields.items():
                if not hasattr(job, key):
                    continue
                if key in {"probe_done", "probe_started"}:
                    value = max(int(value or 0), int(getattr(job, key) or 0))
                setattr(job, key, value)
            job.updated_at = now_iso()
            force = (
                "status" in fields and job.status != previous_status
                and job.status in TERMINAL
            ) or not job.active
            self._save(force=force)
            return copy.deepcopy(job)

    def _bump(self, job_id: str, name: str, amount: int = 1) -> None:
        """Increment an event counter; indices are not counters."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            setattr(job, name, int(getattr(job, name) or 0) + int(amount))
            job.updated_at = now_iso()
            self._save(force=False)

    # -- queries -----------------------------------------------------------
    def _live(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        self._load_new_libraries()
        with self._lock:
            return [
                copy.deepcopy(job) for job in sorted(
                    self._jobs.values(),
                    key=lambda job: job.created_at,
                    reverse=True,
                )
            ]

    def get(self, job_id: str) -> Job | None:
        self._load_new_libraries()
        with self._lock:
            job = self._jobs.get(job_id)
            return copy.deepcopy(job) if job is not None else None

    # -- probe -------------------------------------------------------------
    def _default_library(self) -> LibraryRef:
        try:
            ref = self._lib.libraries.default()
        except LookupError as exc:
            raise JobDestinationError(
                "no library is connected; plug one in or add one on the "
                "Storage page"
            ) from exc
        return self._writable(ref)

    @staticmethod
    def _writable(ref: LibraryRef) -> LibraryRef:
        if ref.read_only:
            raise JobDestinationError(READ_ONLY_MESSAGE)
        return ref

    def _resolve_create_library(
        self, library_id: str | None
    ) -> tuple[LibraryRef, bool]:
        """An omitted selection may use the default; an explicit one never may."""
        if library_id:
            ref = self._lib.libraries.get(library_id)
            if ref is None:
                raise JobDestinationError(
                    "that library is not connected; plug it in or choose "
                    "another library"
                )
            return self._writable(ref), True
        return self._default_library(), False

    def create_probe(
        self,
        url: str,
        *,
        limit: int | None = None,
        preserve_audio: bool | None = None,
        library_id: str | None = None,
        upgrade_height: int = 0,
    ) -> Job:
        if upgrade_height and upgrade_height not in UPGRADE_HEIGHTS:
            raise ValueError(f"an upgrade goes to one of {UPGRADE_HEIGHTS}")
        cleaned = url.strip()
        playlist_id = ""
        try:
            normalized = normalize_playlist_url(cleaned)
            playlist_id = (
                parse_qs(urlparse(normalized).query).get("list", [""])[0]
            )
        except Exception:  # noqa: BLE001 - the probe reports the real error
            playlist_id = ""
        location, explicit = self._resolve_create_library(library_id)
        audio_policy = (
            "inherit" if preserve_audio is None
            else ("enabled" if preserve_audio else "disabled")
        )
        resolved_audio = bool(preserve_audio)
        if playlist_id:
            # An explicit library gets its own copy of the playlist; otherwise
            # an existing copy (launch library first) is synced in place.
            resolved = (
                self._lib.resolve_in(location.id, playlist_id) if explicit
                else self._lib.resolve_playlist(playlist_id)
            )
            if self._busy(
                playlist_id, resolved[0].id if resolved else location.id
            ):
                raise ValueError(
                    "finish or cancel the active job for this playlist first"
                )
            if (
                preserve_audio is None
                and resolved is not None
                and (resolved[2].get("options") or {}).get("preserve_audio")
            ):
                resolved_audio = True
            # An existing playlist never moves because the form default changed.
            if resolved is not None:
                location = self._writable(resolved[0])
        config = self._config_for_ref(location)
        job = Job(
            id=uuid.uuid4().hex[:12],
            status="probing",
            url=cleaned,
            kind="playlist",
            playlist_id=playlist_id,
            target_mb_per_hour=config.target_mb_per_hour,
            max_height=upgrade_height or config.max_height,
            upgrade_height=upgrade_height,
            preserve_audio=resolved_audio,
            audio_policy=audio_policy,
            library_id=location.id,
            library_label=location.label,
            library_explicit=explicit,
        )
        with self._lock:
            self._jobs[job.id] = job
            self._cancel[job.id] = threading.Event()
            self._save(force=True)
            self._submit(self._probe_executor, job.id, self._run_probe, job.id, limit)
        return copy.deepcopy(job)

    def create_video_probe(
        self,
        video_id: str,
        *,
        target_playlist_id: str = "",
        preserve_audio: bool | None = None,
        library_id: str | None = None,
    ) -> Job:
        """Queue one video and download it into an existing playlist.

        ``target_playlist_id`` is a playlist key; without one the video goes
        to the Un-grouped playlist of ``library_id`` (or the default library).
        """
        if target_playlist_id:
            resolved = self._lib.resolve_playlist(target_playlist_id)
            if resolved is None:
                raise ValueError("that playlist is not in this library")
            target_location = self._writable(resolved[0])
        else:
            target_location, _explicit = self._resolve_create_library(library_id)
            self._lib.ensure_ungrouped(target_location.id)
            resolved = self._lib.resolve_in(target_location.id, UNGROUPED_ID)
        target_doc: dict | None = resolved[2] if resolved else None
        target_id = str((target_doc or {}).get("id") or UNGROUPED_ID)
        if self._busy(target_id, target_location.id):
            raise ValueError(
                "finish or cancel the active job for this playlist first"
            )
        target_doc = target_doc or {
            "id": target_id,
            "title": UNGROUPED_TITLE,
            "channel": "",
            "description": "",
        }
        inherited = bool(
            (target_doc.get("options") or {}).get("preserve_audio")
        )
        audio_policy = (
            "inherit" if preserve_audio is None
            else ("enabled" if preserve_audio else "disabled")
        )
        config = self._config_for_ref(target_location)
        job = Job(
            id=uuid.uuid4().hex[:12],
            status="probing",
            url=f"https://www.youtube.com/watch?v={video_id}",
            kind="video",
            video_id=video_id,
            playlist_id=target_id,
            target_playlist_id=target_id,
            target_playlist_title=str(target_doc.get("title") or target_id),
            title="",
            target_mb_per_hour=config.target_mb_per_hour,
            max_height=config.max_height,
            preserve_audio=(True if preserve_audio else inherited),
            audio_policy=audio_policy,
            library_id=target_location.id,
            library_label=target_location.label,
            library_explicit=True,
        )
        with self._lock:
            self._jobs[job.id] = job
            self._cancel[job.id] = threading.Event()
            self._save(force=True)
            self._submit(
                self._probe_executor,
                job.id,
                self._run_video_probe,
                job.id,
                str(video_id),
                target_doc,
            )
        return copy.deepcopy(job)

    def _busy(self, playlist_id: str, library_id: str) -> bool:
        """An active job uses this playlist of this library (a move uses it
        in both its source and destination)."""
        with self._lock:
            return any(
                job.active and job.playlist_id == playlist_id
                and (
                    job.library_id in ("", library_id)
                    or (job.kind == "move" and job.source_library_id == library_id)
                )
                for job in self._jobs.values()
            )

    def _job_playlist(self, job: Job, playlist_id: str):
        """The job's playlist in its own library (a bare id can exist in
        several libraries)."""
        if not playlist_id:
            return None
        if job.library_id:
            resolved = self._lib.resolve_in(job.library_id, playlist_id)
            if resolved is not None or job.library_explicit:
                return resolved
        return self._lib.resolve_playlist(playlist_id)

    def _submit(self, executor: ThreadPoolExecutor, job_id: str, fn, *args) -> None:
        if self._closed:
            raise RuntimeError("the job manager is shutting down")
        if not self._start_workers:
            return
        future = executor.submit(fn, *args)
        with self._lock:
            self._futures[job_id] = future

    def _begin_worker(self, job_id: str) -> bool:
        with self._lock:
            if self._closed:
                return False
            job = self._jobs.get(job_id)
            if job is None or job.status in TERMINAL or job.status == PAUSED:
                return False
            self._running.add(job_id)
            return True

    def _end_worker(self, job_id: str) -> None:
        with self._lock:
            self._running.discard(job_id)
            self._futures.pop(job_id, None)
            job = self._jobs.get(job_id)
            if job is not None and job.status == "cancelling":
                job.status = "cancelled"
                job.error = "cancelled by the user"
                job.finished_at = now_iso()
                job.current_id = None
                job.current_title = None
                job.current_bytes = 0
                job.current_total = 0
                for row in job.items:
                    if row.get("status") in {"pending", "downloading"}:
                        row["status"] = "cancelled"
                job.updated_at = now_iso()
                self._save(force=True)
            else:
                self._flush_save()

    def _cancelled(self, job_id: str) -> bool:
        event = self._cancel.get(job_id)
        return bool(event and event.is_set())

    def _review_destination(self, job_id: str, playlist_id: str) -> Path | None:
        """The media folder a reviewed job would download into, if online."""
        job = self._live(job_id)
        if job is None:
            return None
        try:
            ref = (
                self._lib.libraries.get(job.library_id) if job.library_id
                else self._lib.libraries.default()
            )
        except LookupError:
            return None
        if ref is None:
            return None
        try:
            return media_destination(ref.layout, playlist_id)
        except ValueError:
            return None

    def _install_probe(self, job_id: str, probe: ProbeResult) -> None:
        with self._lock:
            self._probes[job_id] = probe

    def _run_probe(self, job_id: str, limit: int | None) -> None:
        if not self._begin_worker(job_id):
            return
        try:
            self._run_probe_work(job_id, limit)
        except JobCancelled:
            self._mark_cancelled(job_id)
        except CorruptDocumentError:
            raise
        except BaseException as exc:  # noqa: BLE001 - never leave a stuck job
            self._mark_failed(job_id, exc)
        finally:
            self._end_worker(job_id)

    def _run_video_probe(
        self, job_id: str, video_id: str, target_doc: dict
    ) -> None:
        if not self._begin_worker(job_id):
            return
        try:
            self._run_video_probe_work(job_id, video_id, target_doc)
        except JobCancelled:
            self._mark_cancelled(job_id)
        except CorruptDocumentError:
            raise
        except BaseException as exc:  # noqa: BLE001
            self._mark_failed(job_id, exc)
        finally:
            self._end_worker(job_id)

    def _run_video_probe_work(
        self, job_id: str, video_id: str, target_doc: dict
    ) -> None:
        job = self._live(job_id)
        if job is None:
            return
        if self._cancelled(job_id):
            raise JobCancelled("cancelled by the user")
        cancel = self._cancel.get(job_id, threading.Event())
        config = self._job_config(job)

        def on_start(item: ProbedItem) -> None:
            self._bump(job_id, "probe_started")
            self._update(
                job_id,
                current_id=item.id,
                current_title=item.title or item.id,
            )

        def on_item(item: ProbedItem) -> None:
            self._bump(job_id, "probe_done")
            self._update(
                job_id,
                current_id=item.id,
                current_title=item.title or item.id,
            )

        try:
            probe = probe_video(
                video_id,
                config,
                playlist=target_doc,
                on_item=on_item,
                on_item_start=on_start,
                max_height=config.max_height,
                preserve_audio=job.preserve_audio,
                cancel=cancel.is_set,
                cache_root=self._job_cache_root(job),
            )
        except JobCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced on the job page
            raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc

        if cancel.is_set():
            raise JobCancelled("cancelled by the user")
        first = probe.items[0]
        if first.error and self._fixable(first.error):
            auto = self._autoupdate
            assert auto is not None
            fixed: list[ProbeResult] = []

            def attempt(_item: object) -> bool:
                again = probe_video(
                    video_id, config, playlist=target_doc,
                    max_height=config.max_height,
                    preserve_audio=job.preserve_audio,
                    cancel=cancel.is_set,
                    cache_root=self._job_cache_root(job),
                )
                if again.items and not again.items[0].error:
                    fixed.append(again)
                    return True
                return False

            auto.fix([None], attempt, say=self._say(job_id), cancel=cancel.is_set)
            if fixed:
                probe = fixed[-1]
            if cancel.is_set():
                raise JobCancelled("cancelled by the user")

        review = _compact_probe(
            probe, config, job.preserve_audio,
            self._review_destination(
                job_id, str(probe.playlist.get("id") or "")
            ),
        )
        row = review["items"][0]
        info = probe.items[0].info or {}
        self._install_probe(job_id, probe)
        if row.get("error"):
            self._update(
                job_id,
                status="failed",
                finished_at=now_iso(),
                error=str(row["error"]),
                items=review["items"],
                probe_done=1,
                current_id=None,
                current_title=None,
            )
            return
        self._update(
            job_id,
            status="ready",
            title=row["title"],
            channel=str(info.get("channel") or ""),
            description=str(info.get("description") or ""),
            video_count=1,
            probe_done=1,
            total_duration=row["duration"],
            estimate_bytes=row["size"],
            estimate_native_bytes=row["native_size"],
            estimate_tiers=review["estimate_tiers"],
            free_bytes=review["free_bytes"],
            fat32_warning=review["fat32_warning"],
            fat32_count=review["fat32_count"],
            items=review["items"],
            in_library=True,
            current_id=None,
            current_title=None,
        )

    def _run_probe_work(self, job_id: str, limit: int | None) -> None:
        job = self._live(job_id)
        if job is None:
            return
        url = job.url

        cancel = self._cancel.get(job_id, threading.Event())
        if cancel.is_set():
            raise JobCancelled("cancelled by the user")
        config = self._job_config(job)

        def on_start(item: ProbedItem) -> None:
            self._bump(job_id, "probe_started")
            self._update(
                job_id,
                current_id=item.id,
                current_title=item.title or item.id,
            )

        def on_item(item: ProbedItem) -> None:
            self._bump(job_id, "probe_done")
            self._update(
                job_id,
                current_id=item.id,
                current_title=item.title or item.id,
            )

        def on_total(total: int) -> None:
            self._update(job_id, video_count=total)

        def run_probe() -> ProbeResult:
            # A new review may reuse recent reads (re-adding a playlist); an
            # upgrade asks YouTube what it offers now.
            reuse = contextlib.nullcontext() if job.upgrade_height else reusing_analyses()
            with reuse:
                return _probe()

        def _probe() -> ProbeResult:
            return probe_playlist(
                url, config, limit=limit,
                on_item=on_item, on_total=on_total,
                on_item_start=on_start,
                max_height=job.upgrade_height or config.max_height,
                target_mb_per_hour=config.target_mb_per_hour,
                min_height=config.min_height,
                probe_jobs=config.probe_jobs,
                cancel=cancel.is_set,
                preserve_audio=job.preserve_audio,
                cache_root=self._job_cache_root(job),
                have=self._media_lookup(job),
            )

        try:
            probe = run_probe()
        except JobCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced on the job page
            retried = (
                self._retry_playlist_probe(job_id, run_probe, cancel)
                if self._fixable(exc) else None
            )
            if retried is None:
                raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc
            probe = retried
        else:
            self._retry_probe_items(job_id, job, probe, config, cancel)

        if cancel.is_set():
            raise JobCancelled("cancelled by the user")

        resolved = self._job_playlist(job, str(probe.playlist.get("id") or ""))
        preserve = job.preserve_audio
        location = None
        stored_quality = None
        if resolved is not None:
            options = resolved[2].get("options")
            options = options if isinstance(options, dict) else {}
            preserve = preserve or bool(options.get("preserve_audio"))
            stored_quality = options.get("quality")
            location = resolved[0]
        if location is not None:
            self._update(
                job_id,
                library_id=location.id,
                library_label=location.label,
            )
        destination = self._review_destination(
            job_id, str(probe.playlist.get("id") or "")
        )
        started = time.monotonic()
        review = _compact_probe(probe, config, preserve, destination)
        timings = [*probe.timings, Phase("reviewing", time.monotonic() - started)]
        for line in summary_lines(timings):
            log.info("analyse %s: %s", job_id, line)
        suggested = _suggested_policy(stored_quality, review)
        self._install_probe(job_id, probe)
        self._update(
            job_id,
            status="ready",
            suggested_policy=suggested,
            preserve_audio=preserve,
            playlist_id=review["id"],
            title=review["title"],
            channel=review["channel"],
            description=review["description"],
            video_count=review["video_count"],
            probe_done=review["video_count"],
            total_duration=review["total_duration"],
            estimate_bytes=review["estimate_bytes"],
            estimate_native_bytes=review["estimate_native_bytes"],
            estimate_tiers=review["estimate_tiers"],
            free_bytes=review["free_bytes"],
            fat32_warning=review["fat32_warning"],
            fat32_count=review["fat32_count"],
            existing_count=review["existing_count"],
            items=review["items"],
            in_library=resolved is not None,
            current_id=None,
            current_title=None,
            timings=[phase.as_dict() for phase in timings],
            **({"notice": BOT_BLOCKED_NOTICE} if probe.bot_blocked else {}),
        )

    def _retry_playlist_probe(
        self, job_id: str, run_probe: Callable[[], ProbeResult],
        cancel: threading.Event,
    ) -> ProbeResult | None:
        """The playlist itself could not be read because YouTube changed:
        update, and read it again (every yt-dlp call of the retry uses the
        new components)."""
        auto = self._autoupdate
        assert auto is not None
        fixed: list[ProbeResult] = []

        def attempt(_item: object) -> bool:
            try:
                fixed.append(run_probe())
            except JobCancelled:
                raise
            except Exception:  # noqa: BLE001 - the first error is reported
                return False
            return True

        auto.fix([None], attempt, say=self._say(job_id), cancel=cancel.is_set)
        return fixed[-1] if fixed else None

    def _retry_probe_items(
        self, job_id: str, job: Job, probe: ProbeResult, config: Config,
        cancel: threading.Event,
    ) -> None:
        """Videos of a playlist that could not be read because YouTube
        changed: update, read each once more, and share the budget again."""
        broken = [item for item in probe.items if item.error and self._fixable(item.error)]
        if not broken or cancel.is_set():
            return
        auto = self._autoupdate
        assert auto is not None

        def attempt(item: ProbedItem) -> bool:
            again = probe_video(
                item.id, config, index=item.index,
                max_height=config.max_height,
                preserve_audio=job.preserve_audio,
                cancel=cancel.is_set,
                cache_root=self._job_cache_root(job),
            )
            fresh = again.items[0] if again.items else None
            if fresh is not None and fresh.error:
                item.error, item.error_kind = fresh.error, fresh.error_kind
                item.countries = fresh.countries
            if fresh is None or fresh.error:
                return False
            item.info, item.selection, item.error = fresh.info, fresh.selection, None
            item.proxy = fresh.proxy
            item.signed_in = fresh.signed_in
            item.title = fresh.title or item.title
            item.duration = fresh.duration or item.duration
            return True

        if any(auto.fix(broken, attempt, say=self._say(job_id), cancel=cancel.is_set)) and (
            config.target_mb_per_hour > 0
        ):
            allocate_efficient(
                probe.items,
                target_mb_per_hour=config.target_mb_per_hour,
                audio_language=config.audio_language,
                min_height=config.min_height,
                max_height=config.max_height or 0,
                preserve_audio=job.preserve_audio,
            )

    # -- move --------------------------------------------------------------
    def move_playlist(self, playlist_id: str, target_library_id: str) -> Job:
        """Queue a move of playlist ``playlist_id`` (a playlist key) to
        another library.

        Refused (``ValueError``) while the playlist has an active job, when
        the destination already has this playlist id, or when the pre-flight
        check fails (a file too big for FAT32, not enough free space)."""
        resolved = self._lib.resolve_playlist(playlist_id)
        if resolved is None:
            raise KeyError(playlist_id)
        source, meta_dir, doc = resolved
        real_id = str(doc.get("id") or "")
        with self._lock:
            if self._busy(real_id, source.id):
                raise ValueError(
                    "finish or cancel the active job for this playlist first"
                )
            job_id = uuid.uuid4().hex[:12]
            plan = fsops.plan_move(
                self._lib, resolved, target_library_id, job_id=job_id
            )
            title = self._lib.titles_for(source).read()["playlists"].get(
                real_id
            ) or str(doc.get("title") or real_id)
            job = Job(
                id=job_id,
                status="queued",
                kind="move",
                playlist_id=real_id,
                title=title,
                video_count=len(doc.get("entries") or []),
                library_id=plan.dest.id,
                library_label=plan.dest.label,
                library_explicit=True,
                source_library_id=source.id,
                source_library_label=source.label,
                move_total_bytes=plan.total_bytes,
                move_total_files=plan.total_files,
                source_path=str(meta_dir),
                target_path=str(plan.target_meta),
                temp_path=str(plan.staging),
                current_title="Waiting to move…",
            )
            self._jobs[job.id] = job
            self._cancel[job.id] = threading.Event()
            self._save(force=True)
            self._submit(self._download_executor, job.id, self._run_move, job.id)
            return copy.deepcopy(job)

    def _run_move(self, job_id: str) -> None:
        if not self._begin_worker(job_id):
            return
        try:
            self._run_move_work(job_id)
        except JobCancelled:
            self._mark_cancelled(job_id)
        except BaseException as exc:  # noqa: BLE001
            self._mark_failed(job_id, exc)
        finally:
            self._end_worker(job_id)

    def _run_move_work(self, job_id: str) -> None:
        job = self._live(job_id)
        if job is None:
            return
        cancel = self._cancel.get(job_id, threading.Event())

        def fail(message: str, *, cleanup: bool = True) -> None:
            self._update(
                job_id,
                status="failed",
                finished_at=now_iso(),
                current_title=None,
                error=message,
                source_cleanup_complete=cleanup,
            )

        if cancel.is_set():
            raise JobCancelled("cancelled by the user")
        if self._lib.resolve_in(job.source_library_id, job.playlist_id) is None:
            fail(
                f"the playlist is no longer in {job.source_library_label or 'its library'}"
            )
            return
        key = self._lib.key_for(job.source_library_id, job.playlist_id)
        self._update(
            job_id,
            status="running",
            started_at=now_iso(),
            finished_at=None,
            move_bytes=0,
            move_files=0,
            current_title="Moving playlist…",
            error=None,
            cancel_requested=bool(cancel.is_set()),
            source_cleanup_complete=True,
        )

        def progress(update: fsops.MoveProgress) -> None:
            self._update(
                job_id,
                move_phase=update.phase,
                move_bytes=update.bytes,
                move_files=update.files,
                move_total_bytes=update.total_bytes,
                move_total_files=update.total_files,
                current_title=update.label or None,
            )

        try:
            fsops.move_playlist(
                self._lib, key, job.library_id, progress,
                job_id=job.id, cancel=cancel.is_set,
            )
        except JobCancelled:
            raise
        except fsops.PublishedError as exc:
            fail(str(exc), cleanup=False)
            return
        except (ValueError, OSError, CorruptDocumentError) as exc:
            # Nothing was published: the source is untouched.
            fail(f"could not move the playlist: {exc}; it was left where it was")
            return
        self._update(
            job_id,
            status="done",
            finished_at=now_iso(),
            move_phase="done",
            source_cleanup_complete=True,
            current_title=None,
            error=None,
            in_library=True,
        )

    # -- make a stick ------------------------------------------------------
    def make_stick(self, plan: makestick.MakePlan, source_image: Path) -> Job:
        """Queue making (or upgrading) the stick ``plan`` describes.

        The job is kept with the default library (an upgrade never writes
        the target's ``data/``).  Refused (``ValueError``) while another make
        uses the same target or a chosen playlist has an active job."""
        target = str(plan.target)
        with self._lock:
            for other in self._jobs.values():
                if other.active and other.kind == "make_stick" and (
                    other.target_path == target
                ):
                    raise ValueError("a stick is already being made there")
            for key in plan.playlists:
                resolved = self._lib.resolve_playlist(key)
                if resolved is not None and self._busy(
                    str(resolved[2].get("id") or ""), resolved[0].id
                ):
                    raise ValueError(
                        "finish or cancel the active job for each chosen "
                        "playlist first"
                    )
            try:
                home: LibraryRef | None = self._lib.libraries.default()
            except LookupError:
                home = None
            job = Job(
                id=uuid.uuid4().hex[:12],
                status="queued",
                kind="make_stick",
                title=plan.label,
                video_count=len(plan.playlists),
                library_id=home.id if home is not None else "",
                library_label=home.label if home is not None else "",
                library_explicit=True,
                target_path=target,
                move_total_bytes=plan.bytes_needed,
                move_total_files=plan.total_files,
                stick_mode=plan.mode,
                stick_platforms=list(plan.platforms),
                current_title="Waiting to start…",
            )
            self._jobs[job.id] = job
            self._cancel[job.id] = threading.Event()
            self._stick_plans[job.id] = (plan, source_image)
            self._save(force=True)
            self._submit(
                self._download_executor, job.id, self._run_make_stick, job.id
            )
            return copy.deepcopy(job)

    def _run_make_stick(self, job_id: str) -> None:
        if not self._begin_worker(job_id):
            self._stick_plans.pop(job_id, None)
            return
        try:
            self._run_make_stick_work(job_id)
        except JobCancelled:
            self._mark_cancelled(job_id)
        except BaseException as exc:  # noqa: BLE001
            self._mark_failed(job_id, exc)
        finally:
            self._stick_plans.pop(job_id, None)
            self._end_worker(job_id)

    def _run_make_stick_work(self, job_id: str) -> None:
        cancel = self._cancel.get(job_id, threading.Event())
        planned = self._stick_plans.get(job_id)
        if planned is None:
            raise RuntimeError("the stick plan was lost; start again")
        plan, source = planned
        if cancel.is_set():
            raise JobCancelled("cancelled by the user")
        self._update(
            job_id,
            status="running",
            started_at=now_iso(),
            finished_at=None,
            move_bytes=0,
            move_files=0,
            current_title="Starting…",
            error=None,
        )

        def progress(update: makestick.MakeProgress) -> None:
            self._update(
                job_id,
                move_phase=update.phase,
                move_bytes=update.bytes,
                move_files=update.files,
                move_total_bytes=update.total_bytes,
                move_total_files=update.total_files,
                current_title=update.label or None,
            )

        before = makestick.drive_id(plan.target)
        try:
            report = makestick.run_make(
                plan, source, self._lib, progress, cancel=cancel.is_set
            )
            for warning in report.warnings:
                log.warning("make stick %s: %s", plan.target, warning)
            for line in summary_lines(report.timings):
                log.info("make stick %s: %s", plan.target, line)
        except JobCancelled:
            raise
        except (ValueError, OSError, CorruptDocumentError) as exc:
            message = (
                makestick.drive_problem(plan.target, exc, before)
                if isinstance(exc, OSError) else str(exc)
            )
            self._update(
                job_id,
                status="failed",
                finished_at=now_iso(),
                current_title=None,
                error=message,
            )
            return
        self._update(
            job_id,
            status="done",
            finished_at=now_iso(),
            move_phase="done",
            current_title=None,
            error=None,
            # e.g. the YouTube parts were not copied: the stick still works.
            notice=" ".join(report.warnings),
            in_library=plan.mode == "new",
            timings=[phase.as_dict() for phase in report.timings],
        )

    # -- component updates -------------------------------------------------
    @property
    def autoupdate(self) -> AutoUpdate | None:
        return self._autoupdate

    def _say(self, job_id: str) -> Callable[[str], None]:
        return lambda message: self._update(job_id, notice=message)

    def _fixable(self, error: object) -> bool:
        return (
            self._autoupdate is not None and self._autoupdate.available
            and classify_failure(error if isinstance(error, BaseException) else str(error or ""))
            == "youtube_changed"
        )

    def start_update(self) -> Job:
        """Update now: a background job that runs the component updater on
        the launch stick's store, listed on the Downloads page."""
        auto = self._autoupdate
        if auto is None or not auto.available:
            raise ValueError("there is no component store to update here")
        with self._lock:
            if any(job.active and job.kind == "update" for job in self._jobs.values()):
                raise ValueError("an update is already running")
            try:
                ref: LibraryRef | None = self._lib.libraries.default()
            except LookupError:
                ref = None
            job = Job(
                id=uuid.uuid4().hex[:12],
                status="running",
                kind="update",
                title=UPDATE_TITLE,
                library_id=ref.id if ref is not None and not ref.read_only else "",
                library_label=ref.label if ref is not None else "",
                started_at=now_iso(),
                current_title="Checking for new versions…",
            )
            self._jobs[job.id] = job
            self._cancel[job.id] = threading.Event()
            self._save(force=True)
            self._submit(self._update_executor, job.id, self._run_update, job.id)
            return copy.deepcopy(job)

    def _run_update(self, job_id: str) -> None:
        if not self._begin_worker(job_id):
            return
        try:
            auto = self._autoupdate
            assert auto is not None
            result = auto.update(
                progress=lambda label: self._update(job_id, current_title=label)
            )
            done = {"current_title": None, "finished_at": now_iso()}
            if result.status == "updated":
                names = ", ".join(f"{name} {new}" for name, (_old, new) in result.updated.items())
                self._update(job_id, status="done", notice=f"Updated {names}.",
                             error=("; ".join(f"{n}: {w}" for n, w in result.failed.items())
                                    or None), **done)
            elif result.status == "up_to_date":
                self._update(job_id, status="done", notice="Already up to date.", **done)
            else:
                why = {
                    "offline": "No network; couldn't check for updates.",
                    "rate_limited": "The update server is busy; try again later.",
                }.get(result.status, result.message or result.status)
                self._update(job_id, status="failed", error=why, **done)
        except BaseException as exc:  # noqa: BLE001 - never leave a stuck job
            self._mark_failed(job_id, exc)
        finally:
            self._end_worker(job_id)

    # -- download ----------------------------------------------------------
    def start_download(
        self,
        job_id: str,
        policy: str = "efficient",
        *,
        preserve_audio: bool | None = None,
        refresh_existing: bool = False,
    ) -> Job:
        # List first, outside the job lock: a re-plugged library whose
        # recovery was deferred (it cannot wait for PUBLISH_LOCK under the
        # job lock) is recovered here instead of looking unplugged below.
        try:
            self._lib.libraries.libraries()
        except Exception:  # noqa: BLE001 - discovery must never break this
            log.exception("library discovery failed")
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job.status not in {"ready", "failed", "cancelled", "done", PAUSED}:
                raise ValueError("this job is not ready to download")
            if (
                job.status == PAUSED and job.library_id
                and not self._library_online(job.library_id)
            ):
                raise ValueError(
                    f"{job.library_label or 'its library'} is still not "
                    "connected; plug it in, then resume"
                )
            if not job.items:
                raise ValueError("this job has no probe data; re-add the playlist")
            if job.status == PAUSED and (
                job.paused_from == "ready"
                # Paused before this was recorded, and never started.
                or (not job.paused_from and job.started_at is None)
            ):
                # A review paused before the user chose to download: show
                # the review again rather than downloading unconfirmed.
                job.status = "ready"
                job.error = None
                job.paused_from = ""
                job.updated_at = now_iso()
                self._save(force=True)
                return copy.deepcopy(job)
            # Validates the choice before anything about the job changes.
            policy_settings(policy, Config())
            # A target that was deleted must not be silently recreated.
            if (
                job.kind != "video"
                and job.in_library
                and job.playlist_id
                and self._job_playlist(job, job.playlist_id) is None
            ):
                raise ValueError(
                    "the downloaded playlist was removed from the library; "
                    "add it again instead of resuming this job"
                )
            if (
                job.kind == "video"
                and job.target_playlist_id
                and job.target_playlist_id != UNGROUPED_ID
                and self._job_playlist(job, job.target_playlist_id) is None
            ):
                raise ValueError(
                    "the destination playlist was removed; add the video again"
                )
            if preserve_audio is not None:
                job.preserve_audio = bool(preserve_audio)
                job.audio_policy = (
                    "enabled" if preserve_audio else "disabled"
                )
            elif job.audio_policy == "inherit":
                resolved = self._job_playlist(job, job.playlist_id)
                if resolved is not None:
                    job.preserve_audio = bool(
                        (resolved[2].get("options") or {}).get("preserve_audio")
                    )
            config = self._job_config(job)
            if job.upgrade_height:  # the height the upgrade was started for
                policy, refresh_existing = f"{job.upgrade_height}p", False
            target_mb, max_height = policy_settings(policy, config)
            # "Sync again" and retries fetch fresh remote metadata; a review
            # just made ("ready") is used as it is, never read twice.
            if job.status in {"done", "failed", "cancelled", PAUSED}:
                self._probes.pop(job_id, None)
            self._cancel[job_id] = threading.Event()
            for row in job.items:
                row["status"] = "pending"
                row["error"] = None
            self._base_bytes[job_id] = 0
            job.status = "queued"
            job.paused_from = ""
            job.policy = policy
            job.refresh_existing = bool(refresh_existing)
            job.target_mb_per_hour = target_mb
            job.max_height = max_height
            job.error = None
            job.notice = ""
            job.finished_at = None
            job.cancel_requested = False
            job.downloaded = 0
            job.skipped = 0
            job.failed = 0
            job.bytes_downloaded = 0
            job.current_id = None
            job.current_title = None
            job.current_bytes = 0
            job.current_total = 0
            job.updated_at = now_iso()
            self._save(force=True)
            self._submit(
                self._download_executor, job_id, self._run_download, job_id
            )
            return copy.deepcopy(job)

    def cancel(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if not job.active:
                raise ValueError("this job is not running")
            if job.kind == "update":
                raise ValueError("an update cannot be stopped once it starts")
            event = self._cancel.get(job_id)
            if event is not None:
                event.set()
            running = job_id in self._running
            future = self._futures.get(job_id)
            if not running:
                # A job still waiting in the executor queue never starts
                # network work; it can become terminal immediately.
                if future is not None:
                    future.cancel()
                self._mark_cancelled(job_id)
                return copy.deepcopy(self._jobs[job_id])
            job.status = "cancelling"
            job.cancel_requested = True
            job.updated_at = now_iso()
            self._save(force=True)
            return copy.deepcopy(job)

    def remove(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job.active:
                raise ValueError("cancel the job before clearing it")
            self._jobs.pop(job_id, None)
            self._probes.pop(job_id, None)
            self._cancel.pop(job_id, None)
            self._base_bytes.pop(job_id, None)
            self._futures.pop(job_id, None)
            self._save(force=True)

    def close(self, only_if_idle: bool = False) -> bool:
        """Signal every worker, join the executors, and flush job state.

        With ``only_if_idle`` nothing happens (``False``) while a job is
        active; the check and the close are one step under the job lock, so
        no job can start in between.
        """
        with self._lock:
            if self._closed:
                return True
            if only_if_idle and any(job.active for job in self._jobs.values()):
                return False
            self._closed = True
            for job_id, event in self._cancel.items():
                event.set()
                job = self._jobs.get(job_id)
                if job is not None and job.active:
                    job.status = "cancelling"
                    job.cancel_requested = True
            for future in self._futures.values():
                future.cancel()
        self._probe_executor.shutdown(wait=True, cancel_futures=True)
        self._download_executor.shutdown(wait=True, cancel_futures=True)
        # An update finishes (its switch is one atomic write either way).
        self._update_executor.shutdown(wait=True, cancel_futures=True)
        with self._lock:
            for job in self._jobs.values():
                if job.status == "cancelling":
                    job.status = "cancelled"
                    job.error = "cancelled by the server shutting down"
                    job.finished_at = now_iso()
            self._save(force=True)
        return True

    def _run_download(self, job_id: str) -> None:
        if not self._begin_worker(job_id):
            return
        try:
            self._run_download_work(job_id)
        except JobCancelled:
            self._mark_cancelled(job_id)
        except CorruptDocumentError:
            raise
        except BaseException as exc:  # noqa: BLE001
            if self._unplugged(job_id):
                self._pause(job_id)
            else:
                self._mark_failed(job_id, exc)
        finally:
            self._end_worker(job_id)

    def _unplugged(self, job_id: str) -> bool:
        job = self._live(job_id)
        return bool(
            job is not None and job.library_id
            and not self._library_online(job.library_id)
        )

    def _fail_download(self, job_id: str, error: str) -> None:
        """A download that broke because its library was unplugged pauses;
        anything else fails."""
        if self._unplugged(job_id):
            self._pause(job_id)
            return
        self._update(
            job_id, status="failed", finished_at=now_iso(), error=error,
        )

    def _downloading_elsewhere(self, job_id: str) -> set[str]:
        """Video ids another job is downloading right now.  Partial
        downloads are keyed by video id alone, so two jobs must never fetch
        the same video at once (even into different libraries)."""
        with self._lock:
            return {
                str(row.get("id"))
                for other in self._jobs.values()
                if other.id != job_id and other.kind != "move"
                and other.status in {"running", "cancelling"}
                for row in other.items
                if row.get("status") in {"pending", "downloading"}
            }

    def _skip_busy_items(self, job_id: str, probe: ProbeResult) -> ProbeResult:
        busy = self._downloading_elsewhere(job_id)
        held = {item.id for item in probe.items if item.id in busy}
        if not held:
            return probe
        with self._lock:
            current = self._jobs.get(job_id)
            if current is not None:
                for row in current.items:
                    if row.get("id") in held:
                        row["status"] = "skipped"
                        row["error"] = ALREADY_DOWNLOADING
                current.skipped += len(held)
                current.updated_at = now_iso()
                self._save(force=False)
        return ProbeResult(
            url=probe.url,
            playlist=probe.playlist,
            items=[item for item in probe.items if item.id not in held],
        )

    def _download_destination(self, job: Job) -> LibraryRef:
        """Revalidate the destination immediately before writing bytes."""
        if job.library_id:
            ref = self._lib.libraries.get(job.library_id)
            if ref is None:
                raise LibraryOfflineError(
                    f"{job.library_label or 'the library'} is not connected; "
                    "plug the drive in and resume this download"
                )
            return self._writable(ref)
        if job.kind != "video" and job.playlist_id:
            current = self._lib.library_for_playlist(job.playlist_id)
            if current is not None:
                self._update(
                    job.id, library_id=current.id, library_label=current.label
                )
                return self._writable(current)
        return self._default_library()

    def _probe_for_download(
        self, job: Job, cancel: threading.Event, config: Config
    ) -> ProbeResult:
        probe = self._probes.get(job.id)
        if probe is not None:
            return probe
        probe: ProbeResult | None = None
        last: Exception | None = None
        attempts = 2 if job.kind != "video" else 1
        for attempt in range(attempts):
            if cancel.is_set():
                raise JobCancelled("cancelled by the user")
            try:
                if job.kind == "video":
                    probe = probe_video(
                        job.video_id, config,
                        playlist=self._target_doc(job),
                        max_height=job.max_height,
                        preserve_audio=job.preserve_audio,
                        cancel=cancel.is_set,
                        cache_root=self._job_cache_root(job),
                    )
                else:
                    probe = probe_playlist(
                        job.url, config,
                        max_height=job.max_height,
                        target_mb_per_hour=job.target_mb_per_hour,
                        min_height=config.min_height,
                        probe_jobs=config.probe_jobs,
                        cancel=cancel.is_set,
                        preserve_audio=job.preserve_audio,
                        cache_root=self._job_cache_root(job),
                        have=self._media_lookup(job),
                    )
                break
            except JobCancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - retried once
                last = exc
                if attempt + 1 < attempts:
                    time.sleep(1.0)
        if probe is None:
            raise last or RuntimeError("the playlist could not be probed again")
        self._install_probe(job.id, probe)
        return probe

    def _run_download_work(self, job_id: str) -> None:
        job = self._live(job_id)
        if job is None:
            return
        self._base_bytes[job_id] = 0
        cancel = self._cancel.get(job_id, threading.Event())
        if cancel.is_set():
            raise JobCancelled("cancelled by the user")
        try:
            location = self._download_destination(job)
        except LibraryOfflineError as exc:
            self._pause(job_id, str(exc))
            return
        except JobDestinationError as exc:
            self._update(
                job_id,
                status="failed",
                finished_at=now_iso(),
                error=str(exc),
            )
            return
        config = self._config_for_ref(location)
        self._update(
            job_id,
            status="running",
            started_at=now_iso(),
            downloaded=0,
            skipped=0,
            failed=0,
            bytes_downloaded=0,
            current_id=None,
            current_title="Reading playlist…",
            current_bytes=0,
            current_total=0,
            library_id=location.id,
            library_label=location.label,
        )

        try:
            probe = self._probe_for_download(job, cancel, config)
        except JobCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            self._fail_download(job_id, f"{type(exc).__name__}: {exc}")
            return
        if cancel.is_set():
            raise JobCancelled("cancelled by the user")
        probe = self._skip_busy_items(job_id, probe)

        def on_start(item: ProbedItem) -> None:
            with self._lock:
                current = self._jobs.get(job_id)
                if current is not None:
                    row = next(
                        (r for r in current.items if r.get("id") == item.id),
                        None,
                    )
                    if row is not None and row.get("status") == "pending":
                        row["status"] = "downloading"
            self._update(
                job_id,
                current_id=item.id,
                current_title=item.title or item.id,
                current_bytes=0,
                current_total=(item.selection.estimated_bytes
                               if item.selection else 0),
            )

        last_progress = {"at": 0.0}

        def on_progress(item: ProbedItem, done: int, total: int) -> None:
            now = time.monotonic()
            if now - last_progress["at"] < 0.75:
                return
            last_progress["at"] = now
            base = self._base_bytes.get(job_id, 0)
            self._update(
                job_id,
                current_id=item.id,
                current_title=item.title or item.id,
                current_bytes=int(done),
                current_total=int(total),
                bytes_downloaded=base + int(done),
            )

        broken: list[str] = []
        timed_out: list[str] = []

        def on_done(result: ItemResult) -> None:
            # A flaky proxy or sign-in never triggers a component update.
            via_proxy = any(
                i.proxy or i.signed_in for i in probe.items if i.id == result.id
            )
            if result.status == "failed" and not via_proxy and (
                result.error_kind == "youtube_changed" or self._fixable(result.error)
            ):
                broken.append(result.id)
            elif result.status == "failed" and _timed_out(result.error):
                timed_out.append(result.id)
            with self._lock:
                base = self._base_bytes.get(job_id, 0) + int(result.size or 0)
                self._base_bytes[job_id] = base
                current = self._jobs.get(job_id)
                if current is None:
                    return
                row = next(
                    (r for r in current.items if r.get("id") == result.id),
                    None,
                )
                if row is not None:
                    row["status"] = result.status
                    row["size"] = int(result.size or 0)
                    row["error"] = result.error
                    row["error_kind"] = result.error_kind
                if result.status == "downloaded":
                    current.downloaded += 1
                elif result.status == "skipped":
                    current.skipped += 1
                else:
                    current.failed += 1
                current.bytes_downloaded = base
                current.current_id = None
                current.current_title = None
                current.current_bytes = 0
                current.current_total = 0
                current.updated_at = now_iso()
                self._save(force=False)

        try:
            fetch = download_probe(
                probe, config,
                jobs=config.jobs,
                quiet=True,
                max_height=job.max_height,
                target_mb_per_hour=job.target_mb_per_hour,
                min_height=config.min_height,
                fetch_transcripts=True,
                preserve_audio=job.preserve_audio,
                refresh_existing=job.refresh_existing,
                upgrade=bool(job.upgrade_height),
                # A single video added to a playlist leaves its quality alone.
                quality=None if job.kind == "video" else job.policy,
                library_root=location.root,
                cancel=cancel.is_set,
                on_item_start=on_start,
                on_item_done=on_done,
                on_progress=on_progress,
            )
        except JobCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            self._fail_download(job_id, f"{type(exc).__name__}: {exc}")
            return

        if cancel.is_set():
            raise JobCancelled("cancelled by the user")
        if self._unplugged(job_id):
            # Items failed because the stick was pulled mid-download.
            self._pause(job_id)
            return
        retried_bytes = 0
        if broken and self._autoupdate is not None and self._autoupdate.available:
            retried_bytes = self._retry_downloads(
                job_id, job, probe, broken, config, location, cancel,
                on_start, on_progress,
            )
            if cancel.is_set():
                raise JobCancelled("cancelled by the user")
            if self._unplugged(job_id):
                self._pause(job_id)
                return
        if timed_out:
            retried_bytes += self._retry_timed_out(
                job_id, job, probe, timed_out, config, location, cancel,
                on_start, on_progress,
            )
            if cancel.is_set():
                raise JobCancelled("cancelled by the user")
            if self._unplugged(job_id):
                self._pause(job_id)
                return

        playlist = fetch.playlist or {}
        if job.kind == "video":
            done_fields = {
                "playlist_id": job.target_playlist_id or job.playlist_id,
                "title": job.title,
                "video_count": 1,
            }
        else:
            done_fields = {
                "playlist_id": str(playlist.get("id") or job.playlist_id),
                "title": str(playlist.get("title") or job.title),
                "video_count": len(playlist.get("entries") or [])
                or job.video_count,
            }
        self._update(
            job_id,
            status="done",
            finished_at=now_iso(),
            estimate_bytes=job.estimate_bytes,
            bytes_downloaded=fetch.total_bytes + retried_bytes,
            in_library=True,
            current_id=None,
            current_title=None,
            error=None,
            cancel_requested=False,
            **done_fields,
        )

    def _retry_downloads(
        self, job_id: str, job: Job, probe: ProbeResult, broken: list[str],
        config: Config, location: LibraryRef, cancel: threading.Event,
        on_start: Callable[[ProbedItem], None],
        on_progress: Callable[[ProbedItem, int, int], None],
    ) -> int:
        """Videos that failed because YouTube changed: update, then read and
        download each once more with the new components.  Returns the bytes
        the retries added."""
        auto = self._autoupdate
        assert auto is not None
        by_id = {item.id: item for item in probe.items}
        items = [by_id[video_id] for video_id in dict.fromkeys(broken) if video_id in by_id]
        added = [0]

        def attempt(item: ProbedItem) -> bool:
            return self._retry_item(job_id, job, probe, item, config, location,
                                    cancel, on_start, on_progress, added)

        auto.fix(items, attempt, say=self._say(job_id), cancel=cancel.is_set)
        return added[0]

    def _retry_timed_out(
        self, job_id: str, job: Job, probe: ProbeResult, timed_out: list[str],
        config: Config, location: LibraryRef, cancel: threading.Event,
        on_start: Callable[[ProbedItem], None],
        on_progress: Callable[[ProbedItem, int, int], None],
    ) -> int:
        """Videos that failed because a helper ran out of time (the PO-token
        script's Deno on a busy stick): read and download each once more,
        with the same components.  Returns the bytes the retries added."""
        by_id = {item.id: item for item in probe.items}
        added = [0]
        for video_id in dict.fromkeys(timed_out):
            if cancel.is_set():
                break
            if video_id not in by_id:
                continue
            try:
                self._retry_item(job_id, job, probe, by_id[video_id], config,
                                 location, cancel, on_start, on_progress, added)
            except JobCancelled:
                raise
            except Exception:  # noqa: BLE001 - the video just stays failed
                log.exception("retrying %s failed", video_id)
        return added[0]

    def _retry_item(
        self, job_id: str, job: Job, probe: ProbeResult, item: ProbedItem,
        config: Config, location: LibraryRef, cancel: threading.Event,
        on_start: Callable[[ProbedItem], None],
        on_progress: Callable[[ProbedItem, int, int], None],
        added: list[int],
    ) -> bool:
        """Read and download one failed video again (with whatever yt-dlp
        the active runner holds); records it and adds its bytes to
        ``added[0]`` when it works."""
        again = probe_video(
            item.id, config, playlist=probe.playlist, index=item.index,
            max_height=job.max_height,
            preserve_audio=job.preserve_audio,
            cancel=cancel.is_set,
            cache_root=location.root,
        )
        fresh = again.items[0] if again.items else None
        if fresh is None or fresh.error or fresh.selection is None:
            return False
        results: list[ItemResult] = []
        download_probe(
            ProbeResult(url=probe.url, playlist=probe.playlist, items=[fresh]),
            config,
            jobs=1,
            quiet=True,
            # The job's quality policy, as the main pass applies it.
            max_height=job.max_height,
            target_mb_per_hour=job.target_mb_per_hour,
            min_height=config.min_height,
            fetch_transcripts=True,
            preserve_audio=job.preserve_audio,
            library_root=location.root,
            cancel=cancel.is_set,
            on_item_start=on_start,
            on_item_done=results.append,
            on_progress=on_progress,
        )
        result = results[-1] if results else None
        if result is None or result.status == "failed":
            return False
        added[0] += int(result.size or 0)
        self._record_retry(job_id, result)
        return True

    def _record_retry(self, job_id: str, result: ItemResult) -> None:
        """A failed video that worked on its retry."""
        with self._lock:
            base = self._base_bytes.get(job_id, 0) + int(result.size or 0)
            self._base_bytes[job_id] = base
            current = self._jobs.get(job_id)
            if current is None:
                return
            row = next((r for r in current.items if r.get("id") == result.id), None)
            if row is not None:
                row["status"] = result.status
                row["size"] = int(result.size or 0)
                row["error"] = None
                row["error_kind"] = None
            current.failed = max(0, current.failed - 1)
            if result.status == "downloaded":
                current.downloaded += 1
            else:
                current.skipped += 1
            current.bytes_downloaded = base
            current.updated_at = now_iso()
            self._save(force=False)

    def _target_doc(self, job: Job) -> dict:
        """Reload the destination playlist for a single-video job."""
        target_id = job.target_playlist_id or UNGROUPED_ID
        resolved = self._job_playlist(job, target_id)
        if resolved is not None:
            return resolved[2]
        if target_id == UNGROUPED_ID:
            self._lib.ensure_ungrouped(job.library_id or None)
            resolved = self._job_playlist(job, UNGROUPED_ID)
            if resolved is not None:
                return resolved[2]
        return {
            "id": target_id,
            "title": job.target_playlist_title or target_id,
            "channel": "",
            "description": "",
            "entries": [],
            "options": {"preserve_audio": job.preserve_audio},
        }

    def _mark_cancelled(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            if job.status in {"done", "ready"}:
                return
            for row in job.items:
                if row.get("status") in {"pending", "downloading"}:
                    row["status"] = "cancelled"
            job.status = "cancelled"
            job.error = "cancelled by the user"
            job.finished_at = now_iso()
            job.current_id = None
            job.current_title = None
            job.current_bytes = 0
            job.current_total = 0
            job.updated_at = now_iso()
            self._save(force=True)

    def _mark_failed(self, job_id: str, exc: BaseException) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            job.finished_at = now_iso()
            job.cancel_requested = False
            job.current_id = None
            job.current_title = None
            job.current_bytes = 0
            job.current_total = 0
            job.updated_at = now_iso()
            self._save(force=True)
