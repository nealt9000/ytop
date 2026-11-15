"""Filesystem orchestration for moving playlists between libraries.

A playlist is a metadata folder (``.ytop/data/playlists/<id>/``) plus a media
folder (``YTOP Library/<folder>/``); a move carries both, the media it only
*references* (copied into its new folder: references never cross libraries)
and its watch state.

A move never deletes the only known complete copy.  Across devices everything
is copied into a job-specific hidden staging folder on the destination,
verified file by file (sizes), published with renames, and only then is the
source removed, following the ownership rules of ``Library.remove_playlist``.
On one device the media folder and the metadata folder are renamed instead,
and only the referenced media is copied.  The caller records each phase
(``Job.move_phase``) so a restart can report the real state instead of
guessing.

This module deliberately knows nothing about jobs or HTTP; ``JobManager`` keeps
the public facade.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .. import fsinfo
from ..fsnames import safe_name
from ..layout import MEDIA_DIR_NAME, UNGROUPED_FOLDER, UNGROUPED_ID, LibraryLayout
from ..libraries import READ_ONLY_MESSAGE, LibraryRef
from ..publish import PUBLISH_LOCK, PUBLISH_PREFIX, sync_playlist_files
from ..transcripts.store import index_playlist_dir
from ..transcripts.store import remove_playlist as remove_playlist_index
from ..yt.metadata import fsync_dir, human_bytes, read_json, write_json
from ..yt.probe import JobCancelled
from .library import Library, Record, _audio_home, _media_ref, _reference_map

log = logging.getLogger(__name__)

STAGING_PREFIX = ".ytopmove-"
CHUNK_SIZE = 4 * 1024 * 1024
PROGRESS_INTERVAL_S = 0.5


class MoveError(ValueError):
    """A planned move cannot be performed safely."""


class PublishedError(MoveError):
    """The playlist is already on the destination, but a later step failed;
    the source must not be reported as untouched."""


class SourceCleanupError(PublishedError):
    """The playlist is published on the destination, but the original could
    not be removed: both copies exist."""


def canonical(path: Path) -> Path:
    """Resolve symlinks so containment checks compare real locations."""
    return Path(path).expanduser().resolve()


def is_within(path: Path, root: Path) -> bool:
    try:
        canonical(path).relative_to(canonical(root))
        return True
    except (OSError, ValueError):
        return False


def _same_device(first: Path, second: Path) -> bool:
    try:
        return os.stat(first).st_dev == os.stat(second).st_dev
    except OSError:
        return False


def free_bytes(path: Path) -> int:
    """Free space at ``path``; 0 when unknown."""
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


# --- copying -----------------------------------------------------------------


def copy_file(
    src: Path,
    dst: Path,
    *,
    on_chunk: Callable[[int], None],
    cancel: Callable[[], bool] | None = None,
) -> None:
    """Copy one file in chunks, reporting progress and honouring cancel."""
    with open(src, "rb") as reader, open(dst, "wb") as writer:
        while True:
            if cancel is not None and cancel():
                raise JobCancelled("cancelled by the user")
            chunk = reader.read(CHUNK_SIZE)
            if not chunk:
                break
            writer.write(chunk)
            on_chunk(len(chunk))
    try:
        shutil.copystat(src, dst)
    except OSError:
        pass


def copy_tree(
    source: Path,
    destination: Path,
    *,
    on_chunk: Callable[[int], None],
    on_file: Callable[[int], None],
    cancel: Callable[[], bool] | None = None,
) -> None:
    """Copy a folder file by file, reporting progress and honouring cancel."""
    for root, dirs, names in os.walk(source, followlinks=False):
        dirs[:] = [name for name in dirs if not name.startswith(".")]
        relative = Path(root).relative_to(source)
        target_dir = destination / relative
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in names:
            if cancel is not None and cancel():
                raise JobCancelled("cancelled by the user")
            copy_file(
                Path(root) / name, target_dir / name,
                on_chunk=on_chunk, cancel=cancel,
            )
            on_file(1)


def staging_is_ours(staging: Path, target_playlists: Path) -> bool:
    return is_within(staging, target_playlists) and staging.name.startswith(".")


def remove_staging(staging: Path, target_playlists: Path) -> None:
    if not staging_is_ours(staging, target_playlists):
        return
    shutil.rmtree(staging, ignore_errors=True)


# --- planning ----------------------------------------------------------------


@dataclass(frozen=True)
class MoveFile:
    source: Path
    target: str  # relative to the staging folder: ``meta/…`` or ``media/…``
    size: int


@dataclass
class MovePlan:
    source: LibraryRef
    dest: LibraryRef
    playlist_id: str
    meta_dir: Path
    target_meta: Path
    staging: Path
    old_folder: str | None
    dest_folder: str
    same_device: bool
    # Every file the destination receives (the FAT32 check covers them all).
    files: list[MoveFile]
    # The files actually copied (all of them across devices).
    copies: list[MoveFile]
    # A referenced media ref -> its staged name in the new folder (None when
    # the file is missing: the moved entry then has no media).
    refs: dict[str, str | None] = field(default_factory=dict)
    video_ids: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(item.size for item in self.copies)

    @property
    def total_files(self) -> int:
        return len(self.copies)

    @property
    def dest_media(self) -> Path:
        return self.dest.layout.media_root / self.dest_folder

    def spec(self) -> FinishSpec:
        return FinishSpec(
            mode="rename" if self.same_device else "copy",
            playlist_id=self.playlist_id,
            source_root=self.source.layout.root,
            dest_root=self.dest.layout.root,
            staging=self.staging,
            target_meta=self.target_meta,
            old_folder=self.old_folder,
            dest_folder=self.dest_folder,
            refs=dict(self.refs),
        )


@dataclass(frozen=True)
class FinishSpec:
    """What placing a published move needs; journalled beside the staging
    folder (folders relative to each media root) so a restart can repair an
    interrupted move."""

    mode: str  # rename (same device) | copy
    playlist_id: str
    source_root: Path
    dest_root: Path
    staging: Path
    target_meta: Path
    old_folder: str | None
    dest_folder: str
    refs: dict[str, str | None]

    @property
    def dest_layout(self) -> LibraryLayout:
        return LibraryLayout(self.dest_root)

    @property
    def dest_media(self) -> Path:
        return self.dest_root / MEDIA_DIR_NAME / self.dest_folder

    @property
    def source_media(self) -> Path | None:
        if not self.old_folder:
            return None
        return self.source_root / MEDIA_DIR_NAME / self.old_folder


def _synced(*dirs: Path) -> None:
    """fsync the directories a rename touched, so it survives an unplug."""
    for directory in dict.fromkeys(dirs):
        fsync_dir(directory)


def journal_path(staging: Path) -> Path:
    return staging.with_name(staging.name + ".json")


def _write_journal(spec: FinishSpec) -> None:
    write_json(journal_path(spec.staging), {
        "schema": 1,
        "mode": spec.mode,
        "playlist_id": spec.playlist_id,
        "old_folder": spec.old_folder,
        "dest_folder": spec.dest_folder,
        "refs": spec.refs,
    })


def _read_journal(
    source_meta: Path, target_meta: Path, staging: Path
) -> FinishSpec | None:
    """The journalled spec; roots come from the meta paths
    (``<root>/.ytop/data/playlists/<id>``), so a remounted stick still
    recovers."""
    doc = read_json(journal_path(staging))
    if not isinstance(doc, dict) or doc.get("mode") not in {"rename", "copy"}:
        return None
    try:
        return FinishSpec(
            mode=str(doc["mode"]),
            playlist_id=str(doc["playlist_id"]),
            source_root=source_meta.parents[3],
            dest_root=target_meta.parents[3],
            staging=staging,
            target_meta=target_meta,
            old_folder=str(doc["old_folder"]) if doc.get("old_folder") else None,
            dest_folder=str(doc["dest_folder"]),
            refs={
                str(key): (str(value) if value else None)
                for key, value in (doc.get("refs") or {}).items()
            },
        )
    except (KeyError, IndexError, TypeError):
        return None


def _drop_staging(staging: Path, meta_root: Path) -> None:
    """Remove a move's staging folder and its journal."""
    if not staging_is_ours(staging, meta_root):
        return
    shutil.rmtree(staging, ignore_errors=True)
    journal_path(staging).unlink(missing_ok=True)


