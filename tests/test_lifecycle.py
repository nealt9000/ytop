"""Quit, idle exit, safe-to-unplug and late-plugged library recovery."""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from factories import add_playlist, library_set, make_library
from ytop import publish
from ytop.layout import LibraryLayout, init_library, read_library
from ytop.web import app as app_module
from ytop.web.app import create_app
from ytop.web.fsops import STAGING_PREFIX
from ytop.web.jobs import Job
from ytop.yt import metadata
from ytop.yt.metadata import read_json, write_json

HOME_PLAYLIST = "PLHOME000000000000000000000000001"
USB_PLAYLIST = "PLUSB0000000000000000000000000002"
BROWSER = {"origin": "http://testserver", "sec-fetch-site": "same-origin"}


def _app(tmp_path: Path, **kwargs):
    home = make_library(tmp_path / "home", "Laptop")
    add_playlist(home, HOME_PLAYLIST, "Laptop shows", [("vid1", "Video One")])
    libs = library_set(home)
    app = create_app(home.root, libraries=libs, **kwargs)
    app.state.server = SimpleNamespace(should_exit=False)
    return app, home, libs


def _add_job(app, library_id: str, status: str = "running") -> Job:
    job = Job(id=f"job-{status}-{library_id[:6]}", status=status,
              library_id=library_id, kind="playlist", playlist_id="PLX")
    manager = app.state.jobs
    with manager._lock:
        manager._jobs[job.id] = job
    return job


def _usb(tmp_path: Path, label: str = "USB stick") -> LibraryLayout:
    usb = LibraryLayout(tmp_path / "usb")
    init_library(usb.root, label)
    add_playlist(usb, USB_PLAYLIST, "Drive lectures", [("vid9", "On the drive")])
    return usb


# -- quit ------------------------------------------------------------------
def test_quit_refuses_with_active_jobs(tmp_path):
    app, home, libs = _app(tmp_path)
    with TestClient(app) as client:
        _add_job(app, libs.default().id)
        response = client.post("/api/quit")
        assert response.status_code == 409
        assert response.json() == {"active_jobs": 1}
        assert app.state.server.should_exit is False


def test_quit_requires_csrf_from_a_browser(tmp_path):
    app, _home, _libs = _app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/api/quit", headers=BROWSER)
        assert response.status_code == 403
        assert app.state.server.should_exit is False
        ok = client.post(
            "/api/quit", headers=BROWSER,
            data={"csrf_token": app.state.csrf_token},
        )
        assert ok.status_code == 200
        assert app.state.server.should_exit is True


def test_quit_force_stops(tmp_path, monkeypatch, _no_host_sync):
    app, home, libs = _app(tmp_path)
    flushed: list[Path] = []
    monkeypatch.setattr(app_module, "fsync_dir", flushed.append)
    released: list[bool] = []
    app.state.shutdown_hooks.append(lambda: released.append(True))
    with TestClient(app) as client:
        _add_job(app, libs.default().id)
        response = client.post("/api/quit", data={"force": "1"})
        assert response.status_code == 200
        assert "YTOP has stopped. It's safe to unplug." in response.text
        # A static page: nothing it shows needs the stopped server.
        assert "/static/" not in response.text
        assert app.state.server.should_exit is True
        assert app.state.jobs._closed is True
        assert home.root in flushed
        assert _no_host_sync == [True]
    # The instance file is released by the shutdown hooks, exactly once.
    assert released == [True]


def test_takeover_shutdown_goes_through_quit(tmp_path, monkeypatch, _no_host_sync):
    app, home, libs = _app(tmp_path, instance_token="secret")
    flushed: list[Path] = []
    monkeypatch.setattr(app_module, "fsync_dir", flushed.append)
    headers = {"X-YTOP-Token": "secret"}
    with TestClient(app) as client:
        response = client.post("/api/instance/shutdown", headers=headers)
        assert response.status_code == 202
        assert app.state.stopping is True
        assert app.state.jobs._closed is True
        assert _wait_for(lambda: app.state.server.should_exit is True)
        assert home.root in flushed and _no_host_sync == [True]


