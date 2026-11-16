"""Migrate a pre-portable library into the portable layout.

The old layout is ``<root>/playlists/<Title> [<id>]/videos/<vid>/video.<ext>``
with ``progress.json``, ``titles.json`` and ``jobs.json`` at ``<root>`` (and
more playlists on the extra locations listed in ``storage.json``).

Metadata is **copied** into ``.ytop/data/playlists/``, so the old JSON stays as
a backup. Media and alternate audio are **moved** with ``os.replace`` when the
source is on the target's device, otherwise copied and verified (size) with the
source left in place. Nothing in the old tree is ever deleted.

Every step can be repeated: a video's ``meta.json`` records the media's name
in the target *before* the file is moved there, and the migration journal
(``.ytop/data/migration-journal.json``) records finished playlists and which
source inode became which video. A rerun skips finished playlists, and a
playlist interrupted midway picks up its already-placed media by video id.
"""

from __future__ import annotations

import os
import re
import shutil
import tomllib
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ytop.layout import (
    SCHEMA_VERSION,
    UNGROUPED_FOLDER,
    UNGROUPED_ID,
    LibraryLayout,
    init_library,
    read_library,
    safe_id,
)
from ytop.publish import (
    META_FILE,
    PLAYLIST_FILE,
    PUBLISH_PREFIX,
    _unique_folder,
    sync_playlist_files,
)
from ytop.yt.metadata import (
    fsync_dir,
    now_iso,
    read_json,
    read_json_document,
    write_json,
)

JOURNAL_NAME = "migration-journal.json"
MARKER_NAME = "MIGRATED.txt"
INTERRUPTED = "Interrupted by migration — re-add to resume"
FINISHED_JOB_STATES = {"done", "failed", "cancelled"}
MEDIA_SUFFIXES = {
    ".webm", ".mp4", ".mkv", ".m4a", ".mov", ".opus", ".ogg", ".mp3",
    ".flv", ".3gp", ".m4v", ".aac", ".flac", ".wav",
}
_PARTIAL = re.compile(r"\.(part(-Frag\d+)?|ytdl)$", re.IGNORECASE)
# One stream of an unfinished merge (``video.f399.mp4`` beside
# ``video.f249.webm.part``): video only, so the download is incomplete.
_FORMAT_STREAM = re.compile(r"^video\.f\d+(-\d+)?\.[A-Za-z0-9]+$")


@dataclass
class OldVideo:
    id: str
    path: Path
    media: Path | None
    audio: list[Path]
    partials: list[Path]


@dataclass
class OldPlaylist:
    id: str
    title: str
    path: Path
    location: Path
    videos: list[OldVideo] = field(default_factory=list)
    migrated: bool = False
    # Why this playlist will not be migrated (a duplicate id in the old
    # setup, or a playlist the target already holds); empty if it will be.
    skip_reason: str = ""


@dataclass
class MigrationPlan:
    playlists: list[OldPlaylist]
    bytes_to_copy: int
    bytes_to_move: int
    dropped_partials: list[Path]
    warnings: list[str]
    old_root: Path = Path()
    # Folders the plan could not read, as ``(path, reason)``: playlist
    # folders without a readable playlist.json and storage locations that
    # are not connected. Never migrated, so always listed as left behind.
    unscanned: list[tuple[Path, str]] = field(default_factory=list)


@dataclass
class MigrationReport:
    target: Path
    migrated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    unmigrated: list[str] = field(default_factory=list)
    left_behind: list[str] = field(default_factory=list)
    moved: int = 0
    copied: int = 0
    references: int = 0
    without_media: list[str] = field(default_factory=list)
    jobs_carried: int = 0
    indexed: int = 0
    warnings: list[str] = field(default_factory=list)


# --- helpers -----------------------------------------------------------------


def _host_config_path() -> Path:
    return Path.home() / ".config" / "ytop" / "config.toml"


def _existing_ancestor(path: Path) -> Path:
    path = Path(path).absolute()
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def _same_device(source: Path, target: Path) -> bool:
    """True when ``source`` and ``target`` (or its nearest existing
    ancestor) are on one device, so ``os.replace`` can move between them."""
    return os.stat(source).st_dev == os.stat(_existing_ancestor(target)).st_dev


def _is_partial(path: Path) -> bool:
    return bool(_PARTIAL.search(path.name))


