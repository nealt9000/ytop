"""Stick entry point: ``python -s -E boot.py [--home DIR] [--runtime-root DIR] [-- ytop args]``.

Standalone and stdlib-only: it runs before ``ytop`` is importable, so it
builds ``sys.path`` and the environment itself and only then imports the CLI.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import stat
import sys
import time
from collections.abc import Callable
from pathlib import Path

# Keep in step with ytop.runtime.current_platform (same five keys and rules).
EXIT_DAMAGED = 2
EXIT_NO_RUNTIME = 3
EXIT_REPAIRED = 5  # the launchers use 1-4
EXIT_SUDO = 6
DAMAGED = (
    "This YTOP stick is damaged: .ytop/build.json is missing or unreadable. "
    "Make the stick again."
)


# The upgrade switch (makestick imports these: one implementation).
OLD = ".ytop.old"
MAKING = ".ytop.making"
# Inside .ytop.old: one line per image file the switch adds (to undo).
JOURNAL = "upgrade-journal.txt"
# The image's own files at the top of .ytop/, replaced (never absent).
ROOT_FILES = ("boot.py", "build.json", "manifest.sha256")
# A root file being backed up into .ytop.old (incomplete until renamed).
PART_SUFFIX = ".part"
# A downloaded app update, staged on the stick it upgrades (ytop.appupdate).
MAKING_SRC = ".ytop.making-src"
# Held (O_EXCL) by the process making or upgrading a stick there:
# {"pid": ..., "started": <its OS start time or null>} and nothing about the
# computer (Ruling R-B8); touched again just before a switch starts.
LOCK = ".ytop.lock"
# A lock's start time and the live process's agree this closely (Linux
# reads whole-second boot times): otherwise the pid was recycled.
START_SLACK_S = 1.5
# A live lock older than this no longer stops a repair (a recycled pid must
# never keep a stick from starting for good); a switch takes seconds.
# Cleanup uses the same cap when start times are unknown (macOS).
SWITCH_LOCK_MAX_AGE_S = 3600.0
# An unreadable lock this young is being written, not torn by a crash.
LOCK_WRITE_GRACE_S = 60.0
UPDATING = (
    "YTOP is updating this stick; try again in a minute."
)


def switch_interrupted(target: Path) -> bool:
    """An upgrade stopped half way: ``.ytop.old`` exists, ``.ytop/app`` not."""
    return (target / OLD).is_dir() and not (target / ".ytop" / "app").exists()


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


def _clear(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif os.path.lexists(path):
        os.unlink(path)


def make_parents(folder: Path, touched: set[Path]) -> None:
    """``mkdir -p folder``, adding each new folder's parent to ``touched``
    (fsync those, or a power cut can lose the new entries)."""
    missing: list[Path] = []
    while not os.path.isdir(folder):
        missing.append(folder)
        folder = folder.parent
    for new in reversed(missing):
        new.mkdir(exist_ok=True)
        touched.add(new.parent)


def _exists_exactly(root: Path, rel: str, listings: dict[Path, list[str]]) -> bool:
    """``root/rel`` exists with exactly this case in every component.  On FAT
    ``foo.py`` "exists" while only the old ``Foo.py`` is there."""
    current = root
    for part in rel.split("/"):
        if current not in listings:
            try:
                listings[current] = os.listdir(current)
            except OSError:
                listings[current] = []
        if part not in listings[current]:
            return False
        current = current / part
    return True


def _inside(rel: str) -> bool:
    """``rel`` (a journal line) names a path inside ``.ytop``."""
    parts = rel.split("/")
    return "\\" not in rel and not any(p in ("", ".", "..") for p in parts)


def _move_aside(path: Path, stale: Path) -> None:
    """Rename ``path`` to ``stale`` (under .ytop.making), never deleting it."""
    _clear(stale)
    stale.parent.mkdir(parents=True, exist_ok=True)
    os.rename(path, stale)


def rollback_upgrade(target: Path) -> bool:
    """Undo an interrupted incremental upgrade switch; True if it did.

    Only a ``.ytop.old`` holding the journal is undone here (an older
    whole-piece swap has none; makestick restores those).  Files the switch
    added are moved to ``.ytop.making/stale-added/`` (folders that leaves
    empty are removed), every old file goes back (a new one in its way to
    ``.ytop.making/stale-new/``), and the old ``app`` returns last.  Nothing is deleted before the old file is back, so
    an interrupted rollback is finished by the next one."""
    ytop, old, making = target / ".ytop", target / OLD, target / MAKING
    if not switch_interrupted(target) or not (old / JOURNAL).is_file():
        return False
    # surrogateescape: a garbled journal (failing media) still reads, and a
    # name that is not UTF-8 matches os.listdir's spelling of it.
    added = (old / JOURNAL).read_text(
        encoding="utf-8", errors="surrogateescape"
    ).splitlines()
    listings: dict[Path, list[str]] = {}
    touched: set[Path] = {ytop}
    stops = {ytop, ytop / "lib", ytop / "runtime"}
    for rel in added:
        if not _inside(rel):
            continue  # never a name outside .ytop
        if _exists_exactly(ytop, rel, listings):
            _move_aside(ytop / rel, making / "stale-added" / rel)
            touched.add((ytop / rel).parent)
        # A folder the switch made (even one whose file never arrived), left
        # empty, would import as a namespace package, and on FAT would keep
        # its spelling for the old files.  Restoring remakes every folder an
        # old file needs, as it was spelled.
        parent = (ytop / rel).parent
        while parent not in stops and ytop in parent.parents:
            try:
                parent.rmdir()
            except (OSError, ValueError):
                break  # not empty, not there, or not a name here
            touched.add(parent.parent)
            parent = parent.parent
    for synced in sorted(touched):
        _fsync_dir(synced)
    restore: list[str] = []
    for current, dirs, names in os.walk(old):
        folder = Path(current).relative_to(old).as_posix()
        if folder == ".":
            dirs[:] = [d for d in dirs if d != "app"]
            names = [
                n for n in names
                if n != JOURNAL and not n.endswith(PART_SUFFIX)
            ]
            restore += names
        else:
            restore += [f"{folder}/{name}" for name in names]
    for rel in sorted(restore):
        if rel in ROOT_FILES:
            os.replace(old / rel, ytop / rel)
            continue
        if os.path.lexists(ytop / rel):
            _move_aside(ytop / rel, making / "stale-new" / rel)
        make_parents((ytop / rel).parent, touched)
        os.rename(old / rel, ytop / rel)
        touched.add((ytop / rel).parent)
    # Every restored file durable before its only other copy goes.
    for synced in sorted(touched):
        _fsync_dir(synced)
    if os.path.lexists(old / "app"):
        os.rename(old / "app", ytop / "app")
    _fsync_dir(ytop)
    shutil.rmtree(old)
    _fsync_dir(target)
    return True


def pid_alive(pid: int) -> bool:
    """A process with this id runs on this computer (stdlib only)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = getattr(ctypes, "windll").kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFO
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def process_start_time(pid: int | None = None) -> float | None:
    """When process ``pid`` (default: this one) started, in seconds since
    the epoch; None when unknown (macOS) or there is no such process.
    Mirrors ytop.startup.process_start_time (boot.py is stdlib-only)."""
    try:
        if sys.platform.startswith("linux"):
            with open(f"/proc/{pid or 'self'}/stat", encoding="ascii") as handle:
                text = handle.read()
            fields = text[text.rindex(")") + 2:].split()
            with open("/proc/stat", encoding="ascii") as handle:
                btime = next(
                    (line.split()[1] for line in handle if line.startswith("btime ")),
                    None,
                )
            if btime is None:
                return None
            return int(btime) + int(fields[19]) / os.sysconf("SC_CLK_TCK")
        if sys.platform == "win32":
            return _windows_start(pid)
    except Exception:  # unknown: callers fall back to the pid alone
        return None
    return None


