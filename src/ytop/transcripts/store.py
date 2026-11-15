"""Transcript files and the SQLite FTS5 index.

The on-disk transcript is the shared contract with Android::

    videos/<id>/transcript.json
    videos/<id>/subtitles/<language>.<kind>.<ext>

``.ytop/data/index.sqlite3`` is a derived, disposable search index.  It is
safe to delete and rebuild with ``ytop transcripts --index-only``.  It uses
``journal_mode=DELETE``: a WAL sidecar on a removable stick is lost or left
stale when the stick is pulled.
"""

from __future__ import annotations

import html
import re
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from .. import __version__
from ..yt.metadata import now_iso, read_json, write_json
from .captions import Segment

if TYPE_CHECKING:
    from ..layout import LibraryLayout

TRANSCRIPT_FILE = "transcript.json"
SUBTITLE_DIR = "subtitles"
SNIPPET_START = "\x01"
SNIPPET_END = "\x02"


def transcript_path(video_dir: Path) -> Path:
    return Path(video_dir) / TRANSCRIPT_FILE


def read_transcript(video_dir: Path) -> dict | None:
    doc = read_json(transcript_path(video_dir))
    if not doc or doc.get("schema") != 1 or not doc.get("segments"):
        return None
    return doc


def write_transcript(
    video_dir: Path,
    *,
    video_id: str,
    title: str,
    language: str,
    kind: str,
    segments: list[Segment],
    raw_file: str | None,
    source: str = "youtube",
) -> dict:
    doc = {
        "schema": 1,
        "video_id": video_id,
        "title": title,
        "language": language,
        "kind": kind,
        "source": source,
        "fetched_at": now_iso(),
        "generator": f"ytop {__version__}",
        "raw_file": raw_file,
        "segments": [segment.to_dict() for segment in segments],
    }
    write_json(transcript_path(video_dir), doc)
    return doc


def segments_from_doc(doc: dict | None) -> list[Segment]:
    if not doc:
        return []
    out = []
    for row in doc.get("segments") or []:
        try:
            start = float(row["start"])
            end = float(row["end"])
            text = str(row.get("text") or "").strip()
        except (KeyError, TypeError, ValueError):
            continue
        if text and end > start:
            out.append(Segment(start, end, text))
    return out


def db_path(layout: LibraryLayout) -> Path:
    return layout.index_db


