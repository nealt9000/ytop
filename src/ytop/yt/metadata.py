"""Library metadata documents and their normalization.

A portable library (see ``ytop.layout``) keeps readable media apart from
hidden metadata::

    YTOP Library/<folder>/<NN - Title>.<ext>     media + <stem>.<lang>.srt
    .ytop/data/playlists/<playlist-id>/
      playlist.json            entries, "folder"
      playlist-thumb.jpg
      transcripts.json
      videos/<video-id>/
        meta.json              files.media -> path relative to "YTOP Library"
        raw.json
        thumb.jpg
        transcript.json
        subtitles/<language>.<kind>.<ext>
        audio/<format-id>.<ext>   alternate audio tracks (app-only)

``.ytop/data`` also holds ``progress.json``, ``jobs.json`` and the derived
``index.sqlite3`` transcript index.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import io
import json
import logging
import os
import re
import sys
import tempfile
import threading
import unicodedata
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image

if TYPE_CHECKING:
    from ytop.layout import LibraryLayout

log = logging.getLogger(__name__)

MEDIA_EXTENSIONS = {".webm", ".mp4", ".mkv", ".m4v", ".mov"}
THUMB_NAME = "thumb.jpg"
PLAYLIST_THUMB_NAME = "playlist-thumb.jpg"

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sanitize_component(value: str, limit: int = 90) -> str:
    """Make a stable, filesystem-safe name while keeping it readable."""
    value = unicodedata.normalize("NFKD", value or "")
    value = value.encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[\x00-\x1f/\\:*?\"<>|]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value[:limit].rstrip(" .") or "playlist")


def playlist_directory(layout: LibraryLayout, playlist: dict) -> Path:
    """The playlist's metadata directory, keyed by id (a retitle keeps it)."""
    return layout.playlist_meta_dir(str(playlist.get("id") or "unknown"))


def video_directory(layout: LibraryLayout, playlist_id: str, video_id: str) -> Path:
    return layout.video_meta_dir(playlist_id, video_id)


def human_bytes(size: float) -> str:
    size = float(size or 0)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{size:.0f} B"
        size /= 1024
    return f"{size:.1f} GiB"


@dataclass(frozen=True)
class JsonRead:
    """Outcome of reading one JSON document.

    ``status`` is one of ``ok``, ``missing``, ``malformed``, ``unreadable`` or
    ``wrong_shape``.  Callers that own user state distinguish these instead of
    treating every failure as an empty document.
    """

    data: dict | None
    status: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def corrupt(self) -> bool:
        return self.status in {"malformed", "wrong_shape"}


class CorruptDocumentError(RuntimeError):
    """A user-owned JSON document exists but cannot be safely rewritten."""

    def __init__(self, path: Path, status: str, detail: str = ""):
        self.path = Path(path)
        self.status = status
        super().__init__(
            f"{self.path} is {status}"
            + (f" ({detail})" if detail else "")
            + "; the file was left untouched. Repair or remove it first."
        )


def read_json_document(path: Path) -> JsonRead:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return JsonRead(None, "missing")
    except OSError as exc:
        return JsonRead(None, "unreadable", str(exc))
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return JsonRead(None, "malformed", str(exc))
    if not isinstance(data, dict):
        return JsonRead(None, "wrong_shape", f"top-level {type(data).__name__}")
    return JsonRead(data, "ok")


def read_json(path: Path) -> dict | None:
    """Compatibility reader: returns a dict only for a valid document."""
    doc = read_json_document(path)
    return doc.data if doc.ok else None


def read_json_strict(path: Path) -> dict | None:
    """Reader for user-owned state: a corrupt document raises instead of
    silently becoming an empty one."""
    doc = read_json_document(path)
    if doc.corrupt or doc.status == "unreadable":
        raise CorruptDocumentError(path, doc.status, doc.detail)
    return doc.data


def quarantine_document(path: Path, doc: JsonRead | None = None) -> Path | None:
    """Preserve a corrupt document beside the original, then free the name.

    Never deletes the bytes: the caller can repair or inspect the copy.
    """
    path = Path(path)
    doc = doc or read_json_document(path)
    if not (doc.corrupt or doc.status == "unreadable"):
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = path.with_name(f"{path.name}.corrupt-{stamp}")
    try:
        path.replace(target)
    except OSError as exc:  # pragma: no cover - permissions/races
        log.warning("could not quarantine %s: %s", path, exc)
        return None
    log.warning("preserved corrupt document %s as %s", path, target)
    return target


# -- cross-process document locking -----------------------------------------
_registry_lock = threading.Lock()
_thread_locks: dict[str, threading.RLock] = {}
_held: dict[str, int] = {}


