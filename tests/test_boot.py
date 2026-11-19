from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from ytop.cli import main
from ytop.runtime import current_platform
from ytop.stick import boot

BOOT = Path(boot.__file__)

STUB = '''
import json, os, sys
def main(argv):
    out = {"argv": list(argv), "path": sys.path[:3], "fullpath": list(sys.path),
           "env": {k: os.environ.get(k) for k in
                   ("YTOP_HOME", "YTOP_RUNTIME", "YTOP_BUILD_ID", "YTOP_IMAGE_ROOT")}}
    open(os.environ["BOOT_OUT"], "w").write(json.dumps(out))
    return 0
'''


def fake_ytop(home: Path, platforms: list[str], with_build_in_home: bool = True) -> Path:
    root = home / ".ytop"
    (root / "app" / "ytop").mkdir(parents=True)
    (root / "app" / "ytop" / "__init__.py").write_text("")
    (root / "app" / "ytop" / "cli.py").write_text(STUB)
    (root / "build.json").write_text(
        json.dumps({"build_id": "b123", "platforms": platforms}))
    shutil.copy(BOOT, root / "boot.py")
    return root


def run_boot(root: Path, tmp_path: Path, *args: str, env_extra=None):
    import os

    env = {k: v for k, v in os.environ.items() if not k.startswith("YTOP_")}
    env["BOOT_OUT"] = str(tmp_path / "out.json")
    env.update(env_extra or {})
    proc = subprocess.run(
        [sys.executable, "-s", "-E", str(root / "boot.py"), *args],
        env=env, capture_output=True, text=True,
    )
    out = tmp_path / "out.json"
    return proc, json.loads(out.read_text()) if out.exists() else None


