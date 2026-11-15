"""tools/smoke_stick.py against a fake stick that runs the repo's own YTOP."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import sysconfig
from pathlib import Path
from types import ModuleType

import pytest

from ytop.layout import LibraryLayout, init_library
from ytop.yt.metadata import read_json

REPO = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="fake launcher is a Linux sh script"
)

BUILD_ID = "202611130000-test000"


def load_smoke() -> ModuleType:
    name = "smoke_stick"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, REPO / "tools/smoke_stick.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


smoke_stick = load_smoke()

def _ffmpeg() -> Path | None:
    from ytop.toolchain import ffmpeg_path

    try:
        return ffmpeg_path()
    except RuntimeError:
        return None


# Mimics start-ytop-linux.sh: stage the runtime into the cache, then start
# YTOP in portable mode on the staged "bundled" python -- here a symlink to
# this repo's interpreter, with the venv's site-packages added by hand.
FAKE_LAUNCHER = """#!/bin/sh
STICK=$(cd "$(dirname "$0")" && pwd) || exit 1
CACHE="$XDG_CACHE_HOME/ytop/runtime/{build_id}"
mkdir -p "$CACHE/runtime" && cp -R "$STICK/.ytop/runtime/{plat}" "$CACHE/runtime/" || exit 4
{runtime_env}YTOP_HOME="$STICK" exec "$CACHE/runtime/{plat}/python/bin/python3" -s -c \\
    'import site, sys; site.addsitedir("{site}"); from ytop.cli import main; sys.exit(main(sys.argv[1:]))' \\
    serve --portable --no-open
"""
BUNDLED_ENV = 'YTOP_RUNTIME="$CACHE/runtime/{plat}" '


def make_fake_stick(
    tmp_path: Path, launcher: str = FAKE_LAUNCHER, *, bundled: bool = True
) -> Path:
    stick = tmp_path / "stick"
    init_library(stick, "Smoke")
    smoke_stick.seed(stick)
    # Off the default port: the closed connections linger in TIME_WAIT there,
    # which would trip tests that bind 8765 themselves.
    LibraryLayout(stick).config_file.write_text("[web]\nport = 18765\n", encoding="utf-8")
    plat = smoke_stick.platform_key()
    (stick / ".ytop/build.json").write_text(
        json.dumps({"build_id": BUILD_ID, "platforms": [plat]}), encoding="utf-8"
    )
    runtime = stick / ".ytop/runtime" / plat
    (runtime / "python/bin").mkdir(parents=True)
    (runtime / "python/bin/python3").symlink_to(os.path.realpath(sys.executable))
    bin_dir = runtime / "bin"
    bin_dir.mkdir()
    real_ffmpeg = _ffmpeg()
    scripts = {
        "ffmpeg": f'exec "{real_ffmpeg}" "$@"' if real_ffmpeg else "exit 1",
        "ffprobe": "echo 'ffprobe version fake'",
        "deno": "echo 'deno 0.0.0'",
    }
    for name, body in scripts.items():
        tool = bin_dir / name
        tool.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        tool.chmod(0o755)
    (stick / "start-ytop-linux.sh").write_text(
        launcher.format(
            build_id=BUILD_ID, plat=plat, site=sysconfig.get_paths()["purelib"],
            runtime_env=BUNDLED_ENV.format(plat=plat) if bundled else "",
        ),
        encoding="utf-8",
    )
    return stick


def test_seed_adds_playable_fixture_playlist(tmp_path):
    stick = tmp_path / "stick"
    init_library(stick, "Smoke")
    smoke_stick.seed(stick)
    layout = LibraryLayout(stick)
    doc = read_json(layout.playlist_meta_dir(smoke_stick.PLAYLIST_ID) / "playlist.json")
    assert doc["title"] == smoke_stick.PLAYLIST_TITLE
    meta = read_json(
        layout.video_meta_dir(smoke_stick.PLAYLIST_ID, smoke_stick.VIDEO_ID) / "meta.json"
    )
    media = layout.media_root / meta["files"]["media"]
    assert media.read_bytes() == smoke_stick.FIXTURE.read_bytes()
    assert not layout.partial_dir(smoke_stick.VIDEO_ID).exists()


def test_seed_needs_a_library(tmp_path):
    with pytest.raises(smoke_stick.SmokeError, match="ytop init"):
        smoke_stick.seed(tmp_path / "nothing")


@pytest.mark.skipif(_ffmpeg() is None, reason="the preview check needs ffmpeg")
def test_smoke_passes_against_running_app(tmp_path, capsys):
    stick = make_fake_stick(tmp_path)
    assert smoke_stick.main([str(stick), "--timeout", "60"]) == 0
    out = capsys.readouterr().out
    for name in ("toolchain is bundled", "python is the bundled one",
                 "ffmpeg is the bundled one", "deno is the bundled one",
                 "GET / lists the fixture playlist", "ranged media request",
                 "preview frame", "ffmpeg -version", "ffprobe -version",
                 "deno --version", "launcher exits within 90 s",
                 "port 18765 closed after shutdown",
                 "no bytecode written at runtime"):
        assert f"ok   {name}" in out
    assert "smoke passed" in out


def test_smoke_fails_when_tools_are_not_bundled(tmp_path, capsys):
    # Without YTOP_RUNTIME the app falls back to the project/host ffmpeg.
    stick = make_fake_stick(tmp_path, bundled=False)
    assert smoke_stick.main([str(stick), "--timeout", "60"]) == 1
    out = capsys.readouterr().out
    assert "FAIL toolchain is bundled" in out


def test_runtime_bytecode_check(tmp_path, capsys):
    import importlib.util
    import py_compile

    src = tmp_path / "rt/app/mod.py"
    src.parent.mkdir(parents=True)
    src.write_text("X = 1\n")
    py_compile.compile(
        str(src), invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH
    )
    smoke_stick.check_no_runtime_bytecode(tmp_path / "rt")  # the image's own
    assert "ok   no bytecode written at runtime" in capsys.readouterr().out
    written = importlib.util.cache_from_source(str(tmp_path / "rt/app/other.py"))
    Path(written).write_bytes(importlib.util.MAGIC_NUMBER + bytes(12))  # timestamp
    with pytest.raises(smoke_stick.SmokeError):
        smoke_stick.check_no_runtime_bytecode(tmp_path / "rt")


def test_smoke_path_holds_no_ffmpeg(tmp_path):
    env = smoke_stick.smoke_env(tmp_path)
    assert shutil.which("ffmpeg", path=env["PATH"]) is None
    assert shutil.which("python3", path=env["PATH"]) is None
    for name in ("sh", "cp", "sed", "uname"):
        assert shutil.which(name, path=env["PATH"]), name


def test_smoke_fails_when_launcher_dies(tmp_path, capsys):
    stick = make_fake_stick(tmp_path, launcher="#!/bin/sh\necho broken stick\nexit 3\n")
    assert smoke_stick.main([str(stick), "--timeout", "20"]) == 1
    captured = capsys.readouterr()
    assert "launcher exited early with status 3" in captured.err
    assert "broken stick" in captured.out
    assert "smoke FAILED" in captured.out


def test_smoke_fails_without_the_fixture_playlist(tmp_path, capsys):
    stick = make_fake_stick(tmp_path)
    layout = LibraryLayout(stick)
    shutil.rmtree(layout.playlist_meta_dir(smoke_stick.PLAYLIST_ID))
    assert smoke_stick.main([str(stick), "--timeout", "60"]) == 1
    out = capsys.readouterr().out
    assert "FAIL GET / lists the fixture playlist" in out