def _regular_size(path: Path) -> int | None:
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    return info.st_size if stat.S_ISREG(info.st_mode) else None


def _tree(root: Path, prefix: str) -> list[MoveFile]:
    rows: list[MoveFile] = []
    for current, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if not name.startswith("."))
        for name in sorted(names):
            path = Path(current) / name
            size = _regular_size(path)
            if size is not None:
                relative = path.relative_to(root).as_posix()
                rows.append(MoveFile(path, f"{prefix}/{relative}", size))
    return rows


def _srt_files(media: Path) -> list[tuple[Path, str]]:
    """``(path, suffix)`` of a media file's ``<stem>.<lang>.srt`` siblings."""
    prefix = media.stem + "."
    found = []
    try:
        siblings = sorted(media.parent.iterdir())
    except OSError:
        return []
    for path in siblings:
        name = path.name
        if path.is_file() and name.startswith(prefix) and name.endswith(".srt"):
            lang = name[len(prefix):-4]
            if lang and "." not in lang:
                found.append((path, name[len(media.stem):]))
    return found


def _dest_folder(dest: LibraryRef, playlist_id: str, wanted: str) -> str:
    """``wanted`` if the destination does not use it yet (nor is it the
    reserved Un-grouped folder or an existing name on disk), else ``wanted
    [<id>]``."""
    layout = dest.layout
    used: set[str] = set()
    if playlist_id != UNGROUPED_ID:
        used.add(UNGROUPED_FOLDER.casefold())
    try:
        candidates = list(layout.meta_root.iterdir())
    except OSError:
        candidates = []
    for path in candidates:
        if path.name.startswith("."):
            continue
        doc = read_json(path / "playlist.json") or {}
        if doc.get("folder"):
            used.add(str(doc["folder"]).casefold())
    suffix = f" [{playlist_id}]"
    for name in (
        wanted,
        safe_name(wanted, limit=max(1, 100 - len(suffix))) + suffix,
    ):
        if name.casefold() not in used and not os.path.lexists(
            layout.media_root / name
        ):
            return name
    raise MoveError(
        f"{dest.label} already has a folder named “{wanted}”; rename one of "
        "the playlists first"
    )


