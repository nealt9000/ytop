from __future__ import annotations

import os
from pathlib import Path

import pytest

from factories import add_playlist, make_library
from ytop.layout import UNGROUPED_ID, LibraryLayout
from ytop.publish import (
    playlist_folder,
    publish_download,
    recover_rename_journal,
    rename_folder,
    sync_playlist_files,
    transcript_to_srt,
)
from ytop.yt.metadata import read_json, write_json


def _stage(tmp_path: Path, name: str, data: bytes) -> Path:
    staged = tmp_path / "staging" / name
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(data)
    return staged


def _media_ref(layout: LibraryLayout, pid: str, vid: str) -> str:
    meta = read_json(layout.video_meta_dir(pid, vid) / "meta.json") or {}
    return meta["files"]["media"]


def _swap_entries(layout: LibraryLayout, pid: str) -> None:
    path = layout.playlist_meta_dir(pid) / "playlist.json"
    doc = read_json(path) or {}
    doc["entries"] = list(reversed(doc["entries"]))
    write_json(path, doc)


def _leftovers(layout: LibraryLayout) -> list[Path]:
    return list(layout.media_root.rglob(".ytoprename-*"))


def _publish_pair(tmp_path: Path) -> LibraryLayout:
    layout = make_library(tmp_path)
    add_playlist(layout, "PL1", "Mix", [("v1", "Same"), ("v2", "Same")], media=False)
    publish_download(layout, "PL1", "v1", _stage(tmp_path, "a.webm", b"one"))
    publish_download(layout, "PL1", "v2", _stage(tmp_path, "b.webm", b"two"))
    return layout


def test_publish_names_and_records(tmp_path):
    layout = make_library(tmp_path)
    add_playlist(
        layout, "PL1", "Mix", [("v1", "First"), ("v2", "Second")], media=False
    )
    final = publish_download(layout, "PL1", "v2", _stage(tmp_path, "x.webm", b"x"))
    expected = layout.media_root / "Mix" / "02 - Second.webm"
    assert final == expected
    assert expected.read_bytes() == b"x"
    assert _media_ref(layout, "PL1", "v2") == "Mix/02 - Second.webm"
    assert (layout.media_root / "Mix" / "folder.jpg").is_file()


def test_swap_reorder(tmp_path):
    layout = _publish_pair(tmp_path)
    folder = layout.media_root / "Mix"
    assert (folder / "01 - Same.webm").read_bytes() == b"one"
    assert (folder / "02 - Same.webm").read_bytes() == b"two"

    _swap_entries(layout, "PL1")
    report = sync_playlist_files(layout, "PL1")

    assert report.errors == []
    assert report.renamed == 2
    assert (folder / "01 - Same.webm").read_bytes() == b"two"
    assert (folder / "02 - Same.webm").read_bytes() == b"one"
    assert _media_ref(layout, "PL1", "v2") == "Mix/01 - Same.webm"
    assert _media_ref(layout, "PL1", "v1") == "Mix/02 - Same.webm"
    assert _leftovers(layout) == []
    assert not layout.rename_journal.exists()


def test_retitle_renames_srt_too(tmp_path):
    layout = make_library(tmp_path)
    add_playlist(layout, "PL1", "Mix", [("v1", "First")], media=False)
    publish_download(layout, "PL1", "v1", _stage(tmp_path, "a.webm", b"a"))
    folder = layout.media_root / "Mix"
    (folder / "01 - First.en.srt").write_text("subs", encoding="utf-8")
    write_json(
        layout.titles_file,
        {"schema": 1, "playlists": {}, "videos": {"v1": "Renamed"}},
    )

    report = sync_playlist_files(layout, "PL1")

    assert report.errors == []
    assert (folder / "01 - Renamed.webm").read_bytes() == b"a"
    assert (folder / "01 - Renamed.en.srt").read_text(encoding="utf-8") == "subs"
    assert not (folder / "01 - First.webm").exists()
    assert not (folder / "01 - First.en.srt").exists()
    assert _media_ref(layout, "PL1", "v1") == "Mix/01 - Renamed.webm"


def test_srt_written_from_transcript(tmp_path):
    layout = make_library(tmp_path)
    add_playlist(layout, "PL1", "Mix", [("v1", "First")], media=False)
    doc = {
        "schema": 1,
        "video_id": "v1",
        "segments": [
            {"start": 1.5, "end": 3.0, "text": "Hello"},
            {"start": 3.0, "end": 3661.25, "text": "World"},
        ],
    }
    write_json(layout.video_meta_dir("PL1", "v1") / "transcript.json", doc)
    assert transcript_to_srt(doc) == (
        "1\n00:00:01,500 --> 00:00:03,000\nHello\n\n"
        "2\n00:00:03,000 --> 01:01:01,250\nWorld\n\n"
    )

    publish_download(layout, "PL1", "v1", _stage(tmp_path, "a.webm", b"a"))

    srt = layout.media_root / "Mix" / "01 - First.en.srt"
    assert srt.is_file()
    assert "00:00:01,500 --> 00:00:03,000" in srt.read_text(encoding="utf-8")


