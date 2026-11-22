"""Publish downloaded media under readable names, with journalled renames.

Media lives at ``YTOP Library/<folder>/<NN - Title>.<ext>``. Every rename
batch goes through ``rename-journal.json`` in two phases (``src -> tmp``, then
``tmp -> dst``), so a swap such as ``01 - A`` <-> ``02 - A`` never collides and
a batch interrupted by an unplugged stick is finished on the next start.

The hop through a temporary name is only for targets that clash with the
batch's own sources (swaps, case-only renames).  Any other op is recorded with
``tmp == dst`` and renamed straight to its target in phase 1, so a renumber
after a removal renames each file and rewrites each ``meta.json`` once rather
than twice: on a FAT stick every small file write takes a large fraction of a
second.  An older build recovering such a journal completes it correctly (it
only logs that the target already exists).

Journal paths are relative to the media root, so a stick that comes back under
a different mount point or drive letter still recovers. ``files.media`` refs
(and playlist ``folder`` values) follow each phase: ``src -> tmp`` with phase 1
and ``tmp -> dst`` with phase 2. Temporary names are unique, so re-applying
either mapping during recovery is a no-op.
"""

from __future__ import annotations

import functools
import logging
import os
import shutil
import threading
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ytop.fsnames import plan_names, safe_name
from ytop.layout import UNGROUPED_FOLDER, UNGROUPED_ID, LibraryLayout
from ytop.transcripts.store import read_transcript, segments_from_doc
from ytop.yt.metadata import (
    fsync_dir,
    quarantine_document,
    read_json,
    read_json_document,
    write_json,
)

log = logging.getLogger(__name__)

PLAYLIST_FILE = "playlist.json"
META_FILE = "meta.json"
FOLDER_THUMB = "folder.jpg"
DEFAULT_PLAYLIST_THUMB = "playlist-thumb.jpg"
TMP_PREFIX = ".ytoprename-"
PUBLISH_PREFIX = ".ytoppublish-"
# A playlist move's journal (``web.fsops``): ``<meta_root>/.ytopmove-<job>.json``.
MOVE_JOURNAL_GLOB = ".ytopmove-*.json"


# Parallel download workers publish and sync concurrently; two rename batches
# must never interleave on the one journal.  Callers that rewrite a meta.json
# ``files.media`` outside this module hold it too, so a rename cannot be undone
# by a stale write.
PUBLISH_LOCK = threading.RLock()


