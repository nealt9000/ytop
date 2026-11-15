from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from factories import add_playlist, lib_root, library_set, make_library
from ytop.config import Config, WebConfig, is_loopback_host
from ytop.layout import LibraryLayout
from ytop.transcripts.store import index_library, remove_playlist, search
from ytop.web import fsops
from ytop.web import jobs as jobs_module
from ytop.web.app import _safe_next_url, create_app
from ytop.web.jobs import JobDestinationError, JobManager
from ytop.web.library import ProgressStore
from ytop.yt.formats import Selection
from ytop.yt.metadata import CorruptDocumentError, write_json
from ytop.yt.probe import JobCancelled, ProbedItem, ProbeResult

PLAYLIST_ID = "PLMODERN000000000000000000001"


def _wait(manager: JobManager, job_id: str, statuses: set[str], timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = manager.get(job_id)
        if job is not None and job.status in statuses:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job did not reach {statuses}: {manager.get(job_id)}")


def _probe(value, config, limit=None, on_item=None, on_total=None, **kwargs):
    playlist = {
        "id": PLAYLIST_ID, "title": "Modern", "channel": "Test",
        "entries": [{"id": "vid1", "title": "One"}],
    }
    if on_total:
        on_total(1)
    item = ProbedItem(
        index=1, id="vid1", url="https://example.test/watch?v=vid1",
        title="One", duration=60.0,
    )
    item.selection = Selection(
        video={"format_id": "v", "vcodec": "av01", "height": 240,
               "filesize": 1000},
        audio={"format_id": "a", "acodec": "opus", "filesize": 100},
    )
    if on_item:
        on_item(item)
    return ProbeResult(url=value, playlist=playlist, items=[item])


def _make_playlist(root: Path, title="Modern", playlist_id=PLAYLIST_ID) -> Path:
    """A one-video playlist in the library at ``root``; returns its meta dir."""
    return add_playlist(LibraryLayout(root), playlist_id, title, [("vid1", "One")])


# -- Phase 1: lifecycle -----------------------------------------------------

def test_unexpected_worker_exception_marks_the_job_failed(tmp_path, monkeypatch):
    def boom(value, config, **kwargs):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(jobs_module, "probe_playlist", boom)
    manager = JobManager(lib_root(tmp_path))
    job = manager.create_probe("PLMODERN000000000000000000001")
    job = _wait(manager, job.id, {"failed"})
    assert "RuntimeError" in (job.error or "")
    assert job.active is False


def test_more_than_fifty_active_jobs_stay_discoverable(tmp_path, monkeypatch):
    release = threading.Event()

    def blocked(value, config, **kwargs):
        release.wait(5)
        raise JobCancelled("cancelled by the user")

    monkeypatch.setattr(jobs_module, "probe_playlist", blocked)
    manager = JobManager(lib_root(tmp_path))
    jobs = [
        manager.create_probe(f"PLMODERN{i:019d}") for i in range(55)
    ]
    assert len(manager.list()) == 55
    assert all(job.active for job in manager.list())
    release.set()
    manager.close()
    assert all(not job.active for job in manager.list())


def test_close_cancels_running_work_and_flushes(tmp_path, monkeypatch):
    started = threading.Event()

    def blocked(value, config, **kwargs):
        started.set()
        while True:
            cancel = kwargs.get("cancel")
            if cancel and cancel():
                raise JobCancelled("cancelled by the user")
            time.sleep(0.01)

    monkeypatch.setattr(jobs_module, "probe_playlist", blocked)
    manager = JobManager(lib_root(tmp_path))
    job = manager.create_probe("PLMODERN000000000000000000001")
    assert started.wait(2)
    manager.close()
    loaded = manager.get(job.id)
    assert loaded.status == "cancelled"
    # Reloading the durable record must not resurrect an active job.
    reopened = JobManager(lib_root(tmp_path))
    assert reopened.get(job.id).status == "cancelled"


def test_offline_explicit_location_never_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "probe_playlist", _probe)
    manager = JobManager(lib_root(tmp_path))
    offline = manager._lib.libraries.add_folder(tmp_path / "unplugged", "Unplugged")
    import shutil

    shutil.rmtree(tmp_path / "unplugged")
    manager._lib.libraries.refresh()
    with pytest.raises(JobDestinationError):
        manager.create_probe(
            "PLMODERN000000000000000000001", library_id=offline.id
        )