def test_takeover_shutdown_answers_before_a_slow_sync(tmp_path, monkeypatch):
    """A slow stick's final sync must not outlast the new launcher's
    request timeout: the endpoint answers at once and flushes afterwards,
    stopping the server only once everything is on the drive."""
    app, _home, _libs = _app(tmp_path, instance_token="secret")
    release = threading.Event()
    synced: list[bool] = []

    def slow_sync() -> None:
        release.wait(10)
        synced.append(True)

    monkeypatch.setattr(app_module, "_sync_filesystems", slow_sync)
    headers = {"X-YTOP-Token": "secret"}
    with TestClient(app) as client:
        started = time.monotonic()
        response = client.post("/api/instance/shutdown", headers=headers)
        assert response.status_code == 202
        assert time.monotonic() - started < 2.0
        assert app.state.stopping is True and app.state.jobs._closed is True
        assert app.state.server.should_exit is False  # still flushing
        release.set()
        assert _wait_for(lambda: app.state.server.should_exit is True)
        assert synced == [True]


def test_quit_during_a_takeover_flush_waits_for_it(tmp_path, monkeypatch):
    """Quit in the old tab while a takeover's background flush runs must not
    say "safe to unplug" before the sync has finished."""
    app, _home, _libs = _app(tmp_path, instance_token="secret")
    release = threading.Event()
    synced: list[bool] = []

    def slow_sync() -> None:
        release.wait(10)
        synced.append(True)

    monkeypatch.setattr(app_module, "_sync_filesystems", slow_sync)
    with TestClient(app) as client:
        assert client.post(
            "/api/instance/shutdown", headers={"X-YTOP-Token": "secret"}
        ).status_code == 202
        answered: list[int] = []
        quitter = threading.Thread(
            target=lambda: answered.append(client.post("/api/quit").status_code)
        )
        quitter.start()
        quitter.join(0.5)
        assert answered == [] and synced == []  # still waiting for the sync
        release.set()
        quitter.join(5)
        assert answered == [200] and synced == [True]


def test_takeover_shutdown_check_and_close_are_atomic(tmp_path):
    """A job that slipped in after the endpoint counted none still wins."""
    app, _home, libs = _app(tmp_path, instance_token="secret")
    headers = {"X-YTOP-Token": "secret"}
    with TestClient(app) as client:
        _add_job(app, libs.default().id, status="queued")
        app.state.jobs.list = lambda: []  # the early count sees nothing
        response = client.post("/api/instance/shutdown", headers=headers)
        assert response.status_code == 409
        assert app.state.stopping is False
        assert app.state.jobs._closed is False
        assert app.state.server.should_exit is False


def test_heartbeat_endpoint(tmp_path):
    now = [1000.0]
    app, _home, _libs = _app(tmp_path, clock=lambda: now[0])
    with TestClient(app) as client:
        now[0] = 1234.0
        response = client.post("/api/heartbeat")
        assert response.status_code == 204
        assert app.state.last_heartbeat == 1234.0


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_idle_exit_after_no_heartbeat(tmp_path):
    now = [0.0]
    app, _home, libs = _app(
        tmp_path, portable=True, clock=lambda: now[0],
        idle_timeout_s=300.0, idle_check_s=0.01,
    )
    with TestClient(app) as client:
        client.post("/api/heartbeat")
        now[0] = 299.0
        time.sleep(0.1)
        assert app.state.server.should_exit is False
        # Silent for over 300 s, but a download is running: stay up.
        job = _add_job(app, libs.default().id)
        now[0] = 400.0
        time.sleep(0.1)
        assert app.state.server.should_exit is False
        with app.state.jobs._lock:
            app.state.jobs._jobs[job.id].status = "done"
        assert _wait_for(lambda: app.state.server.should_exit is True)
        assert app.state.jobs._closed is True