def _video_metas(meta_dir: Path) -> list[tuple[Path, dict]]:
    videos = meta_dir / "videos"
    try:
        dirs = sorted(videos.iterdir())
    except OSError:
        return []
    rows = []
    for video_dir in dirs:
        meta = read_json(video_dir / "meta.json")
        if meta is not None:
            rows.append((video_dir, meta))
    return rows


# (meta files, owned media files, referenced extras, refs, video ids)
_Collected = tuple[
    list[MoveFile], list[MoveFile], list[MoveFile], dict[str, str | None], list[str]
]


def _collect_files(
    layout: LibraryLayout,
    meta_dir: Path,
    playlist_id: str,
    old_folder: str | None,
    same: bool,
) -> _Collected:
    """``(meta files, owned media files, referenced extras, refs, video ids)``
    of one playlist leaving ``layout``."""
    references = _reference_map(layout)
    owned_dir = layout.media_root / old_folder if old_folder else None
    meta_files = _tree(meta_dir, "meta")
    owned_files = _tree(owned_dir, "media") if owned_dir is not None else []
    extra: list[MoveFile] = []  # referenced media (+ srt, audio) to copy
    refs: dict[str, str | None] = {}
    video_ids: list[str] = []
    for video_dir, meta in _video_metas(meta_dir):
        video_ids.append(str(meta.get("id") or video_dir.name))
        ref = _media_ref(meta)
        if not ref:
            continue
        owned = bool(old_folder) and ref.startswith(f"{old_folder}/")
        shared = owned and any(
            row[0] != playlist_id for row in references.get(ref, [])
        )
        if owned and not (same and shared):
            continue  # travels with the media folder
        # A same-device move first hands shared media to the source's other
        # playlists (see ``move_playlist``); then it is copied like any
        # referenced file, alternate audio included.
        if ref not in refs:
            media = layout.resolve_media(ref)
            size = _regular_size(media) if media is not None else None
            if media is None or size is None:
                refs[ref] = None
            else:
                stem = f"{PUBLISH_PREFIX}{uuid.uuid4().hex[:8]}"
                refs[ref] = f"{stem}{media.suffix}"
                extra.append(MoveFile(media, f"media/{stem}{media.suffix}", size))
                for srt, suffix in _srt_files(media):
                    srt_size = _regular_size(srt)
                    if srt_size is not None:
                        extra.append(MoveFile(srt, f"media/{stem}{suffix}", srt_size))
        if not meta.get("audio_tracks"):
            continue
        audio_target = f"meta/videos/{video_dir.name}/audio"
        if owned:  # shared, same device: its audio goes to the heir
            extra += _tree(video_dir / "audio", audio_target)
        elif not (video_dir / "audio").is_dir():
            home = _audio_home(layout, video_dir, meta)
            if home != video_dir:
                extra += _tree(home / "audio", audio_target)
    return meta_files, owned_files, extra, refs, video_ids


