"""Per-library SQLite index of playlists, videos and derived tags.

The tables live in the library's existing ``index.sqlite3`` next to the
transcript search tables (which this module never changes).  Like the rest of
that file the index is derived and disposable: it is rebuilt from
``playlist.json``/``meta.json`` whenever it is missing, stale or damaged.

A read-only library (a newer schema, a locked stick, a read-only mount, a
folder without write permission) gets an in-memory index for the life of the
process, so the stick is never written.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import html
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from .layout import LibraryLayout, safe_id
from .tags import Tag, TagInput, derive_tags, playlist_category
from .yt.metadata import find_media, read_json

log = logging.getLogger(__name__)

T = TypeVar("T")

INDEX_SCHEMA = 2

# FAT32 stores modification times to the nearest 2 seconds, so a playlist.json
# rewritten within 2 s of being indexed can keep the same (mtime, size).  Such
# a row is re-checked against the file's hash (the "racy git" rule).
MTIME_SLACK_NS = 2_000_000_000

# How long a call waits for another process's lock on the database.  Writes
# wait long (a skipped write is retried, but later); reads answer from their
# last result instead of holding a page up.
WRITE_TIMEOUT_S = 30.0
READ_TIMEOUT_S = 2.0

# OSError numbers that mean "this place cannot be written".
_UNWRITABLE_ERRNOS = frozenset({errno.EROFS, errno.EACCES, errno.EPERM})

PLAYLIST_FILE = "playlist.json"
META_FILE = "meta.json"
TRANSCRIPT_FILE = "transcript.json"
CORRUPT_SUFFIX = ".corrupt"

# Every idx_* table that a schema change drops; idx_video_extra survives it
# because it caches the (slow) raw.json backfill.
_DERIVED_TABLES = (
    "idx_playlists",
    "idx_videos",
    "idx_tags",
    "idx_categories",
    "idx_video_fts",
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS idx_playlists (
    playlist_id     TEXT PRIMARY KEY,
    title           TEXT,
    channel         TEXT,
    description     TEXT,
    kind            TEXT,
    updated_at      TEXT,
    has_thumb       INTEGER,
    meta_bytes      INTEGER,
    source_mtime_ns INTEGER,
    source_size     INTEGER,
    source_hash     TEXT,
    indexed_ns      INTEGER
);
CREATE TABLE IF NOT EXISTS idx_videos (
    playlist_id    TEXT NOT NULL,
    video_id       TEXT NOT NULL,
    position       INTEGER,
    title          TEXT,
    channel        TEXT,
    description    TEXT,
    duration       REAL,
    upload_date    TEXT,
    media_ref      TEXT,
    media_bytes    INTEGER,
    available      INTEGER,
    has_transcript INTEGER,
    categories     TEXT,
    yt_tags        TEXT,
    PRIMARY KEY (playlist_id, video_id)
);
CREATE TABLE IF NOT EXISTS idx_tags (
    playlist_id TEXT NOT NULL,
    position    INTEGER,
    key         TEXT,
    label       TEXT
);
CREATE INDEX IF NOT EXISTS idx_tags_playlist ON idx_tags (playlist_id);
CREATE TABLE IF NOT EXISTS idx_categories (
    playlist_id TEXT PRIMARY KEY,
    category    TEXT
);
CREATE TABLE IF NOT EXISTS idx_video_extra (
    playlist_id  TEXT NOT NULL,
    video_id     TEXT NOT NULL,
    raw_mtime_ns INTEGER,
    categories   TEXT,
    yt_tags      TEXT,
    PRIMARY KEY (playlist_id, video_id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS idx_video_fts USING fts5(
    title,
    description,
    channel,
    playlist_title,
    video_id UNINDEXED,
    playlist_id UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


@dataclass(frozen=True)
class MetaHit:
    playlist_id: str
    video_id: str
    field: str  # "title", "description", "channel" or "playlist"
    html: str  # escaped snippet with <mark>
    rank: float  # bm25, lower is better
    playlist_match: bool = False  # the playlist title matched, whatever `field` is


@dataclass(frozen=True)
class VideoRow:
    playlist_id: str
    video_id: str
    position: int
    title: str
    channel: str
    description: str
    duration: float | None
    upload_date: str
    media_ref: str
    media_bytes: int
    available: bool
    has_transcript: bool
    categories: tuple[str, ...]
    yt_tags: tuple[str, ...]


@dataclass(frozen=True)
class PlaylistRow:
    playlist_id: str
    title: str
    channel: str
    description: str
    kind: str
    updated_at: str
    has_thumb: bool
    meta_bytes: int
    videos: tuple[VideoRow, ...]


@dataclass(frozen=True)
class Derived:
    category: str
    tags: tuple[Tag, ...]


_INDEXES: dict[tuple[str, bool], LibraryIndex] = {}
_INDEXES_LOCK = threading.Lock()


def index_for(layout: LibraryLayout, *, read_only: bool = False) -> LibraryIndex:
    """The shared index for one library, so every caller shares one lock."""
    key = (str(layout.root), read_only)
    with _INDEXES_LOCK:
        index = _INDEXES.get(key)
        if index is None:
            index = _INDEXES[key] = LibraryIndex(layout, read_only=read_only)
        return index


def refresh_video_quietly(
    layout: LibraryLayout, playlist_id: str, video_id: str
) -> None:
    """Bring one video's index rows up to date after its files changed.

    The index is disposable, so this never fails its caller (a download or
    a transcript fetch).
    """
    try:
        index_for(layout).refresh_video(playlist_id, video_id)
    except Exception as exc:  # noqa: BLE001
        log.debug("library index refresh of %s/%s failed: %s",
                  playlist_id, video_id, exc)


# -- small helpers --------------------------------------------------------------


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _playlist_title(doc: dict, playlist_id: str) -> str:
    return _text(doc.get("title")) or playlist_id


def _str_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if isinstance(item, (str, int, float)))


def _duration(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _json_tuple(text: object) -> tuple[str, ...]:
    if not isinstance(text, str) or not text:
        return ()
    try:
        return _str_tuple(json.loads(text))
    except ValueError:
        return ()


def _source_stat(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _file_hash(path: Path) -> str:
    try:
        return hashlib.sha1(path.read_bytes(), usedforsecurity=False).hexdigest()
    except OSError:
        return ""


def _read_playlist(path: Path) -> tuple[dict | None, str]:
    """``playlist.json`` parsed, with the hash of the very bytes parsed (a
    write between two separate reads could pair one version's rows with the
    other's hash, and the racy-mtime check would then trust stale rows)."""
    try:
        data = path.read_bytes()
    except OSError:
        return None, ""
    digest = hashlib.sha1(data, usedforsecurity=False).hexdigest()
    try:
        doc = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, digest
    return (doc if isinstance(doc, dict) else None), digest


def _racy(mtime_ns: int | None, indexed_ns: int | None) -> bool:
    """The file's mtime is too close to when it was indexed to trust."""
    if mtime_ns is None or indexed_ns is None:
        return True
    return abs(indexed_ns - mtime_ns) < MTIME_SLACK_NS


def _tree_bytes(path: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(dirpath, name)).st_size
    return total


def _has_thumb(meta_dir: Path, doc: dict) -> bool:
    files = doc.get("files")
    name = files.get("thumbnail") if isinstance(files, dict) else None
    if not isinstance(name, str) or not name or Path(name).name != name:
        return False
    return (meta_dir / name).is_file()


def _folder_for(playlist_id: str) -> str | None:
    try:
        return safe_id(playlist_id)
    except ValueError:
        return None


def _none() -> None:
    return None


def _is_corruption(exc: BaseException) -> bool:
    """A damaged file, as opposed to a lock or a bug in the caller.

    ``OperationalError`` ("database is locked", "unable to open") says nothing
    about the file's contents, and ``ProgrammingError``/``InterfaceError``/
    ``NotSupportedError`` are code bugs: none of them may rename a database.
    """
    return type(exc) is sqlite3.DatabaseError or isinstance(
        exc, sqlite3.IntegrityError
    )


# -- the index ------------------------------------------------------------------


class LibraryIndex:
    def __init__(self, layout: LibraryLayout, *, read_only: bool = False):
        self.layout = layout
        self.read_only = read_only
        # Re-entrant: refresh_video falls through to refresh_playlist, and a
        # damaged database is rebuilt from inside the call that found it.
        self._lock = threading.RLock()
        self._memory: sqlite3.Connection | None = None
        # Set when the file cannot be used: a damaged file that could not be
        # moved aside (on Windows another open handle blocks the rename), or a
        # library that cannot be written (a locked stick, a read-only mount,
        # no write permission).  This process then indexes in memory and
        # leaves the file alone.
        self._memory_fallback = False
        self._last_playlists: list[PlaylistRow] = []
        self._last_derived: dict[str, Derived] = {}
        # Refreshes a locked database refused, in the order asked for.
        self._pending_refresh: dict[tuple[str, str | None], None] = {}
        # Where the raw.json backfill carries on from, and whether the last
        # backfill_tags() call stopped (at its budget) with videos left.
        self._backfill_after: tuple[str, str] | None = None
        self.backfill_pending = False
        # Completed full rebuilds, and how many playlists the last one found.
        self._rebuilds = 0
        self._rebuilt_count = 0

    # -- connections ----------------------------------------------------------

    @property
    def _in_memory(self) -> bool:
        return self.read_only or self._memory_fallback

    def _open(self, timeout: float | None = None) -> sqlite3.Connection:
        if self._in_memory:
            if self._memory is None:
                conn = sqlite3.connect(":memory:", check_same_thread=False)
                conn.row_factory = sqlite3.Row
                self._ensure_schema(conn)
                self._memory = conn
            return self._memory
        path = self.layout.index_db
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            path, timeout=WRITE_TIMEOUT_S if timeout is None else timeout
        )
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=DELETE")
            self._ensure_schema(conn)
        except BaseException:
            conn.close()
            raise
        return conn

    @contextlib.contextmanager
    def _session(
        self, timeout: float | None = None
    ) -> Iterator[sqlite3.Connection]:
        """The lock plus a connection whose schema is current."""
        with self._lock:
            conn = self._open(timeout)
            try:
                yield conn
            finally:
                if conn is not self._memory:
                    conn.close()

    def _guarded(
        self,
        op: Callable[[sqlite3.Connection], T],
        default: Callable[[], T],
        *,
        timeout: float | None = None,
    ) -> T:
        """Run ``op`` in a session.

        A library that cannot be written switches to the in-memory index for
        the rest of the process, and ``op`` is retried once.  A damaged
        database (found on open, at the schema check or mid-query) is set
        aside and rebuilt, and ``op`` retried once.  A locked database, or a
        second failure, logs and returns ``default()``.
        """
        with self._lock:
            for attempt in (1, 2):
                try:
                    with self._session(timeout) as conn:
                        return op(conn)
                except (sqlite3.DatabaseError, OSError) as exc:
                    if attempt == 1 and not self._in_memory and self._unwritable(exc):
                        log.warning(
                            "library index %s cannot be written (%s); indexing "
                            "in memory until restart", self.layout.index_db, exc,
                        )
                        self._memory_fallback = True
                        try:
                            with self._session() as conn:
                                self._rebuild(conn)
                        except sqlite3.Error as again:
                            log.warning("in-memory index failed: %s", again)
                            return default()
                        continue
                    if not isinstance(exc, sqlite3.DatabaseError):
                        if isinstance(exc, OSError) and exc.errno in _UNWRITABLE_ERRNOS:
                            # Refused, but the library is writable: busy
                            # (a Windows sharing violation, a stick being
                            # replugged).  Try the file again next call.
                            log.warning(
                                "library index %s unavailable: %s",
                                self.layout.index_db, exc,
                            )
                            return default()
                        raise
                    if attempt == 1 and _is_corruption(exc) and not self._in_memory:
                        try:
                            self._recover(exc)
                        except (sqlite3.Error, OSError) as again:
                            log.warning(
                                "library index %s could not be rebuilt: %s",
                                self.layout.index_db, again,
                            )
                            return default()
                        continue
                    log.warning(
                        "library index %s unavailable: %s", self.layout.index_db, exc
                    )
                    return default()
            return default()  # pragma: no cover - the loop always returns

    def _unwritable(self, exc: BaseException) -> bool:
        """``exc`` means the index file (or its folder) cannot be written.

        A locked stick or read-only mount, or a folder without write
        permission, as opposed to a busy lock or a damaged file.  Only
        SQLite's own "readonly" error and EROFS are taken at their word;
        anything else that could be passing ("unable to open", EACCES, EPERM,
        "disk I/O error") must be confirmed by the file system, so one
        failed open does not move a healthy library into memory for good.
        """
        if isinstance(exc, OSError):
            if exc.errno == errno.EROFS:
                return True
            return exc.errno in _UNWRITABLE_ERRNOS and self._cannot_write()
        if not isinstance(exc, sqlite3.OperationalError):
            return False
        message = str(exc).casefold()
        if "readonly" in message or "read-only" in message:
            return True
        return self._cannot_write()

    def _cannot_write(self) -> bool:
        """The file system says the index file, or the folder that holds (or
        would hold) it, is not writable."""
        db = self.layout.index_db
        try:
            if db.exists() and not os.access(db, os.W_OK):
                return True
            folder = db.parent
            while not folder.exists() and folder.parent != folder:
                folder = folder.parent
            return not os.access(folder, os.W_OK)
        except OSError:
            return False

    def _recover(self, exc: sqlite3.DatabaseError) -> None:
        """Set a damaged database aside and rebuild everything it held."""
        path = self.layout.index_db
        log.warning("library index %s is damaged (%s); rebuilding it", path, exc)
        journal = path.with_name(path.name + "-journal")
        journal_aside = journal.with_name(journal.name + CORRUPT_SUFFIX)
        # The journal goes first: a hot journal must never be paired with
        # the fresh file, so the database moves only once its journal has.
        moved_journal = False
        try:
            os.replace(journal, journal_aside)
            moved_journal = True
        except FileNotFoundError:
            pass
        except OSError as move_error:
            self._recover_in_memory(path, move_error)
            return
        try:
            os.replace(path, path.with_name(path.name + CORRUPT_SUFFIX))
        except FileNotFoundError:
            pass
        except OSError as move_error:
            if moved_journal:
                # Keep the pair together for whoever opens the file next.
                with contextlib.suppress(OSError):
                    os.replace(journal_aside, journal)
            self._recover_in_memory(path, move_error)
            return
        with self._session() as conn:
            self._rebuild(conn)
        from .transcripts.store import index_library  # lazy: layout -> yt -> store

        index_library(self.layout)

    def _recover_in_memory(self, path: Path, move_error: OSError) -> None:
        log.warning(
            "cannot move %s aside (%s); indexing in memory until restart",
            path, move_error,
        )
        self._memory_fallback = True
        with self._session() as conn:
            self._rebuild(conn)

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS idx_meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        row = conn.execute("SELECT value FROM idx_meta WHERE key = 'schema'").fetchone()
        if row is None or row[0] != str(INDEX_SCHEMA):
            for table in _DERIVED_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            conn.execute("DELETE FROM idx_meta")
            conn.executescript(_SCHEMA_SQL)
            conn.executemany(
                "INSERT INTO idx_meta (key, value) VALUES (?, ?)",
                [("schema", str(INDEX_SCHEMA)), ("tags_dirty", "1")],
            )
            conn.commit()
        else:
            conn.executescript(_SCHEMA_SQL)

    # -- writes ---------------------------------------------------------------

    SYNC_BATCH_S = 0.5

    def ensure_fresh(self) -> int:
        """Re-index playlists whose ``playlist.json`` changed; returns how many.

        Works in batches of about ``SYNC_BATCH_S`` seconds, each its own
        transaction with the lock released in between, so a first build of a
        large library never holds up pages or download hooks for long.
        """
        self._retry_pending()
        dirs = self._playlist_dirs()
        changed = 0
        seen: set[str] = set()
        if dirs is not None:
            start = 0
            while start < len(dirs):
                step = self._guarded(
                    lambda conn: self._sync_batch(conn, dirs, start, seen), _none
                )
                if step is None:
                    return changed  # locked or unusable: try again next time
                start, done = step
                changed += done
                if start < len(dirs):
                    time.sleep(0.01)  # an RLock is not fair: let waiting callers in
            changed += self._guarded(lambda conn: self._drop_missing(conn, seen), lambda: 0)
        self._guarded(lambda conn: self._derive_if_needed(conn, changed), _none)
        return changed

    def rebuild(self) -> int:
        """Re-index every playlist from scratch; returns how many."""
        generation = self._rebuilds

        def op(conn: sqlite3.Connection) -> int:
            if self._rebuilds != generation:
                # Recovering from a damaged (or unwritable) file has just
                # rebuilt everything: once is enough.
                return self._rebuilt_count
            return self._rebuild(conn)

        return self._guarded(op, lambda: 0)

    def refresh_playlist(self, playlist_id: str) -> None:
        self._refresh(playlist_id, None)

    def refresh_video(self, playlist_id: str, video_id: str) -> None:
        self._refresh(playlist_id, video_id)

    def _refresh(self, playlist_id: str, video_id: str | None) -> None:
        """Refresh now, or (if the database is locked or unusable) remember
        it for the next write or sync.  Only ``playlist.json`` changes are
        found by a sync, so a skipped refresh would otherwise leave a
        downloaded video looking missing until its playlist next changed."""
        with self._lock:
            if self._retry_pending() and self._run_refresh(playlist_id, video_id):
                return
            self._pending_refresh[(playlist_id, video_id)] = None

    def _run_refresh(self, playlist_id: str, video_id: str | None) -> bool:
        def op(conn: sqlite3.Connection) -> bool:
            if video_id is None:
                self._refresh_playlist(conn, playlist_id)
            else:
                self._refresh_video(conn, playlist_id, video_id)
            return True

        return self._guarded(op, lambda: False)

    def _retry_pending(self) -> bool:
        """Run refreshes skipped earlier; False while the database is still
        unavailable (the rest are kept for later)."""
        with self._lock:
            for key in list(self._pending_refresh):
                try:
                    done = self._run_refresh(*key)
                except Exception as exc:  # noqa: BLE001 - never retry a bug forever
                    log.warning("library index refresh of %s failed: %s", key, exc)
                    done = True
                if not done:
                    return False
                self._pending_refresh.pop(key, None)
            return True

    def drop_playlist(self, playlist_id: str) -> None:
        self._guarded(lambda conn: self._drop_playlist(conn, playlist_id), _none)

    BACKFILL_BATCH = 25
    BACKFILL_BATCH_S = 0.5

    def backfill_tags(self, budget_s: float | None = None) -> int:
        """Read ``categories``/``tags`` from ``raw.json`` for videos whose
        ``meta.json`` predates them; returns how many were read in this call.

        Works in small batches, each its own transaction with the lock
        released in between, so readers and download hooks are never held up
        for long.  Stops once ``budget_s`` seconds have passed (the rest wait
        for the next call).  Never writes ``meta.json``, and does nothing while
        the index is in memory (a read-only library): a ``raw.json`` averages
        megabytes, too slow to re-read for every video at each launch.
        """
        if self._in_memory:
            self.backfill_pending = False
            return 0
        deadline = None if budget_s is None else time.monotonic() + budget_s
        total = 0
        self.backfill_pending = True
        # A positive budget always gets one batch (of at least one video),
        # so a caller looping on backfill_pending always progresses.
        first = budget_s is None or budget_s > 0
        while first or deadline is None or time.monotonic() < deadline:
            first = False
            changed, more = self._guarded(
                lambda conn: self._backfill_batch(conn, deadline), lambda: (0, False)
            )
            total += changed
            if not more or self._in_memory:
                # Finished (or the database is unavailable: try next launch).
                self.backfill_pending = False
                break
            time.sleep(0.01)  # an RLock is not fair: let waiting callers in
        return total

    BACKFILL_SCAN = 256

    def _backfill_batch(
        self, conn: sqlite3.Connection, deadline: float | None
    ) -> tuple[int, bool]:
        """One batch: (videos whose tags were read, whether any are left).

        Carries on from where the last batch stopped, so a call reads the
        list of untagged videos once, not once per batch.
        """
        stop = time.monotonic() + self.BACKFILL_BATCH_S
        if deadline is not None:
            stop = min(stop, deadline)
        processed = changed = 0
        more = first = True
        while more and processed < self.BACKFILL_BATCH and (
            first or time.monotonic() < stop
        ):
            first = False
            # Only videos the index shows without tags can need a backfill;
            # the raw.json mtime then says whether it was already looked at.
            after = self._backfill_after or ("", "")
            rows = conn.execute(
                "SELECT v.playlist_id, v.video_id, e.raw_mtime_ns "
                "FROM idx_videos v "
                "LEFT JOIN idx_video_extra e ON e.playlist_id = v.playlist_id "
                "AND e.video_id = v.video_id "
                "WHERE (v.yt_tags IS NULL OR v.yt_tags IN ('', '[]')) "
                "AND (v.playlist_id, v.video_id) > (?, ?) "
                "ORDER BY v.playlist_id, v.video_id LIMIT ?",
                (*after, self.BACKFILL_SCAN),
            ).fetchall()
            if len(rows) < self.BACKFILL_SCAN:
                more = False
            done, found, last = self._backfill_rows(conn, rows, stop)
            processed += done
            changed += found
            if rows and last != (rows[-1][0], rows[-1][1]):
                more = True  # stopped part-way through this chunk
            if last is not None:
                self._backfill_after = last
        if not more:
            self._backfill_after = None  # the next call starts over
        if changed:
            self._set_dirty(conn)
        conn.commit()
        return changed, more

    def _backfill_rows(
        self, conn: sqlite3.Connection, rows: list, stop: float
    ) -> tuple[int, int, tuple[str, str] | None]:
        """Backfill ``rows`` until the batch is full or its time is up:
        (videos looked at, videos whose tags were read, last row handled)."""
        processed = changed = 0
        last: tuple[str, str] | None = None
        for playlist_id, video_id, done_mtime in rows:
            if processed >= self.BACKFILL_BATCH:
                break
            # At least one video per batch, so even a tiny budget progresses.
            if processed and time.monotonic() >= stop:
                break
            last = (playlist_id, video_id)
            try:
                video_dir = self.layout.video_meta_dir(playlist_id, video_id)
            except ValueError:
                continue
            raw_path = video_dir / "raw.json"
            stat = _source_stat(raw_path)
            if stat is None or done_mtime == stat[0]:
                continue
            processed += 1
            meta = read_json(video_dir / META_FILE) or {}
            categories: list[str] = []
            yt_tags: list[str] = []
            if "tags" not in meta:
                raw = read_json(raw_path)
                if raw is not None:
                    categories = list(_str_tuple(raw.get("categories")))
                    yt_tags = list(_str_tuple(raw.get("tags")))
                    changed += 1
            # Recorded even when nothing was found, so it is not read again.
            conn.execute(
                "INSERT OR REPLACE INTO idx_video_extra "
                "(playlist_id, video_id, raw_mtime_ns, categories, yt_tags) "
                "VALUES (?, ?, ?, ?, ?)",
                (playlist_id, video_id, stat[0],
                 json.dumps(categories), json.dumps(yt_tags)),
            )
            if yt_tags or categories:
                conn.execute(
                    "UPDATE idx_videos SET categories = ?, yt_tags = ? "
                    "WHERE playlist_id = ? AND video_id = ?",
                    (json.dumps(categories), json.dumps(yt_tags),
                     playlist_id, video_id),
                )
        return processed, changed, last

    def _derive_if_needed(self, conn: sqlite3.Connection, changed: int) -> None:
        if changed or self._tags_dirty(conn):
            self._derive(conn)
        conn.commit()

    def _refresh_playlist(self, conn: sqlite3.Connection, playlist_id: str) -> None:
        meta_dir = self.layout.playlist_meta_dir(playlist_id)
        path = meta_dir / PLAYLIST_FILE
        stat = _source_stat(path)
        doc, digest = _read_playlist(path)
        self._delete_playlist(conn, playlist_id)
        if stat is not None and doc is not None:
            self._index_playlist(conn, meta_dir, doc, stat, digest)
        self._set_dirty(conn)
        conn.commit()

    def _refresh_video(
        self, conn: sqlite3.Connection, playlist_id: str, video_id: str
    ) -> None:
        known = conn.execute(
            "SELECT 1 FROM idx_playlists WHERE playlist_id = ?", (playlist_id,)
        ).fetchone()
        if known is None:
            self._refresh_playlist(conn, playlist_id)
            return
        meta_dir = self.layout.playlist_meta_dir(playlist_id)
        playlist = read_json(meta_dir / PLAYLIST_FILE) or {}
        entries = [e for e in playlist.get("entries") or [] if isinstance(e, dict)]
        position, entry = next(
            (
                (number, e)
                for number, e in enumerate(entries, start=1)
                if str(e.get("id") or "") == video_id
            ),
            (0, None),
        )
        self._delete_video(conn, playlist_id, video_id)
        if entry is not None:
            extra = self._extras(conn, playlist_id)
            row = self._video_row(playlist_id, playlist, entry, position, extra)
            if row is not None:
                self._insert_videos(conn, [row], _playlist_title(playlist, playlist_id))
        # A download adds a thumbnail and metadata bytes to the playlist's
        # folder; keep those columns as a full build would set them.
        conn.execute(
            "UPDATE idx_playlists SET has_thumb = ?, meta_bytes = ? "
            "WHERE playlist_id = ?",
            (int(_has_thumb(meta_dir, playlist)), _tree_bytes(meta_dir), playlist_id),
        )
        self._set_dirty(conn)
        conn.commit()

    def _drop_playlist(self, conn: sqlite3.Connection, playlist_id: str) -> None:
        self._delete_playlist(conn, playlist_id)
        self._set_dirty(conn)
        conn.commit()

    def _rebuild(self, conn: sqlite3.Connection) -> int:
        for table in _DERIVED_TABLES:
            conn.execute(f"DELETE FROM {table}")
        count = 0
        for meta_dir in self._playlist_dirs() or []:
            path = meta_dir / PLAYLIST_FILE
            stat = _source_stat(path)
            doc, digest = _read_playlist(path)
            if stat is None or doc is None:
                continue
            if self._index_playlist(conn, meta_dir, doc, stat, digest):
                count += 1
        self._derive(conn)
        conn.commit()
        self._rebuilds += 1
        self._rebuilt_count = count
        self._pending_refresh.clear()  # everything was just read afresh
        return count

    def _playlist_dirs(self) -> list[Path] | None:
        """Every playlist folder, or None when the folder holding them exists
        but cannot be listed (its rows must then be kept, not dropped)."""
        try:
            candidates = sorted(self.layout.meta_root.iterdir())
        except FileNotFoundError:
            return []
        except OSError as exc:
            log.warning("cannot list %s: %s", self.layout.meta_root, exc)
            return None
        out = []
        for path in candidates:
            try:
                if path.name.startswith(".") or not path.is_dir():
                    continue
            except OSError:
                continue
            out.append(path)
        return out

    def _stored(self, conn: sqlite3.Connection) -> dict[str, tuple]:
        """Indexed playlists by folder: (id, mtime_ns, size, hash, indexed_ns).

        A row's folder is safe_id(playlist_id): _index_playlist only indexes
        playlists that live there.
        """
        return {
            _folder_for(row[0]) or f"\0{row[0]}": tuple(row)
            for row in conn.execute(
                "SELECT playlist_id, source_mtime_ns, source_size, source_hash, "
                "indexed_ns FROM idx_playlists"
            )
        }

    def _sync_batch(
        self,
        conn: sqlite3.Connection,
        dirs: list[Path],
        start: int,
        seen: set[str],
    ) -> tuple[int, int]:
        """Sync ``dirs[start:]`` until the batch's time is up and commit;
        returns (where the next batch starts, how many changed)."""
        stop = time.monotonic() + self.SYNC_BATCH_S
        stored = self._stored(conn)
        changed = 0
        index = start
        while index < len(dirs):
            if index > start and time.monotonic() >= stop:
                break
            changed += self._sync_one(conn, dirs[index], stored.get(dirs[index].name), seen)
            index += 1
        if changed:
            # Committed with the rows, so a derive that later finds the
            # database locked is still owed (and readers recompute meanwhile).
            self._set_dirty(conn)
        conn.commit()
        return index, changed

    def _sync_one(
        self,
        conn: sqlite3.Connection,
        meta_dir: Path,
        old: tuple | None,
        seen: set[str],
    ) -> int:
        path = meta_dir / PLAYLIST_FILE
        stat = _source_stat(path)
        if stat is None:
            return 0
        seen.add(meta_dir.name)
        if old is not None and (old[1], old[2]) == stat:
            if not _racy(old[1], old[4]):
                return 0
            # Same mtime and size, but too close to when it was indexed for
            # a 2-second clock: compare the bytes.
            if old[3] and _file_hash(path) == old[3]:
                conn.execute(
                    "UPDATE idx_playlists SET indexed_ns = ? WHERE playlist_id = ?",
                    (time.time_ns(), old[0]),
                )
                return 0
        doc, digest = _read_playlist(path)
        if old is not None:
            self._delete_playlist(conn, old[0])
        if doc is None:
            # Unreadable now: drop the old rows once, then wait for a valid
            # rewrite (counted only if something was dropped).
            return 1 if old is not None else 0
        return 1 if self._index_playlist(conn, meta_dir, doc, stat, digest) else 0

    def _drop_missing(self, conn: sqlite3.Connection, seen: set[str]) -> int:
        """Drop rows whose playlist folder is gone (checked again now: the
        lock was released while the folders were synced)."""
        changed = 0
        for folder, row in self._stored(conn).items():
            if folder in seen:
                continue
            if not folder.startswith("\0") and _source_stat(
                self.layout.meta_root / folder / PLAYLIST_FILE
            ) is not None:
                continue
            self._delete_playlist(conn, row[0])
            changed += 1
        if changed:
            self._set_dirty(conn)
        conn.commit()
        return changed

    def _index_playlist(
        self,
        conn: sqlite3.Connection,
        meta_dir: Path,
        doc: dict,
        stat: tuple[int, int],
        source_hash: str,
    ) -> bool:
        playlist_id = str(doc.get("id") or meta_dir.name)
        if _folder_for(playlist_id) != meta_dir.name:
            # Every path for this id resolves to another folder; its videos
            # could not be found (and _sync could not match it to its row).
            log.debug("skipping %s: id %r lives elsewhere", meta_dir, playlist_id)
            return False
        conn.execute(
            """
            INSERT INTO idx_playlists
                (playlist_id, title, channel, description, kind, updated_at,
                 has_thumb, meta_bytes, source_mtime_ns, source_size,
                 source_hash, indexed_ns)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                playlist_id,
                _playlist_title(doc, playlist_id),
                _text(doc.get("channel")),
                _text(doc.get("description")),
                _text(doc.get("kind")) or "youtube",
                _text(doc.get("updated_at")),
                int(_has_thumb(meta_dir, doc)),
                _tree_bytes(meta_dir),
                stat[0],
                stat[1],
                source_hash,
                time.time_ns(),
            ),
        )
        extra = self._extras(conn, playlist_id)
        rows: list[VideoRow] = []
        seen: set[str] = set()
        entries = [e for e in doc.get("entries") or [] if isinstance(e, dict)]
        for position, entry in enumerate(entries, start=1):
            row = self._video_row(playlist_id, doc, entry, position, extra)
            if row is None or row.video_id in seen:
                continue
            seen.add(row.video_id)
            rows.append(row)
        self._insert_videos(conn, rows, _playlist_title(doc, playlist_id))
        self._set_dirty(conn)
        return True

    def _video_row(
        self,
        playlist_id: str,
        playlist: dict,
        entry: dict,
        position: int,
        extra: dict[str, tuple[tuple[str, ...], tuple[str, ...]]],
    ) -> VideoRow | None:
        video_id = str(entry.get("id") or "")
        if not video_id:
            return None
        try:
            video_dir = self.layout.video_meta_dir(playlist_id, video_id)
        except ValueError:
            return None
        meta = read_json(video_dir / META_FILE) or {}
        files = meta.get("files")
        media_ref = files.get("media") if isinstance(files, dict) else None
        media_ref = media_ref if isinstance(media_ref, str) else ""
        media = find_media(self.layout, video_dir)
        media_bytes = 0
        if media is not None:
            with contextlib.suppress(OSError):
                media_bytes = media.stat().st_size
        duration = _duration(meta.get("duration"))
        if duration is None:
            duration = _duration(entry.get("duration"))
        fallback = extra.get(video_id, ((), ()))
        categories = (
            _str_tuple(meta.get("categories")) if "categories" in meta else fallback[0]
        )
        yt_tags = _str_tuple(meta.get("tags")) if "tags" in meta else fallback[1]
        return VideoRow(
            playlist_id=playlist_id,
            video_id=video_id,
            position=position,
            title=_text(meta.get("title")) or _text(entry.get("title")) or video_id,
            channel=_text(meta.get("channel")) or _text(playlist.get("channel")),
            description=_text(meta.get("description")),
            duration=duration,
            upload_date=_text(meta.get("upload_date")),
            media_ref=media_ref,
            media_bytes=media_bytes,
            available=media is not None,
            has_transcript=bool(meta.get("transcript"))
            or (video_dir / TRANSCRIPT_FILE).is_file(),
            categories=categories,
            yt_tags=yt_tags,
        )

    @staticmethod
    def _extras(
        conn: sqlite3.Connection, playlist_id: str
    ) -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
        return {
            row[0]: (_json_tuple(row[1]), _json_tuple(row[2]))
            for row in conn.execute(
                "SELECT video_id, categories, yt_tags FROM idx_video_extra "
                "WHERE playlist_id = ?",
                (playlist_id,),
            )
        }

    @staticmethod
    def _insert_videos(
        conn: sqlite3.Connection, rows: list[VideoRow], playlist_title: str
    ) -> None:
        conn.executemany(
            """
            INSERT OR REPLACE INTO idx_videos
                (playlist_id, video_id, position, title, channel, description,
                 duration, upload_date, media_ref, media_bytes, available,
                 has_transcript, categories, yt_tags)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    r.playlist_id, r.video_id, r.position, r.title, r.channel,
                    r.description, r.duration, r.upload_date, r.media_ref,
                    r.media_bytes, int(r.available), int(r.has_transcript),
                    json.dumps(list(r.categories)), json.dumps(list(r.yt_tags)),
                )
                for r in rows
            ],
        )
        conn.executemany(
            """
            INSERT INTO idx_video_fts
                (title, description, channel, playlist_title, video_id, playlist_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (r.title, r.description, r.channel, playlist_title,
                 r.video_id, r.playlist_id)
                for r in rows
            ],
        )

    @staticmethod
    def _delete_video(conn: sqlite3.Connection, playlist_id: str, video_id: str) -> None:
        conn.execute(
            "DELETE FROM idx_videos WHERE playlist_id = ? AND video_id = ?",
            (playlist_id, video_id),
        )
        conn.execute(
            "DELETE FROM idx_video_fts WHERE playlist_id = ? AND video_id = ?",
            (playlist_id, video_id),
        )

    @staticmethod
    def _delete_playlist(conn: sqlite3.Connection, playlist_id: str) -> None:
        for table in _DERIVED_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE playlist_id = ?", (playlist_id,))

    @staticmethod
    def _set_dirty(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO idx_meta (key, value) VALUES ('tags_dirty', '1')"
        )

    @staticmethod
    def _tags_dirty(conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT value FROM idx_meta WHERE key = 'tags_dirty'"
        ).fetchone()
        return row is not None and row[0] == "1"

    def _derive(self, conn: sqlite3.Connection) -> None:
        """Recompute every playlist's tags and category across the library."""
        playlists = conn.execute(
            "SELECT playlist_id, title, description, channel FROM idx_playlists "
            "ORDER BY playlist_id"
        ).fetchall()
        video_tags: dict[str, list[tuple[str, ...]]] = {}
        video_categories: dict[str, list[tuple[str, ...]]] = {}
        for row in conn.execute(
            "SELECT playlist_id, categories, yt_tags FROM idx_videos "
            "ORDER BY playlist_id, position"
        ):
            video_categories.setdefault(row[0], []).append(_json_tuple(row[1]))
            video_tags.setdefault(row[0], []).append(_json_tuple(row[2]))
        inputs = [
            TagInput(
                playlist_id=row[0],
                title=row[1] or "",
                description=row[2] or "",
                channel=row[3] or "",
                video_tags=tuple(video_tags.get(row[0], [])),
            )
            for row in playlists
        ]
        tags = derive_tags(inputs)
        conn.execute("DELETE FROM idx_tags")
        conn.execute("DELETE FROM idx_categories")
        conn.executemany(
            "INSERT INTO idx_tags (playlist_id, position, key, label) "
            "VALUES (?, ?, ?, ?)",
            [
                (playlist_id, position, tag.key, tag.label)
                for playlist_id, found in tags.items()
                for position, tag in enumerate(found)
            ],
        )
        conn.executemany(
            "INSERT INTO idx_categories (playlist_id, category) VALUES (?, ?)",
            [
                (row[0], playlist_category(video_categories.get(row[0], [])))
                for row in playlists
            ],
        )
        conn.execute(
            "INSERT OR REPLACE INTO idx_meta (key, value) VALUES ('tags_dirty', '0')"
        )

    # -- reads ----------------------------------------------------------------

    def playlists(self) -> list[PlaylistRow]:
        """Every indexed playlist, each with its videos in playlist order.

        A locked (or unrecoverable) database returns the rows from the last
        successful read, or an empty list.
        """
        result = self._guarded(
            self._read_playlists, lambda: None, timeout=READ_TIMEOUT_S
        )
        if result is None:
            return list(self._last_playlists)
        self._last_playlists = result
        return list(result)

    def derived(self) -> dict[str, Derived]:
        """Each playlist's derived category and tags (recomputed if stale)."""
        result = self._guarded(
            self._read_derived, lambda: None, timeout=READ_TIMEOUT_S
        )
        if result is None:
            return dict(self._last_derived)
        self._last_derived = result
        return dict(result)

    def search_meta(
        self, query: str, limit: int = 100, *, per_playlist: int | None = None
    ) -> list[MetaHit]:
        """Videos whose title, description, channel or playlist title match.

        Best matches first (title weighs most).  With ``per_playlist``, at
        most that many per playlist, and each playlist's best hit comes
        before any playlist's next one (so ``limit`` cuts the least useful).
        A locked or damaged index returns an empty list.
        """
        from .transcripts.store import fts_query

        expression = fts_query(query)
        if not expression:
            return []
        limit = max(1, min(int(limit), 5000))
        result = self._guarded(
            lambda conn: self._search_meta(conn, expression, limit, per_playlist),
            lambda: [],
            timeout=READ_TIMEOUT_S,
        )
        return list(result)

    @staticmethod
    def _search_meta(
        conn: sqlite3.Connection,
        expression: str,
        limit: int,
        per_playlist: int | None = None,
    ) -> list[MetaHit]:
        from .transcripts.store import SNIPPET_END, SNIPPET_START

        columns = """
            video_id, playlist_id,
            snippet(idx_video_fts, 0, ?1, ?2, '…', 18) AS s_title,
            snippet(idx_video_fts, 1, ?1, ?2, '…', 18) AS s_desc,
            snippet(idx_video_fts, 2, ?1, ?2, '…', 18) AS s_channel,
            snippet(idx_video_fts, 3, ?1, ?2, '…', 18) AS s_playlist
        """
        rows: list = []
        ranks: list[float] = []
        if per_playlist is None:
            rows = conn.execute(
                f"""
                SELECT {columns},
                       bm25(idx_video_fts, 10.0, 1.0, 3.0, 4.0) AS rank
                FROM idx_video_fts
                WHERE idx_video_fts MATCH ?3
                ORDER BY rank
                LIMIT ?4
                """,
                (SNIPPET_START, SNIPPET_END, expression, limit),
            ).fetchall()
            ranks = [float(row["rank"]) for row in rows]
        else:
            # Each playlist's best few, every playlist's best hit before any
            # playlist's second: one playlist's many matches cannot crowd
            # the others out of ``limit``.  Snippets are made only for the
            # rows picked.
            picked = conn.execute(
                """
                SELECT rid, rank FROM (
                    SELECT rid, rank, row_number() OVER (
                        PARTITION BY playlist_id ORDER BY rank
                    ) AS n
                    FROM (
                        SELECT rowid AS rid, playlist_id,
                               bm25(idx_video_fts, 10.0, 1.0, 3.0, 4.0) AS rank
                        FROM idx_video_fts
                        WHERE idx_video_fts MATCH ?1
                        ORDER BY rank
                    )
                )
                WHERE n <= ?2
                ORDER BY n, rank
                LIMIT ?3
                """,
                (expression, max(1, int(per_playlist)), limit),
            ).fetchall()
            if picked:
                marks = ",".join("?" for _ in picked)
                by_rowid = {
                    row["rid"]: row
                    for row in conn.execute(
                        f"SELECT rowid AS rid, {columns} FROM idx_video_fts "
                        f"WHERE idx_video_fts MATCH ?3 AND rowid IN ({marks})",
                        (SNIPPET_START, SNIPPET_END, expression,
                         *(row["rid"] for row in picked)),
                    )
                }
                for row in picked:
                    found = by_rowid.get(row["rid"])
                    if found is not None:
                        rows.append(found)
                        ranks.append(float(row["rank"]))
        hits = []
        for row, rank in zip(rows, ranks):
            field, raw = "title", str(row["s_title"] or "")
            for name, column in (
                ("title", "s_title"), ("description", "s_desc"),
                ("channel", "s_channel"), ("playlist", "s_playlist"),
            ):
                text = str(row[column] or "")
                if SNIPPET_START in text:
                    field, raw = name, text
                    break
            escaped = html.escape(raw, quote=False)
            hits.append(MetaHit(
                playlist_id=row["playlist_id"],
                video_id=row["video_id"],
                field=field,
                html=escaped.replace(SNIPPET_START, "<mark>").replace(
                    SNIPPET_END, "</mark>"
                ),
                rank=rank,
                playlist_match=SNIPPET_START in str(row["s_playlist"] or ""),
            ))
        return hits

    def vocabulary(self) -> dict[str, Tag]:
        """Every derived tag in the library, by key."""
        vocab: dict[str, Tag] = {}
        for derived in self.derived().values():
            for tag in derived.tags:
                vocab.setdefault(tag.key, tag)
        return vocab

    @staticmethod
    def _read_playlists(conn: sqlite3.Connection) -> list[PlaylistRow]:
        videos: dict[str, list[VideoRow]] = {}
        for row in conn.execute(
            "SELECT * FROM idx_videos ORDER BY playlist_id, position"
        ):
            videos.setdefault(row["playlist_id"], []).append(
                VideoRow(
                    playlist_id=row["playlist_id"],
                    video_id=row["video_id"],
                    position=int(row["position"] or 0),
                    title=row["title"] or "",
                    channel=row["channel"] or "",
                    description=row["description"] or "",
                    duration=row["duration"],
                    upload_date=row["upload_date"] or "",
                    media_ref=row["media_ref"] or "",
                    media_bytes=int(row["media_bytes"] or 0),
                    available=bool(row["available"]),
                    has_transcript=bool(row["has_transcript"]),
                    categories=_json_tuple(row["categories"]),
                    yt_tags=_json_tuple(row["yt_tags"]),
                )
            )
        return [
            PlaylistRow(
                playlist_id=row["playlist_id"],
                title=row["title"] or "",
                channel=row["channel"] or "",
                description=row["description"] or "",
                kind=row["kind"] or "",
                updated_at=row["updated_at"] or "",
                has_thumb=bool(row["has_thumb"]),
                meta_bytes=int(row["meta_bytes"] or 0),
                videos=tuple(videos.get(row["playlist_id"], [])),
            )
            for row in conn.execute("SELECT * FROM idx_playlists ORDER BY playlist_id")
        ]

    def _read_derived(self, conn: sqlite3.Connection) -> dict[str, Derived]:
        if self._tags_dirty(conn):
            self._derive(conn)
            conn.commit()
        tags: dict[str, list[Tag]] = {}
        for row in conn.execute(
            "SELECT playlist_id, key, label FROM idx_tags "
            "ORDER BY playlist_id, position"
        ):
            tags.setdefault(row[0], []).append(Tag(row[1], row[2]))
        return {
            row[0]: Derived(category=row[1] or "", tags=tuple(tags.get(row[0], [])))
            for row in conn.execute("SELECT playlist_id, category FROM idx_categories")
        }