def _connect(layout: LibraryLayout) -> sqlite3.Connection:
    path = db_path(layout)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS transcript_meta (
            playlist_id TEXT NOT NULL,
            video_id    TEXT NOT NULL,
            title       TEXT,
            language    TEXT,
            kind        TEXT,
            segments    INTEGER,
            indexed_at  TEXT,
            PRIMARY KEY (playlist_id, video_id)
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts USING fts5(
            text,
            video_id UNINDEXED,
            playlist_id UNINDEXED,
            title UNINDEXED,
            start_ms UNINDEXED,
            end_ms UNINDEXED,
            tokenize = 'unicode61 remove_diacritics 2'
        );
        """
    )
    # Older derived databases keyed metadata by video id alone, so a
    # hard-linked video indexed under two playlists lost one of its rows.
    columns = conn.execute("PRAGMA table_info(transcript_meta)").fetchall()
    primary = [
        row[1] for row in sorted(columns, key=lambda row: row[5])
        if row[5]
    ]
    if primary != ["playlist_id", "video_id"]:
        conn.execute("DROP TABLE transcript_meta")
        conn.execute(
            """
            CREATE TABLE transcript_meta (
                playlist_id TEXT NOT NULL,
                video_id    TEXT NOT NULL,
                title       TEXT,
                language    TEXT,
                kind        TEXT,
                segments    INTEGER,
                indexed_at  TEXT,
                PRIMARY KEY (playlist_id, video_id)
            )
            """
        )


def index_transcript(
    conn: sqlite3.Connection,
    *,
    playlist_id: str,
    video_id: str,
    title: str,
    doc: dict,
    commit: bool = True,
    replace: bool = True,
    ensure: bool = True,
) -> int:
    if ensure:
        ensure_schema(conn)
    segments = segments_from_doc(doc)
    if replace:
        conn.execute(
            "DELETE FROM transcript_fts "
            "WHERE playlist_id = ? AND video_id = ?",
            (playlist_id, video_id),
        )
        conn.execute(
            "DELETE FROM transcript_meta "
            "WHERE playlist_id = ? AND video_id = ?",
            (playlist_id, video_id),
        )
    conn.executemany(
        """
        INSERT INTO transcript_fts
            (text, video_id, playlist_id, title, start_ms, end_ms)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                segment.text,
                video_id,
                playlist_id,
                title,
                int(round(segment.start * 1000)),
                int(round(segment.end * 1000)),
            )
            for segment in segments
        ],
    )
    conn.execute(
        """
        INSERT INTO transcript_meta
            (playlist_id, video_id, title, language, kind, segments, indexed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            playlist_id,
            video_id,
            title,
            str(doc.get("language") or ""),
            str(doc.get("kind") or ""),
            len(segments),
            now_iso(),
        ),
    )
    if commit:
        conn.commit()
    return len(segments)


def index_video_dir(
    layout: LibraryLayout,
    video_dir: Path,
    *,
    playlist_id: str,
    title: str,
    conn: sqlite3.Connection | None = None,
    replace: bool = True,
) -> int | None:
    doc = read_transcript(video_dir)
    if doc is None:
        return None
    if conn is not None:
        return index_transcript(
            conn,
            playlist_id=playlist_id,
            video_id=str(doc.get("video_id") or Path(video_dir).name),
            title=str(title or doc.get("title") or video_dir.name),
            doc=doc,
            replace=replace,
            ensure=False,
        )
    with _connect(layout) as own:
        return index_transcript(
            own,
            playlist_id=playlist_id,
            video_id=str(doc.get("video_id") or Path(video_dir).name),
            title=str(title or doc.get("title") or video_dir.name),
            doc=doc,
        )


def index_playlist_dir(
    layout: LibraryLayout,
    playlist_dir: Path,
    playlist: dict,
    *,
    conn: sqlite3.Connection | None = None,
    replace: bool = True,
) -> int:
    """Index every transcript that exists under one playlist directory."""
    playlist_id = str(playlist.get("id") or "")
    if conn is None:
        with _connect(layout) as own:
            return index_playlist_dir(
                layout, playlist_dir, playlist, conn=own, replace=replace
            )
    ensure_schema(conn)
    count = 0
    for entry in playlist.get("entries") or []:
        video_id = str(entry.get("id") or "")
        if not video_id:
            continue
        video_dir = layout.video_meta_dir(playlist_id, video_id)
        meta = read_json(video_dir / "meta.json") or {}
        title = str(meta.get("title") or entry.get("title") or video_id)
        try:
            if index_video_dir(
                layout,
                video_dir,
                playlist_id=playlist_id,
                title=title,
                conn=conn,
                replace=replace,
            ) is not None:
                count += 1
        except (OSError, sqlite3.Error, ValueError):
            continue
    conn.commit()
    return count


def index_library(layout: LibraryLayout) -> int:
    """(Re)build the library's transcript index from the on-disk transcripts.

    One connection and one transaction per playlist keep a large rebuild from
    paying a connect/fsync cost for every video.
    """
    root = layout.meta_root
    total = 0
    with _connect(layout) as conn:
        ensure_schema(conn)
        conn.execute("DELETE FROM transcript_fts")
        conn.execute("DELETE FROM transcript_meta")
        conn.commit()
        if not root.is_dir():
            return total
        for playlist_dir in sorted(root.iterdir()):
            if not playlist_dir.is_dir() or playlist_dir.name.startswith("."):
                continue
            playlist = read_json(playlist_dir / "playlist.json") or {}
            if not playlist.get("id"):
                continue
            total += index_playlist_dir(
                layout, playlist_dir, playlist, conn=conn, replace=False
            )
    return total


def remove_playlist(layout: LibraryLayout, playlist_id: str) -> None:
    """Drop a removed playlist from the derived search index."""
    path = db_path(layout)
    if not path.exists():
        return
    try:
        with _connect(layout) as conn:
            if not _has_fts(conn):
                return
            conn.execute(
                "DELETE FROM transcript_fts WHERE playlist_id = ?",
                (playlist_id,),
            )
            conn.execute(
                "DELETE FROM transcript_meta WHERE playlist_id = ?",
                (playlist_id,),
            )
            conn.commit()
    except sqlite3.Error:
        # The index is disposable; a locked or partial database must not stop
        # the user from removing a playlist.
        return


def remove_video(layout: LibraryLayout, playlist_id: str, video_id: str) -> None:
    """Drop one removed video from the derived search index."""
    path = db_path(layout)
    if not path.exists():
        return
    try:
        with _connect(layout) as conn:
            if not _has_fts(conn):
                return
            conn.execute(
                "DELETE FROM transcript_fts "
                "WHERE playlist_id = ? AND video_id = ?",
                (playlist_id, video_id),
            )
            conn.execute(
                "DELETE FROM transcript_meta "
                "WHERE playlist_id = ? AND video_id = ?",
                (playlist_id, video_id),
            )
            conn.commit()
    except sqlite3.Error:
        return


def query_tokens(query: str) -> list[str]:
    """The words of ``query`` as FTS sees them (at most 16, original case)."""
    tokens = re.findall(r"[^\W_]+(?:['\u2019][^\W_]+)*", query or "", re.UNICODE)
    return tokens[:16]


def fts_query(query: str) -> str:
    """Turn user text into a safe FTS5 expression.

    Every token is quoted and prefix-matched, so punctuation cannot inject FTS
    operators and a partial word still finds a hit.
    """
    terms = []
    for token in query_tokens(query):
        token = token.replace('"', '""')
        terms.append(f'"{token}"*')
    return " AND ".join(terms)


_fts_query = fts_query  # the old private name


def _has_fts(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transcript_fts'"
    ).fetchone()
    return row is not None


def search(
    layouts: LibraryLayout | Sequence[LibraryLayout],
    query: str,
    limit: int = 50,
    *,
    playlist_id: str | None = None,
) -> list[dict]:
    """Search every library's index; hits are merged by FTS rank (bm25,
    lower is better) and ``limit`` applies after the merge.  Each hit carries
    the ``library_id`` it came from."""
    from ..layout import LibraryLayout as Layout  # lazy: layout -> yt -> store

    if isinstance(layouts, Layout):
        layouts = [layouts]
    limit = max(1, min(int(limit), 200))
    hits: list[dict] = []
    for layout in layouts:
        hits.extend(_search_one(layout, query, limit, playlist_id))
    hits.sort(key=lambda hit: hit["rank"])
    return hits[:limit]


def _search_one(
    layout: LibraryLayout,
    query: str,
    limit: int,
    playlist_id: str | None,
) -> list[dict]:
    path = db_path(layout)
    expression = fts_query(query)
    if not expression or not path.exists():
        return []
    try:
        with _connect(layout) as conn:
            ensure_schema(conn)
            if not _has_fts(conn):
                return []
            sql = """
                SELECT video_id, playlist_id, title, start_ms, end_ms,
                       snippet(transcript_fts, 0, ?, ?, '…', 18) AS snip,
                       bm25(transcript_fts) AS rank
                FROM transcript_fts
                WHERE transcript_fts MATCH ?
            """
            params: list = [SNIPPET_START, SNIPPET_END, expression]
            if playlist_id:
                sql += " AND playlist_id = ?"
                params.append(playlist_id)
            sql += " ORDER BY rank LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
    except (sqlite3.Error, OSError):
        return []

    from ..layout import read_library  # lazy: layout -> yt -> store

    info = read_library(layout.root)
    library_id = info.id if info is not None else ""
    results = []
    for row in rows:
        raw = str(row["snip"] or "")
        plain = raw.replace(SNIPPET_START, "").replace(SNIPPET_END, "")
        escaped = html.escape(raw, quote=False)
        highlighted = escaped.replace(
            SNIPPET_START, "<mark>"
        ).replace(SNIPPET_END, "</mark>")
        results.append({
            "video_id": row["video_id"],
            "playlist_id": row["playlist_id"],
            "title": row["title"],
            "start": round(int(row["start_ms"]) / 1000.0, 3),
            "end": round(int(row["end_ms"]) / 1000.0, 3),
            "text": plain,
            "html": highlighted,
            "library_id": library_id,
            "rank": float(row["rank"]),
        })
    return results