def _windows_start(pid: int | None) -> float | None:
    import ctypes
    from ctypes import wintypes

    created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = []
    filetime_p = ctypes.POINTER(wintypes.FILETIME)
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [filetime_p] * 4
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.GetCurrentProcess()
    opened = None
    if pid is not None and pid != os.getpid():
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        opened = handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED
        if not opened:
            return None
    try:
        if not kernel32.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited),
            ctypes.byref(kernel), ctypes.byref(user),
        ):
            return None
    finally:
        if opened:
            kernel32.CloseHandle(opened)
    ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
    return ticks / 10_000_000 - 11_644_473_600  # FILETIME: 100 ns since 1601


def process_start_ticks(pid: int | None = None) -> int | None:
    """Linux: when process ``pid`` (default: this one) started, in clock
    ticks since boot (``/proc/<pid>/stat`` field 22); None elsewhere or when
    there is no such process.  Unlike :func:`process_start_time` it does not
    move when the wall clock is stepped (NTP just after boot)."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        with open(f"/proc/{pid or 'self'}/stat", encoding="ascii") as handle:
            text = handle.read()
        return int(text[text.rindex(")") + 2:].split()[19])
    except Exception:  # unknown: callers fall back to the start time
        return None


def lock_payload() -> dict:
    """What a lock records: this process, never the computer's name."""
    return {
        "pid": os.getpid(),
        "started": process_start_time(),
        "ticks": process_start_ticks(),
    }


