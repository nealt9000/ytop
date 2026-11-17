"""Local playlists store each media file once: references, never hard links."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from factories import add_playlist, library_set, make_library
from ytop.layout import LibraryLayout, init_library
from ytop.web.library import Library, media_references
from ytop.yt.metadata import read_json, write_json

SOURCE_ID = "PLSHARED000000000000000000000001"
USB_ID = "PLSHAREDUSB000000000000000000002"


def _add_audio(layout: LibraryLayout, playlist_id: str, video_id: str) -> Path:
    """Give one video an alternate audio track stored beside its meta.json."""
    video_dir = layout.video_meta_dir(playlist_id, video_id)
    (video_dir / "audio").mkdir()
    track = video_dir / "audio" / "es.m4a"
    track.write_bytes(b"spanish".ljust(512, b"\0"))
    meta = read_json(video_dir / "meta.json") or {}
    meta["audio_tracks"] = [
        {"id": "es", "label": "Spanish", "file": "audio/es.m4a", "codec": "mp4a"}
    ]
    write_json(video_dir / "meta.json", meta)
    return track


def _media_ref(layout: LibraryLayout, playlist_id: str, video_id: str) -> str:
    meta = read_json(layout.video_meta_dir(playlist_id, video_id) / "meta.json")
    return meta["files"]["media"]


def _media_files(layout: LibraryLayout) -> list[Path]:
    return sorted(p for p in layout.media_root.rglob("*.webm"))


@pytest.fixture
def home(tmp_path: Path) -> LibraryLayout:
    layout = make_library(tmp_path / "home", "Laptop")
    add_playlist(
        layout, SOURCE_ID, "Source",
        [("vida", "Video A"), ("vidb", "Video B")],
    )
    _add_audio(layout, SOURCE_ID, "vidb")
    return layout


def test_create_playlist_references_media(home: LibraryLayout):
    library = Library(library_set(home))
    before = _media_files(home)

    local_id = library.create_playlist("Mix", [(SOURCE_ID, "vidb")])

    assert local_id is not None
    assert _media_files(home) == before  # no new media bytes
    assert _media_ref(home, local_id, "vidb") == _media_ref(home, SOURCE_ID, "vidb")
    assert all(os.stat(p).st_nlink == 1 for p in _media_files(home))
    # Alternate audio belongs to the owner's copy; it is not duplicated...
    assert not (home.video_meta_dir(local_id, "vidb") / "audio").exists()
    # ...yet the referencing playlist can still play it.
    track = library.audio_path(local_id, "vidb", "es")
    assert track == home.video_meta_dir(SOURCE_ID, "vidb") / "audio" / "es.m4a"
    view = library.video_view(local_id, "vidb")
    assert view is not None and [t["id"] for t in view.audio_tracks] == ["es"]


def test_media_references_lists_every_playlist(home: LibraryLayout):
    library = Library(library_set(home))
    local_id = library.create_playlist("Mix", [(SOURCE_ID, "vidb")])
    ref = _media_ref(home, SOURCE_ID, "vidb")

    assert sorted(media_references(home, ref)) == sorted(
        [(SOURCE_ID, "vidb"), (local_id, "vidb")]
    )
    assert media_references(home, "Nowhere/x.webm") == []


def test_remove_owner_transfers_media(home: LibraryLayout):
    library = Library(library_set(home))
    local_id = library.create_playlist("Mix", [(SOURCE_ID, "vidb")])
    assert local_id is not None
    content = library.media_path(SOURCE_ID, "vidb").read_bytes()
    unshared = library.media_path(SOURCE_ID, "vida")

    assert library.remove_playlist(SOURCE_ID)

    ref = _media_ref(home, local_id, "vidb")
    doc = read_json(home.playlist_meta_dir(local_id) / "playlist.json")
    assert ref == f"{doc['folder']}/01 - Video B.webm"
    moved = library.media_path(local_id, "vidb")
    assert moved == home.media_root / ref
    assert moved.read_bytes() == content
    # Only the media nobody else referenced is gone.
    assert not unshared.exists()
    assert _media_files(home) == [moved]
    assert not (home.media_root / "Source").exists()
    # The alternate audio followed the media to its new owner.
    track = library.audio_path(local_id, "vidb", "es")
    assert track == home.video_meta_dir(local_id, "vidb") / "audio" / "es.m4a"
    assert track.read_bytes().startswith(b"spanish")


def test_remove_owner_video_transfers_media(home: LibraryLayout):
    library = Library(library_set(home))
    first = library.create_playlist("Mix", [(SOURCE_ID, "vidb")])
    second = library.create_playlist("Other", [(SOURCE_ID, "vidb")])
    assert first is not None and second is not None
    content = library.media_path(SOURCE_ID, "vidb").read_bytes()

    assert library.remove_video(SOURCE_ID, "vidb")

    refs = {_media_ref(home, pid, "vidb") for pid in (first, second)}
    assert len(refs) == 1  # every reference follows the move
    ref = refs.pop()
    owner = sorted([first, second])[0]  # first referencing playlist
    doc = read_json(home.playlist_meta_dir(owner) / "playlist.json")
    assert ref.startswith(f"{doc['folder']}/01 - ")
    assert (home.media_root / ref).read_bytes() == content
    assert library.media_path(second, "vidb") == home.media_root / ref
    assert not home.video_meta_dir(SOURCE_ID, "vidb").exists()
    assert not list(home.media_root.rglob(".ytop*"))
    assert not home.rename_journal.exists()


def test_remove_reference_keeps_owner(home: LibraryLayout):
    library = Library(library_set(home))
    mix = library.create_playlist("Mix", [(SOURCE_ID, "vidb")])
    solo = library.create_playlist("Solo", [(SOURCE_ID, "vidb")])
    assert mix is not None and solo is not None
    owner_media = library.media_path(SOURCE_ID, "vidb")
    owner_ref = _media_ref(home, SOURCE_ID, "vidb")

    assert library.remove_playlist(mix)
    assert library.remove_video(solo, "vidb")

    assert owner_media.is_file()
    assert _media_ref(home, SOURCE_ID, "vidb") == owner_ref
    assert library.media_path(SOURCE_ID, "vidb") == owner_media
    assert (home.video_meta_dir(SOURCE_ID, "vidb") / "audio" / "es.m4a").is_file()
    assert media_references(home, owner_ref) == [(SOURCE_ID, "vidb")]


def test_remove_video_keeping_media_drops_the_reference(home: LibraryLayout):
    library = Library(library_set(home))
    mix = library.create_playlist("Mix", [(SOURCE_ID, "vidb")])
    assert mix is not None
    unshared = library.media_path(SOURCE_ID, "vida")

    # Shared: the entry's meta goes and ownership moves to the reference.
    assert library.remove_video(SOURCE_ID, "vidb", delete_media=False)
    assert not home.video_meta_dir(SOURCE_ID, "vidb").exists()
    ref = _media_ref(home, mix, "vidb")
    assert media_references(home, ref) == [(mix, "vidb")]
    assert library.media_path(mix, "vidb") is not None

    # Unshared: the file stays on disk, but no meta still counts as using it.
    assert library.remove_video(SOURCE_ID, "vida", delete_media=False)
    assert not home.video_meta_dir(SOURCE_ID, "vida").exists()
    assert unshared.is_file()
    assert media_references(home, home.relative_media(unshared)) == []


def test_cross_library_selection_copies(home: LibraryLayout, tmp_path: Path):
    usb = LibraryLayout(tmp_path / "usb")
    init_library(usb.root, "USB stick")
    add_playlist(usb, USB_ID, "Stick", [("vidu", "On the stick")])
    libs = library_set(home)
    usb_ref = libs.add_folder(usb.root, "USB stick")
    library = Library(libs)
    source_media = library.media_path(SOURCE_ID, "vidb")
    content = source_media.read_bytes()

    local_id = library.create_playlist(
        "Mixed", [(SOURCE_ID, "vidb"), (USB_ID, "vidu")], library_id=usb_ref.id
    )

    assert local_id is not None
    assert (usb.playlist_meta_dir(local_id) / "playlist.json").is_file()
    copied = library.media_path(local_id, "vidb")
    doc = read_json(usb.playlist_meta_dir(local_id) / "playlist.json")
    assert copied == usb.media_root / doc["folder"] / "01 - Video B.webm"
    assert copied.read_bytes() == content
    assert source_media.read_bytes() == content  # the source is untouched
    # The same-library selection is still a reference, not a copy.
    assert _media_ref(usb, local_id, "vidu") == _media_ref(usb, USB_ID, "vidu")
    # The copy carries its own alternate audio.
    track = library.audio_path(local_id, "vidb", "es")
    assert track == usb.video_meta_dir(local_id, "vidb") / "audio" / "es.m4a"
    assert not list(usb.meta_root.glob(".*"))
    assert not list(usb.cache_dir.rglob("*.webm"))


def test_no_os_link_calls(home: LibraryLayout, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("os.link must not be used")

    monkeypatch.setattr(os, "link", refuse)
    monkeypatch.setattr(os, "symlink", refuse)
    library = Library(library_set(home))

    local_id = library.create_playlist(
        "Mix", [(SOURCE_ID, "vida"), (SOURCE_ID, "vidb")]
    )

    assert local_id is not None
    doc = json.loads(
        (home.playlist_meta_dir(local_id) / "playlist.json").read_text()
    )
    assert [row["id"] for row in doc["entries"]] == ["vida", "vidb"]


def test_remove_owner_during_create_keeps_media(home: LibraryLayout, monkeypatch):
    """A removal of the owner racing a create must see the new reference."""
    import shutil
    import threading

    import ytop.web.library as library_module

    library = Library(library_set(home))
    content = library.media_path(SOURCE_ID, "vidb").read_bytes()
    real_copytree = shutil.copytree
    remover: list[threading.Thread] = []

    def copytree_then_race(*args, **kwargs):
        result = real_copytree(*args, **kwargs)
        if not remover:
            # The owner is removed while the new playlist is still staged.
            thread = threading.Thread(
                target=library.remove_playlist, args=(SOURCE_ID,)
            )
            remover.append(thread)
            thread.start()
            thread.join(timeout=0.5)
        return result

    monkeypatch.setattr(library_module.shutil, "copytree", copytree_then_race)
    local_id = library.create_playlist("Mix", [(SOURCE_ID, "vidb")])
    monkeypatch.setattr(library_module.shutil, "copytree", real_copytree)
    remover[0].join(timeout=5)

    assert local_id is not None
    assert not remover[0].is_alive()
    assert library.resolve_playlist(SOURCE_ID) is None
    media = library.media_path(local_id, "vidb")
    assert media is not None and media.read_bytes() == content
    assert media_references(home, home.relative_media(media)) == [(local_id, "vidb")]


def test_video_without_audio_tracks_does_not_scan(home: LibraryLayout, monkeypatch):
    import ytop.web.library as library_module

    library = Library(library_set(home))

    def no_scan(*args, **kwargs):
        raise AssertionError("media_references scanned the library")

    monkeypatch.setattr(library_module, "media_references", no_scan)
    view = library.video_view(SOURCE_ID, "vida")
    assert view is not None
    assert library.audio_path(SOURCE_ID, "vida", "es") is None


def test_cross_library_copy_keeps_srt_and_checks_fat32(
    home: LibraryLayout, tmp_path: Path, monkeypatch
):
    from ytop import fsinfo
    from ytop.publish import sync_playlist_files
    from ytop.transcripts.captions import Segment
    from ytop.transcripts.store import write_transcript

    usb = LibraryLayout(tmp_path / "usb")
    init_library(usb.root, "USB stick")
    libs = library_set(home)
    usb_ref = libs.add_folder(usb.root, "USB stick")
    library = Library(libs)
    write_transcript(
        home.video_meta_dir(SOURCE_ID, "vidb"), video_id="vidb", title="B",
        language="en", kind="manual", segments=[Segment(0, 1, "hello")],
        raw_file=None,
    )
    sync_playlist_files(home, SOURCE_ID)

    copy_id = library.create_playlist(
        "Copy", [(SOURCE_ID, "vidb")], library_id=usb_ref.id
    )
    copied = library.media_path(copy_id, "vidb")
    assert copied.with_name(copied.stem + ".en.srt").is_file()

    monkeypatch.setattr(
        fsinfo, "fs_info",
        lambda path: fsinfo.FsInfo("vfat", 10)
        if Path(path).is_relative_to(usb.root) else fsinfo.FsInfo("ext4", None),
    )
    before = sorted(p.name for p in usb.meta_root.iterdir())
    with pytest.raises(ValueError, match="FAT32"):
        library.create_playlist("Big", [(SOURCE_ID, "vidb")], library_id=usb_ref.id)
    assert sorted(p.name for p in usb.meta_root.iterdir()) == before
    assert not list(usb.cache_dir.glob("import-*"))
