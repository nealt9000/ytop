"""Making and upgrading YTOP sticks (tmp dirs stand in for USB sticks)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import ytop.makestick as ms
from factories import add_playlist, library_set, make_library
from stick_fakes import code_bytes, make_fake_image
from ytop.cli import main as cli_main
from ytop.fsinfo import FsInfo
from ytop.layout import LibraryLayout, read_library
from ytop.libraries import LibrarySet
from ytop.manifest import (
    MANIFEST_NAME,
    for_platforms,
    hash_tree,
    read_manifest,
    write_manifest,
)
from ytop.web.app import create_app
from ytop.web.library import Library
from ytop.yt.metadata import write_json

PLATS = ["linux-x86_64", "macos-arm64", "windows-x86_64"]
PL_ONE = "PLMAKE00000000000000000000000001"
PL_TWO = "PLMAKE00000000000000000000000002"
LAUNCHERS = [
    "Start YTOP (Windows).cmd",
    "Start YTOP (Mac).command",
    "start-ytop-linux.sh",
    "README.txt",
]


class Crash(BaseException):
    """Stands in for a power cut / unplug: no ``except Exception`` runs."""


@pytest.fixture
def image(tmp_path: Path) -> Path:
    # The image ships precompiled bytecode (Ruling R12).
    return make_fake_image(tmp_path / "img", PLATS)


def home_library(tmp_path: Path):
    home = make_library(tmp_path / "home", "Laptop")
    add_playlist(home, PL_ONE, "Lectures", [("vid1", "One"), ("vid2", "Two")])
    add_playlist(home, PL_TWO, "Music", [("vid3", "Three")])
    libs = library_set(home)
    return home, libs, Library(libs)


def new_target(tmp_path: Path, name: str = "stick") -> Path:
    target = tmp_path / name
    target.mkdir()
    return target


def tree_hash(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_file():
        digest.update(path.read_bytes())
        return digest.hexdigest()
    for item in sorted(path.rglob("*")):
        digest.update(item.relative_to(path).as_posix().encode())
        if item.is_file():
            digest.update(item.read_bytes())
    return digest.hexdigest()


def make(target: Path, image: Path, library: Library, **kwargs) -> ms.MakeReport:
    plan = ms.plan_make(target, image, library, **kwargs)
    return ms.run_make(plan, image, library, lambda update: None)


def second_image(tmp_path: Path, platforms=PLATS) -> Path:
    """Another build: a distinct build_id and a marker file in its app."""
    yt = make_fake_image(tmp_path / "img2", platforms)
    build = json.loads((yt / "build.json").read_text())
    build["build_id"] = "209912312359-newer00"
    (yt / "build.json").write_text(json.dumps(build))
    (yt / "app/ytop/NEW_APP").write_text("new\n")
    refresh_manifest(yt, platforms)
    return yt


def refresh_manifest(yt: Path, platforms=PLATS) -> None:
    """Rewrite the image's manifest from its files, then check it matches."""
    rels = [rel for rel, _size in ms.image_files(yt, platforms)]
    write_manifest(yt / MANIFEST_NAME, hash_tree(yt, rels))
    assert read_manifest(yt / MANIFEST_NAME) == hash_tree(yt, rels)
    build = json.loads((yt / "build.json").read_text())
    build["manifest_sha256"] = hashlib.sha256(
        (yt / MANIFEST_NAME).read_bytes()
    ).hexdigest()
    (yt / "build.json").write_text(json.dumps(build, indent=2) + "\n")


def test_make_new_stick_layout(tmp_path: Path, image: Path):
    home, libs, library = home_library(tmp_path)
    target = new_target(tmp_path)
    events: list = []
    plan = ms.plan_make(
        target, image, library,
        platforms=["linux-x86_64", "windows-x86_64"], playlists=[],
        label="Blue stick",
    )
    assert plan.mode == "new"
    assert plan.bytes_needed > 0
    report = ms.run_make(plan, image, library, events.append)

    yt = target / ".ytop"
    source_build = json.loads((image / "build.json").read_text())
    build = json.loads((yt / "build.json").read_text())
    assert build["platforms"] == ["linux-x86_64", "windows-x86_64"]
    assert build["build_id"] == source_build["build_id"]
    assert "macos-arm64" not in build["sizes"]
    assert sorted(p.name for p in (yt / "runtime").iterdir()) == [
        "linux-x86_64", "windows-x86_64",
    ]
    assert sorted(p.name for p in (yt / "lib").iterdir()) == [
        "common", "linux-x86_64", "windows-x86_64",
    ]
    assert (yt / "boot.py").read_bytes() == (image / "boot.py").read_bytes()
    assert (yt / "app/ytop/cli.py").is_file()
    assert code_bytes(yt / "lib/common", "sharedpkg/data/x.txt") is not None
    for rel in (
        "runtime/linux-x86_64/python/bin/python3",
        "runtime/windows-x86_64/python/python.exe",
        "runtime/linux-x86_64/bin/ffmpeg",
        "runtime/windows-x86_64/bin/deno.exe",
    ):
        assert (yt / rel).read_bytes() == (image / rel).read_bytes()
    # Bytecode is part of the image: copied byte for byte (Ruling R12).
    pycs = sorted(p.relative_to(image) for p in image.rglob("*.pyc"))
    assert pycs and any(str(p).startswith("app/ytop/__pycache__") for p in pycs)
    for rel in pycs:
        if rel.parts[1:2] == ("macos-arm64",):
            assert not (yt / rel).exists()  # a platform not chosen
            continue
        assert (yt / rel).read_bytes() == (image / rel).read_bytes()

    for name in LAUNCHERS:
        assert (target / name).is_file()
    readme = (target / "README.txt").read_bytes()
    assert b"YTOP stick: Blue stick\r\n" in readme
    assert f"Build: {source_build['build_id']}\r\n".encode() in readme
    assert b"{label}" not in readme and b"{build_id}" not in readme
    # The Linux command names the drive folder as mounted, not the label.
    assert f'sh "/run/media/$USER/{target.name}/start-ytop-linux.sh"'.encode() in readme
    assert b"{drive}" not in readme
    assert readme.count(b"\n") == readme.count(b"\r\n")
    assert os.access(target / "start-ytop-linux.sh", os.X_OK)
    assert os.access(target / "Start YTOP (Mac).command", os.X_OK)

    info = read_library(target)
    assert info is not None and info.label == "Blue stick"
    assert report.library_id == info.id
    assert any(ref.root == target for ref in libs.libraries(refresh=True))
    assert not (target / ".ytop.making").exists()
    assert not (target / ".ytop.old").exists()
    assert events and events[-1].phase == "done"
    assert events[-1].bytes == events[-1].total_bytes


def test_make_copies_selected_playlists(tmp_path: Path, image: Path):
    home, libs, library = home_library(tmp_path)
    home_ref = libs.default()
    library.progress_for(home_ref).update("vid1", 42.0, duration=60.0)
    before = {
        "data": tree_hash(home.data_dir / "playlists"),
        "media": tree_hash(home.media_root),
        "progress": tree_hash(home.progress_file),
    }
    target = new_target(tmp_path)
    report = make(
        target, image, library, platforms=["linux-x86_64"],
        playlists=[PL_ONE], label="Gift",
    )
    assert report.playlists == [PL_ONE]

    stick = LibraryLayout(target)
    assert (stick.playlist_meta_dir(PL_ONE) / "playlist.json").is_file()
    assert not stick.playlist_meta_dir(PL_TWO).exists()
    key = library.key_for(report.library_id, PL_ONE)
    media = library.media_path(key, "vid1")
    assert media is not None and media.is_relative_to(stick.media_root)
    assert media.read_bytes() == library.media_path(
        library.key_for(home_ref.id, PL_ONE), "vid1"
    ).read_bytes()
    progress = json.loads(stick.progress_file.read_text())["videos"]["vid1"]
    assert progress["position"] == 42.0
    # The source is unchanged and still holds its playlist.
    assert library.resolve_in(home_ref.id, PL_ONE) is not None
    assert before == {
        "data": tree_hash(home.data_dir / "playlists"),
        "media": tree_hash(home.media_root),
        "progress": tree_hash(home.progress_file),
    }
    assert not [p for p in stick.meta_root.iterdir() if p.name.startswith(".")]


def test_interrupted_make_is_not_a_stick(tmp_path: Path, image: Path, monkeypatch):
    home, libs, library = home_library(tmp_path)
    target = new_target(tmp_path)
    real_copy = ms.copy_file
    calls = {"n": 0}

    def crashing(src, dst, **kwargs):
        calls["n"] += 1
        if calls["n"] == 5:
            raise Crash()
        return real_copy(src, dst, **kwargs)

    monkeypatch.setattr(ms, "copy_file", crashing)
    plan = ms.plan_make(target, image, library, platforms=PLATS, label="X")
    with pytest.raises(Crash):
        ms.run_make(plan, image, library, lambda update: None)
    monkeypatch.setattr(ms, "copy_file", real_copy)

    assert (target / ".ytop.making").is_dir()
    assert read_library(target) is None
    assert not any(ref.root == target for ref in libs.libraries(refresh=True))
    assert LibrarySet(target, detect=False).libraries(refresh=True) == []

    info = ms.inspect_target(target)
    assert any("interrupted" in warning for warning in info.warnings)
    assert not (target / ".ytop.making").exists()
    assert info.library is None and not info.has_ytop

    # An ordinary error cleans up at once.
    def failing(src, dst, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ms, "copy_file", failing)
    with pytest.raises(OSError):
        ms.run_make(plan, image, library, lambda update: None)
    assert not (target / ".ytop.making").exists()
    assert read_library(target) is None


def _library_hashes(target: Path) -> dict[str, str]:
    yt = target / ".ytop"
    return {
        "data": tree_hash(yt / "data"),
        "library.json": tree_hash(yt / "library.json"),
        "config.toml": tree_hash(yt / "config.toml"),
        "cache": tree_hash(yt / "cache"),
        "media": tree_hash(target / "YTOP Library"),
    }


def _used_stick(tmp_path: Path, image: Path, library: Library) -> Path:
    target = new_target(tmp_path)
    make(target, image, library, platforms=PLATS, playlists=[PL_ONE], label="Old")
    stick = LibraryLayout(target)
    add_playlist(stick, "PLONSTICK", "On the stick", [("vid9", "Nine")])
    stick.config_file.write_text('audio_language = "de"\n')
    (stick.cache_dir / "yt-dlp").mkdir(parents=True, exist_ok=True)
    (stick.cache_dir / "yt-dlp" / "x.json").write_text("{}")
    write_json(stick.progress_file, {"schema": 1, "videos": {"vid9": {"position": 7}}})
    return target


def test_upgrade_leaves_library_untouched(tmp_path: Path, image: Path):
    home, libs, library = home_library(tmp_path)
    target = _used_stick(tmp_path, image, library)
    before = _library_hashes(target)
    newer = second_image(tmp_path)

    plan = ms.plan_make(target, newer, library, platforms=["linux-x86_64"])
    assert plan.mode == "upgrade"
    report = ms.run_make(plan, newer, library, lambda update: None)
    assert report.mode == "upgrade"

    assert _library_hashes(target) == before
    yt = target / ".ytop"
    build = json.loads((yt / "build.json").read_text())
    assert build["build_id"] == "209912312359-newer00"
    assert build["platforms"] == ["linux-x86_64"]
    assert (yt / "app/ytop/NEW_APP").is_file()
    assert sorted(p.name for p in (yt / "runtime").iterdir()) == ["linux-x86_64"]
    assert b"YTOP stick: Old\r\n" in (target / "README.txt").read_bytes()
    assert b"209912312359-newer00" in (target / "README.txt").read_bytes()
    assert not (target / ".ytop.old").exists()
    assert not (target / ".ytop.making").exists()
    with pytest.raises(ms.MakeStickError):
        ms.plan_make(target, newer, library, playlists=[PL_TWO], mode="upgrade")
    with pytest.raises(ms.MakeStickError):
        ms.plan_make(target, newer, library, mode="new")


def _plan2_swap(target: Path, image: Path, platforms: list[str]) -> None:
    """The whole-piece swap (older upgrades; new sticks still use it): the
    full image staged in ``.ytop.making`` and switched in piece by piece."""
    making = target / ms.MAKING
    making.mkdir()
    for rel, _size in ms.image_files(image, platforms):
        (making / rel).parent.mkdir(parents=True, exist_ok=True)
        (making / rel).write_bytes((image / rel).read_bytes())
    ms._write_build(making, ms.read_build(image), platforms)
    write_manifest(
        making / MANIFEST_NAME, ms.image_manifest(image, platforms),
    )
    ms._swap_in(target, making)


@pytest.mark.parametrize("crash_at", [2, 3, 7, 9])
def test_upgrade_interrupted_restores_old(
    tmp_path: Path, image: Path, monkeypatch, crash_at: int,
):
    home, libs, library = home_library(tmp_path)
    target = _used_stick(tmp_path, image, library)
    before = _library_hashes(target)
    old_build = (target / ".ytop/build.json").read_bytes()
    newer = second_image(tmp_path)
    real_place = ms._place
    calls = {"n": 0}

    def crashing(src: Path, dst: Path) -> None:
        calls["n"] += 1
        if calls["n"] == crash_at:
            raise Crash()
        real_place(src, dst)

    monkeypatch.setattr(ms, "_place", crashing)
    with pytest.raises(Crash):
        _plan2_swap(target, newer, PLATS)
    monkeypatch.setattr(ms, "_place", real_place)
    assert not (target / ".ytop/app").exists()

    info = ms.inspect_target(target)
    assert any("restored" in warning for warning in info.warnings)
    yt = target / ".ytop"
    assert (yt / "build.json").read_bytes() == old_build
    assert (yt / "app/ytop/cli.py").is_file()
    assert not (yt / "app/ytop/NEW_APP").exists()
    assert sorted(p.name for p in (yt / "runtime").iterdir()) == sorted(PLATS)
    assert not (target / ".ytop.old").exists()
    assert not (target / ".ytop.making").exists()
    assert _library_hashes(target) == before
    assert info.library is not None and info.mode == "upgrade"


