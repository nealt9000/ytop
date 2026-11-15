from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import ytop.libraries as libraries_module
from factories import add_playlist, make_library
from ytop.layout import LibraryLayout, init_library, read_library
from ytop.libraries import READ_ONLY_MESSAGE, LibrarySet
from ytop.web.app import create_app

# conftest replaces registry_path_default per test; keep the real one.
_REAL_REGISTRY_PATH_DEFAULT = libraries_module.registry_path_default


def _set(launch: Path | None, tmp_path: Path, detect: bool = True) -> LibrarySet:
    return LibrarySet(launch, registry_path=tmp_path / "registry.json", detect=detect)


def test_discovers_launch_and_mounted(tmp_path: Path, monkeypatch):
    launch = make_library(tmp_path / "launch", "Laptop")
    stick = tmp_path / "STICK"
    init_library(stick, "Stick")
    empty = tmp_path / "EMPTY"
    empty.mkdir()
    nested = tmp_path / "OTHER"
    init_library(nested / "YTOP", "Nested")
    monkeypatch.setattr(
        libraries_module, "_mount_candidates", lambda: [stick, empty, nested]
    )

    refs = _set(launch.root, tmp_path).libraries()

    assert [ref.label for ref in refs] == ["Laptop", "Stick", "Nested"]
    assert [ref.source for ref in refs] == ["launch", "detected", "detected"]
    assert refs[1].root == stick
    assert refs[2].root == nested / "YTOP"
    assert all(ref.online and not ref.read_only for ref in refs)
    assert refs[0].total_bytes > 0


def test_dedupe_by_id(tmp_path: Path, monkeypatch):
    launch = make_library(tmp_path / "launch", "Laptop")
    # The same volume seen through a second mount point carries the same id.
    second = tmp_path / "second-mount"
    shutil.copytree(launch.root, second)
    monkeypatch.setattr(libraries_module, "_mount_candidates", lambda: [second])
    write = tmp_path / "registry.json"
    write.write_text(json.dumps({"schema": 1, "libraries": [str(launch.root)]}))

    refs = _set(launch.root, tmp_path).libraries()

    assert len(refs) == 1
    assert refs[0].source == "launch"
    assert refs[0].root == launch.root


def test_offline_library_hidden(tmp_path: Path, monkeypatch):
    launch = make_library(tmp_path / "launch", "Laptop")
    add_playlist(launch, "PLLOCAL", "Local Lectures", [("v1", "One")])
    stick_root = tmp_path / "STICK"
    init_library(stick_root, "Stick")
    add_playlist(LibraryLayout(stick_root), "PLSTICK", "Stick Lectures", [("v2", "Two")])
    monkeypatch.setattr(libraries_module, "_mount_candidates", lambda: [stick_root])

    libs = _set(launch.root, tmp_path)
    assert len(libs.libraries()) == 2
    client = TestClient(create_app(launch.root, libraries=libs))
    page = client.get("/")
    assert "Stick Lectures" in page.text and "Local Lectures" in page.text

    shutil.rmtree(stick_root)

    # Still cached as online: pages must render the rest, never a 500.
    for url in ("/", "/storage", "/playlist/PLSTICK", "/media/PLSTICK/v2",
                "/watch/PLSTICK/v2", "/search?q=x", "/playlist/new", "/add"):
        assert client.get(url).status_code < 500, url
    assert [ref.label for ref in libs.libraries(refresh=True)] == ["Laptop"]
    page = client.get("/")
    assert page.status_code == 200
    assert "Local Lectures" in page.text
    assert "Stick Lectures" not in page.text


def test_newer_schema_read_only(tmp_path: Path):
    layout = make_library(tmp_path / "newer", "Future")
    add_playlist(layout, "PLFUTURE", "Future", [("v1", "One"), ("v2", "Two")])
    marker = json.loads(layout.marker.read_text())
    marker["schema"] = 2
    layout.marker.write_text(json.dumps(marker))
    libs = _set(layout.root, tmp_path, detect=False)

    ref = libs.default()
    assert ref.read_only is True

    client = TestClient(create_app(layout.root, libraries=libs))
    assert client.get("/playlist/PLFUTURE").status_code == 200
    response = client.post(
        "/playlist/PLFUTURE/reorder", data={"order": "v2,v1"},
        follow_redirects=False,
    )
    assert response.status_code == 409
    assert READ_ONLY_MESSAGE in response.text
    doc = json.loads((layout.playlist_meta_dir("PLFUTURE") / "playlist.json").read_text())
    assert [row["id"] for row in doc["entries"]] == ["v1", "v2"]
    api = client.post("/api/progress", json={
        "playlist_id": "PLFUTURE", "video_id": "v1", "position": 5,
    })
    assert api.status_code == 409
    assert not layout.progress_file.exists()


def test_add_folder_registers(tmp_path: Path):
    libs = _set(None, tmp_path, detect=False)
    target = tmp_path / "new-stick"

    ref = libs.add_folder(target, "New stick")

    assert ref.label == "New stick"
    assert read_library(target) is not None
    registry = json.loads((tmp_path / "registry.json").read_text())
    assert str(target.resolve()) in registry["libraries"]
    again = _set(None, tmp_path, detect=False).libraries()
    assert [(r.id, r.source) for r in again] == [(ref.id, "registered")]

    assert libs.forget(ref.id) is True
    assert _set(None, tmp_path, detect=False).libraries() == []
    assert read_library(target) is not None  # files are never touched
    assert libs.forget(ref.id) is False


