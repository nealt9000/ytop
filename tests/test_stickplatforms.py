"""Adding a platform to a stick from its own release (a fake GitHub on
127.0.0.1; tmp dirs stand in for sticks)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import ytop.makestick as ms
from factories import library_set, make_library
from fake_releases import FakeReleases
from stick_fakes import REPO, make_fake_image
from ytop import stickplatforms as sp
from ytop import updater
from ytop.config import Config, UpdatesConfig
from ytop.manifest import MANIFEST_NAME, hash_tree, read_manifest
from ytop.web.library import Library

PLATS = ["linux-x86_64", "windows-x86_64"]
BUILD_ID = "202611200000-same000"
GH_REPO = "someone/ytop"


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


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    base = tmp_path_factory.mktemp("platforms")
    image = make_fake_image(base / "image", PLATS)
    _set_build(image, version="0.1.0", build_id=BUILD_ID)
    # The manifest hash in build.json must stay right after the edit above.
    build = json.loads((image / "build.json").read_text())
    build["manifest_sha256"] = hashlib.sha256((image / MANIFEST_NAME).read_bytes()).hexdigest()
    (image / "build.json").write_text(json.dumps(build, indent=2) + "\n")
    release = base / "release"
    _package_module().package(image, release)
    stick = base / "stick"
    stick.mkdir()
    library = Library(library_set(make_library(base / "home", "Laptop")))
    plan = ms.plan_make(stick, image, library, platforms=["linux-x86_64"], label="Stick")
    ms.run_make(plan, image, library, lambda update: None)
    return SimpleNamespace(image=image, release=release, stick=stick)


@pytest.fixture
def stick(built, tmp_path) -> Path:
    target = tmp_path / "stick"
    shutil.copytree(built.stick, target)
    return target


@pytest.fixture
def server(built):
    fake = FakeReleases()
    assets = {p.name: p.read_bytes() for p in sorted(built.release.iterdir())}
    fake.app_release(GH_REPO, "v0.1.0", assets)
    yield fake
    fake.close()


def _config(fake: FakeReleases | None) -> Config:
    base = fake.base + "/api" if fake is not None else "http://127.0.0.1:9/api"
    return Config(updates=UpdatesConfig(repo=GH_REPO, api_base=base))


def _downloads(fake: FakeReleases) -> list[str]:
    return sorted(r.path.rsplit("/", 1)[-1] for r in fake.requests if r.path.startswith("/dl/"))


def _consistent(yt: Path) -> dict[str, str]:
    """The stick's manifest, checked against its files and its build.json."""
    manifest = read_manifest(yt / MANIFEST_NAME)
    assert manifest is not None
    assert hash_tree(yt, sorted(manifest)) == manifest
    build = json.loads((yt / "build.json").read_text())
    assert build["manifest_sha256"] == hashlib.sha256((yt / MANIFEST_NAME).read_bytes()).hexdigest()
    return manifest


def test_adds_a_platform_from_the_sticks_own_release(stick: Path, built, server):
    yt = stick / ".ytop"
    assert sp.stick_platforms(yt) == ["linux-x86_64"]
    labels: list[str] = []
    result = sp.add_platforms(yt, _config(server), ["windows-x86_64"],
                              http=updater.Http(), progress=labels.append)
    assert result.status == "added", result.message
    assert result.platforms == ("windows-x86_64",)
    assert "Windows" in result.message
    assert sp.stick_platforms(yt) == ["linux-x86_64", "windows-x86_64"]
    # Only the new platform's pieces were downloaded.
    pieces = [name for name in _downloads(server) if name.endswith(".tar.gz")]
    assert pieces and all("windows-x86_64" in name for name in pieces)
    # Its files are the image's, and the stick's records agree with them.
    manifest = _consistent(yt)
    image_manifest = read_manifest(built.image / MANIFEST_NAME)
    assert image_manifest is not None
    for rel, digest in image_manifest.items():
        if rel.startswith(("runtime/windows-x86_64/", "lib/windows-x86_64/")):
            assert manifest[rel] == digest
    assert (yt / "runtime/windows-x86_64/python/python.exe").is_file()
    build = json.loads((yt / "build.json").read_text())
    assert "windows-x86_64" in build["sizes"]
    assert not (stick / sp.STAGING).exists()
    assert not (stick / ".ytop.lock").exists()
    assert labels and labels[-1] == "Putting it on the stick…"
    # The upgrade check sees a complete stick: nothing to copy.
    diff = ms.diff_upgrade(built.image, yt, ms.image_manifest(built.image, PLATS), verify=True)
    assert diff.copy == []


