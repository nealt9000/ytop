from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

import pytest

from factories import add_playlist, make_library
from ytop.index import index_for
from ytop.layout import LibraryLayout
from ytop.transcripts.captions import Segment
from ytop.transcripts.store import index_video_dir, search, write_transcript
from ytop.yt.metadata import read_json, write_json


def _library(tmp_path: Path) -> LibraryLayout:
    layout = make_library(tmp_path)
    add_playlist(layout, "P1", "First", [("a1", "A One"), ("a2", "A Two"), ("a3", "A Three")])
    add_playlist(layout, "P2", "Second", [("b1", "B One"), ("b2", "B Two"), ("b3", "B Three")])
    return layout


def _bump(path: Path, doc: dict) -> None:
    before = path.stat().st_mtime_ns
    write_json(path, doc)
    if path.stat().st_mtime_ns == before:
        os.utime(path, ns=(before + 1_000_000_000, before + 1_000_000_000))


def test_rebuild_indexes_playlists_and_videos(tmp_path: Path) -> None:
    layout = _library(tmp_path)
    index = index_for(layout)
    assert index.rebuild() == 2
    playlists = index.playlists()
    assert len(playlists) == 2
    first = next(p for p in playlists if p.playlist_id == "P1")
    assert [v.video_id for v in first.videos] == ["a1", "a2", "a3"]
    assert [v.position for v in first.videos] == [1, 2, 3]
    assert all(v.available for v in first.videos)
    assert all(v.media_bytes == 1024 for v in first.videos)
    assert first.has_thumb
    assert first.title == "First"
    assert first.videos[0].duration == 60


def test_ensure_fresh_only_reindexes_changed(tmp_path: Path) -> None:
    layout = _library(tmp_path)
    index = index_for(layout)
    index.rebuild()
    assert index.ensure_fresh() == 0
    path = layout.playlist_meta_dir("P1") / "playlist.json"
    doc = read_json(path) or {}
    doc["title"] = "Renamed"
    _bump(path, doc)
    assert index.ensure_fresh() == 1
    titles = {p.playlist_id: p.title for p in index.playlists()}
    assert titles == {"P1": "Renamed", "P2": "Second"}


def test_ensure_fresh_drops_vanished_playlist(tmp_path: Path) -> None:
    import shutil

    layout = _library(tmp_path)
    index = index_for(layout)
    index.ensure_fresh()
    shutil.rmtree(layout.playlist_meta_dir("P2"))
    assert index.ensure_fresh() == 1
    assert [p.playlist_id for p in index.playlists()] == ["P1"]


