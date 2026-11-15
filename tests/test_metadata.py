from __future__ import annotations

import json
import os
from pathlib import Path

from factories import add_playlist, make_library
from ytop.yt.formats import Selection
from ytop.yt.metadata import (
    chapters_from_description,
    find_media,
    normalize_chapters,
    normalize_playlist,
    normalize_video,
    playlist_directory,
    remove_media,
    sanitize_component,
    video_directory,
    write_json,
)


def selection() -> Selection:
    return Selection(
        video={
            "format_id": "395",
            "vcodec": "av01.0.00M.08",
            "height": 240,
            "width": 320,
            "fps": 15,
        },
        audio={"format_id": "249-9", "acodec": "opus", "language": "en-US"},
    )


def test_sanitize_component_handles_unicode_and_separators():
    assert sanitize_component("Gödel, Escher/Bach") == "Godel, Escher Bach"
    assert sanitize_component("  ...  ") == "playlist"


def test_playlist_directory_is_the_meta_dir_keyed_by_id(tmp_path: Path):
    layout = make_library(tmp_path)
    first = playlist_directory(layout, {"id": "PL123", "title": "A List"})
    renamed = playlist_directory(layout, {"id": "PL123", "title": "New Name"})
    other = playlist_directory(layout, {"id": "PL999", "title": "A List"})
    assert first == renamed == layout.playlist_meta_dir("PL123")
    assert other == layout.playlist_meta_dir("PL999")


def test_video_directory_is_the_video_meta_dir(tmp_path: Path):
    layout = make_library(tmp_path)
    assert video_directory(layout, "PL1", "vid1") == layout.video_meta_dir(
        "PL1", "vid1"
    )


def test_normalize_video_keeps_description_chapters_and_first_seen():
    info = {
        "id": "abc",
        "title": "A Talk",
        "description": "Line one\n\n00:00 Intro",
        "channel": "Channel",
        "channel_id": "UC1",
        "upload_date": "20130101",
        "duration": 100,
        "webpage_url": "https://www.youtube.com/watch?v=abc",
        "thumbnail": "https://example/thumb.jpg",
        "chapters": [
            {"start_time": 0, "end_time": 10, "title": "Intro"},
            {"start_time": 10, "end_time": 100, "title": "Body"},
            {"start_time": None, "title": "Broken"},
        ],
    }
    meta = normalize_video(
        info,
        index=1,
        playlist={"id": "PL1", "title": "List"},
        video_dir=Path("/tmp/v"),
        thumbnail_file="thumb.jpg",
        media_file="video.webm",
        selection=selection(),
        existing={"first_seen_at": "2020-01-01T00:00:00+00:00"},
    )
    assert meta["description"] == info["description"]
    assert [c["title"] for c in meta["chapters"]] == ["Intro", "Body"]
    assert meta["first_seen_at"] == "2020-01-01T00:00:00+00:00"
    assert meta["files"]["media"] == "video.webm"
    assert meta["format"]["container"] == "webm"


def test_normalize_chapters_drops_invalid_entries():
    assert normalize_chapters([{"title": ""}, {"start_time": 5}]) == []


def test_chapters_from_description_needs_two_timestamp_lines():
    description = "A talk.\n0:00 Intro\n1:23 Main idea\n01:02:03 End"
    chapters = chapters_from_description(description)
    assert [chapter["title"] for chapter in chapters] == [
        "Intro", "Main idea", "End"
    ]
    assert chapters[0]["end_time"] == 83.0
    assert chapters[-1]["end_time"] is None
    assert chapters_from_description("Only 5:00 in prose") == []
    assert chapters_from_description("0:00 Intro") == []


def test_chapters_from_description_accepts_common_separators():
    chapters = chapters_from_description("00:00 - A\n00:30 — B\n1:00: C")
    assert [chapter["title"] for chapter in chapters] == ["A", "B", "C"]
    assert [chapter["start_time"] for chapter in chapters] == [0.0, 30.0, 60.0]


def test_normalize_video_falls_back_to_description_chapters():
    info = {
        "id": "abc",
        "title": "A Talk",
        "description": "0:00 Opening\n2:00 Body",
        "chapters": [],
    }
    meta = normalize_video(
        info,
        index=1,
        playlist={"id": "PL1", "title": "List"},
        video_dir=Path("/tmp/v"),
        thumbnail_file=None,
        media_file="video.webm",
        selection=selection(),
    )
    assert [chapter["title"] for chapter in meta["chapters"]] == [
        "Opening", "Body"
    ]
    assert meta["chapters"][0]["end_time"] == 120.0


def test_normalize_playlist_preserves_order_and_first_seen():
    entries = [
        {"id": "b", "index": 2},
        {"id": "a", "index": 1},
    ]
    doc = normalize_playlist(
        {"id": "PL1", "title": "List", "channel": "C"},
        entries=entries,
        thumbnail_file="playlist-thumb.jpg",
        existing={"first_seen_at": "2020-01-01T00:00:00+00:00"},
    )
    assert doc["entries"] == entries
    assert doc["video_count"] == 2
    assert doc["first_seen_at"] == "2020-01-01T00:00:00+00:00"


def test_find_media_resolves_the_meta_reference(tmp_path: Path):
    layout = make_library(tmp_path)
    add_playlist(layout, "PL1", "List", [("vid1", "One")])
    vdir = layout.video_meta_dir("PL1", "vid1")
    assert find_media(layout, vdir) == layout.media_root / "List" / "01 - One.webm"