def _lock_key(path: Path) -> str:
    resolved = Path(path).expanduser()
    try:
        resolved = resolved.resolve()
    except OSError:
        resolved = Path(os.path.abspath(str(resolved)))
    return str(resolved)


def lock_dir() -> Path:
    """``<instance dir>/locks`` (0700): the per-user home of the sidecars.

    It lives in the per-user instance dir (``<tmp>/ytop-<user>``, or
    ``YTOP_INSTANCE_DIR``) so two users never share one lock dir and the
    host temp dir gets nothing outside it.
    """
    from ..instance import ensure_dir  # stdlib-only module: no import cycle

    path = ensure_dir() / "locks"
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def _sidecar(key: str) -> Path:
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return lock_dir() / f"{digest}.lock"


def _same_file(fd: int, path: Path) -> bool:
    """``path`` still names the open file ``fd`` (not pruned meanwhile)."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    mine = os.fstat(fd)
    return (st.st_dev, st.st_ino) == (mine.st_dev, mine.st_ino)


def _open_locked(key: str) -> int:
    """Open and lock ``key``'s sidecar; retry if it was pruned while we
    waited for the lock (we would hold a lock nobody else can see)."""
    while True:
        path = _sidecar(key)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            _lock_fd(fd)
        except BaseException:
            os.close(fd)
            raise
        if _same_file(fd, path):
            return fd
        try:
            _unlock_fd(fd)
        finally:
            os.close(fd)


def prune_lock_sidecars() -> int:
    """Delete the sidecars nobody holds; returns how many went.

    Safe against a concurrent ``document_lock``: a sidecar is removed only
    while this process holds its lock, and a waiter that then gets the lock
    on the removed file notices (``_same_file``) and starts over.
    """
    removed = 0
    try:
        paths = sorted(lock_dir().glob("*.lock"))
    except OSError:
        return 0
    for path in paths:
        try:
            fd = os.open(path, os.O_RDWR)
        except OSError:
            continue
        try:
            if not _try_lock_fd(fd):
                continue
            if sys.platform == "win32":
                # Windows cannot delete an open file; once closed, anyone
                # who opened it since keeps it alive (the delete fails).
                _unlock_fd(fd)
                os.close(fd)
                fd = -1
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        finally:
            if fd >= 0:
                os.close(fd)  # closing drops the lock too
    return removed


if sys.platform == "win32":
    import msvcrt

    def _lock_fd(fd: int) -> None:
        """Block until the first byte of ``fd`` is locked (exclusive)."""
        os.lseek(fd, 0, os.SEEK_SET)
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                return
            except OSError as exc:
                # LK_LOCK gives up with EDEADLOCK after ~10 s; keep waiting.
                if exc.errno != errno.EDEADLOCK:
                    raise

    def _unlock_fd(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

    def _try_lock_fd(fd: int) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
else:
    import fcntl

    def _lock_fd(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def _unlock_fd(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)

    def _try_lock_fd(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True


@contextlib.contextmanager
def document_lock(path: Path):
    """Serialise one logical document mutation.

    The in-process lock is re-entrant.  A sidecar file lock (``flock``;
    ``msvcrt.locking`` on Windows) in the per-user :func:`lock_dir`
    coordinates a CLI writer and a web writer in different processes.  Global locks are never
    held across network operations; callers lock only around a read-modify-
    write of one document.
    """
    key = _lock_key(path)
    with _registry_lock:
        lock = _thread_locks.setdefault(key, threading.RLock())
    with lock:
        with _registry_lock:
            depth = _held.get(key, 0)
            _held[key] = depth + 1
        fd: int | None = None
        locked = False
        try:
            if depth == 0:
                fd = _open_locked(key)
                locked = True
            yield
        finally:
            if fd is not None:
                try:
                    if locked:
                        _unlock_fd(fd)
                finally:
                    os.close(fd)
            with _registry_lock:
                remaining = _held.get(key, 1) - 1
                if remaining <= 0:
                    _held.pop(key, None)
                else:
                    _held[key] = remaining


def write_json(path: Path, data: dict, *, durable: bool = True) -> None:
    """Atomically replace one JSON document using a unique temp file.

    A fixed ``<name>.tmp`` name let two writers truncate each other's file;
    ``mkstemp`` gives every writer its own temporary file in the destination
    directory so the final ``os.replace`` is the only shared step.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n"
    ).encode("utf-8")
    with document_lock(path):
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                if durable:
                    os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
        if durable:
            fsync_dir(path.parent)


def fsync_dir(path: Path) -> None:
    """Make a rename or replace inside ``path`` durable.

    Platforms that cannot open or fsync a directory (Windows) are ignored:
    there the rename itself is already as durable as it gets.
    """
    try:
        dir_fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def _media_ref(meta: dict | None) -> str | None:
    files = (meta or {}).get("files")
    ref = files.get("media") if isinstance(files, dict) else None
    return ref if isinstance(ref, str) and ref else None