def test_boot_sets_path_and_env(tmp_path: Path):
    key = current_platform()
    root = fake_ytop(tmp_path, [key])
    proc, out = run_boot(root, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert out["path"] == [str(root / "app"), str(root / "lib" / "common"),
                           str(root / "lib" / key)]
    env = out["env"]
    assert env["YTOP_HOME"] == str(tmp_path)
    assert env["YTOP_RUNTIME"] == str(root / "runtime" / key)
    assert env["YTOP_BUILD_ID"] == "b123"
    assert env["YTOP_IMAGE_ROOT"] == str(root)
    assert out["argv"] == ["serve", "--portable", "--open"]


def test_boot_image_root_falls_back_to_runtime_root(tmp_path: Path):
    key = current_platform()
    root = fake_ytop(tmp_path / "cache", [key])
    home = tmp_path / "stick"
    home.mkdir()
    proc, out = run_boot(root, tmp_path, "--home", str(home))
    assert proc.returncode == 0, proc.stderr
    assert out["env"]["YTOP_HOME"] == str(home)
    assert out["env"]["YTOP_IMAGE_ROOT"] == str(root)


COMPONENTS = ("yt-dlp", "yt-dlp-ejs", "bgutil-plugin", "youtube-transcript-api")


def add_components(home: Path, versions: dict[str, str], current: object) -> None:
    """Version folders on the stick, and ``current.json`` as given (a dict,
    raw bytes, or None for no file)."""
    store = home / ".ytop" / "components"
    for name, version in versions.items():
        (store / name / version).mkdir(parents=True)
        (store / name / version / ".complete").write_text("")
    if isinstance(current, dict):
        (store / "current.json").write_text(json.dumps(
            {"schema": 1, "current": current, "previous": {}, "pinned_bad": {},
             "last_check": "", "last_result": ""}
        ))
    elif isinstance(current, bytes):
        (store / "current.json").write_bytes(current)


def test_boot_puts_current_components_first(tmp_path: Path):
    key = current_platform()
    root = fake_ytop(tmp_path, [key])
    versions = {name: f"1.{i}" for i, name in enumerate(COMPONENTS)}
    add_components(tmp_path, {**versions, "yt-dlp-old": "0.1"}, versions)
    proc, out = run_boot(root, tmp_path)
    assert proc.returncode == 0, proc.stderr
    store = root / "components"
    assert out["fullpath"][:7] == [
        str(root / "app"),
        *(str(store / name / versions[name]) for name in COMPONENTS),
        str(root / "lib" / "common"),
        str(root / "lib" / key),
    ]


def test_boot_reads_components_from_the_stick_not_the_linux_cache(tmp_path: Path):
    """On Linux boot runs from the runtime cache; the components stay on
    the stick (``--home``)."""
    key = current_platform()
    cache = fake_ytop(tmp_path / "cache", [key])
    home = tmp_path / "stick"
    (home / ".ytop").mkdir(parents=True)
    add_components(home, {"yt-dlp": "2026.11.01"}, {"yt-dlp": "2026.11.01"})
    proc, out = run_boot(cache, tmp_path, "--home", str(home))
    assert proc.returncode == 0, proc.stderr
    assert out["fullpath"][:3] == [
        str(cache / "app"),
        str(home / ".ytop/components/yt-dlp/2026.11.01"),
        str(cache / "lib" / "common"),
    ]


@pytest.mark.parametrize(
    "current",
    [None, b"{garbled", b"\xff\xfe", b"[1, 2]", b'{"current": "yt-dlp"}',
     {"yt-dlp": "9.9.9"}, {"yt-dlp": ".."}, {"yt-dlp": 5}],
    ids=["missing", "garbled", "not-utf8", "not-object", "bad-current",
         "absent-folder", "dotdot", "not-a-string"],
)
def test_boot_falls_back_without_current_json(tmp_path: Path, current):
    """No usable entry for a component: its ``lib/common`` copy is used, and
    the others still come from the store."""
    key = current_platform()
    root = fake_ytop(tmp_path, [key])
    good = {"yt-dlp-ejs": "0.9.0"}
    if isinstance(current, dict):
        current = {**current, **good}
    add_components(tmp_path, {"yt-dlp": "1.0", **good}, current)
    proc, out = run_boot(root, tmp_path)
    assert proc.returncode == 0, proc.stderr
    common = out["fullpath"].index(str(root / "lib" / "common"))
    before = out["fullpath"][1:common]
    assert str(root / "components/yt-dlp/1.0") not in out["fullpath"]
    if isinstance(current, dict):
        assert before == [str(root / "components/yt-dlp-ejs/0.9.0")]
    else:
        assert before == []
    assert out["fullpath"][common + 1] == str(root / "lib" / key)


def test_boot_skips_a_component_folder_without_its_marker(tmp_path: Path):
    """A folder an interrupted install or prune left behind (no
    ``.complete``) is never imported: ``lib/common`` is used instead."""
    key = current_platform()
    root = fake_ytop(tmp_path, [key])
    add_components(tmp_path, {"yt-dlp": "2.0", "yt-dlp-ejs": "0.9.0"},
                   {"yt-dlp": "2.0", "yt-dlp-ejs": "0.9.0"})
    (root / "components/yt-dlp/2.0/.complete").unlink()
    proc, out = run_boot(root, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert str(root / "components/yt-dlp/2.0") not in out["fullpath"]
    assert out["fullpath"][1] == str(root / "components/yt-dlp-ejs/0.9.0")


def test_boot_passes_ytop_args(tmp_path: Path):
    root = fake_ytop(tmp_path, [current_platform()])
    proc, out = run_boot(root, tmp_path, "--", "doctor", "--json")
    assert proc.returncode == 0, proc.stderr
    assert out["argv"] == ["doctor", "--json"]


def test_boot_missing_platform_exit_3(tmp_path: Path):
    root = fake_ytop(tmp_path, ["some-other-platform"])
    proc, out = run_boot(root, tmp_path)
    assert proc.returncode == 3
    assert out is None
    assert "This YTOP stick has no runtime for" in proc.stderr
    assert "Make the stick again with that platform included." in proc.stderr


def test_boot_no_browser_env(tmp_path: Path):
    root = fake_ytop(tmp_path, [current_platform()])
    proc, out = run_boot(root, tmp_path, env_extra={"YTOP_NO_BROWSER": "1"})
    assert proc.returncode == 0, proc.stderr
    assert out["argv"] == ["serve", "--portable", "--no-open"]


def _portable_env(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "lib"
    main(["init", str(root)])
    monkeypatch.setenv("YTOP_HOME", str(root))
    return root


def test_portable_port_fallback(tmp_path: Path, monkeypatch, capsys):
    _portable_env(tmp_path, monkeypatch)
    seen = {}
    monkeypatch.setattr(
        "ytop.web.app._run_server",
        lambda app, host, port, sock=None: seen.update(host=host, port=port),
    )
    with socket.socket() as blocker:
        # REUSEADDR like the app's own bind, so a TIME_WAIT entry cannot
        # make this blocker fail while the app still gets 8765.
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            blocker.bind(("127.0.0.1", 8765))
            blocker.listen()
        except OSError:
            # Only acceptable when a real listener (a running YTOP) holds it.
            with socket.create_connection(("127.0.0.1", 8765), timeout=2):
                pass
        code = main(["serve", "--portable", "--no-open"])
    assert code == 0
    assert 8766 <= seen["port"] < 8800
    assert seen["host"] == "127.0.0.1"
    out = capsys.readouterr().out
    assert (f"YTOP is running at http://127.0.0.1:{seen['port']}/  "
            "(close this window or use Quit in the app to stop)") in out


def _flush_probe(monkeypatch):
    """Swap stdout for a block-buffered pipe-like stream; return a dict that
    records what had reached the underlying bytes when the server started."""
    import io

    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="utf-8", write_through=False,
                              line_buffering=False)
    monkeypatch.setattr(sys, "stdout", stream)
    seen: dict = {}

    def fake_run(app, host, port, sock=None):
        seen["bytes"] = raw.getvalue().decode()

    monkeypatch.setattr("ytop.web.app._run_server", fake_run)
    return seen


@pytest.mark.parametrize("portable", [True, False])
def test_startup_lines_flushed_before_server_starts(
        tmp_path: Path, monkeypatch, portable):
    from ytop.web.app import serve

    root = _portable_env(tmp_path, monkeypatch)
    seen = _flush_probe(monkeypatch)
    serve(root, portable=portable, open_browser=False)
    if portable:
        assert "YTOP is running at http://127.0.0.1:" in seen["bytes"]
    else:
        assert "ytop web UI: http://" in seen["bytes"]
        assert "library:" in seen["bytes"]


def test_portable_on_bound_callback(tmp_path: Path, monkeypatch):
    from ytop.web.app import serve

    root = tmp_path / "lib"
    main(["init", str(root)])
    monkeypatch.setattr("ytop.web.app._run_server", lambda *a, **kw: None)
    ports: list[int] = []
    serve(root, portable=True, open_browser=False, on_bound=ports.append)
    assert len(ports) == 1 and ports[0] >= 1


def test_portable_rejects_host(tmp_path: Path, monkeypatch, capsys):
    _portable_env(tmp_path, monkeypatch)
    monkeypatch.setattr("ytop.web.app._run_server",
                        lambda *a, **kw: pytest.fail("ran"))
    code = main(["serve", "--portable", "--host", "127.0.0.1", "--no-open"])
    assert code == 2
    assert "--host" in capsys.readouterr().err


def test_portable_serve_pins_deno_dir_to_launch_library(tmp_path: Path, monkeypatch):
    import os

    from ytop.web.app import serve

    root = tmp_path / "lib"
    main(["init", str(root)])
    monkeypatch.setenv("DENO_DIR", str(tmp_path / "host-cache"))
    seen = {}
    monkeypatch.setattr(
        "ytop.web.app._run_server",
        lambda *a, **kw: seen.update(deno=os.environ.get("DENO_DIR")),
    )
    serve(root, portable=True, open_browser=False)
    assert Path(seen["deno"]) == root / ".ytop" / "cache" / "deno"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX terminals send SIGHUP")
def test_portable_sighup_quits_gracefully(tmp_path: Path, monkeypatch):
    import os
    import signal
    import time

    from ytop.web.app import serve

    root = tmp_path / "lib"
    main(["init", str(root)])
    before = signal.getsignal(signal.SIGHUP)
    seen: dict = {}

    def fake_run(app, host, port, sock=None):
        stops: list[int] = []
        app.state.request_stop = lambda: stops.append(1)
        os.kill(os.getpid(), signal.SIGHUP)  # the console window closed
        deadline = time.monotonic() + 5
        while not stops and time.monotonic() < deadline:
            time.sleep(0.01)
        seen.update(stops=stops, stopping=app.state.stopping,
                    closed=app.state.jobs._closed)

    monkeypatch.setattr("ytop.web.app._run_server", fake_run)
    monkeypatch.setattr("ytop.web.app._sync_filesystems", lambda: None)
    serve(root, portable=True, open_browser=False)
    assert seen == {"stops": [1], "stopping": True, "closed": True}
    # The handler is removed again once the server is gone.
    assert signal.getsignal(signal.SIGHUP) == before


@pytest.mark.parametrize("content", [None, "{not json", "[]", b"\xff\xfe\x00"])
def test_boot_damaged_build_json_exit_2(tmp_path: Path, content):
    root = fake_ytop(tmp_path, [current_platform()])
    build = root / "build.json"
    if content is None:
        build.unlink()
    elif isinstance(content, bytes):
        build.write_bytes(content)
    else:
        build.write_text(content)
    proc, out = run_boot(root, tmp_path)
    assert proc.returncode == 2
    assert out is None
    assert proc.stderr.strip() == (
        "This YTOP stick is damaged: .ytop/build.json is missing or "
        "unreadable. Make the stick again."
    )
    assert "Traceback" not in proc.stderr


# --- a launch repairs an interrupted upgrade ----------------------------------

REPAIR_PLATS = ["linux-x86_64", "macos-arm64", "windows-x86_64"]
REPAIRED = (
    "YTOP's last upgrade on this stick was interrupted, so the previous "
    "version was put back. Start YTOP again."
)
NOT_UNDONE = (
    "YTOP's last upgrade on this stick was interrupted and couldn't be "
    "undone here. Open YTOP from another stick and make or upgrade this one "
    "again."
)


def interrupted_stick(tmp_path: Path, monkeypatch) -> tuple[Path, dict[str, str]]:
    """A stick whose upgrade crashed just before the new app went in."""
    import ytop.makestick as ms
    from stick_fakes import make_fake_image
    from test_makestick import (
        SWITCH_RENAMES,
        Crash,
        _crash_scenario,
        _owned_state,
        home_library,
        refresh_manifest,
    )

    _home, _libs, library = home_library(tmp_path)
    image = make_fake_image(tmp_path / "img", REPAIR_PLATS)
    shutil.copy(BOOT, image / "boot.py")  # the real one: launchers run it
    refresh_manifest(image, REPAIR_PLATS)
    target, newer = _crash_scenario(tmp_path, image, library)
    before = _owned_state(target)
    real_place = ms._place
    calls = {"n": 0}

    def crashing(src: Path, dst: Path) -> None:
        calls["n"] += 1
        if calls["n"] == SWITCH_RENAMES:
            raise Crash()
        real_place(src, dst)

    monkeypatch.setattr(ms, "_place", crashing)
    plan = ms.plan_make(target, newer, library, platforms=REPAIR_PLATS)
    with pytest.raises(Crash):
        ms.run_make(plan, newer, library, lambda update: None)
    monkeypatch.setattr(ms, "_place", real_place)
    assert boot.switch_interrupted(target)
    return target, before


def test_boot_rolls_back_an_interrupted_upgrade(tmp_path: Path, monkeypatch, capsys):
    from test_makestick import _owned_state

    target, before = interrupted_stick(tmp_path, monkeypatch)
    assert boot.main(["--home", str(target)]) == boot.EXIT_REPAIRED == 5
    assert REPAIRED in capsys.readouterr().err
    assert _owned_state(target) == before
    assert not boot.switch_interrupted(target)


def test_boot_repair_invalidates_the_linux_cache(tmp_path: Path, monkeypatch, capsys):
    target, _before = interrupted_stick(tmp_path, monkeypatch)
    cache = tmp_path / "cache" / "b123"
    cache.mkdir(parents=True)
    (cache / ".complete").write_text("")
    (cache / "keep.txt").write_text("x")
    rc = boot.main(["--home", str(target), "--runtime-root", str(cache)])
    assert rc == 5
    assert not (cache / ".complete").exists()
    assert (cache / "keep.txt").exists()


def test_boot_repair_leaves_the_stick_runtime_complete_marker_alone(
    tmp_path: Path, monkeypatch
):
    """runtime_root == home/.ytop is the stick itself, not a cache."""
    target, _before = interrupted_stick(tmp_path, monkeypatch)
    marker = target / ".ytop" / ".complete"
    marker.write_text("")
    assert boot.main(["--home", str(target)]) == 5
    assert marker.exists()


def test_boot_reports_a_rollback_that_fails(tmp_path: Path, monkeypatch, capsys):
    target, _before = interrupted_stick(tmp_path, monkeypatch)

    def broken(_target: Path) -> bool:
        raise OSError("read-only")

    monkeypatch.setattr(boot, "rollback_upgrade", broken)
    assert boot.main(["--home", str(target)]) == 5
    assert NOT_UNDONE in capsys.readouterr().err


def test_boot_does_not_claim_a_journal_less_switch_was_undone(
    tmp_path: Path, capsys
):
    """A whole-piece swap (no journal) is only restored by make-stick."""
    stick = tmp_path / "stick"
    (stick / ".ytop").mkdir(parents=True)
    (stick / ".ytop.old").mkdir()
    (stick / ".ytop.old" / "something").write_text("x")
    assert boot.main(["--home", str(stick)]) == 5
    err = capsys.readouterr().err
    assert NOT_UNDONE in err
    assert "put back" not in err
    assert (stick / ".ytop.old" / "something").exists()


def test_boot_rolls_back_with_a_journal_that_is_not_utf8(
    tmp_path: Path, monkeypatch, capsys
):
    """Failing FAT media can garble the journal: still no traceback, and the
    names it can read are still undone."""
    from test_makestick import _owned_state

    target, before = interrupted_stick(tmp_path, monkeypatch)
    journal = target / boot.OLD / boot.JOURNAL
    journal.write_bytes(b"lib/\xff\xfe-garbled.py\n" + journal.read_bytes())
    assert boot.main(["--home", str(target)]) == 5
    err = capsys.readouterr().err
    assert REPAIRED in err and "Traceback" not in err
    assert _owned_state(target) == before
    owned = [
        p for part in ("app", "lib", "runtime")
        for p in (target / ".ytop" / part).rglob("*") if p.is_dir()
    ]
    assert [p for p in owned if not any(p.iterdir())] == []


def test_boot_names_a_journal_entry_with_undecodable_bytes(tmp_path: Path):
    """A file whose name is not UTF-8 is matched by its exact bytes."""
    if os.name == "nt":
        pytest.skip("Windows names are Unicode")
    target = tmp_path / "stick"
    yt = target / ".ytop"
    (yt / "lib/common").mkdir(parents=True)
    (yt / "lib/common/kept.py").write_text("kept\n")
    raw = b"lib/common/odd-\xff.py"
    with open(os.fsencode(yt) + b"/" + raw, "wb") as handle:
        handle.write(b"added\n")
    (target / boot.OLD / "app").mkdir(parents=True)
    (target / boot.OLD / boot.JOURNAL).write_bytes(raw + b"\n")
    assert boot.rollback_upgrade(target)
    assert os.listdir(os.fsencode(yt / "lib/common")) == [b"kept.py"]


def test_boot_rollback_never_touches_folders_outside_ytop(tmp_path: Path):
    target = tmp_path / "stick"
    (target / ".ytop/lib/common").mkdir(parents=True)
    (target / "Empty Folder").mkdir()
    (target / boot.OLD / "app").mkdir(parents=True)
    (target / boot.OLD / boot.JOURNAL).write_text(
        "../Empty Folder/x.py\nlib/../../Empty Folder/y.py\n"
    )
    assert boot.rollback_upgrade(target)
    assert (target / "Empty Folder").is_dir()


def test_boot_reports_a_journal_it_cannot_parse(tmp_path: Path, monkeypatch, capsys):
    target, _before = interrupted_stick(tmp_path, monkeypatch)

    def garbled(_target: Path) -> bool:
        raise ValueError("bad journal")

    monkeypatch.setattr(boot, "rollback_upgrade", garbled)
    assert boot.main(["--home", str(target)]) == 5
    err = capsys.readouterr().err
    assert NOT_UNDONE in err and "Traceback" not in err


# --- never under sudo (#9) -------------------------------------------------------


def _as_root(monkeypatch, sudo: bool) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    if sudo:
        monkeypatch.setenv("SUDO_UID", "1000")
    else:
        monkeypatch.delenv("SUDO_UID", raising=False)


def test_boot_refuses_to_run_under_sudo_before_touching_the_stick(
    tmp_path: Path, monkeypatch, capsys,
):
    """Root from sudo would leave root-owned files the user's own YTOP
    cannot change, and would not see the user's running YTOP: refused,
    before even a repair."""
    _as_root(monkeypatch, sudo=True)
    touched: list[Path] = []
    monkeypatch.setattr(boot, "repair_interrupted_upgrade",
                        lambda home, root: touched.append(home) or False)
    assert boot.main(["--home", str(tmp_path)]) == boot.EXIT_SUDO
    assert touched == []
    assert "sudo" in capsys.readouterr().err


def test_boot_runs_as_plain_root(tmp_path: Path, monkeypatch):
    """A root login (a container, say) is one user throughout: allowed."""
    _as_root(monkeypatch, sudo=False)
    assert boot.main(["--home", str(tmp_path), "--runtime-root", str(tmp_path)]) \
        == boot.EXIT_DAMAGED  # got past the check (no build.json here)