def copy_size(library: Library, key: str) -> tuple[int, int, int]:
    """``(bytes, files, biggest file)`` a copy of playlist ``key`` writes to
    another library (zeros when it is not available)."""
    record = library.resolve_playlist(key)
    if record is None:
        return 0, 0, 0
    source, meta_dir, doc = record
    meta_files, owned_files, extra, _refs, _ids = _collect_files(
        source.layout, meta_dir, str(doc.get("id") or ""),
        str(doc.get("folder") or "") or None, False,
    )
    rows = meta_files + owned_files + extra
    return (
        sum(item.size for item in rows), len(rows),
        max((item.size for item in rows), default=0),
    )


def plan_move(
    library: Library, record: Record, dest_id: str, *, job_id: str,
    force_copy: bool = False,
) -> MovePlan:
    """Validate a move and work out every file it writes.

    ``force_copy`` plans a copy even on one device (a copy keeps its source,
    so nothing may be renamed away).  Raises :class:`MoveError` (a
    ``ValueError``) when the destination is missing or read-only, already
    holds this playlist id, cannot store a file (FAT32) or lacks the free
    space."""
    source, meta_dir, doc = record
    playlist_id = str(doc.get("id") or "")
    dest = library.libraries.get(dest_id)
    if dest is None:
        raise MoveError("that library is not connected; plug it in and try again")
    if dest.id == source.id:
        raise MoveError("this playlist is already in that library")
    if source.read_only or dest.read_only:
        raise MoveError(READ_ONLY_MESSAGE)
    layout, dlayout = source.layout, dest.layout
    try:
        target_meta = dlayout.playlist_meta_dir(playlist_id)
        staging = dlayout.meta_root / f"{STAGING_PREFIX}{job_id}"
    except ValueError as exc:
        raise MoveError(str(exc)) from exc
    if target_meta.exists() or library.resolve_in(dest.id, playlist_id) is not None:
        raise MoveError(
            f"{dest.label} already has a playlist with this id; remove it "
            "there first (playlists are never merged)"
        )
    same = not force_copy and _same_device(meta_dir, dlayout.root)
    old_folder = str(doc.get("folder") or "") or None
    title = library.titles_for(source).read()["playlists"].get(playlist_id) or str(
        doc.get("title") or playlist_id
    )
    dest_folder = _dest_folder(
        dest, playlist_id, old_folder or safe_name(title, fallback=playlist_id)
    )

    meta_files, owned_files, extra, refs, video_ids = _collect_files(
        layout, meta_dir, playlist_id, old_folder, same
    )
    if same:
        files = meta_files + owned_files + extra
        copies = extra
    else:
        files = copies = meta_files + owned_files + extra
    plan = MovePlan(
        source=source, dest=dest, playlist_id=playlist_id, meta_dir=meta_dir,
        target_meta=target_meta, staging=staging, old_folder=old_folder,
        dest_folder=dest_folder, same_device=same, files=files, copies=copies,
        refs=refs, video_ids=video_ids,
    )
    _preflight(plan)
    return plan


def _preflight(plan: MovePlan) -> None:
    root = plan.dest.layout.root
    # Sizes are known exactly, so only the filesystem's real per-file limit
    # applies (no estimate margin); the biggest file covers them all.
    limit = fsinfo.fs_info(root).max_file_bytes
    biggest = max(plan.files, key=lambda item: item.size, default=None)
    if limit is not None and biggest is not None and biggest.size > limit:
        raise MoveError(
            f"“{biggest.source.name}” is {human_bytes(biggest.size)}, larger "
            f"than the FAT32 limit of {human_bytes(limit)} per file on "
            f"{plan.dest.label}; nothing was moved"
        )
    need = plan.total_bytes
    free = free_bytes(root)
    if free and need > free:
        raise MoveError(
            f"{plan.dest.label} has {human_bytes(free)} free but the move "
            f"needs {human_bytes(need)}; nothing was moved"
        )