def test_ready_job_for_a_deleted_playlist_cannot_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_module, "probe_playlist", _probe)
    manager = JobManager(lib_root(tmp_path))
    job = manager.create_probe("PLMODERN000000000000000000001")
    _wait(manager, job.id, {"ready"})
    manager._update(job.id, in_library=True)
    assert manager._lib.resolve_playlist(PLAYLIST_ID) is None
    with pytest.raises(ValueError):
        manager.start_download(job.id)


# -- Phase 2: persistence, storage, moves -----------------------------------

def test_corrupt_progress_is_preserved_not_reset(tmp_path):
    store = ProgressStore(make_library(tmp_path))
    store.path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CorruptDocumentError):
        store.update("vid1", 12.0)
    assert store.path.read_text(encoding="utf-8") == "{not json"
    assert store.read() == {}


def test_storage_rejects_a_folder_nested_in_a_library(tmp_path):
    root = lib_root(tmp_path)
    _make_playlist(root)
    nested = LibraryLayout(root).media_root / "Modern" / "nested"
    nested.mkdir()
    with pytest.raises(ValueError):
        library_set(root).add_folder(nested, "Nested")
    assert not (nested / ".ytop").exists()


def test_move_reconciliation_reports_an_uncleaned_source(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    source = _make_playlist(library)
    target = tmp_path / "usb" / "playlists" / f"Modern [{PLAYLIST_ID}]"
    target.mkdir(parents=True)
    (target / "playlist.json").write_text("{}", encoding="utf-8")
    result = fsops.reconcile_move(
        source=source, target=target,
        staging=tmp_path / "usb" / "playlists" / ".stale.moving",
    )
    assert result.status == "failed"
    assert result.cleanup_complete is False
    assert "original folder is still there" in (result.error or "")


# -- Phase 2D: request protection ------------------------------------------

def test_non_loopback_bind_is_rejected():
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("::1")
    assert is_loopback_host("localhost")
    with pytest.raises(ValueError):
        WebConfig(host="0.0.0.0")
    with pytest.raises(ValueError):
        WebConfig(host="192.168.1.20")


def test_protocol_relative_redirects_are_rejected():
    assert _safe_next_url("//evil.example/x") == "/downloads"
    assert _safe_next_url("https://evil.example") == "/downloads"
    assert _safe_next_url("/add/abc?x=1") == "/add/abc?x=1"


def test_host_and_csrf_protection(tmp_path):
    library = lib_root(tmp_path)
    app = create_app(library, libraries=library_set(library))
    client = TestClient(app)
    assert client.get(
        "/healthz", headers={"host": "evil.example"}
    ).status_code == 400
    assert client.post(
        "/storage/add",
        data={"path": str(tmp_path / "usb")},
        headers={"origin": "http://evil.example"},
    ).status_code == 403
    assert client.post(
        "/storage/add",
        data={"path": str(tmp_path / "usb")},
        headers={
            "origin": "http://testserver",
            "sec-fetch-site": "cross-site",
        },
    ).status_code == 403
    assert client.post(
        "/storage/add",
        data={"path": str(tmp_path / "usb")},
        headers={
            "origin": "http://testserver",
            "sec-fetch-site": "same-origin",
        },
    ).status_code == 403

    page = client.get("/storage")
    token = page.text.split('name="csrf-token" content="', 1)[1].split(
        '"', 1
    )[0]
    response = client.post(
        "/storage/add",
        data={"path": str(tmp_path / "usb"), "csrf_token": token},
        headers={
            "origin": "http://testserver",
            "sec-fetch-site": "same-origin",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    # Non-browser clients still work without a token.
    assert client.post(
        "/storage/add", data={"path": str(tmp_path / "usb2")},
        follow_redirects=False,
    ).status_code == 303


def test_non_finite_progress_and_preview_values_are_rejected(tmp_path):
    client = TestClient(create_app(lib_root(tmp_path), libraries=library_set(lib_root(tmp_path))))
    response = client.post(
        "/api/progress",
        content='{"video_id":"vid1","position":1e999}',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422
    assert client.get(
        "/media/pl/vid/preview?t=inf"
    ).status_code == 422


# -- Phase 3: correctness ---------------------------------------------------

def test_each_stream_is_estimated_independently():
    selection = Selection(
        video={"format_id": "v", "vcodec": "av01", "height": 240,
               "filesize": 1_000_000, "duration": 100},
        audio={"format_id": "a", "acodec": "opus", "abr": 128,
               "duration": 100},
        alternates=[{"format_id": "a2", "acodec": "opus", "abr": 64,
                     "duration": 100}],
    )
    assert selection.estimated_bytes == 1_000_000 + 1_600_000 + 800_000


def test_transcript_index_keeps_both_referencing_playlists(tmp_path):
    layout = make_library(tmp_path)
    for playlist_id, title in (("PLA", "A"), ("PLB", "B")):
        add_playlist(layout, playlist_id, title, [("vid1", f"{title} video")])
        write_json(layout.video_meta_dir(playlist_id, "vid1") / "transcript.json", {
            "schema": 1, "video_id": "vid1", "title": f"{title} video",
            "language": "en", "kind": "manual",
            "segments": [{"start": 0, "end": 2, "text": "shared words"}],
        })
    index_library(layout)
    rows = search(layout, "shared")
    assert {row["playlist_id"] for row in rows} == {"PLA", "PLB"}
    remove_playlist(layout, "PLA")
    rows = search(layout, "shared")
    assert {row["playlist_id"] for row in rows} == {"PLB"}


def test_same_video_id_can_be_indexed_twice_without_losing_a_row(tmp_path):
    from ytop.transcripts.store import _connect, ensure_schema, index_transcript

    doc = {
        "schema": 1, "video_id": "vid1", "language": "en", "kind": "manual",
        "segments": [{"start": 0, "end": 1, "text": "duplicate identity"}],
    }
    with _connect(make_library(tmp_path)) as conn:
        ensure_schema(conn)
        index_transcript(conn, playlist_id="PLA", video_id="vid1",
                         title="A", doc=doc)
        index_transcript(conn, playlist_id="PLB", video_id="vid1",
                         title="B", doc=doc)
        rows = conn.execute(
            "SELECT playlist_id FROM transcript_meta WHERE video_id = ?",
            ("vid1",),
        ).fetchall()
    assert {row["playlist_id"] for row in rows} == {"PLA", "PLB"}


# -- Phase 4/5: library views, live updates and UI hooks --------------------

def _app_for(tmp_path, monkeypatch):
    library = lib_root(tmp_path)
    _make_playlist(library)
    monkeypatch.setattr(jobs_module, "probe_playlist", _probe)
    app = create_app(library, libraries=library_set(library))
    return app, TestClient(app)


def test_library_page_exposes_filter_sort_hooks(tmp_path, monkeypatch):
    _, client = _app_for(tmp_path, monkeypatch)
    text = client.get("/").text
    assert 'id="browse-group"' in text
    assert 'id="browse-sort"' in text
    assert 'id="browse-count"' in text
    assert 'data-tags=' in text
    assert 'data-updated=' in text
    assert 'aria-valuenow=' in text


def test_playlist_page_exposes_filter_and_safe_confirm_dialogs(
    tmp_path, monkeypatch
):
    _, client = _app_for(tmp_path, monkeypatch)
    text = client.get(f"/playlist/{PLAYLIST_ID}").text
    assert 'id="playlist-filter"' in text
    assert 'id="playlist-filter-state"' in text
    assert 'data-watched="0"' in text
    assert "onsubmit=" not in text
    assert 'data-confirm="Remove' in text


def test_downloads_page_uses_live_hooks_not_meta_refresh(
    tmp_path, monkeypatch
):
    app, client = _app_for(tmp_path, monkeypatch)
    job = app.state.jobs.create_probe(PLAYLIST_ID)
    text = client.get("/downloads").text
    assert 'http-equiv="refresh"' not in text
    assert 'id="downloads-content"' in text
    assert f'data-job-id="{job.id}"' in text
    assert "data-job-bar" in text
    assert "data-job-metrics" in text
    data = client.get(f"/api/jobs/{job.id}").json()
    assert data["active"] in (True, False)
    assert 0.0 <= data["percent"] <= 1.0


def test_browser_modules_and_watch_controls_are_served(
    tmp_path, monkeypatch
):
    _, client = _app_for(tmp_path, monkeypatch)
    home = client.get("/").text
    assert 'type="module"' in home
    module = client.get("/static/app.js")
    assert module.status_code == 200
    assert 'from "./js/player.js"' in module.text
    assert client.get("/static/js/player.js").status_code == 200
    assert client.get("/static/js/live.js").status_code == 200
    watch = client.get(f"/watch/{PLAYLIST_ID}/vid1").text
    assert 'id="speed-select"' in watch
    assert 'data-live-status' not in watch  # only active pages poll


def test_add_page_live_state_does_not_target_the_body(
    tmp_path, monkeypatch
):
    """The polling code must not write text into <body>.

    Regression: <body> carried ``data-live-status``, so the global selector
    replaced the whole UI with the status word.
    """
    import re

    library = lib_root(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def blocked(value, config, **kwargs):
        started.set()
        release.wait(5)
        raise JobCancelled("cancelled by the user")

    monkeypatch.setattr(jobs_module, "probe_playlist", blocked)
    app = create_app(library, libraries=library_set(library))
    client = TestClient(app)
    job = app.state.jobs.create_probe("PLNOTINLIB0000000000000000002")
    assert started.wait(2)
    try:
        text = client.get(f"/add/{job.id}").text
        body = re.search(r"<body[^>]*>", text)
        assert body is not None
        assert "data-live-status" not in body.group(0)
        assert 'data-live-state="probing"' in body.group(0)
        main = re.search(r'<main[^>]*id="main-content"[^>]*>(.*?)</main>', text, re.S)
        assert main is not None
        assert "data-live-status" in main.group(1)
        module = client.get("/static/js/live.js").text
        assert '$("[data-live-status]", main)' in module
    finally:
        app.state.jobs.close()
        release.set()


def test_forgetting_a_location_used_by_an_active_job_is_rejected(
    tmp_path, monkeypatch
):
    library = lib_root(tmp_path)
    _make_playlist(library)
    started = threading.Event()
    release = threading.Event()

    def blocked(value, config, **kwargs):
        started.set()
        release.wait(5)
        raise JobCancelled("cancelled by the user")

    monkeypatch.setattr(jobs_module, "probe_playlist", blocked)
    app = create_app(library, libraries=library_set(library))
    client = TestClient(app)
    location = app.state.library.libraries.add_folder(tmp_path / "usb", "USB")
    app.state.jobs.create_probe(
        "PLNOTINLIB0000000000000000001", library_id=location.id
    )
    assert started.wait(2)
    try:
        response = client.post(f"/storage/{location.id}/remove")
        assert response.status_code == 409
        assert "using that library" in response.text
    finally:
        app.state.jobs.close()
        release.set()


def _finish_job(app, job_id: str, status: str, finished_at: str) -> None:
    manager = app.state.jobs
    deadline = time.time() + 5
    while manager.get(job_id).active and time.time() < deadline:
        time.sleep(0.01)  # let the fake probe finish before overriding it
    with manager._lock:
        job = manager._jobs[job_id]
        job.status = status
        job.finished_at = finished_at


def test_downloads_page_dates_finished_jobs(tmp_path, monkeypatch):
    # A stick carries its job history to other computers, so a finished job
    # says when it ran instead of looking like current work.
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        app, client = _app_for(tmp_path, monkeypatch)
        done = app.state.jobs.create_probe(PLAYLIST_ID)
        _finish_job(app, done.id, "done", "2026-11-14T02:09:57+00:00")
        failed = app.state.jobs.create_probe(PLAYLIST_ID)
        _finish_job(app, failed.id, "failed", "2026-11-13T23:05:00+00:00")
        text = client.get("/downloads").text
        assert "finished 14 Nov 2026, 02:09" in text
        assert "ended 13 Nov 2026, 23:05" in text
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_format_finished_uses_local_time(monkeypatch):
    from ytop.web.render import format_finished

    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        assert format_finished("2026-11-14T02:09:57+00:00") == "13 Nov 2026, 21:09"
        assert format_finished("") == ""
        assert format_finished(None) == ""
        assert format_finished("not a date") == "not a date"
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_job_history_is_cleared_not_removed(tmp_path, monkeypatch):
    # "Remove" read as deleting the downloaded files; it only drops the entry.
    app, client = _app_for(tmp_path, monkeypatch)
    job = app.state.jobs.create_probe(PLAYLIST_ID)
    _finish_job(app, job.id, "done", "2026-11-14T02:09:57+00:00")
    downloads = client.get("/downloads").text
    assert ">Clear</button>" in downloads
    assert ">Remove</button>" not in downloads
    assert "until you clear them" in downloads
    for status in ("cancelled", "failed", "paused_offline", "ready"):
        _finish_job(app, job.id, status, "2026-11-14T02:09:57+00:00")
        page = client.get(f"/add/{job.id}").text
        assert ">Remove" not in page, status
        assert "Clear" in page, status
