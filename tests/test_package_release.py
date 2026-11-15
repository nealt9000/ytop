import hashlib
import importlib.util
import json
import re
import sys
import tarfile
from pathlib import Path

import pytest

from stick_fakes import REPO, make_fake_image

PLATS = ["linux-x86_64", "windows-x86_64"]


def _load():
    spec = importlib.util.spec_from_file_location(
        "package_release", REPO / "tools" / "package_release.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["package_release"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def image(tmp_path_factory):
    return make_fake_image(tmp_path_factory.mktemp("rel") / "stick", PLATS)


@pytest.fixture(scope="module")
def release(image, tmp_path_factory):
    out = tmp_path_factory.mktemp("out")
    _load().package(image, out)
    return out


def _doc(release: Path) -> dict:
    return json.loads((release / "ytop-release.json").read_text())


def _members(archive: Path) -> list[tarfile.TarInfo]:
    with tarfile.open(archive) as tar:
        return tar.getmembers()


def test_pieces_cover_the_manifest_exactly(release):
    doc = _doc(release)
    seen: dict[str, str] = {}
    for name, piece in doc["pieces"].items():
        for rel in piece["files"]:
            assert rel not in seen, f"{rel} in {seen[rel]} and {name}"
            seen[rel] = name
        # The archive holds exactly the listed files.
        assert [m.name for m in _members(release / piece["file"])] == piece["files"]
    assert sorted(seen) == sorted(doc["manifest"])
    assert set(doc["pieces"]) == {
        "app", "lib-common", "root",
        *(f"lib-{p}" for p in PLATS),
        *(f"runtime-{p}-{k}" for p in PLATS for k in ("python", "bin")),
    }
    assert {"boot.py", "LICENSE", "THIRD_PARTY_NOTICES.md"} <= set(
        doc["pieces"]["root"]["files"]
    )


def test_archives_are_deterministic(image, release, tmp_path):
    again = tmp_path / "again"
    _load().package(image, again)
    first = sorted(p.name for p in release.iterdir())
    assert first == sorted(p.name for p in again.iterdir())
    for name in first:
        assert (
            hashlib.sha256((release / name).read_bytes()).hexdigest()
            == hashlib.sha256((again / name).read_bytes()).hexdigest()
        ), name
    for path in release.glob("*.tar.gz"):
        assert path.read_bytes()[4:8] == b"\0\0\0\0"  # gzip mtime
        for m in _members(path):
            assert (m.mtime, m.uid, m.gid, m.uname, m.gname) == (0, 0, 0, "", "")
            assert m.isfile()
            assert m.mode in (0o644, 0o755)


def test_executables_keep_their_bit(image, release):
    doc = _doc(release)
    bin_piece = doc["pieces"]["runtime-linux-x86_64-bin"]
    with tarfile.open(release / bin_piece["file"]) as tar:
        for m in tar.getmembers():
            src = image / m.name
            assert (m.mode == 0o755) == bool(src.stat().st_mode & 0o111), m.name


def test_sha256sums_lists_every_file(release):
    lines = (release / "SHA256SUMS").read_text().splitlines()
    listed = {}
    for line in lines:
        digest, name = re.fullmatch(r"([0-9a-f]{64})  (.+)", line).groups()
        listed[name] = digest
    files = {p.name for p in release.iterdir()} - {"SHA256SUMS"}
    assert set(listed) == files
    assert "ytop-release.json" in listed and "components-seed.tar.gz" in listed
    for name, digest in listed.items():
        assert hashlib.sha256((release / name).read_bytes()).hexdigest() == digest
    doc = _doc(release)
    for piece in doc["pieces"].values():
        assert listed[piece["file"]] == piece["sha256"]
        assert (release / piece["file"]).stat().st_size == piece["size"]


def test_release_json_min_components(image, release):
    doc = _doc(release)
    current = json.loads((image / "components" / "current.json").read_text())["current"]
    assert doc["min_components"] == current and current
    build = json.loads((image / "build.json").read_text())
    assert doc["build"] == build
    assert (doc["version"], doc["build_id"], doc["platforms"]) == (
        build["version"], build["build_id"], PLATS,
    )
    seed = doc["components_seed"]
    names = [m.name for m in _members(release / seed["file"])]
    assert "components/current.json" in names
    assert seed["sha256"] == hashlib.sha256((release / seed["file"]).read_bytes()).hexdigest()


def test_symlinks_are_refused(image, tmp_path):
    import shutil

    copy = tmp_path / "img"
    shutil.copytree(image, copy, symlinks=True)
    (copy / "app" / "link").symlink_to("../boot.py")
    with pytest.raises(Exception, match="symlink"):
        _load().package(copy, tmp_path / "out")


def test_version_matches_pyproject():
    import tomllib

    want = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
    from ytop import __version__

    assert __version__ == want
    assert _load().project_version() == want


@pytest.mark.parametrize("current", [{}, None, {"yt-dlp": ""}])
def test_min_components_must_be_listed(image, tmp_path, current):
    """App updates gate on min_components: a release without it is refused."""
    import shutil

    copy = tmp_path / "img"
    shutil.copytree(image, copy)
    doc = json.loads((copy / "components" / "current.json").read_text())
    if current is None:
        doc.pop("current")
    else:
        doc["current"] = current
    (copy / "components" / "current.json").write_text(json.dumps(doc))
    with pytest.raises(Exception, match="min_components"):
        _load().package(copy, tmp_path / "out")
