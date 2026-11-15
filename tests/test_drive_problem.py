"""make-stick explains a drive that vanished, failed or turned read-only in
plain words instead of a traceback or a bare "Permission denied"."""

from __future__ import annotations

import errno
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ytop import makestick
from ytop.makestick import drive_id, drive_problem


def test_unplugged_drive_says_disconnected(tmp_path: Path) -> None:
    gone = tmp_path / "STICK"
    message = drive_problem(gone, OSError(errno.ENOENT, "No such file"), None)
    assert "STICK was disconnected" in message


def test_remounted_drive_says_disconnected(tmp_path: Path) -> None:
    # The drive came back as a new mount: the device changed under the copy,
    # and the error itself was a misleading "Permission denied".
    target = tmp_path / "STICK"
    target.mkdir()
    before = drive_id(target)
    assert before is not None
    message = drive_problem(target, PermissionError(errno.EACCES, "denied"), before + 1)
    assert "was disconnected" in message
    assert "run the same step again" in message


def test_io_error_on_read_only_drive_asks_for_a_disk_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "STICK"
    target.mkdir()
    monkeypatch.setattr(makestick.os, "access", lambda path, mode: False)
    message = drive_problem(target, OSError(errno.EIO, "Input/output error"), drive_id(target))
    assert "read or write error and the computer made it read-only" in message
    assert "fsck" in message and "chkdsk" in message


def test_read_only_drive(tmp_path: Path) -> None:
    target = tmp_path / "STICK"
    target.mkdir()
    message = drive_problem(target, OSError(errno.EROFS, "Read-only"), drive_id(target))
    assert "STICK is read-only" in message


def test_other_errors_keep_the_system_message(tmp_path: Path) -> None:
    target = tmp_path / "STICK"
    target.mkdir()
    exc = OSError(errno.ENOSPC, "No space left on device")
    assert drive_problem(target, exc, drive_id(target)) == str(exc)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths")
def test_cli_reports_drive_error_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from ytop import cli

    target = tmp_path / "STICK"
    target.mkdir()

    def broken(path, source=None, cleanup=True):
        raise OSError(errno.EIO, "Input/output error", str(path / ".ytop" / "lib"))

    monkeypatch.setattr(makestick, "inspect_target", broken)
    monkeypatch.setattr(makestick, "source_image", lambda image=None: tmp_path)
    monkeypatch.setattr(makestick.os, "access", lambda path, mode: os.F_OK == mode)
    args = SimpleNamespace(
        target=str(target), library=str(tmp_path / "lib"), platforms="all",
        playlists="", upgrade=True, label="", image=None,
    )
    assert cli.cmd_make_stick(args) == 1
    err = capsys.readouterr().err
    assert "STICK reported a read or write error" in err
    assert "Traceback" not in err
