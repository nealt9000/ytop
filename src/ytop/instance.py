"""One YTOP per user: the instance file, the launch lock and takeover rules.

A running portable server records itself in ``instance.json`` inside a
per-user directory in the OS temp dir.  A second launch reads that file and
asks the recorded port whether it really is that instance (the token must
match), then either hands over to it or, when it carries a newer build and is
idle, asks it to stop and takes its place.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import socket
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Literal

TOKEN_HEADER = "X-YTOP-Token"
HTTP_TIMEOUT_S = 1.0

Decision = Literal["open", "takeover", "open_and_offer"]


@dataclass
class InstanceInfo:
    pid: int
    port: int
    token: str
    version: str
    build_id: str
    launch_root: str
    started_at: float
    # Filled by ``read_live`` from the live server; never written to disk.
    active_jobs: int = field(default=0, compare=False)

    def to_json(self) -> dict:
        data = asdict(self)
        data.pop("active_jobs")
        return data

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"


class InstanceDirError(OSError):
    """The per-user instance dir is unsafe to use (symlink, wrong owner)."""


def _user_name() -> str:
    try:
        return getpass.getuser()
    except (KeyError, ImportError, OSError):
        getuid = getattr(os, "getuid", None)
        return str(getuid()) if getuid is not None else "user"


def instance_dir() -> Path:
    """``<tmp>/ytop-<user>``; ``YTOP_INSTANCE_DIR`` overrides it (tests)."""
    override = os.environ.get("YTOP_INSTANCE_DIR")
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / f"ytop-{_user_name()}"


def instance_path() -> Path:
    return instance_dir() / "instance.json"


def lock_path() -> Path:
    return instance_dir() / "instance.lock"


def ensure_dir() -> Path:
    """Create the instance dir (0700 where supported).

    Its file carries the control token, so a symlink, a non-directory or a
    dir owned by another user is refused with :class:`InstanceDirError`
    (a fallback dir would hide this instance from the next launch).
    """
    path = instance_dir()
    try:
        path.mkdir(mode=0o700, parents=True)
    except FileExistsError:
        pass
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise InstanceDirError(
            f"{path} is not a plain directory; remove it and start YTOP again"
        )
    if os.name != "nt":
        if st.st_uid != os.getuid():
            raise InstanceDirError(
                f"{path} belongs to another user; remove it and start YTOP again"
            )
        if st.st_mode & 0o077:
            os.chmod(path, 0o700)
    return path


def write_info(info: InstanceInfo) -> None:
    """Atomically replace ``instance.json`` (mode 0600)."""
    directory = ensure_dir()
    fd, tmp = tempfile.mkstemp(prefix=".instance-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(info.to_json(), handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, instance_path())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_info() -> InstanceInfo | None:
    try:
        data = json.loads(instance_path().read_text(encoding="utf-8"))
        return InstanceInfo(
            pid=int(data["pid"]),
            port=int(data["port"]),
            token=str(data["token"]),
            version=str(data["version"]),
            build_id=str(data.get("build_id") or ""),
            launch_root=str(data.get("launch_root") or ""),
            started_at=float(data.get("started_at") or 0.0),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _request(
    info: InstanceInfo, path: str, method: str = "GET", timeout: float = HTTP_TIMEOUT_S
) -> tuple[int, bytes]:
    """Call the instance's control API; proxies are never used for loopback."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{info.port}{path}",
        method=method,
        headers={TOKEN_HEADER: info.token},
        data=b"" if method == "POST" else None,
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), b""


def read_live() -> InstanceInfo | None:
    """The recorded instance, only if the server on its port proves it is it."""
    info = read_info()
    if info is None or not 0 < info.port < 65536:
        return None
    try:
        status, body = _request(info, "/api/instance")
        data = json.loads(body)
    except Exception:
        # Whatever answers there (nothing, SSH, a broken HTTP server...) is
        # not us; http.client errors are not OSErrors, so catch broadly.
        return None
    if status != 200 or not isinstance(data, dict):
        return None
    if data.get("token_ok") is not True or data.get("pid") != info.pid:
        return None
    jobs = data.get("active_jobs")
    return replace(info, active_jobs=jobs if isinstance(jobs, int) else 0)


def post(info: InstanceInfo, path: str, timeout: float = 3.0) -> int:
    """POST to an instance control endpoint; returns the HTTP status or 0."""
    try:
        return _request(info, path, method="POST", timeout=timeout)[0]
    except Exception:
        return 0


# Open lock files by instance token; the fd stays open (and locked) for the
# life of the server, and the kernel drops the lock if the process dies.
_held: dict[str, int] = {}
_held_guard = threading.Lock()


def _try_lock(fd: int) -> bool:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def claim(info: InstanceInfo) -> bool:
    """Take the launch lock: an advisory lock on ``instance.lock`` held until
    ``release`` or process exit (a crashed process releases it with it).

    The lock is per open file, so two claims in one process contend too.
    """
    ensure_dir()
    fd = os.open(lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    if not _try_lock(fd):
        os.close(fd)
        return False
    if sys.platform != "win32":
        # Informational only (the lock is what counts).  On Windows the
        # locked byte makes the file unwritable through other handles anyway.
        os.ftruncate(fd, 0)
        os.write(fd, str(info.pid).encode("ascii"))
    with _held_guard:
        _held[info.token] = fd
    return True


def claim_wait(info: InstanceInfo, timeout: float) -> bool:
    """``claim`` retried until ``timeout`` (a stopping instance releases soon)."""
    deadline = time.monotonic() + timeout
    while True:
        if claim(info):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def release(info: InstanceInfo) -> None:
    """Remove our instance file and drop our lock; idempotent.

    The lock file itself stays: unlinking it could let a later launcher lock
    a fresh inode while another still holds the old one.
    """
    current = read_info()
    if current is not None and current.token == info.token:
        try:
            instance_path().unlink()
        except FileNotFoundError:
            pass
    with _held_guard:
        fd = _held.pop(info.token, None)
    if fd is not None:
        _unlock(fd)
        os.close(fd)


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if os.name != "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def wait_port_free(port: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while not port_free(port):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


def parse_version(value: str) -> tuple[int, ...]:
    """``"0.10.2"`` -> ``(0, 10, 2)``; non-numeric tails are ignored and
    trailing zeros dropped so ``0.1`` equals ``0.1.0``."""
    parts: list[int] = []
    for piece in value.strip().lstrip("vV").split("."):
        match = re.match(r"\d+", piece)
        parts.append(int(match.group()) if match else 0)
    while parts and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def is_newer(mine: tuple[str, str], theirs: tuple[str, str]) -> bool:
    return (parse_version(mine[0]), mine[1]) > (parse_version(theirs[0]), theirs[1])


def decide(mine: tuple[str, str], live: InstanceInfo) -> Decision:
    """``mine`` is ``(version, build_id)`` of the launching copy."""
    if not is_newer(mine, (live.version, live.build_id)):
        return "open"
    return "takeover" if live.active_jobs == 0 else "open_and_offer"