def _serialised[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        with PUBLISH_LOCK:
            return fn(*args, **kwargs)

    return wrapper


@dataclass
class SyncReport:
    renamed: int = 0
    srt_written: int = 0
    errors: list[str] = field(default_factory=list)


# --- library walking ---------------------------------------------------------


def _iter_playlists(layout: LibraryLayout) -> Iterator[tuple[Path, dict]]:
    if not layout.meta_root.is_dir():
        return
    for playlist_dir in sorted(layout.meta_root.iterdir()):
        if not playlist_dir.is_dir() or playlist_dir.name.startswith("."):
            continue
        path = playlist_dir / PLAYLIST_FILE
        doc = read_json(path)
        if doc is not None:
            yield path, doc


def iter_video_metas(layout: LibraryLayout) -> Iterator[tuple[str, str, Path, dict]]:
    """Yield ``(playlist_id, video_id, meta.json path, meta)`` for the library."""
    if not layout.meta_root.is_dir():
        return
    for playlist_dir in sorted(layout.meta_root.iterdir()):
        if not playlist_dir.is_dir() or playlist_dir.name.startswith("."):
            continue
        playlist = read_json(playlist_dir / PLAYLIST_FILE) or {}
        playlist_id = str(playlist.get("id") or playlist_dir.name)
        videos_dir = playlist_dir / "videos"
        if not videos_dir.is_dir():
            continue
        for video_dir in sorted(videos_dir.iterdir()):
            path = video_dir / META_FILE
            meta = read_json(path)
            if meta is None:
                continue
            yield playlist_id, str(meta.get("id") or video_dir.name), path, meta


# --- folders -----------------------------------------------------------------


def _unique_folder(layout: LibraryLayout, playlist_id: str, title: str) -> str:
    """A folder name for ``playlist_id`` that no other playlist uses, that is
    not the reserved Un-grouped folder, and that does not exist on disk (the
    playlist's own current folder excepted)."""
    name = safe_name(title, fallback=safe_name(playlist_id))
    used = {UNGROUPED_FOLDER.casefold()} if playlist_id != UNGROUPED_ID else set()
    own: str | None = None
    for _, doc in _iter_playlists(layout):
        if not doc.get("folder"):
            continue
        if str(doc.get("id")) == playlist_id:
            own = str(doc["folder"]).casefold()
        else:
            used.add(str(doc["folder"]).casefold())

    def taken(candidate: str) -> bool:
        if candidate.casefold() in used:
            return True
        if candidate.casefold() == own:
            return False
        return os.path.lexists(layout.media_root / candidate)

    if not taken(name):
        return name
    suffix = f" [{playlist_id}]"
    base = safe_name(title, limit=max(1, 100 - len(suffix) - 4))
    name = base + suffix
    counter = 2
    while taken(name):
        name = f"{base}{suffix} {counter}"
        counter += 1
    return name


def playlist_folder(layout: LibraryLayout, playlist_doc: dict) -> Path:
    """The playlist's media folder; assigns and saves ``folder`` if missing."""
    folder = playlist_doc.get("folder")
    if folder:
        return layout.media_root / str(folder)
    playlist_id = str(playlist_doc.get("id") or "")
    if playlist_id == UNGROUPED_ID:
        name = UNGROUPED_FOLDER
    else:
        title = str(playlist_doc.get("title") or playlist_id)
        name = _unique_folder(layout, playlist_id, title)
    playlist_doc["folder"] = name
    write_json(layout.playlist_meta_dir(playlist_id) / PLAYLIST_FILE, playlist_doc)
    return layout.media_root / name


# --- journal -----------------------------------------------------------------


def _map_ref(ref: str, mapping: dict[str, str]) -> str:
    for src, dst in mapping.items():
        if ref == src:
            return dst
        if ref.startswith(src + "/"):
            return dst + ref[len(src):]
    return ref


def _rewrite_refs(layout: LibraryLayout, mapping: dict[str, str]) -> None:
    """Apply a media-relative path mapping to every ref in the library."""
    if not mapping:
        return
    for _, _, path, meta in iter_video_metas(layout):
        files = meta.get("files")
        if not isinstance(files, dict):
            continue
        ref = files.get("media")
        if isinstance(ref, str) and ref:
            new = _map_ref(ref, mapping)
            if new != ref:
                files["media"] = new
                write_json(path, meta)
    for path, doc in _iter_playlists(layout):
        folder = doc.get("folder")
        if isinstance(folder, str) and folder:
            new = _map_ref(folder, mapping)
            if new != folder:
                doc["folder"] = new
                write_json(path, doc)


def _fsync_dirs(dirs: set[Path]) -> None:
    for directory in sorted(dirs):
        fsync_dir(directory)


def _safe_relative(value: object) -> bool:
    """A non-empty media-relative path that cannot leave the media root."""
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts


def _valid_ops(journal: dict) -> list[dict]:
    ops = []
    raw = journal.get("ops")
    for op in raw if isinstance(raw, list) else []:
        if isinstance(op, dict) and all(
            _safe_relative(op.get(key)) for key in ("src", "tmp", "dst")
        ):
            ops.append(op)
        else:
            log.warning("ignoring invalid rename journal entry %r", op)
    return ops


def _finish_journal(layout: LibraryLayout, journal: dict) -> int:
    """Run the remaining phases of a journal; returns renames completed."""
    base = layout.media_root
    ops = _valid_ops(journal)
    done = 0
    if journal.get("phase") == 1:
        touched: set[Path] = set()
        moved: dict[str, str] = {}
        for op in ops:
            src, tmp = base / op["src"], base / op["tmp"]
            direct = op["tmp"] == op["dst"]
            if os.path.lexists(src) and not os.path.lexists(tmp):
                tmp.parent.mkdir(parents=True, exist_ok=True)
                os.replace(src, tmp)
                touched.update((src.parent, tmp.parent))
                if direct:
                    done += 1
            elif direct and os.path.lexists(src):
                # Something appeared at dst since the batch was planned; the
                # file and its refs stay where they are.
                log.warning("rename target %s exists; left %s", tmp, src)
                continue
            moved[op["src"]] = op["tmp"]
        # The renames must be on disk before the journal says they happened.
        _fsync_dirs(touched)
        _rewrite_refs(layout, moved)
        journal["phase"] = 2
        write_json(layout.rename_journal, journal)
    completed: dict[str, str] = {}
    renamed: set[Path] = set()
    for op in ops:
        if op["tmp"] == op["dst"]:
            continue  # a direct op: finished in phase 1
        tmp, dst = base / op["tmp"], base / op["dst"]
        if os.path.lexists(tmp):
            if os.path.lexists(dst):
                # Something appeared at dst since the batch was planned; keep
                # our file under its temporary name rather than overwrite.
                log.warning("rename target %s exists; left %s", dst, tmp)
                continue
            os.replace(tmp, dst)
            renamed.update((tmp.parent, dst.parent))
            done += 1
        completed[op["tmp"]] = op["dst"]
    _fsync_dirs(renamed)
    _rewrite_refs(layout, completed)
    layout.rename_journal.unlink(missing_ok=True)
    return done


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _blocked(dst: Path, sources: list[Path]) -> bool:
    """``dst`` holds a file that this batch does not move away."""
    if not os.path.lexists(dst) or dst in sources:
        return False
    # A case-only rename on a case-insensitive volume is not a clash.
    return not any(_same_file(dst, src) for src in sources)


def _run_batch(
    layout: LibraryLayout,
    ops: list[tuple[Path, Path]],
    report: SyncReport,
    *,
    partial: bool = False,
    groups: dict[Path, int] | None = None,
) -> bool:
    """Run ``ops`` as one journalled batch; never renames onto a file.

    A clash refuses the whole batch, or with ``partial`` only the ops it
    blocks (directly, or by keeping a source in place), so one foreign file
    does not leave every other name in a playlist unsynced.  ``groups`` maps
    a source to a group number; a blocked op holds back its whole group (a
    media file and its ``.srt`` files keep matching names)."""
    groups = groups or {}
    if _finish_pending(layout):
        # A pending batch moved files after these ops were planned.
        ops = [(src, dst) for src, dst in ops if os.path.lexists(src)]
    ops = [(src, dst) for src, dst in ops if src != dst]
    if not ops:
        return True
    clean = True
    while True:
        sources = [src for src, _ in ops]
        blocked = [(src, dst) for src, dst in ops if _blocked(dst, sources)]
        if not blocked:
            break
        clean = False
        for _, dst in blocked:
            report.errors.append(
                f"not renaming onto existing file {layout.relative_media(dst)}"
            )
        if not partial:
            return False
        held = {groups[src] for src, _ in blocked if src in groups}
        ops = [
            op for op in ops
            if op not in blocked and groups.get(op[0], -1) not in held
        ]
    if not ops:
        return clean
    entries = []
    hop_needed = _hop_needed(layout, ops)
    for i, (src, dst) in enumerate(ops):
        target = layout.relative_media(dst)
        if hop_needed(dst):
            # Another op's source (or this one's, in another case) holds the
            # name: hop through a unique temporary name so nothing collides.
            tmp = dst.parent / f"{TMP_PREFIX}{i}-{uuid.uuid4().hex[:8]}{dst.suffix}"
            hop = layout.relative_media(tmp)
        else:
            hop = target  # a direct op: renamed straight to dst in phase 1
        entries.append({
            "src": layout.relative_media(src),
            "tmp": hop,
            "dst": target,
        })
    write_json(layout.rename_journal, {"phase": 1, "ops": entries})
    report.renamed += _finish_journal(layout, {"phase": 1, "ops": entries})
    return clean


def _fold(relative: str) -> tuple[str, ...]:
    """A media-relative path as case-folded parts: FAT, NTFS and APFS ignore
    case, so ``01 - a`` and ``01 - A`` are one name."""
    return PurePosixPath(relative.casefold()).parts


def _hop_needed(layout: LibraryLayout, ops: list[tuple[Path, Path]]):
    """A test for whether an op's target needs the temporary hop: it does
    when the target, or a folder above it, is one of the batch's sources
    (a swap, a name taken over from another op, a case-only rename), or
    when it is a folder that holds a source.  Any other target is free, so
    renaming straight to it cannot collide and its ref mapping stays a no-op
    when recovery applies it again."""
    sources = {_fold(layout.relative_media(src)) for src, _ in ops}
    holders = {parts[:end] for parts in sources for end in range(1, len(parts))}

    def needed(dst: Path) -> bool:
        parts = _fold(layout.relative_media(dst))
        lineage = {parts[:end] for end in range(1, len(parts) + 1)}
        return bool(lineage & sources) or parts in holders

    return needed


def _finish_pending(layout: LibraryLayout) -> int:
    doc = read_json_document(layout.rename_journal)
    if doc.data is None:
        # Keep an unreadable journal for the owner instead of letting the
        # next batch overwrite it.
        quarantine_document(layout.rename_journal, doc)
        return 0
    return _finish_journal(layout, doc.data)


def _referenced_media(
    layout: LibraryLayout, exclude: tuple[str, str] | None = None
) -> set[str]:
    refs = set()
    for playlist_id, video_id, _, meta in iter_video_metas(layout):
        if (playlist_id, video_id) == exclude:
            continue
        files = meta.get("files")
        ref = files.get("media") if isinstance(files, dict) else None
        if isinstance(ref, str) and ref:
            refs.add(ref)
    return refs


def _move_journal_media(layout: LibraryLayout) -> set[str]:
    """Media refs an interrupted playlist move still owns: the staged names
    its journal lists, which the move's reconciliation (after this sweep)
    points the moved playlist at."""
    refs: set[str] = set()
    try:
        journals = sorted(layout.meta_root.glob(MOVE_JOURNAL_GLOB))
    except OSError:
        return refs
    for path in journals:
        doc = read_json(path)
        if not isinstance(doc, dict) or not doc.get("dest_folder"):
            continue
        names = doc.get("refs")
        if not isinstance(names, dict):
            continue
        for name in names.values():
            if isinstance(name, str) and name:
                refs.add(f"{doc['dest_folder']}/{name}")
    return refs


def _is_referenced(relative: str, referenced: set[str], stems: set[str]) -> bool:
    """A referenced media file, or one of its ``<stem>.<lang>.srt`` siblings."""
    if relative in referenced:
        return True
    if relative.endswith(".srt"):
        stem = relative[: -len(".srt")].rpartition(".")[0]
        return stem in stems
    return False


def _sweep_publish_orphans(layout: LibraryLayout) -> None:
    """Move unreferenced ``.ytoppublish-*`` files (a crash between the move
    and the meta.json write) into ``cache/orphans``; never delete them.

    Names an interrupted playlist move journals count as referenced: this
    sweep runs at startup before moves are reconciled."""
    if not layout.media_root.is_dir():
        return
    orphans = [
        path
        for path in layout.media_root.rglob(f"{PUBLISH_PREFIX}*")
        if path.is_file()
    ]
    if not orphans:
        return
    referenced = _referenced_media(layout) | _move_journal_media(layout)
    stems = {ref.rpartition(".")[0] for ref in referenced if "." in ref}
    target_dir = layout.cache_dir / "orphans"
    for path in orphans:
        if _is_referenced(layout.relative_media(path), referenced, stems):
            continue
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / path.name
        while os.path.lexists(target):
            target = target_dir / f"{uuid.uuid4().hex[:8]}-{path.name}"
        shutil.move(path, target)
        log.warning("moved orphaned download %s to %s", path, target_dir)


@_serialised
def recover_rename_journal(layout: LibraryLayout) -> int:
    """Complete any pending rename batch; returns the renames completed.

    Also moves orphaned ``.ytoppublish-*`` files aside. Call it at startup,
    not while downloads may be publishing.
    """
    done = _finish_pending(layout)
    _sweep_publish_orphans(layout)
    return done


# --- subtitles ---------------------------------------------------------------


def _srt_time(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, rest = divmod(total_ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, ms = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def transcript_to_srt(doc: dict) -> str:
    blocks = []
    for number, segment in enumerate(segments_from_doc(doc), start=1):
        blocks.append(
            f"{number}\n{_srt_time(segment.start)} --> {_srt_time(segment.end)}\n"
            f"{segment.text}\n\n"
        )
    return "".join(blocks)


def _srt_siblings(media: Path) -> list[tuple[Path, str]]:
    prefix = media.stem + "."
    found = []
    for path in sorted(media.parent.iterdir()):
        name = path.name
        if path.is_file() and name.startswith(prefix) and name.endswith(".srt"):
            lang = name[len(prefix):-4]
            if lang and "." not in lang:
                found.append((path, lang))
    return found


def _write_text_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".ytopsrt-{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# --- sync --------------------------------------------------------------------


def _owned_media(layout: LibraryLayout, folder_rel: str, meta: dict) -> Path | None:
    files = meta.get("files")
    ref = files.get("media") if isinstance(files, dict) else None
    if not isinstance(ref, str) or not ref.startswith(folder_rel + "/"):
        return None
    return layout.resolve_media(ref)


@_serialised
def sync_playlist_files(layout: LibraryLayout, playlist_id: str) -> SyncReport:
    """Bring owned media, srt files and ``folder.jpg`` in line with the playlist."""
    report = SyncReport()
    _finish_pending(layout)
    playlist_dir = layout.playlist_meta_dir(playlist_id)
    doc = read_json(playlist_dir / PLAYLIST_FILE)
    if doc is None:
        report.errors.append(f"playlist {playlist_id} has no playlist.json")
        return report
    folder = playlist_folder(layout, doc)
    folder_rel = layout.relative_media(folder)
    overrides = (read_json(layout.titles_file) or {}).get("videos") or {}

    rows: list[tuple[str, str]] = []
    metas: dict[str, dict] = {}
    for entry in doc.get("entries") or []:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        video_id = str(entry["id"])
        meta = read_json(layout.video_meta_dir(playlist_id, video_id) / META_FILE)
        metas[video_id] = meta or {}
        title = (
            overrides.get(video_id)
            or metas[video_id].get("title")
            or entry.get("title")
            or video_id
        )
        rows.append((video_id, str(title)))
    stems = plan_names(rows, numbered=playlist_id != UNGROUPED_ID)

    ops: list[tuple[Path, Path]] = []
    groups: dict[Path, int] = {}
    scheduled: set[Path] = set()
    for video_id, _ in rows:
        media = _owned_media(layout, folder_rel, metas[video_id])
        if media is None or media in scheduled:
            # Two videos sharing one file: the first one names it.
            continue
        scheduled.add(media)
        stem = stems[video_id]
        target = folder / f"{stem}{media.suffix}"
        if media == target:
            continue
        ops.append((media, target))
        groups[media] = len(scheduled)
        for srt, lang in _srt_siblings(media):
            ops.append((srt, folder / f"{stem}.{lang}.srt"))
            groups[srt] = len(scheduled)
    _run_batch(layout, ops, report, partial=True, groups=groups)

    captioned: set[Path] = set()
    for video_id, _ in rows:
        video_dir = layout.video_meta_dir(playlist_id, video_id)
        media = _owned_media(layout, folder_rel, read_json(video_dir / META_FILE) or {})
        if media is None or media in captioned:
            continue
        captioned.add(media)
        transcript = read_transcript(video_dir)
        if transcript is None:
            continue
        lang = safe_name(str(transcript.get("language") or "en"), fallback="en")
        srt = media.with_name(f"{media.stem}.{lang}.srt")
        source = video_dir / "transcript.json"
        if srt.exists() and source.stat().st_mtime <= srt.stat().st_mtime:
            continue
        text = transcript_to_srt(transcript)
        if text:
            _write_text_atomic(srt, text)
            report.srt_written += 1

    playlist_files = doc.get("files")
    thumb_name = (
        playlist_files.get("thumbnail") if isinstance(playlist_files, dict) else None
    )
    thumb = playlist_dir / str(thumb_name or DEFAULT_PLAYLIST_THUMB)
    cover = folder / FOLDER_THUMB
    if folder.is_dir() and thumb.is_file() and not cover.exists():
        shutil.copyfile(thumb, cover)
    return report


@_serialised
def publish_download(
    layout: LibraryLayout, playlist_id: str, video_id: str, staged: Path
) -> Path:
    """Move a finished download into its playlist folder and name it."""
    _finish_pending(layout)
    playlist_path = layout.playlist_meta_dir(playlist_id) / PLAYLIST_FILE
    doc = read_json(playlist_path)
    if doc is None:
        raise FileNotFoundError(playlist_path)
    folder = playlist_folder(layout, doc)
    folder.mkdir(parents=True, exist_ok=True)
    staged = Path(staged)
    temp = folder / f"{PUBLISH_PREFIX}{uuid.uuid4().hex[:8]}{staged.suffix}"
    shutil.move(staged, temp)

    meta_path = layout.video_meta_dir(playlist_id, video_id) / META_FILE
    meta = read_json(meta_path) or {"id": video_id}
    previous = _owned_media(layout, layout.relative_media(folder), meta)
    files = meta.get("files")
    if not isinstance(files, dict):
        files = meta["files"] = {}
    files["media"] = layout.relative_media(temp)
    write_json(meta_path, meta)

    if previous is not None and previous != temp:
        others = _referenced_media(layout, exclude=(playlist_id, video_id))
        if layout.relative_media(previous) not in others:
            # The replaced download; it would otherwise block the new name.
            for srt, _ in _srt_siblings(previous):
                srt.unlink(missing_ok=True)
            previous.unlink(missing_ok=True)

    report = sync_playlist_files(layout, playlist_id)
    for error in report.errors:
        log.warning("publish %s/%s: %s", playlist_id, video_id, error)
    final_files = (read_json(meta_path) or {}).get("files")
    final = final_files.get("media") if isinstance(final_files, dict) else None
    return layout.media_root / final if final else temp


@_serialised
def transfer_media(
    layout: LibraryLayout, relative_media: str, playlist_id: str
) -> None:
    """Move a shared media file (and its ``.srt`` files) into another
    playlist's folder, making that playlist its owner.

    The move is one journalled batch, so every ref follows it (and an
    interrupted move is finished on the next start); the new owner is then
    synced, which gives the file its numbered name there.
    """
    _finish_pending(layout)
    media = layout.resolve_media(relative_media)
    if media is None or not os.path.lexists(media):
        raise FileNotFoundError(relative_media)
    playlist_path = layout.playlist_meta_dir(playlist_id) / PLAYLIST_FILE
    doc = read_json(playlist_path)
    if doc is None:
        raise FileNotFoundError(playlist_path)
    folder = playlist_folder(layout, doc)
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{PUBLISH_PREFIX}{uuid.uuid4().hex[:8]}"
    ops = [(media, folder / f"{stem}{media.suffix}")]
    ops += [(srt, folder / f"{stem}.{lang}.srt") for srt, lang in _srt_siblings(media)]
    report = SyncReport()
    if not _run_batch(layout, ops, report):
        raise OSError("; ".join(report.errors))
    report = sync_playlist_files(layout, playlist_id)
    for error in report.errors:
        log.warning("transfer %s to %s: %s", relative_media, playlist_id, error)


@_serialised
def rename_folder(
    layout: LibraryLayout, playlist_id: str, new_title: str
) -> SyncReport:
    """Rename a playlist's media folder and repoint refs in every playlist."""
    report = SyncReport()
    _finish_pending(layout)
    playlist_path = layout.playlist_meta_dir(playlist_id) / PLAYLIST_FILE
    doc = read_json(playlist_path)
    if doc is None:
        report.errors.append(f"playlist {playlist_id} has no playlist.json")
        return report
    if playlist_id == UNGROUPED_ID:
        report.errors.append("the Un-grouped folder cannot be renamed")
        return report
    old = playlist_folder(layout, doc)
    new = layout.media_root / _unique_folder(layout, playlist_id, new_title)
    if new == old:
        return report
    if os.path.lexists(old):
        _run_batch(layout, [(old, new)], report)
    else:
        doc["folder"] = new.name
        write_json(playlist_path, doc)
    return report