def test_ntfs_warning_and_space_check(tmp_path: Path, image: Path, monkeypatch):
    home, libs, library = home_library(tmp_path)
    target = new_target(tmp_path)
    monkeypatch.setattr(ms.fsinfo, "fs_info", lambda path: FsInfo("ntfs", None))
    monkeypatch.setattr(ms, "_disk_usage", lambda path: (1_000, 64_000))
    monkeypatch.setattr(ms, "_internal_disk", lambda path: False)

    info = ms.inspect_target(target, bytes_needed=5_000)
    assert info.fstype == "ntfs"
    assert info.free_bytes == 1_000 and info.total_bytes == 64_000
    assert any(
        "Phones and some computers may not read NTFS/APFS/ext4 drives" in w
        for w in info.warnings
    )
    assert any("space" in w for w in info.warnings)
    with pytest.raises(ms.MakeStickError, match="space"):
        ms.plan_make(target, image, library, platforms=PLATS)

    monkeypatch.setattr(ms.fsinfo, "fs_info", lambda path: FsInfo("exfat", None))
    monkeypatch.setattr(ms, "_disk_usage", lambda path: (10**12, 10**12))
    info = ms.inspect_target(target, bytes_needed=5_000)
    assert info.warnings == []
    ms.plan_make(target, image, library, platforms=PLATS)

    # The stick YTOP runs from cannot be its own target.
    info = ms.inspect_target(image.parent, source=image)
    assert any("running from" in w for w in info.warnings)
    with pytest.raises(ms.MakeStickError, match="running from"):
        ms.plan_make(image.parent, image, library, platforms=PLATS)


def test_source_image_from_env_and_option(tmp_path: Path, image: Path, monkeypatch):
    monkeypatch.delenv("YTOP_IMAGE_ROOT", raising=False)
    with pytest.raises(ms.MakeStickError):
        ms.source_image()
    assert ms.source_image(image) == image
    monkeypatch.setenv("YTOP_IMAGE_ROOT", str(image))
    assert ms.source_image() == image
    with pytest.raises(ms.MakeStickError):
        ms.source_image(tmp_path)  # no build.json


def test_cli_make_stick(tmp_path: Path, image: Path, capsys):
    home, libs, library = home_library(tmp_path)
    target = new_target(tmp_path)
    code = cli_main([
        "--library", str(home.root), "make-stick", str(target),
        "--image", str(image), "--platforms", "linux-x86_64,macos-arm64",
        "--playlists", PL_TWO, "--label", "CLI stick",
    ])
    assert code == 0, capsys.readouterr().err
    build = json.loads((target / ".ytop/build.json").read_text())
    assert build["platforms"] == ["linux-x86_64", "macos-arm64"]
    assert read_library(target).label == "CLI stick"
    assert LibraryLayout(target).playlist_meta_dir(PL_TWO).is_dir()
    assert "CLI stick" in capsys.readouterr().out

    # Already a stick: a plain make refuses, --upgrade replaces the app.
    assert cli_main([
        "--library", str(home.root), "make-stick", str(target), "--image", str(image),
    ]) == 2
    assert "--upgrade" in capsys.readouterr().err
    assert cli_main([
        "--library", str(home.root), "make-stick", str(target),
        "--image", str(image), "--upgrade", "--platforms", "all",
    ]) == 0
    build = json.loads((target / ".ytop/build.json").read_text())
    assert build["platforms"] == PLATS
    assert LibraryLayout(target).playlist_meta_dir(PL_TWO).is_dir()
    assert cli_main([
        "--library", str(home.root), "make-stick", str(target),
        "--image", str(image), "--upgrade", "--platforms", "beos-m68k",
    ]) == 2


def _wait(manager, job_id: str, statuses: set[str], timeout: float = 10.0):
    deadline = time.time() + timeout
    job = manager.get(job_id)
    while time.time() < deadline:
        job = manager.get(job_id)
        if job is not None and job.status in statuses:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job did not reach {statuses}: {job}")


def test_make_stick_route_and_job(tmp_path: Path, image: Path, monkeypatch):
    home, libs, library = home_library(tmp_path)
    # Generous: under a loaded full-suite run the default 2 s probe can miss
    # the home library and the page loses its playlists.
    libs.probe_timeout = 60.0
    monkeypatch.setenv("YTOP_IMAGE_ROOT", str(image))
    target = new_target(tmp_path)
    app = create_app(home.root, libraries=libs)
    client = TestClient(app)

    assert 'href="/make-stick"' in client.get("/storage").text
    page = client.get("/make-stick")
    assert page.status_code == 200
    assert "Make a stick" in page.text
    page = client.get("/make-stick", params={"target": str(target)}).text
    for plat in PLATS:
        assert f'value="{plat}"' in page
    assert "Lectures" in page and "Music" in page

    response = client.post("/make-stick", data={
        "target": str(target),
        "platforms": ["linux-x86_64", "windows-x86_64"],
        "playlists": [PL_ONE],
        "label": "Web stick",
    }, follow_redirects=False)
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert location.startswith("/make-stick/")
    job_id = location.rsplit("/", 1)[1]
    job = _wait(app.state.jobs, job_id, {"done", "failed", "cancelled"})
    assert job.status == "done", job.error
    assert job.kind == "make_stick"
    assert job.percent == 1.0
    assert "Web stick" in client.get(location).text
    assert read_library(target).label == "Web stick"
    assert LibraryLayout(target).playlist_meta_dir(PL_ONE).is_dir()
    assert client.get(f"/api/jobs/{job_id}").json()["kind"] == "make_stick"
    assert job_id in client.get("/downloads").text

    # Now it has YTOP: the page offers an upgrade that leaves the library be.
    page = client.get("/make-stick", params={"target": str(target)}).text
    assert "won’t change the library" in page
    response = client.post("/make-stick", data={
        "target": str(target), "platforms": ["linux-x86_64"],
    }, follow_redirects=False)
    assert response.status_code == 303
    job = _wait(
        app.state.jobs, response.headers["location"].rsplit("/", 1)[1],
        {"done", "failed", "cancelled"},
    )
    assert job.status == "done", job.error
    assert LibraryLayout(target).playlist_meta_dir(PL_ONE).is_dir()

    # Bad input is refused on the form page, not with a crash.
    bad = client.post("/make-stick", data={
        "target": str(tmp_path / "missing"), "platforms": ["linux-x86_64"],
    })
    assert bad.status_code == 400


def test_make_stick_upgrade_summary_counts_the_ticked_platforms(
    tmp_path: Path, image: Path, monkeypatch,
):
    """The upgrade page ticks the platforms the stick already has and counts
    its summary for those, not for every platform the image can give."""
    home, libs, library = home_library(tmp_path)
    target = new_target(tmp_path)
    make(target, image, library, platforms=["linux-x86_64"], label="Old")
    newer = second_image(tmp_path)
    monkeypatch.setenv("YTOP_IMAGE_ROOT", str(newer))
    client = TestClient(create_app(home.root, libraries=libs))

    def summary(platforms: list[str]) -> str:
        return ms.upgrade_summary(ms.plan_make(
            target, newer, library, mode="upgrade", platforms=platforms,
        ))

    assert summary(["linux-x86_64"]) != summary(PLATS)
    page = client.get("/make-stick", params={"target": str(target)}).text
    assert 'value="linux-x86_64" checked' in page
    assert 'value="macos-arm64">' in page and 'value="windows-x86_64">' in page
    assert summary(["linux-x86_64"]) in page
    assert summary(PLATS) not in page
    assert "Counted for linux-x86_64," in page

    page = client.get("/make-stick", params={
        "target": str(target), "platforms": ["windows-x86_64", "linux-x86_64"],
    }).text
    assert summary(["linux-x86_64", "windows-x86_64"]) in page
    assert 'value="windows-x86_64" checked' in page
    assert "Counted for linux-x86_64, windows-x86_64," in page


@pytest.mark.parametrize("recover_crash_at", [1, 2, 3, 5, 7, 8])
def test_interrupted_recovery_still_restores_old(
    tmp_path: Path, image: Path, monkeypatch, recover_crash_at: int,
):
    """A second interruption, during the rollback itself, is repaired by the
    next one: never a mix of old app and new pieces, never a lost piece."""
    home, libs, library = home_library(tmp_path)
    target = _used_stick(tmp_path, image, library)
    before = _library_hashes(target)
    old_build = (target / ".ytop/build.json").read_bytes()
    newer = second_image(tmp_path)
    real_place = ms._place
    calls = {"n": 0, "crash": 9}

    def crashing(src: Path, dst: Path) -> None:
        calls["n"] += 1
        if calls["n"] == calls["crash"]:
            raise Crash()
        real_place(src, dst)

    monkeypatch.setattr(ms, "_place", crashing)
    with pytest.raises(Crash):  # all old pieces out, three new ones in
        _plan2_swap(target, newer, PLATS)
    calls.update(n=0, crash=recover_crash_at)
    with pytest.raises(Crash):
        ms.recover_target(target)
    monkeypatch.setattr(ms, "_place", real_place)
    # Until the rollback finishes, app stays missing (so it is resumed).
    assert not (target / ".ytop/app").exists()
    assert (target / ".ytop.old").is_dir()

    ms.inspect_target(target)
    yt = target / ".ytop"
    assert (yt / "build.json").read_bytes() == old_build
    assert (yt / "app/ytop/cli.py").is_file()
    assert not (yt / "app/ytop/NEW_APP").exists()
    assert (yt / "boot.py").read_bytes() == (image / "boot.py").read_bytes()
    assert sorted(p.name for p in (yt / "runtime").iterdir()) == sorted(PLATS)
    assert sorted(p.name for p in (yt / "lib").iterdir()) == sorted(PLATS + ["common"])
    assert not (target / ".ytop.old").exists()
    assert not (target / ".ytop.making").exists()
    assert _library_hashes(target) == before


def test_new_stick_never_overwrites_foreign_files(tmp_path: Path, image: Path):
    home, libs, library = home_library(tmp_path)
    target = new_target(tmp_path)
    (target / "README.txt").write_text("my own notes\n")
    with pytest.raises(ms.MakeStickError, match="README.txt"):
        ms.plan_make(target, image, library, platforms=["linux-x86_64"])
    assert (target / "README.txt").read_text() == "my own notes\n"
    (target / "README.txt").unlink()
    make(target, image, library, platforms=["linux-x86_64"], label="A")
    # An upgrade may replace YTOP's own start files.
    ms.plan_make(target, image, library, platforms=["linux-x86_64"])


def test_new_stick_refuses_damaged_library_and_bad_folders(
    tmp_path: Path, image: Path, monkeypatch,
):
    home, libs, library = home_library(tmp_path)
    damaged = new_target(tmp_path, "damaged")
    (damaged / ".ytop").mkdir()
    (damaged / ".ytop/library.json").write_text("{not json")
    with pytest.raises(ms.MakeStickError, match="damaged"):
        ms.plan_make(damaged, image, library, platforms=["linux-x86_64"])
    media_only = new_target(tmp_path, "media-only")
    (media_only / "YTOP Library/List").mkdir(parents=True)
    (media_only / "YTOP Library/List/01 a.webm").write_bytes(b"x")
    with pytest.raises(ms.MakeStickError, match="damaged"):
        ms.plan_make(media_only, image, library, platforms=["linux-x86_64"])

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for bad in (Path("/"), tmp_path / "home", tmp_path / "missing"):
        with pytest.raises(ms.MakeStickError):
            ms.plan_make(bad, image, library, platforms=["linux-x86_64"])


def test_fat32_limit_and_space_margin(tmp_path: Path, image: Path, monkeypatch):
    home, libs, library = home_library(tmp_path)
    target = new_target(tmp_path)
    monkeypatch.setattr(ms.fsinfo, "fs_info", lambda path: FsInfo("vfat", 500))
    with pytest.raises(ms.MakeStickError, match="FAT32"):
        ms.plan_make(target, image, library, platforms=["linux-x86_64"],
                     playlists=[PL_ONE])
    monkeypatch.setattr(ms.fsinfo, "fs_info", lambda path: FsInfo("exfat", None))
    plan = ms.plan_make(target, image, library, platforms=["linux-x86_64"])
    # Exactly the bytes free is not enough: clusters and a margin count.
    monkeypatch.setattr(ms, "_disk_usage", lambda path: (plan.bytes_needed, 10**12))
    with pytest.raises(ms.MakeStickError, match="space"):
        ms.plan_make(target, image, library, platforms=["linux-x86_64"])


def test_lock_blocks_other_process_and_cleanup(tmp_path: Path, image: Path, monkeypatch):
    home, libs, library = home_library(tmp_path)
    target = new_target(tmp_path)
    (target / ".ytop.making").mkdir()
    (target / ".README.txt.ytop-tmp").write_text("half")
    lock = target / ".ytop.lock"
    lock.write_text(json.dumps({"pid": 4242, "started": None}))
    monkeypatch.setattr(ms, "_pid_alive", lambda pid: True)
    info = ms.inspect_target(target)  # another live process is making it
    assert (target / ".ytop.making").is_dir()
    assert any("interrupted" in w for w in info.warnings)
    plan = ms.plan_make(target, image, library, platforms=["linux-x86_64"])
    with pytest.raises(ms.MakeStickError, match="another YTOP") as refused:
        ms.run_make(plan, image, library, lambda update: None)
    # A recycled pid looks alive forever: say how to get out of it.
    assert "delete the file .ytop.lock" in str(refused.value)
    assert "no other YTOP is running" in str(refused.value)

    monkeypatch.setattr(ms, "_pid_alive", lambda pid: False)  # it died
    ms.inspect_target(target)
    assert not (target / ".ytop.making").exists()
    assert not (target / ".README.txt.ytop-tmp").exists()
    ms.run_make(plan, image, library, lambda update: None)
    assert not lock.exists()
    assert read_library(target) is not None


