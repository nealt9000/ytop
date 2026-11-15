"""The startup timing line: where a slow start spends its time."""

from __future__ import annotations

import os
import sys
import time

import pytest
from fastapi.testclient import TestClient

from factories import lib_root, library_set
from ytop.startup import BOOT_T0_ENV, StartupClock, from_environment, process_start_time
from ytop.web.app import create_app


class FakeClock:
    def __init__(self, now: float = 100.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_phases_are_the_time_between_marks():
    clock = FakeClock(100.0)
    startup = StartupClock(start=98.0, clock=clock)
    clock.now = 100.5
    startup.mark("loading YTOP")
    clock.now = 101.0
    startup.mark("finding drives")
    assert startup.phases == [("loading YTOP", 2.5), ("finding drives", 0.5)]
    assert startup.summary() == (
        "Started in 3.0 s: loading YTOP 2.5 s, finding drives 0.5 s"
    )


def test_python_phase_comes_from_the_process_start():
    clock = FakeClock(10.0)
    startup = StartupClock(start=9.0, clock=clock, process_start=8.2)
    clock.now = 10.0
    startup.mark("loading YTOP")
    assert startup.summary() == "Started in 1.8 s: Python 0.8 s, loading YTOP 1.0 s"


def test_no_start_means_no_line():
    startup = StartupClock(start=None, clock=FakeClock())
    startup.mark("loading YTOP")
    assert startup.phases == []
    assert startup.summary() is None


def test_from_environment_uses_the_boot_time(monkeypatch):
    monkeypatch.setenv(BOOT_T0_ENV, "1000.0")
    monkeypatch.setattr("ytop.startup.process_start_time", lambda: 999.5)
    startup = from_environment(clock=FakeClock(1001.0))
    startup.mark("loading YTOP")
    assert startup.summary() == "Started in 1.5 s: Python 0.5 s, loading YTOP 1.0 s"


def test_from_environment_ignores_a_garbled_boot_time(monkeypatch):
    monkeypatch.setenv(BOOT_T0_ENV, "not a number")
    monkeypatch.setattr("ytop.startup.process_start_time", lambda: None)
    assert from_environment(clock=FakeClock()).summary() is None


@pytest.mark.skipif(sys.platform not in ("linux", "win32"), reason="no OS start time")
def test_process_start_time_is_recent():
    started = process_start_time()
    assert started is not None
    assert time.time() - 24 * 3600 < started <= time.time() + 1


def test_boot_records_its_start(monkeypatch, tmp_path):
    from ytop.stick import boot

    # setenv first, so teardown restores the variable boot.main writes.
    monkeypatch.setenv(BOOT_T0_ENV, "")
    monkeypatch.delenv(BOOT_T0_ENV)
    before = time.time()
    # No build.json: main stops early, but only after noting when it began.
    assert boot.main(["--home", str(tmp_path), "--runtime-root", str(tmp_path)]) != 0
    assert before - 1 <= float(os.environ[BOOT_T0_ENV]) <= time.time()


def test_the_app_prints_the_line_once_it_is_ready(tmp_path, capsys):
    library = lib_root(tmp_path)
    startup = StartupClock(start=time.time() - 1.0)
    startup.mark("loading YTOP")
    app = create_app(library, libraries=library_set(library), startup=startup)
    assert "Started in" not in capsys.readouterr().out
    with TestClient(app):
        out = capsys.readouterr().out
    names = [name for name, _seconds in startup.phases]
    assert names == [
        "loading YTOP", "finding drives", "opening libraries", "loading jobs",
        "starting the server",
    ]
    assert "Started in " in out
    assert "finding drives" in out and "starting the server" in out


def test_a_missing_btime_never_breaks_a_start(monkeypatch):
    import io

    import ytop.startup as startup_module

    real_open = open

    def fake_open(path, *args, **kwargs):
        if path == "/proc/stat":
            return io.StringIO("cpu  1 2 3\n")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(startup_module.sys, "platform", "linux")
    monkeypatch.setattr("builtins.open", fake_open)
    assert process_start_time() is None


def test_the_line_prints_once_and_never_without_a_clock(tmp_path, capsys):
    library = lib_root(tmp_path)
    plain = create_app(library, libraries=library_set(library))
    with TestClient(plain):
        pass
    assert "Started in" not in capsys.readouterr().out
    startup = StartupClock(start=time.time() - 1.0)
    app = create_app(library, libraries=library_set(library), startup=startup)
    with TestClient(app):
        pass
    with TestClient(app):
        pass
    assert capsys.readouterr().out.count("Started in") == 1
