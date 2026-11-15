#!/usr/bin/env python3
"""Smoke-test a built YTOP stick on this computer.

    python3 tools/smoke_stick.py <stick_dir> [--timeout 60]
    python3 tools/smoke_stick.py --seed <stick_dir>

The smoke run starts the stick's launcher for this OS (with
``YTOP_NO_BROWSER=1``), waits for the instance file and ``/healthz``, checks
through the token-gated ``/api/instance`` that the server runs the bundled
python, ffmpeg, ffprobe and deno (under the stick, or the launcher's cache
copy on Linux), checks the home page, a ranged media request and a preview
frame, runs the bundled ``ffmpeg``/``ffprobe -version`` and
``deno --version``, then stops YTOP through ``POST /api/instance/shutdown``
and expects the launcher to exit within 15 s and the port to close, and
that no bytecode was written at runtime (the image ships its own).  Exit
status 0 only when every check passed.

Host state is kept out of the way: the instance dir, ``HOME`` (``APPDATA`` and
``USERPROFILE`` on Windows) and ``XDG_CACHE_HOME`` point into a temp dir, and
``PATH`` holds only the commands the launchers use (no host ffmpeg).

``--seed`` (run with the repo's environment, where ``ytop`` is importable)
adds a one-video playlist made from ``tests/fixtures/media/h264_aac.mp4`` to
the library at ``<stick_dir>`` (create it first with ``ytop init``).

Stdlib only: the runner's python drives the test, YTOP runs on its bundled
python.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests/fixtures/media/h264_aac.mp4"

PLAYLIST_ID = "PLytopSmokeTest"
PLAYLIST_TITLE = "YTOP Smoke Playlist"
VIDEO_ID = "ytopSmoke01"
VIDEO_TITLE = "Smoke Test Video"

SHUTDOWN_GRACE_S = 90.0  # a slow drive's final sync comes first
HTTP_TIMEOUT_S = 10.0


class SmokeError(Exception):
    """A smoke check failed."""


# ------------------------------------------------------------------- seeding


def seed(stick: Path) -> None:
    """Add the fixture playlist to the library at ``stick`` via ytop's API."""
    from ytop.layout import LibraryLayout, read_library
    from ytop.publish import publish_download
    from ytop.yt.metadata import write_json

    if read_library(stick) is None:
        raise SmokeError(f"no YTOP library at {stick}; run 'ytop init {stick}' first")
    layout = LibraryLayout(stick)
    write_json(
        layout.playlist_meta_dir(PLAYLIST_ID) / "playlist.json",
        {
            "id": PLAYLIST_ID,
            "title": PLAYLIST_TITLE,
            "entries": [
                {"id": VIDEO_ID, "title": VIDEO_TITLE, "index": 1,
                 "duration": 1, "available": True},
            ],
        },
    )
    write_json(
        layout.video_meta_dir(PLAYLIST_ID, VIDEO_ID) / "meta.json",
        {"id": VIDEO_ID, "title": VIDEO_TITLE, "duration": 1, "files": {}},
    )
    staged = layout.partial_dir(VIDEO_ID) / FIXTURE.name
    staged.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURE, staged)
    final = publish_download(layout, PLAYLIST_ID, VIDEO_ID, staged)
    shutil.rmtree(layout.partial_dir(VIDEO_ID), ignore_errors=True)
    print(f"seeded {PLAYLIST_TITLE!r}: {final}")


# --------------------------------------------------------------------- smoke


def launcher_command(stick: Path) -> list[str] | str:
    if sys.platform == "win32":
        # The name holds "(", so cmd strips one pair of quotes around the
        # whole /c string; doubling them keeps the path quoted.
        return f'cmd /d /c ""{stick / "Start YTOP (Windows).cmd"}""'
    if sys.platform == "darwin":
        return ["sh", str(stick / "Start YTOP (Mac).command")]
    return ["sh", str(stick / "start-ytop-linux.sh")]


def platform_key() -> str:
    import platform

    machine = platform.machine().lower()
    if sys.platform == "win32":
        return "windows-x86_64"
    if sys.platform == "darwin":
        return "macos-arm64" if machine == "arm64" else "macos-x86_64"
    return "linux-aarch64" if machine in ("aarch64", "arm64") else "linux-x86_64"


def runtime_root(stick: Path, env: dict[str, str]) -> Path:
    """Where the running YTOP must take its python and tools from.

    Linux runs from the launcher's cache copy (the stick may be mounted
    without exec permission); the other systems run from the stick.
    """
    if sys.platform.startswith("linux"):
        build = json.loads((stick / ".ytop/build.json").read_text(encoding="utf-8"))
        return Path(env["XDG_CACHE_HOME"]) / "ytop/runtime" / build["build_id"]
    return stick / ".ytop"


def tool_dir(stick: Path, env: dict[str, str]) -> Path:
    return runtime_root(stick, env) / "runtime" / platform_key() / "bin"