def test_check_folder_refuses_system_roots_only(tmp_path: Path, monkeypatch):
    with pytest.raises(ms.MakeStickError, match="root"):
        ms._check_folder(Path("/"))
    seen: list[Path] = []

    def fake(path):
        seen.append(path)
        return False

    monkeypatch.setattr(ms.fsinfo, "refused_root", fake)
    ms._check_folder(tmp_path)  # a drive root the helper allows passes
    assert seen == [tmp_path]


def test_cluster_size_on_windows_asks_the_drive(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ms.sys, "platform", "win32")
    monkeypatch.setattr(ms, "_windows_cluster_size", lambda path: 4096)
    assert ms._cluster_size(tmp_path) == 4096
    monkeypatch.setattr(ms, "_windows_cluster_size", lambda path: None)
    assert ms._cluster_size(tmp_path) == ms.DEFAULT_CLUSTER

    def broken(path):
        raise OSError("no such drive")

    monkeypatch.setattr(ms, "_windows_cluster_size", broken)
    assert ms._cluster_size(tmp_path) == ms.DEFAULT_CLUSTER


def test_windows_cluster_size_is_none_elsewhere(tmp_path: Path):
    if ms.sys.platform != "win32":
        assert ms._windows_cluster_size(tmp_path) is None
    else:  # pragma: no cover - exercised on the Windows CI runner
        assert (ms._windows_cluster_size(tmp_path) or 0) >= 512


def test_cli_make_stick_progress_lines(capsys):
    """A long copy prints how far it has got, not just the phase name."""
    from ytop.cli import make_stick_progress_printer

    now = [0.0]
    show = make_stick_progress_printer(clock=lambda: now[0], every=15.0)
    mib = 1024 * 1024

    def update(phase, done_mib, files, label):
        show(ms.MakeProgress(phase, done_mib * mib, files, 1000 * mib, 10000, label))

    update("copying", 0, 0, "Copying YTOP…")
    now[0] = 5.0
    update("copying", 10, 100, "Copying YTOP…")   # too soon: nothing
    now[0] = 16.0
    update("copying", 250, 2500, "Copying YTOP…")
    now[0] = 20.0
    update("verifying", 1000, 10000, "Checking the copy…")
    now[0] = 21.0
    update("done", 1000, 10000, "")
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "  Copying YTOP…",
        "    25% — 250.0 MiB of 1000.0 MiB, 2500 of 10000 files",
        "  Checking the copy…",
    ]


def test_readme_drive_name_for_a_drive_root():
    """A Windows drive root has no folder name; the Linux line must not
    pretend the library label is the mount folder."""
    from pathlib import PurePosixPath, PureWindowsPath

    assert ms.readme_drive_name(PurePosixPath("/run/media/me/YTOP_16GB")) == "YTOP_16GB"
    assert ms.readme_drive_name(PureWindowsPath("E:/")) == "<stick name>"


# --- incremental upgrade planning -----------------------------------------


def _stick_with_manifest(
    tmp_path: Path, image: Path, library: Library, platforms=PLATS,
) -> Path:
    """A stick made by run_make, given the manifest a later task will write."""
    target = new_target(tmp_path)
    make(target, image, library, platforms=platforms)
    entries = read_manifest(image / MANIFEST_NAME)
    assert entries is not None
    write_manifest(target / ".ytop" / MANIFEST_NAME, for_platforms(entries, platforms))
    return target


def _changed_image(tmp_path: Path, rel: str, platforms=PLATS) -> Path:
    """A second build where only ``rel`` differs (manifest consistent)."""
    yt = make_fake_image(tmp_path / "img2", platforms)
    (yt / rel).write_text("changed\n")
    refresh_manifest(yt, platforms)
    return yt


def test_fixture_manifests_match_their_files(tmp_path: Path, image: Path):
    for yt in (image, second_image(tmp_path)):
        rels = [rel for rel, _size in ms.image_files(yt, PLATS)]
        assert read_manifest(yt / MANIFEST_NAME) == hash_tree(yt, rels)
    assert "app/ytop/NEW_APP" in read_manifest(tmp_path / "img2/.ytop" / MANIFEST_NAME)  # type: ignore[operator]


def test_upgrade_plan_is_empty_for_the_same_build(tmp_path: Path, image: Path):
    home, libs, library = home_library(tmp_path)
    target = _stick_with_manifest(tmp_path, image, library)
    plan = ms.plan_make(target, image, library, platforms=PLATS)
    assert plan.mode == "upgrade"
    assert plan.diff is not None
    assert plan.diff.copy == []
    assert plan.diff.remove == []
    assert plan.diff.unchanged > 0
    assert plan.bytes_needed < 64 * 1024


def test_upgrade_plan_copies_only_changed_files(tmp_path: Path, image: Path):
    home, libs, library = home_library(tmp_path)
    target = _stick_with_manifest(tmp_path, image, library)
    entries = read_manifest(image / MANIFEST_NAME)
    assert entries is not None
    victim = next(r for r in sorted(entries) if r.startswith("lib/common/"))
    newer = _changed_image(tmp_path, victim)
    plan = ms.plan_make(target, newer, library, platforms=PLATS)
    assert plan.diff is not None
    assert plan.diff.copy == [victim]
    assert [r for r, _s in plan.files] == [victim]
    assert plan.diff.copy_bytes == len("changed\n")


def test_upgrade_plan_counts_the_whole_app_when_any_app_file_changed(
    tmp_path: Path, image: Path,
):
    home, libs, library = home_library(tmp_path)
    target = _stick_with_manifest(tmp_path, image, library)
    entries = read_manifest(image / MANIFEST_NAME)
    assert entries is not None
    victim = next(r for r in sorted(entries) if r.startswith("app/"))
    newer = _changed_image(tmp_path, victim)
    plan = ms.plan_make(target, newer, library, platforms=PLATS)
    assert plan.diff is not None
    assert plan.diff.copy == [victim]
    app_rels = {r for r in entries if r.startswith("app/")}
    assert app_rels <= {r for r, _s in plan.files}
    assert plan.bytes_needed >= sum(
        (newer / r).stat().st_size for r in app_rels
    )


def test_upgrade_plan_drops_unchosen_platforms(tmp_path: Path, image: Path):
    home, libs, library = home_library(tmp_path)
    target = _stick_with_manifest(tmp_path, image, library)
    plan = ms.plan_make(target, image, library, platforms=["linux-x86_64"])
    assert plan.diff is not None
    assert plan.diff.copy == []
    assert plan.diff.remove
    assert all(
        r.startswith(("lib/macos-arm64/", "runtime/macos-arm64/",
                      "lib/windows-x86_64/", "runtime/windows-x86_64/"))
        for r in plan.diff.remove
    )
    assert any(r.startswith("runtime/macos-arm64/") for r in plan.diff.remove)


def test_upgrade_plan_without_target_manifest_copies_all_until_verified(
    tmp_path: Path, image: Path,
):
    home, libs, library = home_library(tmp_path)
    target = _stick_with_manifest(tmp_path, image, library)
    (target / ".ytop" / MANIFEST_NAME).unlink()
    plan = ms.plan_make(target, image, library, platforms=PLATS)
    assert plan.diff is not None
    wanted = ms.image_manifest(image, PLATS)
    assert sorted(plan.diff.copy) == sorted(wanted)
    assert plan.diff.checked_by_hash

    diff = ms.diff_upgrade(
        image, target / ".ytop", wanted, verify=True,
    )
    assert diff.checked_by_hash
    assert diff.copy == []
    assert diff.unchanged == len(wanted)
    assert diff.remove == []


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_new_and_upgraded_sticks_tie_build_json_to_the_manifest(
    tmp_path: Path, image: Path,
):
    home, libs, library = home_library(tmp_path)
    target = new_target(tmp_path)
    make(target, image, library, platforms=["linux-x86_64"])
    yt = target / ".ytop"
    build = json.loads((yt / "build.json").read_text())
    assert build["manifest_sha256"] == _sha(yt / MANIFEST_NAME)
    newer = second_image(tmp_path)
    plan = ms.plan_make(target, newer, library, platforms=["linux-x86_64"])
    assert plan.diff is not None and not plan.diff.checked_by_hash
    ms.run_make(plan, newer, library, lambda update: None)
    build = json.loads((yt / "build.json").read_text())
    assert build["build_id"] == "209912312359-newer00"
    assert build["manifest_sha256"] == _sha(yt / MANIFEST_NAME)
    # The next upgrade trusts the manifest again (the fast path).
    plan = ms.plan_make(target, image, library, platforms=["linux-x86_64"])
    assert plan.diff is not None and not plan.diff.checked_by_hash


def test_upgrade_hashes_after_a_plan2_upgrade_left_the_manifest(
    tmp_path: Path, image: Path,
):
    """An older (pre-manifest) YTOP swaps lib/runtime/build.json but leaves manifest.sha256
    describing the old files: the next upgrade must not trust it."""
    home, libs, library = home_library(tmp_path)
    target = new_target(tmp_path)
    make(target, image, library, platforms=PLATS)
    yt = target / ".ytop"
    victim = _rels(image, "lib/common/")[0]
    size = (yt / victim).stat().st_size
    (yt / victim).write_bytes(b"P" * size)  # same size, other bytes
    build = json.loads((yt / "build.json").read_text())
    del build["manifest_sha256"]  # an older build.json has no such field
    (yt / "build.json").write_text(json.dumps(build, indent=2) + "\n")

    plan = ms.plan_make(target, image, library, platforms=PLATS)
    assert plan.diff is not None and plan.diff.checked_by_hash
    ms.run_make(plan, image, library, lambda update: None)
    assert (yt / victim).read_bytes() == (image / victim).read_bytes()
    source = read_manifest(image / MANIFEST_NAME)
    assert source is not None
    assert hash_tree(yt, sorted(ms._target_files(yt))) == source
    assert json.loads((yt / "build.json").read_text())["manifest_sha256"] == (
        _sha(yt / MANIFEST_NAME)
    )


def test_a_source_with_a_stale_manifest_is_hashed(tmp_path: Path, image: Path):
    """A stick an older (pre-manifest) YTOP upgraded, used as the source: its manifest no
    longer describes its files, so the image is hashed instead."""
    home, libs, library = home_library(tmp_path)
    victim = _rels(image, "lib/common/")[0]
    size = (image / victim).stat().st_size
    (image / victim).write_bytes(b"Q" * size)
    build = json.loads((image / "build.json").read_text())
    build.pop("manifest_sha256", None)
    (image / "build.json").write_text(json.dumps(build))
    target = new_target(tmp_path)
    make(target, image, library, platforms=PLATS)
    assert (target / ".ytop" / victim).read_bytes() == b"Q" * size


def test_upgrade_hashes_when_the_manifest_was_edited(tmp_path: Path, image: Path):
    home, libs, library = home_library(tmp_path)
    target = new_target(tmp_path)
    make(target, image, library, platforms=PLATS)
    yt = target / ".ytop"
    with open(yt / MANIFEST_NAME, "a", encoding="utf-8") as handle:
        handle.write(f"{'0' * 64}  app/ytop/extra.py\n")
    wanted = ms.image_manifest(image, PLATS)
    assert ms.diff_upgrade(image, yt, wanted, verify=False).checked_by_hash


def test_upgrade_verify_catches_a_wrong_size(tmp_path: Path, image: Path):
    home, libs, library = home_library(tmp_path)
    target = _stick_with_manifest(tmp_path, image, library)
    wanted = ms.image_manifest(image, PLATS)
    victim = next(r for r in sorted(wanted) if r.startswith("app/"))
    with open(target / ".ytop" / victim, "ab") as handle:
        handle.write(b"x")
    plain = ms.diff_upgrade(image, target / ".ytop", wanted, verify=False)
    assert plain.copy == []
    checked = ms.diff_upgrade(image, target / ".ytop", wanted, verify=True)
    assert checked.copy == [victim]


def test_upgrade_needs_space_only_for_changes(
    tmp_path: Path, image: Path, monkeypatch,
):
    home, libs, library = home_library(tmp_path)
    target = _stick_with_manifest(tmp_path, image, library)
    entries = read_manifest(image / MANIFEST_NAME)
    assert entries is not None
    victim = next(r for r in sorted(entries) if r.startswith("lib/common/"))
    newer = _changed_image(tmp_path, victim)
    free = len("changed\n") + 1024 * 1024 + ms.SPACE_MARGIN
    monkeypatch.setattr(ms, "_disk_usage", lambda path: (free, free * 2))
    plan = ms.plan_make(target, newer, library, platforms=PLATS)
    assert plan.mode == "upgrade"
    other = new_target(tmp_path, "fresh")
    with pytest.raises(ms.MakeStickError, match="not enough space"):
        ms.plan_make(other, newer, library, platforms=PLATS)


def test_upgrade_plan_removes_strays(tmp_path: Path, image: Path):
    home, libs, library = home_library(tmp_path)
    target = _stick_with_manifest(tmp_path, image, library)
    stray = target / ".ytop/app/ytop/__pycache__/x.cpython-312.pyc"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"junk")
    plan = ms.plan_make(target, image, library, platforms=PLATS)
    assert plan.diff is not None
    assert plan.diff.remove == ["app/ytop/__pycache__/x.cpython-312.pyc"]


def test_image_manifest_hashes_a_plan2_source(tmp_path: Path, image: Path):
    (image / MANIFEST_NAME).unlink()
    rels = [rel for rel, _size in ms.image_files(image, PLATS)]
    assert ms.image_manifest(image, PLATS) == hash_tree(image, rels)


