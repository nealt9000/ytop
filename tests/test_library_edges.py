"""Libraries that are read-only, unplugged mid-run or plugged in late (#11)."""

from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from factories import add_playlist, lib_root, library_set, make_library
from test_jobs import _manager, _two_libraries, fake_probe, wait_for
from ytop.layout import LibraryLayout
from ytop.libraries import RecoveryDeferred
from ytop.tags import TagStore
from ytop.web import jobs as jobs_module
from ytop.web.app import create_app
from ytop.web.jobs import PAUSED, JobManager
from ytop.yt.download import FetchResult
from ytop.yt.metadata import write_json

posix_perms = pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="POSIX permission bits do not stop root (or Windows)",
)


def _rows(path: Path) -> list[dict]:
    return json.loads(path.read_text())["jobs"]


def _done_download(root: Path):
    return lambda probe, config, **kwargs: FetchResult(
        root, probe.playlist, [], total_bytes=0,
    )


def _stale_running(tmp_path: Path, monkeypatch) -> tuple[Path, str]:
    """A library whose jobs.json still says a download is running, as a
    stick pulled mid-download (or a killed server) leaves it."""
    monkeypatch.setattr(jobs_module, "probe_playlist", fake_probe)
    root = lib_root(tmp_path)
    manager = JobManager(root)
    job = manager.create_probe("PLTESTJOBS00000000000000000001")
    wait_for(manager, job.id, {"ready"})
    manager.close()
    layout = LibraryLayout(root)
    rows = _rows(layout.jobs_file)
    rows[0]["status"] = "running"
    rows[0]["started_at"] = "2026-11-01T00:00:00+00:00"
    rows[0]["items"][0]["status"] = "downloading"
    write_json(layout.jobs_file, {"schema": 1, "jobs": rows})
    return root, job.id


# -- read-only libraries ---------------------------------------------------
@posix_perms
def test_startup_does_not_rewrite_an_unchanged_read_only_library(
    tmp_path: Path, monkeypatch, caplog
):
    monkeypatch.setattr(jobs_module, "probe_playlist", fake_probe)
    root = lib_root(tmp_path)
    manager = JobManager(root)
    job = manager.create_probe("PLTESTJOBS00000000000000000001")
    wait_for(manager, job.id, {"ready"})
    manager.close()
    layout = LibraryLayout(root)
    before = layout.jobs_file.read_bytes()
    mode = layout.data_dir.stat().st_mode
    layout.data_dir.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    try:
        with caplog.at_level(logging.WARNING, logger="ytop.web.jobs"):
            again = JobManager(root)
            assert again.get(job.id).status == "ready"
            again.close()
    finally:
        layout.data_dir.chmod(mode)
    assert "could not save jobs" not in caplog.text
    assert layout.jobs_file.read_bytes() == before


@posix_perms
def test_unwritable_library_warns_once(tmp_path: Path, monkeypatch, caplog):
    root, job_id = _stale_running(tmp_path, monkeypatch)
    layout = LibraryLayout(root)
    mode = layout.data_dir.stat().st_mode
    layout.data_dir.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    try:
        with caplog.at_level(logging.WARNING, logger="ytop.web.jobs"):
            manager = JobManager(root)
            manager.set_notice(job_id, "one")
            manager.set_notice(job_id, "two")
            manager.close()
    finally:
        layout.data_dir.chmod(mode)
    assert caplog.text.count("could not save jobs") == 1


# -- a stick pulled mid-run ------------------------------------------------
def test_restart_pauses_a_stale_running_download(tmp_path: Path, monkeypatch):
    root, job_id = _stale_running(tmp_path, monkeypatch)
    manager = JobManager(root)
    job = manager.get(job_id)
    assert job.status == PAUSED
    assert not job.active
    assert "Resume" in (job.error or "")
    assert [row["status"] for row in job.items] == ["pending", "pending"]
    # Saved back as paused, and Resume restarts the download.
    assert _rows(LibraryLayout(root).jobs_file)[0]["status"] == PAUSED
    monkeypatch.setattr(jobs_module, "download_probe", _done_download(root))
    resumed = manager.start_download(job_id)
    assert resumed.status == "queued"
    assert wait_for(manager, job_id, {"done", "failed"}).status == "done"
    manager.close()