def test_derived_tags_and_category(tmp_path: Path) -> None:
    layout = _library(tmp_path)
    for vid in ("a1", "a2", "a3"):
        path = layout.video_meta_dir("P1", vid) / "meta.json"
        meta = read_json(path) or {}
        meta["tags"] = ["Counterpoint"]
        meta["categories"] = ["Music"]
        write_json(path, meta)
    index = index_for(layout)
    index.ensure_fresh()
    derived = index.derived()
    assert derived["P1"].category == "Music"
    assert "counterpoint" in [tag.key for tag in derived["P1"].tags]
    assert derived["P2"].category == "Uncategorized"
    assert "counterpoint" in index.vocabulary()
    row = next(p for p in index.playlists() if p.playlist_id == "P1")
    assert row.videos[0].categories == ("Music",)
    assert row.videos[0].yt_tags == ("Counterpoint",)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_read_only_library_uses_memory_index(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    layout = _library(tmp_path)
    data = layout.data_dir
    os.chmod(data, 0o555)
    try:
        index = index_for(layout, read_only=True)
        index.ensure_fresh()
        assert index.playlists()
        assert not layout.index_db.exists()
    finally:
        os.chmod(data, 0o755)


def test_corrupt_index_is_set_aside_and_rebuilt(tmp_path: Path) -> None:
    layout = _library(tmp_path)
    video_dir = layout.video_meta_dir("P1", "a1")
    write_transcript(
        video_dir,
        video_id="a1",
        title="A One",
        language="en",
        kind="auto",
        segments=[Segment(0.0, 4.0, "Isomorphism is discussed here.")],
        raw_file=None,
    )
    index_video_dir(layout, video_dir, playlist_id="P1", title="A One")
    layout.index_db.write_bytes(b"garbage" * 100)
    index = index_for(layout)
    index.ensure_fresh()
    assert layout.index_db.with_name("index.sqlite3.corrupt").exists()
    assert len(index.playlists()) == 2
    row = next(p for p in index.playlists() if p.playlist_id == "P1")
    assert row.videos[0].has_transcript
    hits = search(layout, "isomorphism")
    assert [hit["video_id"] for hit in hits] == ["a1"]


def test_refresh_video_and_drop_playlist(tmp_path: Path) -> None:
    layout = _library(tmp_path)
    index = index_for(layout)
    index.refresh_video("P1", "a2")  # no row yet: indexes the whole playlist
    assert [p.playlist_id for p in index.playlists()] == ["P1"]
    path = layout.video_meta_dir("P1", "a2") / "meta.json"
    meta = read_json(path) or {}
    meta["title"] = "Retitled"
    write_json(path, meta)
    index.refresh_video("P1", "a2")
    row = index.playlists()[0]
    assert [v.title for v in row.videos] == ["A One", "Retitled", "A Three"]
    index.drop_playlist("P1")
    assert index.playlists() == []


def test_schema_change_rebuilds_but_keeps_video_extra(tmp_path: Path) -> None:
    import sqlite3

    layout = _library(tmp_path)
    index = index_for(layout)
    index.ensure_fresh()
    with sqlite3.connect(layout.index_db) as conn:
        conn.execute(
            "INSERT INTO idx_video_extra VALUES ('P2', 'b1', 0, '[\"Music\"]', '[\"x\"]')"
        )
        conn.execute("UPDATE idx_meta SET value = '0' WHERE key = 'schema'")
    assert index.ensure_fresh() == 2
    row = next(p for p in index.playlists() if p.playlist_id == "P2")
    assert row.videos[0].categories == ("Music",)
    assert row.videos[0].yt_tags == ("x",)


def _corrupt_page(db: Path, table: str) -> None:
    import sqlite3

    with sqlite3.connect(db) as conn:
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        root = conn.execute(
            "SELECT rootpage FROM sqlite_master WHERE name = ?", (table,)
        ).fetchone()[0]
    conn.close()
    assert root > 1
    with db.open("r+b") as handle:
        handle.seek((root - 1) * page_size)
        handle.write(b"\xff" * page_size)


def test_corruption_found_mid_query_heals(tmp_path: Path) -> None:
    layout = _library(tmp_path)
    index = index_for(layout)
    index.rebuild()
    _corrupt_page(layout.index_db, "idx_videos")
    playlists = index.playlists()
    assert layout.index_db.with_name("index.sqlite3.corrupt").exists()
    assert [p.playlist_id for p in playlists] == ["P1", "P2"]
    assert all(len(p.videos) == 3 for p in playlists)
    assert index.ensure_fresh() == 0


def test_corruption_mid_query_retries_once_then_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    from ytop.index import LibraryIndex

    layout = _library(tmp_path)
    index = index_for(layout)
    index.rebuild()
    good = index.playlists()
    calls = []

    def broken(conn: sqlite3.Connection) -> list:
        calls.append(1)
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(LibraryIndex, "_read_playlists", staticmethod(broken))
    assert index.playlists() == good  # last rows, after one recovery + retry
    assert len(calls) == 2
    assert layout.index_db.with_name("index.sqlite3.corrupt").exists()


def test_unmovable_corrupt_index_falls_back_to_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _library(tmp_path)
    layout.index_db.write_bytes(b"garbage" * 100)
    real_replace = os.replace

    def locked(src: object, dst: object) -> None:
        if Path(str(src)) == layout.index_db:
            raise PermissionError("in use by another process")
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", locked)
    index = index_for(layout)
    index.ensure_fresh()
    assert len(index.playlists()) == 2
    assert layout.index_db.read_bytes() == b"garbage" * 100
    assert not layout.index_db.with_name("index.sqlite3.corrupt").exists()


def test_corrupt_index_moves_its_journal_too(tmp_path: Path) -> None:
    import sqlite3

    layout = _library(tmp_path)
    layout.index_db.write_bytes(b"garbage" * 100)
    journal = layout.index_db.with_name("index.sqlite3-journal")
    journal.write_bytes(b"stale journal")
    index = index_for(layout)
    # Called directly: on open SQLite itself discards a journal whose header
    # is invalid, which would hide whether recovery moved it.
    index._recover(sqlite3.DatabaseError("file is not a database"))
    assert not journal.exists()
    assert journal.with_name("index.sqlite3-journal.corrupt").read_bytes() == (
        b"stale journal"
    )
    assert layout.index_db.with_name("index.sqlite3.corrupt").exists()
    assert len(index.playlists()) == 2


def test_only_damage_counts_as_corruption() -> None:
    import sqlite3

    from ytop.index import _is_corruption

    assert _is_corruption(sqlite3.DatabaseError("file is not a database"))
    assert _is_corruption(sqlite3.IntegrityError("constraint"))
    assert not _is_corruption(sqlite3.OperationalError("database is locked"))
    assert not _is_corruption(sqlite3.ProgrammingError("closed"))
    assert not _is_corruption(sqlite3.InterfaceError("bad parameter"))
    assert not _is_corruption(sqlite3.NotSupportedError("nope"))


def test_finished_video_appears_without_rescan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ytop.index import LibraryIndex

    layout = make_library(tmp_path)
    add_playlist(layout, "P1", "First", [("v1", "One")])
    add_playlist(layout, "P2", "Second", [("v2", "Two")], media=False)
    index = index_for(layout)
    index.ensure_fresh()
    row = next(p for p in index.playlists() if p.playlist_id == "P2")
    assert not row.videos[0].available

    media = layout.media_root / "Second"
    media.mkdir(parents=True, exist_ok=True)
    (media / "01 - Two.webm").write_bytes(b"x" * 512)
    path = layout.video_meta_dir("P2", "v2") / "meta.json"
    meta = read_json(path) or {}
    meta["files"] = {"media": layout.relative_media(media / "01 - Two.webm")}
    write_json(path, meta)
    index.refresh_video("P2", "v2")

    def no_rescan(*args: object, **kwargs: object) -> None:
        raise RuntimeError("full rescan")

    monkeypatch.setattr(LibraryIndex, "_index_playlist", no_rescan)
    assert index.ensure_fresh() == 0
    row = next(p for p in index.playlists() if p.playlist_id == "P2")
    assert row.videos[0].available


def test_removed_playlist_drops_from_index(tmp_path: Path) -> None:
    from factories import library_set
    from ytop.web.library import Library

    layout = _library(tmp_path)
    index = index_for(layout)
    index.ensure_fresh()
    assert Library(library_set(layout)).remove_playlist("P1")
    index.ensure_fresh()
    assert [p.playlist_id for p in index.playlists()] == ["P2"]


def _raw_video(tmp_path: Path) -> LibraryLayout:
    layout = _library(tmp_path)
    write_json(
        layout.video_meta_dir("P1", "a1") / "raw.json",
        {"tags": ["Fugue"], "categories": ["Music"]},
    )
    return layout


def test_backfill_reads_raw_json_once(tmp_path: Path) -> None:
    layout = _raw_video(tmp_path)
    index = index_for(layout)
    index.ensure_fresh()
    assert index.backfill_tags() == 1
    assert index.backfill_tags() == 0
    assert "fugue" in {t.key for d in index.derived().values() for t in d.tags}
    row = next(p for p in index.playlists() if p.playlist_id == "P1")
    assert row.videos[0].yt_tags == ("Fugue",)
    # meta.json is never rewritten
    assert "tags" not in (read_json(layout.video_meta_dir("P1", "a1") / "meta.json") or {})


def test_backfill_skips_read_only(tmp_path: Path) -> None:
    layout = _raw_video(tmp_path)
    assert index_for(layout, read_only=True).backfill_tags() == 0


def test_backfill_stops_at_budget(tmp_path: Path) -> None:
    layout = _raw_video(tmp_path)
    index = index_for(layout)
    index.ensure_fresh()
    assert index.backfill_tags(budget_s=-1.0) == 0
    assert index.backfill_tags() == 1


def test_backfill_does_not_block_readers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    import time

    import ytop.index as index_mod

    layout = make_library(tmp_path)
    add_playlist(layout, "P1", "First", [(f"v{i}", f"V{i}") for i in range(60)])
    for i in range(60):
        write_json(layout.video_meta_dir("P1", f"v{i}") / "raw.json", {"tags": ["t"]})
    index = index_for(layout)
    index.ensure_fresh()
    real = index_mod.read_json

    def slow(path: Path):
        if path.name == "raw.json":
            time.sleep(0.05)
        return real(path)

    monkeypatch.setattr(index_mod, "read_json", slow)
    result: list[int] = []
    worker = threading.Thread(target=lambda: result.append(index.backfill_tags()))
    worker.start()
    time.sleep(0.2)
    start = time.monotonic()
    index.playlists()
    waited = time.monotonic() - start
    worker.join()
    assert result == [60]
    # a whole-library lock would hold the reader for ~3 s
    assert waited < 1.6, waited


def test_second_backfill_reads_no_meta_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ytop.index as index_mod

    layout = _raw_video(tmp_path)
    index = index_for(layout)
    index.ensure_fresh()
    assert index.backfill_tags() == 1
    real = index_mod.read_json
    reads: list[str] = []

    def spy(path: Path):
        reads.append(path.name)
        return real(path)

    monkeypatch.setattr(index_mod, "read_json", spy)
    assert index.backfill_tags() == 0
    assert "meta.json" not in reads


def test_search_meta_ranks_title_over_description(tmp_path: Path) -> None:
    layout = make_library(tmp_path)
    add_playlist(layout, "P1", "First", [("t1", "Sonata form"), ("d1", "Plain")])
    path = layout.video_meta_dir("P1", "d1") / "meta.json"
    meta = read_json(path) or {}
    meta["description"] = "a talk about sonata structure"
    write_json(path, meta)
    index = index_for(layout)
    index.rebuild()
    hits = index.search_meta("sonata")
    assert [h.video_id for h in hits] == ["t1", "d1"]
    assert hits[0].field == "title" and hits[1].field == "description"
    assert "<mark>Sonata</mark>" in hits[0].html
    assert hits[0].rank <= hits[1].rank
    assert index.search_meta("") == []


def test_search_meta_hostile_query(tmp_path: Path) -> None:
    layout = _library(tmp_path)
    index = index_for(layout)
    index.rebuild()
    assert isinstance(index.search_meta('c++ (2) "OR" *'), list)
    assert isinstance(index.search_meta('"'), list)


def _same_size_rewrite(path: Path, title: str) -> None:
    """Rewrite playlist.json with a same-length title and the old mtime, as
    FAT32's 2-second clock can leave it."""
    before = path.stat()
    doc = read_json(path) or {}
    assert len(doc["title"]) == len(title)
    doc["title"] = title
    write_json(path, doc)
    assert path.stat().st_size == before.st_size
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))


