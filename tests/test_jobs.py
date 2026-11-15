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


def test_restart_marks_an_active_job_failed(tmp_path: Path, monkeypatch):
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
    assert loaded.status == "failed"
    assert "restart" in (loaded.error or "")


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
    assert listed[ready.id].status_label == "Paused — library offline"
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