# What the launchers run from PATH; nothing else (no ffmpeg) is reachable.
LAUNCHER_COMMANDS = (
    "sh", "sed", "head", "uname", "mkdir", "cp", "chmod", "find", "touch",
    "rm", "mv", "rmdir", "sleep", "dirname", "cat", "xattr",
)


def minimal_path(work: Path) -> str:
    """A PATH with only what the launchers need, so a host ffmpeg (or
    python) can never stand in for the bundled one."""
    if sys.platform == "win32":
        root = os.environ.get("SystemRoot", r"C:\Windows")
        return os.pathsep.join([os.path.join(root, "System32"), root])
    shim = work / "bin"
    shim.mkdir(parents=True, exist_ok=True)
    for name in LAUNCHER_COMMANDS:
        found = shutil.which(name)
        if found and not (shim / name).exists():
            (shim / name).symlink_to(found)
    return str(shim)


def smoke_env(work: Path) -> dict[str, str]:
    env = dict(os.environ)
    home = work / "home"
    home.mkdir(parents=True, exist_ok=True)
    env.update(
        YTOP_NO_BROWSER="1",
        YTOP_INSTANCE_DIR=str(work / "instance"),
        HOME=str(home),
        XDG_CACHE_HOME=str(work / "cache"),
        PATH=minimal_path(work),
    )
    if sys.platform == "win32":
        appdata = home / "AppData" / "Roaming"
        appdata.mkdir(parents=True, exist_ok=True)
        env.update(USERPROFILE=str(home), APPDATA=str(appdata))
    return env


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http(
    port: int, path: str, *, method: str = "GET", headers: dict[str, str] | None = None
) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        method=method,
        headers=headers or {},
        data=b"" if method == "POST" else None,
    )
    try:
        with _OPENER.open(req, timeout=HTTP_TIMEOUT_S) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


