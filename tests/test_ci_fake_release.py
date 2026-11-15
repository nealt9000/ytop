"""The self-update CI job's fixtures (tools/ci_fake_release.py and
tools/fake_release_server.py) and the CLI entries it drives, end to end on
a fake stick and a server on 127.0.0.1."""

from __future__ import annotations

import importlib.util
import json
import marshal
import shutil
import sys
import threading
import urllib.request
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

import ytop.makestick as ms
from factories import library_set, make_library
from stick_fakes import REPO, make_fake_image
from ytop import cli
from ytop.components import read_current
from ytop.config import set_updates_option
from ytop.manifest import read_trusted_manifest
from ytop.web.library import Library

PLATS = ["linux-x86_64", "windows-x86_64"]


def _tool(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO / "tools" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tools() -> tuple[ModuleType, ModuleType]:
    return _tool("ci_fake_release"), _tool("fake_release_server")


@pytest.fixture(scope="module")
def image(tmp_path_factory) -> Path:
    return make_fake_image(tmp_path_factory.mktemp("ci") / "stick", PLATS)


@pytest.fixture
def served(tools, image, tmp_path):
    ci, srv = tools
    root = tmp_path / "serve"
    a, b = ci.app_releases(image, tmp_path / "work", root, "test/ytop")
    log = tmp_path / "access.log"
    server = srv.make_server(root, log)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield root, a, b, log, base
    server.shutdown()
    server.server_close()


@pytest.fixture
def stick(image, tmp_path, monkeypatch) -> Path:
    target = tmp_path / "stick"
    target.mkdir()
    library = Library(library_set(make_library(tmp_path / "home", "Laptop")))
    plan = ms.plan_make(target, image, library, platforms=PLATS, label="Stick")
    ms.run_make(plan, image, library, lambda update: None)
    monkeypatch.setenv("YTOP_HOME", str(target))
    return target


def _point(stick: Path, base: str) -> None:
    set_updates_option(stick, "api_base", base + "/api")
    set_updates_option(stick, "repo", "test/ytop")
    set_updates_option(stick, "smoke_video", "")


def _get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.read()


def test_second_build_differs_in_one_app_file(served, image):
    root, a, b, _log, _base = served
    doc_a = json.loads((a / "ytop-release.json").read_text())
    doc_b = json.loads((b / "ytop-release.json").read_text())
    assert b == root / "test/ytop" / f"v{doc_b['version']}"
    assert doc_b["version"] == "0.1.1" and doc_a["version"] == "0.1.0"
    assert doc_b["build_id"] == doc_a["build_id"] + "-b"
    changed = [rel for rel, h in doc_b["manifest"].items() if doc_a["manifest"][rel] != h]
    assert changed == ["app/ytop/__init__.py",
                       "app/ytop/__pycache__/__init__.cpython-312.pyc"]
    assert doc_b["pieces"]["app"]["sha256"] != doc_a["pieces"]["app"]["sha256"]
    assert {n for n in doc_a["pieces"] if doc_a["pieces"][n] == doc_b["pieces"][n]} == (
        set(doc_a["pieces"]) - {"app"}
    )
    # The image itself is untouched (the copy was hard-linked).
    assert "0.1.1" not in (image / "app/ytop/__init__.py").read_text()
    assert read_trusted_manifest(image) is not None


def test_second_builds_bytecode_runs_the_new_code(tools, image, tmp_path):
    ci, _srv = tools
    second, version = ci.make_second_build(image, tmp_path)
    assert read_trusted_manifest(second) is not None
    pyc = (second / "app/ytop/__pycache__/__init__.cpython-312.pyc").read_bytes()
    scope: dict[str, object] = {"os": __import__("os")}
    exec(marshal.loads(pyc[16:]), scope)  # noqa: S102 - our own fixture
    assert scope["__version__"] == version


def test_server_serves_the_newest_release_and_logs(served, tools):
    root, _a, b, log, base = served
    latest = json.loads(_get(base + "/api/repos/test/ytop/releases/latest"))
    assert latest["tag_name"] == b.name and not latest["draft"]
    names = {a["name"] for a in latest["assets"]}
    assert {"ytop-release.json", "SHA256SUMS", "app.tar.gz"} <= names
    asset = next(a for a in latest["assets"] if a["name"] == "SHA256SUMS")
    assert _get(asset["browser_download_url"]) == (b / "SHA256SUMS").read_bytes()
    with pytest.raises(urllib.error.HTTPError):
        _get(base + "/api/repos/none/none/releases/latest")
    with pytest.raises(urllib.error.HTTPError):
        _get(base + "/dl/test/ytop/" + b.name + "/..%2F..%2Fx")
    # A release added while the server runs is seen.
    (root / "x/y/v1").mkdir(parents=True)
    (root / "x/y/v1/f.txt").write_text("hi")
    assert _get(base + "/dl/x/y/v1/f.txt") == b"hi"
    assert "/api/repos/test/ytop/releases/latest" in log.read_text().splitlines()


def test_ytdlp_release_is_a_valid_newer_wheel(tools, image, tmp_path):
    ci, _srv = tools
    out = ci.ytdlp_release(image, tmp_path, "2099.1.1")
    wheel = out / "yt_dlp-2099.1.1-py3-none-any.whl"
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
        assert "yt_dlp-2099.1.1.dist-info/METADATA" in names
        assert not any("2026.10.26" in n for n in names)
        assert b"Version: 2099.1.1" in zf.read("yt_dlp-2099.1.1.dist-info/METADATA")
        assert b"2099.1.1" in zf.read("yt_dlp/version.py")
        record = zf.read("yt_dlp-2099.1.1.dist-info/RECORD").decode()
        assert "yt_dlp/version.py,sha256=" in record
    import hashlib
    assert (out / "SHA2-256SUMS").read_text() == (
        f"{hashlib.sha256(wheel.read_bytes()).hexdigest()}  {wheel.name}\n"
    )


def test_app_update_install_needs_consent(served, stick, capsys):
    _root, _a, _b, log, base = served
    _point(stick, base)
    before = (stick / ".ytop/build.json").read_bytes()
    assert cli.main(["app-update", "install"]) == 2
    assert "--yes" in capsys.readouterr().err
    assert not log.exists()  # not even a look at the network
    assert (stick / ".ytop/build.json").read_bytes() == before


def test_app_update_check_then_install_fetches_only_the_app_piece(served, stick, capsys):
    _root, _a, b, log, base = served
    _point(stick, base)
    assert cli.main(["app-update", "check"]) == 0
    assert "available: 0.1.1" in capsys.readouterr().out
    log.unlink()
    assert cli.main(["app-update", "install", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "updated" in out
    fetched = sorted(
        line.rsplit("/", 1)[-1] for line in log.read_text().splitlines()
        if line.startswith("/dl/")
    )
    assert fetched == ["SHA256SUMS", "app.tar.gz", "ytop-release.json"]
    doc = json.loads((b / "ytop-release.json").read_text())
    build = json.loads((stick / ".ytop/build.json").read_text())
    assert build["build_id"] == doc["build_id"] and build["version"] == "0.1.1"
    assert read_trusted_manifest(stick / ".ytop") is not None
    # Nothing newer now.
    assert cli.main(["app-update", "install", "--yes"]) == 0
    assert "up_to_date" in capsys.readouterr().out


def test_components_update_installs_the_newer_ytdlp(tools, image, stick, tmp_path, capsys):
    ci, srv = tools
    root = tmp_path / "gh"
    # Real yt-dlp's version.py has __version__; the fake one lacks it.
    fake = image / "components/yt-dlp/2026.10.26/yt_dlp/version.py"
    original = fake.read_text()
    fake.write_text(original + "__version__ = '2026.10.26'\n")
    try:
        ci.ytdlp_release(image, root, "2099.1.1")
    finally:
        fake.write_text(original)
    server = srv.make_server(root, tmp_path / "gh.log")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        _point(stick, f"http://127.0.0.1:{server.server_address[1]}")
        assert cli.main(["components", "update", "yt-dlp"]) == 0
    finally:
        server.shutdown()
        server.server_close()
    assert "updated yt-dlp: 2026.10.26 -> 2099.1.1" in capsys.readouterr().out
    assert read_current(stick / ".ytop").current["yt-dlp"] == "2099.1.1"
    assert (stick / ".ytop/components/yt-dlp/2099.1.1/yt_dlp/version.py").is_file()
    shutil.rmtree(root)


def test_update_commands_need_a_stick(monkeypatch, capsys):
    monkeypatch.delenv("YTOP_HOME", raising=False)
    assert cli.main(["app-update", "check"]) == 2
    assert cli.main(["components", "update"]) == 2
    assert "not a YTOP stick" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["app-update", "check"], ["components", "update"]])
def test_update_commands_import_in_a_fresh_interpreter(tmp_path, argv):
    # In-process tests import ytop.web first; a stick's boot.py does not.
    import os
    import subprocess

    env = {k: v for k, v in os.environ.items() if k != "YTOP_HOME"}
    run = subprocess.run(
        [sys.executable, "-c",
         "import sys; from ytop.cli import main; sys.exit(main(%r))" % (argv,)],
        capture_output=True, text=True, env=env, cwd=tmp_path,
    )
    assert run.returncode == 2 and "not a YTOP stick" in run.stderr, run.stderr
