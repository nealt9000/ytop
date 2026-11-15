"""Shared builders for portable-library tests."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from ytop.fsnames import numbered_stem, safe_name
from ytop.layout import (
    UNGROUPED_FOLDER,
    UNGROUPED_ID,
    LibraryLayout,
    init_library,
)
from ytop.libraries import LibrarySet
from ytop.yt.metadata import write_json


def make_library(tmp: Path, label: str = "Test") -> LibraryLayout:
    root = Path(tmp) / "lib"
    init_library(root, label)
    return LibraryLayout(root)


def add_playlist(
    layout: LibraryLayout,
    playlist_id: str,
    title: str,
    videos: list[tuple[str, str]],
    media: bool = True,
    folder: str | None = None,
) -> Path:
    """Write a playlist's metadata (and optionally media); returns its meta dir.

    ``folder`` is the folder name directly under the media root; it defaults to
    ``safe_name(title)`` (``Un-grouped`` for the Un-grouped playlist).
    """
    if folder is None:
        folder = UNGROUPED_FOLDER if playlist_id == UNGROUPED_ID else safe_name(title)
    meta = layout.playlist_meta_dir(playlist_id)
    meta.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (4, 4)).save(meta / "playlist-thumb.jpg")
    write_json(
        meta / "playlist.json",
        {
            "id": playlist_id,
            "title": title,
            "folder": folder,
            "entries": [
                {
                    "id": vid,
                    "title": vtitle,
                    "index": index,
                    "duration": 60,
                    "available": True,
                }
                for index, (vid, vtitle) in enumerate(videos, start=1)
            ],
            "files": {"thumbnail": "playlist-thumb.jpg"},
        },
    )
    media_folder = layout.media_root / folder
    total = len(videos)
    for index, (vid, vtitle) in enumerate(videos, start=1):
        vdir = layout.video_meta_dir(playlist_id, vid)
        files: dict = {}
        if media:
            media_folder.mkdir(parents=True, exist_ok=True)
            stem = numbered_stem(index, total, vtitle)
            media_path = media_folder / f"{stem}.webm"
            media_path.write_bytes(vid.encode().ljust(1024, b"\0"))
            files["media"] = layout.relative_media(media_path)
        write_json(vdir / "meta.json", {"id": vid, "title": vtitle, "files": files})
        Image.new("RGB", (4, 4)).save(vdir / "thumb.jpg")
    return meta


def lib_root(tmp: Path, label: str = "Test") -> Path:
    """The root of ``tmp``'s test library, created on first use."""
    root = Path(tmp) / "lib"
    init_library(root, label)
    return root


def add_big_library(layout: LibraryLayout, playlists: int = 50, videos: int = 100) -> None:
    """The timing fixture: ``playlists`` x ``videos`` videos, no media.  Call it
    inside a test body, never a module fixture (discovery isolation is
    autouse per test)."""
    for p in range(playlists):
        add_playlist(
            layout, f"PL{p}", f"List {p}",
            [(f"v{p}_{i}", f"Lecture {i}") for i in range(videos)], media=False,
        )


def library_set(root: Path | LibraryLayout | None) -> LibrarySet:
    """A LibrarySet for tests: no mount scanning (the registry is isolated
    by ``conftest``)."""
    if isinstance(root, LibraryLayout):
        root = root.root
    return LibrarySet(root, detect=False)


def make_old_playlist(
    root: Path,
    playlist_id: str,
    title: str,
    *,
    videos: tuple[tuple[str, str], ...] = (("vid1", "Video One"),),
    media: bool = True,
    ext: str = "mp4",
    options: dict | None = None,
    kind: str | None = None,
    audio_for: tuple[str, ...] = (),
    transcript_for: tuple[str, ...] = (),
    partial_for: tuple[str, ...] = (),
) -> Path:
    """Build a playlist in the pre-portable layout (for migration tests):
    ``playlists/<title> [<id>]/videos/<vid>/video.<ext>``.

    ``audio_for`` videos get an ``audio/<fmt>-<n>.<ext>`` alternate track,
    ``transcript_for`` a ``transcript.json`` and ``partial_for`` only a
    leftover yt-dlp fragment (``video.f249-17.webm.part``) instead of media.
    """
    playlist_dir = root / "playlists" / f"{title} [{playlist_id}]"
    playlist_dir.mkdir(parents=True)
    Image.new("RGB", (160, 90), (40, 70, 110)).save(
        playlist_dir / "playlist-thumb.jpg", "JPEG"
    )
    entries = []
    for index, (video_id, video_title) in enumerate(videos, start=1):
        video_dir = playlist_dir / "videos" / video_id
        video_dir.mkdir(parents=True)
        has_media = media and video_id not in partial_for
        media_name = f"video.{ext}"
        if has_media:
            (video_dir / media_name).write_bytes(
                f"media-{video_id}".encode() * 100
            )
        if video_id in partial_for:
            (video_dir / "video.f249-17.webm.part").write_bytes(b"partial" * 50)
        Image.new("RGB", (160, 90), (80, 40, 40)).save(
            video_dir / "thumb.jpg", "JPEG"
        )
        write_json(video_dir / "raw.json", {"id": video_id})
        meta: dict = {
            "schema": 1, "id": video_id, "title": video_title,
            "description": "", "channel": "Test Channel",
            "duration": 100,
            "format": {
                "video_codec": "av01", "audio_codec": "opus",
                "audio_bitrate": 160, "height": 240, "fps": 15,
                "container": "webm",
            },
            "files": {
                "video": media_name if has_media else None,
                "thumbnail": "thumb.jpg",
            },
        }
        if video_id in audio_for:
            audio = video_dir / "audio" / "251-1.webm"
            audio.parent.mkdir()
            audio.write_bytes(f"audio-{video_id}".encode() * 50)
            meta["audio_tracks"] = [{
                "id": "251-1", "label": "English", "language": "en",
                "codec": "opus", "bitrate": 160, "original": True,
                "file": "audio/251-1.webm", "size": audio.stat().st_size,
            }]
        if video_id in transcript_for:
            write_json(video_dir / "transcript.json", {
                "schema": 1, "video_id": video_id, "title": video_title,
                "language": "en", "kind": "manual",
                "segments": [{"start": 0.0, "end": 2.5, "text": "Hello there"}],
            })
            (video_dir / "subtitles").mkdir()
            (video_dir / "subtitles" / f"{video_id}.en.vtt").write_text(
                "WEBVTT\n", encoding="utf-8"
            )
        write_json(video_dir / "meta.json", meta)
        entries.append({
            "id": video_id, "index": index, "title": video_title,
            "path": f"videos/{video_id}",
            "video": media_name if has_media else None,
            "available": has_media, "duration": 100,
        })
    doc: dict = {
        "schema": 1, "id": playlist_id, "title": title,
        "description": "", "channel": "Test Channel",
        "video_count": len(entries), "entries": entries,
        "options": dict(options or {}),
        "files": {"thumbnail": "playlist-thumb.jpg"},
    }
    if kind:
        doc["kind"] = kind
    write_json(playlist_dir / "playlist.json", doc)
    return playlist_dir
