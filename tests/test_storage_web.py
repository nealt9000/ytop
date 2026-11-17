from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from factories import add_playlist, library_set, make_library
from ytop.config import Config
from ytop.layout import LibraryLayout, init_library
from ytop.web import jobs as jobs_module
from ytop.web.app import create_app
from ytop.web.jobs import JobManager
from ytop.web.library import Library
from ytop.yt.download import FetchResult
from ytop.yt.metadata import write_json

PLAYLIST_ID = "PLSTORAGE0000000000000000000001"
USB_PLAYLIST_ID = "PLUSB00000000000000000000000002"


def two_libraries(tmp_path: Path, *, usb_playlist: bool = True):
    """A launch library with one playlist and a registered USB library."""
    home = make_library(tmp_path / "home", "Laptop")
    add_playlist(home, PLAYLIST_ID, "Local", [("vid1", "Video One")])
    usb = LibraryLayout(tmp_path / "usb")
    init_library(usb.root, "USB stick")
    if usb_playlist:
        add_playlist(usb, USB_PLAYLIST_ID, "Drive lectures", [("vid1", "On the drive")])
    libs = library_set(home)
    usb_ref = libs.add_folder(usb.root, "USB stick")
    return home, usb, libs, usb_ref


def _wait(manager: JobManager, job_id: str, statuses: set[str], timeout=5.0):
    deadline = time.time() + timeout
    job = manager.get(job_id)
    while time.time() < deadline:
        job = manager.get(job_id)
        if job is not None and job.status in statuses:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job did not reach {statuses}: {job}")


def _probe_one(value, config, **kwargs):
    item = jobs_module.ProbedItem(
        index=1, id="vid1",
        url="https://www.youtube.com/watch?v=vid1",
        title="One", duration=10,
    )
    item.error = "not used in this test"
    return jobs_module.ProbeResult(
        url=value,
        playlist={"id": "PLNEW", "title": "New", "entries": []},
        items=[item],
    )


def test_library_shows_every_library_and_hides_one_when_unplugged(tmp_path: Path):
    home, usb, libs, _ = two_libraries(tmp_path)
    library = Library(libs)

    playlists = {playlist.id: playlist for playlist in library.playlists()}
    assert set(playlists) == {PLAYLIST_ID, USB_PLAYLIST_ID}
    assert playlists[USB_PLAYLIST_ID].library_label == "USB stick"
    assert playlists[PLAYLIST_ID].library_label == "Laptop"
    media = library.media_path(USB_PLAYLIST_ID, "vid1")
    assert media is not None and media.is_relative_to(usb.media_root)

    usb.root.rename(tmp_path / "unplugged")
    libs.refresh()
    assert library.resolve_playlist(USB_PLAYLIST_ID) is None
    assert [p.id for p in library.playlists()] == [PLAYLIST_ID]


def test_progress_and_titles_are_stored_per_library(tmp_path: Path):
    home, usb, libs, _ = two_libraries(tmp_path)
    client = TestClient(create_app(home.root, libraries=libs))

    assert client.post("/api/progress", json={
        "playlist_id": USB_PLAYLIST_ID, "video_id": "vid1",
        "position": 30, "duration": 100,
    }).status_code == 200
    client.post(
        f"/playlist/{USB_PLAYLIST_ID}/title", data={"title": "Stick title"},
        follow_redirects=False,
    )

    usb_progress = json.loads(usb.progress_file.read_text())
    assert usb_progress["videos"]["vid1"]["position"] == 30
    assert not home.progress_file.exists()
    assert json.loads(usb.titles_file.read_text())["playlists"] == {
        USB_PLAYLIST_ID: "Stick title"
    }
    assert not home.titles_file.exists()
    # The same video id in the launch library keeps its own state.
    assert "0/1 watched" in client.get(f"/playlist/{PLAYLIST_ID}").text
    assert "Stick title" in client.get("/").text


def test_create_a_local_playlist_in_another_library(tmp_path: Path):
    home, usb, libs, usb_ref = two_libraries(tmp_path)
    library = Library(libs)

    playlist_id = library.create_playlist(
        "Drive mix", [(USB_PLAYLIST_ID, "vid1")], library_id=usb_ref.id
    )

    assert playlist_id is not None
    assert (usb.playlist_meta_dir(playlist_id) / "playlist.json").is_file()
    view = library.playlist_view(playlist_id)
    assert view is not None and view.library_label == "USB stick"
    assert library.media_path(playlist_id, "vid1") == library.media_path(
        USB_PLAYLIST_ID, "vid1"
    )
    # References never cross libraries: a video from another library is copied.
    mixed = library.create_playlist(
        "Mixed", [(PLAYLIST_ID, "vid1")], library_id=usb_ref.id
    )
    assert mixed is not None
    copied = library.media_path(mixed, "vid1")
    assert copied is not None and copied.is_relative_to(usb.media_root)
    assert [p.name for p in usb.meta_root.iterdir() if p.name.startswith(".")] == []