def test_find_media_does_not_glob_and_rejects_empty_files(tmp_path: Path):
    layout = make_library(tmp_path)
    add_playlist(layout, "PL1", "List", [("vid1", "One")], media=False)
    vdir = layout.video_meta_dir("PL1", "vid1")
    # A stray old-layout file in the meta dir is not media.
    (vdir / "video.webm").write_bytes(b"media")
    assert find_media(layout, vdir) is None

    empty = layout.media_root / "List" / "01 - One.webm"
    empty.parent.mkdir(parents=True, exist_ok=True)
    empty.write_bytes(b"")
    write_json(vdir / "meta.json", {
        "id": "vid1", "files": {"media": "List/01 - One.webm"},
    })
    assert find_media(layout, vdir) is None
    write_json(vdir / "meta.json", {"id": "vid1", "files": {"media": "../x"}})
    assert find_media(layout, vdir) is None


def test_remove_media_deletes_media_srt_and_audio(tmp_path: Path):
    layout = make_library(tmp_path)
    add_playlist(layout, "PL1", "List", [("vid1", "One")])
    vdir = layout.video_meta_dir("PL1", "vid1")
    media = layout.media_root / "List" / "01 - One.webm"
    srt = media.with_name("01 - One.en.srt")
    srt.write_text("1\n", encoding="utf-8")
    unrelated = media.with_name("01 - Onex.en.srt")
    unrelated.write_text("1\n", encoding="utf-8")
    (vdir / "audio").mkdir()
    (vdir / "audio" / "249-0.webm").write_bytes(b"audio")

    remove_media(layout, vdir)

    assert not media.exists()
    assert not srt.exists()
    assert unrelated.exists()
    assert not (vdir / "audio").exists()
    meta = json.loads((vdir / "meta.json").read_text())
    assert meta["files"].get("media") is None
    assert meta["title"] == "One"


def test_normalize_video_preserves_the_transcript_block():
    existing = {
        "first_seen_at": "2026-01-01T00:00:00+00:00",
        "transcript": {"language": "en", "segments": 42,
                       "file": "transcript.json"},
    }
    meta = normalize_video(
        {"id": "abc", "title": "A Talk", "duration": 60},
        index=1,
        playlist={"id": "PL1", "title": "List"},
        video_dir=Path("/tmp/v"),
        thumbnail_file=None,
        media_file="video.webm",
        selection=selection(),
        existing=existing,
    )
    assert meta["transcript"] == existing["transcript"]


def test_normalize_playlist_preserves_the_transcript_summary():
    existing = {"transcripts": {"videos": 2, "available": 2, "segments": 99}}
    doc = normalize_playlist(
        {"id": "PL1", "title": "List"},
        entries=[],
        thumbnail_file=None,
        existing=existing,
    )
    assert doc["transcripts"] == existing["transcripts"]


def test_document_lock_sidecars_live_in_the_instance_dir(tmp_path, monkeypatch):
    import stat as stat_mod

    from ytop.yt import metadata as md

    inst = tmp_path / "inst"
    monkeypatch.setenv("YTOP_INSTANCE_DIR", str(inst))
    doc = tmp_path / "lib" / "playlist.json"
    with md.document_lock(doc):
        sidecars = list((inst / "locks").glob("*.lock"))
        assert len(sidecars) == 1
        # Held: pruning leaves it alone.
        assert md.prune_lock_sidecars() == 0
        assert sidecars[0].exists()
    if os.name != "nt":
        assert stat_mod.S_IMODE((inst / "locks").stat().st_mode) == 0o700
        assert stat_mod.S_IMODE(inst.stat().st_mode) == 0o700
    assert md.prune_lock_sidecars() == 1
    assert list((inst / "locks").glob("*.lock")) == []
    with md.document_lock(doc):  # recreated on demand
        assert len(list((inst / "locks").glob("*.lock"))) == 1


def test_document_lock_retries_when_its_sidecar_was_pruned(tmp_path, monkeypatch):
    from ytop.yt import metadata as md

    monkeypatch.setenv("YTOP_INSTANCE_DIR", str(tmp_path / "inst"))
    doc = tmp_path / "doc.json"
    key = md._lock_key(doc)
    real_lock = md._lock_fd
    pruned: list[bool] = []

    def lock_then_prune(fd):
        real_lock(fd)
        if not pruned:  # another process pruned it while we waited
            pruned.append(True)
            md._sidecar(key).unlink()

    monkeypatch.setattr(md, "_lock_fd", lock_then_prune)
    fd = md._open_locked(key)
    try:
        assert md._same_file(fd, md._sidecar(key))
    finally:
        md._unlock_fd(fd)
        os.close(fd)
    assert pruned == [True]


def test_normalize_video_keeps_categories_and_tags() -> None:
    info = {"id": "x", "categories": ["Music"], "tags": ["a"]}
    doc = normalize_video(
        info,
        index=1,
        playlist={"id": "PL1", "title": "List"},
        video_dir=Path("/tmp/v"),
        thumbnail_file=None,
        media_file=None,
        selection=None,
    )
    assert doc["categories"] == ["Music"]
    assert doc["tags"] == ["a"]