def test_adding_a_platform_it_has_does_nothing(stick: Path, server):
    result = sp.add_platforms(stick / ".ytop", _config(server), ["linux-x86_64"],
                              http=updater.Http())
    assert result.status == "added" and result.platforms == ()
    assert _downloads(server) == []


def test_an_unpublished_build_is_refused(stick: Path, server):
    yt = stick / ".ytop"
    before = (yt / "build.json").read_bytes()
    _set_build(yt, build_id="202611209999-local00")
    result = sp.add_platforms(yt, _config(server), ["windows-x86_64"], http=updater.Http())
    assert result.status == "failed"
    assert "not a published release" in result.message
    assert not (yt / "runtime/windows-x86_64").exists()
    (yt / "build.json").write_bytes(before)
    # No release for the version at all.
    _set_build(yt, version="0.0.1")
    result = sp.add_platforms(yt, _config(server), ["windows-x86_64"], http=updater.Http())
    assert result.status == "failed" and "not a published release" in result.message


def test_a_bad_piece_changes_nothing(stick: Path, built):
    yt = stick / ".ytop"
    before = {name: (yt / name).read_bytes() for name in ("build.json", MANIFEST_NAME)}
    fake = FakeReleases()
    try:
        assets = {p.name: p.read_bytes() for p in sorted(built.release.iterdir())}
        for name in assets:
            if name.endswith(".tar.gz") and "windows-x86_64" in name:
                assets[name] = assets[name] + b"tampered"
        fake.app_release(GH_REPO, "v0.1.0", assets)
        result = sp.add_platforms(yt, _config(fake), ["windows-x86_64"], http=updater.Http())
    finally:
        fake.close()
    assert result.status == "failed"
    assert {name: (yt / name).read_bytes() for name in before} == before
    assert not (yt / "runtime/windows-x86_64").exists()
    assert not (stick / sp.STAGING).exists()


def test_leftovers_of_a_cut_off_add_are_replaced(stick: Path, server):
    yt = stick / ".ytop"
    stray = yt / "runtime/windows-x86_64/python/stray.txt"
    stray.parent.mkdir(parents=True)
    stray.write_text("half")
    result = sp.add_platforms(yt, _config(server), ["windows-x86_64"], http=updater.Http())
    assert result.status == "added", result.message
    assert not stray.exists()
    _consistent(yt)


def test_offline_says_so(stick: Path):
    result = sp.add_platforms(stick / ".ytop", _config(None), ["windows-x86_64"],
                              http=updater.Http())
    assert result.status == "failed"
    assert "network" in result.message.lower()


def test_background_runner(stick: Path, server):
    adds = sp.PlatformAdds(lambda: _config(server), stick / ".ytop", http=updater.Http)
    assert adds.available and adds.platforms() == ["linux-x86_64"]
    with pytest.raises(ValueError):
        adds.start(["nonsense"])
    adds.start(["windows-x86_64"])
    adds.join(30)
    status = adds.status()
    assert not status["running"]
    assert status["result"]["status"] == "added", status
    assert adds.platforms() == ["linux-x86_64", "windows-x86_64"]
    assert not sp.PlatformAdds(lambda: Config(), None).available


def test_labels():
    assert sp.platform_label("windows-x86_64") == "Windows"
    assert sp.platform_label("macos-arm64") == "Mac (Apple silicon)"
    assert sp.platform_label("other") == "other"