def find_media(layout: LibraryLayout, video_dir: Path) -> Path | None:
    """The media file ``meta.json`` points at, if it exists and is non-empty."""
    meta = read_json(Path(video_dir) / "meta.json")
    path = layout.resolve_media(_media_ref(meta))
    if path is None:
        return None
    try:
        return path if path.stat().st_size > 0 else None
    except OSError:
        return None


def remove_media(layout: LibraryLayout, video_dir: Path) -> None:
    """Delete a video's media, its ``.srt`` siblings and alternate audio."""
    video_dir = Path(video_dir)
    meta_path = video_dir / "meta.json"
    meta = read_json(meta_path)
    media = layout.resolve_media(_media_ref(meta))
    if media is not None:
        prefix = media.stem + "."
        for sibling in media.parent.iterdir():
            name = sibling.name
            if (
                sibling.is_file()
                and name.startswith(prefix)
                and name.endswith(".srt")
                and "." not in name[len(prefix):-4]
            ):
                sibling.unlink(missing_ok=True)
        media.unlink(missing_ok=True)
    # Alternate audio tracks belong to the media file being removed.
    audio_dir = video_dir / "audio"
    if audio_dir.is_dir():
        for p in audio_dir.iterdir():
            if p.is_file():
                p.unlink(missing_ok=True)
        audio_dir.rmdir()
    if meta is not None and _media_ref(meta) is not None:
        meta["files"]["media"] = None
        write_json(meta_path, meta)


def _thumbnail_candidates(info: dict) -> list[str]:
    urls: list[str] = []
    thumbnails = info.get("thumbnails") or []
    # Prefer larger images, but not the 4K variants that are slow to fetch.
    usable = [
        t for t in thumbnails
        if t.get("url") and 120 <= int(t.get("width") or 0) <= 1920
    ]
    usable.sort(key=lambda t: (int(t.get("width") or 0), int(t.get("height") or 0)))
    for t in reversed(usable):
        if t["url"] not in urls:
            urls.append(t["url"])
    direct = info.get("thumbnail")
    if direct and direct not in urls:
        urls.append(direct)
    for t in thumbnails:
        url = t.get("url")
        if url and url not in urls:
            urls.append(url)
    return urls


def fetch_thumbnail(info: dict, dest: Path, timeout: int = 40) -> bool:
    """Download the best thumbnail and normalize it to JPEG."""
    for url in _thumbnail_candidates(info):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read(10 * 1024 * 1024)
            image = Image.open(io.BytesIO(data))
            tmp = Path(dest).with_name(Path(dest).name + ".tmp")
            try:
                image.convert("RGB").save(tmp, "JPEG", quality=90, optimize=True)
                tmp.replace(dest)
            finally:
                tmp.unlink(missing_ok=True)
            return True
        except Exception:  # noqa: BLE001 - try the next thumbnail URL
            continue
    return False


_DESCRIPTION_TIMESTAMP = re.compile(
    r"^(?P<stamp>(?:\d{1,2}:)?\d{1,2}:\d{2})\s*"
    r"(?:[-\u2013\u2014:|)\]]\s*)?(?P<title>.+)$"
)


def _timestamp_seconds(value: str) -> int | None:
    parts = value.split(":")
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return None
    if len(numbers) == 2:
        minutes, seconds = numbers
        return minutes * 60 + seconds
    if len(numbers) == 3:
        hours, minutes, seconds = numbers
        return hours * 3600 + minutes * 60 + seconds
    return None


def chapters_from_description(
    description: str | None, limit: int = 100
) -> list[dict]:
    """Recover a chapter list from a description's timestamp lines.

    yt-dlp exposes creator chapters for many videos, but some uploaders only
    write ``0:00 Intro`` lines in the description.  Only lines that start with
    a timestamp count, and two lines are required before this is treated as a
    chapter list, so an incidental timestamp in prose cannot fabricate one.
    """
    found: list[dict] = []
    seen: set[int] = set()
    for line in (description or "").splitlines():
        match = _DESCRIPTION_TIMESTAMP.match(line.strip())
        if not match:
            continue
        seconds = _timestamp_seconds(match.group("stamp"))
        title = match.group("title").strip(" -\u2013\u2014:|)]")
        if seconds is None or not title or seconds in seen:
            continue
        seen.add(seconds)
        found.append({
            "start_time": float(seconds),
            "end_time": None,
            "title": title[:200],
        })
        if len(found) >= limit:
            break
    if len(found) < 2:
        return []
    found.sort(key=lambda chapter: chapter["start_time"])
    for index, chapter in enumerate(found[:-1]):
        chapter["end_time"] = found[index + 1]["start_time"]
    return found