def test_no_idle_exit_outside_portable_mode(tmp_path):
    now = [0.0]
    app, _home, _libs = _app(
        tmp_path, clock=lambda: now[0], idle_timeout_s=300.0, idle_check_s=0.01,
    )
    with TestClient(app):
        now[0] = 10_000.0
        time.sleep(0.1)
        assert app.state.server.should_exit is False


# -- running-from banner -----------------------------------------------------
def test_portable_banner_and_quit_item(tmp_path, monkeypatch):
    stick = tmp_path / "stick"
    monkeypatch.setenv("YTOP_HOME", str(stick))
    monkeypatch.setenv("YTOP_RUNTIME", str(stick / ".ytop/runtime/linux-x86_64"))
    app, _home, _libs = _app(tmp_path, portable=True)
    with TestClient(app) as client:
        page = client.get("/").text
        assert "Running from Laptop" in page
        assert 'action="/api/quit"' in page
        assert "running from this computer" not in page


def test_portable_banner_running_from_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("YTOP_HOME", str(tmp_path / "stick"))
    monkeypatch.setenv("YTOP_RUNTIME", str(tmp_path / "cache/ytop/runtime/b1"))
    app, _home, _libs = _app(tmp_path, portable=True)
    with TestClient(app) as client:
        page = client.get("/").text
        assert (
            "(running from this computer's cache — the stick can be unplugged "
            "after Quit)"
        ) in page.replace("&#39;", "'")


def test_no_banner_outside_portable_mode(tmp_path):
    app, _home, _libs = _app(tmp_path)
    with TestClient(app) as client:
        page = client.get("/").text
        assert "Running from" not in page
        assert 'action="/api/quit"' not in page


# -- safe to unplug ------------------------------------------------------------
def _storage_row(page: str, label: str) -> str:
    start = page.index(f"<strong>{label}</strong>")
    return page[start:page.index("</tr>", start)]


def test_safe_to_unplug_states(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "_unplug_needs_quit", lambda: False)
    app, home, libs = _app(tmp_path)
    usb = _usb(tmp_path)
    usb_ref = libs.add_folder(usb.root, "USB stick")
    other = LibraryLayout(tmp_path / "other")
    init_library(other.root, "Spare")
    libs.add_folder(other.root, "Spare")
    with TestClient(app) as client:
        page = client.get("/storage?refresh=1").text
        for label in ("Laptop", "USB stick", "Spare"):
            assert "Safe to unplug" in _storage_row(page, label)

        _add_job(app, usb_ref.id)
        (other.meta_root / f"{STAGING_PREFIX}abc.json").write_text("{}")
        page = client.get("/storage").text
        assert "Safe to unplug" in _storage_row(page, "Laptop")
        assert "Safe to unplug" not in _storage_row(page, "USB stick")
        assert "Not safe to unplug yet" in _storage_row(page, "USB stick")
        assert "Not safe to unplug yet" in _storage_row(page, "Spare")

        (other.meta_root / f"{STAGING_PREFIX}abc.json").unlink()
        write_json(other.rename_journal, {"phase": 2, "ops": []})
        page = client.get("/storage").text
        assert "Not safe to unplug yet" in _storage_row(page, "Spare")


def test_launch_library_needs_quit_on_windows_and_macos(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "_unplug_needs_quit", lambda: True)
    app, _home, libs = _app(tmp_path, portable=True)
    usb = _usb(tmp_path)
    libs.add_folder(usb.root, "USB stick")
    with TestClient(app) as client:
        page = client.get("/storage?refresh=1").text
        assert "Quit YTOP before unplugging" in _storage_row(page, "Laptop")
        assert "Safe to unplug" in _storage_row(page, "USB stick")


