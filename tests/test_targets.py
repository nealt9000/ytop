from __future__ import annotations

import json
from pathlib import Path

import pytest

from factories import add_playlist, make_library
from ytop.targets import (
    Target,
    TargetError,
    parse_target,
    resolve_playlist,
    resolve_video,
)


def test_parse_target_accepts_playlist_and_video_forms():
    assert parse_target("PLBOgSgXfJ6B2nbZ_YREW_Nb-AX8FW9U9K") == Target(
        "playlist", "PLBOgSgXfJ6B2nbZ_YREW_Nb-AX8FW9U9K"
    )
    assert parse_target(
        "https://www.youtube.com/playlist?list=PL123&index=2"
    ) == Target("playlist", "PL123")
    assert parse_target(
        "https://www.youtube.com/watch?v=lWZ2Bz0tS-s&list=PL123"
    ) == Target("playlist", "PL123")
    assert parse_target(
        "https://www.youtube.com/watch?v=lWZ2Bz0tS-s"
    ) == Target("video", "lWZ2Bz0tS-s")
    assert parse_target("https://youtu.be/lWZ2Bz0tS-s") == Target(
        "video", "lWZ2Bz0tS-s"
    )
    assert parse_target("lWZ2Bz0tS-s") == Target("video", "lWZ2Bz0tS-s")


def test_parse_target_rejects_junk():
    with pytest.raises(TargetError):
        parse_target("")
    with pytest.raises(TargetError):
        parse_target("not a target")
    with pytest.raises(TargetError):
        parse_target("https://example.com/watch")


def test_resolve_playlist_and_video(tmp_path: Path):
    layout = make_library(tmp_path)
    playlist_dir = add_playlist(layout, "PL1", "A List", [("vid1", "Video 1")])
    found = resolve_playlist(layout, "PL1")
    assert found is not None
    assert found[0] == playlist_dir

    found_video = resolve_video(layout, "vid1")
    assert found_video is not None
    pdir, doc, entry, vdir, meta = found_video
    assert pdir == playlist_dir
    assert doc["id"] == "PL1"
    assert entry["id"] == "vid1"
    assert vdir == layout.video_meta_dir("PL1", "vid1")
    assert meta["title"] == "Video 1"

    assert resolve_playlist(layout, "missing") is None
    assert resolve_video(layout, "missing") is None


def test_resolve_video_finds_an_orphaned_meta_dir(tmp_path: Path):
    layout = make_library(tmp_path)
    playlist_dir = add_playlist(layout, "PL1", "A List", [("vid1", "Video 1")])
    doc_path = playlist_dir / "playlist.json"
    doc = json.loads(doc_path.read_text())
    doc["entries"] = []
    doc_path.write_text(json.dumps(doc), encoding="utf-8")

    found = resolve_video(layout, "vid1")
    assert found is not None
    assert found[3] == layout.video_meta_dir("PL1", "vid1")
    assert found[2] == {"id": "vid1"}


def test_parse_target_prefer_video():
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123"
    assert parse_target(url) == Target("playlist", "PL123")
    assert parse_target(url, prefer_video=True) == Target("video", "dQw4w9WgXcQ")
    pure = "https://www.youtube.com/playlist?list=PLx"
    assert parse_target(pure, prefer_video=True) == Target("playlist", "PLx")


@pytest.mark.parametrize("url", [
    "https://youtu.be/dQw4w9WgXcQ?list=PL123",
    "https://www.youtube.com/shorts/dQw4w9WgXcQ?list=PL123",
    "https://www.youtube.com/embed/dQw4w9WgXcQ?list=PL123",
    "https://www.youtube.com/live/dQw4w9WgXcQ?list=PL123",
])
def test_parse_target_prefer_video_path_ids(url):
    assert parse_target(url) == Target("playlist", "PL123")
    assert parse_target(url, prefer_video=True) == Target("video", "dQw4w9WgXcQ")