def test_srt_language_from_transcript(tmp_path):
    layout = make_library(tmp_path)
    add_playlist(layout, "PL1", "Mix", [("v1", "First")], media=False)
    write_json(
        layout.video_meta_dir("PL1", "v1") / "transcript.json",
        {
            "schema": 1,
            "language": "de",
            "segments": [{"start": 0, "end": 1, "text": "Hallo"}],
        },
    )
    publish_download(layout, "PL1", "v1", _stage(tmp_path, "a.webm", b"a"))
    assert (layout.media_root / "Mix" / "01 - First.de.srt").is_file()
    report = sync_playlist_files(layout, "PL1")
    assert report.srt_written == 0


def test_recover_after_crash_between_phases(tmp_path, monkeypatch):
    layout = _publish_pair(tmp_path)
    _swap_entries(layout, "PL1")
    real_replace = os.replace

    def flaky_replace(src, dst, *args, **kwargs):
        if Path(src).name.startswith(".ytoprename-"):
            raise OSError("stick yanked")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", flaky_replace)
    with pytest.raises(OSError):
        sync_playlist_files(layout, "PL1")
    monkeypatch.undo()
    assert layout.rename_journal.exists()

    assert recover_rename_journal(layout) == 2

    folder = layout.media_root / "Mix"
    assert (folder / "01 - Same.webm").read_bytes() == b"two"
    assert (folder / "02 - Same.webm").read_bytes() == b"one"
    assert _media_ref(layout, "PL1", "v2") == "Mix/01 - Same.webm"
    assert _media_ref(layout, "PL1", "v1") == "Mix/02 - Same.webm"
    assert not layout.rename_journal.exists()
    assert _leftovers(layout) == []
    assert recover_rename_journal(layout) == 0


def test_never_overwrites_foreign_file(tmp_path):
    layout = make_library(tmp_path)
    add_playlist(layout, "PL1", "Mix", [("v1", "First")], media=False)
    foreign = layout.media_root / "Mix" / "01 - First.webm"
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(b"foreign")

    publish_download(layout, "PL1", "v1", _stage(tmp_path, "a.webm", b"mine"))
    report = sync_playlist_files(layout, "PL1")

    assert report.errors
    assert report.renamed == 0
    assert foreign.read_bytes() == b"foreign"
    mine = layout.resolve_media(_media_ref(layout, "PL1", "v1"))
    assert mine is not None and mine != foreign
    assert mine.read_bytes() == b"mine"
    assert not layout.rename_journal.exists()


def test_ungrouped_not_numbered(tmp_path):
    layout = make_library(tmp_path)
    add_playlist(layout, UNGROUPED_ID, "Un-grouped", [("v1", "Loose")], media=False)
    final = publish_download(
        layout, UNGROUPED_ID, "v1", _stage(tmp_path, "a.webm", b"a")
    )
    assert final == layout.media_root / "Un-grouped" / "Loose.webm"
    assert _media_ref(layout, UNGROUPED_ID, "v1") == "Un-grouped/Loose.webm"


def test_rename_folder_updates_refs_across_playlists(tmp_path):
    layout = make_library(tmp_path)
    add_playlist(layout, "PLA", "Alpha", [("v1", "One")])
    add_playlist(layout, "local1", "Faves", [("v1", "One")], media=False)
    local_meta = layout.video_meta_dir("local1", "v1") / "meta.json"
    doc = read_json(local_meta) or {}
    doc["files"] = {"media": "Alpha/01 - One.webm"}
    write_json(local_meta, doc)

    # The local playlist references, but does not own, the media.
    assert sync_playlist_files(layout, "local1").renamed == 0
    assert (layout.media_root / "Alpha" / "01 - One.webm").is_file()

    report = rename_folder(layout, "PLA", "Beta")

    assert report.errors == []
    assert not (layout.media_root / "Alpha").exists()
    assert (layout.media_root / "Beta" / "01 - One.webm").is_file()
    assert _media_ref(layout, "PLA", "v1") == "Beta/01 - One.webm"
    assert _media_ref(layout, "local1", "v1") == "Beta/01 - One.webm"
    playlist = read_json(layout.playlist_meta_dir("PLA") / "playlist.json") or {}
    assert playlist["folder"] == "Beta"
    assert _leftovers(layout) == []