def verify_files(staging: Path, files: list[MoveFile]) -> str | None:
    """A human-readable mismatch, or ``None`` when every copy has its size."""
    missing = 0
    wrong = 0
    for item in files:
        size = _regular_size(staging / item.target)
        if size is None:
            missing += 1
        elif size != item.size:
            wrong += 1
    if missing:
        return f"{missing} file(s) missing from the copy"
    if wrong:
        return f"{wrong} file(s) have the wrong size"
    return None


# --- executing ---------------------------------------------------------------


@dataclass(frozen=True)
class MoveProgress:
    phase: str  # copying | published | cleaning_source | done
    bytes: int
    files: int
    total_bytes: int
    total_files: int
    label: str


ProgressCallback = Callable[[MoveProgress], None]


class _Reporter:
    """Throttled progress for one plan."""

    def __init__(self, callback: ProgressCallback, plan: MovePlan):
        self.callback = callback
        self.plan = plan
        self.bytes = 0
        self.files = 0
        self.phase_name = "copying"
        self.label = ""
        self._last = 0.0

    def phase(self, phase: str, label: str) -> None:
        self.phase_name = phase
        self.label = label
        self._emit()

    def chunk(self, size: int) -> None:
        self.bytes += size
        now = time.monotonic()
        if now - self._last >= PROGRESS_INTERVAL_S:
            self._emit()

    def file(self) -> None:
        self.files += 1

    def finish(self) -> None:
        self.bytes = self.plan.total_bytes
        self.files = self.plan.total_files
        self.phase("done", "")

    def _emit(self) -> None:
        self._last = time.monotonic()
        self.callback(MoveProgress(
            self.phase_name, self.bytes, self.files,
            self.plan.total_bytes, self.plan.total_files, self.label,
        ))


def _copy_into_staging(
    plan: MovePlan, report: _Reporter, cancelled: Callable[[], bool]
) -> None:
    shutil.rmtree(plan.staging, ignore_errors=True)
    plan.staging.mkdir(parents=True)
    report.phase("copying", "Copying media…")
    for item in plan.copies:
        if cancelled():
            raise JobCancelled("cancelled by the user")
        target = plan.staging / item.target
        target.parent.mkdir(parents=True, exist_ok=True)
        copy_file(item.source, target, on_chunk=report.chunk, cancel=cancelled)
        report.file()
    mismatch = verify_files(plan.staging, plan.copies)
    if mismatch:
        raise MoveError(f"copy verification failed: {mismatch}")
    if cancelled():
        raise JobCancelled("cancelled by the user")


def _new_ref(plan: FinishSpec, ref: str) -> str | None:
    if ref in plan.refs:
        name = plan.refs[ref]
        return f"{plan.dest_folder}/{name}" if name else None
    if plan.old_folder and ref.startswith(f"{plan.old_folder}/"):
        return plan.dest_folder + ref[len(plan.old_folder):]
    if ref.startswith(f"{plan.dest_folder}/"):
        return ref  # already rewritten (a repaired move runs this twice)
    return None  # never point at another library's file


def _rewrite_refs(meta_dir: Path, plan: FinishSpec) -> None:
    """Point a moved playlist's ``files.media`` refs at its new folder."""
    for video_dir, meta in _video_metas(meta_dir):
        ref = _media_ref(meta)
        if not ref:
            continue
        new = _new_ref(plan, ref)
        if new == ref:
            continue
        files = meta["files"]
        if new is None:
            files.pop("media", None)
        else:
            files["media"] = new
        write_json(video_dir / "meta.json", meta)
    doc_path = meta_dir / "playlist.json"
    doc = read_json(doc_path) or {}
    doc["folder"] = plan.dest_folder
    write_json(doc_path, doc)


def _finish_destination(plan: FinishSpec) -> None:
    layout = plan.dest_layout
    report = sync_playlist_files(layout, plan.playlist_id)
    for error in report.errors:
        log.warning("move %s: %s", plan.playlist_id, error)
    doc = read_json(plan.target_meta / "playlist.json") or {}
    try:
        index_playlist_dir(layout, plan.target_meta, doc)
    except Exception:  # noqa: BLE001 - the index is disposable
        log.exception("could not index moved playlist %s", plan.playlist_id)