def _inode_key(path: Path) -> str:
    stat = os.stat(path)
    return f"{stat.st_dev}:{stat.st_ino}"


def _journal_path(target: Path) -> Path:
    return LibraryLayout(Path(target)).data_dir / JOURNAL_NAME


def _read_journal(target: Path) -> dict:
    """``playlists`` maps a source playlist folder to ``{"id", "state"}``
    (``started`` or ``done``); ``media`` maps a source inode to the
    ``[playlist, video]`` that owns it in the target."""
    doc = read_json(_journal_path(target)) or {}
    playlists = doc.get("playlists")
    media = doc.get("media")
    return {
        "schema": 1,
        "playlists": {
            str(key): dict(value) for key, value in playlists.items()
            if isinstance(value, dict)
        } if isinstance(playlists, dict) else {},
        "media": dict(media) if isinstance(media, dict) else {},
    }


def _source_key(playlist: OldPlaylist) -> str:
    return str(playlist.path.resolve())


def _journal_state(journal: dict, playlist: OldPlaylist) -> str | None:
    row = journal["playlists"].get(_source_key(playlist))
    if not isinstance(row, dict) or str(row.get("id")) != playlist.id:
        return None
    state = row.get("state")
    return str(state) if state in {"started", "done"} else None


def _target_has(target: Path, playlist_id: str) -> bool:
    try:
        meta_dir = LibraryLayout(Path(target)).playlist_meta_dir(playlist_id)
    except ValueError:
        return False
    return (meta_dir / PLAYLIST_FILE).exists()


def _conflict_reason(target: Path, journal: dict, playlist: OldPlaylist) -> str:
    """Why ``playlist`` must be left alone, or ``""``: a playlist the target
    already holds that this migration never started is not overwritten."""
    if _journal_state(journal, playlist) is None and _target_has(target, playlist.id):
        return (
            f"the target library already has playlist {playlist.id}; "
            "it was left untouched"
        )
    return ""


def _find_media(video_dir: Path, meta: dict) -> Path | None:
    """The old video's complete media file, if it has one."""
    files = meta.get("files")
    name = files.get("video") if isinstance(files, dict) else None
    if isinstance(name, str) and name and Path(name).name == name:
        candidate = video_dir / name
        if (
            not _is_partial(candidate)
            and candidate.is_file()
            and candidate.stat().st_size > 0
        ):
            return candidate
    found = [path for path in _stray_media(video_dir) if path.stat().st_size > 0]
    return max(found, key=lambda path: path.stat().st_size) if found else None


def _stray_media(video_dir: Path) -> list[Path]:
    """``video.<ext>`` media files in an old video folder."""
    return sorted(
        path for path in video_dir.iterdir()
        if path.is_file()
        and path.name.startswith("video.")
        and path.suffix.lower() in MEDIA_SUFFIXES
        and not _is_partial(path)
    )


def _scan_video(video_dir: Path) -> OldVideo:
    meta = read_json(video_dir / META_FILE) or {}
    audio: list[Path] = []
    partials = [path for path in sorted(video_dir.iterdir())
                if path.is_file() and _is_partial(path)]
    media = _find_media(video_dir, meta)
    if media is not None and partials and _FORMAT_STREAM.match(media.name):
        # An interrupted merge: not migrated as media (it stays in the old
        # folder) and the video is shown as not downloaded.
        partials.append(media)
        media = None
    audio_dir = video_dir / "audio"
    if audio_dir.is_dir():
        for path in sorted(audio_dir.rglob("*")):
            if not path.is_file():
                continue
            (partials if _is_partial(path) else audio).append(path)
    return OldVideo(
        id=str(meta.get("id") or video_dir.name),
        path=video_dir,
        media=media,
        audio=audio,
        partials=partials,
    )


def _scan_playlist(playlist_dir: Path, location: Path) -> OldPlaylist | None:
    doc = read_json(playlist_dir / PLAYLIST_FILE)
    if not doc or not doc.get("id"):
        return None
    playlist = OldPlaylist(
        id=str(doc["id"]),
        title=str(doc.get("title") or doc["id"]),
        path=playlist_dir,
        location=location,
    )
    videos_dir = playlist_dir / "videos"
    if videos_dir.is_dir():
        for video_dir in sorted(videos_dir.iterdir()):
            if video_dir.is_dir() and not video_dir.name.startswith("."):
                playlist.videos.append(_scan_video(video_dir))
    return playlist


