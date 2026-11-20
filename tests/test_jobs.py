from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from factories import lib_root, library_set
from ytop.config import Config
from ytop.layout import LibraryLayout
from ytop.web import jobs as jobs_module
from ytop.web.app import create_app
from ytop.web.jobs import JobManager
from ytop.yt.download import FetchResult, ItemResult
from ytop.yt.formats import Selection
from ytop.yt.probe import ProbedItem, ProbeResult


def _selection(size: int = 1000) -> Selection:
    return Selection(
        video={
            "format_id": "v", "vcodec": "av01", "height": 240,
            "width": 320, "fps": 15, "filesize": size,
        },
        audio={"format_id": "a", "acodec": "opus", "filesize": 100},
    )


def fake_probe(value, config, limit=None, on_item=None, on_total=None, **kwargs):
    playlist = {
        "id": "PLTESTJOBS00000000000000000001",
        "title": "Job Test Playlist",
        "channel": "Test Channel",
        "description": "Two videos for the job test.",
        "entries": [
            {"id": "vid1", "title": "First"},
            {"id": "vid2", "title": "Second"},
        ],
    }
    if on_total is not None:
        on_total(2)
    items = []
    for index, (vid, title) in enumerate(
        (("vid1", "First"), ("vid2", "Second")), start=1
    ):
        item = ProbedItem(
            index=index, id=vid,
            url=f"https://www.youtube.com/watch?v={vid}",
            title=title, duration=60.0,
        )
        item.selection = _selection(1000 * index)
        item.info = {
            "id": vid, "title": title,
            "formats": [
                {"format_id": "v", "vcodec": "av01", "acodec": "none",
                 "height": 240, "width": 320, "fps": 15, "ext": "webm"},
                {"format_id": "a", "vcodec": "none", "acodec": "opus",
                 "abr": 50, "language": "en", "ext": "webm"},
            ],
        }
        items.append(item)
        if on_item is not None:
            on_item(item)
    return ProbeResult(url=value, playlist=playlist, items=items)


def wait_for(manager: JobManager, job_id: str, statuses: set[str],
             timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = manager.get(job_id)
        if job is not None and job.status in statuses:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not reach {statuses}: "
                         f"{manager.get(job_id)}")


