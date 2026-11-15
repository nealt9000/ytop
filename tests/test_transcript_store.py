from __future__ import annotations

import sqlite3
from pathlib import Path

from factories import add_playlist, make_library
from ytop.layout import LibraryLayout
from ytop.transcripts.captions import Segment
from ytop.transcripts.store import (
    _connect,
    db_path,
    index_library,
    index_video_dir,
    read_transcript,
    search,
    write_transcript,
)


def make_transcript(
    layout: LibraryLayout,
    video_id: str = "vid1",
    *,
    playlist_id: str = "PL1",
    texts: list[str] | None = None,
) -> Path:
    video_dir = layout.video_meta_dir(playlist_id, video_id)
    video_dir.mkdir(parents=True, exist_ok=True)
    texts = texts or [
        "Gödel, Escher, Bach is about strange loops.",
        "An isomorphism preserves structure.",
        "The Sierpiński triangle has dimension log 3 over log 2.",
    ]
    segments = [
        Segment(index * 10.0, index * 10.0 + 8.0, text)
        for index, text in enumerate(texts)
    ]
    write_transcript(
        video_dir,
        video_id=video_id,
        title=f"Title {video_id}",
        language="en",
        kind="auto",
        segments=segments,
        raw_file="subtitles/en.auto.json",
    )
    return video_dir


def test_write_and_read_transcript_round_trip(tmp_path: Path):
    video_dir = make_transcript(make_library(tmp_path))
    doc = read_transcript(video_dir)
    assert doc["schema"] == 1
    assert doc["language"] == "en"
    assert len(doc["segments"]) == 3
    assert doc["segments"][0]["start"] == 0.0


def test_index_and_search_with_diacritics_prefix_and_marks(tmp_path: Path):
    layout = make_library(tmp_path)
    video_dir = make_transcript(layout)
    count = index_video_dir(
        layout, video_dir, playlist_id="PL1", title="Title vid1"
    )
    assert count == 3

    rows = search(layout, "godel")
    assert len(rows) == 1
    assert rows[0]["video_id"] == "vid1"
    assert rows[0]["start"] == 0.0
    assert "<mark>Gödel</mark>" in rows[0]["html"]

    rows = search(layout, "isomorph")
    assert len(rows) == 1
    assert "isomorphism" in rows[0]["text"]

    assert search(layout, "") == []
    assert search(layout, "!!!") == []
    assert search(layout, "not-in-the-transcript") == []


def test_reindexing_does_not_duplicate_rows(tmp_path: Path):
    layout = make_library(tmp_path)
    video_dir = make_transcript(layout)
    for _ in range(3):
        index_video_dir(layout, video_dir, playlist_id="PL1", title="Title")
    rows = search(layout, "structure")
    assert len(rows) == 1


def test_playlist_filter_and_snippet_escaping(tmp_path: Path):
    layout = make_library(tmp_path)
    make_transcript(
        layout, "vid1", playlist_id="PL1",
        texts=["<script>alert(1)</script> payload"],
    )
    make_transcript(layout, "vid2", playlist_id="PL2", texts=["payload other"])

    for vid, pid in (("vid1", "PL1"), ("vid2", "PL2")):
        video_dir = layout.video_meta_dir(pid, vid)
        index_video_dir(layout, video_dir, playlist_id=pid, title=vid)

    rows = search(layout, "payload", playlist_id="PL2")
    assert [row["video_id"] for row in rows] == ["vid2"]

    rows = search(layout, "script")
    assert len(rows) == 1
    assert "<script>" not in rows[0]["html"]
    assert "&lt;" in rows[0]["html"] and "&gt;" in rows[0]["html"]
    assert "<mark>script</mark>" in rows[0]["html"]


def test_index_library_rebuilds_from_playlist_documents(tmp_path: Path):
    layout = make_library(tmp_path)
    add_playlist(layout, "PL1", "List", [("vid1", "One")])
    make_transcript(layout, "vid1", playlist_id="PL1")
    assert index_library(layout) == 1
    assert db_path(layout) == layout.index_db
    assert layout.index_db.is_file()
    assert len(search(layout, "Sierpinski")) == 1


def test_index_uses_delete_journal(tmp_path: Path):
    layout = make_library(tmp_path)
    add_playlist(layout, "PL1", "List", [("vid1", "One")])
    make_transcript(layout, "vid1", playlist_id="PL1")
    index_library(layout)
    with _connect(layout) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "delete"
    # A fresh connection sees the persisted mode, and no WAL sidecar exists.
    raw = sqlite3.connect(layout.index_db)
    try:
        assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        raw.close()
    assert not layout.index_db.with_name("index.sqlite3-wal").exists()