def test_add_folder_rejects_nesting(tmp_path: Path):
    launch = make_library(tmp_path / "launch", "Laptop")
    libs = _set(launch.root, tmp_path, detect=False)
    try:
        libs.add_folder(launch.media_root / "inside", "Nested")
    except ValueError as exc:
        assert "overlaps" in str(exc)
    else:
        raise AssertionError("a nested library must be refused")


def test_slow_candidate_skipped(tmp_path: Path, monkeypatch):
    fast = tmp_path / "fast"
    slow = tmp_path / "slow"
    init_library(fast, "Fast")
    init_library(slow, "Slow")
    (tmp_path / "registry.json").write_text(json.dumps(
        {"schema": 1, "libraries": [str(slow), str(fast)]}
    ))
    real = libraries_module.read_library

    def sluggish(root):
        if Path(root) == slow:
            time.sleep(3)
        return real(root)

    monkeypatch.setattr(libraries_module, "read_library", sluggish)
    started = time.monotonic()
    refs = _set(None, tmp_path, detect=False).libraries()
    assert time.monotonic() - started < 3
    assert [ref.label for ref in refs] == ["Fast"]


def test_default_ref_for_path_and_set_info(tmp_path: Path):
    launch = make_library(tmp_path / "launch", "Laptop")
    libs = _set(launch.root, tmp_path, detect=False)
    other = libs.add_folder(tmp_path / "other", "Other")

    assert libs.default().label == "Laptop"
    assert libs.ref_for_path(launch.media_root / "x" / "y.webm").label == "Laptop"
    assert libs.ref_for_path(tmp_path / "elsewhere") is None

    updated = libs.set_info(other.id, label="Renamed", description="Spare stick")
    assert updated.label == "Renamed"
    assert read_library(other.root).description == "Spare stick"


def test_dead_mount_does_not_block_discovery(tmp_path: Path, monkeypatch):
    media = tmp_path / "media"
    good = media / "GOOD"
    dead = media / "DEAD"
    init_library(good, "Good")
    init_library(dead, "Dead")
    monkeypatch.setattr(libraries_module, "_mount_parents", lambda: [media])
    real_is_dir, real_resolve = Path.is_dir, Path.resolve

    def hangs(path: Path) -> bool:
        return path == dead or dead in path.parents

    def slow_is_dir(self):
        if hangs(self):
            time.sleep(3)
        return real_is_dir(self)

    def slow_resolve(self, *args, **kwargs):
        if hangs(self):
            time.sleep(3)
        return real_resolve(self, *args, **kwargs)

    real_read = libraries_module.read_library

    def slow_read(root):
        if hangs(Path(root)):
            time.sleep(3)
        return real_read(root)

    monkeypatch.setattr(Path, "is_dir", slow_is_dir)
    monkeypatch.setattr(Path, "resolve", slow_resolve)
    monkeypatch.setattr(libraries_module, "read_library", slow_read)

    started = time.monotonic()
    refs = _set(None, tmp_path).libraries()
    assert time.monotonic() - started < 3
    assert [ref.label for ref in refs] == ["Good"]


def test_windows_candidates_filter_types(monkeypatch):
    monkeypatch.setattr(libraries_module.sys, "platform", "win32")
    monkeypatch.setenv("SystemDrive", "C:")
    monkeypatch.setattr(
        libraries_module,
        "_windows_drives",
        lambda: [("C:\\", 3), ("D:\\", 2), ("E:\\", 4), ("F:\\", 5), ("G:\\", 3)],
    )
    assert [str(p) for p in libraries_module._mount_candidates()] == [
        str(Path("D:\\")),
        str(Path("G:\\")),
    ]


def test_windows_candidates_not_statted(monkeypatch):
    monkeypatch.setattr(libraries_module.sys, "platform", "win32")
    monkeypatch.setenv("SystemDrive", "C:")
    monkeypatch.setattr(libraries_module, "_windows_drives", lambda: [("D:\\", 2)])

    def boom(self):
        raise AssertionError("statted")

    monkeypatch.setattr(Path, "is_dir", boom)
    monkeypatch.setattr(Path, "exists", boom)
    assert len(libraries_module._mount_candidates()) == 1


def test_registry_path_default_windows_appdata(monkeypatch, tmp_path):
    real = _REAL_REGISTRY_PATH_DEFAULT
    monkeypatch.setattr(libraries_module.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert real() == tmp_path / "ytop" / "libraries.json"


def test_add_folder_root_rules(tmp_path: Path, monkeypatch):
    import ytop.libraries as libmod

    libs = LibrarySet(None, registry_path=tmp_path / "reg.json", detect=False)
    with pytest.raises(ValueError, match="root"):
        libs.add_folder(Path("/"), "Root")
    seen: list[Path] = []
    monkeypatch.setattr(libmod, "refused_root", lambda p: seen.append(p) or False)
    ref = libs.add_folder(tmp_path / "stick", "Stick")
    assert ref.label == "Stick"
    assert seen == [tmp_path / "stick"]