def test_cli_upgrade_prints_the_summary(tmp_path: Path, image: Path, capsys):
    home, libs, library = home_library(tmp_path)
    target = _stick_with_manifest(tmp_path, image, library)
    argv = [
        "--library", str(home.root), "make-stick", str(target),
        "--image", str(image), "--upgrade", "--platforms", "all",
    ]
    assert cli_main(argv) == 0
    out = capsys.readouterr().out
    assert "Upgrade: 0 files to write (0 B), 0 to remove, " in out
    assert " unchanged." in out

    (target / ".ytop" / MANIFEST_NAME).unlink()
    assert cli_main(argv) == 0
    out = capsys.readouterr().out
    assert (
        "Upgrade: this stick has no file list yet, so every file is checked "
        "first (slower, once)."
    ) in out


def test_upgrade_page_shows_the_plan(tmp_path: Path, image: Path, monkeypatch):
    home, libs, library = home_library(tmp_path)
    monkeypatch.setenv("YTOP_IMAGE_ROOT", str(image))
    entries = read_manifest(image / MANIFEST_NAME)
    assert entries is not None
    victim = next(r for r in sorted(entries) if r.startswith("lib/common/"))
    newer = _changed_image(tmp_path, victim)
    target = _stick_with_manifest(tmp_path, image, library)
    monkeypatch.setenv("YTOP_IMAGE_ROOT", str(newer))
    app = create_app(home.root, libraries=libs)
    page = TestClient(app).get("/make-stick", params={"target": str(target)}).text
    assert "1 file to write" in page
    assert "0 to remove" in page
    assert "unchanged" in page


# --- incremental upgrade switch ----------------------------------------------


def _owned_state(target: Path) -> dict[str, str]:
    """rel -> sha256 of every image-owned file on the stick, root files too."""
    yt = target / ".ytop"
    rels = sorted(ms._target_files(yt))
    state = hash_tree(yt, rels)
    for name in ("build.json", MANIFEST_NAME):
        if (yt / name).is_file():
            state[name] = hashlib.sha256((yt / name).read_bytes()).hexdigest()
    return state


def _owned_dirs(target: Path) -> set[str]:
    """Every folder under the stick's app/, lib/ and runtime/, as spelled.
    A leftover empty folder is an importable namespace package."""
    yt = target / ".ytop"
    return {
        p.relative_to(yt).as_posix()
        for part in ("app", "lib", "runtime") if (yt / part).is_dir()
        for p in (yt / part).rglob("*") if p.is_dir()
    }


def _new_build(tmp_path: Path, name: str = "img2") -> Path:
    """A fresh build of the fake image with its own build_id (manifest kept
    consistent by the caller via ``refresh_manifest``)."""
    yt = make_fake_image(tmp_path / name, PLATS)
    build = json.loads((yt / "build.json").read_text())
    build["build_id"] = "209912312359-newer00"
    (yt / "build.json").write_text(json.dumps(build))
    return yt


def _rels(yt: Path, prefix: str) -> list[str]:
    entries = read_manifest(yt / MANIFEST_NAME)
    assert entries is not None
    return sorted(r for r in entries if r.startswith(prefix))


def test_upgrade_writes_only_changed_files(tmp_path: Path, image: Path):
    home, libs, library = home_library(tmp_path)
    target = new_target(tmp_path)
    make(target, image, library, platforms=PLATS)
    yt = target / ".ytop"
    newer = _new_build(tmp_path)
    app_victim = _rels(newer, "app/ytop/")[0]
    lib_victim = _rels(newer, "lib/common/")[0]
    (newer / app_victim).write_text("changed app\n")
    (newer / lib_victim).write_text("changed lib\n")
    refresh_manifest(newer)

    def stamp(rel: str) -> tuple[int, int]:
        info = os.stat(yt / rel)
        return info.st_ino, info.st_mtime_ns

    others = [
        rel for rel in sorted(ms._target_files(yt))
        if rel.startswith(("lib/", "runtime/")) and rel != lib_victim
    ]
    assert others
    before = {rel: stamp(rel) for rel in others}
    time.sleep(0.01)
    report = ms.run_make(
        ms.plan_make(target, newer, library, platforms=PLATS),
        newer, library, lambda update: None,
    )
    assert report.mode == "upgrade"
    assert {rel: stamp(rel) for rel in others} == before
    assert (yt / app_victim).read_text() == "changed app\n"
    assert (yt / lib_victim).read_text() == "changed lib\n"
    assert json.loads((yt / "build.json").read_text())["build_id"] == (
        "209912312359-newer00"
    )
    source = read_manifest(newer / MANIFEST_NAME)
    assert source is not None
    assert read_manifest(yt / MANIFEST_NAME) == for_platforms(source, PLATS)
    assert hash_tree(yt, sorted(ms._target_files(yt))) == for_platforms(source, PLATS)
    assert not (target / ms.OLD).exists() and not (target / ms.MAKING).exists()


def _crash_scenario(tmp_path: Path, image: Path, library: Library):
    """A stick and a newer build that changes app, lib, boot.py, adds a lib
    file and drops a runtime file: every kind of switch step."""
    target = new_target(tmp_path)
    make(target, image, library, platforms=PLATS)
    newer = _new_build(tmp_path)
    (newer / _rels(newer, "app/ytop/")[0]).write_text("changed app\n")
    (newer / _rels(newer, "lib/common/")[0]).write_text("changed lib\n")
    (newer / "lib/common/added_pkg").mkdir()
    (newer / "lib/common/added_pkg/new.py").write_text("added\n")
    (newer / _rels(newer, "runtime/linux-x86_64/")[-1]).unlink()
    with open(newer / "boot.py", "a", encoding="utf-8") as handle:
        handle.write("# newer boot\n")
    refresh_manifest(newer)
    return target, newer


# app out, lib aside (changed + dropped), lib in (changed + added),
# boot.py/build.json/manifest backed up and replaced, app in.
SWITCH_RENAMES = 1 + 2 + 2 + 6 + 1


@pytest.mark.parametrize("crash_at", range(1, SWITCH_RENAMES + 1))
def test_upgrade_crash_at_every_switch_step_rolls_back(
    tmp_path: Path, image: Path, monkeypatch, crash_at: int,
):
    """A crash just before any rename of the switch leaves a stick that the
    rollback returns exactly to the old build."""
    from ytop.stick import boot

    home, libs, library = home_library(tmp_path)
    target, newer = _crash_scenario(tmp_path, image, library)
    before = _owned_state(target)
    dirs_before = _owned_dirs(target)
    library_before = (target / ".ytop/library.json").read_bytes()
    real_place = ms._place
    calls = {"n": 0}

    def crashing(src: Path, dst: Path) -> None:
        calls["n"] += 1
        if calls["n"] == crash_at:
            raise Crash()
        real_place(src, dst)

    monkeypatch.setattr(ms, "_place", crashing)
    plan = ms.plan_make(target, newer, library, platforms=PLATS)
    with pytest.raises(Crash):
        ms.run_make(plan, newer, library, lambda update: None)
    monkeypatch.setattr(ms, "_place", real_place)

    if crash_at == 1:  # before app stepped aside: nothing was switched yet
        assert not boot.switch_interrupted(target)
        assert not boot.rollback_upgrade(target)
    else:
        assert boot.switch_interrupted(target)
        assert boot.rollback_upgrade(target)
        assert not (target / ms.OLD).exists()
    assert _owned_state(target) == before
    # No folder the switch added is left behind (empty: a namespace package).
    assert _owned_dirs(target) == dirs_before
    assert (target / ".ytop/library.json").read_bytes() == library_before
    # The next make-stick (or inspect) clears the staging leftovers.
    ms.inspect_target(target)
    assert not (target / ms.OLD).exists() and not (target / ms.MAKING).exists()
    assert _owned_state(target) == before


def test_upgrade_switch_rename_count(tmp_path: Path, image: Path, monkeypatch):
    """Pins SWITCH_RENAMES, so the crash test above covers every rename."""
    home, libs, library = home_library(tmp_path)
    target, newer = _crash_scenario(tmp_path, image, library)
    real_place = ms._place
    calls: list[tuple[Path, Path]] = []

    def counting(src: Path, dst: Path) -> None:
        calls.append((src, dst))
        real_place(src, dst)

    monkeypatch.setattr(ms, "_place", counting)
    ms.run_make(
        ms.plan_make(target, newer, library, platforms=PLATS),
        newer, library, lambda update: None,
    )
    assert len(calls) == SWITCH_RENAMES
    assert calls[0] == (target / ".ytop/app", target / ms.OLD / "app")
    assert calls[-1][1] == target / ".ytop/app"
    source = read_manifest(newer / MANIFEST_NAME)
    assert source is not None
    yt = target / ".ytop"
    assert hash_tree(yt, sorted(ms._target_files(yt))) == for_platforms(source, PLATS)


def test_upgrade_crash_after_the_commit_keeps_the_new_build(
    tmp_path: Path, image: Path, monkeypatch,
):
    from ytop.stick import boot

    home, libs, library = home_library(tmp_path)
    target, newer = _crash_scenario(tmp_path, image, library)
    real_place = ms._place

    def crash_after_app(src: Path, dst: Path) -> None:
        real_place(src, dst)
        if dst == target / ".ytop/app":
            raise Crash()

    monkeypatch.setattr(ms, "_place", crash_after_app)
    with pytest.raises(Crash):
        ms.run_make(
            ms.plan_make(target, newer, library, platforms=PLATS),
            newer, library, lambda update: None,
        )
    monkeypatch.setattr(ms, "_place", real_place)
    assert not boot.switch_interrupted(target)
    assert not boot.rollback_upgrade(target)
    ms.inspect_target(target)
    assert not (target / ms.OLD).exists() and not (target / ms.MAKING).exists()
    source = read_manifest(newer / MANIFEST_NAME)
    assert source is not None
    yt = target / ".ytop"
    assert hash_tree(yt, sorted(ms._target_files(yt))) == for_platforms(source, PLATS)


