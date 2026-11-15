"""Portable-runtime discovery.

``boot.py`` on a YTOP stick sets ``YTOP_HOME`` (the stick root),
``YTOP_RUNTIME`` (the active platform's runtime directory, which is the cache
copy on Linux), ``YTOP_BUILD_ID`` and ``YTOP_IMAGE_ROOT`` (the full ``.ytop``
image that "Make a stick" copies from: the stick's own when it has
``build.json``, else the runtime root).  Outside a stick none are set and
every field is ``None``.
"""

from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

PLATFORMS: tuple[str, ...] = (
    "linux-x86_64",
    "linux-aarch64",
    "macos-arm64",
    "macos-x86_64",
    "windows-x86_64",
)


class UnsupportedPlatform(RuntimeError):
    """This operating system / CPU has no YTOP runtime."""


def current_platform() -> str:
    machine = platform.machine()
    lowered = machine.lower()
    if sys.platform.startswith("linux"):
        if lowered in ("x86_64", "amd64"):
            return "linux-x86_64"
        if lowered in ("aarch64", "arm64"):
            return "linux-aarch64"
    elif sys.platform == "darwin":
        if lowered == "arm64":
            return "macos-arm64"
        if lowered == "x86_64":
            return "macos-x86_64"
    elif sys.platform == "win32":
        # ARM64 Windows runs the x64 build through emulation.
        if lowered in ("amd64", "x86_64", "arm64"):
            return "windows-x86_64"
    raise UnsupportedPlatform(f"unsupported platform: {sys.platform} / {machine}")


@dataclass(frozen=True)
class Runtime:
    home: Path | None
    runtime_dir: Path | None
    build_id: str | None


def active_runtime() -> Runtime:
    home = os.environ.get("YTOP_HOME")
    runtime_dir = os.environ.get("YTOP_RUNTIME")
    return Runtime(
        home=Path(home) if home else None,
        runtime_dir=Path(runtime_dir) if runtime_dir else None,
        build_id=os.environ.get("YTOP_BUILD_ID") or None,
    )


def bundled_tool(name: Literal["ffmpeg", "ffprobe", "deno"]) -> Path | None:
    runtime_dir = active_runtime().runtime_dir
    if runtime_dir is None:
        return None
    exe = name + (".exe" if sys.platform == "win32" else "")
    path = runtime_dir / "bin" / exe
    return path if path.is_file() else None