def lock_holder(doc: object, alive: Callable[[int], bool] | None = None) -> str:
    """``"dead"``, ``"verified"`` (pid alive and its start matches) or
    ``"unverified"`` (pid alive, its start unknown) for a lock document.
    The start is compared in raw ticks where both are known (Linux), else
    as a wall-clock time.  An old lock's ``host`` field is ignored."""
    try:
        pid = int(doc["pid"])  # type: ignore[index]
    except (KeyError, TypeError, ValueError):
        return "dead"
    if not (alive or pid_alive)(pid):
        return "dead"
    ticks = doc.get("ticks") if isinstance(doc, dict) else None
    if isinstance(ticks, int) and not isinstance(ticks, bool):
        now_ticks = process_start_ticks(pid)
        if now_ticks is not None:
            return "verified" if now_ticks == ticks else "dead"
    started = doc.get("started") if isinstance(doc, dict) else None
    now = process_start_time(pid)
    if isinstance(started, (int, float)) and now is not None:
        return "verified" if abs(now - float(started)) <= START_SLACK_S else "dead"
    return "unverified"


def lock_live(
    home: Path,
    max_age_s: float | None = None,
    unverified_max_age_s: float | None = None,
) -> bool:
    """A live process holds ``home``'s make/upgrade lock (lock_holder).

    Older than ``max_age_s`` it is stale whatever; older than
    ``unverified_max_age_s`` too, when its start time cannot be checked (a
    recycled pid must not hold a stick for ever).  An unreadable lock is
    live only while it may still be being written."""
    try:
        seen = read_lock(home / LOCK)
    except FileNotFoundError:
        return False
    except OSError:
        # There but unreadable: doubtful, so held (within max_age_s).
        try:
            age = time.time() - os.stat(home / LOCK).st_mtime
        except FileNotFoundError:
            return False
        except OSError:
            age = 0.0
        return max_age_s is None or age <= max_age_s
    return _lock_seen_live(seen, max_age_s, unverified_max_age_s)


def _lock_seen_live(
    seen: tuple[bytes, int],
    max_age_s: float | None = None,
    unverified_max_age_s: float | None = None,
) -> bool:
    data, mtime_ns = seen
    age = time.time() - mtime_ns / 1e9
    if max_age_s is not None and age > max_age_s:
        return False
    try:
        doc = json.loads(data.decode("utf-8"))
    except ValueError:  # UnicodeDecodeError too
        return age < LOCK_WRITE_GRACE_S
    holder = lock_holder(doc)
    if holder == "unverified" and unverified_max_age_s is not None:
        return age <= unverified_max_age_s
    return holder != "dead"