def normalize_chapters(chapters: list | None) -> list[dict]:
    out: list[dict] = []
    for chapter in chapters or []:
        if not isinstance(chapter, dict):
            continue
        title = str(chapter.get("title") or "").strip()
        start = chapter.get("start_time")
        if not title or start is None:
            continue
        out.append({
            "start_time": round(float(start), 3),
            "end_time": (
                round(float(chapter["end_time"]), 3)
                if chapter.get("end_time") is not None else None
            ),
            "title": title,
        })
    return out


def normalize_video(
    info: dict,
    *,
    index: int,
    playlist: dict,
    video_dir: Path,
    thumbnail_file: str | None,
    media_file: str | None,
    selection,
    audio_tracks: list[dict] | None = None,
    existing: dict | None = None,
) -> dict:
    existing = existing or {}
    fetched_at = now_iso()
    chapters = normalize_chapters(info.get("chapters"))
    if not chapters:
        chapters = chapters_from_description(info.get("description"))
    doc = {
        "schema": 1,
        "id": str(info.get("id") or video_dir.name),
        "title": str(info.get("title") or ""),
        "description": str(info.get("description") or ""),
        "channel": info.get("channel") or info.get("uploader"),
        "channel_id": info.get("channel_id") or info.get("uploader_id"),
        "upload_date": info.get("upload_date"),
        "timestamp": info.get("timestamp"),
        "duration": info.get("duration"),
        "webpage_url": info.get("webpage_url")
        or f"https://www.youtube.com/watch?v={info.get('id')}",
        "playlist": {
            "id": str(playlist.get("id") or ""),
            "title": playlist.get("title"),
            "index": index,
        },
        "chapters": chapters,
        "categories": list(info.get("categories") or []),
        "tags": list(info.get("tags") or []),
        "format": {
            "video_id": selection.video.get("format_id") if selection else None,
            "audio_id": selection.audio.get("format_id") if selection and selection.audio else None,
            "video_codec": selection.video_family if selection else None,
            "audio_codec": selection.audio_family if selection else None,
            "audio_bitrate": selection.audio_bitrate if selection else None,
            "audio_channels": selection.audio_channels if selection else None,
            "audio_label": selection.audio_label if selection else None,
            "audio_original": selection.audio_original if selection else None,
            "audio_auto_dubbed": selection.audio_auto_dubbed if selection else None,
            "width": selection.video.get("width") if selection else None,
            "height": selection.video.get("height") if selection else None,
            "fps": selection.video.get("fps") if selection else None,
            "container": selection.container if selection else None,
        },
        "audio_tracks": audio_tracks or (
            existing.get("audio_tracks") if existing else []
        ),
        "files": {
            "media": media_file,
            "thumbnail": thumbnail_file,
            "thumbnail_url": info.get("thumbnail"),
        },
        "first_seen_at": existing.get("first_seen_at") or fetched_at,
        "fetched_at": fetched_at,
    }
    # A sync refreshes extractor metadata; it must not erase the transcript
    # discovery block written by `ytop transcripts`.
    if existing.get("transcript"):
        doc["transcript"] = existing["transcript"]
    return doc


def normalize_playlist(
    playlist_info: dict,
    *,
    entries: list[dict],
    thumbnail_file: str | None,
    existing: dict | None = None,
    options: dict | None = None,
) -> dict:
    existing = existing or {}
    fetched_at = now_iso()
    if options is None:
        options = existing.get("options") if isinstance(
            existing.get("options"), dict
        ) else {}
    doc = {
        "schema": 1,
        "id": str(playlist_info.get("id") or ""),
        "title": str(playlist_info.get("title") or ""),
        "description": str(playlist_info.get("description") or ""),
        "channel": playlist_info.get("channel") or playlist_info.get("uploader"),
        "channel_id": playlist_info.get("channel_id") or playlist_info.get("uploader_id"),
        "webpage_url": playlist_info.get("webpage_url")
        or f"https://www.youtube.com/playlist?list={playlist_info.get('id')}",
        # A sync must not turn a locally-built playlist into a YouTube one.
        "kind": (playlist_info.get("kind") or existing.get("kind") or "youtube"),
        "video_count": len(entries),
        "entries": entries,
        "options": dict(options or {}),
        "files": {"thumbnail": thumbnail_file},
        "first_seen_at": existing.get("first_seen_at") or fetched_at,
        "updated_at": fetched_at,
    }
    # Preserve the media folder, transcript summary and manual-order marker
    # across a metadata-only sync.
    if existing.get("folder"):
        doc["folder"] = existing["folder"]
    if existing.get("transcripts"):
        doc["transcripts"] = existing["transcripts"]
    if existing.get("order"):
        doc["order"] = existing["order"]
    return doc
