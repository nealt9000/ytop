"""Resolve external tools to the project-local toolchain.

The Fedora system ffmpeg is ``ffmpeg-free``: it is built with
``--disable-decoder=h264`` and cannot decode H.264 at all.  Every script in this
project therefore resolves ``ffmpeg`` / ``ffprobe`` through this module, which
prefers the pinned static build under ``.tools/ffmpeg`` and only falls back to
``PATH`` when that build is absent.

Overrides, for experiments:

    YTOP_FFMPEG=/path/to/ffmpeg
    YTOP_FFPROBE=/path/to/ffprobe
    YTOP_FFPLAY=/path/to/ffplay
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Literal, cast

from .components import executable_deno
from .runtime import UnsupportedPlatform, active_runtime, bundled_tool, current_platform

ROOT = Path(__file__).resolve().parents[2]
PROJECT_FFMPEG_DIR = ROOT / ".tools" / "ffmpeg"

_ENV = {
    "ffmpeg": "YTOP_FFMPEG",
    "ffprobe": "YTOP_FFPROBE",
    "ffplay": "YTOP_FFPLAY",
}


def _resolve(name: str) -> Path:
    override = os.environ.get(_ENV[name])
    if override:
        path = Path(override).expanduser()
        if path.is_file():
            return path
        raise RuntimeError(f"{_ENV[name]} is set to {override!r}, but that is not a file")

    if name in ("ffmpeg", "ffprobe"):
        bundled = bundled_tool(cast(Literal["ffmpeg", "ffprobe"], name))
        if bundled is not None:
            return bundled

    project = PROJECT_FFMPEG_DIR / "bin" / name
    if project.is_file() and os.access(project, os.X_OK):
        return project

    found = shutil.which(name)
    if found:
        return Path(found)
    raise RuntimeError(
        f"cannot find {name}; run tools/fetch_ffmpeg.sh or install it on PATH"
    )


def ffmpeg_path() -> Path:
    return _resolve("ffmpeg")


def ffprobe_path() -> Path:
    return _resolve("ffprobe")


def ffplay_path() -> Path:
    return _resolve("ffplay")


def deno_path() -> Path | None:
    """Deno (yt-dlp's JS runtime): the stick's component store first (see
    ``ytop.components.executable_deno``), then the runtime's ``bin/deno``;
    None when there is neither."""
    runtime = active_runtime()
    if runtime.home is not None and runtime.runtime_dir is not None:
        try:
            plat = current_platform()
        except UnsupportedPlatform:
            return bundled_tool("deno")
        return executable_deno(runtime.home / ".ytop", plat, runtime.runtime_dir)
    return bundled_tool("deno")


def toolchain_source() -> str:
    """Return ``bundled``, ``project`` or ``system`` for the ffmpeg in use."""
    if not os.environ.get("YTOP_FFMPEG") and bundled_tool("ffmpeg") is not None:
        return "bundled"
    project = PROJECT_FFMPEG_DIR / "bin" / "ffmpeg"
    if not os.environ.get("YTOP_FFMPEG") and project.is_file():
        return "project"
    return "system"