def test_restart_still_fails_interrupted_moves_and_probes(tmp_path: Path):
    root = lib_root(tmp_path)
    layout = LibraryLayout(root)
    write_json(layout.jobs_file, {"schema": 1, "jobs": [
        {"id": "probejob0001", "status": "probing", "kind": "playlist",
         "created_at": "2026-11-01T00:00:00+00:00"},
        {"id": "updatejob001", "status": "running", "kind": "update",
         "created_at": "2026-11-01T00:00:01+00:00"},
    ]})
    manager = JobManager(root)
    assert manager.get("probejob0001").status == "failed"
    assert manager.get("updatejob001").status == "failed"
    manager.close()


def test_newly_plugged_library_pauses_its_stale_running_rows(
    tmp_path: Path, monkeypatch
):
    home, usb, libs, usb_ref = _two_libraries(tmp_path)
    write_json(usb.jobs_file, {"schema": 1, "jobs": [
        {"id": "stickjob0001", "status": "running", "kind": "playlist",
         "library_id": usb_ref.id, "library_label": "USB stick",
         "title": "Pulled", "items": [{"id": "v1", "status": "downloading"}],
         "started_at": "2026-11-01T00:00:00+00:00",
         "created_at": "2026-11-01T00:00:00+00:00"},
    ]})
    parked = tmp_path / "parked"
    shutil.move(usb.root, parked)
    libs.refresh()
    manager = _manager(libs)
    assert manager.get("stickjob0001") is None
    shutil.move(parked, usb.root)
    libs.refresh()
    manager.recover_library(libs.get(usb_ref.id))
    job = manager.get("stickjob0001")
    assert job.status == PAUSED
    assert job.items[0]["status"] == "pending"
    manager.close()


# -- a paused review must not skip the review ------------------------------
def test_resuming_a_paused_review_returns_to_the_review(
    tmp_path: Path, monkeypatch
):
    home, usb, libs, usb_ref = _two_libraries(tmp_path)
    monkeypatch.setattr(jobs_module, "probe_playlist", fake_probe)
    monkeypatch.setattr(jobs_module, "download_probe", _done_download(usb.root))
    manager = _manager(libs)
    job = manager.create_probe(
        "PLTESTJOBS00000000000000000001", library_id=usb_ref.id
    )
    wait_for(manager, job.id, {"ready"})
    parked = tmp_path / "parked"
    shutil.move(usb.root, parked)
    libs.refresh()
    assert manager.get(job.id).status == PAUSED
    shutil.move(parked, usb.root)
    libs.refresh()
    resumed = manager.start_download(job.id, "native")
    assert resumed.status == "ready"
    assert resumed.error is None
    # Nothing was queued: the user still has to confirm the download.
    assert manager.get(job.id).status == "ready"
    started = manager.start_download(job.id, "efficient")
    assert started.status == "queued"
    assert wait_for(manager, job.id, {"done", "failed"}).status == "done"
    manager.close()


def test_resuming_a_legacy_paused_review_returns_to_the_review(tmp_path: Path):
    root = lib_root(tmp_path)
    write_json(LibraryLayout(root).jobs_file, {"schema": 1, "jobs": [
        {"id": "pausedjob001", "status": PAUSED, "kind": "playlist",
         "url": "PLX", "items": [{"id": "v1", "status": "pending"}],
         "error": "not connected",
         "created_at": "2026-11-01T00:00:00+00:00"},
    ]})
    manager = JobManager(root)
    assert manager.start_download("pausedjob001").status == "ready"
    manager.close()


# -- late-plugged libraries ------------------------------------------------
def test_replugged_library_is_reread_even_if_the_manager_missed_the_unplug(
    tmp_path: Path,
):
    home, usb, libs, usb_ref = _two_libraries(tmp_path)
    manager = _manager(libs)
    with manager._lock:
        assert usb_ref.id in manager._refs
    # Edited elsewhere while it was unplugged (the manager never noticed).
    write_json(usb.jobs_file, {"schema": 1, "jobs": [
        {"id": "elsewhere001", "status": "done", "kind": "playlist",
         "library_id": usb_ref.id, "title": "From another computer",
         "created_at": "2026-11-01T00:00:00+00:00"},
    ]})
    manager.recover_library(libs.get(usb_ref.id))
    assert manager.get("elsewhere001") is not None
    manager.close()