def _extra_locations(
    old_root: Path, warnings: list[str], unscanned: list[tuple[Path, str]]
) -> list[Path]:
    doc = read_json(old_root / "storage.json") or {}
    rows = doc.get("locations")
    found: list[Path] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not row.get("path"):
            continue
        path = Path(str(row["path"])).expanduser()
        label = str(row.get("label") or path)
        try:
            reachable = (path / "playlists").is_dir()
        except OSError:
            reachable = False
        if not reachable:
            unscanned.append((path, f"storage location {label} is not connected"))
            warnings.append(
                f"storage location {label} ({path}) is not connected; its "
                "playlists were skipped (connect it and rerun to migrate them)"
            )
            continue
        if path.resolve() != old_root.resolve():
            found.append(path)
    return found


def _state_warnings(old_root: Path) -> list[str]:
    warnings = []
    for name in ("progress.json", "titles.json"):
        document = read_json_document(old_root / name)
        if document.corrupt or document.status == "unreadable":
            warnings.append(
                f"{old_root / name} is damaged; repair it before migrating "
                "(the migration stops rather than lose it)"
            )
    return warnings


def _cookie_warnings() -> list[str]:
    path = _host_config_path()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return []
    if not any("cookies" in str(key) for key in data):
        return []
    return [
        f"{path} sets cookies; they are not carried into the library. Use "
        "the session-only browser login when a download needs it."
    ]


# --- planning ----------------------------------------------------------------


def plan_migration(old_root: Path, target: Path) -> MigrationPlan:
    """Describe the migration of ``old_root`` into ``target``; reads only."""
    old_root = Path(old_root).expanduser()
    target = Path(target).expanduser()
    if not (old_root / "playlists").is_dir():
        raise ValueError(f"{old_root} has no playlists/ folder (not an old library)")
    old_resolved = old_root.resolve()
    target_resolved = target.resolve()
    if target_resolved == old_resolved or target_resolved.is_relative_to(
        old_resolved / "playlists"
    ):
        raise ValueError("the target must be outside the old library's folders")

    warnings: list[str] = []
    unscanned: list[tuple[Path, str]] = []
    locations = [old_root, *_extra_locations(old_root, warnings, unscanned)]
    warnings += _state_warnings(old_root)
    warnings += _cookie_warnings()
    journal = _read_journal(target)

    playlists: list[OldPlaylist] = []
    seen: dict[str, Path] = {}
    for location in locations:
        try:
            children = sorted((location / "playlists").iterdir())
        except OSError as exc:
            if location == old_root:
                raise
            unscanned.append((location, f"storage location is unreadable ({exc})"))
            warnings.append(
                f"storage location {location} could not be read ({exc}); its "
                "playlists were skipped"
            )
            continue
        for playlist_dir in children:
            if playlist_dir.name.startswith("."):
                continue
            if not playlist_dir.is_dir():
                # Never migrated, so listed with what is left behind.
                unscanned.append((playlist_dir, "not a playlist folder"))
                continue
            playlist = _scan_playlist(playlist_dir, location)
            if playlist is None:
                warnings.append(f"{playlist_dir} has no readable playlist.json; skipped")
                unscanned.append((playlist_dir, "no readable playlist.json"))
                continue
            if playlist.id in seen:
                playlist.skip_reason = (
                    f"playlist {playlist.id} also exists at "
                    f"{seen[playlist.id]}; only that copy is migrated"
                )
            else:
                seen[playlist.id] = playlist_dir
                playlist.migrated = _journal_state(journal, playlist) == "done"
                playlist.skip_reason = _conflict_reason(target, journal, playlist)
            if playlist.skip_reason:
                warnings.append(f"{playlist_dir}: {playlist.skip_reason}")
            playlists.append(playlist)

    to_copy = to_move = 0
    partials: list[Path] = []
    counted: set[str] = set()
    for playlist in playlists:
        if playlist.migrated or playlist.skip_reason:
            continue
        for video in playlist.videos:
            partials += video.partials
            files = [video.media] if video.media is not None else []
            for path in files + video.audio:
                key = _inode_key(path)
                if key in counted or key in journal["media"]:
                    continue
                counted.add(key)
                size = path.stat().st_size
                if _same_device(path, target):
                    to_move += size
                else:
                    to_copy += size
    return MigrationPlan(
        playlists=playlists,
        bytes_to_copy=to_copy,
        bytes_to_move=to_move,
        dropped_partials=partials,
        warnings=warnings,
        old_root=old_root,
        unscanned=unscanned,
    )