def _owner_marker(media_dir: Path, staging: Path) -> Path:
    """The file that proves a published media folder was created by the move
    staged at ``staging`` (and so may be removed when it is undone)."""
    return media_dir / staging.name


def _require_free_destination(plan: MovePlan) -> None:
    if os.path.lexists(plan.dest_media) or plan.target_meta.exists():
        raise MoveError("the destination playlist appeared while moving")


def _publish_staging(plan: MovePlan) -> None:
    """Expose a verified staging copy: media folder first, then metadata.

    The media folder carries an ownership marker until the metadata is in
    place, so undoing an interrupted publish only ever removes a folder this
    move created."""
    media_stage = plan.staging / "media"
    _require_free_destination(plan)
    moved_media = False
    if media_stage.is_dir():
        _owner_marker(media_stage, plan.staging).touch()
        plan.dest_media.parent.mkdir(parents=True, exist_ok=True)
        os.rename(media_stage, plan.dest_media)
        _synced(plan.staging, plan.dest_media.parent)
        moved_media = True
    try:
        plan.target_meta.parent.mkdir(parents=True, exist_ok=True)
        os.rename(plan.staging / "meta", plan.target_meta)
        _synced(plan.staging, plan.target_meta.parent)
    except Exception:
        if moved_media:
            os.rename(plan.dest_media, media_stage)
        raise
    _owner_marker(plan.dest_media, plan.staging).unlink(missing_ok=True)


def _copy_move(
    library: Library, plan: MovePlan, report: _Reporter,
    cancelled: Callable[[], bool], *, remove_source: bool = True,
) -> None:
    meta_root = plan.dest.layout.meta_root
    spec = plan.spec()
    # Cleanup runs for errors, not for a crash (or SystemExit): what a crash
    # leaves is repaired from the journal by ``reconcile_move``.
    try:
        _copy_into_staging(plan, report, cancelled)
        _rewrite_refs(plan.staging / "meta", spec)
        library.merge_state_into(
            plan.source, plan.dest, plan.playlist_id, plan.video_ids
        )
        report.phase("published", "Publishing…")
        with PUBLISH_LOCK:
            # Checked before the journal exists: a journal names a
            # destination folder that this move, not another playlist, owns.
            _require_free_destination(plan)
            _write_journal(spec)
            _publish_staging(plan)
    except Exception:
        _drop_staging(plan.staging, meta_root)
        raise
    _drop_staging(plan.staging, meta_root)
    try:
        _finish_destination(spec)
    except Exception as exc:  # noqa: BLE001 - reported honestly
        raise PublishedError(
            f"the playlist was copied to {plan.dest.label}, but finishing it "
            f"there failed ({exc}); the original was kept"
        ) from exc
    if not remove_source:
        return
    report.phase("cleaning_source", "Removing the original…")
    try:
        library.remove_from(plan.source.id, plan.playlist_id)
    except Exception as exc:  # noqa: BLE001 - reported honestly
        raise SourceCleanupError(
            "the playlist was copied to the new library, but the original "
            f"could not be removed ({exc}); both copies still exist"
        ) from exc


def _rename_move(
    library: Library, plan: MovePlan, report: _Reporter,
    cancelled: Callable[[], bool],
) -> None:
    """Same device: rename the media and metadata folders; copy only what
    the source must keep (media the playlist references)."""
    source = plan.source
    meta_root = plan.dest.layout.meta_root
    spec = plan.spec()
    with PUBLISH_LOCK:
        try:
            _copy_into_staging(plan, report, cancelled)
            library.merge_state_into(
                source, plan.dest, plan.playlist_id, plan.video_ids
            )
            report.phase("published", "Moving folders…")
            owned_dir = (
                source.layout.media_root / plan.old_folder
                if plan.old_folder else None
            )
            _require_free_destination(plan)
            _write_journal(spec)
            moved_media = False
            if owned_dir is not None and owned_dir.is_dir():
                plan.dest_media.parent.mkdir(parents=True, exist_ok=True)
                os.rename(owned_dir, plan.dest_media)
                _synced(owned_dir.parent, plan.dest_media.parent)
                moved_media = True
            try:
                plan.target_meta.parent.mkdir(parents=True, exist_ok=True)
                os.rename(plan.meta_dir, plan.target_meta)
                _synced(plan.meta_dir.parent, plan.target_meta.parent)
            except Exception:
                if moved_media and owned_dir is not None:
                    os.rename(plan.dest_media, owned_dir)
                raise
        except Exception:
            _drop_staging(plan.staging, meta_root)
            raise
        # Renamed: from here the staging and journal are only dropped once
        # the copies are in place, so a restart can finish the job.
        # Refs first, then the files: a staged copy under the media root is
        # never unreferenced, so a startup orphan sweep cannot take it.
        try:
            _rewrite_refs(plan.target_meta, spec)
            _place_staged(spec)
        except Exception as exc:  # noqa: BLE001 - reported honestly
            raise PublishedError(
                f"the playlist was moved to {plan.dest.label}, but some of "
                f"its media could not be put in place ({exc}); it is "
                "finished on the next start"
            ) from exc
        _drop_staging(plan.staging, meta_root)
        library.forget_moved(source, plan.playlist_id)
        try:
            _finish_destination(spec)
        except Exception as exc:  # noqa: BLE001 - reported honestly
            raise PublishedError(
                f"the playlist was moved to {plan.dest.label}, but finishing "
                f"it there failed ({exc})"
            ) from exc