def test_rewrite_within_two_seconds_of_indexing_is_noticed(tmp_path: Path) -> None:
    layout = _library(tmp_path)
    index = index_for(layout)
    index.ensure_fresh()
    path = layout.playlist_meta_dir("P1") / "playlist.json"
    _same_size_rewrite(path, "Fixed")
    assert index.ensure_fresh() == 1
    assert {p.playlist_id: p.title for p in index.playlists()}["P1"] == "Fixed"
    assert index.ensure_fresh() == 0  # same bytes: not re-indexed again


def test_old_unchanged_playlist_is_trusted_without_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ytop.index as index_mod

    layout = _library(tmp_path)
    for pid in ("P1", "P2"):
        path = layout.playlist_meta_dir(pid) / "playlist.json"
        old = path.stat().st_mtime_ns - 60_000_000_000
        os.utime(path, ns=(old, old))
    index = index_for(layout)
    index.ensure_fresh()
    hashed: list[Path] = []
    real = index_mod._file_hash
    monkeypatch.setattr(
        index_mod, "_file_hash", lambda path: hashed.append(path) or real(path)
    )
    assert index.ensure_fresh() == 0
    assert hashed == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_unlistable_playlist_folder_keeps_rows(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    layout = _library(tmp_path)
    index = index_for(layout)
    index.ensure_fresh()
    root = layout.meta_root
    mode = root.stat().st_mode
    root.chmod(0o300)  # can enter, cannot list
    try:
        assert index.ensure_fresh() == 0
        assert [p.playlist_id for p in index.playlists()] == ["P1", "P2"]
    finally:
        root.chmod(mode)


def test_sync_commits_each_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    from ytop.index import LibraryIndex

    layout = _library(tmp_path)
    add_playlist(layout, "P3", "Third", [("c1", "C One")])
    index = index_for(layout)
    monkeypatch.setattr(LibraryIndex, "SYNC_BATCH_S", 0.0)
    real = LibraryIndex._index_playlist
    committed: list[set[str]] = []

    def spy(self, conn, meta_dir, doc, stat):
        if layout.index_db.exists():
            with contextlib.closing(sqlite3.connect(layout.index_db, timeout=0)) as other:
                committed.append(
                    {r[0] for r in other.execute("SELECT playlist_id FROM idx_playlists")}
                )
        return real(self, conn, meta_dir, doc, stat)

    monkeypatch.setattr(LibraryIndex, "_index_playlist", spy)
    assert index.ensure_fresh() == 3
    # Another connection sees each playlist as soon as its batch is done,
    # so the write lock is not held across the whole first build.
    assert committed == [set(), {"P1"}, {"P1", "P2"}]


def _flag(layout: LibraryLayout) -> str | None:
    import sqlite3

    with contextlib.closing(sqlite3.connect(layout.index_db)) as conn:
        row = conn.execute("SELECT value FROM idx_meta WHERE key = 'tags_dirty'").fetchone()
    return None if row is None else row[0]


def test_synced_batch_marks_tags_stale_when_derive_is_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    from ytop.index import LibraryIndex

    layout = _library(tmp_path)
    index = index_for(layout)
    index.ensure_fresh()
    assert _flag(layout) == "0"
    add_playlist(layout, "P3", "Third", [("c1", "C One")])

    def locked(self, conn, changed):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(LibraryIndex, "_derive_if_needed", locked)
    assert index.ensure_fresh() == 1
    # The batch's commit says the derived tags are stale, so the next read
    # recomputes them instead of leaving the new playlist without any.
    assert _flag(layout) == "1"
    monkeypatch.undo()
    assert "P3" in index.derived()


def test_dropped_playlist_marks_tags_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil
    import sqlite3

    from ytop.index import LibraryIndex

    layout = _library(tmp_path)
    index = index_for(layout)
    index.ensure_fresh()
    shutil.rmtree(layout.playlist_meta_dir("P2"))

    def locked(self, conn, changed):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(LibraryIndex, "_derive_if_needed", locked)
    assert index.ensure_fresh() == 1
    assert _flag(layout) == "1"


@pytest.mark.parametrize(
    "error",
    [
        "sqlite",
        "eacces",
    ],
)
def test_transient_open_failure_does_not_latch_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: str
) -> None:
    """A writable library that fails to open once (a pulled-and-replugged
    stick, antivirus holding the file) is treated as busy, not read-only."""
    import errno
    import sqlite3

    from ytop.index import LibraryIndex

    layout = _library(tmp_path)
    index = index_for(layout)
    real = LibraryIndex._open
    failures = [1]

    def flaky(self):
        if failures:
            failures.pop()
            if error == "sqlite":
                raise sqlite3.OperationalError("unable to open database file")
            raise PermissionError(errno.EACCES, "Permission denied")
        return real(self)

    monkeypatch.setattr(LibraryIndex, "_open", flaky)
    assert index.ensure_fresh() == 0  # busy this time: nothing indexed
    assert not index._memory_fallback
    assert index.ensure_fresh() == 2
    assert not index._memory_fallback
    assert {p.playlist_id for p in index.playlists()} == {"P1", "P2"}
    assert _flag(layout) == "0"  # written to the file, not to memory