# --- running -----------------------------------------------------------------


@dataclass
class _Context:
    layout: LibraryLayout
    journal: dict
    progress: dict
    titles: dict
    report: MigrationReport

    def save_journal(self) -> None:
        write_json(self.layout.data_dir / JOURNAL_NAME, self.journal)


def _read_state(old_root: Path) -> tuple[dict, dict]:
    from ytop.web.library import _read_section_doc

    progress = _read_section_doc(old_root / "progress.json", ("videos",))
    titles = _read_section_doc(old_root / "titles.json", ("playlists", "videos"))
    return progress, titles


def _place_media(ctx: _Context, source: Path, dest: Path) -> None:
    """Move ``source`` to ``dest`` (same device) or copy and verify it."""
    if os.path.lexists(dest):
        if dest.stat().st_size == source.stat().st_size:
            return
        raise FileExistsError(f"{dest} already exists with different content")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if _same_device(source, ctx.layout.root):
        os.replace(source, dest)
        fsync_dir(source.parent)
        fsync_dir(dest.parent)
        ctx.report.moved += 1
        return
    staging_dir = ctx.layout.cache_dir / "migrate"
    staging_dir.mkdir(parents=True, exist_ok=True)
    staging = staging_dir / f"{uuid.uuid4().hex}.partial"
    try:
        shutil.copyfile(source, staging)
        with open(staging, "rb") as handle:
            os.fsync(handle.fileno())
        expected = source.stat().st_size
        actual = staging.stat().st_size
        if actual != expected:
            raise OSError(
                f"copy of {source} is {actual} bytes, expected {expected}"
            )
        os.replace(staging, dest)
        fsync_dir(dest.parent)
    finally:
        staging.unlink(missing_ok=True)
    ctx.report.copied += 1


def _media_ref(meta: dict | None) -> str | None:
    files = (meta or {}).get("files")
    ref = files.get("media") if isinstance(files, dict) else None
    return ref if isinstance(ref, str) and ref else None


def _copy_metadata(old_dir: Path, new_dir: Path, skip: set[Path]) -> None:
    """Copy an old video folder's metadata files (never media or audio)."""
    new_dir.mkdir(parents=True, exist_ok=True)
    for child in sorted(old_dir.iterdir()):
        if child in skip or child.name in {META_FILE, "audio"} or _is_partial(child):
            continue
        if child.is_dir():
            shutil.copytree(
                child, new_dir / child.name, dirs_exist_ok=True,
                ignore=lambda _d, names: [n for n in names if _is_partial(Path(n))],
            )
        elif child.is_file():
            shutil.copy2(child, new_dir / child.name)


def _migrate_audio(ctx: _Context, video: OldVideo, new_dir: Path) -> None:
    audio_root = video.path / "audio"
    for source in video.audio:
        dest = new_dir / "audio" / source.relative_to(audio_root)
        if os.path.lexists(dest) and not source.exists():
            continue  # moved by an earlier, interrupted run
        _place_media(ctx, source, dest)


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _set_aside(ctx: _Context, path: Path) -> None:
    aside = ctx.layout.cache_dir / "migrate" / "set-aside"
    aside.mkdir(parents=True, exist_ok=True)
    target = aside / f"{uuid.uuid4().hex[:8]}-{path.name}"
    os.replace(path, target)
    ctx.report.warnings.append(f"set aside a damaged earlier copy as {target}")


def _owner_ref(ctx: _Context, owner: list, size: int) -> tuple[str, Path] | None:
    """The owner's media ref and meta dir, if its file is in place."""
    try:
        owner_dir = ctx.layout.video_meta_dir(str(owner[0]), str(owner[1]))
    except (ValueError, IndexError):
        return None
    ref = _media_ref(read_json(owner_dir / META_FILE))
    path = ctx.layout.resolve_media(ref)
    if ref is None or path is None or path.stat().st_size != size:
        return None
    return ref, owner_dir