def test_probe_job_reports_playlist_review(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(jobs_module, "probe_playlist", fake_probe)
    manager = JobManager(lib_root(tmp_path))
    job = manager.create_probe("PLTESTJOBS00000000000000000001")
    job = wait_for(manager, job.id, {"ready"})

    assert job.title == "Job Test Playlist"
    assert job.video_count == 2
    assert job.estimate_bytes == 3200
    assert job.items[0]["height"] == 240
    assert job.items[0]["size"] == 1100
    assert not job.in_library
    assert LibraryLayout(lib_root(tmp_path)).jobs_file.is_file()

    again = JobManager(lib_root(tmp_path)).get(job.id)
    assert again is not None
    assert again.status == "ready"
    assert again.title == "Job Test Playlist"


def test_download_job_tracks_items_and_bytes(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(jobs_module, "probe_playlist", fake_probe)

    def fake_download(probe, config, *, jobs=None, quiet=False,
                      on_item_start=None, on_item_done=None, on_progress=None,
                      **kwargs):
        results = []
        for item in probe.items:
            if on_item_start:
                on_item_start(item)
            if on_progress:
                on_progress(item, 50, 100)
                on_progress(item, 100, 100)
            result = ItemResult(
                item.index, item.id, item.title, "downloaded",
                size=1000 * item.index,
            )
            results.append(result)
            if on_item_done:
                on_item_done(result)
        return FetchResult(
            tmp_path, probe.playlist, results,
            total_bytes=sum(r.size for r in results),
        )

    monkeypatch.setattr(jobs_module, "download_probe", fake_download)
    manager = JobManager(lib_root(tmp_path))
    job = manager.create_probe("PLTESTJOBS00000000000000000001")
    wait_for(manager, job.id, {"ready"})
    manager.start_download(job.id)
    job = wait_for(manager, job.id, {"done"})

    assert job.downloaded == 2
    assert job.bytes_downloaded == 3000
    assert job.in_library is True
    assert [row["status"] for row in job.items] == ["downloaded", "downloaded"]


def test_restart_pauses_an_active_download(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(jobs_module, "probe_playlist", fake_probe)
    manager = JobManager(lib_root(tmp_path))
    job = manager.create_probe("PLTESTJOBS00000000000000000001")
    wait_for(manager, job.id, {"ready"})
    # Replace the live record with a stale running one and reload.
    manager._update(job.id, status="running")
    manager._save()
    reopened = JobManager(lib_root(tmp_path))
    loaded = reopened.get(job.id)
    assert loaded is not None
    assert loaded.status == "paused_offline"
    assert "Resume" in (loaded.error or "")


def test_add_and_download_pages(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(jobs_module, "probe_playlist", fake_probe)
    monkeypatch.setattr(
        jobs_module, "download_probe",
        lambda probe, config, **kwargs: FetchResult(
            tmp_path, probe.playlist,
            [ItemResult(1, "vid1", "First", "downloaded", size=1000)],
            total_bytes=1000,
        ),
    )
    client = TestClient(create_app(lib_root(tmp_path), libraries=library_set(lib_root(tmp_path))))

    empty = client.get("/downloads")
    assert empty.status_code == 200
    assert "No downloads yet" in empty.text

    response = client.post(
        "/add", data={"url": "PLTESTJOBS00000000000000000001"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/add/")

    job_id = location.rsplit("/", 1)[1]
    deadline = time.time() + 5
    page = None
    while time.time() < deadline:
        page = client.get(location)
        if "Ready to add" in page.text:
            break
        time.sleep(0.02)
    assert page is not None
    assert "Ready to add" in page.text
    assert "Job Test Playlist" in page.text
    assert "Download playlist" in page.text
    assert "2:00" in page.text

    listed = client.get("/downloads")
    assert "Ready to download" in listed.text

    api = client.get(f"/api/jobs/{job_id}").json()
    assert api["status"] == "ready"
    assert len(api["items"]) == 2

    response = client.post(f"/jobs/{job_id}/download", follow_redirects=False)
    assert response.status_code == 303
    deadline = time.time() + 5
    while time.time() < deadline:
        api = client.get(f"/api/jobs/{job_id}").json()
        if api["status"] == "done":
            break
        time.sleep(0.02)
    assert api["status"] == "done"
    page = client.get(f"/add/{job_id}")
    assert "Complete" in client.get("/downloads").text or "Complete" in page.text


def test_cancel_probe_marks_the_job_cancelled(tmp_path: Path, monkeypatch):
    from ytop.yt.probe import JobCancelled

    def slow_probe(value, config, **kwargs):
        cancel = kwargs.get("cancel")
        on_total = kwargs.get("on_total")
        if on_total:
            on_total(10)
        for _ in range(200):
            if cancel and cancel():
                raise JobCancelled("cancelled by the user")
            time.sleep(0.01)
        raise AssertionError("probe should have been cancelled")

    monkeypatch.setattr(jobs_module, "probe_playlist", slow_probe)
    manager = JobManager(lib_root(tmp_path))
    job = manager.create_probe("PLTESTJOBS00000000000000000001")
    time.sleep(0.05)
    manager.cancel(job.id)
    job = wait_for(manager, job.id, {"cancelled"})
    assert job.cancel_requested is True
    assert "cancelled" in (job.error or "")


def test_remove_only_works_for_finished_jobs(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(jobs_module, "probe_playlist", fake_probe)
    manager = JobManager(lib_root(tmp_path))
    job = manager.create_probe("PLTESTJOBS00000000000000000001")
    wait_for(manager, job.id, {"ready"})
    manager.remove(job.id)
    assert manager.get(job.id) is None
    assert manager.list() == []

    active = manager.create_probe("PLTESTJOBS00000000000000000001")
    with pytest.raises(ValueError):
        manager.remove(active.id)
    manager.cancel(active.id)


def test_cancel_download_marks_pending_items_cancelled(tmp_path: Path,
                                                       monkeypatch):
    from ytop.yt.probe import JobCancelled

    monkeypatch.setattr(jobs_module, "probe_playlist", fake_probe)

    def blocking_download(probe, config, **kwargs):
        cancel = kwargs.get("cancel")
        for _ in range(300):
            if cancel and cancel():
                raise JobCancelled("cancelled by the user")
            time.sleep(0.01)
        raise AssertionError("download should have been cancelled")

    monkeypatch.setattr(jobs_module, "download_probe", blocking_download)
    manager = JobManager(lib_root(tmp_path))
    job = manager.create_probe("PLTESTJOBS00000000000000000001")
    wait_for(manager, job.id, {"ready"})
    manager.start_download(job.id, policy="native")
    assert manager.get(job.id).target_mb_per_hour == 0
    time.sleep(0.05)
    manager.cancel(job.id)
    job = wait_for(manager, job.id, {"cancelled"})
    assert any(row["status"] == "cancelled" for row in job.items)


def test_cancel_a_queued_probe_does_not_wait_for_the_running_one(
    tmp_path: Path, monkeypatch,
):
    started = threading.Event()
    release = threading.Event()

    def slow_probe(value, config, **kwargs):
        started.set()
        release.wait(3)
        return fake_probe(value, config, **kwargs)

    monkeypatch.setattr(jobs_module, "probe_playlist", slow_probe)
    manager = JobManager(lib_root(tmp_path))
    first = manager.create_probe("PLTESTJOBS00000000000000000001")
    assert started.wait(2)
    second = manager.create_probe("PLTESTJOBS00000000000000000002")

    manager.cancel(second.id)
    assert manager.get(second.id).status == "cancelled"

    release.set()
    wait_for(manager, first.id, {"ready"}, timeout=5)


def test_a_cancelled_job_can_be_resumed(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(jobs_module, "probe_playlist", fake_probe)

    def fake_download(probe, config, *, jobs=None, quiet=False,
                      on_item_done=None, **kwargs):
        results = []
        for item in probe.items:
            result = ItemResult(
                item.index, item.id, item.title, "skipped", size=1000,
            )
            results.append(result)
            if on_item_done:
                on_item_done(result)
        return FetchResult(
            tmp_path, probe.playlist, results,
            total_bytes=sum(r.size for r in results),
        )

    monkeypatch.setattr(jobs_module, "download_probe", fake_download)
    manager = JobManager(lib_root(tmp_path))
    job = manager.create_probe("PLTESTJOBS00000000000000000001")
    wait_for(manager, job.id, {"ready"})
    manager._update(job.id, status="cancelled", error="cancelled by the user")
    manager.start_download(job.id)
    job = wait_for(manager, job.id, {"done"})
    assert job.status == "done"
    assert job.skipped == 2


def test_a_new_probe_inherits_the_playlist_audio_policy(
    tmp_path: Path, monkeypatch,
):
    import json

    playlist_id = "PLTESTJOBS00000000000000000001"
    playlist_dir = LibraryLayout(lib_root(tmp_path)).playlist_meta_dir(playlist_id)
    playlist_dir.mkdir(parents=True)
    (playlist_dir / "playlist.json").write_text(json.dumps({
        "schema": 1, "id": playlist_id, "title": "Existing",
        "entries": [], "options": {"preserve_audio": True},
    }), encoding="utf-8")

    monkeypatch.setattr(jobs_module, "probe_playlist", fake_probe)
    manager = JobManager(lib_root(tmp_path))
    job = manager.create_probe(playlist_id)
    job = wait_for(manager, job.id, {"ready"})
    assert job.preserve_audio is True


def test_review_payload_flags_fat32_oversize(tmp_path: Path, monkeypatch):
    from ytop import fsinfo

    monkeypatch.setattr(
        fsinfo, "fs_info",
        lambda path: fsinfo.FsInfo("vfat", fsinfo.FAT32_MAX_FILE),
    )
    probe = ProbeResult(
        url="https://www.youtube.com/playlist?list=PL1",
        playlist={"id": "PL1", "title": "List"},
        items=[
            ProbedItem(index=1, id="big", url="u1", selection=_selection(3_900_000_000)),
            ProbedItem(index=2, id="small", url="u2", selection=_selection()),
        ],
    )
    review = jobs_module._compact_probe(probe, Config(), False, tmp_path)
    assert [row["fat32_warning"] for row in review["items"]] == [True, False]
    assert review["fat32_warning"] is True
    assert review["fat32_count"] == 1

    review = jobs_module._compact_probe(probe, Config(), False, None)
    assert review["fat32_count"] == 0


# -- per-library jobs ----------------------------------------------------------
def _two_libraries(tmp_path: Path):
    from factories import make_library
    from ytop.layout import init_library

    home = make_library(tmp_path / "home", "Laptop")
    usb = LibraryLayout(tmp_path / "usb")
    init_library(usb.root, "USB stick")
    libs = library_set(home)
    usb_ref = libs.add_folder(usb.root, "USB stick")
    return home, usb, libs, usb_ref


def _job_ids(path: Path) -> set[str]:
    import json

    if not path.exists():
        return set()
    return {row["id"] for row in json.loads(path.read_text())["jobs"]}


def _manager(libs) -> JobManager:
    from ytop.web.library import Library

    return JobManager(Library(libs))


def test_job_saved_to_destination_library(tmp_path: Path, monkeypatch):
    home, usb, libs, usb_ref = _two_libraries(tmp_path)
    monkeypatch.setattr(jobs_module, "probe_playlist", fake_probe)
    manager = _manager(libs)
    job = manager.create_probe(
        "PLTESTJOBS00000000000000000001", library_id=usb_ref.id
    )
    job = wait_for(manager, job.id, {"ready"})
    assert job.library_id == usb_ref.id
    assert job.library_label == "USB stick"
    assert job.id in _job_ids(usb.jobs_file)
    assert job.id not in _job_ids(home.jobs_file)
    manager.close()

    # Every library's file is read back on the next start.
    again = _manager(library_set(home))
    loaded = again.get(job.id)
    assert loaded is not None and loaded.library_id == usb_ref.id
    assert loaded.status == "ready"
    again.close()


def test_job_config_from_destination(tmp_path: Path, monkeypatch):
    home, usb, libs, usb_ref = _two_libraries(tmp_path)
    usb.config_file.write_text(
        "[download]\ntarget_mb_per_hour = 50\n", encoding="utf-8"
    )
    seen = {}

    def probe(value, config, **kwargs):
        seen["kwarg"] = kwargs.get("target_mb_per_hour")
        seen["config"] = config.target_mb_per_hour
        return fake_probe(value, config, **kwargs)

    monkeypatch.setattr(jobs_module, "probe_playlist", probe)
    manager = _manager(libs)
    job = manager.create_probe(
        "PLTESTJOBS00000000000000000001", library_id=usb_ref.id
    )
    job = wait_for(manager, job.id, {"ready"})
    assert seen == {"kwarg": 50.0, "config": 50.0}
    assert job.target_mb_per_hour == 50.0

    home_job = manager.create_probe("PLTESTJOBS00000000000000000002")
    wait_for(manager, home_job.id, {"ready"})
    assert seen == {"kwarg": 100.0, "config": 100.0}
    manager.close()


def test_job_config_carries_the_session_login(tmp_path: Path, monkeypatch):
    from ytop.config import SESSION_AUTH

    home, usb, libs, usb_ref = _two_libraries(tmp_path)
    seen = {}

    def probe(value, config, **kwargs):
        seen["browser"] = config.cookies_from_browser
        return fake_probe(value, config, **kwargs)

    monkeypatch.setattr(jobs_module, "probe_playlist", probe)
    SESSION_AUTH.enable("firefox")
    try:
        manager = _manager(libs)
        job = manager.create_probe(
            "PLTESTJOBS00000000000000000001", library_id=usb_ref.id
        )
        wait_for(manager, job.id, {"ready"})
    finally:
        SESSION_AUTH.disable()
    assert seen["browser"] == "firefox"
    manager.close()


def test_jobs_paused_when_library_offline(tmp_path: Path, monkeypatch):
    import shutil

    home, usb, libs, usb_ref = _two_libraries(tmp_path)
    monkeypatch.setattr(jobs_module, "probe_playlist", fake_probe)
    monkeypatch.setattr(
        jobs_module, "download_probe",
        lambda probe, config, **kwargs: FetchResult(
            usb.root, probe.playlist, [], total_bytes=0,
        ),
    )
    manager = _manager(libs)
    ready = manager.create_probe(
        "PLTESTJOBS00000000000000000001", library_id=usb_ref.id
    )
    wait_for(manager, ready.id, {"ready"})
    queued = manager.create_probe(
        "PLTESTJOBS00000000000000000002", library_id=usb_ref.id
    )
    wait_for(manager, queued.id, {"ready"})

    # Unplug the stick: its jobs are paused, never failed, and stay listed.
    parked = tmp_path / "parked"
    shutil.move(usb.root, parked)
    libs.refresh()
    listed = {job.id: job for job in manager.list()}
    assert listed[ready.id].status == "paused_offline"
    assert listed[ready.id].status_label == "Paused"
    assert "USB stick" in (listed[ready.id].error or "")
    assert not listed[ready.id].active
    # Resume while it is still unplugged is refused with a clear message.
    with pytest.raises(ValueError, match="not connected"):
        manager.start_download(ready.id)

    # A download started before the page noticed the unplug pauses too.
    manager._update(queued.id, status="ready", error=None)
    manager.start_download(queued.id)
    job = wait_for(manager, queued.id, {"paused_offline", "failed"})
    assert job.status == "paused_offline"

    # Plug it back in: Resume restarts through the normal download path.
    shutil.move(parked, usb.root)
    libs.refresh()
    assert manager.get(ready.id).status == "paused_offline"
    # A paused review goes back to its review; the user confirms again.
    assert manager.start_download(ready.id).status == "ready"
    manager.start_download(ready.id)
    job = wait_for(manager, ready.id, {"done", "failed", "paused_offline"})
    assert job.status == "done"
    assert ready.id in _job_ids(usb.jobs_file)
    manager.close()


def test_old_location_keys_loaded(tmp_path: Path):
    from ytop.yt.metadata import write_json

    home, usb, libs, usb_ref = _two_libraries(tmp_path)
    home_id = libs.default().id
    write_json(home.jobs_file, {"schema": 1, "jobs": [
        {"id": "oldusbjob001", "status": "done", "kind": "playlist",
         "title": "On the stick", "playlist_id": "PLOLD",
         "location_id": usb_ref.id, "location_label": "USB stick",
         "location_explicit": True,
         "source_location_id": home_id, "source_location_label": "Laptop",
         "created_at": "2026-01-01T00:00:00+00:00"},
        {"id": "oldhomejob01", "status": "ready", "kind": "playlist",
         "title": "At home", "location_id": home_id,
         "location_label": "Laptop",
         "created_at": "2026-01-02T00:00:00+00:00"},
        {"id": "ancientjob01", "status": "failed", "kind": "playlist",
         "title": "No location at all",
         "created_at": "2026-01-03T00:00:00+00:00"},
    ]})
    manager = _manager(libs)
    stick = manager.get("oldusbjob001")
    assert stick is not None
    assert stick.library_id == usb_ref.id
    assert stick.library_label == "USB stick"
    assert stick.library_explicit is True
    assert stick.source_library_id == home_id
    assert stick.source_library_label == "Laptop"
    assert manager.get("oldhomejob01").library_id == home_id
    assert manager.get("ancientjob01").library_id == home_id

    # Mapped onto per-library files; no old keys are written back.
    assert _job_ids(usb.jobs_file) == {"oldusbjob001"}
    assert _job_ids(home.jobs_file) == {"oldhomejob01", "ancientjob01"}
    assert "location_id" not in home.jobs_file.read_text()
    manager.close()


def test_old_job_for_an_offline_library_is_kept(tmp_path: Path):
    from ytop.yt.metadata import write_json

    home = LibraryLayout(lib_root(tmp_path))
    write_json(home.jobs_file, {"schema": 1, "jobs": [
        {"id": "gonejob00001", "status": "done", "kind": "playlist",
         "location_id": "0123456789abcdef", "location_label": "Lost stick",
         "created_at": "2026-01-01T00:00:00+00:00"},
    ]})
    manager = JobManager(home.root)
    assert manager.get("gonejob00001") is not None
    manager.close()
    # Its own library is unplugged, so it stays where it was found.
    assert _job_ids(home.jobs_file) == {"gonejob00001"}


def test_video_already_downloading_in_another_job_is_skipped(
    tmp_path: Path, monkeypatch,
):
    home, usb, libs, usb_ref = _two_libraries(tmp_path)
    monkeypatch.setattr(jobs_module, "probe_playlist", fake_probe)
    passed = {}

    def fake_download(probe, config, *, on_item_done=None, **kwargs):
        passed["ids"] = [item.id for item in probe.items]
        results = []
        for item in probe.items:
            result = ItemResult(item.index, item.id, item.title, "downloaded",
                                size=10)
            results.append(result)
            if on_item_done:
                on_item_done(result)
        return FetchResult(usb.root, probe.playlist, results, total_bytes=10)

    monkeypatch.setattr(jobs_module, "download_probe", fake_download)
    manager = _manager(libs)
    other = manager.create_probe("PLTESTJOBS00000000000000000001")
    wait_for(manager, other.id, {"ready"})
    # Another job (in the other library) is downloading vid1 right now.
    with manager._lock:
        live = manager._jobs[other.id]
        live.status = "running"
        live.items[0]["status"] = "downloading"
        live.items[1]["status"] = "downloaded"

    job = manager.create_probe(
        "PLTESTJOBS00000000000000000001", library_id=usb_ref.id
    )
    wait_for(manager, job.id, {"ready"})
    manager.start_download(job.id)
    job = wait_for(manager, job.id, {"done", "failed"})
    assert passed["ids"] == ["vid2"]
    rows = {row["id"]: row for row in job.items}
    assert rows["vid1"]["status"] == "skipped"
    assert rows["vid1"]["error"] == "already downloading in another job"
    assert rows["vid2"]["status"] == "downloaded"
    assert job.skipped == 1 and job.downloaded == 1
    with manager._lock:
        manager._jobs[other.id].status = "ready"
    manager.close()


def test_unplug_mid_download_pauses_instead_of_finishing(
    tmp_path: Path, monkeypatch,
):
    import shutil

    home, usb, libs, usb_ref = _two_libraries(tmp_path)
    monkeypatch.setattr(jobs_module, "probe_playlist", fake_probe)
    parked = tmp_path / "parked"

    def yanked(probe, config, *, on_item_done=None, **kwargs):
        shutil.move(usb.root, parked)
        libs.refresh()
        results = [
            ItemResult(item.index, item.id, item.title, "failed",
                       error="OSError: gone")
            for item in probe.items
        ]
        for result in results:
            if on_item_done:
                on_item_done(result)
        return FetchResult(usb.root, probe.playlist, results, total_bytes=0)

    monkeypatch.setattr(jobs_module, "download_probe", yanked)
    manager = _manager(libs)
    job = manager.create_probe(
        "PLTESTJOBS00000000000000000001", library_id=usb_ref.id
    )
    wait_for(manager, job.id, {"ready"})
    manager.start_download(job.id)
    job = wait_for(manager, job.id, {"paused_offline", "done", "failed"})
    assert job.status == "paused_offline"
    manager.close()


def test_start_workers_false_queues_without_running(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(jobs_module, "probe_playlist", fake_probe)
    manager = JobManager(lib_root(tmp_path), start_workers=False)
    job = manager.create_probe("PLTESTJOBS00000000000000000001")
    time.sleep(0.05)
    assert manager.get(job.id).status == "probing"
    manager.close()


def test_probe_job_uses_default_library_cache_root(tmp_path: Path, monkeypatch):
    seen: dict = {}

    def spy(value, config, **kwargs):
        seen["cache_root"] = kwargs.get("cache_root")
        return fake_probe(value, config, **kwargs)

    monkeypatch.setattr(jobs_module, "probe_playlist", spy)
    manager = JobManager(lib_root(tmp_path))
    job = manager.create_probe("PLTESTJOBS00000000000000000001")
    wait_for(manager, job.id, {"ready"})

    root = seen["cache_root"]
    assert root is not None
    assert Path(root) == lib_root(tmp_path)
    from ytop.yt.probe import base_options

    opts = base_options(Config(), cache_root=root)
    assert Path(opts["cachedir"]) == (
        LibraryLayout(lib_root(tmp_path)).cache_dir / "yt-dlp"
    )

    job.library_id = ""
    assert manager._job_cache_root(job) == lib_root(tmp_path)


# -- quality tiers -------------------------------------------------------------
def _tier_video(fid: str, height: int, size: int) -> dict:
    return {"format_id": fid, "vcodec": "av01", "acodec": "none",
            "height": height, "width": height * 16 // 9, "fps": 30,
            "ext": "webm", "protocol": "https", "filesize": size}


def _tier_audio() -> dict:
    return {"format_id": "a", "vcodec": "none", "acodec": "opus", "abr": 50,
            "language": "en", "ext": "webm", "filesize": 100}


def _tier_probe(*heights_per_item: tuple[int, ...]) -> ProbeResult:
    items = []
    for index, heights in enumerate(heights_per_item, start=1):
        info = {"id": f"v{index}", "duration": 60, "formats": [
            _tier_video(f"{h}", h, h * 10) for h in heights
        ] + [_tier_audio()]}
        item = ProbedItem(index=index, id=f"v{index}", url=f"u{index}",
                          duration=60.0)
        item.info = info
        item.selection = _selection()
        items.append(item)
    return ProbeResult(url="https://www.youtube.com/playlist?list=PL1",
                       playlist={"id": "PL1", "title": "List"}, items=items)


def test_review_offers_only_tiers_smaller_than_native():
    probe = _tier_probe((360, 720, 1080), (360, 720))
    review = jobs_module._compact_probe(probe, Config(), False, None)
    assert set(review["estimate_tiers"]) == {"480p", "720p"}
    # 480p falls back to 360p on both items.
    assert review["estimate_tiers"]["480p"] == 2 * (3600 + 100)
    assert review["estimate_tiers"]["720p"] == 2 * (7200 + 100)
    assert review["estimate_tiers"]["720p"] < review["estimate_native_bytes"]
    assert review["free_bytes"] is None


def test_review_has_no_tiers_when_playlist_tops_out_at_720p():
    probe = _tier_probe((360, 720), (360, 720))
    review = jobs_module._compact_probe(probe, Config(), False, None)
    assert set(review["estimate_tiers"]) == {"480p"}


def test_review_reads_free_space_of_the_destination(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "free_bytes", lambda path: 12345)
    probe = _tier_probe((360,))
    review = jobs_module._compact_probe(probe, Config(), False, tmp_path / "new")
    assert review["free_bytes"] == 12345


def test_quality_options_order_labels_and_fits():
    job = jobs_module.Job(
        id="j", estimate_bytes=10, estimate_native_bytes=100,
        estimate_tiers={"720p": 50, "480p": 30}, free_bytes=40,
    )
    opts = job.quality_options
    assert [o["policy"] for o in opts] == ["efficient", "480p", "720p", "native"]
    assert [o["label"] for o in opts] == [
        "Efficient", "480p max", "720p max", "Native",
    ]
    assert [o["bytes"] for o in opts] == [10, 30, 50, 100]
    assert [o["fits"] for o in opts] == [True, True, False, False]
    # A sync's totals count only new videos, so it shows fits too.
    job.in_library = True
    assert [o["fits"] for o in job.quality_options] == [True, True, False, False]
    job.in_library = False
    job.free_bytes = None
    assert all(o["fits"] is None for o in job.quality_options)


def test_policy_label_for_a_tier():
    job = jobs_module.Job(id="j", policy="720p")
    assert job.policy_label == "720p max"


def test_policy_settings():
    cfg = Config()
    assert jobs_module.policy_settings("720p", cfg) == (0.0, 720)
    assert jobs_module.policy_settings("2160p", cfg) == (0.0, 2160)
    assert jobs_module.policy_settings("native", cfg) == (0.0, 0)
    assert jobs_module.policy_settings("efficient", cfg) == (
        cfg.target_mb_per_hour, cfg.max_height,
    )
    for bad in ("999p", "720", "", "cap", "360p"):
        with pytest.raises(ValueError):
            jobs_module.policy_settings(bad, cfg)


def _capturing_download(tmp_path: Path, captured: dict):
    def fake_download(probe, config, *, on_item_done=None, **kwargs):
        captured.update(kwargs)
        results = []
        for item in probe.items:
            result = ItemResult(item.index, item.id, item.title, "skipped",
                                size=1000)
            results.append(result)
            if on_item_done:
                on_item_done(result)
        return FetchResult(tmp_path, probe.playlist, results,
                           total_bytes=sum(r.size for r in results))
    return fake_download


def test_start_download_with_a_tier(tmp_path: Path, monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(jobs_module, "probe_playlist", fake_probe)
    monkeypatch.setattr(jobs_module, "download_probe",
                        _capturing_download(tmp_path, captured))
    manager = JobManager(lib_root(tmp_path))
    job = manager.create_probe("PLTESTJOBS00000000000000000001")
    wait_for(manager, job.id, {"ready"})
    manager.start_download(job.id, policy="720p")
    job = wait_for(manager, job.id, {"done"})
    assert (job.policy, job.max_height, job.target_mb_per_hour) == (
        "720p", 720, 0,
    )
    assert captured["max_height"] == 720
    assert captured["target_mb_per_hour"] == 0
    assert captured["quality"] == "720p"
    assert job.policy_label == "720p max"


def test_start_download_rejects_an_unknown_tier(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(jobs_module, "probe_playlist", fake_probe)
    manager = JobManager(lib_root(tmp_path))
    job = manager.create_probe("PLTESTJOBS00000000000000000001")
    wait_for(manager, job.id, {"ready"})
    with pytest.raises(ValueError):
        manager.start_download(job.id, policy="999p")
    assert manager.get(job.id).status == "ready"


def test_a_video_job_does_not_change_the_playlist_quality(
    tmp_path: Path, monkeypatch,
):
    from test_web import fake_probe_video

    captured: dict = {}
    monkeypatch.setattr(jobs_module, "probe_video", fake_probe_video)
    monkeypatch.setattr(jobs_module, "download_probe",
                        _capturing_download(tmp_path, captured))
    manager = JobManager(lib_root(tmp_path))
    job = manager.create_video_probe("abc12345678")
    wait_for(manager, job.id, {"ready"})
    manager.start_download(job.id, policy="720p")
    wait_for(manager, job.id, {"done"})
    assert captured["max_height"] == 720
    assert captured["quality"] is None


def _stored_quality(tmp_path: Path, quality) -> str:
    import json

    playlist_id = "PLTESTJOBS00000000000000000001"
    playlist_dir = LibraryLayout(lib_root(tmp_path)).playlist_meta_dir(playlist_id)
    playlist_dir.mkdir(parents=True)
    (playlist_dir / "playlist.json").write_text(json.dumps({
        "schema": 1, "id": playlist_id, "title": "Existing",
        "entries": [], "options": {"quality": quality},
    }), encoding="utf-8")
    return playlist_id


def test_review_preselects_the_stored_quality(tmp_path: Path, monkeypatch):
    playlist_id = _stored_quality(tmp_path, "native")
    monkeypatch.setattr(jobs_module, "probe_playlist", fake_probe)
    manager = JobManager(lib_root(tmp_path))
    job = wait_for(manager, manager.create_probe(playlist_id).id, {"ready"})
    assert job.suggested_policy == "native"


@pytest.mark.parametrize("stored", ["bogus", 7, None, "360p"])
def test_review_ignores_a_stored_quality_that_is_not_offered(
    tmp_path: Path, monkeypatch, stored,
):
    # fake_probe offers only 240p, so no tier is smaller than native.
    playlist_id = _stored_quality(tmp_path, stored)
    monkeypatch.setattr(jobs_module, "probe_playlist", fake_probe)
    manager = JobManager(lib_root(tmp_path))
    job = wait_for(manager, manager.create_probe(playlist_id).id, {"ready"})
    assert job.suggested_policy == "efficient"


def test_a_stored_tier_merged_into_native_preselects_native(
    tmp_path: Path, monkeypatch,
):
    # fake_probe tops out at 240p: a 720p cap now picks the native renditions,
    # so the remembered 720p choice must not fall back to Efficient.
    playlist_id = _stored_quality(tmp_path, "720p")
    monkeypatch.setattr(jobs_module, "probe_playlist", fake_probe)
    manager = JobManager(lib_root(tmp_path))
    job = wait_for(manager, manager.create_probe(playlist_id).id, {"ready"})
    assert job.suggested_policy == "native"


@pytest.mark.parametrize("stored", [["720p"], {"q": 1}])
def test_an_unhashable_stored_quality_does_not_break_the_review(
    tmp_path: Path, monkeypatch, stored,
):
    playlist_id = _stored_quality(tmp_path, stored)
    monkeypatch.setattr(jobs_module, "probe_playlist", fake_probe)
    manager = JobManager(lib_root(tmp_path))
    job = wait_for(manager, manager.create_probe(playlist_id).id, {"ready"})
    assert job.suggested_policy == "efficient"


def test_a_single_video_review_shows_fits():
    job = jobs_module.Job(
        id="j", kind="video", in_library=True, estimate_bytes=10,
        estimate_native_bytes=100, free_bytes=50,
    )
    assert [o["fits"] for o in job.quality_options] == [True, False]


def test_probe_job_keeps_its_timings(tmp_path: Path, monkeypatch):
    from ytop.timing import Phase

    def timed_probe(*args, **kwargs):
        result = fake_probe(*args, **kwargs)
        result.timings = [Phase("listing", 1.5, count=2, unit="videos"),
                          Phase("videos", 12.0, count=2, unit="videos")]
        return result

    monkeypatch.setattr(jobs_module, "probe_playlist", timed_probe)
    manager = JobManager(lib_root(tmp_path))
    job = manager.create_probe("PLTESTJOBS00000000000000000001")
    job = wait_for(manager, job.id, {"ready"})

    assert [row["name"] for row in job.timings] == ["listing", "videos", "reviewing"]
    lines = job.timing_lines
    assert lines[0].startswith("listing") and lines[-1].startswith("total")

    again = JobManager(lib_root(tmp_path)).get(job.id)
    assert again is not None
    assert [row["name"] for row in again.timings] == ["listing", "videos", "reviewing"]


def test_timing_lines_skip_bad_rows():
    from ytop.web.jobs import Job

    job = Job(id="x", timings=[{"name": "a", "seconds": 2}, {"bogus": 1}, "nope"])
    assert job.timing_lines[0].startswith("a")
    assert len(job.timing_lines) == 2  # a + total


def test_sync_review_leaves_out_existing_videos_and_is_read_once(
    tmp_path: Path, monkeypatch,
):
    probes: list[dict] = []

    def probe_with_existing(value, config, **kwargs):
        probes.append(kwargs)
        result = fake_probe(value, config, **kwargs)
        first = result.items[0]
        first.existing, first.info, first.selection = True, None, None
        return result

    downloads: list[dict] = []

    def fake_download(probe, config, **kwargs):
        downloads.append(kwargs)
        return FetchResult(tmp_path, probe.playlist, [], total_bytes=0)

    monkeypatch.setattr(jobs_module, "probe_playlist", probe_with_existing)
    monkeypatch.setattr(jobs_module, "download_probe", fake_download)
    manager = JobManager(lib_root(tmp_path))
    job = wait_for(
        manager, manager.create_probe("PLTESTJOBS00000000000000000001").id,
        {"ready"},
    )
    assert callable(probes[0]["have"])
    assert job.existing_count == 1
    assert job.items[0]["existing"] is True
    assert job.items[0]["size"] == 0
    assert job.estimate_bytes == job.items[1]["size"]  # only the new video

    monkeypatch.setattr(manager, "_job_playlist", lambda job, playlist_id: (None, None, {}))
    manager._update(job.id, in_library=True)
    manager.start_download(job.id, refresh_existing=True)
    job = wait_for(manager, job.id, {"done", "failed"})
    assert len(probes) == 1  # the review's analysis was used, not redone
    assert downloads[0]["refresh_existing"] is True
    assert job.refresh_existing is True


def test_a_sync_review_shows_fits_too():
    job = jobs_module.Job(
        id="j", kind="playlist", in_library=True, estimate_bytes=10,
        estimate_native_bytes=100, free_bytes=50, existing_count=3,
    )
    assert [o["fits"] for o in job.quality_options] == [True, False]


def test_review_page_offers_refreshing_existing_videos(tmp_path: Path, monkeypatch):
    def probe_with_existing(value, config, **kwargs):
        result = fake_probe(value, config, **kwargs)
        first = result.items[0]
        first.existing, first.info, first.selection = True, None, None
        return result

    seen: list[dict] = []

    def fake_download(probe, config, **kwargs):
        seen.append(kwargs)
        return FetchResult(tmp_path, probe.playlist, [], total_bytes=0)

    monkeypatch.setattr(jobs_module, "probe_playlist", probe_with_existing)
    monkeypatch.setattr(jobs_module, "download_probe", fake_download)
    client = TestClient(create_app(lib_root(tmp_path), libraries=library_set(lib_root(tmp_path))))
    response = client.post(
        "/add", data={"url": "PLTESTJOBS00000000000000000001"},
        follow_redirects=False,
    )
    location = response.headers["location"]
    deadline = time.time() + 5
    page = client.get(location)
    while "Ready to add" not in page.text and time.time() < deadline:
        time.sleep(0.02)
        page = client.get(location)
    assert 'name="refresh_existing"' in page.text
    assert "Also refresh the 1 video already downloaded" in page.text
    assert "already downloaded" in page.text
    assert "(1 new)" in page.text

    job_id = location.rsplit("/", 1)[1]
    client.post(
        f"/jobs/{job_id}/download",
        data={"policy": "efficient", "refresh_existing": "1"},
        follow_redirects=False,
    )
    deadline = time.time() + 5
    while not seen and time.time() < deadline:
        time.sleep(0.02)
    assert seen and seen[0]["refresh_existing"] is True


def test_a_bot_blocked_review_says_so(tmp_path: Path, monkeypatch):
    def blocked_probe(*args, **kwargs):
        result = fake_probe(*args, **kwargs)
        result.bot_blocked = True
        return result

    monkeypatch.setattr(jobs_module, "probe_playlist", blocked_probe)
    manager = JobManager(lib_root(tmp_path))
    job = wait_for(
        manager, manager.create_probe("PLTESTJOBS00000000000000000001").id, {"ready"},
    )
    assert job.notice == jobs_module.BOT_BLOCKED_NOTICE