def _place_staged(plan: FinishSpec) -> None:
    """Move staged copies (referenced media, their audio) into a playlist
    renamed onto the destination."""
    staged_media = plan.staging / "media"
    if staged_media.is_dir():
        plan.dest_media.mkdir(parents=True, exist_ok=True)
        for path in staged_media.iterdir():
            os.replace(path, plan.dest_media / path.name)
        _synced(staged_media, plan.dest_media)
    staged_videos = plan.staging / "meta" / "videos"
    if staged_videos.is_dir():
        for video in staged_videos.iterdir():
            audio = plan.target_meta / "videos" / video.name / "audio"
            if (video / "audio").is_dir() and not audio.exists():
                audio.parent.mkdir(parents=True, exist_ok=True)
                os.replace(video / "audio", audio)
                _synced(video, audio.parent)


def move_playlist(
    library: Library,
    playlist_id: str,
    dest_id: str,
    progress_cb: ProgressCallback,
    *,
    job_id: str | None = None,
    cancel: Callable[[], bool] | None = None,
) -> None:
    """Move playlist ``playlist_id`` (a playlist key) to library ``dest_id``.

    Carries the metadata folder, the media folder, media the playlist only
    references, and its progress, titles and tags; then publishes and syncs it
    on the destination, removes it from the source (media other source
    playlists reference is handed to them, never deleted) and updates both
    search indexes.  Raises :class:`MoveError` before touching anything when
    the move cannot be done safely, ``JobCancelled`` when cancelled before
    publication, and :class:`SourceCleanupError` when the copy is published but
    the original could not be removed."""
    job_id = job_id or uuid.uuid4().hex[:12]
    cancelled = cancel or (lambda: False)
    record = library.resolve_playlist(playlist_id)
    if record is None:
        raise MoveError("the playlist is no longer available")
    plan = plan_move(library, record, dest_id, job_id=job_id)
    if plan.same_device:
        source_id, real_id = plan.source.id, plan.playlist_id
        with PUBLISH_LOCK:
            # Media other source playlists use stays in the source: hand it
            # to them first, then copy it like any referenced file.
            library.release_shared_media(source_id, real_id)
            record = library.resolve_in(source_id, real_id)
            if record is None:
                raise MoveError("the playlist is no longer available")
            plan = plan_move(library, record, dest_id, job_id=job_id)
            report = _Reporter(progress_cb, plan)
            _rename_move(library, plan, report, cancelled)
    else:
        report = _Reporter(progress_cb, plan)
        _copy_move(library, plan, report, cancelled)
    report.finish()


def copy_playlist(
    library: Library,
    playlist_id: str,
    dest_id: str,
    progress_cb: ProgressCallback,
    *,
    job_id: str | None = None,
    cancel: Callable[[], bool] | None = None,
) -> None:
    """Copy playlist ``playlist_id`` (a playlist key) to library ``dest_id``,
    keeping the source.

    The copy path of :func:`move_playlist` without its last step: staged,
    verified, published and synced on the destination exactly like a move
    across devices (watch progress, titles and tags included), but the
    source is never touched."""
    job_id = job_id or uuid.uuid4().hex[:12]
    record = library.resolve_playlist(playlist_id)
    if record is None:
        raise MoveError("the playlist is no longer available")
    plan = plan_move(library, record, dest_id, job_id=job_id, force_copy=True)
    report = _Reporter(progress_cb, plan)
    _copy_move(
        library, plan, report, cancel or (lambda: False), remove_source=False
    )
    report.finish()