def wait_for_instance(
    proc: subprocess.Popen, instance_file: Path, deadline: float
) -> tuple[int, str, int]:
    """Poll the instance file and ``/healthz``; return (port, token, pid)."""
    last = "no instance file yet"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SmokeError(f"launcher exited early with status {proc.returncode}")
        try:
            data = json.loads(instance_file.read_text(encoding="utf-8"))
            port, token, pid = int(data["port"]), str(data["token"]), int(data["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            time.sleep(0.25)
            continue
        if 0 < port < 65536:
            try:
                status, _, body = http(port, "/healthz")
                if status == 200 and json.loads(body).get("ok") is True:
                    return port, token, pid
                last = f"/healthz answered {status}"
            except (OSError, ValueError) as exc:
                last = f"/healthz: {exc}"
        time.sleep(0.25)
    raise SmokeError(f"YTOP did not come up in time ({last})")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{f'  ({detail})' if detail else ''}")
    if not ok:
        raise SmokeError(f"{name} failed{f': {detail}' if detail else ''}")


def check_app(port: int) -> None:
    status, _, body = http(port, "/")
    check("GET / lists the fixture playlist",
          status == 200 and PLAYLIST_TITLE.encode() in body, f"status {status}")
    status, headers, body = http(
        port, f"/media/{PLAYLIST_ID}/{VIDEO_ID}", headers={"Range": "bytes=0-1023"}
    )
    check("ranged media request", status == 206 and len(body) == 1024,
          f"status {status}, {len(body)} bytes")
    status, headers, body = http(port, f"/media/{PLAYLIST_ID}/{VIDEO_ID}/preview?t=0.5")
    check("preview frame (bundled ffmpeg)",
          status == 200 and headers.get("content-type", "").startswith("image/jpeg")
          and body[:2] == b"\xff\xd8",
          f"status {status}, {headers.get('content-type')}")


def _under(path: str, root: Path) -> bool:
    """``path`` lies inside ``root`` (comparing both as given and resolved)."""
    norm = os.path.normcase
    paths = {norm(os.path.abspath(path)), norm(os.path.realpath(path))}
    roots = {norm(os.path.abspath(root)), norm(os.path.realpath(root))}
    return any(
        p == r or p.startswith(r.rstrip(os.sep) + os.sep) for p in paths for r in roots
    )


def check_provenance(port: int, token: str, root: Path) -> None:
    """The server must run the bundled python, ffmpeg, ffprobe and deno."""
    status, _, body = http(port, "/api/instance", headers={"X-YTOP-Token": token})
    check("GET /api/instance", status == 200, f"status {status}")
    try:
        info = json.loads(body)
    except ValueError:
        info = {}
    print(f"  runtime root: {root}")
    check("toolchain is bundled", info.get("toolchain_source") == "bundled",
          f"source {info.get('toolchain_source')!r}")
    for name in ("python", "ffmpeg", "ffprobe", "deno"):
        path = info.get(name)
        check(f"{name} is the bundled one",
              isinstance(path, str) and _under(path, root), str(path))


def check_port_closed(port: int) -> None:
    """Nothing may answer on the port after shutdown (no stray server)."""
    deadline = time.monotonic() + 5.0
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                pass
        except OSError:
            check(f"port {port} closed after shutdown", True)
            return
        if time.monotonic() > deadline:
            check(f"port {port} closed after shutdown", False, "still accepting")
        time.sleep(0.25)


def check_no_runtime_bytecode(root: Path) -> None:
    """Every pyc under the runtime root is the image's own unchecked-hash
    one (Ruling R12); Python writes timestamp-based ones, so any other kind
    means YTOP wrote bytecode onto the stick (or the cache) at runtime."""
    written = []
    for pyc in sorted(root.rglob("*.pyc")):
        with open(pyc, "rb") as handle:
            header = handle.read(8)
        if int.from_bytes(header[4:8], "little") != 0b01:
            written.append(pyc.relative_to(root).as_posix())
    shown = ", ".join(written[:5]) + (" ..." if len(written) > 5 else "")
    check("no bytecode written at runtime", not written, shown)


def check_tools(tools: Path, env: dict[str, str]) -> None:
    exe = ".exe" if sys.platform == "win32" else ""
    for name, flag in (("ffmpeg", "-version"), ("ffprobe", "-version"),
                       ("deno", "--version")):
        path = tools / f"{name}{exe}"
        try:
            run = subprocess.run([str(path), flag], capture_output=True, text=True,
                                 timeout=60, env=env, stdin=subprocess.DEVNULL)
            first = (run.stdout.strip().splitlines() or [""])[0]
            check(f"{name} {flag}", run.returncode == 0,
                  first if run.returncode == 0 else f"status {run.returncode}: {run.stderr[-300:]}")
        except (OSError, subprocess.SubprocessError) as exc:
            check(f"{name} {flag}", False, f"{path}: {exc}")


def stop(proc: subprocess.Popen, app_pid: int | None = None) -> None:
    """Kill the launcher and everything it started -- also when the launcher
    itself has already exited, since a child may have outlived it."""
    if sys.platform == "win32":
        taskkill = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                "System32", "taskkill.exe")
        if proc.poll() is None:
            subprocess.run([taskkill, "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True)
        if app_pid is not None:
            subprocess.run([taskkill, "/T", "/F", "/PID", str(app_pid)],
                           capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # the launcher's whole session
        except OSError:
            pass
        if app_pid is not None:
            try:
                os.kill(app_pid, signal.SIGKILL)
            except OSError:
                pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def smoke(
    stick: Path,
    *,
    timeout: float = 60.0,
    command: Callable[[Path], list[str] | str] = launcher_command,
) -> int:
    stick = stick.resolve()
    work = Path(tempfile.mkdtemp(prefix="ytop-smoke-"))
    env = smoke_env(work)
    log_path = work / "launcher.log"
    print(f"smoke: {stick} ({platform_key()})")
    log = open(log_path, "wb")
    popen_extra: dict = (
        {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
        if sys.platform == "win32" else {"start_new_session": True}
    )
    proc = subprocess.Popen(
        command(stick), cwd=stick, env=env, stdin=subprocess.DEVNULL,
        stdout=log, stderr=subprocess.STDOUT, **popen_extra,
    )
    ok = False
    app_pid: int | None = None
    try:
        port, token, app_pid = wait_for_instance(
            proc, work / "instance/instance.json", time.monotonic() + timeout
        )
        check("healthz", True, f"port {port}")
        check_provenance(port, token, runtime_root(stick, env))
        check_app(port)
        check_tools(tool_dir(stick, env), env)
        status, _, _ = http(port, "/api/instance/shutdown", method="POST",
                            headers={"X-YTOP-Token": token})
        check("POST /api/instance/shutdown", status == 202, f"status {status}")
        try:
            code = proc.wait(timeout=SHUTDOWN_GRACE_S)
        except subprocess.TimeoutExpired:
            check(f"launcher exits within {SHUTDOWN_GRACE_S:.0f} s", False)
        check(f"launcher exits within {SHUTDOWN_GRACE_S:.0f} s", code == 0,
              f"status {code}")
        check_port_closed(port)
        check_no_runtime_bytecode(runtime_root(stick, env))
        ok = True
    except SmokeError as exc:
        print(f"smoke FAILED: {exc}", file=sys.stderr)
    finally:
        stop(proc, app_pid)
        log.close()
        output = log_path.read_text(encoding="utf-8", errors="replace")
        print("---- launcher output ----")
        print(output.rstrip() or "(none)")
        print("-------------------------")
        shutil.rmtree(work, ignore_errors=True)
    print("smoke passed" if ok else "smoke FAILED")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("stick", type=Path, help="the stick (or built image) folder")
    ap.add_argument("--timeout", type=float, default=60.0,
                    help="seconds to wait for YTOP to come up (default 60)")
    ap.add_argument("--seed", action="store_true",
                    help="add the fixture playlist to the stick's library instead")
    opts = ap.parse_args(argv)
    if opts.seed:
        try:
            seed(opts.stick)
        except SmokeError as exc:
            print(f"smoke_stick: {exc}", file=sys.stderr)
            return 1
        return 0
    return smoke(opts.stick, timeout=opts.timeout)


if __name__ == "__main__":
    sys.exit(main())