def test_playlist_folder_assigned_and_deduped(tmp_path):
    layout = make_library(tmp_path)
    add_playlist(layout, "PLA", "Mix", [], media=False)
    path = layout.playlist_meta_dir("PLB") / "playlist.json"
    write_json(path, {"id": "PLB", "title": "Mix", "entries": []})

    folder = playlist_folder(layout, read_json(path) or {})

    assert folder == layout.media_root / "Mix [PLB]"
    assert (read_json(path) or {})["folder"] == "Mix [PLB]"


def test_pending_journal_finished_before_new_batch(tmp_path, monkeypatch):
    layout = _publish_pair(tmp_path)
    add_playlist(layout, "PL2", "Other", [("w1", "Same"), ("w2", "Same")], media=False)
    publish_download(layout, "PL2", "w1", _stage(tmp_path, "c.webm", b"three"))
    publish_download(layout, "PL2", "w2", _stage(tmp_path, "d.webm", b"four"))
    _swap_entries(layout, "PL1")
    _swap_entries(layout, "PL2")
    real_replace = os.replace
    phase1_calls = []

    def flaky_replace(src, dst, *args, **kwargs):
        if Path(dst).name.startswith(".ytoprename-"):
            phase1_calls.append(src)
            if len(phase1_calls) == 2:
                raise OSError("stick yanked")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", flaky_replace)
    with pytest.raises(OSError):
        sync_playlist_files(layout, "PL1")
    monkeypatch.undo()
    assert layout.rename_journal.exists()

    report = sync_playlist_files(layout, "PL2")

    assert report.errors == []
    assert recover_rename_journal(layout) == 0
    mix = layout.media_root / "Mix"
    other = layout.media_root / "Other"
    assert (mix / "01 - Same.webm").read_bytes() == b"two"
    assert (mix / "02 - Same.webm").read_bytes() == b"one"
    assert (other / "01 - Same.webm").read_bytes() == b"four"
    assert (other / "02 - Same.webm").read_bytes() == b"three"
    assert _media_ref(layout, "PL1", "v2") == "Mix/01 - Same.webm"
    assert _media_ref(layout, "PL1", "v1") == "Mix/02 - Same.webm"
    assert _media_ref(layout, "PL2", "w2") == "Other/01 - Same.webm"
    assert _media_ref(layout, "PL2", "w1") == "Other/02 - Same.webm"
    assert _leftovers(layout) == []
    assert not layout.rename_journal.exists()


def test_redownload_same_extension_replaces(tmp_path):
    layout = make_library(tmp_path)
    add_playlist(layout, "PL1", "Mix", [("v1", "First")], media=False)
    folder = layout.media_root / "Mix"
    publish_download(layout, "PL1", "v1", _stage(tmp_path, "a.webm", b"old"))
    (folder / "01 - First.en.srt").write_text("subs", encoding="utf-8")

    final = publish_download(layout, "PL1", "v1", _stage(tmp_path, "b.webm", b"new"))

    assert final == folder / "01 - First.webm"
    assert final.read_bytes() == b"new"
    assert sorted(p.name for p in folder.glob("*.webm")) == ["01 - First.webm"]
    assert list(folder.glob(".ytoppublish-*")) == []
    assert not (folder / "01 - First.en.srt").exists()
    assert sync_playlist_files(layout, "PL1").errors == []


def test_redownload_new_extension_leaves_no_orphan(tmp_path):
    layout = make_library(tmp_path)
    add_playlist(layout, "PL1", "Mix", [("v1", "First")], media=False)
    folder = layout.media_root / "Mix"
    publish_download(layout, "PL1", "v1", _stage(tmp_path, "a.webm", b"old"))

    final = publish_download(layout, "PL1", "v1", _stage(tmp_path, "b.mp4", b"new"))

    assert final == folder / "01 - First.mp4"
    assert sorted(p.name for p in folder.iterdir() if p.name != "folder.jpg") == [
        "01 - First.mp4"
    ]
    assert _media_ref(layout, "PL1", "v1") == "Mix/01 - First.mp4"


def test_redownload_keeps_media_referenced_elsewhere(tmp_path):
    layout = make_library(tmp_path)
    add_playlist(layout, "PL1", "Mix", [("v1", "First")], media=False)
    add_playlist(layout, "local1", "Faves", [("v1", "First")], media=False)
    folder = layout.media_root / "Mix"
    publish_download(layout, "PL1", "v1", _stage(tmp_path, "a.webm", b"old"))
    local_meta = layout.video_meta_dir("local1", "v1") / "meta.json"
    doc = read_json(local_meta) or {}
    doc["files"] = {"media": "Mix/01 - First.webm"}
    write_json(local_meta, doc)

    final = publish_download(layout, "PL1", "v1", _stage(tmp_path, "b.mp4", b"new"))

    assert final == folder / "01 - First.mp4"
    assert (folder / "01 - First.webm").read_bytes() == b"old"