@dataclass(frozen=True)
class ReconcileResult:
    phase: str
    status: str
    cleanup_complete: bool
    error: str | None = None


def reconcile_move(
    *,
    source: Path,
    target: Path,
    staging: Path | None,
) -> ReconcileResult:
    """Work out what an interrupted move actually left on disk.

    ``source`` and ``target`` are the playlist's metadata folders.
    ``published`` without source cleanup means the media is safe on the target
    but the original still exists; the caller reports that honestly rather than
    claiming the source was kept or removed.
    """
    spec = _read_journal(source, target, staging) if staging is not None else None
    if spec is not None:
        repaired = _reconcile_journal(source, target, spec)
        if repaired is not None:
            return repaired
    target_ok = target.is_dir()
    source_ok = source.is_dir()
    if target_ok and not source_ok:
        if staging is not None:
            remove_staging(staging, target.parent)
        return ReconcileResult("done", "done", True, None)
    if target_ok and source_ok:
        return ReconcileResult(
            "published", "failed", False,
            "the playlist was copied to the new location, but the original "
            "folder is still there; remove it after checking the copy",
        )
    if source_ok:
        if staging is not None:
            remove_staging(staging, target.parent)
        return ReconcileResult(
            "staging", "failed", True,
            "the move was interrupted before anything was published; the "
            "playlist was left where it was",
        )
    return ReconcileResult(
        "unknown", "failed", False,
        "the move was interrupted and neither the source nor the destination "
        "is available; check the drive",
    )


def _reconcile_journal(
    source: Path, target: Path, spec: FinishSpec
) -> ReconcileResult | None:
    """Repair what a crash left between a move's renames; ``None`` lets the
    plain meta-folder rules decide."""
    target_ok = target.is_dir()
    source_ok = source.is_dir()
    meta_root = target.parent
    dest_media = spec.dest_media
    if source_ok and not target_ok:
        source_media = spec.source_media
        if spec.mode == "rename":
            # The media folder was renamed but its metadata was not: put
            # the folder back so the source's refs resolve again.
            if (
                source_media is not None and dest_media.is_dir()
                and not os.path.lexists(source_media)
            ):
                os.rename(dest_media, source_media)
                _synced(dest_media.parent, source_media.parent)
        elif dest_media.is_dir() and is_within(dest_media, spec.dest_layout.media_root):
            if _owner_marker(dest_media, spec.staging).is_file():
                # A complete copy published without its metadata; the
                # source is intact, so the orphan goes.
                shutil.rmtree(dest_media)
            else:
                # Not created by this move (another playlist claimed the
                # name): never remove it.
                _drop_staging(spec.staging, meta_root)
                return ReconcileResult(
                    "staging", "failed", False,
                    "the move was interrupted before anything was published; "
                    "the playlist was left where it was. The folder "
                    f"“{spec.dest_folder}” on the destination was not created "
                    "by this move and was left alone",
                )
        _drop_staging(spec.staging, meta_root)
        return ReconcileResult(
            "staging", "failed", True,
            "the move was interrupted before anything was published; the "
            "playlist was left where it was",
        )
    if target_ok and not source_ok and spec.mode == "rename":
        try:
            _rewrite_refs(target, spec)
            _place_staged(spec)
            _finish_destination(spec)
        except Exception as exc:  # noqa: BLE001 - reported honestly
            return ReconcileResult(
                "published", "failed", False,
                f"the playlist was moved, but finishing it failed ({exc}); "
                "it is retried on the next start",
            )
        _drop_staging(spec.staging, meta_root)
        try:
            remove_playlist_index(LibraryLayout(spec.source_root), spec.playlist_id)
        except Exception:  # noqa: BLE001 - the index is disposable
            log.exception("could not drop %s from the source index", spec.playlist_id)
        return ReconcileResult("done", "done", True, None)
    if target_ok:
        # A copy move whose publish completed: the staging is spent.
        _owner_marker(dest_media, spec.staging).unlink(missing_ok=True)
        _drop_staging(spec.staging, meta_root)
    return None