def read_lock(path: Path) -> tuple[bytes, int]:
    """A lock file's bytes and mtime (ns): what :func:`break_stale_lock`
    checks it is still breaking.  Raises OSError (FileNotFoundError when
    there is no lock)."""
    with open(path, "rb") as handle:
        return handle.read(), os.fstat(handle.fileno()).st_mtime_ns


def break_stale_lock(
    path: Path, seen: tuple[bytes, int], before: Callable[[], None] | None = None,
) -> bool:
    """Remove the lock at ``path`` that was read as ``seen`` and judged
    stale; True if it is gone (by us or another breaker).

    Not delete-then-check: the lock is first renamed to a name only this
    process uses (atomic), and deleted only if it is still the very file
    judged stale.  If another breaker got there first and the renamed file
    is its fresh lock, it is put back and False returned: treat the lock as
    held.  ``before`` runs just before the rename (tests)."""
    aside = path.with_name(f"{path.name}.stale-{os.getpid()}-{os.urandom(4).hex()}")
    if before is not None:
        before()
    try:
        os.rename(path, aside)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        got: tuple[bytes, int] | None = read_lock(aside)
    except OSError:
        got = None
    if got == seen:
        try:
            os.unlink(aside)
        except OSError:
            pass
        return True
    _put_back(aside, path)
    return False


def _put_back(aside: Path, path: Path) -> None:
    """Return a live lock renamed by mistake, never over a newer one."""
    try:
        os.link(aside, path)
    except FileExistsError:
        pass
    except OSError:  # no hard links (FAT): rename, if the name is still free
        try:
            if not os.path.lexists(path):
                os.rename(aside, path)
                return
        except OSError:
            pass
    try:
        os.unlink(aside)
    except OSError:
        pass


def remove_tree_best_effort(path: Path) -> bool:
    """Delete ``path`` as far as possible; True if it is gone.

    A file that cannot be deleted (Windows: a ``python.exe``, ``.dll`` or
    ``.pyd`` the running YTOP still has open) is skipped, with the folders
    holding it; whatever is left is tried again at the next start."""
    if not os.path.lexists(path):
        return True
    if not path.is_dir() or path.is_symlink():
        try:
            os.unlink(path)
        except OSError:
            return False
        return True
    for current, dirs, names in os.walk(path, topdown=False):
        for name in names:
            _unlink_best_effort(Path(current) / name)
        for name in dirs:
            child = Path(current) / name
            try:
                if child.is_symlink():
                    os.unlink(child)
                else:
                    os.rmdir(child)
            except OSError:
                pass  # not empty: something in it is in use
    try:
        os.rmdir(path)
    except OSError:
        return False
    return True


def _unlink_best_effort(path: Path) -> None:
    try:
        os.unlink(path)
    except PermissionError:
        try:  # Windows will not delete a read-only file
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            os.unlink(path)
        except OSError:
            pass
    except OSError:
        pass


def clear_leftovers(home: Path) -> None:
    """Best effort, at every start after the repair check: delete what a
    finished (or abandoned) make, upgrade or app update left beside
    ``.ytop`` (``.ytop.old``, ``.ytop.making``, ``.ytop.making-src``).

    Never while a switch is still to be repaired, never while a live
    process holds the stick's lock (a stale one, left by a crash, is
    removed), and never anything inside ``.ytop`` (its ``previous/``
    rollback copies included).  ``.ytop.old`` loses its journal first, so
    no later start can mistake what is left for an interrupted switch."""
    try:
        if switch_interrupted(home):
            return
        try:
            seen: tuple[bytes, int] | None = read_lock(home / LOCK)
        except FileNotFoundError:
            seen = None
        if seen is not None:
            if _lock_seen_live(seen, unverified_max_age_s=SWITCH_LOCK_MAX_AGE_S):
                return
            if not break_stale_lock(home / LOCK, seen):
                return  # another process has just taken the stick
        old = home / OLD
        if old.is_dir():
            journal = old / JOURNAL
            if os.path.lexists(journal):
                try:
                    os.unlink(journal)
                except OSError:
                    return  # leave .ytop.old whole rather than half
            remove_tree_best_effort(old)
        for name in (MAKING, MAKING_SRC):
            if (home / name).is_dir():
                remove_tree_best_effort(home / name)
    except OSError:
        pass  # a start never fails over tidying up