def _migrate_video(
    ctx: _Context, playlist_id: str, folder: Path, video: OldVideo
) -> bool:
    """Migrate one old video folder; returns whether it has media."""
    layout = ctx.layout
    old_meta = read_json(video.path / META_FILE) or {"id": video.id}
    new_dir = layout.video_meta_dir(playlist_id, video.id)
    skip = set(_stray_media(video.path))
    if video.media is not None:
        skip.add(video.media)
    _copy_metadata(video.path, new_dir, skip)

    meta = dict(old_meta)
    old_files = old_meta.get("files")
    files = dict(old_files) if isinstance(old_files, dict) else {}
    files.pop("video", None)
    meta["files"] = files
    meta_path = new_dir / META_FILE

    current = _media_ref(read_json(meta_path))
    placed = layout.resolve_media(current)
    if current is not None and placed is not None:
        # Placed by an earlier, interrupted run.
        files["media"] = current
        write_json(meta_path, meta)
        source = video.media
        if source is not None and source.exists() and not _same_file(source, placed):
            if placed.stat().st_size != source.stat().st_size:
                # A damaged placement: set it aside (never delete) and
                # place the source again under the same name.
                _set_aside(ctx, placed)
                _place_media(ctx, source, placed)
        _migrate_audio(ctx, video, new_dir)
        return True

    media = video.media
    if media is None:
        files["media"] = None
        write_json(meta_path, meta)
        _migrate_audio(ctx, video, new_dir)
        ctx.report.without_media.append(f"{playlist_id}/{video.id}")
        return False

    key = _inode_key(media)
    me = [playlist_id, video.id]
    owner = ctx.journal["media"].get(key)
    found = None
    if isinstance(owner, list) and owner != me:
        found = _owner_ref(ctx, owner, media.stat().st_size)
    if found is not None:
        # A hard-linked duplicate: reference the owner's file instead of
        # storing it twice.
        ref, owner_dir = found
        files["media"] = ref
        write_json(meta_path, meta)
        if not (owner_dir / "audio").is_dir():
            _migrate_audio(ctx, video, new_dir)
        ctx.report.references += 1
        return True
    if owner != me:
        # Recorded before the move: after it the source inode is gone from
        # the old tree, and a rerun must still know this video owns it.
        ctx.journal["media"][key] = me
        ctx.save_journal()

    dest = folder / f"{PUBLISH_PREFIX}m-{safe_id(video.id)}{media.suffix.lower()}"
    # The ref is written before the move, so the file is never unreferenced.
    files["media"] = layout.relative_media(dest)
    write_json(meta_path, meta)
    _place_media(ctx, media, dest)
    _migrate_audio(ctx, video, new_dir)
    return True


def _carry_state(ctx: _Context, playlist_id: str, video_ids: set[str]) -> None:
    from ytop.web.library import _merge_sections

    rows = {
        key: value for key, value in ctx.progress["videos"].items()
        if key in video_ids and isinstance(value, dict)
    }
    if rows:
        _merge_sections(ctx.layout.progress_file, ("videos",), {"videos": rows},
                        prefer_newer=True)
    existing = (read_json(ctx.layout.titles_file) or {}).get("playlists") or {}
    picked = {
        "playlists": {
            key: value for key, value in ctx.titles["playlists"].items()
            if key == playlist_id and not existing.get(key)
        },
        "videos": {
            key: value for key, value in ctx.titles["videos"].items()
            if key in video_ids
        },
    }
    if any(picked.values()):
        _merge_sections(ctx.layout.titles_file, ("playlists", "videos"), picked)