def test_resume_recovers_a_deferred_library_first(tmp_path: Path, monkeypatch):
    home, usb, libs, usb_ref = _two_libraries(tmp_path)
    monkeypatch.setattr(jobs_module, "probe_playlist", fake_probe)
    monkeypatch.setattr(jobs_module, "download_probe", _done_download(usb.root))
    manager = _manager(libs)
    job = manager.create_probe(
        "PLTESTJOBS00000000000000000001", library_id=usb_ref.id
    )
    wait_for(manager, job.id, {"ready"})
    parked = tmp_path / "parked"
    shutil.move(usb.root, parked)
    libs.refresh()
    libs.libraries()  # the library set sees the unplug
    # A download that paused when its stick was pulled.
    with manager._lock:
        manager._jobs[job.id].status = "queued"
    manager._pause(job.id)

    def hook(ref):
        # Like the app's hook while another thread publishes: it cannot
        # wait for PUBLISH_LOCK while this thread holds the job lock.
        if manager.holds_lock():
            raise RecoveryDeferred(ref.id)
        manager.recover_library(ref)

    libs.on_new = hook
    shutil.move(parked, usb.root)
    libs.refresh()
    resumed = manager.start_download(job.id)
    assert resumed.status == "queued"
    assert wait_for(manager, job.id, {"done", "failed"}).status == "done"
    manager.close()


# -- tag edits -------------------------------------------------------------
def _post(client: TestClient, path: str, **data: str):
    data["csrf_token"] = client.app.state.csrf_token
    return client.post(path, data=data, follow_redirects=False)


@posix_perms
def test_unwritable_library_hides_tag_controls(tmp_path: Path):
    layout = make_library(tmp_path, "Locked")
    add_playlist(layout, "P1", "Zebra lectures", [("v1", "Stripes")])
    libs = library_set(layout)
    client = TestClient(create_app(layout.root, libraries=libs))
    try:
        assert "Add a tag" in client.get("/playlist/P1").text
        mode = layout.data_dir.stat().st_mode
        layout.data_dir.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        try:
            page = client.get("/playlist/P1").text
            tags = client.get("/tags").text
        finally:
            layout.data_dir.chmod(mode)
        assert "Add a tag" not in page
        assert 'action="/tags/' not in tags
    finally:
        client.app.state.jobs.close()


def test_tag_edit_retries_a_brief_lock(tmp_path: Path, monkeypatch):
    layout = make_library(tmp_path, "Shared")
    add_playlist(layout, "P1", "Zebra lectures", [("v1", "Stripes")])
    real_add = TagStore.add
    calls = {"n": 0}

    def flaky(self, pid, tag):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(errno.EACCES, "locked by another process")
        return real_add(self, pid, tag)

    monkeypatch.setattr(TagStore, "add", flaky)
    client = TestClient(create_app(layout.root, libraries=library_set(layout)))
    try:
        response = _post(client, "/playlist/P1/tags", action="add", tag="maths")
        assert response.status_code == 303
        assert calls["n"] == 2
        assert "maths" in layout.tags_file.read_text()
    finally:
        client.app.state.jobs.close()


def test_tag_edit_on_a_read_only_mount_does_not_retry(tmp_path: Path, monkeypatch):
    layout = make_library(tmp_path, "Mounted")
    add_playlist(layout, "P1", "Zebra lectures", [("v1", "Stripes")])
    calls = {"n": 0}

    def readonly(self, pid, tag):
        calls["n"] += 1
        raise OSError(errno.EROFS, "read-only file system")

    monkeypatch.setattr(TagStore, "add", readonly)
    client = TestClient(create_app(layout.root, libraries=library_set(layout)))
    try:
        response = _post(client, "/playlist/P1/tags", action="add", tag="maths")
        assert response.status_code == 409
        assert calls["n"] == 1
    finally:
        client.app.state.jobs.close()

