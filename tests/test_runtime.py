"""Runtime discovery, bundled tools and yt-dlp options."""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

import pytest

from ytop import runtime, toolchain
from ytop.config import Config
from ytop.yt.probe import base_options


def _fake_bin(directory: Path, name: str) -> Path:
    path = directory / "bin" / (name + (".exe" if sys.platform == "win32" else ""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("YTOP_HOME", "YTOP_RUNTIME", "YTOP_BUILD_ID", "YTOP_FFMPEG",
                 "YTOP_FFPROBE"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    ("plat", "machine", "expected"),
    [
        ("linux", "x86_64", "linux-x86_64"),
        ("linux", "amd64", "linux-x86_64"),
        ("linux", "aarch64", "linux-aarch64"),
        ("linux", "arm64", "linux-aarch64"),
        ("darwin", "arm64", "macos-arm64"),
        ("darwin", "x86_64", "macos-x86_64"),
        ("win32", "AMD64", "windows-x86_64"),
        ("win32", "x86_64", "windows-x86_64"),
        ("win32", "ARM64", "windows-x86_64"),
    ],
)
def test_current_platform_mapping(monkeypatch, plat, machine, expected):
    monkeypatch.setattr(sys, "platform", plat)
    monkeypatch.setattr(platform, "machine", lambda: machine)
    assert runtime.current_platform() == expected
    assert expected in runtime.PLATFORMS


def test_current_platform_unsupported(monkeypatch):
    monkeypatch.setattr(sys, "platform", "freebsd13")
    monkeypatch.setattr(platform, "machine", lambda: "amd64")
    with pytest.raises(runtime.UnsupportedPlatform):
        runtime.current_platform()


def test_platforms_order():
    assert runtime.PLATFORMS == (
        "linux-x86_64", "linux-aarch64", "macos-arm64", "macos-x86_64",
        "windows-x86_64",
    )


def test_active_runtime_from_env(monkeypatch, tmp_path):
    assert runtime.active_runtime() == runtime.Runtime(None, None, None)
    monkeypatch.setenv("YTOP_HOME", str(tmp_path / "stick"))
    monkeypatch.setenv("YTOP_RUNTIME", str(tmp_path / "rt"))
    monkeypatch.setenv("YTOP_BUILD_ID", "abc123")
    rt = runtime.active_runtime()
    assert rt.home == tmp_path / "stick"
    assert rt.runtime_dir == tmp_path / "rt"
    assert rt.build_id == "abc123"


def test_bundled_tool_prefers_runtime(monkeypatch, tmp_path):
    rt = tmp_path / "rt"
    ffmpeg = _fake_bin(rt, "ffmpeg")
    monkeypatch.setenv("YTOP_RUNTIME", str(rt))
    project = tmp_path / "tools"
    _fake_bin(project, "ffmpeg")
    monkeypatch.setattr(toolchain, "PROJECT_FFMPEG_DIR", project)
    assert runtime.bundled_tool("ffmpeg") == ffmpeg
    assert toolchain.ffmpeg_path() == ffmpeg
    assert runtime.bundled_tool("ffprobe") is None


def test_bundled_tool_none_when_not_portable():
    assert runtime.bundled_tool("ffmpeg") is None


def test_deno_optional_none(monkeypatch, tmp_path):
    monkeypatch.setenv("YTOP_RUNTIME", str(tmp_path / "rt"))
    assert toolchain.deno_path() is None


def test_base_options_js_runtime_and_cachedir(monkeypatch, tmp_path):
    cfg = Config()
    opts = base_options(cfg)
    assert "js_runtimes" not in opts
    assert "cachedir" not in opts

    rt = tmp_path / "rt"
    deno = _fake_bin(rt, "deno")
    monkeypatch.setenv("YTOP_RUNTIME", str(rt))
    library = tmp_path / "lib"
    opts = base_options(cfg, cache_root=library)
    assert opts["js_runtimes"]["deno"]["path"] == str(deno)
    assert Path(opts["cachedir"]) == library / ".ytop" / "cache" / "yt-dlp"


def test_toolchain_source_bundled(monkeypatch, tmp_path):
    assert toolchain.toolchain_source() in ("project", "system")
    rt = tmp_path / "rt"
    _fake_bin(rt, "ffmpeg")
    monkeypatch.setenv("YTOP_RUNTIME", str(rt))
    assert toolchain.toolchain_source() == "bundled"


def test_base_options_keeps_deno_cache_in_library(monkeypatch, tmp_path):
    library = tmp_path / "lib"
    base_options(Config())
    assert "DENO_DIR" not in os.environ  # no library, nothing to point at
    base_options(Config(), cache_root=library)
    assert Path(os.environ["DENO_DIR"]) == library / ".ytop" / "cache" / "deno"
    # The first library wins; a later destination does not move it.
    base_options(Config(), cache_root=tmp_path / "other")
    assert Path(os.environ["DENO_DIR"]) == library / ".ytop" / "cache" / "deno"