def _migrate_playlist(ctx: _Context, playlist: OldPlaylist) -> None:
    layout = ctx.layout
    fresh = _scan_playlist(playlist.path, playlist.location)
    doc = read_json(playlist.path / PLAYLIST_FILE)
    if fresh is None or doc is None:
        raise FileNotFoundError(playlist.path / PLAYLIST_FILE)
    playlist_id = fresh.id
    meta_dir = layout.playlist_meta_dir(playlist_id)
    existing = read_json(meta_dir / PLAYLIST_FILE) or {}

    new_doc = dict(doc)
    entries = []
    for entry in doc.get("entries") or []:
        if isinstance(entry, dict):
            entries.append({
                key: value for key, value in entry.items()
                if key not in {"path", "video"}
            })
    new_doc["entries"] = entries
    if existing.get("folder"):
        folder_name = str(existing["folder"])
    elif playlist_id == UNGROUPED_ID:
        folder_name = UNGROUPED_FOLDER
    else:
        title = ctx.titles["playlists"].get(playlist_id) or fresh.title
        folder_name = _unique_folder(layout, playlist_id, str(title))
    new_doc["folder"] = folder_name
    meta_dir.mkdir(parents=True, exist_ok=True)
    write_json(meta_dir / PLAYLIST_FILE, new_doc)
    for child in sorted(playlist.path.iterdir()):
        if child.name in {PLAYLIST_FILE, "videos"} or _is_partial(child):
            continue
        if child.is_file():
            shutil.copy2(child, meta_dir / child.name)
        elif child.is_dir():
            shutil.copytree(child, meta_dir / child.name, dirs_exist_ok=True)

    video_ids = {video.id for video in fresh.videos}
    video_ids |= {str(entry["id"]) for entry in entries if entry.get("id")}
    _carry_state(ctx, playlist_id, video_ids)

    folder = layout.media_root / folder_name
    available: dict[str, bool] = {}
    for video in fresh.videos:
        available[video.id] = _migrate_video(ctx, playlist_id, folder, video)
    for entry in entries:
        entry["available"] = available.get(str(entry.get("id")), False)
    write_json(meta_dir / PLAYLIST_FILE, new_doc)

    report = sync_playlist_files(layout, playlist_id)
    ctx.report.warnings += [f"{playlist_id}: {error}" for error in report.errors]


def _carry_jobs(ctx: _Context, old_root: Path, library_id: str, label: str) -> None:
    from ytop.web.jobs import LEGACY_KEYS

    old = read_json_document(old_root / "jobs.json")
    if old.corrupt or old.status == "unreadable":
        ctx.report.warnings.append(f"{old_root / 'jobs.json'} is damaged; jobs not carried")
        return
    rows = (old.data or {}).get("jobs") if old.ok else None
    path = ctx.layout.jobs_file
    current = read_json_document(path)
    if current.corrupt or current.status == "unreadable":
        ctx.report.warnings.append(f"{path} is damaged; jobs not carried")
        return
    data = dict(current.data) if isinstance(current.data, dict) else {}
    jobs = [row for row in data.get("jobs") or [] if isinstance(row, dict)]
    known = {str(row.get("id")) for row in jobs}
    added = 0
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        if str(row.get("status")) in FINISHED_JOB_STATES or str(row["id"]) in known:
            continue
        job = dict(row)
        for old_key, new_key in LEGACY_KEYS.items():
            if old_key in job:
                job.setdefault(new_key, job.pop(old_key))
        job.update({
            "library_id": library_id,
            "library_label": label,
            "status": "failed",
            "error": INTERRUPTED,
            "finished_at": now_iso(),
            "current_id": None,
            "current_title": None,
            "cancel_requested": False,
        })
        jobs.append(job)
        known.add(str(row["id"]))
        added += 1
    if added:
        write_json(path, {"schema": 1, **data, "jobs": jobs})
    ctx.report.jobs_carried += added


def _audio_accounted(ctx: _Context, new_dir: Path, rel: Path, size: int) -> bool:
    from ytop.web.library import media_references

    dirs = [new_dir]
    ref = _media_ref(read_json(new_dir / META_FILE))
    if ref:
        for playlist_id, video_id in media_references(ctx.layout, ref):
            try:
                dirs.append(ctx.layout.video_meta_dir(playlist_id, video_id))
            except ValueError:
                continue
    for directory in dirs:
        candidate = directory / "audio" / rel
        if candidate.is_file() and candidate.stat().st_size == size:
            return True
    return False