def repair_interrupted_upgrade(home: Path, runtime_root: Path) -> bool:
    """Undo an upgrade that was cut off; True if there was one to deal with.

    A switch another live process is making right now (its lock, touched
    when the switch began) is not cut off: nothing is touched."""
    if not switch_interrupted(home):
        return False
    if lock_live(home, SWITCH_LOCK_MAX_AGE_S):
        print(UPDATING, file=sys.stderr)
        return True
    try:
        undone = rollback_upgrade(home)
    except (OSError, ValueError):  # ValueError: a journal it cannot use
        undone = False
    if not undone:  # failed, or a journal-less swap only make-stick restores
        print(
            "YTOP's last upgrade on this stick was interrupted and couldn't "
            "be undone here. Open YTOP from another stick and make or "
            "upgrade this one again.",
            file=sys.stderr,
        )
        return True
    if runtime_root != (home / ".ytop").resolve():
        # A Linux cache staged from the half-switched stick: stage again.
        try:
            (runtime_root / ".complete").unlink()
        except FileNotFoundError:
            pass
        except OSError:
            shutil.rmtree(runtime_root, ignore_errors=True)
    print(
        "YTOP's last upgrade on this stick was interrupted, so the previous "
        "version was put back. Start YTOP again.",
        file=sys.stderr,
    )
    return True


# The component store (ytop.components imports these: one implementation).
# Self-updated YouTube-facing parts live on the stick, outside the image, in
# ``.ytop/components/<name>/<version>/``; ``current.json`` says which
# version of each is current.
COMPONENTS_DIR = "components"
CURRENT_JSON = "current.json"
# Written last into a version folder (and removed first when one goes): a
# folder without it was cut off half way and is never imported.
COMPLETE = ".complete"
# The pure-Python components, in the order they go on sys.path.
PYTHON_COMPONENTS = ("yt-dlp", "yt-dlp-ejs", "bgutil-plugin", "youtube-transcript-api")
_VERSION_KEEP = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_+"
)
# Names Windows (and so FAT on Windows) will not have as a file or folder.
_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)


def version_dir(version: object) -> str | None:
    """The folder name of a component version, safe on FAT (None: unusable).

    ``2026.10.26`` stays as it is; any other character becomes ``_``, a
    trailing dot (Windows drops it) too, and a reserved device name gets a
    leading ``_``."""
    if not isinstance(version, str) or version in ("", ".", ".."):
        return None
    name = "".join(ch if ch in _VERSION_KEEP else "_" for ch in version)
    if name.endswith("."):
        name = name[:-1] + "_"
    if name.split(".")[0].upper() in _RESERVED:
        name = "_" + name
    return name


def read_current_doc(ytop: Path) -> dict:
    """``components/current.json`` as a dict; ``{}`` if missing or garbled."""
    try:
        doc = json.loads(
            (ytop / COMPONENTS_DIR / CURRENT_JSON).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):  # JSONDecodeError and UnicodeError too
        return {}
    return doc if isinstance(doc, dict) else {}


def component_paths(ytop: Path) -> list[Path]:
    """The current folder of each Python component that has one, in
    ``PYTHON_COMPONENTS`` order.  A component missing here (no current.json,
    a garbled one, or a version folder that is absent or incomplete, i.e.
    without its ``COMPLETE`` marker) is imported from the image's
    ``lib/common`` instead."""
    current = read_current_doc(ytop).get("current")
    if not isinstance(current, dict):
        return []
    paths = []
    for name in PYTHON_COMPONENTS:
        folder = version_dir(current.get(name))
        if folder is None:
            continue
        path = ytop / COMPONENTS_DIR / name / folder
        if (path / COMPLETE).is_file():
            paths.append(path)
    return paths