# -- late-plugged libraries ---------------------------------------------------
def test_late_plugged_library_recovered(tmp_path):
    app, _home, libs = _app(tmp_path)
    with TestClient(app) as client:
        assert "Drive lectures" not in client.get("/").text

        # A stick whose rename batch was interrupted on another computer.
        usb = _usb(tmp_path)
        meta_path = usb.video_meta_dir(USB_PLAYLIST, "vid9") / "meta.json"
        meta = read_json(meta_path)
        media = usb.resolve_media(meta["files"]["media"])
        assert media is not None
        tmp_rel = f"{media.parent.name}/{publish.TMP_PREFIX}0-abcd1234.webm"
        os.replace(media, usb.media_root / tmp_rel)
        meta["files"]["media"] = tmp_rel
        write_json(meta_path, meta)
        dst_rel = f"{media.parent.name}/01 - Renamed.webm"
        write_json(usb.rename_journal, {"phase": 2, "ops": [
            {"src": meta["files"]["media"], "tmp": tmp_rel, "dst": dst_rel},
        ]})
        # Registered behind the app's back, as another process would.
        write_json(libs.registry_path, {"schema": 1, "libraries": [str(usb.root)]})

        seen: list[bool] = []
        original = libs.on_new

        def spy(ref):
            assert original is not None
            original(ref)
            # Recovered before any page or job is handed the library.
            seen.append(usb.rename_journal.exists())

        libs.on_new = spy
        libs.refresh()
        page = client.get("/").text
        assert "Drive lectures" in page
        assert seen == [False]
        assert not usb.rename_journal.exists()
        assert (usb.media_root / dst_rel).is_file()
        assert read_json(meta_path)["files"]["media"] == dst_rel
        # Seen once: a later rescan does not recover it again.
        libs.refresh()
        client.get("/")
        assert seen == [False]


def _interrupt_rename(usb: LibraryLayout) -> str:
    """Leave ``usb`` with a half-done rename batch; returns the final name."""
    meta_path = usb.video_meta_dir(USB_PLAYLIST, "vid9") / "meta.json"
    meta = read_json(meta_path)
    media = usb.resolve_media(meta["files"]["media"])
    assert media is not None
    tmp_rel = f"{media.parent.name}/{publish.TMP_PREFIX}0-abcd1234.webm"
    os.replace(media, usb.media_root / tmp_rel)
    src_rel = meta["files"]["media"]
    meta["files"]["media"] = tmp_rel
    write_json(meta_path, meta)
    dst_rel = f"{media.parent.name}/01 - Renamed.webm"
    write_json(usb.rename_journal, {"phase": 2, "ops": [
        {"src": src_rel, "tmp": tmp_rel, "dst": dst_rel},
    ]})
    return dst_rel


def test_replugged_library_recovered_again(tmp_path):
    app, _home, libs = _app(tmp_path)
    with TestClient(app) as client:
        usb = _usb(tmp_path)
        write_json(libs.registry_path, {"schema": 1, "libraries": [str(usb.root)]})
        libs.refresh()
        assert "Drive lectures" in client.get("/").text
        # Unplugged: a scan no longer sees it.
        away = tmp_path / "usb-away"
        os.replace(usb.root, away)
        libs.refresh()
        assert "Drive lectures" not in client.get("/").text
        # Changed on another computer, then plugged back in.
        os.replace(away, usb.root)
        dst_rel = _interrupt_rename(usb)
        libs.refresh()
        assert "Drive lectures" in client.get("/").text
        assert not usb.rename_journal.exists()
        assert (usb.media_root / dst_rel).is_file()


def test_reseen_library_not_recovered_under_an_active_job(tmp_path):
    app, _home, libs = _app(tmp_path)
    with TestClient(app) as client:
        usb = _usb(tmp_path)
        write_json(libs.registry_path, {"schema": 1, "libraries": [str(usb.root)]})
        libs.refresh()
        client.get("/")
        usb_id = read_library(usb.root).id
        _add_job(app, usb_id)  # e.g. a download publishing into it
        away = tmp_path / "usb-away"
        os.replace(usb.root, away)
        libs.refresh()
        assert usb_id not in {ref.id for ref in libs.libraries()}  # one miss
        os.replace(away, usb.root)
        _interrupt_rename(usb)  # stands in for the job's own live batch
        libs.refresh()
        client.get("/")
        assert usb.rename_journal.exists()  # left to the job that owns it