def test_recover_moves_orphaned_publish_files(tmp_path):
    layout = make_library(tmp_path)
    add_playlist(layout, "PL1", "Mix", [("v1", "First")], media=False)
    folder = layout.media_root / "Mix"
    folder.mkdir(parents=True)
    (folder / "01 - First.webm").write_bytes(b"foreign")
    # Sync cannot rename this one, so it stays referenced under its temp name.
    kept = publish_download(layout, "PL1", "v1", _stage(tmp_path, "a.webm", b"mine"))
    assert kept.name.startswith(".ytoppublish-")
    orphan = folder / ".ytoppublish-deadbeef.webm"
    orphan.write_bytes(b"crashed")

    assert recover_rename_journal(layout) == 0

    assert not orphan.exists()
    moved = layout.cache_dir / "orphans" / orphan.name
    assert moved.read_bytes() == b"crashed"
    assert kept.read_bytes() == b"mine"


def test_recover_keeps_publish_files_a_move_journal_lists(tmp_path):
    """A same-device move journals its staged names; the startup sweep runs
    before move reconciliation and must leave those (and their subtitles)."""
    layout = make_library(tmp_path)
    add_playlist(layout, "PL1", "Mix", [("v1", "First")], media=False)
    folder = layout.media_root / "Moved"
    folder.mkdir(parents=True)
    staged = folder / ".ytoppublish-0badf00d.webm"
    staged.write_bytes(b"moved copy")
    subs = folder / ".ytoppublish-0badf00d.en.srt"
    subs.write_text("subs", encoding="utf-8")
    write_json(layout.meta_root / ".ytopmove-job1.json", {
        "schema": 1, "mode": "rename", "playlist_id": "PL2",
        "old_folder": "Moved", "dest_folder": "Moved",
        "refs": {"Elsewhere/01 - First.webm": staged.name},
    })
    orphan = folder / ".ytoppublish-deadbeef.webm"
    orphan.write_bytes(b"crashed")

    recover_rename_journal(layout)

    assert staged.read_bytes() == b"moved copy"
    assert subs.exists()
    assert not orphan.exists()


def test_recover_keeps_subtitles_of_referenced_publish_files(tmp_path):
    layout = make_library(tmp_path)
    add_playlist(layout, "PL1", "Mix", [("v1", "First")], media=False)
    folder = layout.media_root / "Mix"
    folder.mkdir(parents=True)
    (folder / "01 - First.webm").write_bytes(b"foreign")
    kept = publish_download(layout, "PL1", "v1", _stage(tmp_path, "a.webm", b"mine"))
    assert kept.name.startswith(".ytoppublish-")
    subs = kept.with_name(kept.stem + ".en.srt")
    subs.write_text("subs", encoding="utf-8")

    recover_rename_journal(layout)

    assert subs.exists()


def test_new_folder_never_reuses_a_name_on_disk(tmp_path):
    layout = make_library(tmp_path)
    stray = layout.media_root / "Mix"
    stray.mkdir(parents=True)
    (stray / "mine.webm").write_bytes(b"not a playlist's")
    add_playlist(layout, "PL1", "Mix", [("v1", "First")], media=False)
    path = layout.playlist_meta_dir("PL1") / "playlist.json"
    doc = read_json(path) or {}
    doc.pop("folder")
    write_json(path, doc)

    folder = playlist_folder(layout, doc)

    assert folder.name == "Mix [PL1]"
    # Its own folder is not "taken": renaming to the same title is a no-op.
    assert rename_folder(layout, "PL1", "Mix").errors == []
    assert playlist_folder(layout, read_json(path) or {}).name == "Mix [PL1]"


def test_a_playlist_titled_un_grouped_gets_its_own_folder(tmp_path):
    from ytop.layout import UNGROUPED_FOLDER

    layout = make_library(tmp_path)
    add_playlist(layout, "PL1", UNGROUPED_FOLDER, [("v1", "First")], media=False)
    doc = read_json(layout.playlist_meta_dir("PL1") / "playlist.json") or {}
    doc.pop("folder")

    folder = playlist_folder(layout, doc)

    assert folder.name.casefold() != UNGROUPED_FOLDER.casefold()
    assert folder.name == f"{UNGROUPED_FOLDER} [PL1]"