def platform_key() -> str | None:
    machine = platform.machine().lower()
    if sys.platform.startswith("linux"):
        if machine in ("x86_64", "amd64"):
            return "linux-x86_64"
        if machine in ("aarch64", "arm64"):
            return "linux-aarch64"
    elif sys.platform == "darwin":
        if machine == "arm64":
            return "macos-arm64"
        if machine == "x86_64":
            return "macos-x86_64"
    elif sys.platform == "win32":
        if machine in ("amd64", "x86_64", "arm64"):
            return "windows-x86_64"
    return None


SUDO_REFUSED = (
    "Don't run YTOP with sudo: start it as your own user. Run as root it "
    "would leave files your own YTOP can't change, and it wouldn't see the "
    "YTOP you already have running."
)


def under_sudo() -> bool:
    """Running as root through sudo (a real root login, e.g. in a
    container, is one user throughout and fine).  POSIX only."""
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() == 0 and bool(os.environ.get("SUDO_UID"))


def main(argv: list[str] | None = None) -> int:
    # When this start began, for YTOP's "Started in …" timing line.
    os.environ["YTOP_BOOT_T0"] = repr(time.time())
    argv = list(sys.argv[1:] if argv is None else argv)
    passthrough: list[str] = []
    if "--" in argv:
        split = argv.index("--")
        argv, passthrough = argv[:split], argv[split + 1:]
    ap = argparse.ArgumentParser(prog="boot.py")
    ap.add_argument("--home", default=None)
    ap.add_argument("--runtime-root", default=None)
    opts = ap.parse_args(argv)
    if under_sudo():
        print(SUDO_REFUSED, file=sys.stderr)
        return EXIT_SUDO

    runtime_root = Path(opts.runtime_root or Path(__file__).resolve().parent).resolve()
    home = Path(opts.home).resolve() if opts.home else runtime_root.parent
    if repair_interrupted_upgrade(home, runtime_root):
        return EXIT_REPAIRED
    clear_leftovers(home)
    try:
        build = json.loads((runtime_root / "build.json").read_text(encoding="utf-8"))
        if not isinstance(build, dict):
            raise ValueError("build.json is not an object")
    except (OSError, ValueError):  # JSONDecodeError and UnicodeError too
        print(DAMAGED, file=sys.stderr)
        return EXIT_DAMAGED
    key = platform_key()
    if key is None or key not in build.get("platforms", []):
        label = key or f"{sys.platform}/{platform.machine()}"
        print(
            f"This YTOP stick has no runtime for {label}. "
            "Make the stick again with that platform included.",
            file=sys.stderr,
        )
        return EXIT_NO_RUNTIME

    image_root = home / ".ytop"
    # Components come from the stick, even when the rest runs from the
    # Linux cache: they change without a new build, so the cache (one per
    # build) never holds them.  The stick is there at launch.
    sys.path[0:0] = [
        str(runtime_root / "app"),
        *(str(path) for path in component_paths(image_root)),
        str(runtime_root / "lib" / "common"),
        str(runtime_root / "lib" / key),
    ]
    os.environ["YTOP_HOME"] = str(home)
    os.environ["YTOP_RUNTIME"] = str(runtime_root / "runtime" / key)
    os.environ["YTOP_BUILD_ID"] = str(build.get("build_id", ""))
    os.environ["YTOP_IMAGE_ROOT"] = str(
        image_root if (image_root / "build.json").is_file() else runtime_root
    )

    args = passthrough or ["serve", "--portable", "--open"]
    if os.environ.get("YTOP_NO_BROWSER") == "1":
        args = ["--no-open" if a == "--open" else a for a in args]

    from ytop.cli import main as ytop_main

    return int(ytop_main(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