def _replug_with_interrupted_rename(tmp_path, libs, client) -> LibraryLayout:
    """A stick seen, missed by one scan, then back with a half-done batch;
    the next scan will find it new again."""
    usb = _usb(tmp_path)
    write_json(libs.registry_path, {"schema": 1, "libraries": [str(usb.root)]})
    libs.refresh()
    client.get("/")
    away = tmp_path / "usb-away"
    os.replace(usb.root, away)
    libs.refresh()
    libs.libraries()
    os.replace(away, usb.root)
    _interrupt_rename(usb)
    return usb


def test_reseen_library_recovery_waits_for_publish_lock(tmp_path, monkeypatch):
    app, _home, libs = _app(tmp_path)
    with TestClient(app) as client:
        usb = _replug_with_interrupted_rename(tmp_path, libs, client)
        usb_id = read_library(usb.root).id
        manager = app.state.jobs
        journal_ran = threading.Event()
        library_ran = threading.Event()
        entered = threading.Event()
        real_journal = app_module.recover_rename_journal
        real_library = manager.recover_library

        def journal(layout):
            # Recorded before the real (self-locking) call: only the
            # recovery's own hold on PUBLISH_LOCK may keep this from running.
            journal_ran.set()
            return real_journal(layout)

        def library(ref):
            library_ran.set()
            return real_library(ref)

        monkeypatch.setattr(app_module, "recover_rename_journal", journal)
        monkeypatch.setattr(manager, "recover_library", library)
        hook = libs.on_new
        assert hook is not None

        def on_new(ref):
            entered.set()
            hook(ref)

        libs.on_new = on_new
        holding, release = threading.Event(), threading.Event()

        def edit():  # a web rename in flight
            with publish.PUBLISH_LOCK:
                holding.set()
                release.wait(10)

        listed: list[set[str]] = []
        editor = threading.Thread(target=edit, daemon=True)
        scanner = threading.Thread(
            target=lambda: listed.append(
                {ref.id for ref in libs.libraries(refresh=True)}
            ),
            daemon=True,
        )
        editor.start()
        try:
            assert holding.wait(5)
            scanner.start()
            assert entered.wait(5)
            assert not journal_ran.wait(0.3)
            assert not library_ran.is_set()
            assert usb.rename_journal.exists()
        finally:
            release.set()
        editor.join(5)
        scanner.join(10)
        assert not scanner.is_alive()
        assert journal_ran.is_set() and library_ran.is_set()
        assert not usb.rename_journal.exists()
        assert listed and usb_id in listed[0]


def test_reseen_library_busy_with_job_never_waits_for_publish_lock(tmp_path):
    """A stick a running job uses that misses one scan (a slow probe under
    a long same-device move holding PUBLISH_LOCK) is listed again at once,
    without waiting for the move and without recovering its live batch."""
    app, _home, libs = _app(tmp_path)
    with TestClient(app) as client:
        usb = _replug_with_interrupted_rename(tmp_path, libs, client)
        usb_id = read_library(usb.root).id
        _add_job(app, usb_id)
        holding, release = threading.Event(), threading.Event()
        listed: list[set[str]] = []

        def move():  # a same-device move copying under PUBLISH_LOCK
            with publish.PUBLISH_LOCK:
                holding.set()
                release.wait(10)

        mover = threading.Thread(target=move, daemon=True)
        lister = threading.Thread(
            target=lambda: listed.append(
                {ref.id for ref in libs.libraries(refresh=True)}
            ),
            daemon=True,
        )
        mover.start()
        try:
            assert holding.wait(5)
            lister.start()
            lister.join(5)
            assert not lister.is_alive(), "listing waited for the move"
            assert listed and usb_id in listed[0]
            assert usb.rename_journal.exists()  # the job's own batch
        finally:
            release.set()
            mover.join(5)
            lister.join(5)