def test_storage_page_add_label_and_forget(tmp_path: Path):
    home = make_library(tmp_path / "home", "Laptop")
    add_playlist(home, PLAYLIST_ID, "Local", [("vid1", "Video One")])
    libs = library_set(home)
    client = TestClient(create_app(home.root, libraries=libs))

    page = client.get("/storage")
    assert page.status_code == 200
    assert "Laptop" in page.text
    assert "running from" in page.text
    assert "Add a library" in page.text
    assert "Make default" not in page.text

    usb = tmp_path / "usb"
    response = client.post(
        "/storage/add", data={"path": str(usb), "label": "USB stick"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert (usb / ".ytop" / "library.json").is_file()
    assert (usb / "YTOP Library").is_dir()
    page = client.get("/storage").text
    assert "USB stick" in page
    ref = next(ref for ref in libs.libraries() if ref.root == usb)
    assert ref.source == "registered"

    response = client.post(
        f"/storage/{ref.id}/label",
        data={"label": "Blue stick", "description": "Lectures"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Blue stick" in client.get("/storage").text

    response = client.post(f"/storage/{ref.id}/remove", follow_redirects=False)
    assert response.status_code == 303
    assert all(r.root != usb for r in libs.libraries(refresh=True))
    assert (usb / ".ytop" / "library.json").is_file()

    # The launch library is not in the registry, so it cannot be forgotten.
    launch_id = libs.default().id
    assert client.post(f"/storage/{launch_id}/remove").status_code == 409
    assert client.post("/storage/add", data={"path": ""}).status_code == 200


def test_media_from_another_library_is_served(tmp_path: Path):
    home, usb, libs, _ = two_libraries(tmp_path)
    client = TestClient(create_app(home.root, libraries=libs))

    response = client.get(f"/media/{USB_PLAYLIST_ID}/vid1")
    assert response.status_code == 200
    assert response.content == b"vid1".ljust(1024, b"\0")
    page = client.get(f"/playlist/{USB_PLAYLIST_ID}")
    assert page.status_code == 200
    assert "USB stick" in page.text


def test_startup_recovers_rename_journal(tmp_path: Path):
    layout = make_library(tmp_path, "Laptop")
    add_playlist(layout, PLAYLIST_ID, "Local", [("vid1", "Video One")])
    folder = layout.media_root / "Local"
    tmp_name = ".ytoprename-0-abcd1234.webm"
    (folder / "01 - Video One.webm").rename(folder / tmp_name)
    meta_path = layout.video_meta_dir(PLAYLIST_ID, "vid1") / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["files"]["media"] = f"Local/{tmp_name}"
    write_json(meta_path, meta)
    write_json(layout.rename_journal, {"phase": 2, "ops": [{
        "src": "Local/01 - Old.webm",
        "tmp": f"Local/{tmp_name}",
        "dst": "Local/01 - Video One.webm",
    }]})
    orphan = folder / ".ytoppublish-deadbeef.webm"
    orphan.write_bytes(b"crashed")

    create_app(layout.root, libraries=library_set(layout))

    assert not layout.rename_journal.exists()
    assert (folder / "01 - Video One.webm").is_file()
    assert json.loads(meta_path.read_text())["files"]["media"] == "Local/01 - Video One.webm"
    assert not orphan.exists()
    assert (layout.cache_dir / "orphans" / orphan.name).read_bytes() == b"crashed"


def test_a_probe_uses_the_selected_library(tmp_path: Path, monkeypatch):
    home, usb, libs, usb_ref = two_libraries(tmp_path, usb_playlist=False)
    manager = JobManager(Library(libs))

    monkeypatch.setattr(jobs_module, "probe_playlist", _probe_one)
    job = manager.create_probe(
        "https://www.youtube.com/playlist?list=PLNEW", library_id=usb_ref.id
    )
    assert job.library_id == usb_ref.id
    assert job.library_label == "USB stick"
    manager.close()


def test_download_passes_the_library_root(tmp_path: Path, monkeypatch):
    home, usb, libs, usb_ref = two_libraries(tmp_path, usb_playlist=False)
    manager = JobManager(Library(libs))
    captured = {}

    def fake_download(probe, config, **kwargs):
        captured.update(kwargs)
        return FetchResult(usb.root, probe.playlist, [], total_bytes=0)

    monkeypatch.setattr(jobs_module, "download_probe", fake_download)
    monkeypatch.setattr(jobs_module, "probe_playlist", _probe_one)
    job = manager.create_probe("PLNEW0000000000", library_id=usb_ref.id)
    _wait(manager, job.id, {"ready"})
    manager.start_download(job.id)
    job = _wait(manager, job.id, {"done", "failed"})
    assert job.status == "done"
    assert Path(captured["library_root"]) == usb.root
    # jobs.json lives with the destination library.
    assert usb.jobs_file.is_file()
    manager.close()


def test_a_playlist_sync_stays_in_its_library(tmp_path: Path, monkeypatch):
    home, usb, libs, usb_ref = two_libraries(tmp_path)
    manager = JobManager(Library(libs))
    monkeypatch.setattr(
        jobs_module, "probe_playlist",
        lambda value, config, **kwargs: jobs_module.ProbeResult(
            url=value,
            playlist={"id": USB_PLAYLIST_ID, "title": "Drive lectures",
                      "entries": []},
            items=[],
        ),
    )
    job = manager.create_probe(USB_PLAYLIST_ID)
    assert job.library_id == usb_ref.id
    assert job.library_label == "USB stick"
    manager.close()


def test_unplugging_before_download_fails_with_a_clear_message(
    tmp_path: Path, monkeypatch,
):
    home, usb, libs, usb_ref = two_libraries(tmp_path, usb_playlist=False)
    manager = JobManager(Library(libs))
    monkeypatch.setattr(jobs_module, "probe_playlist", _probe_one)
    job = manager.create_probe("PLNEW0000000000", library_id=usb_ref.id)
    _wait(manager, job.id, {"ready"})

    shutil.rmtree(usb.root)
    libs.refresh()
    manager.start_download(job.id)
    job = _wait(manager, job.id, {"done", "failed", "paused_offline"})
    # An unplugged destination pauses the job; it is never auto-failed.
    assert job.status == "paused_offline"
    assert "not connected" in (job.error or "")
    manager.close()


def test_add_form_sends_the_selected_library(tmp_path: Path, monkeypatch):
    home, usb, libs, usb_ref = two_libraries(tmp_path, usb_playlist=False)
    app = create_app(home.root, libraries=libs)
    monkeypatch.setattr(
        jobs_module, "probe_playlist",
        lambda value, config, **kwargs: jobs_module.ProbeResult(
            url=value,
            playlist={"id": "PLNEW", "title": "New", "entries": []},
            items=[],
        ),
    )
    client = TestClient(app)
    page = client.get("/add").text
    assert usb_ref.id in page
    response = client.post(
        "/add",
        data={
            "url": "https://www.youtube.com/playlist?list=PLNEW0000000000",
            "location": usb_ref.id,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    job_id = response.headers["location"].rsplit("/", 1)[-1]
    payload = client.get(f"/api/jobs/{job_id}").json()
    assert payload["library_id"] == usb_ref.id
    assert payload["library_label"] == "USB stick"
    app.state.jobs.close()


def test_a_plugged_in_drive_appears_in_the_library_and_leaves_on_unplug(
    tmp_path: Path, monkeypatch,
):
    import ytop.libraries as libraries_module

    home = make_library(tmp_path / "home", "Laptop")
    add_playlist(home, PLAYLIST_ID, "Local", [("vid1", "Video One")])
    mount = tmp_path / "USB"
    init_library(mount / "YTOP", "Blue stick")
    add_playlist(LibraryLayout(mount / "YTOP"), USB_PLAYLIST_ID, "Drive lectures",
                 [("vid1", "On the drive")])
    monkeypatch.setattr(libraries_module, "_mount_candidates", lambda: [mount])

    libs = libraries_module.LibrarySet(home.root, detect=True)
    app = create_app(home.root, libraries=libs)
    client = TestClient(app)
    page = client.get("/").text
    assert "Drive lectures" in page
    assert "Blue stick" in page

    mount.rename(tmp_path / "unplugged")
    libs.refresh()
    page = client.get("/").text
    assert "Drive lectures" not in page


def _force_device(monkeypatch, same: bool) -> None:
    """Both test libraries share tmp_path's filesystem; pick the move path."""
    from ytop.web import fsops

    monkeypatch.setattr(fsops, "_same_device", lambda first, second: same)


def _add_audio(layout: LibraryLayout, playlist_id: str, video_id: str) -> Path:
    video_dir = layout.video_meta_dir(playlist_id, video_id)
    (video_dir / "audio").mkdir()
    track = video_dir / "audio" / "es.m4a"
    track.write_bytes(b"spanish".ljust(512, b"\0"))
    meta = json.loads((video_dir / "meta.json").read_text())
    meta["audio_tracks"] = [
        {"id": "es", "label": "Spanish", "file": "audio/es.m4a", "codec": "mp4a"}
    ]
    write_json(video_dir / "meta.json", meta)
    return track


def _media_ref(layout: LibraryLayout, playlist_id: str, video_id: str) -> str:
    meta = json.loads(
        (layout.video_meta_dir(playlist_id, video_id) / "meta.json").read_text()
    )
    return meta["files"]["media"]


def _move(manager: JobManager, key: str, library_id: str):
    job = manager.move_playlist(key, library_id)
    return _wait(manager, job.id, {"done", "failed", "cancelled"})


@pytest.mark.parametrize("same_device", [False, True])
def test_move_carries_progress_and_titles(
    tmp_path: Path, monkeypatch, same_device: bool,
):
    from ytop.transcripts.captions import Segment
    from ytop.transcripts.store import index_playlist_dir, search, write_transcript

    _force_device(monkeypatch, same_device)
    home, usb, libs, usb_ref = two_libraries(tmp_path, usb_playlist=False)
    library = Library(libs)
    home_ref = libs.get(libs.default().id)
    library.progress_for(home_ref).update("vid1", 42.0, duration=60.0)
    library.set_playlist_title(PLAYLIST_ID, "Renamed list")
    library.set_video_title(PLAYLIST_ID, "vid1", "Custom lecture")
    write_json(home.tags_file, {
        "schema": 1,
        "playlists": {PLAYLIST_ID: ["physics"], "PLother": ["keep"]},
        "videos": {"vid1": ["exam"]},
    })
    video_dir = home.video_meta_dir(PLAYLIST_ID, "vid1")
    write_transcript(
        video_dir, video_id="vid1", title="Video One", language="en",
        kind="manual", segments=[Segment(0, 1, "quantum entanglement")],
        raw_file=None,
    )
    index_playlist_dir(
        home, home.playlist_meta_dir(PLAYLIST_ID),
        json.loads((home.playlist_meta_dir(PLAYLIST_ID) / "playlist.json").read_text()),
    )
    assert search(home, "entanglement")
    content = library.media_path(PLAYLIST_ID, "vid1").read_bytes()

    manager = JobManager(library)
    job = _move(manager, PLAYLIST_ID, usb_ref.id)

    assert job.status == "done", job.error
    resolved = library.resolve_playlist(PLAYLIST_ID)
    assert resolved is not None and resolved[0].id == usb_ref.id
    assert not home.playlist_meta_dir(PLAYLIST_ID).exists()
    progress = json.loads(usb.progress_file.read_text())["videos"]["vid1"]
    assert progress["position"] == 42.0
    titles = json.loads(usb.titles_file.read_text())
    assert titles["playlists"][PLAYLIST_ID] == "Renamed list"
    assert titles["videos"]["vid1"] == "Custom lecture"
    tags = json.loads(usb.tags_file.read_text())
    assert tags["playlists"] == {PLAYLIST_ID: ["physics"]}
    assert tags["videos"] == {"vid1": ["exam"]}
    media = library.media_path(PLAYLIST_ID, "vid1")
    assert media is not None and media.is_relative_to(usb.media_root)
    assert media.read_bytes() == content
    assert "Custom lecture" in media.name
    assert [p for p in home.media_root.rglob("*.webm")] == []
    # Both search indexes follow the playlist.
    assert search(home, "entanglement") == []
    hits = search(usb, "entanglement")
    assert [hit["playlist_id"] for hit in hits] == [PLAYLIST_ID]
    # No staging is left behind.
    assert not [p for p in usb.meta_root.iterdir() if p.name.startswith(".")]
    view = library.playlist_view(PLAYLIST_ID)
    assert view is not None and view.title == "Renamed list"


@pytest.mark.parametrize("same_device", [False, True])
def test_move_copies_referenced_media(
    tmp_path: Path, monkeypatch, same_device: bool,
):
    _force_device(monkeypatch, same_device)
    home, usb, libs, usb_ref = two_libraries(tmp_path, usb_playlist=False)
    library = Library(libs)
    owner_track = _add_audio(home, PLAYLIST_ID, "vid1")
    mix_id = library.create_playlist("Mix", [(PLAYLIST_ID, "vid1")])
    assert mix_id is not None
    owner_ref = _media_ref(home, PLAYLIST_ID, "vid1")
    content = (home.media_root / owner_ref).read_bytes()

    job = _move(JobManager(library), mix_id, usb_ref.id)

    assert job.status == "done", job.error
    # The referencing playlist gets its own copy (refs never cross libraries).
    mix_doc = json.loads(
        (usb.playlist_meta_dir(mix_id) / "playlist.json").read_text()
    )
    moved_ref = _media_ref(usb, mix_id, "vid1")
    assert moved_ref.startswith(f"{mix_doc['folder']}/")
    assert (usb.media_root / moved_ref).read_bytes() == content
    assert "ytoppublish" not in moved_ref
    track = library.audio_path(mix_id, "vid1", "es")
    assert track is not None and track.is_relative_to(usb.root)
    assert track.read_bytes() == owner_track.read_bytes()
    # The owner keeps its media and audio untouched.
    assert _media_ref(home, PLAYLIST_ID, "vid1") == owner_ref
    assert (home.media_root / owner_ref).read_bytes() == content
    assert owner_track.is_file()
    assert not home.playlist_meta_dir(mix_id).exists()


@pytest.mark.parametrize("same_device", [False, True])
def test_move_owner_transfers_shared_media_within_source(
    tmp_path: Path, monkeypatch, same_device: bool,
):
    _force_device(monkeypatch, same_device)
    home, usb, libs, usb_ref = two_libraries(tmp_path, usb_playlist=False)
    library = Library(libs)
    _add_audio(home, PLAYLIST_ID, "vid1")
    mix_id = library.create_playlist("Mix", [(PLAYLIST_ID, "vid1")])
    assert mix_id is not None
    content = library.media_path(PLAYLIST_ID, "vid1").read_bytes()

    job = _move(JobManager(library), PLAYLIST_ID, usb_ref.id)

    assert job.status == "done", job.error
    # The source's referencing playlist now owns the file (Task 8 rules).
    mix_doc = json.loads(
        (home.playlist_meta_dir(mix_id) / "playlist.json").read_text()
    )
    mix_ref = _media_ref(home, mix_id, "vid1")
    assert mix_ref.startswith(f"{mix_doc['folder']}/")
    assert (home.media_root / mix_ref).read_bytes() == content
    mix_track = library.audio_path(mix_id, "vid1", "es")
    assert mix_track is not None and mix_track.is_relative_to(home.root)
    # The moved playlist has its own copy and audio on the destination.
    moved = library.media_path(PLAYLIST_ID, "vid1")
    assert moved is not None and moved.is_relative_to(usb.media_root)
    assert moved.read_bytes() == content
    track = library.audio_path(PLAYLIST_ID, "vid1", "es")
    assert track is not None and track.is_relative_to(usb.root)


def test_move_refuses_fat32_oversize(tmp_path: Path, monkeypatch):
    from ytop import fsinfo

    home, usb, libs, usb_ref = two_libraries(tmp_path, usb_playlist=False)
    monkeypatch.setattr(
        fsinfo, "fs_info",
        lambda path: fsinfo.FsInfo("vfat", fsinfo.FAT32_MAX_FILE)
        if Path(path).is_relative_to(usb.root) else fsinfo.FsInfo("ext4", None),
    )
    library = Library(libs)
    media = library.media_path(PLAYLIST_ID, "vid1")
    with open(media, "r+b") as handle:
        handle.truncate(fsinfo.FAT32_MAX_FILE + 1)  # sparse: 4 294 967 296
    client = TestClient(create_app(home.root, libraries=libs))

    response = client.post(
        f"/playlist/{PLAYLIST_ID}/move", data={"location": usb_ref.id},
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert media.name in response.text
    assert "FAT32" in response.text
    assert library.resolve_playlist(PLAYLIST_ID)[0].id != usb_ref.id
    assert not usb.playlist_meta_dir(PLAYLIST_ID).exists()
    assert media.stat().st_size == fsinfo.FAT32_MAX_FILE + 1


def test_move_allows_a_big_file_under_the_fat32_limit(tmp_path: Path, monkeypatch):
    from ytop import fsinfo
    from ytop.web import fsops

    _force_device(monkeypatch, False)
    home, usb, libs, usb_ref = two_libraries(tmp_path, usb_playlist=False)
    monkeypatch.setattr(
        fsinfo, "fs_info",
        lambda path: fsinfo.FsInfo("vfat", fsinfo.FAT32_MAX_FILE)
        if Path(path).is_relative_to(usb.root) else fsinfo.FsInfo("ext4", None),
    )
    monkeypatch.setattr(fsops, "free_bytes", lambda path: 0)  # unknown
    library = Library(libs)
    media = library.media_path(PLAYLIST_ID, "vid1")
    with open(media, "r+b") as handle:
        handle.truncate(4_000_000_000)  # sparse; over fits_on's estimate margin

    plan = fsops.plan_move(
        library, library.resolve_playlist(PLAYLIST_ID), usb_ref.id, job_id="big"
    )

    assert max(item.size for item in plan.files) == 4_000_000_000


def test_move_refuses_insufficient_space(tmp_path: Path, monkeypatch):
    from ytop.web import fsops

    _force_device(monkeypatch, False)
    home, usb, libs, usb_ref = two_libraries(tmp_path, usb_playlist=False)
    monkeypatch.setattr(fsops, "free_bytes", lambda path: 10)
    manager = JobManager(Library(libs))
    with pytest.raises(ValueError, match="free"):
        manager.move_playlist(PLAYLIST_ID, usb_ref.id)
    assert (home.playlist_meta_dir(PLAYLIST_ID) / "playlist.json").is_file()


def test_move_interrupted_leaves_source_intact(tmp_path: Path, monkeypatch):
    from ytop.web import fsops

    _force_device(monkeypatch, False)
    home, usb, libs, usb_ref = two_libraries(tmp_path, usb_playlist=False)
    add_playlist(home, PLAYLIST_ID, "Local", [("vid1", "Video One"), ("vid2", "Two")])
    library = Library(libs)
    content = library.media_path(PLAYLIST_ID, "vid1").read_bytes()
    real_copy = fsops.copy_file
    copied: list[Path] = []

    def flaky(src, dst, **kwargs):
        if len(copied) >= 3:
            raise OSError("stick yanked")
        copied.append(src)
        return real_copy(src, dst, **kwargs)

    monkeypatch.setattr(fsops, "copy_file", flaky)
    client = TestClient(create_app(home.root, libraries=libs))
    app_manager = client.app.state.jobs

    job = _move(app_manager, PLAYLIST_ID, usb_ref.id)

    assert job.status == "failed"
    assert "stick yanked" in (job.error or "")
    resolved = library.resolve_playlist(PLAYLIST_ID)
    assert resolved is not None and resolved[0].id != usb_ref.id
    response = client.get(f"/media/{PLAYLIST_ID}/vid1")
    assert response.status_code == 200 and response.content == content
    assert client.get(f"/watch/{PLAYLIST_ID}/vid1").status_code == 200
    assert not usb.playlist_meta_dir(PLAYLIST_ID).exists()
    assert not [p for p in usb.meta_root.iterdir() if p.name.startswith(".")]
    assert list(usb.media_root.iterdir()) == []
    assert "Could not move this playlist" in client.get(f"/move/{job.id}").text


def _crash_move(manager: JobManager, key: str, library_id: str) -> str:
    """Queue a move and run it in this thread until a simulated crash."""
    job = manager.move_playlist(key, library_id)
    with pytest.raises(SystemExit):
        manager._run_move_work(job.id)
    return job.id


def _crash_once(real, should_crash):
    state = {"crashed": False}

    def wrapper(*args, **kwargs):
        if not state["crashed"] and should_crash(*args):
            state["crashed"] = True
            raise SystemExit("power cut")
        return real(*args, **kwargs)

    return wrapper


def test_crash_between_same_device_renames_puts_the_folder_back(
    tmp_path: Path, monkeypatch,
):
    import os

    _force_device(monkeypatch, True)
    home, usb, libs, usb_ref = two_libraries(tmp_path, usb_playlist=False)
    library = Library(libs)
    content = library.media_path(PLAYLIST_ID, "vid1").read_bytes()
    source_meta = home.playlist_meta_dir(PLAYLIST_ID)
    monkeypatch.setattr(os, "rename", _crash_once(
        os.rename, lambda src, dst: Path(src) == source_meta,
    ))
    job_id = _crash_move(JobManager(library, start_workers=False), PLAYLIST_ID, usb_ref.id)
    # The crash left the media folder on the destination, its meta behind.
    assert (usb.media_root / "Local").is_dir()
    assert library.media_path(PLAYLIST_ID, "vid1") is None

    job = JobManager(Library(libs), start_workers=False).get(job_id)

    assert job.status == "failed" and job.source_cleanup_complete
    assert "left where it was" in job.error
    media = Library(libs).media_path(PLAYLIST_ID, "vid1")
    assert media is not None and media.is_relative_to(home.media_root)
    assert media.read_bytes() == content
    assert not (usb.media_root / "Local").exists()
    assert not [p for p in usb.meta_root.iterdir() if p.name.startswith(".")]


def test_crash_before_staged_copies_are_placed_is_finished_on_restart(
    tmp_path: Path, monkeypatch,
):
    from ytop.web import fsops

    _force_device(monkeypatch, True)
    home, usb, libs, usb_ref = two_libraries(tmp_path, usb_playlist=False)
    library = Library(libs)
    _add_audio(home, PLAYLIST_ID, "vid1")
    mix_id = library.create_playlist("Mix", [(PLAYLIST_ID, "vid1")])
    content = library.media_path(PLAYLIST_ID, "vid1").read_bytes()
    monkeypatch.setattr(fsops, "_place_staged", _crash_once(
        fsops._place_staged, lambda spec: True,
    ))
    job_id = _crash_move(JobManager(library, start_workers=False), mix_id, usb_ref.id)
    assert usb.playlist_meta_dir(mix_id).is_dir()  # renamed, refs not yet fixed

    job = JobManager(Library(libs), start_workers=False).get(job_id)

    assert job.status == "done", job.error
    fresh = Library(libs)
    media = fresh.media_path(mix_id, "vid1")
    assert media is not None and media.is_relative_to(usb.media_root)
    assert media.read_bytes() == content
    assert "ytoppublish" not in media.name
    track = fresh.audio_path(mix_id, "vid1", "es")
    assert track is not None and track.is_relative_to(usb.root)
    assert not [p for p in usb.meta_root.iterdir() if p.name.startswith(".")]
    # The owner is untouched in the source.
    assert fresh.media_path(PLAYLIST_ID, "vid1").is_relative_to(home.media_root)


def test_crash_between_placing_and_rewriting_refs_keeps_media_on_restart(
    tmp_path: Path, monkeypatch,
):
    """A crash in the window between placing a same-device move's staged
    copies and pointing the metas at them must not let the startup orphan
    sweep (which runs before move reconciliation) strand them."""
    from ytop.web import fsops

    _force_device(monkeypatch, True)
    home, usb, libs, usb_ref = two_libraries(tmp_path, usb_playlist=False)
    library = Library(libs)
    mix_id = library.create_playlist("Mix", [(PLAYLIST_ID, "vid1")])
    content = library.media_path(PLAYLIST_ID, "vid1").read_bytes()
    target_meta = usb.playlist_meta_dir(mix_id)
    monkeypatch.setattr(fsops, "_rewrite_refs", _crash_once(
        fsops._rewrite_refs, lambda meta_dir, spec: Path(meta_dir) == target_meta,
    ))
    job_id = _crash_move(JobManager(library, start_workers=False), mix_id, usb_ref.id)

    # Restart: the app's startup recovery (orphan sweep) and then the jobs'
    # move reconciliation.
    client = TestClient(create_app(home.root, libraries=libs))
    job = client.app.state.jobs.get(job_id)

    assert job.status == "done", job.error
    fresh = Library(libs)
    media = fresh.media_path(mix_id, "vid1")
    assert media is not None and media.is_relative_to(usb.media_root)
    assert media.read_bytes() == content
    orphans = usb.cache_dir / "orphans"
    assert not orphans.exists() or not list(orphans.iterdir())
    assert not [p for p in usb.meta_root.iterdir() if p.name.startswith(".")]


def test_crash_between_copy_publish_renames_removes_the_orphan(
    tmp_path: Path, monkeypatch,
):
    import os

    _force_device(monkeypatch, False)
    home, usb, libs, usb_ref = two_libraries(tmp_path, usb_playlist=False)
    library = Library(libs)
    content = library.media_path(PLAYLIST_ID, "vid1").read_bytes()
    monkeypatch.setattr(os, "rename", _crash_once(
        os.rename,
        lambda src, dst: Path(src).name == "meta"
        and Path(src).parent.name.startswith(".ytopmove-"),
    ))
    job_id = _crash_move(JobManager(library, start_workers=False), PLAYLIST_ID, usb_ref.id)
    assert (usb.media_root / "Local").is_dir()  # the orphaned copy

    job = JobManager(Library(libs), start_workers=False).get(job_id)

    assert job.status == "failed" and "left where it was" in job.error
    assert not (usb.media_root / "Local").exists()
    assert not [p for p in usb.meta_root.iterdir() if p.name.startswith(".")]
    media = Library(libs).media_path(PLAYLIST_ID, "vid1")
    assert media is not None and media.read_bytes() == content


def test_crash_after_another_playlist_claims_the_folder_keeps_it(
    tmp_path: Path, monkeypatch,
):
    """A destination folder another playlist claimed while the move was
    publishing is never removed by the move's reconciliation."""
    from ytop.web import fsops

    _force_device(monkeypatch, False)
    home, usb, libs, usb_ref = two_libraries(tmp_path, usb_playlist=False)
    library = Library(libs)
    content = library.media_path(PLAYLIST_ID, "vid1").read_bytes()
    claimed = usb.media_root / "Local"
    real_journal = fsops._write_journal

    def claim_then_journal(spec):
        add_playlist(usb, "PLCLAIM", "Local", [("vidX", "Claimed")])
        assert (claimed / "01 - Claimed.webm").is_file()
        real_journal(spec)

    monkeypatch.setattr(fsops, "_write_journal", claim_then_journal)
    monkeypatch.setattr(fsops, "_publish_staging", _crash_once(
        fsops._publish_staging, lambda plan: True,
    ))
    job_id = _crash_move(JobManager(library, start_workers=False), PLAYLIST_ID, usb_ref.id)

    job = JobManager(Library(libs), start_workers=False).get(job_id)

    assert job.status == "failed" and "left where it was" in job.error
    assert (claimed / "01 - Claimed.webm").is_file()
    fresh = Library(libs)
    assert fresh.media_path("PLCLAIM", "vidX") is not None
    media = fresh.media_path(PLAYLIST_ID, "vid1")
    assert media is not None and media.read_bytes() == content


def test_crash_between_copy_publish_renames_keeps_an_unmarked_folder(
    tmp_path: Path, monkeypatch,
):
    import os

    _force_device(monkeypatch, False)
    home, usb, libs, usb_ref = two_libraries(tmp_path, usb_playlist=False)
    library = Library(libs)
    monkeypatch.setattr(os, "rename", _crash_once(
        os.rename,
        lambda src, dst: Path(src).name == "meta"
        and Path(src).parent.name.startswith(".ytopmove-"),
    ))
    job_id = _crash_move(JobManager(library, start_workers=False), PLAYLIST_ID, usb_ref.id)
    folder = usb.media_root / "Local"
    assert folder.is_dir()
    # The folder no longer carries this move's marker (say the user replaced
    # it with their own folder of the same name).
    for marker in folder.glob(".ytopmove-*"):
        marker.unlink()
    (folder / "mine.txt").write_text("keep me")

    job = JobManager(Library(libs), start_workers=False).get(job_id)

    assert job.status == "failed" and "left alone" in job.error
    assert (folder / "mine.txt").read_text() == "keep me"
    assert not [p for p in usb.meta_root.iterdir() if p.name.startswith(".")]
    assert Library(libs).media_path(PLAYLIST_ID, "vid1") is not None


@pytest.mark.parametrize("same_device", [True, False])
def test_a_rolled_back_media_rename_is_fsynced(
    tmp_path: Path, monkeypatch, same_device: bool,
):
    """When the metadata rename fails, the media folder is renamed back and
    both directories that rollback touched are fsynced (#10)."""
    import os

    from ytop.web import fsops

    _force_device(monkeypatch, same_device)
    home, usb, libs, usb_ref = two_libraries(tmp_path, usb_playlist=False)
    library = Library(libs)
    source_meta = home.playlist_meta_dir(PLAYLIST_ID)
    events: list[tuple] = []
    real_rename = os.rename

    def rename(src, dst):
        src, dst = Path(src), Path(dst)
        is_meta = src == source_meta or (
            src.name == "meta" and src.parent.name.startswith(".ytopmove-")
        )
        if is_meta:
            raise OSError("stick yanked")
        real_rename(src, dst)
        events.append(("rename", src, dst))

    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(
        fsops, "fsync_dir", lambda path: events.append(("fsync", Path(path)))
    )
    manager = JobManager(library, start_workers=False)
    job = manager.move_playlist(PLAYLIST_ID, usb_ref.id)
    manager._run_move_work(job.id)

    assert manager.get(job.id).status == "failed"
    renames = [i for i, event in enumerate(events) if event[0] == "rename"]
    assert len(renames) == 2  # out, then back
    _, back_src, back_dst = events[renames[-1]]
    synced = {event[1] for event in events[renames[-1] + 1:] if event[0] == "fsync"}
    assert {back_src.parent, back_dst.parent} <= synced
    assert library.media_path(PLAYLIST_ID, "vid1") is not None


def test_move_route_page_and_conflicts(tmp_path: Path, monkeypatch):
    home, usb, libs, usb_ref = two_libraries(tmp_path, usb_playlist=False)
    add_playlist(usb, "PLCLASH", "Clash", [("vid9", "Nine")])
    add_playlist(home, "PLCLASH", "Clash home", [("vid8", "Eight")])
    app = create_app(home.root, libraries=libs)
    client = TestClient(app)

    page = client.get(f"/playlist/{PLAYLIST_ID}").text
    assert "Move to another location" in page
    assert usb_ref.id in page
    response = client.post(
        f"/playlist/{PLAYLIST_ID}/move", data={"location": usb_ref.id},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/move/")
    job_id = response.headers["location"].rsplit("/", 1)[1]
    _wait(app.state.jobs, job_id, {"done"})
    assert "Moved" in client.get(f"/move/{job_id}").text

    key = Library(libs).key_for(libs.default().id, "PLCLASH")
    clash = client.post(
        f"/playlist/{key}/move", data={"location": usb_ref.id},
        follow_redirects=False,
    )
    assert clash.status_code == 409
    assert "already has a playlist" in clash.text
    assert client.post(
        "/playlist/PLmissing/move", data={"location": usb_ref.id}
    ).status_code == 404


def test_move_refused_while_downloading(tmp_path: Path):
    home, usb, libs, usb_ref = two_libraries(tmp_path, usb_playlist=False)
    manager = JobManager(Library(libs), start_workers=False)
    manager.create_probe(PLAYLIST_ID)
    with pytest.raises(ValueError, match="active job"):
        manager.move_playlist(PLAYLIST_ID, usb_ref.id)


def test_copy_tree_reports_progress_and_matches_contents(tmp_path: Path):
    source = tmp_path / "source"
    (source / "videos" / "vid1").mkdir(parents=True)
    (source / "video.bin").write_bytes(b"a" * 1000)
    (source / "videos" / "vid1" / "meta.json").write_text('{"id": "vid1"}')
    destination = tmp_path / "destination"
    copied = {"bytes": 0, "files": 0}

    jobs_module._copy_tree(
        source,
        destination,
        on_chunk=lambda size: copied.__setitem__(
            "bytes", copied["bytes"] + size
        ),
        on_file=lambda count: copied.__setitem__(
            "files", copied["files"] + count
        ),
        cancel=lambda: False,
    )
    assert copied["bytes"] == 1014
    assert copied["files"] == 2
    assert (destination / "video.bin").read_bytes() == b"a" * 1000
    assert (destination / "videos" / "vid1" / "meta.json").is_file()


def test_review_fat_warning_follows_the_destination_library(
    tmp_path: Path, monkeypatch,
):
    from ytop import fsinfo
    from ytop.yt.formats import Selection

    home, usb, libs, usb_ref = two_libraries(tmp_path, usb_playlist=False)
    monkeypatch.setattr(
        fsinfo, "fs_info",
        lambda path: fsinfo.FsInfo("vfat", fsinfo.FAT32_MAX_FILE)
        if Path(path).is_relative_to(usb.root) else fsinfo.FsInfo("ext4", None),
    )

    def big_probe(value, config, **kwargs):
        item = jobs_module.ProbedItem(
            index=1, id="big", url="https://www.youtube.com/watch?v=big",
            title="Big", duration=10,
        )
        item.selection = Selection(
            video={"format_id": "v", "vcodec": "av01", "height": 2160,
                   "filesize": 3_900_000_000},
            audio={"format_id": "a", "acodec": "opus", "filesize": 100},
        )
        return jobs_module.ProbeResult(
            url=value, playlist={"id": "PLBIG", "title": "Big", "entries": []},
            items=[item],
        )

    monkeypatch.setattr(jobs_module, "probe_playlist", big_probe)
    manager = JobManager(Library(libs))
    to_usb = manager.create_probe("PLBIG0000000000", library_id=usb_ref.id)
    to_usb = _wait(manager, to_usb.id, {"ready"})
    assert to_usb.fat32_warning is True and to_usb.fat32_count == 1
    to_home = manager.create_probe("PLBIG0000000000")
    to_home = _wait(manager, to_home.id, {"ready"})
    assert to_home.library_label == "Laptop"
    assert to_home.fat32_warning is False
    manager.close()


def _same_ids_in_two_libraries(tmp_path: Path):
    from ytop.layout import UNGROUPED_ID

    home, usb, libs, usb_ref = two_libraries(tmp_path, usb_playlist=False)
    add_playlist(home, UNGROUPED_ID, "Un-grouped", [("h0", "Home single")])
    add_playlist(usb, UNGROUPED_ID, "Un-grouped", [("s0", "Stick single")])
    add_playlist(home, "PLSAME", "Home copy", [("h1", "Home lecture")])
    add_playlist(usb, "PLSAME", "Stick copy", [("s1", "Stick lecture")])
    return home, usb, libs, usb_ref


def test_same_playlist_id_in_two_libraries_gets_distinct_keys(tmp_path: Path):
    home, usb, libs, usb_ref = _same_ids_in_two_libraries(tmp_path)
    client = TestClient(create_app(home.root, libraries=libs))
    views = {
        (v.library_label, v.id): v for v in client.app.state.library.playlists()
    }
    home_key = views[("Laptop", "PLSAME")].key
    usb_key = views[("USB stick", "PLSAME")].key
    assert home_key != usb_key and usb_key == f"PLSAME@{usb_ref.id[:8]}"
    # A playlist id held by only one library keeps its plain key.
    assert views[("Laptop", PLAYLIST_ID)].key == PLAYLIST_ID

    page = client.get("/").text
    assert "Home copy" in page and "Stick copy" in page
    # Once each in Browse all (Start something new repeats them above).
    assert page.split('id="browse"', 1)[1].count("/playlist/un-grouped@") == 2

    stick = client.get(f"/playlist/{usb_key}")
    assert stick.status_code == 200 and "Stick lecture" in stick.text
    assert f"/watch/{usb_key}/s1" in stick.text
    assert client.get(f"/watch/{usb_key}/s1").status_code == 200
    assert client.get(f"/media/{usb_key}/s1").content == b"s1".ljust(1024, b"\0")
    assert client.get(f"/media/{home_key}/h1").content == b"h1".ljust(1024, b"\0")
    ungrouped_usb = views[("USB stick", "un-grouped")].key
    assert "Stick single" in client.get(f"/playlist/{ungrouped_usb}").text

    # Edits through a key land in that library only.
    client.post(f"/playlist/{usb_key}/title", data={"title": "Renamed stick"},
                follow_redirects=False)
    assert json.loads(usb.titles_file.read_text())["playlists"] == {
        "PLSAME": "Renamed stick"
    }
    assert not home.titles_file.exists()
    assert client.post("/api/progress", json={
        "playlist_id": usb_key, "video_id": "s1", "position": 7,
    }).status_code == 200
    assert "s1" in json.loads(usb.progress_file.read_text())["videos"]
    assert client.post(f"/playlist/{usb_key}/videos/s1/remove",
                       follow_redirects=False).status_code == 303
    assert json.loads(
        (home.playlist_meta_dir("PLSAME") / "playlist.json").read_text()
    )["entries"][0]["id"] == "h1"


def test_single_video_lands_in_the_chosen_librarys_ungrouped(
    tmp_path: Path, monkeypatch,
):
    from ytop.layout import UNGROUPED_ID

    home, usb, libs, usb_ref = two_libraries(tmp_path, usb_playlist=False)
    add_playlist(home, UNGROUPED_ID, "Un-grouped", [("h0", "Home single")])

    def fake_probe_video(video_id, config, *, playlist=None, **kwargs):
        item = jobs_module.ProbedItem(
            index=1, id=video_id, url="u", title="Solo", duration=5,
        )
        item.error = "not used"
        return jobs_module.ProbeResult(url="u", playlist=playlist, items=[item])

    monkeypatch.setattr(jobs_module, "probe_video", fake_probe_video)
    client = TestClient(create_app(home.root, libraries=libs))
    response = client.post(
        "/add",
        data={"url": "https://youtu.be/abc12345678", "location": usb_ref.id},
        follow_redirects=False,
    )
    assert response.status_code == 303
    job_id = response.headers["location"].rsplit("/", 1)[-1]
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["library_id"] == usb_ref.id
    assert job["target_playlist_id"] == UNGROUPED_ID
    assert (usb.playlist_meta_dir(UNGROUPED_ID) / "playlist.json").is_file()

    picker = client.get("/add/video").text
    assert f'value="library:{usb_ref.id}"' in picker
    response = client.post(
        "/add/video",
        data={"url": "https://youtu.be/xyz98765432",
              "playlist": f"library:{usb_ref.id}"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    job_id = response.headers["location"].rsplit("/", 1)[-1]
    assert client.get(f"/api/jobs/{job_id}").json()["library_id"] == usb_ref.id
    client.app.state.jobs.close()


def test_config_overrides_reach_jobs(tmp_path: Path, monkeypatch):
    from ytop import cli
    from ytop.web import app as app_module

    home = make_library(tmp_path, "Laptop")
    seen = {}

    def probe(value, config, **kwargs):
        seen["audio_language"] = config.audio_language
        return jobs_module.ProbeResult(
            url=value, playlist={"id": "PLX", "title": "X", "entries": []},
            items=[],
        )

    monkeypatch.setattr(jobs_module, "probe_playlist", probe)
    app = create_app(
        home.root, libraries=library_set(home),
        config_overrides={"audio_language": "de"},
    )
    assert app.state.jobs.config.audio_language == "de"
    job = app.state.jobs.create_probe("PLX0000000000")
    _wait(app.state.jobs, job.id, {"ready", "failed"})
    assert seen["audio_language"] == "de"
    app.state.jobs.close()

    captured = {}
    monkeypatch.setattr(
        app_module, "serve", lambda *args, **kwargs: captured.update(kwargs)
    )
    assert cli.main(["serve", "--library", str(home.root), "--audio-lang", "de",
                     "--no-open"]) == 0
    assert captured["config_overrides"] == {"audio_language": "de"}
    assert cli.main(["serve", "--library", str(home.root), "--no-open"]) == 0
    assert captured["config_overrides"] == {}


def test_job_pages_link_with_playlist_keys(tmp_path: Path, monkeypatch):
    home, usb, libs, usb_ref = _same_ids_in_two_libraries(tmp_path)

    def fake_probe_video(video_id, config, *, playlist=None, **kwargs):
        item = jobs_module.ProbedItem(
            index=1, id=video_id, url="u", title="Solo", duration=5,
        )
        item.selection = None
        item.info = {"id": video_id, "title": "Solo"}
        return jobs_module.ProbeResult(url="u", playlist=playlist, items=[item])

    monkeypatch.setattr(jobs_module, "probe_video", fake_probe_video)
    client = TestClient(create_app(home.root, libraries=libs))
    usb_key = f"PLSAME@{usb_ref.id[:8]}"
    response = client.post(
        "/add/video",
        data={"url": "https://youtu.be/abc12345678", "playlist": usb_key},
        follow_redirects=False,
    )
    assert response.status_code == 303
    job_id = response.headers["location"].rsplit("/", 1)[-1]
    manager = client.app.state.jobs
    _wait(manager, job_id, {"ready", "failed"})
    manager._update(job_id, status="ready", error=None)
    page = client.get(f"/add/{job_id}").text
    assert f'href="/playlist/{usb_key}"' in page
    assert 'href="/playlist/PLSAME"' not in page

    manager._update(job_id, status="done")
    page = client.get(f"/add/{job_id}").text
    assert f'href="/playlist/{usb_key}"' in page
    manager.close()


def test_moved_folder_never_takes_the_un_grouped_name(tmp_path: Path):
    from ytop.layout import UNGROUPED_FOLDER, UNGROUPED_ID
    from ytop.web import fsops

    _, _, _, usb_ref = two_libraries(tmp_path, usb_playlist=False)

    assert fsops._dest_folder(usb_ref, "PLX", UNGROUPED_FOLDER) == (
        f"{UNGROUPED_FOLDER} [PLX]"
    )
    assert fsops._dest_folder(usb_ref, UNGROUPED_ID, UNGROUPED_FOLDER) == (
        UNGROUPED_FOLDER
    )


def test_bare_id_held_by_two_libraries_is_refused_on_write_routes(
    tmp_path: Path,
):
    """A page loaded while only one library held the id posts the bare id;
    once a second library holds it too, writes must not pick one."""
    home, usb, libs, usb_ref = two_libraries(tmp_path, usb_playlist=False)
    client = TestClient(create_app(home.root, libraries=libs))
    # The second library now holds the same playlist id.
    add_playlist(usb, PLAYLIST_ID, "Stick copy", [("vid1", "Stick video")])

    def snapshot() -> dict:
        state = {}
        for layout in (home, usb):
            meta = layout.playlist_meta_dir(PLAYLIST_ID)
            state[str(layout.root)] = sorted(
                (str(p.relative_to(layout.root)), p.read_bytes())
                for p in layout.root.rglob("*") if p.is_file()
                and "jobs.json" not in p.name and p.suffix != ".sqlite3"
                and not p.name.startswith("index")
            ) if meta.is_dir() else None
        return state

    before = snapshot()
    pid = PLAYLIST_ID
    posts = [
        (f"/playlist/{pid}/title", {"data": {"title": "Changed"}}),
        (f"/playlist/{pid}/videos/vid1/title", {"data": {"title": "Changed"}}),
        (f"/playlist/{pid}/delete", {}),
        (f"/playlist/{pid}/videos/vid1/remove", {"data": {}}),
        (f"/playlist/{pid}/reorder", {"data": {"order": "vid1"}}),
        (f"/playlist/{pid}/options", {"data": {"preserve_audio": "1"}}),
        (f"/playlist/{pid}/move", {"data": {"location": usb_ref.id}}),
        ("/add/video", {"data": {"url": "dQw4w9WgXcQ", "playlist": pid}}),
        ("/api/progress", {"json": {
            "playlist_id": pid, "video_id": "vid1", "position": 30,
        }}),
    ]
    for url, kwargs in posts:
        response = client.post(url, follow_redirects=False, **kwargs)
        assert response.status_code == 409, (url, response.status_code)
        assert "more than one library" in response.text, url

    assert snapshot() == before


def test_storage_page_shows_components(tmp_path: Path):
    from ytop.autoupdate import AutoUpdate
    from ytop.components import Current, write_current
    from ytop.stick.boot import COMPLETE

    home = make_library(tmp_path / "home", "Laptop")
    ytop = tmp_path / "stick" / ".ytop"
    for name, version in (("yt-dlp", "2026.10.26"), ("yt-dlp-ejs", "0.3.0")):
        folder = ytop / "components" / name / version
        folder.mkdir(parents=True)
        (folder / COMPLETE).write_text("")
    write_current(ytop, Current(
        current={"yt-dlp": "2026.10.26", "yt-dlp-ejs": "0.3.0"},
        pinned_bad={"yt-dlp": ["2026.11.01"]},
        last_check="2026-11-14T09:30:00+00:00",
        last_result="updated yt-dlp 2026.10.26",
    ))
    auto = AutoUpdate(ytop, Config, loaded=[], base_path=[])
    client = TestClient(create_app(home.root, libraries=library_set(home), autoupdate=auto))

    page = client.get("/storage").text
    assert "YouTube parts" in page
    assert "2026.10.26" in page and "0.3.0" in page
    assert "14 Nov 2026" in page
    assert "updated yt-dlp 2026.10.26" in page
    assert "2026.11.01" in page  # pinned bad
    assert 'action="/storage/components/update"' in page
    assert 'name="check_components"' in page and "checked" in page
    assert "Check for updates to YouTube parts" in page
    assert "automatic update after a download" in " ".join(page.split())

    # A checkout without a store says so, and offers no Update now.
    bare = TestClient(create_app(home.root, libraries=library_set(home),
                                 autoupdate=AutoUpdate(None, Config)))
    page = bare.get("/storage").text
    assert "YouTube parts" in page
    assert 'action="/storage/components/update"' not in page
    client.app.state.jobs.close()
    bare.app.state.jobs.close()


def test_create_playlist_and_add_video_on_read_only_library_give_409(
    tmp_path: Path,
):
    home, usb, libs, usb_ref = two_libraries(tmp_path)
    marker = json.loads(usb.marker.read_text())
    marker["schema"] = 99
    usb.marker.write_text(json.dumps(marker))
    libs.refresh()
    client = TestClient(create_app(home.root, libraries=libs))
    token = client.app.state.csrf_token
    try:
        response = client.post(
            "/playlist/new",
            data={"title": "Nope", "location": usb_ref.id, "csrf_token": token},
            follow_redirects=False,
        )
        assert response.status_code == 409
        response = client.post(
            "/add/video",
            data={"url": "jNQXAC9IVRw", "playlist": f"library:{usb_ref.id}",
                  "csrf_token": token},
            follow_redirects=False,
        )
        # The job layer refuses a read-only destination with its own
        # (form-level) message before anything is written.
        assert "made by a newer YTOP" in response.text
        assert client.app.state.jobs.list() == []
    finally:
        client.app.state.jobs.close()


def test_interrupted_move_reconciles_after_a_remount(tmp_path: Path, monkeypatch):
    """Job paths are absolute; a stick back under another mount point must
    still be repaired, not reported as missing."""
    import os

    _force_device(monkeypatch, True)
    home, usb, libs, usb_ref = two_libraries(tmp_path, usb_playlist=False)
    library = Library(libs)
    content = library.media_path(PLAYLIST_ID, "vid1").read_bytes()
    source_meta = home.playlist_meta_dir(PLAYLIST_ID)
    monkeypatch.setattr(os, "rename", _crash_once(
        os.rename, lambda src, dst: Path(src) == source_meta,
    ))
    job_id = _crash_move(JobManager(library, start_workers=False), PLAYLIST_ID, usb_ref.id)
    monkeypatch.undo()
    _force_device(monkeypatch, True)

    remounted = tmp_path / "usb-again"
    usb.root.rename(remounted)
    libs.add_folder(remounted, "USB stick")
    job = JobManager(Library(libs), start_workers=False).get(job_id)

    assert job.status == "failed" and job.source_cleanup_complete, job.error
    assert "left where it was" in job.error
    media = Library(libs).media_path(PLAYLIST_ID, "vid1")
    assert media is not None and media.is_relative_to(home.media_root)
    assert media.read_bytes() == content
    again = LibraryLayout(remounted)
    assert not (again.media_root / "Local").exists()
    assert not [p for p in again.meta_root.iterdir() if p.name.startswith(".")]


def test_crash_while_finishing_a_copy_move_is_finished_on_restart(
    tmp_path: Path, monkeypatch,
):
    from ytop.web import fsops

    _force_device(monkeypatch, False)
    home, usb, libs, usb_ref = two_libraries(tmp_path, usb_playlist=False)
    library = Library(libs)
    monkeypatch.setattr(fsops, "_finish_destination", _crash_once(
        fsops._finish_destination, lambda spec: True,
    ))
    job_id = _crash_move(JobManager(library, start_workers=False), PLAYLIST_ID, usb_ref.id)
    # The journal survived the crash.
    assert [p for p in usb.meta_root.iterdir() if p.name.endswith(".json")]

    JobManager(Library(libs), start_workers=False).get(job_id)

    assert not [p for p in usb.meta_root.iterdir() if p.name.startswith(".")]
    media = Library(libs).resolve_in(usb_ref.id, PLAYLIST_ID)
    assert media is not None
    assert (usb.media_root / "Local" / "01 - Video One.webm").is_file()


def test_unparsable_move_journal_is_removed_with_its_staging(tmp_path: Path):
    from ytop.web import fsops

    home, usb, libs, usb_ref = two_libraries(tmp_path, usb_playlist=False)
    staging = usb.meta_root / ".ytopmove-deadbeef"
    staging.mkdir(parents=True)
    fsops.journal_path(staging).write_text("{garbage", encoding="utf-8")

    result = fsops.reconcile_move(
        source=home.playlist_meta_dir(PLAYLIST_ID),
        target=usb.playlist_meta_dir(PLAYLIST_ID),
        staging=staging,
    )

    assert result.phase == "staging"
    assert not staging.exists()
    assert not fsops.journal_path(staging).exists()