def _left_behind(ctx: _Context, plan: MigrationPlan) -> list[str]:
    """Everything in the old tree whose content is not in the target."""
    items = [f"{path}  (not migrated: {reason})" for path, reason in plan.unscanned]
    for playlist in plan.playlists:
        if playlist.skip_reason or _journal_state(ctx.journal, playlist) != "done":
            reason = playlist.skip_reason or "not migrated"
            items.append(f"{playlist.path}  (whole playlist: {reason})")
            continue
        fresh = _scan_playlist(playlist.path, playlist.location)
        for video in fresh.videos if fresh is not None else []:
            for path in video.partials:
                items.append(f"{path}  (unfinished download)")
            for path in _stray_media(video.path):
                if path != video.media and path not in video.partials:
                    items.append(f"{path}  (extra media file)")
            try:
                new_dir = ctx.layout.video_meta_dir(playlist.id, video.id)
            except ValueError:
                continue
            if video.media is not None:
                placed = ctx.layout.resolve_media(
                    _media_ref(read_json(new_dir / META_FILE))
                )
                if placed is None or (
                    placed.stat().st_size != video.media.stat().st_size
                ):
                    items.append(f"{video.media}  (media not in the new library)")
            audio_root = video.path / "audio"
            for path in video.audio:
                rel = path.relative_to(audio_root)
                if not _audio_accounted(ctx, new_dir, rel, path.stat().st_size):
                    items.append(f"{path}  (audio not in the new library)")
    return items


def _write_marker(old_root: Path, target: Path, left_behind: list[str]) -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    text = (
        "This YTOP library was migrated to the portable layout.\n"
        f"Target library: {target.resolve()}\n"
        f"Last migration run: {stamp}\n\n"
        "Media files that were moved are no longer in this folder; the JSON\n"
        "files are kept as a backup.\n\n"
    )
    if left_behind:
        text += (
            "NOT everything was migrated. Keep the items below: they are not\n"
            "in the new library. Do not remove this folder until they have\n"
            "been dealt with (rerunning the migration picks up anything that\n"
            "can be migrated).\n\n"
            + "".join(f"  {item}\n" for item in left_behind)
        )
    else:
        text += (
            "Everything was migrated. Remove this folder yourself once the\n"
            "new library has been checked.\n"
        )
    (old_root / MARKER_NAME).write_text(text, encoding="utf-8")


def run_migration(
    plan: MigrationPlan,
    target: Path,
    progress_cb: Callable[[int, int, OldPlaylist], None] | None = None,
) -> MigrationReport:
    """Carry out ``plan``: build (or resume) the library at ``target``."""
    from ytop.index import index_for
    from ytop.transcripts.store import index_library

    target = Path(target).expanduser()
    existing = read_library(target)
    if existing is not None and existing.schema > SCHEMA_VERSION:
        raise ValueError(
            f"{target} was made by a newer YTOP (schema {existing.schema}); "
            "update this app before migrating into it"
        )
    old_root = plan.old_root
    # Read (and validate) the old state before anything is written.
    progress, titles = _read_state(old_root)
    info = existing or init_library(target, target.name or "Library")
    layout = LibraryLayout(target)
    report = MigrationReport(target=target, warnings=list(plan.warnings))
    ctx = _Context(
        layout=layout,
        journal=_read_journal(target),
        progress=progress,
        titles=titles,
        report=report,
    )
    total = len(plan.playlists)
    for number, playlist in enumerate(plan.playlists, start=1):
        if progress_cb is not None:
            progress_cb(number, total, playlist)
        state = _journal_state(ctx.journal, playlist)
        if state == "done":
            report.skipped.append(playlist.id)
            continue
        reason = playlist.skip_reason or _conflict_reason(
            target, ctx.journal, playlist
        )
        if reason:
            playlist.skip_reason = reason
            report.unmigrated.append(playlist.id)
            message = f"{playlist.path}: {reason}"
            if message not in report.warnings:
                report.warnings.append(message)
            continue
        key = _source_key(playlist)
        # Recorded before the playlist's first write, so a rerun knows this
        # target playlist is ours to resume.
        ctx.journal["playlists"][key] = {"id": playlist.id, "state": "started"}
        ctx.save_journal()
        _migrate_playlist(ctx, playlist)
        ctx.journal["playlists"][key] = {"id": playlist.id, "state": "done"}
        ctx.save_journal()
        report.migrated.append(playlist.id)

    _carry_jobs(ctx, old_root, info.id, info.label)
    report.left_behind = _left_behind(ctx, plan)
    _write_marker(old_root, target, report.left_behind)
    staging = layout.cache_dir / "migrate"
    if staging.is_dir() and not any(staging.iterdir()):
        staging.rmdir()
    report.indexed = index_library(layout)
    index_for(layout).rebuild()
    return report
