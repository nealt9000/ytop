"""Small web-UI display fixes (issue #7)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from factories import add_playlist, library_set, make_library
from ytop.web.app import create_app
from ytop.web.jobs import Job

PLAYLIST = "PLDISP00000000000000000000000001"


def _app(tmp_path: Path):
    home = make_library(tmp_path / "home", "Laptop")
    add_playlist(home, PLAYLIST, "Lectures", [("vid1", "Video One")])
    libs = library_set(home)
    return create_app(home.root, libraries=libs), home, libs


def _add_job(app, job: Job) -> Job:
    manager = app.state.jobs
    with manager._lock:
        manager._jobs[job.id] = job
    return job


# -- job progress ------------------------------------------------------------
def test_file_counting_phases_do_not_show_zero_bytes(tmp_path):
    app, _home, libs = _app(tmp_path)
    job = _add_job(app, Job(
        id="stick1", kind="make_stick", status="running", title="Stick",
        library_id=libs.default().id, move_total_bytes=0, move_files=3,
        move_total_files=10, current_title="Checking what changed…",
    ))
    assert job.copy_progress_label == "3/10 files · Checking what changed…"
    with TestClient(app) as client:
        for page in ("/make-stick/stick1", "/downloads"):
            text = client.get(page).text
            assert "0 B of 0 B" not in text, page
            assert "3/10 files" in text, page


def test_copy_progress_keeps_the_byte_pair_while_copying():
    job = Job(id="m", kind="move", status="running", move_bytes=1024,
              move_total_bytes=2048, move_files=1, move_total_files=2)
    assert job.copy_progress_label == "1.0 KiB of 2.0 KiB · 1/2 files"