def test_reseen_library_under_job_lock_recovered_when_publish_lock_free(
    tmp_path,
):
    app, _home, libs = _app(tmp_path)
    with TestClient(app) as client:
        usb = _replug_with_interrupted_rename(tmp_path, libs, client)
        usb_id = read_library(usb.root).id
        with app.state.jobs._lock:
            assert usb_id in {ref.id for ref in libs.libraries(refresh=True)}
        assert not usb.rename_journal.exists()
        assert usb_id not in libs.recovering()


def test_reseen_library_under_job_lock_deferred_not_deadlocked(tmp_path):
    """A thread holding the job lock that finds a re-seen stick must not
    wait for PUBLISH_LOCK: a move holds PUBLISH_LOCK while it reports
    progress under the job lock."""
    app, _home, libs = _app(tmp_path)
    # No lifespan: on a deadlock its shutdown would wait for the job lock
    # forever, hanging the run instead of failing this test.
    client = TestClient(app)
    usb = _replug_with_interrupted_rename(tmp_path, libs, client)
    usb_id = read_library(usb.root).id
    manager = app.state.jobs
    publisher_has, lister_has = threading.Event(), threading.Event()
    listed: list[set[str]] = []

    def publisher():  # a move: PUBLISH_LOCK, then a progress update
        with publish.PUBLISH_LOCK:
            publisher_has.set()
            lister_has.wait(5)
            with manager._lock:
                pass

    def lister():  # e.g. a resume: the job lock, then a library lookup
        with manager._lock:
            lister_has.set()
            publisher_has.wait(5)
            listed.append({ref.id for ref in libs.libraries(refresh=True)})

    threads = [
        threading.Thread(target=publisher, daemon=True),
        threading.Thread(target=lister, daemon=True),
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 10
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    assert not any(thread.is_alive() for thread in threads), "deadlock"
    # Hidden and not recovered while it could not be done safely ...
    assert listed and usb_id not in listed[0]
    assert usb.rename_journal.exists()
    assert usb_id in libs.recovering()
    # ... and recovered by the next listing.
    assert usb_id in {ref.id for ref in libs.libraries()}
    assert not usb.rename_journal.exists()
    assert usb_id not in libs.recovering()
    manager.close()


def test_late_plugged_library_move_job_reconciled(tmp_path):
    app, home, libs = _app(tmp_path)
    with TestClient(app) as client:
        usb = _usb(tmp_path)
        # A move into this stick, from a laptop library, that was
        # interrupted before publishing.
        staging = usb.meta_root / f"{STAGING_PREFIX}job1"
        staging.mkdir()
        source = tmp_path / "laptop" / "PLMOVING"
        source.mkdir(parents=True)
        write_json(usb.jobs_file, {"schema": 1, "jobs": [{
            "id": "movejob1", "kind": "move", "status": "running",
            "playlist_id": "PLMOVING",
            "library_id": "elsewhere", "source_library_id": "",
            "source_path": str(source),
            "target_path": str(usb.meta_root / "PLMOVING"),
            "temp_path": str(staging),
        }]})
        write_json(libs.registry_path, {"schema": 1, "libraries": [str(usb.root)]})
        libs.refresh()
        job = client.get("/api/jobs/movejob1").json()
        assert job["status"] == "failed"
        assert job["move_phase"] == "staging"
        assert not staging.exists()


# -- durability --------------------------------------------------------------
def test_fsync_dir_called_in_batch(tmp_path, monkeypatch):
    layout = make_library(tmp_path)
    add_playlist(layout, "PL1", "Mix", [("v1", "First"), ("v2", "Second")])
    calls: list[Path] = []
    real = metadata.fsync_dir

    def spy(path):
        calls.append(Path(path))
        real(path)

    monkeypatch.setattr(publish, "fsync_dir", spy)
    folder = layout.media_root / "Mix"
    src = folder / "01 - First.webm"
    dst = folder / "09 - Moved.webm"
    report = publish.SyncReport()
    assert publish._run_batch(layout, [(src, dst)], report)
    assert dst.is_file()
    assert report.renamed == 1
    # Once after phase 1 (src -> tmp) and once after phase 2 (tmp -> dst).
    assert calls.count(folder) >= 2


def test_fsync_dir_tolerates_unsupported_platforms(tmp_path, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise OSError("directories cannot be fsynced here")

    monkeypatch.setattr(os, "fsync", refuse)
    metadata.fsync_dir(tmp_path)  # no exception
    metadata.fsync_dir(tmp_path / "missing")


# -- launch stick unplugged ---------------------------------------------------
def test_launch_library_vanishes_mid_session(tmp_path, monkeypatch):
    monkeypatch.setenv("YTOP_HOME", str(tmp_path / "home" / "lib"))
    app, home, libs = _app(tmp_path, portable=True)
    usb = _usb(tmp_path)
    libs.add_folder(usb.root, "USB stick")
    with TestClient(app) as client:
        page = client.get("/").text
        assert "Laptop shows" in page and "Drive lectures" in page
        assert "Running from Laptop" in page

        shutil.move(str(home.root), str(tmp_path / "unplugged"))
        libs.refresh()
        page_response = client.get("/")
        assert page_response.status_code == 200
        page = page_response.text
        assert "Drive lectures" in page
        assert "Laptop shows" not in page
        assert "Launch stick disconnected" in page
        for path in ("/storage", "/downloads", "/add", "/search?q=x",
                     f"/playlist/{USB_PLAYLIST}"):
            assert client.get(path).status_code == 200, path
        # Quit still works with the stick gone.
        assert client.post("/api/quit").status_code == 200


@pytest.fixture(autouse=True)
def _no_host_sync(monkeypatch):
    """Quit must not flush the whole test machine's page cache."""
    calls: list[bool] = []
    monkeypatch.setattr(app_module, "_sync_filesystems", lambda: calls.append(True))
    return calls


@pytest.fixture(autouse=True)
def _no_stray_threads():
    before = {t.ident for t in threading.enumerate()}
    yield
    stray = [
        t for t in threading.enumerate()
        if t.ident not in before and t.name == "ytop-idle" and t.is_alive()
    ]
    for thread in stray:
        thread.join(1.0)
    assert not [t for t in stray if t.is_alive()]


def test_heartbeat_ignores_visibility_and_quit_text_is_honest():
    js = (app_module.STATIC_DIR / "js" / "lifecycle.js").read_text()
    # R11: a hidden tab is still an open tab, so it keeps the server alive.
    assert "visibilityState" not in js
    assert "setInterval(heartbeat, HEARTBEAT_MS)" in js
    assert 'addEventListener("visibilitychange", heartbeat)' in js
    # Force-quit cancels jobs; re-adding the playlist resumes the partials.
    assert "can be resumed next time" not in js
    assert "add the playlist again" in js


def test_stale_tab_reloads_when_its_token_is_refused(tmp_path):
    """A tab left open from an earlier YTOP run holds that run's CSRF token;
    the new server refuses its heartbeat (403), so the page reloads to get
    the current one instead of failing every action silently."""
    js = (app_module.STATIC_DIR / "js" / "lifecycle.js").read_text()
    assert "response.status !== 403" in js
    assert "window.location.reload()" in js
    assert "mayReloadStaleTab()" in js  # never a reload loop
    # Only a refused token means a stale tab; any other 403 (origin, cross-site) must not reload.
    assert "CSRF token" in js