def test_upgrade_error_in_the_switch_rolls_back_at_once(
    tmp_path: Path, image: Path, monkeypatch,
):
    home, libs, library = home_library(tmp_path)
    target, newer = _crash_scenario(tmp_path, image, library)
    before = _owned_state(target)
    real_place = ms._place
    calls = {"n": 0}

    def failing(src: Path, dst: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 6:
            raise OSError("the stick went away")
        real_place(src, dst)

    monkeypatch.setattr(ms, "_place", failing)
    with pytest.raises(OSError):
        ms.run_make(
            ms.plan_make(target, newer, library, platforms=PLATS),
            newer, library, lambda update: None,
        )
    monkeypatch.setattr(ms, "_place", real_place)
    assert _owned_state(target) == before
    assert not (target / ms.OLD).exists() and not (target / ms.MAKING).exists()


def test_rollback_removes_a_manifest_the_old_stick_never_had(
    tmp_path: Path, image: Path, monkeypatch,
):
    """A pre-manifest stick must not keep the new manifest after a
    rollback: it would vouch for files the stick does not have."""
    from ytop.stick import boot

    home, libs, library = home_library(tmp_path)
    target, newer = _crash_scenario(tmp_path, image, library)
    (target / ".ytop" / MANIFEST_NAME).unlink()
    before = _owned_state(target)
    real_place = ms._place

    def crash_before_app_in(src: Path, dst: Path) -> None:
        if dst == target / ".ytop/app":
            raise Crash()
        real_place(src, dst)

    monkeypatch.setattr(ms, "_place", crash_before_app_in)
    with pytest.raises(Crash):
        ms.run_make(
            ms.plan_make(target, newer, library, platforms=PLATS),
            newer, library, lambda update: None,
        )
    monkeypatch.setattr(ms, "_place", real_place)
    assert (target / ".ytop" / MANIFEST_NAME).exists()
    assert boot.rollback_upgrade(target)
    assert not (target / ".ytop" / MANIFEST_NAME).exists()
    assert _owned_state(target) == before


def test_interrupted_rollback_is_finished_by_the_next_one(
    tmp_path: Path, image: Path, monkeypatch,
):
    from ytop.stick import boot

    home, libs, library = home_library(tmp_path)
    target, newer = _crash_scenario(tmp_path, image, library)
    before = _owned_state(target)
    real_place = ms._place

    def crash_before_app_in(src: Path, dst: Path) -> None:
        if dst == target / ".ytop/app":
            raise Crash()
        real_place(src, dst)

    monkeypatch.setattr(ms, "_place", crash_before_app_in)
    with pytest.raises(Crash):
        ms.run_make(
            ms.plan_make(target, newer, library, platforms=PLATS),
            newer, library, lambda update: None,
        )
    monkeypatch.setattr(ms, "_place", real_place)
    # Each rollback attempt is cut at its first move, until one finishes.
    real_rename, real_replace = boot.os.rename, boot.os.replace
    crashes = 0

    def crash(src, dst):
        raise Crash()

    while crashes < 50:
        monkeypatch.setattr(boot.os, "rename", crash)
        monkeypatch.setattr(boot.os, "replace", crash)
        try:
            boot.rollback_upgrade(target)
        except Crash:
            crashes += 1
        monkeypatch.setattr(boot.os, "rename", real_rename)
        monkeypatch.setattr(boot.os, "replace", real_replace)
        assert boot.switch_interrupted(target)
        # Let exactly one move through, then cut the next attempt again.
        moves = {"n": 0}

        def one_move(src, dst, _real):
            moves["n"] += 1
            if moves["n"] > 1:
                raise Crash()
            _real(src, dst)

        monkeypatch.setattr(boot.os, "rename", lambda s, d: one_move(s, d, real_rename))
        monkeypatch.setattr(boot.os, "replace", lambda s, d: one_move(s, d, real_replace))
        try:
            done = boot.rollback_upgrade(target)
        except Crash:
            done = False
        monkeypatch.setattr(boot.os, "rename", real_rename)
        monkeypatch.setattr(boot.os, "replace", real_replace)
        if done or not boot.switch_interrupted(target):
            break
    assert crashes >= 5
    assert not boot.switch_interrupted(target)
    ms.inspect_target(target)
    assert _owned_state(target) == before
    assert not (target / ms.OLD).exists() and not (target / ms.MAKING).exists()


def test_upgrade_case_only_rename(tmp_path: Path, image: Path, monkeypatch):
    home, libs, library = home_library(tmp_path)
    (image / "lib/common/pkg").mkdir()
    (image / "lib/common/pkg/Foo.py").write_text("old Foo\n")
    refresh_manifest(image)
    target = new_target(tmp_path)
    make(target, image, library, platforms=PLATS)
    newer = _new_build(tmp_path)
    (newer / "lib/common/pkg").mkdir()
    (newer / "lib/common/pkg/foo.py").write_text("new foo\n")
    refresh_manifest(newer)
    real_place = ms._place
    roots = {target / ".ytop" / name for name in ("boot.py", "build.json", MANIFEST_NAME)}

    def fat_rename(src: Path, dst: Path) -> None:
        """FAT on Linux: a name differing only in case is the same name."""
        if dst.parent.is_dir():
            clash = [
                entry.name for entry in dst.parent.iterdir()
                if entry.name.lower() == dst.name.lower()
            ]
            if clash and (clash != [dst.name] or dst not in roots):
                raise FileExistsError(f"{dst} exists on FAT as {clash}")
        real_place(src, dst)

    monkeypatch.setattr(ms, "_place", fat_rename)
    ms.run_make(
        ms.plan_make(target, newer, library, platforms=PLATS),
        newer, library, lambda update: None,
    )
    folder = target / ".ytop/lib/common/pkg"
    assert [p.name for p in folder.iterdir()] == ["foo.py"]
    assert (folder / "foo.py").read_text() == "new foo\n"


def test_upgrade_verify_failure_changes_nothing(
    tmp_path: Path, image: Path, monkeypatch,
):
    home, libs, library = home_library(tmp_path)
    target, newer = _crash_scenario(tmp_path, image, library)
    before = _owned_state(target)
    victim = _rels(newer, "lib/common/")[0]
    real_copy = ms.copy_file

    def corrupting(src, dst, **kwargs):
        real_copy(src, dst, **kwargs)
        if Path(dst) == target / ms.MAKING / victim:
            Path(dst).write_text("bit rot\n")

    monkeypatch.setattr(ms, "copy_file", corrupting)
    plan = ms.plan_make(target, newer, library, platforms=PLATS)
    with pytest.raises(ms.MakeStickError) as failed:
        ms.run_make(plan, newer, library, lambda update: None)
    assert str(failed.value) == (
        f"copy verification failed: {victim} differs from the original; "
        "nothing was changed"
    )
    assert _owned_state(target) == before
    assert not (target / ms.OLD).exists() and not (target / ms.MAKING).exists()


def test_recover_target_still_restores_plan2_piece_swaps(
    tmp_path: Path, image: Path, monkeypatch,
):
    """A ``.ytop.old`` without a journal is a whole-piece swap: the shared
    rollback leaves it alone and recover_target restores it piece by piece."""
    from ytop.stick import boot

    home, libs, library = home_library(tmp_path)
    target = _used_stick(tmp_path, image, library)
    old_build = (target / ".ytop/build.json").read_bytes()
    newer = second_image(tmp_path)
    real_place = ms._place
    calls = {"n": 0}

    def crashing(src: Path, dst: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 8:
            raise Crash()
        real_place(src, dst)

    monkeypatch.setattr(ms, "_place", crashing)
    with pytest.raises(Crash):
        _plan2_swap(target, newer, PLATS)
    monkeypatch.setattr(ms, "_place", real_place)
    assert boot.switch_interrupted(target)
    assert not (target / ms.OLD / boot.JOURNAL).exists()
    assert not boot.rollback_upgrade(target)
    assert (target / ms.OLD).is_dir()
    notes = ms.recover_target(target)
    assert any("restored" in note for note in notes)
    assert (target / ".ytop/build.json").read_bytes() == old_build
    assert not (target / ".ytop/app/ytop/NEW_APP").exists()


def test_new_stick_is_verified_and_gets_a_manifest(
    tmp_path: Path, image: Path, monkeypatch,
):
    home, libs, library = home_library(tmp_path)
    target = new_target(tmp_path)
    make(target, image, library, platforms=["linux-x86_64"])
    entries = read_manifest(image / MANIFEST_NAME)
    assert entries is not None
    yt = target / ".ytop"
    assert read_manifest(yt / MANIFEST_NAME) == for_platforms(entries, ["linux-x86_64"])

    # Every copied file is hashed, not just the interpreters.
    victim = "app/ytop/cli.py"
    real_copy = ms.copy_file

    def corrupting(src, dst, **kwargs):
        real_copy(src, dst, **kwargs)
        if Path(dst).as_posix().endswith(victim):
            Path(dst).write_bytes(Path(dst).read_bytes()[:-1] + b"#")

    monkeypatch.setattr(ms, "copy_file", corrupting)
    other = new_target(tmp_path, "other")
    with pytest.raises(ms.MakeStickError, match=f"{victim} differs"):
        make(other, image, library, platforms=["linux-x86_64"])
    assert not (other / ".ytop").exists() and not (other / ms.MAKING).exists()


def test_progress_reports_every_phase(tmp_path: Path, image: Path):
    home, libs, library = home_library(tmp_path)
    target, newer = _crash_scenario(tmp_path, image, library)
    events: list[ms.MakeProgress] = []
    ms.run_make(
        ms.plan_make(target, newer, library, platforms=PLATS),
        newer, library, events.append,
    )
    phases = [event.phase for event in events]
    for phase in ("checking", "copying", "verifying", "switching", "done"):
        assert phase in phases, phases
        assert any(e.total_files > 0 for e in events if e.phase == phase), phase
    order = list(dict.fromkeys(phases))
    assert order == ["checking", "copying", "verifying", "switching", "done"]
    labels = {e.phase: e.label for e in events if e.label}
    assert labels["checking"] == "Checking what changed…"
    assert labels["switching"] == "Putting YTOP in place…"
    # Each phase ends with its work counted.
    for phase in ("checking", "copying", "verifying", "switching"):
        last = [e for e in events if e.phase == phase][-1]
        assert last.files == last.total_files, (phase, last)


def test_cli_make_stick_progress_prints_new_phases(capsys):
    from ytop.cli import make_stick_progress_printer

    now = [0.0]
    show = make_stick_progress_printer(clock=lambda: now[0], every=15.0)
    show(ms.MakeProgress("checking", 0, 0, 0, 11000, "Checking what changed…"))
    now[0] = 20.0
    show(ms.MakeProgress("checking", 0, 5500, 0, 11000, "Checking what changed…"))
    show(ms.MakeProgress("copying", 0, 0, 100, 3, "Copying YTOP…"))
    show(ms.MakeProgress("verifying", 0, 0, 100, 3, "Checking the copy…"))
    show(ms.MakeProgress("switching", 0, 0, 0, 8, "Putting YTOP in place…"))
    show(ms.MakeProgress("done", 100, 3, 100, 3, ""))
    assert capsys.readouterr().out.splitlines() == [
        "  Checking what changed…",
        "    5500 of 11000 files",
        "  Copying YTOP…",
        "  Checking the copy…",
        "  Putting YTOP in place…",
    ]


def test_upgrade_folder_becomes_a_file_and_back(tmp_path: Path, image: Path):
    home, libs, library = home_library(tmp_path)
    (image / "lib/common/thing").mkdir()
    (image / "lib/common/thing/a.py").write_text("in a folder\n")
    refresh_manifest(image)
    target = new_target(tmp_path)
    make(target, image, library, platforms=PLATS)
    newer = _new_build(tmp_path)
    (newer / "lib/common/thing").write_text("now a file\n")
    refresh_manifest(newer)
    ms.run_make(
        ms.plan_make(target, newer, library, platforms=PLATS),
        newer, library, lambda update: None,
    )
    assert (target / ".ytop/lib/common/thing").read_text() == "now a file\n"
    ms.run_make(
        ms.plan_make(target, image, library, platforms=PLATS),
        image, library, lambda update: None,
    )
    assert (target / ".ytop/lib/common/thing/a.py").read_text() == "in a folder\n"


def _fat_lexists(real):
    """``os.path.lexists`` as FAT answers it: names match in any case."""

    def lexists(path) -> bool:
        path = Path(path)
        if real(path):
            return True
        current = Path(path.anchor)
        for part in path.parts[1:]:
            try:
                names = os.listdir(current)
            except OSError:
                return False
            match = [n for n in names if n.lower() == part.lower()]
            if not match:
                return False
            current = current / match[0]
        return True

    return lexists


# app out, Foo.py aside, foo.py in, build.json/manifest backed up + replaced,
# app in.
CASE_RENAMES = 1 + 1 + 1 + 4 + 1


@pytest.mark.parametrize("crash_at", range(2, CASE_RENAMES + 1))
@pytest.mark.parametrize("folder", ["pkg", "Pkg"])
def test_case_only_rename_crash_rolls_back_on_fat(
    tmp_path: Path, image: Path, monkeypatch, crash_at: int, folder: str,
):
    """On FAT, ``foo.py`` "exists" while the old ``Foo.py`` is still there:
    the rollback must not take the old file away as if it were added."""
    from ytop.stick import boot

    home, libs, library = home_library(tmp_path)
    (image / "lib/common/pkg").mkdir()
    (image / "lib/common/pkg/Foo.py").write_text("old Foo\n")
    refresh_manifest(image)
    target = new_target(tmp_path)
    make(target, image, library, platforms=PLATS)
    newer = make_fake_image(tmp_path / "img2", PLATS)
    (newer / f"lib/common/{folder}").mkdir()
    (newer / f"lib/common/{folder}/foo.py").write_text("new foo\n")
    refresh_manifest(newer)
    before = _owned_state(target)
    real_place = ms._place
    calls = {"n": 0}

    def crashing(src: Path, dst: Path) -> None:
        calls["n"] += 1
        if calls["n"] == crash_at:
            raise Crash()
        real_place(src, dst)

    monkeypatch.setattr(ms, "_place", crashing)
    with pytest.raises(Crash):
        ms.run_make(
            ms.plan_make(target, newer, library, platforms=PLATS),
            newer, library, lambda update: None,
        )
    monkeypatch.setattr(ms, "_place", real_place)
    assert calls["n"] == crash_at
    real_lexists = boot.os.path.lexists
    monkeypatch.setattr(boot.os.path, "lexists", _fat_lexists(real_lexists))
    assert boot.rollback_upgrade(target)
    monkeypatch.setattr(boot.os.path, "lexists", real_lexists)
    ms.inspect_target(target)
    assert _owned_state(target) == before


def test_journal_names_where_fat_puts_a_new_file(tmp_path: Path):
    """FAT puts ``Pkg/foo.py`` into the existing ``pkg/``: the journal names
    that spelling too, and the rollback takes it away."""
    from ytop.stick import boot

    target = tmp_path / "stick"
    yt = target / ".ytop"
    (yt / "lib/common/pkg").mkdir(parents=True)
    assert ms._on_disk_spelling(yt, "lib/common/Pkg/foo.py") == "lib/common/pkg/foo.py"
    assert ms._on_disk_spelling(yt, "lib/new/x.py") == "lib/new/x.py"
    # An interrupted switch whose new file landed as FAT would put it.
    (yt / "lib/common/pkg/foo.py").write_text("new\n")
    (target / ms.OLD / "app").mkdir(parents=True)
    (target / ms.OLD / boot.JOURNAL).write_text(
        "lib/common/Pkg/foo.py\nlib/common/pkg/foo.py\n"
    )
    assert boot.rollback_upgrade(target)
    assert not (yt / "lib/common/pkg/foo.py").exists()
    assert (yt / "app").is_dir()


def _fat_resolve(path, real_lexists):
    """Where FAT finds ``path``: each name matched in any case."""
    path = Path(path)
    if not path.is_absolute() or real_lexists(path):
        return path
    current, parts = Path(path.anchor), path.parts[1:]
    for index, part in enumerate(parts):
        if real_lexists(current / part):
            current = current / part
            continue
        try:
            names = os.listdir(current)
        except OSError:
            return current.joinpath(*parts[index:])
        match = [n for n in names if n.lower() == part.lower()]
        if not match:
            return current.joinpath(*parts[index:])
        current = current / match[0]
    return current


def _simulate_fat(monkeypatch) -> None:
    """Folder names match in any case (``isdir``, ``lexists``, renames), as
    on a FAT stick; new names keep the spelling they are created with."""
    real_isdir, real_lexists = os.path.isdir, os.path.lexists
    real_rename, real_replace = os.rename, os.replace

    def where(dst):
        dst = Path(dst)
        return _fat_resolve(dst.parent, real_lexists) / dst.name

    monkeypatch.setattr(os.path, "isdir", lambda p: real_isdir(_fat_resolve(p, real_lexists)))
    monkeypatch.setattr(os.path, "lexists", _fat_lexists(real_lexists))
    monkeypatch.setattr(os, "rename", lambda s, d: real_rename(where(s), where(d)))
    monkeypatch.setattr(os, "replace", lambda s, d: real_replace(where(s), where(d)))


def _case_folder_scenario(tmp_path: Path, image: Path, library: Library):
    """A stick whose ``lib/common/Pkg/`` the newer build spells ``pkg/``."""
    (image / "lib/common/Pkg").mkdir()
    (image / "lib/common/Pkg/mod.py").write_text("old mod\n")
    refresh_manifest(image)
    target = new_target(tmp_path)
    make(target, image, library, platforms=PLATS)
    newer = _new_build(tmp_path)
    (newer / "lib/common/pkg").mkdir()
    (newer / "lib/common/pkg/mod.py").write_text("new mod\n")
    refresh_manifest(newer)
    return target, newer


def test_case_only_folder_rename_on_fat_takes_the_new_spelling(
    tmp_path: Path, image: Path, monkeypatch,
):
    """FAT's case-insensitive ``isdir`` must not send the new files into the
    old, emptied ``Pkg/``: Python imports are case-sensitive."""
    home, libs, library = home_library(tmp_path)
    target, newer = _case_folder_scenario(tmp_path, image, library)
    plan = ms.plan_make(target, newer, library, platforms=PLATS)
    with monkeypatch.context() as fat:
        _simulate_fat(fat)
        ms.run_make(plan, newer, library, lambda update: None)
    common = target / ".ytop/lib/common"
    names = {p.name for p in common.iterdir()}
    assert "pkg" in names and "Pkg" not in names
    assert [p.name for p in (common / "pkg").iterdir()] == ["mod.py"]
    assert (common / "pkg/mod.py").read_text() == "new mod\n"


# app out, Pkg/mod.py aside, pkg/mod.py in, build.json/manifest backed up +
# replaced, app in.
CASE_FOLDER_RENAMES = 1 + 1 + 1 + 4 + 1


@pytest.mark.parametrize("crash_at", range(1, CASE_FOLDER_RENAMES + 1))
def test_case_only_folder_rename_crash_rolls_back_on_fat(
    tmp_path: Path, image: Path, monkeypatch, crash_at: int,
):
    """Crash at any rename: the rollback (on FAT too) restores the old
    build exactly, the old folder spelling included."""
    from ytop.stick import boot

    home, libs, library = home_library(tmp_path)
    target, newer = _case_folder_scenario(tmp_path, image, library)
    before, dirs_before = _owned_state(target), _owned_dirs(target)
    assert "lib/common/Pkg" in dirs_before
    plan = ms.plan_make(target, newer, library, platforms=PLATS)
    real_place = ms._place
    calls = {"n": 0}

    def crashing(src: Path, dst: Path) -> None:
        calls["n"] += 1
        if calls["n"] == crash_at:
            raise Crash()
        real_place(src, dst)

    with monkeypatch.context() as fat:
        _simulate_fat(fat)
        fat.setattr(ms, "_place", crashing)
        with pytest.raises(Crash):
            ms.run_make(plan, newer, library, lambda update: None)
        fat.setattr(ms, "_place", real_place)
        assert calls["n"] == crash_at
        assert boot.rollback_upgrade(target) == (crash_at > 1)
    ms.inspect_target(target)
    assert _owned_state(target) == before
    assert _owned_dirs(target) == dirs_before


def test_case_folder_rename_count(tmp_path: Path, image: Path, monkeypatch):
    """Pins CASE_FOLDER_RENAMES, so the crash test covers every rename."""
    home, libs, library = home_library(tmp_path)
    target, newer = _case_folder_scenario(tmp_path, image, library)
    real_place = ms._place
    calls: list[Path] = []

    def counting(src: Path, dst: Path) -> None:
        calls.append(dst)
        real_place(src, dst)

    monkeypatch.setattr(ms, "_place", counting)
    ms.run_make(
        ms.plan_make(target, newer, library, platforms=PLATS),
        newer, library, lambda update: None,
    )
    assert len(calls) == CASE_FOLDER_RENAMES


def _interrupted_by_crash(tmp_path: Path, image: Path, library: Library):
    target, newer = _crash_scenario(tmp_path, image, library)
    before = _owned_state(target)
    real_place = ms._place

    def crash_before_app_in(src: Path, dst: Path) -> None:
        if dst == target / ".ytop/app":
            raise Crash()
        real_place(src, dst)

    ms._place = crash_before_app_in
    try:
        with pytest.raises(Crash):
            ms.run_make(
                ms.plan_make(target, newer, library, platforms=PLATS),
                newer, library, lambda update: None,
            )
    finally:
        ms._place = real_place
    return target, newer, before


def test_cli_upgrade_repairs_a_stick_whose_journal_is_not_utf8(
    tmp_path: Path, image: Path, capsys,
):
    home, libs, library = home_library(tmp_path)
    target, newer, _before = _interrupted_by_crash(tmp_path, image, library)
    journal = target / ms.OLD / ms.JOURNAL
    journal.write_bytes(b"lib/\xff-garbled.py\n" + journal.read_bytes())
    code = cli_main([
        "--library", str(home.root), "make-stick", str(target),
        "--image", str(newer), "--upgrade", "--platforms", "all",
    ])
    out, err = capsys.readouterr()
    assert code == 0, err
    assert "restored" in out
    source = read_manifest(newer / MANIFEST_NAME)
    assert source is not None
    yt = target / ".ytop"
    assert hash_tree(yt, sorted(ms._target_files(yt))) == source


def test_cli_make_stick_reports_a_rollback_value_error(
    tmp_path: Path, image: Path, capsys, monkeypatch,
):
    home, libs, library = home_library(tmp_path)
    target, newer, _before = _interrupted_by_crash(tmp_path, image, library)

    def garbled(_target: Path) -> bool:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(ms, "rollback_upgrade", garbled)
    with pytest.raises(ms.MakeStickError, match="could not be undone"):
        ms.recover_target(target)
    code = cli_main([
        "--library", str(home.root), "make-stick", str(target),
        "--image", str(newer), "--upgrade", "--platforms", "all",
    ])
    err = capsys.readouterr().err
    assert code == 2
    assert "make-stick:" in err and "could not be undone" in err


def test_new_stick_carries_licence_and_notices(tmp_path: Path, image: Path):
    home, libs, library = home_library(tmp_path)
    target = new_target(tmp_path)
    make(target, image, library, platforms=PLATS)
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        assert (target / ".ytop" / name).read_bytes() == (image / name).read_bytes()


def test_upgrade_carries_licence_and_notices(tmp_path: Path, image: Path):
    home, libs, library = home_library(tmp_path)
    target = _stick_with_manifest(tmp_path, image, library)
    newer = make_fake_image(tmp_path / "img2", PLATS)
    (newer / "LICENSE").write_text("licence v2\n")
    (newer / "THIRD_PARTY_NOTICES.md").write_text("notices v2\n")
    refresh_manifest(newer)
    plan = ms.plan_make(target, newer, library, platforms=PLATS)
    assert plan.mode == "upgrade"
    assert plan.diff is not None
    assert set(plan.diff.copy) == {"LICENSE", "THIRD_PARTY_NOTICES.md"}
    ms.run_make(plan, newer, library, lambda update: None)
    assert (target / ".ytop/LICENSE").read_text() == "licence v2\n"
    assert (target / ".ytop/THIRD_PARTY_NOTICES.md").read_text() == "notices v2\n"
    entries = read_manifest(target / ".ytop" / MANIFEST_NAME)
    assert entries is not None
    assert {"LICENSE", "THIRD_PARTY_NOTICES.md"} <= set(entries)
    assert not (target / ".ytop.old").exists()


def test_upgrade_adds_licence_files_to_an_older_stick(tmp_path: Path, image: Path):
    home, libs, library = home_library(tmp_path)
    target = _stick_with_manifest(tmp_path, image, library)
    entries = read_manifest(target / ".ytop" / MANIFEST_NAME)
    assert entries is not None
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        (target / ".ytop" / name).unlink()
        entries.pop(name)
    write_manifest(target / ".ytop" / MANIFEST_NAME, entries)
    plan = ms.plan_make(target, image, library, platforms=PLATS)
    assert plan.diff is not None
    assert {"LICENSE", "THIRD_PARTY_NOTICES.md"} <= set(plan.diff.copy)
    ms.run_make(plan, image, library, lambda update: None)
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        assert (target / ".ytop" / name).read_bytes() == (image / name).read_bytes()


# --- the component store -------------------------------------------------------


def test_new_stick_carries_the_images_components(tmp_path: Path, image: Path):
    from ytop import components as comp

    home, libs, library = home_library(tmp_path)
    target = new_target(tmp_path)
    make(target, image, library, platforms=["linux-x86_64", "windows-x86_64"])
    yt = target / ".ytop"
    assert comp.read_current(yt).current == comp.read_current(image).current
    assert comp.python_paths(yt) == [
        yt / p.relative_to(image) for p in comp.python_paths(image)
    ]
    assert len(comp.python_paths(yt)) == len(comp.PYTHON_COMPONENTS)
    assert "deno" not in comp.read_current(yt).current  # not seeded (R-B4)
    # Never image-owned: not in the stick's manifest.
    entries = read_manifest(yt / MANIFEST_NAME)
    assert entries is not None
    assert not [rel for rel in entries if rel.startswith("components/")]


def test_make_stick_never_copies_in_use_markers(tmp_path: Path, image: Path):
    """The source stick's running-app markers stay on the source stick."""
    from ytop import components as comp

    assert comp.write_in_use(image, comp.python_paths(image))
    try:
        home, libs, library = home_library(tmp_path)
        target = new_target(tmp_path)
        make(target, image, library, platforms=PLATS)
        assert comp.read_current(target / ".ytop").current
        assert not (target / ".ytop" / "components" / comp.IN_USE_DIR).exists()
    finally:
        comp.remove_in_use(image)
        (image / "components" / comp.IN_USE_DIR).rmdir()


def test_upgrade_never_touches_components(tmp_path: Path, image: Path):
    """An upgrade from an image with older components leaves the stick's
    newer ones (and anything else in its store) exactly as they were."""
    from test_components import add_component
    from ytop import components as comp

    home, libs, library = home_library(tmp_path)
    target = new_target(tmp_path)
    make(target, image, library, platforms=PLATS)
    yt = target / ".ytop"
    add_component(yt, "yt-dlp", "2026.12.01")
    add_component(yt, "deno", "2.10.0", platforms=PLATS)
    (yt / "components/yt-dlp/2026.12.01/stray.txt").write_text("mine\n")
    before = tree_hash(yt / "components")
    newer = second_image(tmp_path)
    add_component(newer, "yt-dlp", "2026.01.01")  # older than the stick's
    plan = ms.plan_make(target, newer, library, platforms=PLATS)
    assert not [rel for rel, _size in plan.files if rel.startswith("components/")]
    report = ms.run_make(plan, newer, library, lambda update: None)
    assert report.warnings == []
    assert json.loads((yt / "build.json").read_text())["build_id"] == (
        "209912312359-newer00"
    )
    assert tree_hash(yt / "components") == before
    assert comp.read_current(yt).current["yt-dlp"] == "2026.12.01"


def test_make_stick_carries_the_newest_components(tmp_path: Path, image: Path):
    """For each component the newer of source and target ends up current."""
    from test_components import add_component
    from ytop import components as comp

    home, libs, library = home_library(tmp_path)
    target = new_target(tmp_path)
    make(target, image, library, platforms=PLATS)
    yt = target / ".ytop"
    seeded = comp.read_current(image).current
    add_component(yt, "yt-dlp-ejs", "0.9.0")  # the stick is newer
    newer = second_image(tmp_path)
    add_component(newer, "yt-dlp", "2026.12.01")  # the image is newer
    add_component(newer, "deno", "2.10.0", platforms=PLATS)
    events: list[ms.MakeProgress] = []
    report = ms.run_make(
        ms.plan_make(target, newer, library, platforms=PLATS),
        newer, library, events.append,
    )
    assert report.warnings == []
    current = comp.read_current(yt)
    assert current.current == {
        **seeded, "yt-dlp": "2026.12.01", "yt-dlp-ejs": "0.9.0", "deno": "2.10.0",
    }
    assert current.previous["yt-dlp"] == seeded["yt-dlp"]
    for name, version in current.current.items():
        assert (yt / "components" / name / version).is_dir(), name
    for plat in PLATS:
        assert comp.deno_for(yt, plat) is not None
    assert "components" in [e.phase for e in events]
    # Done last, after the components.
    assert events[-1].phase == "done"


def test_a_failed_component_merge_keeps_the_upgrade(
    tmp_path: Path, image: Path, monkeypatch
):
    from test_components import add_component

    home, libs, library = home_library(tmp_path)
    target = new_target(tmp_path)
    make(target, image, library, platforms=PLATS)
    newer = second_image(tmp_path)
    add_component(newer, "yt-dlp", "2026.12.01")

    def broken(*_args, **_kwargs):
        raise OSError("the stick was pulled out")

    monkeypatch.setattr(ms, "merge_newest", broken)
    report = ms.run_make(
        ms.plan_make(target, newer, library, platforms=PLATS),
        newer, library, lambda update: None,
    )
    assert len(report.warnings) == 1
    assert "YTOP itself was upgraded" in report.warnings[0]
    assert "the stick was pulled out" in report.warnings[0]
    yt = target / ".ytop"
    assert json.loads((yt / "build.json").read_text())["build_id"] == (
        "209912312359-newer00"
    )
    assert (yt / "app/ytop/NEW_APP").is_file()
    assert not (target / ms.OLD).exists() and not (target / ms.MAKING).exists()


def test_cli_prints_component_warnings(
    tmp_path: Path, image: Path, monkeypatch, capsys
):
    home, libs, library = home_library(tmp_path)
    target = new_target(tmp_path)
    make(target, image, library, platforms=PLATS)
    newer = second_image(tmp_path)

    def broken(*_args, **_kwargs):
        raise OSError("boom")

    monkeypatch.setattr(ms, "merge_newest", broken)
    rc = cli_main([
        "--library", str(home.root), "make-stick", str(target),
        "--image", str(newer), "--upgrade",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "note: YTOP itself was upgraded" in out


def test_lock_never_names_the_computer(tmp_path: Path):
    """Ruling R-B8: a stick's lock records the process, never the host."""
    import socket

    target = new_target(tmp_path)
    ms._acquire_lock(target)
    try:
        text = (target / ms.LOCK).read_text()
        doc = json.loads(text)
    finally:
        ms._release_lock(target)
    assert set(doc) == {"pid", "started", "ticks"} and doc["pid"] == os.getpid()
    assert socket.gethostname() not in text


def test_lock_with_a_recycled_pid_is_stale(tmp_path: Path, monkeypatch):
    """A live pid whose start time is not the lock's is someone else."""
    from ytop.stick import boot

    target = new_target(tmp_path)
    monkeypatch.setattr(ms, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(boot, "pid_alive", lambda pid: True)
    monkeypatch.setattr(boot, "process_start_time", lambda pid=None: 1000.0)
    (target / ms.LOCK).write_text(json.dumps({"pid": 4242, "started": 1000.4}))
    assert ms._lock_live(target) and boot.lock_live(target)
    (target / ms.LOCK).write_text(json.dumps({"pid": 4242, "started": 900.0}))
    assert not ms._lock_live(target) and not boot.lock_live(target)
    # An old lock still naming a host: the host is ignored, the pid decides.
    (target / ms.LOCK).write_text(json.dumps({"pid": 4242, "host": "elsewhere"}))
    assert ms._lock_live(target) and boot.lock_live(target)
    monkeypatch.setattr(ms, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(boot, "pid_alive", lambda pid: False)
    assert not ms._lock_live(target) and not boot.lock_live(target)


def test_leftover_lock_with_our_own_recycled_pid_is_stale(
    tmp_path: Path, image: Path, monkeypatch,
):
    """A crashed make's lock whose pid is now ours (no start to compare, as
    on macOS) neither blocks a make nor counts as live (#9)."""
    from ytop.stick import boot

    monkeypatch.setattr(boot, "process_start_time", lambda pid=None: None)
    monkeypatch.setattr(boot, "process_start_ticks", lambda pid=None: None)
    home, libs, library = home_library(tmp_path)
    target = new_target(tmp_path)
    lock = target / ms.LOCK
    lock.write_text(json.dumps({"pid": os.getpid(), "started": None}))
    assert not ms._lock_live(target)
    plan = ms.plan_make(target, image, library, platforms=["linux-x86_64"])
    ms.run_make(plan, image, library, lambda update: None)
    assert not lock.exists()
    assert read_library(target) is not None
    # Our own lock, taken by this process, is live (an app update holds it).
    ms._acquire_lock(target)
    try:
        assert ms._lock_live(target)
        with pytest.raises(ms.MakeStickError, match="another YTOP"):
            ms._acquire_lock(target)
    finally:
        ms._release_lock(target)
    assert not lock.exists()


def test_two_breakers_of_one_stale_lock_never_both_win(tmp_path: Path, monkeypatch):
    """Another process breaks the same stale lock first and takes it: ours
    must not delete its fresh lock, and must report the stick busy (#9)."""
    target = new_target(tmp_path)
    lock = target / ms.LOCK
    lock.write_text(json.dumps({"pid": 4242, "started": None}))
    old = time.time() - 120
    os.utime(lock, (old, old))
    monkeypatch.setattr(ms, "_pid_alive", lambda pid: pid == 5151)
    theirs = json.dumps({"pid": 5151, "started": None})

    def rival_breaks_first() -> None:
        lock.unlink()  # it broke the stale lock...
        lock.write_text(theirs)  # ...and took its own

    with pytest.raises(ms.MakeStickError, match="another YTOP"):
        ms._acquire_lock(target, before_break=rival_breaks_first)
    assert lock.read_text() == theirs  # put back, untouched
    assert sorted(p.name for p in target.iterdir()) == [ms.LOCK]  # nothing aside

    # Without a rival, the stale lock is broken and taken.
    lock.write_text(json.dumps({"pid": 4242, "started": None}))
    ms._acquire_lock(target)
    try:
        assert json.loads(lock.read_text())["pid"] == os.getpid()
    finally:
        ms._release_lock(target)


def test_release_leaves_a_lock_another_process_took(tmp_path: Path):
    target = new_target(tmp_path)
    ms._acquire_lock(target)
    theirs = json.dumps({"pid": 5151, "started": None})
    (target / ms.LOCK).write_text(theirs)  # ours was broken and retaken
    ms._release_lock(target)
    assert (target / ms.LOCK).read_text() == theirs


def test_an_unreadable_lock_is_held_while_it_may_be_being_written(tmp_path: Path):
    """A torn (or half-written) lock: held at first, stale once old (#9)."""
    target = new_target(tmp_path)
    lock = target / ms.LOCK
    lock.write_text("")  # another process between O_EXCL and its write
    assert ms._lock_live(target)
    with pytest.raises(ms.MakeStickError, match="another YTOP"):
        ms._acquire_lock(target)
    old = time.time() - 600
    os.utime(lock, (old, old))
    assert not ms._lock_live(target)
    ms._acquire_lock(target)
    ms._release_lock(target)
    assert not lock.exists()


def test_lock_liveness_compares_start_ticks_not_the_wall_clock(
    tmp_path: Path, monkeypatch,
):
    """Linux: a wall-clock step (NTP after boot) moves a process's computed
    start time but not its start ticks; the ticks decide (#9)."""
    from ytop.stick import boot

    target = new_target(tmp_path)
    monkeypatch.setattr(ms, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(boot, "pid_alive", lambda pid: True)
    monkeypatch.setattr(boot, "process_start_ticks", lambda pid=None: 777)
    # The clock jumped an hour since the lock was written.
    monkeypatch.setattr(boot, "process_start_time", lambda pid=None: 4600.0)
    doc = {"pid": 4242, "started": 1000.0, "ticks": 777}
    (target / ms.LOCK).write_text(json.dumps(doc))
    assert ms._lock_live(target) and boot.lock_live(target)
    # Same start time but other ticks: a recycled pid.
    doc = {"pid": 4242, "started": 4600.0, "ticks": 778}
    (target / ms.LOCK).write_text(json.dumps(doc))
    assert not ms._lock_live(target) and not boot.lock_live(target)
    # Ticks unknown here (another OS read the lock): the start time decides.
    monkeypatch.setattr(boot, "process_start_ticks", lambda pid=None: None)
    assert ms._lock_live(target) and boot.lock_live(target)


def test_boot_cleanup_never_breaks_a_lock_another_process_just_took(
    tmp_path: Path, monkeypatch,
):
    """boot.py judges a lock stale, then a make takes the stick before it
    breaks it: the make's fresh lock and its folders stay (#9)."""
    from ytop.stick import boot

    target = new_target(tmp_path)
    lock = target / ms.LOCK
    stale = json.dumps({"pid": 4242, "started": None}).encode()
    lock.write_bytes(stale)
    old = time.time() - 120
    os.utime(lock, (old, old))
    seen = boot.read_lock(lock)
    theirs = json.dumps({"pid": 5151, "started": None})

    def rival_breaks_first() -> None:
        lock.unlink()
        lock.write_text(theirs)

    assert not boot.break_stale_lock(lock, seen, before=rival_breaks_first)
    assert lock.read_text() == theirs
    assert sorted(p.name for p in target.iterdir()) == [ms.LOCK]
    # The very file judged stale goes.
    lock.write_bytes(stale)
    os.utime(lock, (old, old))
    assert boot.break_stale_lock(lock, boot.read_lock(lock))
    assert not lock.exists()


def test_component_merge_reports_files_and_bytes(tmp_path: Path, image: Path):
    """The merge is the long tail of a real FAT32 make: it must count."""
    home, libs, library = home_library(tmp_path)
    target = new_target(tmp_path)
    events: list[ms.MakeProgress] = []
    ms.run_make(
        ms.plan_make(target, image, library, platforms=PLATS),
        image, library, events.append,
    )
    merge = [e for e in events if e.phase == "components"]
    assert merge
    assert merge[0].label == "Copying the YouTube parts…"
    assert merge[0].total_files > 0 and merge[0].total_bytes > 0
    last = merge[-1]
    assert last.files == last.total_files
    assert last.bytes == last.total_bytes
    from ytop import components as comp

    copied = sum(
        1 for p in (target / ".ytop" / "components").rglob("*")
        if p.is_file() and not p.name.endswith(comp.TEMP_SUFFIX)
    )
    assert last.total_files <= copied


def test_current_components_make_no_merge_phase(tmp_path: Path, image: Path):
    home, libs, library = home_library(tmp_path)
    target = new_target(tmp_path)
    make(target, image, library, platforms=PLATS)
    newer = second_image(tmp_path)
    shutil.copytree(image / "components", newer / "components", dirs_exist_ok=True)
    events: list[ms.MakeProgress] = []
    ms.run_make(
        ms.plan_make(target, newer, library, platforms=PLATS),
        newer, library, events.append,
    )
    assert not [e for e in events if e.phase == "components"]


def test_cli_make_stick_prints_the_components_phase(capsys):
    from ytop.cli import make_stick_progress_printer

    now = [0.0]
    show = make_stick_progress_printer(clock=lambda: now[0], every=15.0)
    mib = 1024 * 1024
    show(ms.MakeProgress("components", 0, 0, 80 * mib, 8202, "Copying the YouTube parts…"))
    now[0] = 16.0
    show(ms.MakeProgress("components", 40 * mib, 4000, 80 * mib, 8202,
                         "Copying the YouTube parts…"))
    assert capsys.readouterr().out.splitlines() == [
        "  Copying the YouTube parts…",
        "    50% — 40.0 MiB of 80.0 MiB, 4000 of 8202 files",
    ]


def test_a_cancelled_component_merge_is_a_warning(tmp_path: Path, image: Path):
    """Cancel during the (long) component copy: the stick is made, and the
    report says the YouTube parts come next time."""
    home, libs, library = home_library(tmp_path)
    target = new_target(tmp_path)
    seen: list[str] = []
    report = ms.run_make(
        ms.plan_make(target, image, library, platforms=PLATS),
        image, library, lambda e: seen.append(e.phase),
        cancel=lambda: "components" in seen,
    )
    assert read_library(target) is not None
    assert len(report.warnings) == 1, report.warnings
    warning = report.warnings[0]
    assert "cancelled" in warning and "next time" in warning
    assert "copied" in warning  # a new stick


def test_a_cancel_during_the_component_copy_stops_everything_left(
    tmp_path: Path, image: Path,
):
    """Cancel while the YouTube parts copy, on a new stick with playlists
    chosen: nothing after it runs (owner decision, #12).  The stick is made
    and works; the report says which playlists were not copied."""
    home, libs, library = home_library(tmp_path)
    target = new_target(tmp_path)
    seen: list[str] = []
    report = ms.run_make(
        ms.plan_make(target, image, library, platforms=PLATS, playlists=[PL_ONE]),
        image, library, lambda e: seen.append(e.phase),
        cancel=lambda: "components" in seen,
    )
    assert read_library(target) is not None
    assert "playlists" not in seen
    assert report.playlists == []
    assert not LibraryLayout(target).playlist_meta_dir(PL_ONE).exists()
    assert len(report.warnings) == 2, report.warnings
    assert "next time" in report.warnings[0]
    assert "playlist" in report.warnings[1] and "not copied" in report.warnings[1]


def test_component_copy_counts_in_what_was_written(
    tmp_path: Path, image: Path, monkeypatch,
):
    home, libs, library = home_library(tmp_path)
    with monkeypatch.context() as m:  # the same stick without the component copy
        m.setattr(ms, "merge_newest", lambda *a, **k: None)
        bare = new_target(tmp_path, "bare")
        without = ms.run_make(ms.plan_make(bare, image, library, platforms=PLATS),
                              image, library, lambda e: None)
    target = new_target(tmp_path)
    events: list[ms.MakeProgress] = []
    plan = ms.plan_make(target, image, library, platforms=PLATS)
    report = ms.run_make(plan, image, library, events.append)
    merge = [e for e in events if e.phase == "components"][-1]
    assert merge.total_bytes > 0
    assert report.bytes_copied == without.bytes_copied + merge.total_bytes
    assert report.files_copied == without.files_copied + merge.total_files
    done = events[-1]
    assert done.phase == "done"
    assert done.total_bytes == plan.bytes_needed + merge.total_bytes
    assert done.total_files == plan.total_files + merge.total_files


def test_upgrade_counts_the_component_copy(tmp_path: Path, image: Path, monkeypatch):
    from test_components import add_component

    home, libs, library = home_library(tmp_path)
    newer = second_image(tmp_path)
    add_component(newer, "yt-dlp", "2026.12.01")

    def upgrade(name: str, events: list[ms.MakeProgress]) -> ms.MakeReport:
        target = new_target(tmp_path, name)
        make(target, image, library, platforms=PLATS)
        return ms.run_make(ms.plan_make(target, newer, library, platforms=PLATS),
                           newer, library, events.append)

    with monkeypatch.context() as m:  # the same upgrade without the component copy
        m.setattr(ms, "merge_newest", lambda *a, **k: None)
        without = upgrade("bare", [])
    events: list[ms.MakeProgress] = []
    report = upgrade("stick", events)
    merge = [e for e in events if e.phase == "components"][-1]
    assert merge.total_bytes > 0
    assert report.bytes_copied == without.bytes_copied + merge.total_bytes
    assert report.files_copied == without.files_copied + merge.total_files
    done = events[-1]
    assert (done.total_bytes, done.total_files) == (report.bytes_copied, report.files_copied)


def test_make_stick_job_shows_component_warnings(tmp_path: Path, image: Path, monkeypatch):
    home, libs, library = home_library(tmp_path)
    monkeypatch.setenv("YTOP_IMAGE_ROOT", str(image))
    target = new_target(tmp_path)

    def broken(*_args, **_kwargs):
        raise OSError("the stick was pulled out")

    monkeypatch.setattr(ms, "merge_newest", broken)
    app = create_app(home.root, libraries=libs)
    client = TestClient(app)
    response = client.post("/make-stick", data={
        "target": str(target), "platforms": ["linux-x86_64"], "label": "Web stick",
    }, follow_redirects=False)
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    job = _wait(app.state.jobs, location.rsplit("/", 1)[1], {"done", "failed", "cancelled"})
    assert job.status == "done", job.error
    assert "the stick was pulled out" in job.notice
    assert "the stick was pulled out" in client.get(location).text
    assert "the stick was pulled out" in client.get("/downloads").text


def _mac_leftovers(target: Path) -> None:
    """What a Mac writes to a stick it mounts (issue #17)."""
    store = target / ".Spotlight-V100" / "Store-V2" / "UUID"
    store.mkdir(parents=True)
    (store / "live.0.indexPostings").write_bytes(b"x" * 64)
    (target / ".fseventsd").mkdir(exist_ok=True)
    (target / ".fseventsd" / "fseventsd-uuid").write_text("uuid\n")
    (target / ".fseventsd" / "0000000000123abc").write_bytes(b"log")
    (target / "._README.txt").write_bytes(b"\x00\x05\x16\x07appledouble")


def _assert_mac_markers(target: Path) -> None:
    assert (target / ".metadata_never_index").read_bytes() == b""
    assert (target / ".fseventsd" / "no_log").read_bytes() == b""


def test_new_stick_tells_macos_not_to_index_it(tmp_path: Path, image: Path):
    home, libs, library = home_library(tmp_path)
    target = new_target(tmp_path)
    make(target, image, library, platforms=["linux-x86_64"], label="A")
    _assert_mac_markers(target)
    assert sorted(p.name for p in (target / ".fseventsd").iterdir()) == ["no_log"]


def test_new_stick_over_mac_leftovers(tmp_path: Path, image: Path):
    """Spotlight's and fseventsd's folders and AppleDouble files are no
    foreign files; what a Mac wrote is cleared, ``._*`` files stay."""
    home, libs, library = home_library(tmp_path)
    target = new_target(tmp_path)
    _mac_leftovers(target)
    make(target, image, library, platforms=["linux-x86_64"], label="A")
    _assert_mac_markers(target)
    assert not (target / ".Spotlight-V100").exists()
    assert sorted(p.name for p in (target / ".fseventsd").iterdir()) == ["no_log"]
    assert (target / "._README.txt").is_file()


def test_upgrade_adds_mac_markers_and_clears_the_index(tmp_path: Path, image: Path):
    home, libs, library = home_library(tmp_path)
    target = new_target(tmp_path)
    make(target, image, library, platforms=PLATS, label="Old")
    # A stick made before the markers existed, then plugged into a Mac.
    (target / ".metadata_never_index").unlink()
    shutil.rmtree(target / ".fseventsd")
    _mac_leftovers(target)
    newer = second_image(tmp_path)
    plan = ms.plan_make(target, newer, library, platforms=PLATS)
    assert plan.mode == "upgrade"
    ms.run_make(plan, newer, library, lambda update: None)
    _assert_mac_markers(target)
    assert not (target / ".Spotlight-V100").exists()
    assert sorted(p.name for p in (target / ".fseventsd").iterdir()) == ["no_log"]
    assert (target / "._README.txt").is_file()
    assert (target / ".ytop/app/ytop/NEW_APP").is_file()


def test_on_a_mac_the_live_index_is_left_alone(
    tmp_path: Path, image: Path, monkeypatch,
):
    home, libs, library = home_library(tmp_path)
    target = new_target(tmp_path)
    make(target, image, library, platforms=PLATS, label="Old")
    _mac_leftovers(target)
    monkeypatch.setattr(ms.sys, "platform", "darwin")
    ms._write_mac_markers(target)
    _assert_mac_markers(target)
    assert (target / ".Spotlight-V100/Store-V2/UUID/live.0.indexPostings").is_file()
    assert (target / ".fseventsd" / "fseventsd-uuid").is_file()


def test_mac_markers_never_fail_a_make(tmp_path: Path, image: Path):
    home, libs, library = home_library(tmp_path)
    target = new_target(tmp_path)
    (target / ".fseventsd").write_text("not a folder\n")
    (target / ".metadata_never_index").mkdir()
    report = make(target, image, library, platforms=["linux-x86_64"], label="A")
    assert report.mode == "new"
    assert read_library(target) is not None
    assert (target / ".fseventsd").read_text() == "not a folder\n"


# --- upgrade and repair edge cases (#8) --------------------------------------


def test_upgrade_plan_without_manifest_counts_only_size_changes(
    tmp_path: Path, image: Path,
):
    """An older stick (no trusted manifest) is not sized as the whole image:
    files of the same size count as unchanged until the run hashes them."""
    home, libs, library = home_library(tmp_path)
    target = _stick_with_manifest(tmp_path, image, library)
    yt = target / ".ytop"
    (yt / MANIFEST_NAME).unlink()
    plan = ms.plan_make(target, image, library, platforms=PLATS)
    assert plan.diff is not None and plan.diff.checked_by_hash
    assert plan.files == []
    victim = _rels(image, "lib/common/")[0]
    with open(yt / victim, "ab") as handle:
        handle.write(b"x")
    plan = ms.plan_make(target, image, library, platforms=PLATS)
    assert [rel for rel, _size in plan.files] == [victim]


def test_upgrade_rechecks_space_once_verify_finds_more(
    tmp_path: Path, image: Path, monkeypatch,
):
    home, libs, library = home_library(tmp_path)
    target = _stick_with_manifest(tmp_path, image, library)
    yt = target / ".ytop"
    apps = _rels(image, "app/")
    assert len(apps) > 12
    victim = apps[0]
    with open(yt / victim, "ab") as handle:
        handle.write(b"x")  # same manifest hash, other size: only verify sees it
    before = _owned_state(target)
    mib = 1024 * 1024
    monkeypatch.setattr(ms, "_cluster_size", lambda path: mib)
    free = ms.SPACE_MARGIN + 10 * mib
    monkeypatch.setattr(ms, "_disk_usage", lambda path: (free, free * 2))
    plan = ms.plan_make(target, image, library, platforms=PLATS)
    assert plan.files == []
    with pytest.raises(ms.MakeStickError, match="not enough space"):
        ms.run_make(plan, image, library, lambda update: None)
    assert _owned_state(target) == before
    assert not (target / ms.MAKING).exists() and not (target / ms.OLD).exists()


def test_launcher_failure_after_the_upgrade_commit_is_a_warning(
    tmp_path: Path, image: Path, monkeypatch,
):
    home, libs, library = home_library(tmp_path)
    target, newer = _crash_scenario(tmp_path, image, library)

    def failing(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(ms, "_write_launchers", failing)
    plan = ms.plan_make(target, newer, library, platforms=PLATS)
    report = ms.run_make(plan, newer, library, lambda update: None)
    assert any("start files" in note for note in report.warnings)
    yt = target / ".ytop"
    assert json.loads((yt / "build.json").read_text())["build_id"] == report.build_id
    assert not (target / ms.OLD).exists() and not (target / ms.MAKING).exists()
    source = read_manifest(newer / MANIFEST_NAME)
    assert source is not None
    assert hash_tree(yt, sorted(ms._target_files(yt))) == for_platforms(source, PLATS)
    # The next upgrade writes them.
    monkeypatch.undo()
    ms.run_make(
        ms.plan_make(target, newer, library, platforms=PLATS),
        newer, library, lambda update: None,
    )
    assert (target / "start-ytop-linux.sh").read_bytes() == (
        newer / ms.LAUNCHER_TEMPLATES / "start-ytop-linux.sh"
    ).read_bytes()


def test_upgrade_of_a_library_without_ytop_is_refused_before_copying(
    tmp_path: Path, image: Path,
):
    home, libs, library = home_library(tmp_path)
    drive = make_library(tmp_path / "drive", "Drive").root
    before = sorted(p.relative_to(drive).as_posix() for p in drive.rglob("*"))
    with pytest.raises(ms.MakeStickError, match="no YTOP to upgrade"):
        ms.plan_make(drive, image, library, platforms=PLATS)
    after = sorted(p.relative_to(drive).as_posix() for p in drive.rglob("*"))
    assert after == before


def test_interrupted_upgrade_is_still_planned_as_an_upgrade(
    tmp_path: Path, image: Path, monkeypatch,
):
    """build.json is never absent mid-switch, so the refusal above never
    blocks the repair of an interrupted upgrade."""
    home, libs, library = home_library(tmp_path)
    target, newer = _crash_scenario(tmp_path, image, library)
    real_place = ms._place
    calls = {"n": 0}

    def crashing(src: Path, dst: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 4:
            raise Crash()
        real_place(src, dst)

    monkeypatch.setattr(ms, "_place", crashing)
    with pytest.raises(Crash):
        ms.run_make(
            ms.plan_make(target, newer, library, platforms=PLATS),
            newer, library, lambda update: None,
        )
    monkeypatch.setattr(ms, "_place", real_place)
    assert not (target / ".ytop/app").exists()
    plan = ms.plan_make(target, newer, library, platforms=PLATS)
    assert plan.mode == "upgrade"
    ms.run_make(plan, newer, library, lambda update: None)
    assert (target / ".ytop/app").is_dir()


def test_new_stick_rechecks_foreign_files_at_run_time(tmp_path: Path, image: Path):
    home, libs, library = home_library(tmp_path)
    target = new_target(tmp_path)
    plan = ms.plan_make(target, image, library, platforms=["linux-x86_64"])
    (target / "start-ytop-linux.sh").write_text("echo mine\n")
    with pytest.raises(ms.MakeStickError, match="start-ytop-linux.sh"):
        ms.run_make(plan, image, library, lambda update: None)
    assert (target / "start-ytop-linux.sh").read_text() == "echo mine\n"
    assert not (target / ".ytop").exists()
    assert not (target / ms.MAKING).exists()


def test_new_stick_reports_time_per_step(tmp_path: Path, image: Path):
    home, libs, library = home_library(tmp_path)
    target = new_target(tmp_path)
    plan = ms.plan_make(
        target, image, library, platforms=["linux-x86_64"], playlists=[],
    )
    report = ms.run_make(plan, image, library, lambda update: None)

    names = [phase.name for phase in report.timings]
    assert names[:4] == ["preparing", "copying", "verifying", "switching"]
    assert "tidying" in names and "registering" in names
    assert len(names) == len(set(names))
    registering = report.timings[names.index("registering")]
    assert (registering.count, registering.bytes) == (0, 0)
    assert all(phase.seconds >= 0 for phase in report.timings)
    copying = report.timings[1]
    assert copying.count == len(plan.files)
    assert copying.bytes == sum(size for _rel, size in plan.files)
    assert copying.unit == "files"


def test_upgrade_reports_time_per_step(tmp_path: Path, image: Path):
    home, libs, library = home_library(tmp_path)
    target = new_target(tmp_path)
    plan = ms.plan_make(
        target, image, library, platforms=["linux-x86_64"], playlists=[],
    )
    ms.run_make(plan, image, library, lambda update: None)
    plan = ms.plan_make(
        target, image, library, platforms=["linux-x86_64"], playlists=[],
        mode="upgrade",
    )
    report = ms.run_make(plan, image, library, lambda update: None)
    names = [phase.name for phase in report.timings]
    assert names[:2] == ["preparing", "checking"]
    assert "tidying" in names


def _copy_fixture(tmp_path: Path, count: int = 40):
    src = tmp_path / "src"
    rels = [f"d{i % 5}/f{i}.bin" for i in range(count)]
    for i, rel in enumerate(rels):
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_bytes(bytes([i % 256]) * (100 + i))
    making = tmp_path / "making"
    making.mkdir()
    events: list = []
    progress = ms._Progress(events.append, sum(100 + i for i in range(count)), count)
    return src, making, rels, progress


def test_parallel_copy_copies_everything_and_counts_it(tmp_path: Path):
    src, making, rels, progress = _copy_fixture(tmp_path)
    ms._copy_files(src, making, rels, progress, lambda: False)
    for rel in rels:
        assert (making / rel).read_bytes() == (src / rel).read_bytes()
    assert progress.files == len(rels)
    assert progress.bytes == sum((src / rel).stat().st_size for rel in rels)


def test_parallel_copy_failure_raises_and_nothing_keeps_writing(
    tmp_path: Path, monkeypatch,
):
    src, making, rels, progress = _copy_fixture(tmp_path)
    real = ms.copy_file
    calls: list[str] = []

    def flaky(source, destination, **kwargs):
        calls.append(source.name)
        if source.name == "f7.bin":
            raise OSError(28, "No space left on device")
        time.sleep(0.02)  # a slow stick: the failure is seen long before the end
        real(source, destination, **kwargs)

    monkeypatch.setattr(ms, "copy_file", flaky)
    with pytest.raises(OSError, match="No space"):
        ms._copy_files(src, making, rels, progress, lambda: False)
    done = len(calls)
    time.sleep(0.2)
    assert len(calls) == done  # the barrier held: no copy after the raise
    assert done < len(rels)  # queued copies did not start


def test_parallel_copy_honours_cancel(tmp_path: Path):
    src, making, rels, progress = _copy_fixture(tmp_path)

    def cancelled() -> bool:
        return progress.files >= 3

    with pytest.raises(ms.JobCancelled):
        ms._copy_files(src, making, rels, progress, cancelled)
    copied = sum(1 for p in making.rglob("*") if p.is_file())
    assert copied < len(rels)


def test_make_stick_page_ticks_this_computer_and_windows(
    tmp_path: Path, image: Path, monkeypatch,
):
    import ytop.web.app as web_app

    home, libs, library = home_library(tmp_path)
    target = new_target(tmp_path)
    monkeypatch.setenv("YTOP_IMAGE_ROOT", str(image))
    monkeypatch.setattr(web_app, "current_platform", lambda: "macos-arm64")
    client = TestClient(create_app(home.root, libraries=libs))
    page = client.get("/make-stick", params={"target": str(target)}).text
    assert 'value="macos-arm64" checked' in page
    assert 'value="windows-x86_64" checked' in page
    assert 'value="linux-x86_64">' in page  # offered, not ticked
    assert "Mac (Apple silicon)" in page
    assert "add more later from its Settings page" in page
    client.app.state.jobs.close()
