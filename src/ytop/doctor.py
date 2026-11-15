"""Capability check for the ytop download and web stack.

Run this before a fetch session::

    .venv/bin/ytop doctor
    .venv/bin/ytop doctor --json

Every required check must pass for ``ytop fetch`` and ``ytop serve`` to work.
Optional checks (PO-token provider, Node.js, transcript client) warn without
failing the run.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .runtime import UnsupportedPlatform, active_runtime, current_platform
from .toolchain import (
    ROOT,
    deno_path,
    ffmpeg_path,
    ffplay_path,
    ffprobe_path,
    toolchain_source,
)

REQUIRED_DECODERS = ["h264", "hevc", "av1", "vp9", "opus"]
REQUIRED_DEMUXERS = ["matroska", "mov", "webm"]
REQUIRED_MUXERS = ["matroska", "mp4", "webm"]


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail
    evidence: str
    required: bool = True


@dataclass
class Doctor:
    checks: list[Check] = field(default_factory=list)
    started: str = ""

    def add(self, name: str, status: str, evidence: str,
            required: bool = True) -> None:
        self.checks.append(Check(name, status, evidence, required))


def _first_line(text: str) -> str:
    return text.strip().splitlines()[0] if text.strip() else ""


def _run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "AV_LOG_FORCE_NOCOLOR": "1"},
    )


def _version_of(cmd: list[str]) -> str:
    try:
        p = _run(cmd, timeout=20)
        return _first_line(p.stdout or p.stderr)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"unavailable ({exc})"


def _listed(output: str, names: list[str]) -> tuple[list[str], list[str]]:
    present, missing = [], []
    for name in names:
        if re.search(
            rf"(^|[\s,]){re.escape(name)}([\s,]|$)", output, re.MULTILINE
        ):
            present.append(name)
        else:
            missing.append(name)
    return present, missing


def _check_ffmpeg(d: Doctor, ffmpeg: str) -> None:
    try:
        dec = _run([ffmpeg, "-hide_banner", "-decoders"]).stdout
        dem = _run([ffmpeg, "-hide_banner", "-demuxers"]).stdout
        mux = _run([ffmpeg, "-hide_banner", "-muxers"]).stdout
        ok, missing = _listed(dec, REQUIRED_DECODERS)
        d.add("ffmpeg decoders", "ok" if not missing else "fail",
              f"present: {', '.join(ok)}"
              + (f"; MISSING: {', '.join(missing)}" if missing else ""))
        ok, missing = _listed(dem, REQUIRED_DEMUXERS)
        d.add("ffmpeg demuxers", "ok" if not missing else "fail",
              f"present: {', '.join(ok)}"
              + (f"; MISSING: {', '.join(missing)}" if missing else ""))
        ok, missing = _listed(mux, REQUIRED_MUXERS)
        d.add("ffmpeg muxers", "ok" if not missing else "fail",
              f"present: {', '.join(ok)}"
              + (f"; MISSING: {', '.join(missing)}" if missing else ""))
    except Exception as exc:  # noqa: BLE001
        d.add("ffmpeg features", "fail", f"could not query ffmpeg: {exc}")


def _check_runtime(d: Doctor) -> None:
    try:
        key = current_platform()
    except UnsupportedPlatform as exc:
        key = f"unsupported ({exc})"
    rt = active_runtime()
    d.add("platform", "ok", key, required=False)
    portable = rt.runtime_dir is not None
    d.add("portable", "ok",
          f"yes: runtime {rt.runtime_dir}, build {rt.build_id or 'unknown'}"
          if portable else "no (running from a source checkout)",
          required=False)
    deno = deno_path()
    if deno is None:
        d.add("deno", "warn", "deno not found; YouTube JS challenges may fail",
              required=False)
    else:
        d.add("deno", "ok", f"{_version_of([str(deno), '--version'])} ({deno})",
              required=False)


def _launch_root(library: Path | None) -> Path:
    return Path(library).expanduser() if library else (
        Path(os.environ.get("YTOP_LIBRARY") or "~/Videos/YTOP").expanduser()
    )


def _check_library(d: Doctor, library: Path | None) -> None:
    from .fsinfo import fs_info
    from .layout import read_library

    root = _launch_root(library)
    info = read_library(root)
    if info is None:
        d.add("library", "warn",
              f"no YTOP library at {root}; run 'ytop init {root}'",
              required=False)
        return
    try:
        fstype = fs_info(root).fstype
    except OSError:
        fstype = "unknown"
    try:
        free = f"{shutil.disk_usage(root).free / 1e9:.1f} GB free"
    except OSError:
        free = "free space unknown"
    d.add("library", "ok",
          f"{root} (schema {info.schema}, {fstype}, {free})", required=True)


def _check_resources(d: Doctor, library: Path | None) -> None:
    try:
        meminfo = Path("/proc/meminfo").read_text()
        avail_kb = int(re.search(r"MemAvailable:\s+(\d+)", meminfo).group(1))
        avail_mb = avail_kb / 1024
        status = "ok" if avail_mb >= 512 else ("warn" if avail_mb >= 256 else "fail")
        d.add("free RAM", status, f"{avail_mb:.0f} MiB available")
    except Exception as exc:  # noqa: BLE001
        d.add("free RAM", "warn", f"could not read /proc/meminfo: {exc}",
              required=False)

    root = _launch_root(library)
    try:
        usage = shutil.disk_usage(root if root.exists() else ROOT)
        free_gb = usage.free / 1e9
        status = "ok" if free_gb >= 10 else ("warn" if free_gb >= 2 else "fail")
        d.add("disk", status, f"{free_gb:.1f} GB free for the library")
    except Exception as exc:  # noqa: BLE001
        d.add("disk", "fail", str(exc))


def run_checks(library: Path | None = None) -> Doctor:
    d = Doctor()
    import datetime

    d.started = datetime.datetime.now().astimezone().isoformat(timespec="seconds")

    # --- binaries -----------------------------------------------------------
    try:
        ffmpeg = str(ffmpeg_path())
        ffprobe = str(ffprobe_path())
    except RuntimeError as exc:
        d.add("ffmpeg binaries", "fail", str(exc))
        return d
    try:
        ffplay: str | None = str(ffplay_path())
    except RuntimeError:
        ffplay = None  # the bundled minimal build has no ffplay

    d.add("ffmpeg", "ok",
          f"{_version_of([ffmpeg, '-version'])} — {toolchain_source()} ({ffmpeg})")
    d.add("ffprobe", "ok", _version_of([ffprobe, "-version"]))
    if ffplay:
        d.add("ffplay", "ok", f"{_version_of([ffplay, '-version'])} ({ffplay})")
    else:
        d.add("ffplay", "warn", "not found (optional)", required=False)
    _check_ffmpeg(d, ffmpeg)
    _check_runtime(d)

    # --- library / disk -----------------------------------------------------
    _check_library(d, library)
    _check_resources(d, library)

    # --- Python stack -------------------------------------------------------
    try:
        import importlib.metadata as md

        d.add("yt-dlp", "ok", md.version("yt-dlp"), required=True)
    except Exception as exc:  # noqa: BLE001
        d.add("yt-dlp", "fail", f"not installed: {exc}", required=True)

    try:
        d.add("Python", "ok", f"{platform.python_version()} ({sys.executable})",
              required=True)
    except Exception as exc:  # noqa: BLE001
        d.add("Python", "fail", str(exc))

    # --- optional but useful ------------------------------------------------
    node = shutil.which("node")
    if node:
        d.add("node", "ok",
              f"{_version_of([node, '--version'])} (YouTube JS challenges)",
              required=False)
    else:
        d.add("node", "warn", "not found; YouTube JS challenges may fail",
              required=False)

    try:
        from .yt.pot import pot_status

        status, evidence = pot_status()
        d.add("PO tokens", status, evidence, required=False)
    except Exception as exc:  # noqa: BLE001
        d.add("PO tokens", "warn", f"could not check: {exc}", required=False)

    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE probe_fts USING fts5(text)")
        finally:
            conn.close()
        d.add("SQLite FTS5", "ok", f"sqlite {sqlite3.sqlite_version}",
              required=False)
    except Exception as exc:  # noqa: BLE001
        d.add("SQLite FTS5", "fail",
              f"transcript search unavailable: {exc}", required=False)

    try:
        import importlib.metadata as md

        d.add("transcripts", "ok",
              f"youtube-transcript-api {md.version('youtube-transcript-api')}",
              required=False)
    except Exception as exc:  # noqa: BLE001
        d.add("transcripts", "warn",
              f"youtube-transcript-api missing: {exc}", required=False)

    try:
        import fastapi
        import jinja2
        import uvicorn

        d.add("web UI", "ok",
              f"FastAPI {fastapi.__version__}, uvicorn {uvicorn.__version__}, "
              f"Jinja2 {jinja2.__version__}", required=False)
    except Exception as exc:  # noqa: BLE001
        d.add("web UI", "fail", f"serve unavailable: {exc}", required=False)

    return d


def print_report(d: Doctor, as_json: bool) -> int:
    failed = [c for c in d.checks if c.status == "fail" and c.required]
    warned = [c for c in d.checks if c.status == "warn"]
    if as_json:
        print(json.dumps({
            "started": d.started,
            "toolchain": toolchain_source(),
            "checks": [asdict(c) for c in d.checks],
            "summary": {
                "ok": sum(c.status == "ok" for c in d.checks),
                "warn": len(warned),
                "fail": len(failed),
                "exit": 1 if failed else 0,
            },
        }, indent=2))
        return 1 if failed else 0

    print(f"ytop doctor — {d.started}")
    try:
        ffmpeg = str(ffmpeg_path())
    except RuntimeError as exc:
        ffmpeg = f"unavailable ({exc})"
    print(f"toolchain: {toolchain_source()} ({ffmpeg})")
    print()
    for c in d.checks:
        tag = {"ok": " OK ", "warn": "WARN", "fail": "FAIL"}[c.status]
        req = "" if c.required else " (optional)"
        print(f"[{tag}] {c.name:<16} {c.evidence}{req}")
    print()
    ok = sum(c.status == "ok" for c in d.checks)
    print(f"summary: {ok} ok, {len(warned)} warning, {len(failed)} failure")
    if failed:
        print("FAILED: " + ", ".join(c.name for c in failed))
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="check the ytop download and web stack")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--library", default=None, help="library root to check")
    a = ap.parse_args(argv)
    library = Path(a.library).expanduser() if a.library else None
    return print_report(run_checks(library), a.json)


if __name__ == "__main__":
    raise SystemExit(main())
