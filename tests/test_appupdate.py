"""App updates: opt-in, confirmed, incremental (tmp dirs stand in for sticks,
a fake GitHub on 127.0.0.1 for the network)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import ytop.makestick as ms
from factories import library_set, make_library
from fake_releases import FakeReleases
from stick_fakes import REPO, make_fake_image
from ytop import appupdate, updater
from ytop.autoupdate import AutoUpdate
from ytop.config import Config, UpdatesConfig, load_config
from ytop.manifest import (
    MANIFEST_NAME,
    for_platforms,
    hash_tree,
    read_manifest,
    write_manifest,
)
from ytop.stick import boot
from ytop.web.app import create_app
from ytop.web.library import Library

PLATS = ["linux-x86_64", "windows-x86_64"]
OLD_ID = "202601010000-old0000"
NEW_ID = "209912312359-newer00"
GH_REPO = "someone/ytop"
STAGE = boot.MAKING_SRC
CMD = "Start YTOP (Windows).cmd"


class Crash(BaseException):
    """Stands in for a power cut / unplug: no ``except Exception`` runs."""


def _package_module():
    spec = importlib.util.spec_from_file_location(
        "package_release", REPO / "tools" / "package_release.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["package_release"] = module
    spec.loader.exec_module(module)
    return module


def _set_build(yt: Path, **fields) -> None:
    build = json.loads((yt / "build.json").read_text())
    build.update(fields)
    (yt / "build.json").write_text(json.dumps(build, indent=2) + "\n")


def _refresh_manifest(yt: Path, platforms=PLATS) -> None:
    rels = [rel for rel, _size in ms.image_files(yt, platforms)]
    write_manifest(yt / MANIFEST_NAME, hash_tree(yt, rels))
    _set_build(yt, manifest_sha256=hashlib.sha256((yt / MANIFEST_NAME).read_bytes()).hexdigest())


def _make_stick(image: Path, target: Path, home: Path, platforms: list[str]) -> Path:
    target.mkdir(parents=True)
    library = Library(library_set(make_library(home, "Laptop")))
    plan = ms.plan_make(target, image, library, platforms=platforms, label="Stick")
    ms.run_make(plan, image, library, lambda update: None)
    return target


@pytest.fixture(scope="module")
def builds(tmp_path_factory):
    base = tmp_path_factory.mktemp("builds")
    old = make_fake_image(base / "old", PLATS)
    _set_build(old, version="0.0.9", build_id=OLD_ID)
    new = make_fake_image(base / "new", PLATS)
    # Changes in the app, a Windows lib and the Windows Python (in use while
    # YTOP runs there); everything else is the same build.
    (new / "app/ytop/NEW_APP").write_text("new\n")
    (new / "lib/windows-x86_64/nativepkg/_native.txt").write_text("rebuilt\n")
    (new / "runtime/windows-x86_64/python/python.exe").write_bytes(b"MZ new python")
    with open(new / "app/ytop/stick/launchers" / CMD, "ab") as handle:
        handle.write(b"rem newer launcher\r\n")
    _set_build(new, build_id=NEW_ID)
    _refresh_manifest(new)
    pkg = _package_module()
    release = base / "release"
    pkg.package(new, release)
    # The same build, but it needs a newer yt-dlp than any stick has.
    needy_image = base / "needy"
    shutil.copytree(new.parent, needy_image)
    current = needy_image / ".ytop/components/current.json"
    doc = json.loads(current.read_text())
    doc["current"]["yt-dlp"] = "2099.1.1"
    current.write_text(json.dumps(doc))
    needy = base / "release-needy"
    pkg.package(needy_image / ".ytop", needy)
    import ytop.libraries as libraries

    registry = libraries.registry_path_default()  # as the sticks below see it
    stick = _make_stick(old, base / "stick", base / "home", PLATS)
    linux_stick = _make_stick(old, base / "linux-stick", base / "home2", ["linux-x86_64"])
    return SimpleNamespace(
        old=old, new=new, release=release, needy=needy, stick=stick,
        linux_stick=linux_stick, registry=registry,
    )


@pytest.fixture
def stick(builds, tmp_path) -> Path:
    target = tmp_path / "stick"
    shutil.copytree(builds.stick, target)
    return target


def _serve(fake: FakeReleases, folder: Path, *, notes: str = "Faster pages.",
           edit: dict[str, bytes] | None = None) -> None:
    assets = {p.name: p.read_bytes() for p in sorted(folder.iterdir())}
    assets.update(edit or {})
    fake.app_release(GH_REPO, "v0.1.0", assets, notes=notes)


@pytest.fixture
def server(builds):
    fake = FakeReleases()
    _serve(fake, builds.release)
    yield fake
    fake.close()


def _config(fake: FakeReleases | None, **updates) -> Config:
    base = fake.base + "/api" if fake is not None else "http://127.0.0.1:9/api"
    return Config(updates=UpdatesConfig(repo=GH_REPO, api_base=base, **updates))


def _owned_state(target: Path) -> dict[str, str]:
    yt = target / ".ytop"
    state = hash_tree(yt, sorted(ms._target_files(yt)))
    for name in ("build.json", MANIFEST_NAME):
        if (yt / name).is_file():
            state[name] = hashlib.sha256((yt / name).read_bytes()).hexdigest()
    return state


def _downloads(fake: FakeReleases) -> list[str]:
    return sorted(
        r.path.rsplit("/", 1)[-1] for r in fake.requests if r.path.startswith("/dl/")
    )


def _install(stick: Path, fake: FakeReleases, release=None, **kwargs):
    config = _config(fake)
    if release is None:
        release = appupdate.check_app(stick / ".ytop", config, updater.Http())
        assert release is not None
    kwargs.setdefault("before_switch", lambda: None)
    kwargs.setdefault("update_components", lambda names: pytest.fail(f"updated {names}"))
    return appupdate.install_app(stick / ".ytop", config, release, http=updater.Http(), **kwargs)


# --- checking ------------------------------------------------------------------------


def test_check_offers_only_newer_verified_releases(stick: Path, builds, server):
    yt = stick / ".ytop"
    found = appupdate.check_app(yt, _config(server), updater.Http())
    assert found is not None
    assert (found.version, found.build_id, found.notes) == ("0.1.0", NEW_ID, "Faster pages.")
    # Only the small files are fetched to check: never a piece.
    assert _downloads(server) == ["SHA256SUMS", "ytop-release.json"]

    # A tampered ytop-release.json (not what SHA256SUMS lists) offers nothing.
    doc = json.loads((builds.release / "ytop-release.json").read_text())
    doc["build_id"] = "209912312359-evil000"
    tampered = FakeReleases()
    try:
        _serve(tampered, builds.release, edit={"ytop-release.json": json.dumps(doc).encode()})
        result = appupdate.check_app_result(yt, _config(tampered), updater.Http())
        assert result.release is None and result.status == "error"
        assert appupdate.check_app(yt, _config(tampered), updater.Http()) is None
        # Not listed in SHA256SUMS at all: nothing offered either.
        _serve(tampered, builds.release, edit={"SHA256SUMS": b"0" * 64 + b"  other\n"})
        assert appupdate.check_app(yt, _config(tampered), updater.Http()) is None
    finally:
        tampered.close()

    # The same build, or an older version, is not offered.
    _set_build(yt, build_id=NEW_ID)
    assert appupdate.check_app(yt, _config(server), updater.Http()) is None
    _set_build(yt, build_id=OLD_ID, version="9.9.9")
    assert appupdate.check_app(yt, _config(server), updater.Http()) is None
    _set_build(yt, version="0.0.9")
    assert appupdate.check_app(yt, _config(server), updater.Http()) is not None

    # No network: "offline", quietly.
    offline = appupdate.check_app_result(yt, _config(None), updater.Http())
    assert offline.status == "offline" and offline.release is None


def test_check_never_offers_a_release_missing_the_sticks_platforms(
    stick: Path, builds, server, tmp_path: Path,
):
    _set_build(stick / ".ytop", platforms=[*PLATS, "macos-arm64"])
    assert appupdate.check_app(stick / ".ytop", _config(server), updater.Http()) is None


# --- installing ----------------------------------------------------------------------


def test_install_downloads_only_changed_pieces(stick: Path, builds, server, tmp_path: Path):
    yt = stick / ".ytop"
    library_before = (yt / "library.json").read_bytes()
    unchanged = [
        rel for rel in sorted(ms._target_files(yt))
        if rel.startswith(("lib/common/", "runtime/linux-x86_64/"))
    ]
    stamps = {rel: (os.stat(yt / rel).st_ino, os.stat(yt / rel).st_mtime_ns) for rel in unchanged}
    server.requests.clear()
    result = _install(stick, server)
    assert result.status == "updated", result.message
    assert result.message == "YTOP was updated to 0.1.0. Start it again."
    assert _downloads(server) == sorted([
        "SHA256SUMS", "ytop-release.json", "app.tar.gz",
        "lib-windows-x86_64.tar.gz", "runtime-windows-x86_64-python.tar.gz",
    ])
    # Unchanged files were never written; the stick is the new build.
    assert {
        rel: (os.stat(yt / rel).st_ino, os.stat(yt / rel).st_mtime_ns) for rel in unchanged
    } == stamps
    wanted = for_platforms(read_manifest(builds.new / MANIFEST_NAME), PLATS)
    assert hash_tree(yt, sorted(ms._target_files(yt))) == wanted
    assert read_manifest(yt / MANIFEST_NAME) == wanted
    assert json.loads((yt / "build.json").read_text())["build_id"] == NEW_ID
    assert (yt / "library.json").read_bytes() == library_before
    assert ms.read_trusted_manifest(yt) == wanted
    for leftover in (STAGE, ms.MAKING, ms.OLD):
        assert not (stick / leftover).exists(), leftover

    # A stick without Windows never fetches Windows pieces.
    linux = tmp_path / "linux"
    shutil.copytree(builds.linux_stick, linux)
    server.requests.clear()
    assert _install(linux, server).status == "updated"
    assert _downloads(server) == ["SHA256SUMS", "app.tar.gz", "ytop-release.json"]
    assert not (linux / ".ytop/runtime/windows-x86_64").exists()


def test_install_refuses_a_piece_that_fails_its_checksum(stick: Path, builds):
    before = _owned_state(stick)
    fake = FakeReleases()
    try:
        app = (builds.release / "app.tar.gz").read_bytes()
        _serve(fake, builds.release, edit={"app.tar.gz": app[:-10] + b"0123456789"})
        called = []
        result = _install(stick, fake, before_switch=lambda: called.append(1))
    finally:
        fake.close()
    assert result.status == "failed" and not result.stopped
    assert "app.tar.gz" in result.message
    assert called == []
    assert _owned_state(stick) == before
    assert not (stick / STAGE).exists()


def _dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", ""])
    child.wait()
    return child.pid


def _stale_lock(stick: Path) -> None:
    (stick / ms.LOCK).write_text(
        json.dumps({"pid": _dead_pid(), "started": None})
    )


def _live_lock(stick: Path) -> None:
    (stick / ms.LOCK).write_text(json.dumps(boot.lock_payload()))


def test_untrusted_manifest_stick_still_updates(stick: Path, server):
    """A stick whose file list it cannot trust is hashed, and only the
    pieces that really changed are fetched and switched."""
    _set_build(stick / ".ytop", manifest_sha256="0" * 64)
    assert ms.read_trusted_manifest(stick / ".ytop") is None
    server.requests.clear()
    result = _install(stick, server)
    assert result.status == "updated", result.message
    assert "app.tar.gz" in _downloads(server)
    assert "runtime-linux-x86_64-bin.tar.gz" not in _downloads(server)
    assert ms.read_trusted_manifest(stick / ".ytop") is not None


def test_a_piece_unlike_its_manifest_fails_before_anything_stops(
    stick: Path, builds, tmp_path: Path,
):
    """The archive matches its listed sha256, but a file in it is not the
    one the manifest names: refused while the app still runs."""
    pkg = _package_module()
    release = tmp_path / "rel"
    shutil.copytree(builds.release, release)
    doc = json.loads((release / "ytop-release.json").read_text())
    piece = doc["pieces"]["app"]
    src = tmp_path / "src"
    for rel in piece["files"]:
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(builds.new / rel, src / rel)
    (src / "app/ytop/NEW_APP").write_text("not what the manifest says\n")
    pkg.write_archive(release / "app.tar.gz", src, piece["files"])
    piece["sha256"] = hashlib.sha256((release / "app.tar.gz").read_bytes()).hexdigest()
    piece["size"] = (release / "app.tar.gz").stat().st_size
    (release / "ytop-release.json").write_text(json.dumps(doc))
    names = sorted(p.name for p in release.iterdir() if p.name != "SHA256SUMS")
    (release / "SHA256SUMS").write_text("".join(
        f"{hashlib.sha256((release / n).read_bytes()).hexdigest()}  {n}\n" for n in names
    ))
    before = _owned_state(stick)
    fake = FakeReleases()
    stopped: list[int] = []
    try:
        _serve(fake, release)
        result = _install(stick, fake, before_switch=lambda: stopped.append(1))
    finally:
        fake.close()
    assert result.status == "failed" and not result.stopped and stopped == []
    assert "NEW_APP" in result.message
    assert _owned_state(stick) == before
    assert not (stick / STAGE).exists() and not (stick / ms.LOCK).exists()


def test_boot_never_repairs_a_switch_a_live_process_is_making(stick: Path, capsys):
    old = stick / ms.OLD
    old.mkdir()
    (old / boot.JOURNAL).write_text("")
    os.rename(stick / ".ytop/app", old / "app")
    _live_lock(stick)
    before = sorted(p.relative_to(stick).as_posix() for p in stick.rglob("*"))
    root = (stick / ".ytop").resolve()
    assert boot.repair_interrupted_upgrade(stick, root) is True
    assert boot.UPDATING in capsys.readouterr().err
    assert sorted(p.relative_to(stick).as_posix() for p in stick.rglob("*")) == before
    assert boot.main(["--home", str(stick), "--runtime-root", str(root)]) == boot.EXIT_REPAIRED
    assert (old / "app").is_dir()
    # A lock left by a process that died (or too old to be a switch) is not.
    _stale_lock(stick)
    assert boot.repair_interrupted_upgrade(stick, root) is True
    assert (stick / ".ytop/app").is_dir() and not old.exists()
    boot.clear_leftovers(stick)
    assert not (stick / ms.LOCK).exists()


def test_boot_repairs_under_a_live_lock_too_old_for_a_switch(stick: Path):
    old = stick / ms.OLD
    old.mkdir()
    (old / boot.JOURNAL).write_text("")
    os.rename(stick / ".ytop/app", old / "app")
    _live_lock(stick)
    long_ago = time.time() - boot.SWITCH_LOCK_MAX_AGE_S - 60
    os.utime(stick / ms.LOCK, (long_ago, long_ago))
    assert boot.repair_interrupted_upgrade(stick, (stick / ".ytop").resolve()) is True
    assert (stick / ".ytop/app").is_dir()


def _rename_count(stick: Path, server, monkeypatch) -> int:
    real_place = ms._place
    calls: list[tuple[Path, Path]] = []

    def counting(src: Path, dst: Path) -> None:
        calls.append((src, dst))
        real_place(src, dst)

    monkeypatch.setattr(ms, "_place", counting)
    assert _install(stick, server).status == "updated"
    monkeypatch.setattr(ms, "_place", real_place)
    assert calls[0] == (stick / ".ytop/app", stick / ms.OLD / "app")
    # app in is the commit point; the rollback copy's mark is the last rename.
    assert calls[1 + 2 + 2 + 4][1] == stick / ".ytop/app"
    assert calls[-1][1] == stick / ".ytop" / ms.PREVIOUS / ms.PREVIOUS_MARK
    return len(calls)


def test_install_applies_through_the_journalled_switch(
    builds, server, tmp_path: Path, monkeypatch,
):
    template = tmp_path / "template"
    shutil.copytree(builds.stick, template)
    counted = tmp_path / "counted"
    shutil.copytree(template, counted)
    renames = _rename_count(counted, server, monkeypatch)
    # app out, two files aside, two in, build.json + manifest backed up and
    # replaced, app in: the commit point.  Then the rollback copy: three
    # replaced files, the manifest, build.json and its mark.
    commit = 1 + 2 + 2 + 4 + 1
    assert renames == commit + 3 + 2 + 1
    before = _owned_state(template)
    after = _owned_state(counted)
    library_before = (template / ".ytop/library.json").read_bytes()
    real_place = ms._place
    for crash_at in range(1, renames + 1):
        stick = tmp_path / f"stick-{crash_at}"
        shutil.copytree(template, stick)
        calls = {"n": 0}

        def crashing(src: Path, dst: Path) -> None:
            calls["n"] += 1
            if calls["n"] == crash_at:
                raise Crash()
            real_place(src, dst)

        monkeypatch.setattr(ms, "_place", crashing)
        with pytest.raises(Crash):
            _install(stick, server)
        monkeypatch.setattr(ms, "_place", real_place)
        # A real power cut runs no ``finally``: the lock stays, its process gone.
        _stale_lock(stick)
        interrupted = 1 < crash_at <= commit
        assert boot.switch_interrupted(stick) == interrupted
        # The next start: boot.py repairs, then clears the leftovers.
        home = stick
        assert boot.repair_interrupted_upgrade(home, (home / ".ytop").resolve()) == interrupted
        boot.clear_leftovers(home)
        if crash_at <= commit:
            assert _owned_state(stick) == before, crash_at
        else:  # after the commit: the update is in; a crash before the
            # rollback copy's mark leaves none offered
            assert _owned_state(stick) == after, crash_at
            assert appupdate.previous_version(stick / ".ytop") is None, crash_at
        assert (stick / ".ytop/library.json").read_bytes() == library_before
        for leftover in (STAGE, ms.MAKING, ms.OLD, ms.LOCK):
            assert not (stick / leftover).exists(), (crash_at, leftover)
        # And the update can still be installed afterwards.
        if crash_at == commit:
            assert _install(stick, server).status == "updated"


def test_install_error_in_the_switch_is_undone_and_says_so(
    stick: Path, server, monkeypatch,
):
    before = _owned_state(stick)
    real_place = ms._place
    calls = {"n": 0}

    def failing(src: Path, dst: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 4:
            raise OSError("the stick went away")
        real_place(src, dst)

    monkeypatch.setattr(ms, "_place", failing)
    stopped = []
    result = _install(stick, server, before_switch=lambda: stopped.append(1))
    assert result.status == "undone" and result.stopped and stopped == [1]
    assert "undone" in result.message and "Start it again" in result.message
    assert _owned_state(stick) == before
    assert not boot.switch_interrupted(stick)


def test_install_updates_required_components_first(
    stick: Path, builds, monkeypatch,
):
    fake = FakeReleases()
    order: list[str] = []
    have = {"yt-dlp": "2026.10.26"}
    real_installed = updater.installed_versions

    def installed(ytop: Path) -> dict[str, str]:
        return {**real_installed(ytop), **have}

    monkeypatch.setattr(updater, "installed_versions", installed)

    def update(names: list[str]):
        order.append(f"update {','.join(names)}")
        # The staged update is under the stick's lock: a second start of the
        # stick leaves it alone.
        assert boot.lock_live(stick)
        assert socket.gethostname() not in (stick / ms.LOCK).read_text()
        assert "host" not in json.loads((stick / ms.LOCK).read_text())
        boot.clear_leftovers(stick)
        assert (stick / STAGE / "app/ytop/NEW_APP").is_file()
        have["yt-dlp"] = "2099.1.2"
        return updater.UpdateResult("updated")

    try:
        _serve(fake, builds.needy)
        result = _install(
            stick, fake, update_components=update,
            before_switch=lambda: order.append("stop"),
        )
        assert result.status == "updated", result.message
        assert order == ["update yt-dlp", "stop"]

        # The update could not bring yt-dlp up to what the release needs:
        # nothing is switched and nothing stops.
        again = Path(str(stick) + "-2")
        shutil.copytree(builds.stick, again)
        before = _owned_state(again)
        have["yt-dlp"] = "2026.10.26"
        order.clear()

        def failing(names: list[str]):
            order.append("update")
            return updater.UpdateResult("offline", message="no network")

        result = _install(
            again, fake, update_components=failing,
            before_switch=lambda: order.append("stop"),
        )
    finally:
        fake.close()
    assert result.status == "failed" and not result.stopped
    assert "yt-dlp" in result.message
    assert order == ["update"]
    assert _owned_state(again) == before
    assert not (again / STAGE).exists()


def test_install_stops_when_jobs_cannot_stop(stick: Path, server):
    before = _owned_state(stick)

    def busy() -> None:
        raise appupdate.InstallAborted("A download is running.")

    result = _install(stick, server, before_switch=busy)
    assert result.status == "failed" and not result.stopped
    assert result.message == "A download is running."
    assert _owned_state(stick) == before
    assert not (stick / STAGE).exists()


# --- self mode -----------------------------------------------------------------------


def test_self_mode_allows_only_staged_sources(stick: Path, builds, tmp_path: Path):
    library = Library(library_set(make_library(tmp_path / "home", "Laptop")))
    yt = stick / ".ytop"
    staged = stick / STAGE
    shutil.copytree(builds.new, staged)
    # make-stick never takes a stick as its own source, staged or not.
    for source in (yt, staged):
        with pytest.raises(ms.MakeStickError, match="running from"):
            ms.plan_make(stick, source, library, mode="upgrade")
    # Self mode takes only the staged image under the stick itself.
    for source in (yt, builds.new):
        with pytest.raises(ms.MakeStickError, match="staged"):
            ms.plan_self_upgrade(stick, source)
    other = tmp_path / "other"
    shutil.copytree(builds.stick, other)
    with pytest.raises(ms.MakeStickError, match="staged"):
        ms.plan_self_upgrade(other, staged)
    plan = ms.plan_self_upgrade(stick, staged)
    assert plan.mode == "upgrade" and plan.image_root == yt
    # A staged image whose file list it cannot trust is refused.
    with open(staged / MANIFEST_NAME, "a", encoding="utf-8") as handle:
        handle.write(f"{'0' * 64}  app/ytop/extra.py\n")
    with pytest.raises(ms.MakeStickError, match="verified"):
        ms.plan_self_upgrade(stick, staged)


# --- Windows: files in use -----------------------------------------------------------


LOCKED = ("python.exe", ".dll", ".pyd")


def _locking_unlink(real):
    state = {"on": True}

    def unlink(path, *args, **kwargs):
        name = os.fspath(path)
        if state["on"] and name.endswith(LOCKED) and (ms.OLD in name or STAGE in name):
            raise PermissionError(13, "in use", name)
        return real(path, *args, **kwargs)

    return unlink, state


def test_cleanup_tolerates_locked_files(stick: Path, server, monkeypatch):
    # No trusted old file list: no rollback copy, so .ytop.old is deleted.
    _set_build(stick / ".ytop", manifest_sha256="0" * 64)
    unlink, state = _locking_unlink(os.unlink)
    monkeypatch.setattr(os, "unlink", unlink)
    result = _install(stick, server)
    (stick / ".ytop/previous").mkdir()
    (stick / ".ytop/previous/keep.txt").write_text("rollback copy\n")
    assert result.status == "updated", result.message
    old = stick / ms.OLD
    # The Windows Python that was in use is left for the next start; the
    # journal is gone, so nothing will ever "roll back" the finished update.
    assert (old / "runtime/windows-x86_64/python/python.exe").read_bytes() != b"MZ new python"
    assert not (old / boot.JOURNAL).exists()
    assert not boot.switch_interrupted(stick)
    assert json.loads((stick / ".ytop/build.json").read_text())["build_id"] == NEW_ID
    # Still locked at the next start: skipped, no error.
    boot.clear_leftovers(stick)
    assert old.exists()
    # Unlocked later: cleared.
    state["on"] = False
    boot.clear_leftovers(stick)
    assert not old.exists()
    assert (stick / ".ytop/previous/keep.txt").read_text() == "rollback copy\n"


def test_boot_never_clears_a_switch_still_to_repair(stick: Path, monkeypatch):
    old = stick / ms.OLD
    old.mkdir()
    (old / boot.JOURNAL).write_text("")
    os.rename(stick / ".ytop/app", old / "app")
    assert boot.switch_interrupted(stick)
    boot.clear_leftovers(stick)
    assert (old / "app").is_dir() and (old / boot.JOURNAL).exists()
    # A make in progress (its lock) is left alone too.
    os.rename(old / "app", stick / ".ytop/app")
    (stick / ms.LOCK).write_text(json.dumps(boot.lock_payload()))
    (stick / STAGE).mkdir()
    boot.clear_leftovers(stick)
    assert (stick / STAGE).exists() and old.exists()


def test_boot_clears_leftovers_only_after_the_repair_check(tmp_path: Path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(boot, "repair_interrupted_upgrade",
                        lambda home, root: calls.append("repair") or True)
    monkeypatch.setattr(boot, "clear_leftovers", lambda home: calls.append("clear"))
    assert boot.main(["--home", str(tmp_path)]) == boot.EXIT_REPAIRED
    assert calls == ["repair"]
    calls.clear()
    monkeypatch.setattr(boot, "repair_interrupted_upgrade",
                        lambda home, root: calls.append("repair") or False)
    assert boot.main(["--home", str(tmp_path), "--runtime-root", str(tmp_path)]) == boot.EXIT_DAMAGED
    assert calls == ["repair", "clear"]


# --- the app -------------------------------------------------------------------------


class FakeUpdater:
    def __init__(self, release: appupdate.AppRelease | None = None) -> None:
        self.release = release
        self.checks = 0

    def check(self, ytop, config, http=None) -> appupdate.AppCheck:
        self.checks += 1
        if self.release is None:
            return appupdate.AppCheck("up_to_date")
        return appupdate.AppCheck("available", self.release)


class SilentUpdater:
    """A check that gets no usable answer: ``status`` is what it reports."""

    def __init__(self, status: str) -> None:
        self.status = status
        self.checks = 0

    def check(self, ytop, config, http=None) -> appupdate.AppCheck:
        self.checks += 1
        return appupdate.AppCheck(self.status, message="no answer")


def _release() -> appupdate.AppRelease:
    return appupdate.AppRelease(
        version="0.1.0", build_id=NEW_ID, tag="v0.1.0", notes="Faster pages.",
        doc={}, assets={},
    )


def _client(stick: Path, app_updates: appupdate.AppUpdates):
    auto = AutoUpdate(None, lambda: Config(), loaded=[], base_path=[])
    app = create_app(stick, libraries=library_set(stick), autoupdate=auto,
                     app_updates=app_updates)
    return app, TestClient(app)


ASK = "Check for YTOP updates on this stick?"


def test_asks_once_then_respects_the_answer(stick: Path):
    fake = FakeUpdater()
    updates = appupdate.AppUpdates(
        stick / ".ytop", lambda: load_config(stick), stick, check=fake.check,
    )
    _app, client = _client(stick, updates)
    with client:
        # Not asked before the start-up check found the network.
        assert ASK not in client.get("/").text
        updates.on_start_outcome("offline")
        assert ASK not in client.get("/").text
        updates.on_start_outcome("up to date")
        assert ASK in client.get("/").text
        assert fake.checks == 0  # asking never checks
        response = client.post("/app-update/answer", data={"answer": "no", "next": "/"},
                               follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/"
        assert load_config(stick).updates.check_app is False
        assert ASK not in client.get("/").text
        # A later online start: the answer stands, nothing is checked.
        updates.on_start_outcome("updated")
        assert ASK not in client.get("/").text and fake.checks == 0
        # "Check now" works whatever the setting.
        response = client.post("/app-update/check", follow_redirects=False)
        assert response.status_code == 303 and fake.checks == 1
        assert "up to date" in client.get("/app-update").text
        assert "Check now" in client.get("/settings").text

    # Yes: stored, and the start-up check then looks for a release.
    (stick / ".ytop/config.toml").unlink(missing_ok=True)
    fake = FakeUpdater(_release())
    updates = appupdate.AppUpdates(
        stick / ".ytop", lambda: load_config(stick), stick, check=fake.check,
    )
    _app, client = _client(stick, updates)
    with client:
        updates.on_start_outcome("up to date")
        client.post("/app-update/answer", data={"answer": "yes"})
        assert load_config(stick).updates.check_app is True
        assert fake.checks == 1
        page = client.get("/").text
        assert ASK not in page and "YTOP 0.1.0 is available" in page
        assert 'href="/app-update"' in page and ">Update<" in page
        updates.on_start_outcome("up to date")
        assert fake.checks == 2
        updates.on_start_outcome("offline")
        assert fake.checks == 2
        updates.on_start_outcome("off")  # component checks off: still looks
        assert fake.checks == 3
        detail = client.get("/app-update").text
        assert "Faster pages." in detail and "Install" in detail


def _wait(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.02)


@pytest.mark.parametrize("outcome", ["updated", "undone"])
def test_install_quits_and_says_start_again(
    stick: Path, capsys, outcome: str, tmp_path: Path, monkeypatch,
):
    events: list[str] = []

    def install(ytop, config, release, *, before_switch, progress=None, **kwargs):
        events.append("download")
        before_switch()
        # The switch moves the running app's own files (templates too) aside.
        os.rename(templates, templates.with_name("moved-away"))
        events.append("switch")
        if outcome == "updated":
            return appupdate.InstallResult(
                "updated", release.version,
                appupdate.UPDATED.format(name="YTOP", version=release.version), stopped=True,
            )
        return appupdate.InstallResult(
            "undone", release.version, appupdate.UNDONE.format(name="YTOP"), stopped=True,
        )

    import ytop.web.app as web_app

    templates = tmp_path / "templates"
    shutil.copytree(web_app.TEMPLATE_DIR, templates)
    monkeypatch.setattr(web_app, "TEMPLATE_DIR", templates)
    fake = FakeUpdater(_release())
    updates = appupdate.AppUpdates(
        stick / ".ytop", lambda: load_config(stick), stick, check=fake.check, install=install,
    )
    app, client = _client(stick, updates)
    stopped = threading.Event()
    with client:
        app.state.request_stop = stopped.set
        client.post("/app-update/check")
        # Installing needs the user's confirmation of this very release.
        assert client.post("/app-update/install", data={"build_id": "other"}).status_code == 409
        assert events == []
        response = client.post("/app-update/install", data={"build_id": NEW_ID},
                               follow_redirects=False)
        assert response.status_code == 303
        _wait(lambda: updates.phase in ("updated", "undone"))
        page = client.get("/app-update").text
        # No new check while (or after) installing.
        assert client.post("/app-update/check").status_code == 409
        assert stopped.wait(10)
    assert events == ["download", "switch"]
    assert app.state.stopping
    out = capsys.readouterr().out
    if outcome == "updated":
        assert "YTOP was updated to 0.1.0. Start it again." in page
        assert "YTOP was updated to 0.1.0. Start it again." in out
    else:
        assert "undone" in page and "undone" in out


def test_self_update_never_rewrites_the_running_windows_launcher(
    stick: Path, server, tmp_path: Path, builds, monkeypatch,
):
    """cmd.exe reads a running .cmd by path and offset: replacing it while
    it waits for Python would run part of the new file."""
    before = (stick / CMD).read_bytes()
    readme = (stick / "README.txt").read_bytes()
    monkeypatch.setattr(ms, "_running_launcher", lambda: CMD)
    assert _install(stick, server).status == "updated"
    assert (stick / CMD).read_bytes() == before
    assert (stick / "README.txt").read_bytes() != readme  # names the new build
    # Elsewhere (and from another stick) the launcher is brought up to date.
    other = tmp_path / "other"
    shutil.copytree(builds.stick, other)
    monkeypatch.setattr(ms, "_running_launcher", lambda: None)
    assert _install(other, server).status == "updated"
    assert (other / CMD).read_bytes().endswith(b"rem newer launcher\r\n")


def _stick_updates(stick: Path, now: list, fake: "FakeUpdater") -> appupdate.AppUpdates:
    from ytop.config import set_updates_option

    set_updates_option(stick, "check_app", True)
    return appupdate.AppUpdates(
        stick / ".ytop", lambda: load_config(stick), stick, check=fake.check,
        clock=lambda: now[0],
    )


def test_start_up_app_check_runs_at_every_start(stick: Path):
    from datetime import UTC, datetime, timedelta

    now = [datetime(2026, 11, 14, 9, 0, tzinfo=UTC)]
    fake = FakeUpdater()
    updates = _stick_updates(stick, now, fake)
    updates.on_start_outcome("up to date")
    assert fake.checks == 1 and load_config(stick).updates.last_check
    now[0] += timedelta(hours=5)
    updates.on_start_outcome("up to date")
    assert fake.checks == 2
    updates.on_start_outcome("offline")
    assert fake.checks == 2
    updates.on_start_outcome("off")  # component checks off: still looks
    assert fake.checks == 3


@pytest.mark.parametrize("status", ["offline", "error", "rate_limited"])
def test_a_kept_offer_survives_a_start_check_without_an_answer(
    stick: Path, server, status: str,
):
    from datetime import UTC, datetime, timedelta

    from ytop.config import set_updates_option

    set_updates_option(stick, "check_app", True)
    set_updates_option(stick, "repo", GH_REPO)
    set_updates_option(stick, "api_base", server.base + "/api")
    now = [datetime(2026, 11, 14, 9, 0, tzinfo=UTC)]
    first = appupdate.AppUpdates(stick / ".ytop", lambda: load_config(stick), stick,
                                 clock=lambda: now[0])
    first.on_start_outcome("up to date")
    assert first.offer is not None
    now[0] += timedelta(hours=1)
    silent = SilentUpdater(status)
    again = appupdate.AppUpdates(
        stick / ".ytop", lambda: load_config(stick), stick, check=silent.check,
        clock=lambda: now[0],
    )
    again.on_start_outcome("recent")  # counts as online: GitHub is asked
    assert silent.checks == 1
    assert again.offer is not None and again.offer.build_id == first.offer.build_id


def test_a_broken_release_is_not_rechecked_every_poll(stick: Path):
    from datetime import UTC, datetime, timedelta

    start = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)
    now = [start]
    broken = SilentUpdater("error")
    updates = appupdate.AppUpdates(
        stick / ".ytop", lambda: load_config(stick), stick, check=broken.check,
        clock=lambda: now[0],
    )
    from ytop.config import set_updates_option

    set_updates_option(stick, "check_app", True)
    updates.on_start_outcome("up to date")
    assert broken.checks == 1
    now[0] = start + timedelta(minutes=10)
    updates.recheck_if_due()
    assert broken.checks == 1
    now[0] = start + timedelta(hours=6, minutes=1)
    updates.recheck_if_due()
    assert broken.checks == 2


def test_answering_yes_records_the_check(stick: Path):
    from datetime import UTC, datetime

    from ytop.config import set_updates_option

    now = [datetime(2026, 11, 14, 9, 0, tzinfo=UTC)]
    fake = FakeUpdater()
    updates = _stick_updates(stick, now, fake)
    set_updates_option(stick, "check_app", None)
    updates.online = True
    updates.answer(True)
    assert fake.checks == 1 and load_config(stick).updates.last_check
    updates.recheck_if_due()
    assert fake.checks == 1


def test_recheck_runs_every_six_hours(stick: Path):
    from datetime import UTC, datetime, timedelta

    from ytop.config import set_updates_option

    start = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)
    now = [start]
    fake = FakeUpdater()
    updates = _stick_updates(stick, now, fake)
    updates.on_start_outcome("up to date")
    assert fake.checks == 1
    now[0] = start + timedelta(hours=5)
    updates.recheck_if_due()
    assert fake.checks == 1
    now[0] = start + timedelta(hours=6, minutes=1)
    updates.recheck_if_due()
    assert fake.checks == 2
    now[0] += timedelta(hours=7)
    set_updates_option(stick, "check_app", False)
    updates.recheck_if_due()
    assert fake.checks == 2
    set_updates_option(stick, "check_app", True)
    updates.phase = "installing"
    updates.recheck_if_due()
    assert fake.checks == 2


def test_cleanup_ignores_an_unverifiable_lock_after_an_hour(stick: Path, monkeypatch):
    """Where start times are unknown (macOS) a live pid could be anyone's:
    after an hour it no longer keeps leftovers on the stick."""
    monkeypatch.setattr(boot, "process_start_time", lambda pid=None: None)
    (stick / STAGE).mkdir()
    (stick / ms.LOCK).write_text(json.dumps({"pid": os.getpid(), "started": None}))
    boot.clear_leftovers(stick)
    assert (stick / STAGE).exists()  # an hour has not passed: could be live
    long_ago = time.time() - boot.SWITCH_LOCK_MAX_AGE_S - 60
    os.utime(stick / ms.LOCK, (long_ago, long_ago))
    boot.clear_leftovers(stick)
    assert not (stick / STAGE).exists() and not (stick / ms.LOCK).exists()
    # A lock whose start time is checked and matches stays live however old.
    monkeypatch.setattr(boot, "process_start_time", lambda pid=None: 1000.0)
    (stick / STAGE).mkdir()
    (stick / ms.LOCK).write_text(json.dumps({"pid": os.getpid(), "started": 1000.0}))
    os.utime(stick / ms.LOCK, (long_ago, long_ago))
    boot.clear_leftovers(stick)
    assert (stick / STAGE).exists()


# --- reverting to the previous version -----------------------------------------------


def _revert(stick: Path, **kwargs):
    kwargs.setdefault("before_switch", lambda: None)
    return appupdate.revert_app(stick / ".ytop", **kwargs)


def _tree(folder: Path) -> set[str]:
    return {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()}


def test_previous_holds_exactly_the_replaced_files(stick: Path, server):
    yt = stick / ".ytop"
    old_build = (yt / "build.json").read_bytes()
    old_manifest = read_manifest(yt / MANIFEST_NAME)
    assert old_manifest is not None
    # An older rollback copy (and a half-deleted one) are replaced, never kept.
    (yt / ms.PREVIOUS).mkdir()
    (yt / ms.PREVIOUS / "stale.txt").write_text("older\n")
    (yt / ms.PREVIOUS_DROP).mkdir()
    (yt / ms.PREVIOUS_DROP / "half.txt").write_text("half\n")
    assert appupdate.previous_version(yt) is None  # not a valid one
    assert _install(stick, server).status == "updated"
    new_manifest = read_manifest(yt / MANIFEST_NAME)
    assert new_manifest is not None
    replaced = {rel for rel, digest in old_manifest.items() if new_manifest.get(rel) != digest}
    # The Windows Python and lib, and the Windows launcher template: the
    # update's added app file is not in it, nor the unchanged rest of app/.
    assert replaced == {
        "lib/windows-x86_64/nativepkg/_native.txt",
        "runtime/windows-x86_64/python/python.exe",
        f"app/ytop/stick/launchers/{CMD}",
    }
    previous = yt / ms.PREVIOUS
    assert _tree(previous) == replaced | {"build.json", MANIFEST_NAME, ms.PREVIOUS_MARK}
    assert hash_tree(previous, sorted(replaced)) == {rel: old_manifest[rel] for rel in replaced}
    assert (previous / "build.json").read_bytes() == old_build
    assert read_manifest(previous / MANIFEST_NAME) == old_manifest
    assert not (yt / ms.PREVIOUS_DROP).exists()
    for leftover in (ms.OLD, STAGE, ms.MAKING, ms.LOCK):
        assert not (stick / leftover).exists(), leftover
    found = appupdate.previous_version(yt)
    assert found is not None and (found.version, found.build_id) == ("0.0.9", OLD_ID)
    # It belongs to this build only: another build.json makes it invalid.
    _set_build(yt, build_id="209912312359-other00")
    assert appupdate.previous_version(yt) is None


def test_no_previous_without_a_trusted_old_file_list(stick: Path, server):
    """A stick whose old file list cannot be trusted keeps no rollback copy
    (it could not be checked), and drops any older one."""
    yt = stick / ".ytop"
    (yt / ms.PREVIOUS).mkdir()
    (yt / ms.PREVIOUS / "stale.txt").write_text("older\n")
    _set_build(yt, manifest_sha256="0" * 64)
    assert _install(stick, server).status == "updated"
    assert not (yt / ms.PREVIOUS).exists()
    assert appupdate.previous_version(yt) is None
    assert not (stick / ms.OLD).exists()


def test_revert_restores_the_old_manifest_exactly(stick: Path, server):
    yt = stick / ".ytop"
    before = _owned_state(stick)
    readme = (stick / "README.txt").read_bytes()
    library_before = (yt / "library.json").read_bytes()
    components_before = hash_tree(yt, sorted(
        p.relative_to(yt).as_posix() for p in (yt / "components").rglob("*") if p.is_file()
    ))
    assert _install(stick, server).status == "updated"
    updated = _owned_state(stick)
    stopped: list[int] = []
    result = _revert(stick, before_switch=lambda: stopped.append(1))
    assert result.status == "reverted", result.message
    assert result.message == "YTOP was reverted to 0.0.9. Start it again."
    assert result.stopped and stopped == [1]
    assert _owned_state(stick) == before  # build.json and manifest too
    assert not (yt / "app/ytop/NEW_APP").exists()  # what the update added is gone
    assert ms.read_trusted_manifest(yt) == read_manifest(yt / MANIFEST_NAME)
    assert (stick / "README.txt").read_bytes() == readme
    assert (yt / "library.json").read_bytes() == library_before
    # Components are not image-owned: a revert leaves them as they are.
    assert hash_tree(yt, sorted(components_before)) == components_before
    for leftover in (ms.OLD, STAGE, ms.MAKING, ms.LOCK):
        assert not (stick / leftover).exists(), leftover
    # The files it reverted from are the rollback copy now.
    found = appupdate.previous_version(yt)
    assert found is not None and (found.version, found.build_id) == ("0.1.0", NEW_ID)
    assert updated != before


def test_revert_twice_returns_to_the_update(stick: Path, server):
    yt = stick / ".ytop"
    before = _owned_state(stick)
    assert _install(stick, server).status == "updated"
    updated = _owned_state(stick)
    assert _revert(stick).status == "reverted"
    assert _owned_state(stick) == before
    result = _revert(stick)
    assert result.status == "reverted", result.message
    assert result.message == "YTOP was reverted to 0.1.0. Start it again."
    assert _owned_state(stick) == updated
    found = appupdate.previous_version(yt)
    assert found is not None and found.build_id == OLD_ID
    # Only one previous version is ever kept.
    assert _tree(yt / ms.PREVIOUS) == {
        "lib/windows-x86_64/nativepkg/_native.txt",
        "runtime/windows-x86_64/python/python.exe",
        f"app/ytop/stick/launchers/{CMD}",
        "build.json", MANIFEST_NAME, ms.PREVIOUS_MARK,
    }


def test_revert_refuses_a_damaged_previous_before_anything_stops(stick: Path, server):
    yt = stick / ".ytop"
    assert _install(stick, server).status == "updated"
    updated = _owned_state(stick)
    (yt / ms.PREVIOUS / "runtime/windows-x86_64/python/python.exe").write_bytes(b"MZ junk")
    called: list[int] = []
    result = _revert(stick, before_switch=lambda: called.append(1))
    assert result.status == "failed" and not result.stopped and called == []
    assert "python.exe" in result.message
    assert _owned_state(stick) == updated
    assert not (stick / STAGE).exists() and not (stick / ms.LOCK).exists()
    # No previous at all: nothing to revert to.
    shutil.rmtree(yt / ms.PREVIOUS)
    result = _revert(stick, before_switch=lambda: called.append(1))
    assert result.status == "failed" and called == []


def test_make_stick_upgrade_drops_previous(stick: Path, server, builds, tmp_path: Path):
    yt = stick / ".ytop"
    assert _install(stick, server).status == "updated"
    assert appupdate.previous_version(yt) is not None
    (yt / ms.PREVIOUS_DROP).mkdir()
    library = Library(library_set(make_library(tmp_path / "home", "Laptop")))
    plan = ms.plan_make(stick, builds.old, library, mode="upgrade")
    ms.run_make(plan, builds.old, library, lambda update: None)
    assert json.loads((yt / "build.json").read_text())["build_id"] == OLD_ID
    assert not (yt / ms.PREVIOUS).exists() and not (yt / ms.PREVIOUS_DROP).exists()
    assert appupdate.previous_version(yt) is None
    # A new stick never carries one either.
    fresh = _make_stick(builds.old, tmp_path / "fresh", tmp_path / "home3", PLATS)
    assert not (fresh / ".ytop" / ms.PREVIOUS).exists()


def _counted_revert(stick: Path, monkeypatch) -> list[tuple[Path, Path]]:
    real_place = ms._place
    calls: list[tuple[Path, Path]] = []

    def counting(src: Path, dst: Path) -> None:
        calls.append((src, dst))
        real_place(src, dst)

    monkeypatch.setattr(ms, "_place", counting)
    assert _revert(stick).status == "reverted"
    monkeypatch.setattr(ms, "_place", real_place)
    return calls


def test_revert_crash_rolls_back(builds, server, tmp_path: Path, monkeypatch):
    """A crash before any rename of a revert: before the commit the next
    start puts the update back exactly (its rollback copy still valid);
    after it the stick is the reverted build, launchable, with a valid
    rollback copy or none."""
    template = tmp_path / "template"
    shutil.copytree(builds.stick, template)
    old_state = _owned_state(template)
    assert _install(template, server).status == "updated"
    new_state = _owned_state(template)
    counted = tmp_path / "counted"
    shutil.copytree(template, counted)
    calls = _counted_revert(counted, monkeypatch)
    commit = next(i for i, (_src, dst) in enumerate(calls, 1) if dst == counted / ".ytop/app")
    # The switch: app out, two files aside and two in (NEW_APP leaves with
    # app), build.json + manifest backed up and replaced, app in; then the
    # rollback copy: the old one aside, the four files the revert replaced
    # or removed, the manifest, build.json and its mark.
    assert commit == 1 + 2 + 2 + 4 + 1
    assert len(calls) == commit + 1 + 4 + 2 + 1
    real_place = ms._place
    for crash_at in range(1, len(calls) + 1):
        stick = tmp_path / f"stick-{crash_at}"
        shutil.copytree(template, stick)
        yt = stick / ".ytop"
        count = {"n": 0}

        def crashing(src: Path, dst: Path) -> None:
            count["n"] += 1
            if count["n"] == crash_at:
                raise Crash()
            real_place(src, dst)

        monkeypatch.setattr(ms, "_place", crashing)
        with pytest.raises(Crash):
            _revert(stick)
        monkeypatch.setattr(ms, "_place", real_place)
        _stale_lock(stick)
        assert boot.switch_interrupted(stick) == (1 < crash_at <= commit)
        boot.repair_interrupted_upgrade(stick, yt.resolve())
        boot.clear_leftovers(stick)
        for leftover in (STAGE, ms.MAKING, ms.OLD, ms.LOCK):
            assert not (stick / leftover).exists(), (crash_at, leftover)
        assert (yt / "app").is_dir() and not boot.switch_interrupted(stick)
        found = appupdate.previous_version(yt)
        if crash_at <= commit:
            assert _owned_state(stick) == new_state, crash_at
            assert found is not None and found.build_id == OLD_ID, crash_at
            back, other = old_state, NEW_ID
        else:
            assert _owned_state(stick) == old_state, crash_at
            back, other = new_state, None
            if found is None:
                continue
            assert found.build_id == NEW_ID, crash_at
        # Whatever rollback copy is offered works.
        assert _revert(stick).status == "reverted", crash_at
        assert _owned_state(stick) == back, crash_at
        if other is not None:
            assert appupdate.previous_version(yt).build_id == other


def test_storage_offers_revert_only_with_a_valid_previous(stick: Path, server, tmp_path: Path,
                                                          monkeypatch, capsys):
    import ytop.web.app as web_app

    updates = appupdate.AppUpdates(stick / ".ytop", lambda: load_config(stick), stick,
                                   check=FakeUpdater().check)
    _app, client = _client(stick, updates)
    with client:
        page = client.get("/settings").text
        assert "Revert to previous version" not in page
        assert client.post("/app-update/revert", data={"build_id": OLD_ID}).status_code == 409
    assert _install(stick, server).status == "updated"

    events: list[str] = []

    def revert(ytop, *, before_switch, progress=None):
        events.append("stage")
        before_switch()
        os.rename(templates, templates.with_name("moved-away"))
        events.append("switch")
        return appupdate.InstallResult(
            "reverted", "0.0.9", appupdate.REVERTED.format(name="YTOP", version="0.0.9"),
            stopped=True,
        )

    templates = tmp_path / "templates"
    shutil.copytree(web_app.TEMPLATE_DIR, templates)
    monkeypatch.setattr(web_app, "TEMPLATE_DIR", templates)
    updates = appupdate.AppUpdates(stick / ".ytop", lambda: load_config(stick), stick,
                                   check=FakeUpdater().check, revert=revert)
    app, client = _client(stick, updates)
    stopped = threading.Event()
    with client:
        app.state.request_stop = stopped.set
        page = client.get("/settings").text
        assert "Revert to previous version" in page and "0.0.9" in page
        assert 'action="/app-update/revert"' in page and "data-confirm" in page
        # Only the rollback copy shown may be reverted to.
        assert client.post("/app-update/revert", data={"build_id": "other"}).status_code == 409
        assert events == []
        response = client.post("/app-update/revert", data={"build_id": OLD_ID},
                               follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/app-update"
        _wait(lambda: updates.finished)
        page = client.get("/app-update").text
        assert stopped.wait(10)
    assert events == ["stage", "switch"]
    assert "YTOP was reverted to 0.0.9. Start it again." in page
    assert "YTOP was reverted to 0.0.9. Start it again." in capsys.readouterr().out


def test_module_fixture_sticks_stay_out_of_the_real_registry(builds) -> None:
    # The fixture is module-scoped, so it runs before per-test isolation:
    # the session-wide isolation in conftest must already cover it.
    real = Path.home() / ".config" / "ytop" / "libraries.json"
    assert builds.registry != real


def _other_release(fake: FakeReleases, tag: str = "ffmpeg-minimal-1") -> dict:
    """A release of another kind in the same repo, marked "latest"."""
    url = fake.add(f"/dl/{GH_REPO}/{tag}/ffmpeg.tar.xz", b"ff")
    release = {"tag_name": tag, "draft": False, "prerelease": False,
               "assets": [{"name": "ffmpeg.tar.xz", "browser_download_url": url}]}
    fake.add(f"/api/repos/{GH_REPO}/releases/latest", release)
    return release


def test_check_looks_past_a_latest_release_of_another_kind(stick: Path, builds):
    fake = FakeReleases()
    try:
        _serve(fake, builds.release)
        app = json.loads(fake.routes[f"/api/repos/{GH_REPO}/releases/latest"].body)
        other = _other_release(fake)
        fake.add(f"/api/repos/{GH_REPO}/releases", [other, app])
        found = appupdate.check_app(stick / ".ytop", _config(fake), updater.Http())
        assert found is not None and found.build_id == NEW_ID
    finally:
        fake.close()


def test_check_with_no_app_release_says_none_published(stick: Path):
    fake = FakeReleases()
    try:
        # A private or missing repo: GitHub answers 404 everywhere.
        result = appupdate.check_app_result(stick / ".ytop", _config(fake), updater.Http())
        assert (result.status, result.message) == ("up_to_date", appupdate.NO_RELEASE)
        # Only other kinds of release: the same.
        other = _other_release(fake)
        fake.add(f"/api/repos/{GH_REPO}/releases", [other])
        result = appupdate.check_app_result(stick / ".ytop", _config(fake), updater.Http())
        assert (result.status, result.message) == ("up_to_date", appupdate.NO_RELEASE)
    finally:
        fake.close()


def test_page_says_no_release_published(stick: Path):
    def check(ytop, config, http=None):
        return appupdate.AppCheck("up_to_date", message=appupdate.NO_RELEASE)

    updates = appupdate.AppUpdates(stick / ".ytop", lambda: load_config(stick), stick,
                                   check=check)
    _app, client = _client(stick, updates)
    with client:
        client.post("/app-update/check", follow_redirects=False)
        page = client.get("/app-update").text
        assert "No YTOP release has been published yet" in page
        assert "Couldn&#39;t check" not in page and "Couldn't check" not in page


def test_install_adds_the_mac_markers(stick: Path, server):
    """An app update is an upgrade too: it leaves the macOS markers (#17)."""
    shutil.rmtree(stick / ".fseventsd", ignore_errors=True)
    (stick / ".metadata_never_index").unlink(missing_ok=True)
    assert _install(stick, server).status == "updated"
    assert (stick / ".metadata_never_index").read_bytes() == b""
    assert (stick / ".fseventsd" / "no_log").read_bytes() == b""


# --- edge cases (#12) ----------------------------------------------------------------


def test_tidy_drops_a_crashed_rollback_copy_and_keeps_a_valid_one(stick: Path, server):
    yt = stick / ".ytop"
    assert appupdate.tidy_previous(yt) is False  # nothing to tidy
    # What a crash after the commit point can leave: a half-made previous/
    # (no mark: never offered) and a previous.del.
    (yt / ms.PREVIOUS).mkdir()
    (yt / ms.PREVIOUS / "half.txt").write_text("half\n")
    (yt / ms.PREVIOUS_DROP / "sub").mkdir(parents=True)
    (yt / ms.PREVIOUS_DROP / "sub/older.txt").write_text("older\n")
    assert appupdate.tidy_previous(yt) is True
    assert not (yt / ms.PREVIOUS).exists() and not (yt / ms.PREVIOUS_DROP).exists()
    assert not (stick / ms.LOCK).exists()
    # A valid copy stays; only the leftover beside it goes.
    assert _install(stick, server).status == "updated"
    found = appupdate.previous_version(yt)
    assert found is not None
    (yt / ms.PREVIOUS_DROP).mkdir()
    assert appupdate.tidy_previous(yt) is True
    assert appupdate.previous_version(yt) == found
    assert not (yt / ms.PREVIOUS_DROP).exists()


def test_tidy_never_runs_under_a_live_lock_or_a_switch_to_repair(
    stick: Path, monkeypatch,
):
    yt = stick / ".ytop"
    (yt / ms.PREVIOUS_DROP).mkdir()
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:  # another live YTOP holds the stick
        (stick / ms.LOCK).write_text(json.dumps(
            {"pid": other.pid, "started": boot.process_start_time(other.pid)}
        ))
        assert appupdate.tidy_previous(yt) is False
        assert (yt / ms.PREVIOUS_DROP).exists() and (stick / ms.LOCK).exists()
    finally:
        other.kill()
        other.wait()
    (stick / ms.LOCK).unlink()
    monkeypatch.setattr(appupdate, "switch_interrupted", lambda target: True)
    assert appupdate.tidy_previous(yt) is False
    assert (yt / ms.PREVIOUS_DROP).exists()


def test_tidy_keeps_a_valid_copy_it_cannot_read_just_now(stick: Path, server, monkeypatch):
    """A read that fails for another reason than "not there" (a slow stick,
    a file an antivirus holds) is no verdict: the copy stays."""
    from pathlib import Path as RealPath

    yt = stick / ".ytop"
    assert _install(stick, server).status == "updated"
    found = appupdate.previous_version(yt)
    assert found is not None and appupdate.previous_verdict(yt) == "valid"
    mark = yt / ms.PREVIOUS / ms.PREVIOUS_MARK
    real_read = RealPath.read_text

    def flaky(self, *args, **kwargs):
        if self == mark:
            raise PermissionError(13, "in use by another process")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(RealPath, "read_text", flaky)
    assert appupdate.previous_verdict(yt) == "unknown"
    assert appupdate.tidy_previous(yt) is False
    monkeypatch.undo()
    # An unreadable build.json of the stick is no verdict either.
    real_sha = appupdate.sha256_file

    def eio(path):
        if RealPath(path) == yt / "build.json":
            raise OSError(5, "I/O error")
        return real_sha(path)

    monkeypatch.setattr(appupdate, "sha256_file", eio)
    assert appupdate.previous_verdict(yt) == "unknown"
    assert appupdate.tidy_previous(yt) is False
    monkeypatch.undo()
    assert appupdate.previous_version(yt) == found


def test_tidy_deletes_a_copy_for_another_build(stick: Path, server):
    yt = stick / ".ytop"
    assert _install(stick, server).status == "updated"
    assert appupdate.previous_verdict(yt) == "valid"
    _set_build(yt, build_id="209912312359-other00")  # e.g. a crash, then a make
    assert appupdate.previous_verdict(yt) == "invalid"
    assert appupdate.tidy_previous(yt) is True
    assert not (yt / ms.PREVIOUS).exists()
    assert appupdate.previous_verdict(yt) == "none"


def test_tidy_runs_at_start_on_a_stick(stick: Path, monkeypatch):
    yt = stick / ".ytop"
    (yt / ms.PREVIOUS_DROP).mkdir()
    monkeypatch.setattr(appupdate, "stick_image", lambda: yt)
    appupdate.AppUpdates.for_runtime(lambda: load_config(stick), stick)
    _wait(lambda: not (yt / ms.PREVIOUS_DROP).exists())


def test_page_warns_when_an_update_keeps_no_rollback_copy(stick: Path):
    yt = stick / ".ytop"
    updates = appupdate.AppUpdates(yt, lambda: load_config(stick), stick,
                                   check=FakeUpdater(_release()).check)
    _app, client = _client(stick, updates)
    with client:
        client.post("/app-update/check", follow_redirects=False)
        assert updates.status()["revertible"] is True
        assert "data-not-revertible" not in client.get("/app-update").text
        _set_build(yt, manifest_sha256="0" * 64)  # a file list it can't trust
        assert updates.status()["revertible"] is False
        page = client.get("/app-update").text
        page = " ".join(page.split())
        assert "data-not-revertible" in page and "update can't be reverted" in page


def _latest_requests(fake: FakeReleases) -> int:
    return sum(1 for r in fake.requests if r.path.endswith("/releases/latest"))


def test_a_kept_offer_shows_at_start_and_is_rechecked(stick: Path, server):
    from datetime import UTC, datetime, timedelta

    from ytop.config import set_updates_option

    set_updates_option(stick, "check_app", True)
    set_updates_option(stick, "repo", GH_REPO)
    set_updates_option(stick, "api_base", server.base + "/api")
    now = [datetime(2026, 11, 14, 9, 0, tzinfo=UTC)]

    def start(outcome: str = "up to date") -> appupdate.AppUpdates:
        updates = appupdate.AppUpdates(stick / ".ytop", lambda: load_config(stick), stick,
                                       clock=lambda: now[0])
        updates.on_start_outcome(outcome)
        return updates

    first = start()
    assert first.offer is not None and _latest_requests(server) == 1
    # A start five hours later shows it and also asks GitHub.
    now[0] += timedelta(hours=5)
    again = start()
    assert _latest_requests(server) == 2
    assert again.offer is not None and again.offer.build_id == first.offer.build_id
    assert again.offer.doc == first.offer.doc and again.offer.notes == "Faster pages."
    assert again.status()["last_status"] == "available"
    # Offline an hour later: the kept offer still shows, GitHub is not asked.
    now[0] += timedelta(hours=1)
    offline = start("offline")
    assert offline.offer is not None and _latest_requests(server) == 2
    # Seven hours after the last answer the kept offer is too old.
    now[0] += timedelta(hours=6)
    assert start("offline").offer is None
    now[0] += timedelta(hours=1)
    assert start().offer is not None and _latest_requests(server) == 3
    # A kept offer for another build of the stick (it was updated or
    # reverted since) is not used.
    now[0] += timedelta(hours=1)
    _set_build(stick / ".ytop", build_id="202601010000-other00")
    start()
    assert _latest_requests(server) == 4
    # "Check now" finding nothing forgets it.
    path = appupdate._offer_path(stick)
    assert path.is_file()
    server.add(f"/api/repos/{GH_REPO}/releases/latest", {"message": "Not Found"}, 404)
    server.add(f"/api/repos/{GH_REPO}/releases", [])
    first.check()
    assert not path.exists()


def test_a_damaged_kept_offer_is_ignored(stick: Path):
    from datetime import UTC, datetime

    now = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)
    path = appupdate._offer_path(stick)
    path.parent.mkdir(parents=True, exist_ok=True)
    for content in ("not json", "{}", json.dumps({
        "checked_at": now.isoformat(), "stick_build_id": OLD_ID, "doc": {"version": "9"},
        "assets": {},
    })):
        path.write_text(content)
        assert appupdate.load_offer(stick, stick / ".ytop", now) is None
