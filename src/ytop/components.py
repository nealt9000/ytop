"""The component store: self-updated YouTube-facing parts, versioned.

Layout, on the stick (never part of the image, so never in its manifest and
never touched by an upgrade's switch)::

    .ytop/components/
      current.json                         # which version of each is current
      <name>/<version>/                    # pure Python: packages + dist-info,
                                           #   bytecode, and .complete (last)
      deno/<version>/<plat>/deno[.exe]     # one binary per platform
      bgutil-server/<version>/server/

``current.json`` is ``{"schema": 1, "current": {name: version}, "previous":
{name: version}, "pinned_bad": {name: [versions]}, "last_check": iso,
"last_result": str}``, written atomically.  ``boot.py`` reads it (stdlib
only, the same helpers) to put each current Python component on
``sys.path`` before the image's ``lib/common``, which keeps its own copy as
the always-present fallback.  The image's ``runtime/<plat>/bin/deno`` is the
fallback for Deno.  A build seeds only the Python components: the store
holds no Deno, and ``current.json`` names none, until the updater downloads
a newer one (ruling R-B4: the spec's size budget).

A version folder appears under its real name only when complete (it is
filled under a ``.ytop-tmp`` name and renamed in), and goes the same way
(marker first, then renamed to a temp name, then deleted); a Python
component's folder without its ``.complete`` marker is never imported.
Versions are ordered as PEP 440 orders them (:func:`_version_key`).  Pins
(``pinned_bad``) are per stick and merge as a union: a pinned version never
becomes current.  :mod:`ytop.updater` fills the store.

Note: ``yt_dlp_plugins`` (the bgutil plugin) is a namespace package, so the
store's copy and ``lib/common``'s merge.  The store's modules win, but a
module a newer plugin dropped stays importable from ``lib/common``.

Linux runs YTOP from a cache copy because FAT sticks are mounted
``showexec`` (nothing on them can be run).  Python components are imported
straight from the stick; a store Deno newer than the cache's own is copied
into the cache and made executable (:func:`executable_deno`).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import sys
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .parallel import run_all
from .stick.boot import (
    COMPLETE,
    COMPONENTS_DIR,
    CURRENT_JSON,
    PYTHON_COMPONENTS,
    component_paths,
    process_start_ticks,
    read_current_doc,
    version_dir,
)

__all__ = [
    "COMPLETE",
    "COMPONENT_DISTS",
    "PYTHON_COMPONENTS",
    "Current",
    "deno_for",
    "executable_deno",
    "is_pinned",
    "is_prerelease",
    "merge_newest",
    "merge_size",
    "python_paths",
    "read_current",
    "server_home",
    "version_dir",
    "write_current",
]

SCHEMA = 1
DENO = "deno"
# The distribution behind each pure-Python component.
COMPONENT_DISTS = {
    "yt-dlp": "yt-dlp",
    "yt-dlp-ejs": "yt-dlp-ejs",
    "bgutil-plugin": "bgutil-ytdlp-pot-provider",
    "youtube-transcript-api": "youtube-transcript-api",
}
# A version folder (or Deno platform folder) being copied in.
TEMP_SUFFIX = ".ytop-tmp"
_NAME = re.compile(r"[a-z0-9][a-z0-9-]*\Z")

MergeProgress = Callable[[str], None]


@dataclass
class Current:
    current: dict[str, str] = field(default_factory=dict)
    previous: dict[str, str] = field(default_factory=dict)
    pinned_bad: dict[str, list[str]] = field(default_factory=dict)
    last_check: str = ""
    last_result: str = ""


def store_dir(ytop: Path) -> Path:
    return ytop / COMPONENTS_DIR


def _versions(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {
        name: version for name, version in raw.items()
        if isinstance(name, str) and _NAME.match(name)
        and isinstance(version, str) and version_dir(version) is not None
    }


def read_current(ytop: Path) -> Current:
    """The store's ``current.json``; empty if missing or garbled (entries
    that make no sense are dropped)."""
    doc = read_current_doc(ytop)
    pinned: dict[str, list[str]] = {}
    raw_pinned = doc.get("pinned_bad")
    if isinstance(raw_pinned, dict):
        for name, versions in raw_pinned.items():
            if isinstance(name, str) and _NAME.match(name) and isinstance(versions, list):
                pinned[name] = [v for v in versions if isinstance(v, str)]
    last_check, last_result = doc.get("last_check"), doc.get("last_result")
    return Current(
        current=_versions(doc.get("current")),
        previous=_versions(doc.get("previous")),
        pinned_bad=pinned,
        last_check=last_check if isinstance(last_check, str) else "",
        last_result=last_result if isinstance(last_result, str) else "",
    )


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return  # Windows: a rename is as durable as it gets
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_current(ytop: Path, current: Current) -> None:
    """Atomic: temp file, fsync, ``os.replace``.  A crash leaves the old
    file (or none) in place, never half of the new one."""
    folder = store_dir(ytop)
    folder.mkdir(parents=True, exist_ok=True)
    doc = {
        "schema": SCHEMA,
        "current": dict(sorted(current.current.items())),
        "previous": dict(sorted(current.previous.items())),
        "pinned_bad": {k: list(v) for k, v in sorted(current.pinned_bad.items())},
        "last_check": current.last_check,
        "last_result": current.last_result,
    }
    path = folder / CURRENT_JSON
    temp = path.with_name(path.name + TEMP_SUFFIX)
    with open(temp, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(doc, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temp, path)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass
        raise
    _fsync_dir(folder)


def python_paths(ytop: Path) -> list[Path]:
    """The current Python component folders, in ``PYTHON_COMPONENTS`` order
    (the ones ``boot.py`` puts on ``sys.path``)."""
    return component_paths(ytop)


BGUTIL_SERVER = "bgutil-server"


def server_home(ytop: Path) -> Path | None:
    """The current bgutil PO-token server's ``server/`` folder (it holds
    ``src/generate_once.ts`` and ``node_modules``), or None when the store
    names none or its folder is not there."""
    version = read_current(ytop).current.get(BGUTIL_SERVER)
    folder = None if version is None else _component_dir(ytop, BGUTIL_SERVER, version)
    if folder is None:
        return None
    home = folder / "server"
    return home if (home / "src" / "generate_once.ts").is_file() else None


def deno_exe(plat: str) -> str:
    return "deno.exe" if plat.startswith("windows") else "deno"


def _component_dir(ytop: Path, name: str, version: str) -> Path | None:
    folder = version_dir(version)
    return None if folder is None else store_dir(ytop) / name / folder


def _deno_in(ytop: Path, version: str, plat: str) -> Path | None:
    folder = _component_dir(ytop, DENO, version)
    if folder is None:
        return None
    exe = folder / plat / deno_exe(plat)
    return exe if exe.is_file() else None


def deno_for(ytop: Path, plat: str) -> Path | None:
    """The store's current Deno for ``plat``, or None (use the runtime's)."""
    version = read_current(ytop).current.get(DENO)
    return None if version is None else _deno_in(ytop, version, plat)


# --- an executable Deno --------------------------------------------------------


def _executable(path: Path) -> bool:
    return os.access(path, os.X_OK)


def _image_deno_version(runtime_root: Path) -> str | None:
    """The Deno version the image (whose runtime root this is) was built
    with, from its ``build.json``."""
    try:
        build = json.loads((runtime_root / "build.json").read_text(encoding="utf-8"))
        version = build["sources"]["deno"]["version"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return version if isinstance(version, str) else None


def _same_place(one: Path, two: Path) -> bool:
    try:
        return one.resolve() == two.resolve()
    except OSError:
        return False


def executable_deno(ytop: Path, plat: str, runtime_dir: Path) -> Path | None:
    """The Deno to run: the store's current one where it can be run, else
    the runtime's ``bin/deno`` (``runtime_dir`` is ``YTOP_RUNTIME``).

    On Linux the runtime is a cache copy and the stick is usually mounted
    without exec permission.  A store Deno that cannot be run there is then
    copied into the cache (``<cache>/components/deno/<version>/<plat>/``,
    made executable, reused while it lasts), unless it is the same version
    as the cache's own; if the copy cannot be made the cache's own is used.
    """
    fallback: Path | None = runtime_dir / "bin" / deno_exe(plat)
    if fallback is not None and not fallback.is_file():
        fallback = None
    store = deno_for(ytop, plat)
    if store is None:
        return fallback
    if _executable(store):
        return store
    version = read_current(ytop).current[DENO]
    runtime_root = runtime_dir.parent.parent
    if fallback is not None and _image_deno_version(runtime_root) == version:
        return fallback
    if _same_place(runtime_root, ytop):
        return fallback  # running from the stick: a copy there won't run either
    folder = version_dir(version)
    assert folder is not None  # deno_for found it
    dest = runtime_root / COMPONENTS_DIR / DENO / folder / plat / deno_exe(plat)
    stamp = dest.with_name(dest.name + ".stamp")
    try:
        source = store.stat()
        identity = {"size": source.st_size, "mtime_ns": source.st_mtime_ns}
    except OSError:
        return fallback
    try:
        info = dest.stat()
        recorded = json.loads(stamp.read_text(encoding="utf-8"))
        if (
            stat.S_ISREG(info.st_mode) and info.st_mode & stat.S_IXUSR
            and info.st_size == source.st_size
            and isinstance(recorded, dict)
            and {k: recorded.get(k) for k in identity} == identity
        ):
            return dest
    except (OSError, ValueError):
        pass
    # Per process: two YTOPs starting at once never write the same file.
    temp = dest.with_name(f"{dest.name}.{os.getpid()}{TEMP_SUFFIX}")
    stamp_temp = stamp.with_name(f"{stamp.name}.{os.getpid()}{TEMP_SUFFIX}")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        digest = _copy_hashed(store, temp)
        if _sha256(temp) != digest:
            raise OSError("the copied Deno does not match the stick's")
        os.chmod(temp, 0o755)
        try:
            stamp.unlink()
        except FileNotFoundError:
            pass
        os.replace(temp, dest)
        stamp_temp.write_text(
            json.dumps({**identity, "sha256": digest}) + "\n", encoding="utf-8"
        )
        os.replace(stamp_temp, stamp)
    except OSError:
        for leftover in (temp, stamp_temp):
            try:
                leftover.unlink()
            except OSError:
                pass
        return fallback
    return dest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _copy_hashed(source: Path, dest: Path) -> str:
    """Copy ``source`` to ``dest`` (no metadata); the sha256 of what was read."""
    digest = hashlib.sha256()
    with open(source, "rb") as src, open(dest, "wb") as dst:
        for block in iter(lambda: src.read(1 << 20), b""):
            digest.update(block)
            dst.write(block)
    return digest.hexdigest()


# --- merging two stores --------------------------------------------------------


_PEP440 = re.compile(
    r"""^\s*v?
    (?P<release>[0-9]+(?:\.[0-9]+)*)
    (?:[-_.]?(?P<pre_l>alpha|a|beta|b|preview|pre|c|rc)[-_.]?(?P<pre_n>[0-9]*))?
    (?:-(?P<post_n1>[0-9]+)|[-_.]?(?P<post_l>post|rev|r)[-_.]?(?P<post_n2>[0-9]*))?
    (?:[-_.]?(?P<dev_l>dev)[-_.]?(?P<dev_n>[0-9]*))?
    (?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?
    \s*$""",
    re.VERBOSE | re.IGNORECASE,
)
_PRE_RANK = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "c": 2, "rc": 2, "pre": 2, "preview": 2}
VersionKey = tuple[int, tuple[int, ...], tuple[int, int], int, tuple[int, int], str]


def _version_key(version: str) -> VersionKey:
    """A sort key that orders versions as PEP 440 does (``1.0rc1 < 1.0 <
    1.0.post1``; dev releases first), with yt-dlp's date versions in order
    too (``2026.10.26 < 2026.11.1 < 2026.11.1.232815``, a nightly).  A
    version that is not PEP 440 sorts by its numbers, as a final release.
    ``packaging`` is not in the stick's ``lib/common``, hence this."""
    match = _PEP440.match(version)
    if match is None:
        numbers = tuple(int(n) for n in re.findall(r"\d+", version))
        return (0, _trim(numbers), (1, 0), -1, (1, 0), version)
    release = _trim(tuple(int(n) for n in match["release"].split(".")))
    pre_l, dev_l = match["pre_l"], match["dev_l"]
    post = match["post_n1"] or match["post_n2"]
    has_post = match["post_n1"] is not None or match["post_l"] is not None
    if pre_l:
        pre = (0, _PRE_RANK[pre_l.lower()] * 100_000 + int(match["pre_n"] or 0))
    elif dev_l and not has_post:
        pre = (-1, 0)  # 1.0.dev1 comes before 1.0a1
    else:
        pre = (1, 0)
    dev = (0, int(match["dev_n"] or 0)) if dev_l else (1, 0)
    return (1, release, pre, int(post or 0) if has_post else -1, dev, match["local"] or "")


def _trim(numbers: tuple[int, ...]) -> tuple[int, ...]:
    while len(numbers) > 1 and numbers[-1] == 0:
        numbers = numbers[:-1]
    return numbers


def is_prerelease(version: str) -> bool:
    """An alpha, beta, release candidate or dev release (PEP 440)."""
    match = _PEP440.match(version)
    return match is not None and bool(match["pre_l"] or match["dev_l"])


def is_pinned(version: str | None, pins: Iterable[str]) -> bool:
    """``version`` is pinned bad, under any spelling (``2026.11.01`` is
    ``2026.11.1``)."""
    if version is None:
        return False
    key = _version_key(version)
    return any(_version_key(pin) == key for pin in pins)


def _usable(name: str, folder: Path | None) -> bool:
    """A version folder that is there and complete (Python components:
    their ``COMPLETE`` marker; others: the folder)."""
    if folder is None:
        return False
    if name in PYTHON_COMPONENTS:
        return (folder / COMPLETE).is_file()
    return folder.is_dir()


def _order(names: set[str]) -> list[str]:
    first = [n for n in (*PYTHON_COMPONENTS, DENO) if n in names]
    return first + sorted(names - set(first))


@dataclass
class _Copy:
    label: str
    source: Path  # a folder
    dest: Path  # a folder, absent until renamed in
    executable: bool = False  # a non-Windows Deno
    marker: bool = False  # write COMPLETE last (a version folder)


@dataclass
class _Plan:
    current: Current
    copies: list[_Copy]
    changed: list[str]  # names whose current version changes


def _plan(source: Path, target: Path, platforms: list[str]) -> _Plan:
    src = read_current(source)
    dst = read_current(target)
    new = Current(
        current=dict(dst.current),
        previous=dict(dst.previous),
        pinned_bad={k: list(v) for k, v in dst.pinned_bad.items()},
        last_check=dst.last_check,
        last_result=dst.last_result,
    )
    for name, versions in src.pinned_bad.items():
        merged = new.pinned_bad.setdefault(name, [])
        merged += [v for v in versions if v not in merged]
    copies: list[_Copy] = []
    changed: list[str] = []
    for name in _order(set(src.current) | set(dst.current)):
        bad = set(new.pinned_bad.get(name, []))
        s_ver, t_ver = src.current.get(name), dst.current.get(name)
        if name == DENO:
            chosen = _choose_deno(source, target, s_ver, t_ver, bad, platforms)
            if chosen is None:
                continue
            folder = _component_dir(target, DENO, chosen)
            assert folder is not None
            for plat in platforms:
                if _deno_in(target, chosen, plat) is None:
                    if _deno_in(source, chosen, plat) is not None:
                        src_folder = _component_dir(source, DENO, chosen)
                        assert src_folder is not None
                        copies.append(_Copy(
                            f"Copying Deno {chosen} for {plat}…",
                            src_folder / plat, folder / plat,
                            executable=not plat.startswith("windows"),
                        ))
            if chosen != t_ver:
                new.current[DENO] = chosen
                new.previous.pop(DENO, None)  # Deno keeps no rollback copy
                changed.append(DENO)
            continue
        s_dir = _component_dir(source, name, s_ver) if s_ver else None
        t_dir = _component_dir(target, name, t_ver) if t_ver else None
        s_ok = s_ver is not None and not is_pinned(s_ver, bad) and _usable(name, s_dir)
        t_ok = _usable(name, t_dir)
        # A current version pinned bad (by either stick) gives way to any
        # good one, even an older one.
        t_good = t_ok and not is_pinned(t_ver, bad)
        if not s_ok:
            continue
        assert s_ver is not None and s_dir is not None
        if t_good and t_ver is not None and _version_key(s_ver) <= _version_key(t_ver):
            continue
        dest = _component_dir(target, name, s_ver)
        assert dest is not None
        if not _usable(name, dest):
            copies.append(_Copy(f"Copying {name} {s_ver}…", s_dir, dest, marker=True))
        if s_ver != t_ver:
            new.current[name] = s_ver
            if name in PYTHON_COMPONENTS and t_good and t_ver is not None:
                new.previous[name] = t_ver
            else:
                new.previous.pop(name, None)
            changed.append(name)
    return _Plan(new, copies, changed)


def _choose_deno(
    source: Path,
    target: Path,
    s_ver: str | None,
    t_ver: str | None,
    bad: set[str],
    platforms: list[str],
) -> str | None:
    """The Deno version the target should have: the newest one that source
    and target together hold for every platform the target carries; else
    the target's own; else whatever the source has."""

    def coverage(version: str) -> set[str]:
        return {
            plat for plat in platforms
            if _deno_in(source, version, plat) or _deno_in(target, version, plat)
        }

    full = [
        v for v in {s_ver, t_ver}
        if v is not None and (not is_pinned(v, bad) or v == t_ver)
        and coverage(v) >= set(platforms)
    ]
    if full:
        return max(full, key=_version_key)
    if t_ver is not None and any(_deno_in(target, t_ver, p) for p in platforms):
        return t_ver
    if s_ver is not None and not is_pinned(s_ver, bad) and coverage(s_ver):
        return s_ver
    return None


def _files(folder: Path) -> list[Path]:
    return [p for p in sorted(folder.rglob("*")) if p.is_file() and not p.is_symlink()]


def merge_size(source_ytop: Path, target_ytop: Path, platforms: list[str]) -> int:
    """Bytes :func:`merge_newest` would copy."""
    plan = _plan(source_ytop, target_ytop, platforms)
    return sum(p.stat().st_size for copy in plan.copies for p in _files(copy.source))


def _copy_in(copy: _Copy, on_file: Callable[[int], None] | None = None) -> None:
    """Copy a folder to ``<dest>.ytop-tmp``, then rename it in: a folder
    under its real name is always complete.  A folder already there under
    that name (one a cut-off install left) is replaced."""
    temp = copy.dest.with_name(copy.dest.name + TEMP_SUFFIX)
    if temp.exists():
        shutil.rmtree(temp)
    temp.mkdir(parents=True)
    lock = threading.Lock()
    paths = [
        path for path in _files(copy.source)
        # The marker is written last, below.
        if not (copy.marker and path.relative_to(copy.source).as_posix() == COMPLETE)
    ]

    def one(path: Path, _stopping: Callable[[], bool]) -> None:
        rel = path.relative_to(copy.source)
        (temp / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, temp / rel)  # no metadata: FAT refuses some
        if copy.executable:
            try:
                os.chmod(temp / rel, 0o755)
            except OSError:
                pass  # FAT: Mac and Windows run it anyway
        if on_file is not None:
            with lock:  # it may raise to cancel: that stops the rest
                on_file(path.stat().st_size)

    # A few at a time: thousands of small files (ytop.parallel).
    run_all(paths, one)
    if copy.marker:
        mark_complete(temp)
        if on_file is not None:
            on_file(0)
    _fsync_dir(temp)
    move_in(temp, copy.dest)


def mark_complete(folder: Path) -> None:
    """Write the ``COMPLETE`` marker, durably, into a filled version folder."""
    with open(folder / COMPLETE, "w", encoding="utf-8") as handle:
        handle.flush()
        os.fsync(handle.fileno())


def move_in(temp: Path, dest: Path) -> None:
    """Rename a filled folder in as ``dest``, replacing (and then deleting)
    whatever is there under that name."""
    if os.path.lexists(dest):
        _discard(dest)
    os.rename(temp, dest)
    _fsync_dir(dest.parent)


def _discard(folder: Path) -> None:
    """Delete a version folder without ever leaving part of it under its
    real name: its marker goes first, then it is renamed to a temp name
    (``_clear_temps`` finishes a delete that is cut off)."""
    try:
        (folder / COMPLETE).unlink()
    except OSError:
        pass
    temp = folder.with_name(folder.name + ".old" + TEMP_SUFFIX)
    if os.path.lexists(temp):
        shutil.rmtree(temp, ignore_errors=True)
    try:
        os.rename(folder, temp)
    except OSError:
        shutil.rmtree(folder, ignore_errors=True)
        return
    _fsync_dir(folder.parent)
    shutil.rmtree(temp, ignore_errors=True)


def _clear_temps(target: Path) -> None:
    """Folders a cut-off merge left half-copied."""
    root = store_dir(target)
    if not root.is_dir():
        return
    for temp in sorted(root.rglob(f"*{TEMP_SUFFIX}"), reverse=True):
        if temp.is_dir():
            shutil.rmtree(temp, ignore_errors=True)


# --- versions in use ---------------------------------------------------------------
#
# A running app imported its Python components at start and keeps reading
# their folders (yt-dlp imports extractors lazily; yt-dlp-ejs reads its solver
# files when it solves); it also runs the Deno and bgutil server it found
# then.  Pruning must never delete them: this process protects those folders,
# and each running app leaves a marker, ``components/.in-use/<pid>-<token>.json``
# (the token is random per process; nothing about the computer or its user is
# written), so a second app on the same stick protects them too.  A marker is
# live while its pid is alive and, when both are known, that process started
# when the marker says (so a reused pid does not count); otherwise it is stale
# and removed.  A marker another computer left whose pid happens to be alive
# here just keeps its folders (only disk space) until that pid is gone.

IN_USE_DIR = ".in-use"
START_SLACK_S = 1.5
_protected: set[str] = set()
_protected_lock = threading.Lock()
_token = secrets.token_hex(4)


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def protect(folders: Iterable[Path]) -> None:
    """Never prune these version folders while this process runs."""
    with _protected_lock:
        _protected.update(_norm(Path(f)) for f in folders)


def unprotect(folders: Iterable[Path]) -> None:
    with _protected_lock:
        _protected.difference_update(_norm(Path(f)) for f in folders)


def runtime_folders(ytop: Path, plat: str | None) -> list[Path]:
    """The store's Deno version folder (when ``deno_for`` finds this
    platform's Deno there) and bgutil server folder (when ``server_home``
    finds one): what an app starting now runs besides its Python imports."""
    found: list[Path] = []
    if plat is not None:
        deno = deno_for(ytop, plat)
        if deno is not None:
            found.append(deno.parent.parent)  # deno/<version>/<plat>/deno
    home = server_home(ytop)
    if home is not None:
        found.append(home.parent)  # bgutil-server/<version>/server
    return found


def _marker(ytop: Path) -> Path:
    return store_dir(ytop) / IN_USE_DIR / f"{os.getpid()}-{_token}.json"


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if sys.platform == "win32":
        import ctypes

        kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def marker_live(pid: int, started: float | None, ticks: int | None = None) -> bool:
    """The process that wrote a marker is still running (compared by raw
    start ticks where known: a wall-clock step does not move those)."""
    if not pid_alive(pid):
        return False
    if ticks is not None:
        now_ticks = process_start_ticks(pid)
        if now_ticks is not None:
            return now_ticks == ticks
    if started is None:
        return True
    from .startup import process_start_time

    now = process_start_time(pid)
    return now is None or abs(now - started) <= START_SLACK_S


def write_in_use(ytop: Path, folders: Iterable[Path]) -> Path | None:
    """Record, for other apps on this stick, the store folders this process
    uses.  Returns the marker (None: nothing to record, or a read-only
    stick)."""
    from .startup import process_start_time

    store = store_dir(ytop)
    rel = []
    for folder in folders:
        try:
            rel.append(Path(os.path.abspath(folder)).relative_to(os.path.abspath(store)).as_posix())
        except ValueError:
            continue
    if not rel:
        return None
    marker = _marker(ytop)
    temp = marker.with_name(marker.name + TEMP_SUFFIX)
    doc = {
        "pid": os.getpid(), "started": process_start_time(),
        "ticks": process_start_ticks(), "folders": sorted(set(rel)),
    }
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(doc, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, marker)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass
        return None
    return marker


def remove_in_use(ytop: Path) -> None:
    try:
        _marker(ytop).unlink()
    except OSError:
        pass


def _marker_pid(name: str) -> int | None:
    head = name.split("-", 1)[0]
    return int(head) if head.isdigit() else None


def in_use_folders(ytop: Path) -> set[str]:
    """Store folders (``<name>/<version>``) a live app on this stick uses;
    stale markers, and temp files a crash left, are removed."""
    folder = store_dir(ytop) / IN_USE_DIR
    found: set[str] = set()
    try:
        entries = [p for p in folder.iterdir() if p.is_file()]
    except OSError:
        return found
    for entry in entries:
        if entry.name.endswith(TEMP_SUFFIX):
            pid = _marker_pid(entry.name)
            if pid is None or not pid_alive(pid):  # a live one may be writing it
                _unlink(entry)
            continue
        if entry.suffix != ".json":
            continue
        try:
            doc = json.loads(entry.read_text(encoding="utf-8"))
            pid = int(doc["pid"])
            raw = doc.get("started")
            started = float(raw) if isinstance(raw, (int, float)) else None
            raw_ticks = doc.get("ticks")
            ticks = raw_ticks if type(raw_ticks) is int else None
            folders = [str(f) for f in doc["folders"]]
        except (OSError, ValueError, KeyError, TypeError):
            continue  # garbled: no claim, but not ours to delete
        if not marker_live(pid, started, ticks):
            _unlink(entry)
            continue
        found.update(folders)
    return found


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _prune(target: Path, current: Current, names: list[str]) -> None:
    """Drop versions of ``names`` that are neither current nor (Python
    components only) the rollback copy, nor imported by a running app (this
    process's protected folders, or another's in-use marker)."""
    in_use = in_use_folders(target)
    with _protected_lock:
        protected = set(_protected)
    for name in names:
        keep = {version_dir(current.current.get(name))}
        if name in PYTHON_COMPONENTS:
            keep.add(version_dir(current.previous.get(name)))
        folder = store_dir(target) / name
        try:
            entries = list(folder.iterdir())
        except OSError:
            continue
        for entry in entries:
            if (
                entry.is_dir() and entry.name not in keep
                and not entry.name.endswith(TEMP_SUFFIX)
                and f"{name}/{entry.name}" not in in_use
                and _norm(entry) not in protected
            ):
                _discard(entry)


def merge_newest(
    source_ytop: Path,
    target_ytop: Path,
    platforms: list[str],
    progress: MergeProgress | None,
    *,
    on_plan: Callable[[int, int], None] | None = None,
    on_file: Callable[[int], None] | None = None,
) -> None:
    """Make the target's store hold the newer of source and target for each
    component, and point its ``current.json`` at it.

    Deno is taken only in a version available for every one of
    ``platforms`` (the target's).  A version pinned bad on either side is
    never taken.  ``progress`` gets a label before each copy and may raise
    (a cancel) between copies; ``current.json`` changes only after every
    copy is in place."""
    if not read_current(source_ytop).current:
        return
    _clear_temps(target_ytop)
    plan = _plan(source_ytop, target_ytop, platforms)
    if plan.copies and on_plan is not None:
        listed = [p for copy in plan.copies for p in _files(copy.source)]
        extra = sum(1 for copy in plan.copies if copy.marker and not any(
            p.relative_to(copy.source).as_posix() == COMPLETE
            for p in _files(copy.source)))
        on_plan(len(listed) + extra, sum(p.stat().st_size for p in listed))
    for copy in plan.copies:
        if progress is not None:
            progress(copy.label)
        _copy_in(copy, on_file)
    if plan.changed or plan.current.pinned_bad != read_current(target_ytop).pinned_bad:
        write_current(target_ytop, plan.current)
    _prune(target_ytop, plan.current, plan.changed)
