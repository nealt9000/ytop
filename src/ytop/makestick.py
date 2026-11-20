"""Make and upgrade YTOP sticks: any stick can make more sticks.

The source is a stick *image* (the ``.ytop`` folder holding ``boot.py``,
``build.json``, ``app/``, ``lib/`` and ``runtime/``): the stick YTOP runs
from (``$YTOP_IMAGE_ROOT``, set by ``boot.py``) or, for development, a built
``dist/stick/.ytop``.  A target is any folder, usually the root of a drive;
nothing is ever formatted.

Whatever is copied goes into ``<target>/.ytop.making/`` first, and every
copied file is hashed against the image's ``manifest.sha256`` before
anything on the stick changes.  In every switch ``app/`` leaves
``.ytop/`` first and arrives last, so ``.ytop/app`` missing while
``.ytop.old/`` exists means "roll back".

* **A new stick** gets the whole image (and the manifest, filtered to its
  platforms), switched in piece by piece: ``lib``, ``runtime``, the root
  files, ``app`` last.  Its library marker is written after that; a stick
  without the marker is never listed as a library.
* **An upgrade** compares the image's manifest with the stick's
  (:func:`diff_upgrade`) and copies only new or changed files, plus all of
  ``app/`` when any of it changed.  The switch then moves single files
  under a journal (``.ytop.old/upgrade-journal.txt``, the files it adds):
  ``app`` steps aside into ``.ytop.old/``, every file being replaced or
  dropped steps aside next and the folders that left empty go (before
  anything is placed, so a case-only rename never collides on FAT and a
  renamed folder takes the new spelling), the new files arrive, the root files are
  backed up and replaced (``boot.py`` first, so the rollback code of the
  new build is on the stick), and ``app`` arrives last: the commit point.
  Unchanged files are never written.  The rollback
  (:func:`ytop.stick.boot.rollback_upgrade`) lives in ``boot.py`` so the
  stick's next launch can undo an interrupted switch on any OS.

An upgrade never touches ``library.json``, ``data/``, ``config.toml``,
``cache/`` or ``YTOP Library/``.

Every make and upgrade (self mode too) leaves ``.metadata_never_index`` and
``.fseventsd/no_log`` at the drive root, so a Mac neither indexes the stick
nor logs its file events there (:func:`_write_mac_markers`).

**Self mode** (:func:`plan_self_upgrade`, used by :mod:`ytop.appupdate`):
the running stick upgrades itself from a downloaded update staged on it,
``<stick>/.ytop.making-src/``.  The staged image holds only the release
pieces that changed, plus the release's ``build.json`` and manifest; every
other file is referenced from the stick itself (``image_root``) and never
copied.  Staged files are moved (not copied) into ``.ytop.making/`` and
hashed there like any upgrade, then switched in under the same journal.  No
other source may be the stick itself: :func:`plan_make` still refuses it.
After a self-mode switch the files it replaced or removed (with the old
``build.json`` and manifest) are kept in ``.ytop/previous/`` for "Revert to
previous version" (:func:`read_previous`); a revert is a self-mode upgrade
too, so it keeps the files it reverted from.  Any other make or upgrade
removes ``previous/``: it no longer matches.

After a switch, ``.ytop.old/`` and the staging folders are deleted as far
as possible: on Windows the running app's ``python.exe``, ``.dll`` and
``.pyd`` files can be renamed aside but not deleted, so whatever is left
(never the journal) is deleted at the next start, by ``boot.py``.

``components/`` (the self-updated YouTube-facing parts, :mod:`ytop.components`)
is not image-owned either: it is in no manifest, never walked, set aside or
removed by a switch.  After the switch, new sticks and upgrades alike get
the newer version of each component among the source's store and the
target's (:func:`ytop.components.merge_newest`).  YTOP itself is in place by
then, so a failed merge is a warning in the report, never an undo.  A crash leaves ``.ytop.making/`` or
``.ytop.old/`` behind; the next :func:`inspect_target` (or
:func:`run_make`) reports and repairs it: an upgrade with a journal is
undone file by file, a piece swap (a new stick, or an older YTOP's upgrade) piece
by piece.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import stat
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Literal

from . import fsinfo, sticklock
from .components import merge_newest, merge_size
from .layout import LibraryInfo, _hide, read_library
from .libraries import PROBE_TIMEOUT_S, LibrarySet, _mount_candidates
from .manifest import (
    LEGAL_FILES,
    MANIFEST_HASH_KEY,
    MANIFEST_NAME,
    OWNED_DIRS,
    OWNED_ROOT_FILES,
    for_platforms,
    hash_tree,
    read_trusted_manifest,
    sha256_file,
    write_manifest,
)
from .parallel import COPY_WORKERS, run_all
from .product import NAME
from .runtime import PLATFORMS
from .stick.boot import (
    JOURNAL,
    LOCK,
    MAKING,
    MAKING_SRC,
    OLD,
    PART_SUFFIX,
    ROOT_FILES,
    make_parents,
    pid_alive,
    remove_tree_best_effort,
    rollback_upgrade,
    switch_interrupted,
)
from .timing import Phase, PhaseClock
from .web import fsops
from .web.fsops import canonical, copy_file, is_within
from .web.library import Library
from .yt.metadata import CorruptDocumentError, fsync_dir, human_bytes
from .yt.probe import JobCancelled

IMAGE_ENV = "YTOP_IMAGE_ROOT"
# LOCK (from boot) is held (O_EXCL, pid + host) by the process making a
# stick there; boot.py leaves a locked stick's leftovers alone.
TEMP_SUFFIX = ".ytop-tmp"
# Inside .ytop.making: a piece of an unfinished swap moved aside on rollback.
STALE_PREFIX = "stale-"
# Never fewer bytes spare than this, beyond the files and their clusters.
SPACE_MARGIN = 16 * 1024 * 1024
DEFAULT_CLUSTER = 32 * 1024
# The image pieces a make or upgrade owns.  ``app`` steps aside first and
# arrives last, so its absence marks an unfinished swap.
PIECES_OUT = (
    "app", "lib", "runtime", "boot.py", *LEGAL_FILES, "build.json", MANIFEST_NAME,
)
PIECES_IN = (
    "lib", "runtime", "boot.py", *LEGAL_FILES, "build.json", MANIFEST_NAME, "app",
)
README = "README.txt"
LAUNCHER_NAMES = (
    "Start YTOP (Windows).cmd",
    "Start YTOP (Mac).command",
    "start-ytop-linux.sh",
    README,
)
EXECUTABLE_LAUNCHERS = frozenset({"start-ytop-linux.sh", "Start YTOP (Mac).command"})
# Inside an image: where the launcher templates ship with the app.
LAUNCHER_TEMPLATES = Path("app/ytop/stick/launchers")
PORTABLE_FS = frozenset({"vfat", "exfat", "msdos", "fat", "fat32"})
FS_WARNING = "Phones and some computers may not read NTFS/APFS/ext4 drives"
SOURCE_WARNING = (
    "This is the stick YTOP is running from; choose another drive."
)
INTERNAL_WARNING = (
    "This folder is on this computer's own disk, not on a removable drive."
)
# At the drive root: macOS neither indexes the volume (Spotlight) nor logs
# its file events (fseventsd) there, so a Mac writes far less to the stick.
# Both are empty and inert on Windows and Linux.
NEVER_INDEX = ".metadata_never_index"
FSEVENTSD = ".fseventsd"
NO_LOG = "no_log"
SPOTLIGHT = ".Spotlight-V100"

Mode = Literal["new", "upgrade"]

# Targets a make is running into (in this process): never "repaired" under it.
_ACTIVE: set[str] = set()
_ACTIVE_LOCK = threading.Lock()


class MakeStickError(ValueError):
    """A stick cannot be made (or upgraded) as asked."""


def drive_id(target: Path) -> int | None:
    """The device ``target`` lives on, to tell later whether the drive was
    unplugged (or remounted) under a running make; None if it cannot be read."""
    try:
        return os.stat(target).st_dev
    except OSError:
        return None


_GONE_ERRNOS = frozenset({errno.ENODEV, errno.ENXIO, errno.ENOENT, errno.ESHUTDOWN})


def drive_problem(target: Path, exc: OSError, before: int | None = None) -> str:
    """A plain sentence for an OS error while making or upgrading a stick.

    ``before`` is :func:`drive_id` from when the work started.  A drive that
    vanished or came back as a different mount was disconnected; EIO means
    failing media or a damaged file system; a drive that turned read-only was
    usually switched by the computer after finding errors.  Anything else
    keeps the system's own message.
    """
    name = target.name or str(target)
    now = drive_id(target)
    if (before is not None and now != before) or (
        now is None and exc.errno in _GONE_ERRNOS
    ):
        return (
            f"The drive {name} was disconnected while YTOP was using it. "
            "Plug it back in (directly, not through a hub if you can) and run "
            "the same step again; a half-finished upgrade is undone first."
        )
    try:
        read_only = now is not None and not os.access(target, os.W_OK)
    except OSError:
        read_only = False
    check = (
        "Check it with a disk tool (fsck on Linux, chkdsk on Windows, "
        "First Aid in Disk Utility on a Mac) before trying again; if this "
        "keeps happening the drive may be failing."
    )
    if exc.errno == errno.EIO:
        made = " and the computer made it read-only to protect it" if read_only else ""
        return f"The drive {name} reported a read or write error{made}. {check}"
    if exc.errno == errno.EROFS or (
        read_only and exc.errno in (errno.EACCES, errno.EPERM)
    ):
        return (
            f"The drive {name} is read-only. A computer does this to a drive "
            f"it finds errors on, or the drive has a write-protect switch. {check}"
        )
    return str(exc)


# --- source ------------------------------------------------------------------


def source_image(image: Path | str | None = None) -> Path:
    """The image to copy: ``image`` (the CLI's ``--image``), else the stick
    YTOP runs from (``$YTOP_IMAGE_ROOT``)."""
    raw = image if image is not None else os.environ.get(IMAGE_ENV)
    if not raw:
        raise MakeStickError(
            "YTOP is not running from a stick; pass --image (for example "
            "dist/stick/.ytop)"
        )
    root = Path(raw).expanduser()
    if not (root / "build.json").is_file():
        raise MakeStickError(f"{root} is not a YTOP stick image (no build.json)")
    return root


def read_build(image: Path) -> dict:
    """The image's ``build.json``; its ``platforms`` are what it can give."""
    try:
        build = json.loads((image / "build.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MakeStickError(f"cannot read {image / 'build.json'}: {exc}") from exc
    platforms = build.get("platforms") if isinstance(build, dict) else None
    if not isinstance(platforms, list) or not all(
        isinstance(plat, str) for plat in platforms
    ):
        raise MakeStickError(f"{image / 'build.json'} lists no platforms")
    return build


def _read_build_or_none(image: Path) -> dict | None:
    try:
        return read_build(image)
    except MakeStickError:
        return None


# --- target ------------------------------------------------------------------


def _disk_usage(path: Path) -> tuple[int, int]:
    """``(free, total)`` bytes at ``path``; zeros when unknown."""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return 0, 0
    return int(usage.free), int(usage.total)


def _internal_disk(target: Path) -> bool:
    """``target`` is on the same filesystem as the home folder or ``/``."""
    try:
        dev = os.stat(target).st_dev
    except OSError:
        return False
    for other in (Path.home(), Path(os.path.abspath(os.sep))):
        try:
            if os.stat(other).st_dev == dev:
                return True
        except OSError:
            continue
    return False


def _windows_cluster_size(path: Path) -> int | None:
    """Sectors per cluster x bytes per sector of ``path``'s drive."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = getattr(ctypes, "windll").kernel32
    sectors, sector_bytes = wintypes.DWORD(), wintypes.DWORD()
    free, total = wintypes.DWORD(), wintypes.DWORD()
    root = os.path.splitdrive(os.path.abspath(path))[0] + "\\"
    ok = kernel32.GetDiskFreeSpaceW(
        ctypes.c_wchar_p(root), ctypes.byref(sectors), ctypes.byref(sector_bytes),
        ctypes.byref(free), ctypes.byref(total),
    )
    if not ok:
        return None
    return int(sectors.value) * int(sector_bytes.value) or None


def _cluster_size(path: Path) -> int:
    """The filesystem's allocation unit (each file wastes up to one)."""
    if sys.platform == "win32":
        try:
            return _windows_cluster_size(path) or DEFAULT_CLUSTER
        except (OSError, AttributeError, ValueError):
            return DEFAULT_CLUSTER
    statvfs = getattr(os, "statvfs", None)
    if statvfs is not None:
        try:
            info = statvfs(path)
            return int(info.f_frsize or info.f_bsize) or DEFAULT_CLUSTER
        except OSError:
            pass
    return DEFAULT_CLUSTER


def _check_folder(target: Path) -> None:
    """The preconditions ``LibrarySet.add_folder`` has, before anything is
    copied."""
    if not target.is_absolute():
        raise MakeStickError("the stick needs an absolute folder path")
    if fsinfo.refused_root(target):
        raise MakeStickError(
            "choose a USB drive or a folder, not the system's root"
        )
    try:
        if target.resolve() == Path.home().resolve():
            raise MakeStickError(
                "choose a USB stick or a folder of its own, not your home folder"
            )
    except OSError:
        pass
    if not target.is_dir():
        raise MakeStickError(f"{target} is not a folder")
    if not os.access(target, os.W_OK):
        raise MakeStickError(f"{target} cannot be written to")


def _has_files(folder: Path) -> bool:
    try:
        for _current, _dirs, names in os.walk(folder):
            if names:
                return True
    except OSError:
        return False
    return False


def _damaged_library(target: Path) -> str | None:
    """Why ``target`` looks like a library whose marker is unreadable (a new
    identity must never be minted over it), or ``None``."""
    ytop = target / ".ytop"
    if (ytop / "library.json").exists():
        return "its .ytop/library.json is unreadable"
    if _has_files(ytop / "data"):
        return "it has a .ytop/data folder but no library.json"
    if _has_files(target / "YTOP Library"):
        return "it has a “YTOP Library” folder but no .ytop/library.json"
    return None


def _foreign_launchers(target: Path, templates: Path) -> list[str]:
    """Start-file names at the root that hold someone else's files."""
    if (target / ".ytop" / "build.json").is_file():
        return []  # YTOP's own, from an earlier (interrupted) make
    foreign = []
    for name in LAUNCHER_NAMES:
        path = target / name
        if not os.path.lexists(path):
            continue
        try:
            data = path.read_bytes()
            template = (templates / name).read_bytes()
        except OSError:
            foreign.append(name)
            continue
        ours = data == template or (
            name == README and data.startswith(b"YTOP stick:")
        )
        if not ours:
            foreign.append(name)
    return foreign


def _is_source(target: Path, source: Path | None) -> bool:
    if source is None:
        return False
    try:
        return canonical(target) == canonical(source).parent or is_within(
            target, source
        )
    except OSError:
        return False


@dataclass
class TargetInfo:
    path: Path
    fstype: str
    free_bytes: int
    total_bytes: int
    has_ytop: bool
    library: LibraryInfo | None
    image_build: dict | None
    warnings: list[str] = field(default_factory=list)
    is_source: bool = False

    @property
    def mode(self) -> Mode:
        """Upgrade a drive that already holds a library, else make one."""
        return "upgrade" if self.library is not None else "new"

    @property
    def free_label(self) -> str:
        return human_bytes(self.free_bytes) if self.free_bytes else ""

    @property
    def total_label(self) -> str:
        return human_bytes(self.total_bytes) if self.total_bytes else ""


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif os.path.lexists(path):
        path.unlink()


def _leftover_notes(target: Path) -> list[str]:
    notes = []
    if (target / OLD).is_dir():
        notes.append(
            "An earlier upgrade of this drive did not finish; it is repaired "
            "before anything is copied."
        )
    if (target / MAKING).is_dir():
        notes.append(
            "An earlier stick-making on this drive was interrupted; its "
            "partial copy is removed before anything is copied."
        )
    return notes


def recover_target(target: Path) -> list[str]:
    """Repair what an interrupted make or upgrade left; returns what was done.

    ``.ytop.old/`` with ``.ytop/app`` missing means the switch stopped half
    way.  With the journal it was an incremental upgrade, undone file by
    file by :func:`ytop.stick.boot.rollback_upgrade`.  Without it, it was a
    piece swap: every old piece goes back, ``app`` last, so a second
    interruption is repaired the same way.  A new piece in the way is
    renamed aside into ``.ytop.making/`` (never deleted before the old one
    is back), and ``.ytop.old/`` is only removed once it is empty of pieces
    to restore.  Otherwise the leftovers are deleted."""
    notes: list[str] = []
    ytop, making, old = target / ".ytop", target / MAKING, target / OLD
    if old.is_dir():
        if (old / JOURNAL).is_file():
            try:
                undone = rollback_upgrade(target)
            except ValueError as exc:  # a journal it cannot use
                raise MakeStickError(
                    f"an interrupted upgrade on {target} could not be undone "
                    f"({exc}); nothing more was changed"
                ) from exc
            if undone:
                notes.append(
                    "An interrupted upgrade was undone: the previous YTOP on "
                    "this drive was restored."
                )
        elif not os.path.lexists(ytop / "app"):
            restored = False
            for piece in PIECES_IN:  # app last
                if not os.path.lexists(old / piece):
                    continue
                if os.path.lexists(ytop / piece):
                    making.mkdir(exist_ok=True)
                    stale = making / f"{STALE_PREFIX}{piece}"
                    _remove(stale)
                    _place(ytop / piece, stale)
                _place(old / piece, ytop / piece)
                fsync_dir(ytop)
                restored = True
            if restored:
                notes.append(
                    "An interrupted upgrade was undone: the previous YTOP on "
                    "this drive was restored."
                )
        if old.is_dir():
            if switch_interrupted(target):
                shutil.rmtree(old)
            else:
                clear_old(target)
    if making.is_dir():
        shutil.rmtree(making)
        notes.append(
            "An interrupted stick-making left a partial copy here; it was "
            "removed."
        )
    try:
        temps = [p for p in target.iterdir() if p.name.endswith(TEMP_SUFFIX)]
    except OSError:
        temps = []
    for temp in temps:
        if temp.name.startswith(".") and temp.is_file():
            temp.unlink(missing_ok=True)
    return notes


def clear_old(target: Path) -> bool:
    """Delete a finished switch's ``.ytop.old`` as far as possible (its
    journal first); True if it is gone.  Files still in use (Windows) stay
    for ``boot.py`` to delete at the next start."""
    old = target / OLD
    if not old.is_dir():
        return True
    if switch_interrupted(target):
        raise MakeStickError("an interrupted switch must be repaired, not deleted")
    try:
        (old / JOURNAL).unlink(missing_ok=True)
    except OSError:
        return False  # left whole; the next switch refuses to start over it
    return remove_tree_best_effort(old)


# --- the previous version (self mode) -----------------------------------------
#
# After an app update (or a revert, which is one too) the files the switch
# set aside are kept in ``.ytop/previous/`` instead of being deleted: every
# file of the old manifest that the new one changed or dropped, plus the old
# ``manifest.sha256`` and ``build.json``.  The rest of the old version is
# the stick's current files, so ``previous/`` overlaid on ``.ytop`` is the
# old version exactly ("Revert to previous version", :mod:`ytop.appupdate`).
# ``PREVIOUS_MARK`` is written last and names the sha256 of the
# ``build.json`` it was kept beside: a copy that is half-made, or that
# belongs to another build (a make-stick upgrade, a crash), is never valid.

PREVIOUS = "previous"
# An older rollback copy, renamed here before it is deleted.
PREVIOUS_DROP = "previous.del"
PREVIOUS_MARK = "replaced-by.json"


def drop_previous(ytop: Path) -> None:
    """Remove the rollback copy (it no longer matches this stick); best
    effort, never fails."""
    for name in (PREVIOUS, PREVIOUS_DROP):
        remove_tree_best_effort(ytop / name)


def read_previous(ytop: Path) -> dict | None:
    """The previous version's ``build.json`` when ``ytop/previous`` is a
    complete rollback copy for the build on the stick now, else None."""
    previous = ytop / PREVIOUS
    try:
        mark = json.loads((previous / PREVIOUS_MARK).read_text(encoding="utf-8"))
        if mark.get("build_sha256") != sha256_file(ytop / "build.json"):
            return None
        build = json.loads((previous / "build.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(build, dict) or read_trusted_manifest(previous) is None:
        return None
    have = _read_build_or_none(ytop)
    platforms = build.get("platforms")
    if have is None or not isinstance(platforms, list):
        return None
    if not set(map(str, have.get("platforms") or [])) <= set(map(str, platforms)):
        return None
    return build


def _keep_previous(target: Path) -> None:
    """After a self-mode switch's commit point: move what it set aside into
    ``.ytop/previous/`` (replacing an older copy) and delete the rest of
    ``.ytop.old``.  Best effort; any order a crash leaves is safe: the
    journal goes first (nothing is ever "rolled back" after the commit),
    and the copy is valid only once its mark is written."""
    ytop, old = target / ".ytop", target / OLD
    previous = ytop / PREVIOUS
    old_manifest = read_trusted_manifest(old)
    new_manifest = read_trusted_manifest(ytop)
    keep: list[str] = []
    if old_manifest is not None and new_manifest is not None:
        keep = sorted(
            rel for rel, digest in old_manifest.items()
            if new_manifest.get(rel) != digest
        )
    usable = (
        old_manifest is not None and new_manifest is not None
        and all((old / rel).is_file() for rel in [*keep, *_PREVIOUS_ROOTS])
    )
    try:
        if not usable or not clear_journal(target):
            drop_previous(ytop)
            clear_old(target)
            return
        drop = ytop / PREVIOUS_DROP
        if os.path.lexists(previous):
            if not remove_tree_best_effort(drop):
                drop_previous(ytop)  # no room to set it aside: just delete
            else:
                _place(previous, drop)
                fsync_dir(ytop)
        remove_tree_best_effort(drop)
        if os.path.lexists(previous):
            remove_tree_best_effort(old)
            return
        previous.mkdir()
        touched: set[Path] = {ytop}
        for rel in [*keep, *_PREVIOUS_ROOTS]:  # build.json last
            make_parents((previous / rel).parent, touched)
            _place(old / rel, previous / rel)
            touched.add((previous / rel).parent)
        _fsync_dirs(touched)
        temp = previous / f".{PREVIOUS_MARK}{TEMP_SUFFIX}"
        with open(temp, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(
                {"build_sha256": sha256_file(ytop / "build.json")}
            ) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        _place(temp, previous / PREVIOUS_MARK)
        fsync_dir(previous)
    except OSError:
        pass  # a half-made copy has no mark: never offered
    remove_tree_best_effort(old)


_PREVIOUS_ROOTS = (MANIFEST_NAME, "build.json")


def clear_journal(target: Path) -> bool:
    """Delete a finished switch's journal (first, before anything else of
    ``.ytop.old``); True if it is gone."""
    if switch_interrupted(target):
        raise MakeStickError("an interrupted switch must be repaired, not deleted")
    try:
        (target / OLD / JOURNAL).unlink(missing_ok=True)
    except OSError:
        return False
    return True


# --- cross-process lock --------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    return pid_alive(pid)


def _lock_live(target: Path) -> bool:
    """A make by a live process holds ``target``: its pid is alive and, where
    the OS tells, started when the lock says (Ruling R-B8; the same rule as
    boot.lock_holder, which ignores an old lock's ``host``).  A lock naming
    this process is live only if this process took it (sticklock)."""
    return sticklock.live(target / LOCK, alive=lambda pid: _pid_alive(pid))


def _lock_busy(target: Path) -> MakeStickError:
    return MakeStickError(
        "another YTOP is making or updating a stick there; wait for it to "
        "finish. If no other YTOP is running, delete the file "
        f"{LOCK} on {target} and try again"
    )


def _acquire_lock(target: Path, before_break: Callable[[], None] | None = None) -> None:
    sticklock.acquire(
        target / LOCK, lambda: _lock_busy(target), alive=lambda pid: _pid_alive(pid),
        before_break=before_break,
    )


def _touch_lock(target: Path) -> None:
    """The switch starts now (boot.py honours a lock this fresh)."""
    try:
        os.utime(target / LOCK)
    except OSError:
        pass


def _release_lock(target: Path) -> None:
    sticklock.release(target / LOCK)


def _busy(target: Path) -> bool:
    with _ACTIVE_LOCK:
        return str(canonical(target)) in _ACTIVE


def inspect_target(
    path: Path,
    *,
    source: Path | None = None,
    bytes_needed: int | None = None,
    cleanup: bool = True,
) -> TargetInfo:
    """What is on ``path`` and what to warn about before making a stick there.

    With ``cleanup`` (the default) leftovers of an interrupted make or
    upgrade are repaired and reported; without it they are only reported."""
    target = Path(path).expanduser()
    if not target.is_dir():
        raise MakeStickError(f"{target} is not a folder")
    if cleanup and not _busy(target) and not _lock_live(target):
        warnings = recover_target(target)
    else:
        warnings = _leftover_notes(target)
    try:
        fstype = fsinfo.fs_info(target).fstype
    except OSError:
        fstype = "unknown"
    free, total = _disk_usage(target)
    library = read_library(target)
    image_build = _read_build_or_none(target / ".ytop")
    if fstype.lower() not in PORTABLE_FS and fstype != "unknown":
        warnings.append(
            f"This drive is formatted {fstype}. {FS_WARNING}; FAT32 or exFAT "
            "works everywhere."
        )
    if bytes_needed and free and bytes_needed > free:
        warnings.append(
            f"Not enough space: this needs {human_bytes(bytes_needed)} but the "
            f"drive has {human_bytes(free)} free."
        )
    is_source = _is_source(target, source)
    if is_source:
        warnings.append(SOURCE_WARNING)
    elif _internal_disk(target):
        warnings.append(INTERNAL_WARNING)
    return TargetInfo(
        path=target,
        fstype=fstype,
        free_bytes=free,
        total_bytes=total,
        has_ytop=library is not None or image_build is not None,
        library=library,
        image_build=image_build,
        warnings=warnings,
        is_source=is_source,
    )


def candidate_targets(
    libraries: LibrarySet, source: Path | None, timeout: float = PROBE_TIMEOUT_S
) -> list[TargetInfo]:
    """Where a stick could go: the online libraries, then mounted drives
    without one.  Each is inspected (report only) in its own thread, so a
    dead drive cannot stall the page."""
    # This computer's own libraries are upgraded only when typed in.
    paths = [
        ref.root for ref in libraries.libraries()
        if not ref.read_only and not _internal_disk(ref.root)
    ]
    seen = {str(path) for path in paths}
    for mount in _mount_candidates():
        if not mount.name.startswith(".") and str(mount) not in seen:
            seen.add(str(mount))
            paths.append(mount)
    results: list[TargetInfo | None] = [None] * len(paths)

    def probe(index: int, path: Path) -> None:
        try:
            results[index] = inspect_target(path, source=source, cleanup=False)
        except (MakeStickError, OSError):
            results[index] = None

    threads = [
        threading.Thread(
            target=probe, args=(index, path), daemon=True, name="ytop-stick-probe"
        )
        for index, path in enumerate(paths)
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    found = []
    for index, thread in enumerate(threads):
        thread.join(max(0.0, deadline - time.monotonic()))
        info = None if thread.is_alive() else results[index]
        if info is not None:
            found.append(info)
    return found


# --- planning ----------------------------------------------------------------


def _image_tree(root: Path, base: Path) -> list[tuple[str, int]]:
    rows = []
    for current, dirs, names in os.walk(root, followlinks=False):
        # Bytecode is part of the image (Ruling R12): copied like the rest.
        dirs[:] = sorted(dirs)
        for name in sorted(names):
            path = Path(current) / name
            info = os.stat(path, follow_symlinks=False)
            if stat.S_ISREG(info.st_mode):  # images hold no symlinks
                rows.append((path.relative_to(base).as_posix(), info.st_size))
    return rows


def image_files(source: Path, platforms: list[str]) -> list[tuple[str, int]]:
    """``(relative path, size)`` of every image file a stick with
    ``platforms`` gets (``build.json`` is written fresh; caches are never
    copied)."""
    boot = source / "boot.py"
    if not boot.is_file() or not (source / "app").is_dir():
        raise MakeStickError(f"{source} is not a complete YTOP image")
    rows = [("boot.py", boot.stat().st_size)]
    for name in LEGAL_FILES:  # images built before the licence files lack them
        if (source / name).is_file():
            rows.append((name, (source / name).stat().st_size))
    rows += _image_tree(source / "app", source)
    if (source / "lib" / "common").is_dir():
        rows += _image_tree(source / "lib" / "common", source)
    for plat in platforms:
        for part in ("lib", "runtime"):
            folder = source / part / plat
            if not folder.is_dir():
                raise MakeStickError(f"the image has no {part}/{plat}")
            rows += _image_tree(folder, source)
    return rows


def image_manifest(
    source: Path,
    platforms: list[str],
    on_file: Callable[[str], None] | None = None,
) -> dict[str, str]:
    """rel -> sha256 of the files a stick with ``platforms`` gets.  Read from
    the image's manifest; an image without a trustworthy one (an older
    stick, or one an older YTOP upgraded) is hashed instead, which is
    slower."""
    entries = read_trusted_manifest(source)
    if entries is not None:
        return for_platforms(entries, platforms)
    rels = [rel for rel, _size in image_files(source, platforms)]
    return hash_tree(source, rels, on_file)


@dataclass
class UpgradeDiff:
    """What an upgrade must do to the target's image-owned files."""

    copy: list[str] = field(default_factory=list)  # new or changed
    remove: list[str] = field(default_factory=list)  # on the target, not wanted
    unchanged: int = 0
    copy_bytes: int = 0
    # The target had no trustworthy manifest, so its files are hashed.
    checked_by_hash: bool = False
    # Every image-owned file the target had (as named on the target).
    present: set[str] = field(default_factory=set, repr=False)


def _target_files(image: Path) -> set[str]:
    """Every regular file under the target's boot.py, app/, lib/ and runtime/."""
    found: set[str] = set()
    for name in OWNED_ROOT_FILES:
        try:
            if stat.S_ISREG(os.stat(image / name, follow_symlinks=False).st_mode):
                found.add(name)
        except OSError:
            pass
    for part in OWNED_DIRS:
        base = image / part
        for current, _dirs, names in os.walk(base, followlinks=False):
            folder = Path(current).relative_to(image).as_posix()
            for name in names:
                found.add(f"{folder}/{name}")
    return found


def _source_file(source: Path, image_root: Path | None, rel: str) -> Path:
    """Where the image's ``rel`` is: in ``source``, else (an overlay, see
    self mode) the same file in ``image_root``."""
    path = source / rel
    if image_root is not None and not os.path.lexists(path):
        return image_root / rel
    return path


def diff_upgrade(
    source: Path | None,
    target_ytop: Path,
    wanted: dict[str, str],
    *,
    verify: bool,
    on_file: Callable[[str], None] | None = None,
    image_root: Path | None = None,
) -> UpgradeDiff:
    """Compare ``wanted`` (rel -> hash) with the target's ``.ytop``.

    Without ``verify`` this only reads the target's manifest and walks its
    tree.  With it, sizes are checked against the source, and a target with
    no trustworthy manifest (none, or not the one its ``build.json`` names)
    is hashed file by file.  Without a ``source`` (an update not downloaded
    yet) sizes are neither checked nor counted.  ``image_root`` completes an
    overlay source (self mode): a file the source lacks is the one there."""
    present = _target_files(target_ytop)
    remove = sorted(present - wanted.keys())
    manifest = read_trusted_manifest(target_ytop)
    diff = UpgradeDiff(
        remove=remove, checked_by_hash=manifest is None, present=present,
    )
    for rel in sorted(wanted):
        same = False
        if rel in present:
            if manifest is not None:
                same = manifest.get(rel) == wanted[rel]
                if same and verify and source is not None:
                    same = (
                        os.stat(target_ytop / rel).st_size
                        == os.stat(_source_file(source, image_root, rel)).st_size
                    )
            elif verify:
                if on_file is not None:
                    on_file(rel)
                same = sha256_file(target_ytop / rel) == wanted[rel]
        if same:
            diff.unchanged += 1
        else:
            diff.copy.append(rel)
            if source is not None:
                diff.copy_bytes += os.stat(_source_file(source, image_root, rel)).st_size
    return diff


@dataclass
class MakePlan:
    target: Path
    platforms: list[str]
    playlists: list[str]  # playlist keys
    mode: Mode
    bytes_needed: int
    label: str = ""
    files: list[tuple[str, int]] = field(default_factory=list, repr=False)
    playlist_files: int = 0
    diff: UpgradeDiff | None = None  # set for upgrades
    # Self mode: the source is an overlay staged on the stick, completed by
    # the stick's own image (which is never copied).
    image_root: Path | None = None

    @property
    def total_files(self) -> int:
        return len(self.files) + self.playlist_files


def upgrade_summary(plan: MakePlan) -> str:
    """One line saying what an upgrade will do (empty for a new stick)."""
    diff = plan.diff
    if diff is None:
        return ""
    if diff.checked_by_hash:
        return (
            "Upgrade: this stick has no file list yet, so every file is "
            "checked first (slower, once)."
        )
    count = len(plan.files)
    written = sum(size for _rel, size in plan.files)
    return (
        f"Upgrade: {count:,} file{'' if count == 1 else 's'} to write "
        f"({human_bytes(written)}), {len(diff.remove):,} to remove, "
        f"{diff.unchanged:,} unchanged."
    )


def choose_platforms(build: dict, wanted: list[str] | str | None) -> list[str]:
    """``wanted`` (``None``/``"all"`` for every one) checked against what the
    image can give, in the image's order."""
    available = [str(plat) for plat in build.get("platforms") or []]
    if wanted is None or wanted == "all" or wanted == ["all"]:
        return available
    keys = [wanted] if isinstance(wanted, str) else list(wanted)
    unknown = [key for key in keys if key not in available]
    if unknown:
        raise MakeStickError(
            f"this stick cannot give {', '.join(unknown)}; it has "
            f"{', '.join(available) or 'no platforms'}"
        )
    chosen = [plat for plat in available if plat in set(keys)]
    if not chosen:
        raise MakeStickError("choose at least one platform")
    return chosen


def plan_make(
    target: Path,
    source: Path,
    library: Library,
    *,
    platforms: list[str] | str | None = None,
    playlists: list[str] | None = None,
    mode: Mode | None = None,
    label: str = "",
) -> MakePlan:
    """Check a make (or upgrade) of ``target`` from image ``source``.

    ``mode`` defaults to upgrade when ``target`` already holds a library.
    Raises :class:`MakeStickError` for anything that would not work."""
    target = Path(target).expanduser().absolute()
    _check_folder(target)
    build = read_build(source)
    chosen = choose_platforms(build, platforms)
    info = inspect_target(target, source=source, cleanup=False)
    if info.is_source:
        raise MakeStickError(SOURCE_WARNING)
    if mode is None:
        mode = info.mode
    if mode == "new" and info.library is not None:
        raise MakeStickError(
            f"{target} already holds the YTOP library "
            f"“{info.library.label}”; upgrade it instead (--upgrade)"
        )
    if mode == "upgrade" and info.library is None:
        raise MakeStickError(
            f"{target} holds no YTOP library to upgrade; make a new stick there"
        )
    if mode == "upgrade" and info.library is not None and info.image_build is None:
        # A library without YTOP itself (an interrupted upgrade still has
        # its build.json): the switch has no app to set aside, so it would
        # copy the whole image and then fail.
        raise MakeStickError(
            f"{target} holds the YTOP library “{info.library.label}” "
            "but no YTOP to upgrade"
        )
    keys = list(dict.fromkeys(playlists or []))
    if mode == "upgrade" and keys:
        raise MakeStickError(
            "an upgrade never changes the library, so it copies no playlists"
        )
    templates = source / LAUNCHER_TEMPLATES
    if mode == "new":
        damaged = _damaged_library(target)
        if damaged:
            raise MakeStickError(
                f"{target} looks like a damaged YTOP library ({damaged}); "
                "repair or move it first — a new library is never made over it"
            )
        foreign = _foreign_launchers(target, templates)
        if foreign:
            raise MakeStickError(
                f"{target} already has {', '.join(foreign)}, not made by YTOP; "
                "move or rename it first (nothing on the drive is overwritten)"
            )
        for ref in library.libraries.libraries(refresh=True):
            if is_within(target, ref.root) or is_within(ref.root, target):
                raise MakeStickError(
                    f"{target} overlaps the library {ref.label}; choose a "
                    "separate drive or folder"
                )
    diff: UpgradeDiff | None = None
    if mode == "upgrade":
        wanted = image_manifest(source, chosen)
        diff = diff_upgrade(source, target / ".ytop", wanted, verify=False)
        copy = diff.copy
        if diff.checked_by_hash:
            # No file list to trust: the run hashes every file.  Until then
            # a file of the same size counts as unchanged, or a nearly full
            # older stick would be refused for the whole image; the run
            # checks the space again once it knows (:func:`_check_space`).
            copy = [
                rel for rel in copy
                if not _same_size(target / ".ytop" / rel, source / rel)
            ]
        files = [(rel, os.stat(source / rel).st_size) for rel in copy]
        if any(rel.startswith("app/") for rel in copy):
            # The run copies the whole app/ when any of it changed.
            have = {rel for rel, _size in files}
            files += [
                (rel, os.stat(source / rel).st_size)
                for rel in sorted(wanted)
                if rel.startswith("app/") and rel not in have
            ]
        manifest_bytes = sum(len(rel) + 67 for rel in wanted)
    else:
        files = image_files(source, chosen)
        manifest_bytes = 0
    playlist_bytes = 0
    playlist_files = 0
    try:
        limit = fsinfo.fs_info(target).max_file_bytes
    except OSError:
        limit = None
    for key in keys:
        if library.resolve_playlist(key) is None:
            raise MakeStickError(f"no playlist {key!r} is available")
        size, count, biggest = fsops.copy_size(library, key)
        if limit is not None and biggest > limit:
            view = library.playlist_view(key)
            raise MakeStickError(
                f"“{view.title if view else key}” has a file of "
                f"{human_bytes(biggest)}, larger than the FAT32 limit of "
                f"{human_bytes(limit)} per file on this drive; leave it out"
            )
        playlist_bytes += size
        playlist_files += count
    launcher_bytes = sum(
        (templates / name).stat().st_size
        for name in LAUNCHER_NAMES if (templates / name).is_file()
    )
    try:
        components_bytes = merge_size(source, target / ".ytop", chosen)
    except OSError:
        components_bytes = 0
    need = (
        sum(size for _rel, size in files) + playlist_bytes + launcher_bytes
        + len(json.dumps(build, indent=2)) + 1 + manifest_bytes
        + components_bytes
    )
    # Every file can waste up to one cluster; keep a margin besides.
    total_files = len(files) + playlist_files + len(LAUNCHER_NAMES) + 1
    space = need + total_files * _cluster_size(target) + SPACE_MARGIN
    free, _total = _disk_usage(target)
    if free and space > free:
        raise MakeStickError(
            f"not enough space: this needs about {human_bytes(space)} but "
            f"{target} has {human_bytes(free)} free"
        )
    if mode == "upgrade":
        assert info.library is not None
        name = info.library.label
    else:
        name = " ".join((label or "").split())[:80] or target.name or "YTOP"
    return MakePlan(
        target=target, platforms=chosen, playlists=keys, mode=mode,
        bytes_needed=need, label=name, files=files,
        playlist_files=playlist_files, diff=diff,
    )


def _same_size(a: Path, b: Path) -> bool:
    try:
        return os.stat(a).st_size == os.stat(b).st_size
    except OSError:
        return False


def _check_space(target: Path, sizes: list[int]) -> None:
    """Refuse (before anything is written) to stage files of ``sizes`` that
    the drive has no room for: an upgrade's verify can find more to copy
    than its plan counted."""
    space = sum(sizes) + (len(sizes) + 1) * _cluster_size(target) + SPACE_MARGIN
    free, _total = _disk_usage(target)
    if free and space > free:
        raise MakeStickError(
            f"not enough space: this needs about {human_bytes(space)} but "
            f"{target} has {human_bytes(free)} free; nothing was changed"
        )


def _app_rels(diff: UpgradeDiff, wanted: dict[str, str]) -> set[str]:
    """What an upgrade stages: the changed files, and all of ``app/`` when
    any of it changed (it is switched whole)."""
    rels = set(diff.copy)
    if any(rel.startswith("app/") for rel in diff.copy + diff.remove):
        rels.update(rel for rel in wanted if rel.startswith("app/"))
    return rels


def plan_self_upgrade(target: Path, source: Path) -> MakePlan:
    """Check an upgrade of the stick at ``target`` from an update staged on
    it (self mode, module docstring).

    The source must be exactly ``<target>/.ytop.making-src`` with a
    manifest its ``build.json`` vouches for, and must hold every file the
    upgrade writes; anything else is refused."""
    target = Path(target).expanduser().absolute()
    source = Path(source).expanduser().absolute()
    try:
        staged = canonical(source) == canonical(target / MAKING_SRC)
    except OSError:
        staged = False
    if not staged or not source.is_dir():
        raise MakeStickError(
            "a stick upgrades itself only from an update staged on it "
            f"({MAKING_SRC}); make-stick never copies a stick onto itself"
        )
    if read_trusted_manifest(source) is None:
        raise MakeStickError("the staged update has no verified file list")
    info = read_library(target)
    if info is None:
        raise MakeStickError(f"{target} holds no YTOP library to upgrade")
    ytop = target / ".ytop"
    build = read_build(source)
    have = read_build(ytop)
    chosen = choose_platforms(build, [str(plat) for plat in have["platforms"]])
    wanted = image_manifest(source, chosen)
    # A stick without a file list it can trust is hashed (as the switch
    # does), or every file it has would look changed.
    diff = diff_upgrade(
        None, ytop, wanted, verify=read_trusted_manifest(ytop) is None,
    )
    rels = sorted(_app_rels(diff, wanted))
    missing = [rel for rel in rels if not (source / rel).is_file()]
    if missing:
        raise MakeStickError(
            f"the staged update lacks {missing[0]}"
            + (f" and {len(missing) - 1} more" if len(missing) > 1 else "")
        )
    files = [(rel, os.stat(source / rel).st_size) for rel in rels]
    free, _total = _disk_usage(target)
    if free and free < SPACE_MARGIN:
        raise MakeStickError(
            f"not enough space: {target} has {human_bytes(free)} free"
        )
    return MakePlan(
        target=target, platforms=chosen, playlists=[], mode="upgrade",
        bytes_needed=sum(size for _rel, size in files), label=info.label,
        files=files, diff=diff, image_root=ytop,
    )


# --- making ------------------------------------------------------------------


@dataclass(frozen=True)
class MakeProgress:
    # checking | copying | verifying | switching | playlists | done
    phase: str
    bytes: int
    files: int
    total_bytes: int
    total_files: int
    label: str


ProgressCallback = Callable[[MakeProgress], None]


class _Progress:
    """Throttled progress; each phase may count its own files and bytes."""

    def __init__(self, callback: ProgressCallback, total_bytes: int, total_files: int):
        self.callback = callback
        self.total_bytes = total_bytes
        self.total_files = total_files
        self.bytes = 0
        self.files = 0
        self.phase_name = "copying"
        self.label = ""
        self._last = 0.0
        # Wall-clock per phase (the report's timings); ``mark`` adds steps
        # the progress display does not show on their own.
        self.clock = PhaseClock(lambda: (self.files, self.bytes), unit="files")
        self.clock.start("preparing")

    def mark(self, name: str) -> None:
        """Start timing step ``name`` without changing what is displayed."""
        self.clock.start(name)

    def phase(
        self,
        phase: str,
        label: str,
        *,
        total_files: int | None = None,
        total_bytes: int | None = None,
    ) -> None:
        """Start ``phase``; given totals restart the counts from zero."""
        same = self.clock.current == phase
        if not same:
            self.clock.stop()  # counted before any reset below
        self.phase_name = phase
        self.label = label
        if total_files is not None:
            self.total_files, self.files = total_files, 0
        if total_bytes is not None:
            self.total_bytes, self.bytes = total_bytes, 0
        if same:
            self.clock.rebase()
        elif phase != "done":
            self.clock.start(phase)
        self._emit()

    def resume(self, done_bytes: int, done_files: int, total_bytes: int,
               total_files: int) -> None:
        """Go back to counting against earlier totals (the playlists after
        the image copy)."""
        self.bytes, self.files = done_bytes, done_files
        self.total_bytes, self.total_files = total_bytes, total_files
        self.clock.rebase()

    def chunk(self, size: int) -> None:
        self.bytes += size
        if time.monotonic() - self._last >= fsops.PROGRESS_INTERVAL_S:
            self._emit()

    def file(self) -> None:
        self.files += 1
        if time.monotonic() - self._last >= fsops.PROGRESS_INTERVAL_S:
            self._emit()

    def complete(self) -> None:
        """The phase's work is all counted: say so."""
        self.files = self.total_files
        self.bytes = self.total_bytes
        self._emit()

    def at(self, done_bytes: int, done_files: int, label: str) -> None:
        self.bytes = min(done_bytes, self.total_bytes)
        self.files = min(done_files, self.total_files)
        self.label = label or self.label
        if time.monotonic() - self._last >= fsops.PROGRESS_INTERVAL_S:
            self._emit()

    def finish(self, total_bytes: int, total_files: int) -> None:
        self.clock.stop()  # the jump to the totals below is no step's work
        self.total_bytes, self.total_files = total_bytes, total_files
        self.bytes, self.files = total_bytes, total_files
        self.phase("done", "")

    def _emit(self) -> None:
        self._last = time.monotonic()
        self.callback(MakeProgress(
            self.phase_name, min(self.bytes, self.total_bytes),
            min(self.files, self.total_files), self.total_bytes,
            self.total_files, self.label,
        ))


@dataclass
class MakeReport:
    target: Path
    mode: Mode
    platforms: list[str]
    build_id: str
    label: str
    library_id: str
    bytes_copied: int
    files_copied: int
    playlists: list[str] = field(default_factory=list)
    repaired: list[str] = field(default_factory=list)
    # Things that went wrong after YTOP itself was in place.
    warnings: list[str] = field(default_factory=list)
    # Wall-clock per step, in order.
    timings: list[Phase] = field(default_factory=list)


def _check_cancel(cancelled: Callable[[], bool]) -> None:
    if cancelled():
        raise JobCancelled("cancelled by the user")


def _copy_files(
    source: Path,
    making: Path,
    rels: list[str],
    progress: _Progress,
    cancelled: Callable[[], bool],
    *,
    move: bool = False,
) -> None:
    """Copy ``rels`` from ``source`` into ``making`` (``move``: rename them,
    for a source staged on the same stick).  Copies run a few at a time
    (:mod:`ytop.parallel`); when this returns or raises, none is still
    writing."""
    lock = threading.Lock()

    def chunk(size: int) -> None:
        with lock:
            progress.chunk(size)

    def one(rel: str, stopping: Callable[[], bool]) -> None:
        _check_cancel(cancelled)
        destination = making / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        if move:
            size = os.stat(source / rel).st_size
            os.replace(source / rel, destination)
            chunk(size)
        else:
            copy_file(source / rel, destination, on_chunk=chunk,
                      cancel=lambda: stopping() or cancelled())
        with lock:
            progress.file()

    # Renames on the same stick are instant: no point overlapping them.
    run_all(rels, one, workers=1 if move else COPY_WORKERS)


def _verify(
    wanted: dict[str, str],
    making: Path,
    rels: list[str],
    progress: _Progress,
    cancelled: Callable[[], bool],
) -> None:
    """Hash every copied file against the image's manifest."""
    total = 0
    for rel in rels:
        try:
            total += os.stat(making / rel, follow_symlinks=False).st_size
        except OSError:
            pass
    progress.phase(
        "verifying", "Checking the copy…", total_files=len(rels), total_bytes=total,
    )
    for rel in rels:
        _check_cancel(cancelled)
        try:
            got: str | None = sha256_file(making / rel, on_chunk=progress.chunk)
        except OSError:
            got = None
        if got is None or got != wanted.get(rel):
            raise MakeStickError(
                f"copy verification failed: {rel} differs from the original; "
                "nothing was changed"
            )
        progress.file()
    progress.complete()


def _write_build(making: Path, build: dict, platforms: list[str]) -> None:
    """The stick's ``build.json``, written after its manifest: it records
    the sha256 of ``making``'s manifest (none there, no field)."""
    doc = dict(build)
    doc["platforms"] = list(platforms)
    doc.pop(MANIFEST_HASH_KEY, None)  # the image's, not this stick's
    if (making / MANIFEST_NAME).is_file():
        doc[MANIFEST_HASH_KEY] = sha256_file(making / MANIFEST_NAME)
    sizes = build.get("sizes")
    if isinstance(sizes, dict):
        doc["sizes"] = {
            key: value for key, value in sizes.items()
            if key not in PLATFORMS or key in platforms
        }
    path = making / "build.json"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(doc, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _place(src: Path, dst: Path) -> None:
    """One rename of a switch (the single seam for crash tests).  Replaces
    ``dst`` only for a root file; every other move has a free destination."""
    os.replace(src, dst)


def _swap_in(target: Path, making: Path) -> None:
    """The whole-piece swap of a new stick."""
    ytop, old = target / ".ytop", target / OLD
    ytop.mkdir(exist_ok=True)
    _touch_lock(target)
    old.mkdir()
    for piece in PIECES_OUT:
        if os.path.lexists(ytop / piece):
            _place(ytop / piece, old / piece)
    fsync_dir(ytop)
    fsync_dir(old)
    for piece in PIECES_IN:
        _place(making / piece, ytop / piece)
    fsync_dir(ytop)
    shutil.rmtree(making)
    shutil.rmtree(old)


def _fsync_dirs(folders: set[Path]) -> None:
    for folder in sorted(folders):
        fsync_dir(folder)
    folders.clear()


def _durable_copy(src: Path, dst: Path) -> None:
    shutil.copyfile(src, dst)
    with open(dst, "rb+") as handle:
        os.fsync(handle.fileno())


def _on_disk_spelling(ytop: Path, rel: str) -> str:
    """``rel`` with each folder spelled as an existing folder of the same
    name in any case (where a case-insensitive filesystem puts it)."""
    current, parts = ytop, rel.split("/")
    spelled = []
    for part in parts[:-1]:
        try:
            names = os.listdir(current)
        except OSError:
            names = []
        if part not in names:
            part = next((n for n in names if n.lower() == part.lower()), part)
        spelled.append(part)
        current = current / part
    return "/".join([*spelled, parts[-1]])


def _switch_upgrade(
    target: Path,
    making: Path,
    diff: UpgradeDiff,
    app_changed: bool,
    progress: _Progress,
) -> None:
    """Switch the staged files in under the journal (module docstring)."""
    ytop, old = target / ".ytop", target / OLD
    roots = set(ROOT_FILES)

    def outside_app(rel: str) -> bool:
        return not rel.startswith("app/") and rel not in roots

    aside = sorted(
        {rel for rel in diff.remove if outside_app(rel)}
        | {rel for rel in diff.copy if rel in diff.present and outside_app(rel)}
    )
    place = sorted(rel for rel in diff.copy if outside_app(rel))
    changed_roots = (["boot.py"] if "boot.py" in diff.copy else []) + [
        "build.json", MANIFEST_NAME,
    ]
    had_root = {name: os.path.lexists(ytop / name) for name in ROOT_FILES}
    new_rels = {rel for rel in diff.copy if rel not in diff.present}
    # On FAT a new file lands in an existing folder of another case
    # (``Pkg/foo.py`` in ``pkg/``): journal that spelling too.
    variants = {_on_disk_spelling(ytop, rel) for rel in new_rels}
    added = sorted(
        new_rels
        | (variants - diff.present)
        | {name for name in changed_roots if not had_root[name]}
    )
    renames = (
        2 + len(aside) + len(place)
        + sum(2 if had_root[name] else 1 for name in changed_roots)
    )
    progress.phase(
        "switching", "Putting YTOP in place…", total_files=renames, total_bytes=0,
    )
    touched: set[Path] = set()

    def move(src: Path, dst: Path) -> None:
        make_parents(dst.parent, touched)
        _place(src, dst)
        touched.update((src.parent, dst.parent))
        progress.file()

    _touch_lock(target)  # a second start must not "repair" this switch
    # 1. The journal: what the switch adds, so a rollback can take it away.
    old.mkdir()
    with open(
        old / JOURNAL, "w", encoding="utf-8", errors="surrogateescape",
        newline="\n",
    ) as handle:
        handle.write("".join(f"{rel}\n" for rel in added))
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_dirs({old, target})
    # 2. app steps aside: from here on the switch is "interrupted".
    move(ytop / "app", old / "app")
    _fsync_dirs(touched)
    # 3. Everything replaced or dropped steps aside before anything lands,
    # and the folders that left empty go: on FAT a new ``pkg/`` would
    # otherwise land in the old ``Pkg/`` (imports are case-sensitive).  A
    # rollback makes the old folders again, spelled as they were.
    for rel in aside:
        move(ytop / rel, old / rel)
    _remove_empty_dirs(ytop, aside, touched)
    _fsync_dirs(touched)
    # 4. The new files arrive (a folder that became a file is empty now).
    for rel in place:
        if (ytop / rel).is_dir() and not (ytop / rel).is_symlink():
            (ytop / rel).rmdir()
        move(making / rel, ytop / rel)
    _fsync_dirs(touched)
    # 5. Root files, never absent: backed up, then replaced; boot.py first.
    for name in changed_roots:
        if had_root[name]:
            part = old / f"{name}{PART_SUFFIX}"
            _durable_copy(ytop / name, part)
            move(part, old / name)
            fsync_dir(old)
        move(making / name, ytop / name)
        fsync_dir(ytop)
    # 6. app arrives last: the commit point.
    move(making / "app" if app_changed else old / "app", ytop / "app")
    _fsync_dirs(touched)


def _remove_empty_dirs(ytop: Path, rels: list[str], touched: set[Path]) -> None:
    """Folders under lib/ and runtime/ that moving ``rels`` away left empty
    (their parents go into ``touched``, to be fsynced)."""
    stops = {ytop, ytop / "lib", ytop / "runtime"}
    for rel in rels:
        folder = (ytop / rel).parent
        while folder not in stops and folder.is_relative_to(ytop):
            try:
                folder.rmdir()
            except OSError:
                break  # not empty (or already gone)
            touched.add(folder.parent)
            folder = folder.parent


def readme_drive_name(target: PurePath) -> str:
    """The folder a stick's drive is mounted as, for the README's Linux
    command; a drive root (Windows ``E:\\``) has none, so name it plainly."""
    return target.name or "<stick name>"


def _running_launcher() -> str | None:
    """The launcher this OS keeps reading while YTOP runs: cmd.exe reads a
    ``.cmd`` by path and offset, line by line, so replacing it under a
    running YTOP would run part of the new file once Python exits.  (POSIX
    shells keep the old file open; a replaced script is never re-read.)"""
    return "Start YTOP (Windows).cmd" if sys.platform == "win32" else None


def _write_mac_markers(target: Path) -> None:
    """Best effort, never fails a make: put the macOS markers at the drive
    root (hidden on Windows), and off a Mac delete what Spotlight and
    fseventsd already wrote there (with the markers in place a Mac does not
    rebuild it).  On a Mac the volume is mounted and in use by them, so
    their folders are left alone; the markers count from the next mount."""
    fseventsd = target / FSEVENTSD
    for path in (target / NEVER_INDEX, fseventsd / NO_LOG):
        try:
            path.parent.mkdir(exist_ok=True)
            if not path.is_file():
                path.write_bytes(b"")
        except OSError:
            pass
    for path in (target / NEVER_INDEX, fseventsd):
        try:
            _hide(path)
        except (OSError, AttributeError):
            pass
    if sys.platform == "darwin":
        return
    remove_tree_best_effort(target / SPOTLIGHT)
    try:
        logs = [p for p in fseventsd.iterdir() if p.name != NO_LOG]
    except OSError:
        logs = []
    for log in logs:
        remove_tree_best_effort(log)


def _write_launchers(
    target: Path, templates: Path, label: str, build_id: str, *, running: bool = False,
) -> None:
    """Launchers and the README at the drive root (README filled in, its CRLF
    line endings kept).  ``running`` (self mode): the launcher the running
    YTOP was started by is left as it is (see :func:`_running_launcher`)."""
    busy = _running_launcher() if running else None
    for name in LAUNCHER_NAMES:
        try:
            data = (templates / name).read_bytes()
        except OSError as exc:
            raise MakeStickError(f"the image has no launcher {name}") from exc
        if name == README:
            data = (
                data.decode("utf-8")
                .replace("{product}", NAME)
                .replace("{label}", label)
                .replace("{drive}", readme_drive_name(target))
                .replace("{build_id}", build_id)
                .encode("utf-8")
            )
        if name == busy and os.path.lexists(target / name):
            continue  # refreshed by the next upgrade started elsewhere
        temp = target / f".{name}.ytop-tmp"
        temp.write_bytes(data)
        os.replace(temp, target / name)
        if name in EXECUTABLE_LAUNCHERS:
            try:
                os.chmod(target / name, 0o755)
            except OSError:
                pass  # FAT has no modes
    fsync_dir(target)


def run_make(
    plan: MakePlan,
    source_image: Path,
    library: Library | None,
    progress_cb: ProgressCallback,
    *,
    cancel: Callable[[], bool] | None = None,
    lock_held: bool = False,
) -> MakeReport:
    """Make (or upgrade) the stick ``plan`` describes from ``source_image``.

    An error (or cancel) before the new app is in place leaves the target as
    it was; a crash is repaired by the next :func:`inspect_target`.  Only a
    new stick needs ``library`` (to register it and copy playlists).  With
    ``lock_held`` the caller holds the target's lock (an app update, from
    staging on) and releases it."""
    key = str(canonical(plan.target))
    with _ACTIVE_LOCK:
        if key in _ACTIVE:
            raise MakeStickError("a stick is already being made there")
        _ACTIVE.add(key)
    try:
        if not lock_held:
            _acquire_lock(plan.target)
        try:
            return _run(
                plan, source_image, library, progress_cb, cancel or (lambda: False)
            )
        finally:
            if not lock_held:
                _release_lock(plan.target)
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.discard(key)


def _make_new(
    plan: MakePlan,
    source: Path,
    build: dict,
    progress: _Progress,
    cancelled: Callable[[], bool],
) -> tuple[int, int]:
    """Copy, verify and swap in a whole image; returns (bytes, files) copied."""
    making = plan.target / MAKING
    rels = [rel for rel, _size in plan.files]
    making.mkdir()
    progress.phase("copying", "Copying YTOP…")
    _copy_files(source, making, rels, progress, cancelled)
    copied = (progress.bytes, progress.files)
    wanted = image_manifest(source, plan.platforms)
    _verify(wanted, making, rels, progress, cancelled)
    write_manifest(making / MANIFEST_NAME, wanted)
    _write_build(making, build, plan.platforms)
    _check_cancel(cancelled)
    progress.phase(
        "switching", "Putting YTOP in place…",
        total_files=len(PIECES_IN), total_bytes=0,
    )
    _swap_in(plan.target, making)
    progress.complete()
    return copied


def _upgrade(
    plan: MakePlan,
    source: Path,
    build: dict,
    progress: _Progress,
    cancelled: Callable[[], bool],
) -> tuple[int, int]:
    """Copy, verify and switch in only what changed; returns (bytes, files)
    copied."""
    target = plan.target
    ytop, making = target / ".ytop", target / MAKING
    wanted = image_manifest(source, plan.platforms)
    progress.phase(
        "checking", "Checking what changed…",
        total_files=len(wanted), total_bytes=0,
    )

    def checked(_rel: str) -> None:
        _check_cancel(cancelled)
        progress.file()

    image_root = plan.image_root
    diff = diff_upgrade(
        source, ytop, wanted, verify=True, on_file=checked, image_root=image_root,
    )
    progress.complete()
    app_changed = any(rel.startswith("app/") for rel in diff.copy + diff.remove)
    # app/ is switched whole: stage all of it (it is small).
    copy_rels = sorted(_app_rels(diff, wanted))
    if image_root is not None:
        # Self mode: everything written comes from the staged update, never
        # from the stick itself.
        missing = [rel for rel in copy_rels if not (source / rel).is_file()]
        if missing:
            raise MakeStickError(f"the staged update lacks {missing[0]}")
    sizes = [os.stat(source / rel).st_size for rel in copy_rels]
    if image_root is None:  # self mode moves files on the stick itself
        _check_space(target, sizes)
    progress.phase(
        "copying", "Copying YTOP…", total_files=len(copy_rels),
        total_bytes=sum(sizes),
    )
    making.mkdir()
    _copy_files(
        source, making, copy_rels, progress, cancelled, move=image_root is not None,
    )
    progress.complete()
    copied = (progress.bytes, progress.files)
    _verify(wanted, making, copy_rels, progress, cancelled)
    write_manifest(making / MANIFEST_NAME, wanted)
    _write_build(making, build, plan.platforms)
    _check_cancel(cancelled)
    _switch_upgrade(target, making, diff, app_changed, progress)
    progress.complete()
    return copied


def _run(
    plan: MakePlan,
    source: Path,
    library: Library | None,
    progress_cb: ProgressCallback,
    cancelled: Callable[[], bool],
) -> MakeReport:
    target = plan.target
    if plan.mode == "new":
        # Again now: a start file may have arrived since the plan.
        foreign = _foreign_launchers(target, source / LAUNCHER_TEMPLATES)
        if foreign:
            raise MakeStickError(
                f"{target} already has {', '.join(foreign)}, not made by YTOP; "
                "move or rename it first (nothing on the drive is overwritten)"
            )
    progress = _Progress(progress_cb, plan.bytes_needed, plan.total_files)
    repaired = recover_target(target)
    build = read_build(source)
    build_id = str(build.get("build_id") or "")
    # Cleanup runs for errors, not for a crash: what a crash leaves is
    # repaired by the next inspect.  A switch stopped by an error is rolled
    # back here, before the error goes on.
    try:
        if plan.mode == "upgrade":
            copied_bytes, copied_files = _upgrade(
                plan, source, build, progress, cancelled
            )
        else:
            copied_bytes, copied_files = _make_new(
                plan, source, build, progress, cancelled
            )
            # A new stick is no stick without its start files.
            _write_launchers(
                target, target / ".ytop" / LAUNCHER_TEMPLATES, plan.label, build_id,
            )
    except Exception:
        recover_target(target)
        raise
    notes: list[str] = []
    if plan.mode == "upgrade":
        # The new build is in place (committed): failing now would report
        # an upgrade that happened as failed.  Each start file is replaced
        # whole, so one not written is still the old one.
        try:
            _write_launchers(
                target, target / ".ytop" / LAUNCHER_TEMPLATES, plan.label, build_id,
                running=plan.image_root is not None,
            )
        except (OSError, MakeStickError) as exc:
            notes.append(
                "YTOP was upgraded, but its start files on the drive could "
                f"not all be refreshed ({exc}); upgrade this stick again to "
                "write them."
            )
    # YTOP is in place: what is left is tidying up, never an undo.  Files
    # still in use (Windows) stay for boot.py to delete at the next start.
    progress.mark("tidying")
    if plan.image_root is not None:
        _keep_previous(target)  # self mode: what was set aside is kept
    else:
        clear_old(target)
        drop_previous(target / ".ytop")  # it no longer matches
    remove_tree_best_effort(target / MAKING)
    _write_mac_markers(target)
    if plan.image_root is not None:
        warnings: list[str] = []  # self mode: the stick keeps its own store
        merged_bytes = merged_files = 0
    else:
        progress.mark("components")
        warnings, merged_bytes, merged_files = _merge_components(
            plan, source, progress, cancelled
        )
    warnings = notes + warnings
    # What was written counts the component copy too.
    copied_bytes += merged_bytes
    copied_files += merged_files

    if plan.mode == "upgrade":
        info = read_library(target)
        library_id = info.id if info is not None else ""
        copied: list[str] = []
        progress.finish(copied_bytes, copied_files)
    else:
        assert library is not None, "a new stick needs the library"
        progress.mark("registering")
        try:
            # The library marker is the stick's last write.
            ref = library.libraries.add_folder(target, plan.label)
        except (OSError, ValueError) as exc:
            raise MakeStickError(
                f"YTOP was copied but the library could not be created: {exc}"
            ) from exc
        library_id = ref.id
        progress.resume(
            copied_bytes - merged_bytes, copied_files - merged_files,
            plan.bytes_needed, plan.total_files,
        )
        copied = list(plan.playlists)
        if copied and cancelled():
            # Cancelled during the component copy: a cancel stops everything
            # left, so the chosen playlists are not copied either.
            titles = []
            for key in plan.playlists:
                view = library.playlist_view(key)
                titles.append(f"“{view.title if view is not None else key}”")
            warnings.append(
                f"The playlists you chose ({', '.join(titles)}) were not copied "
                "because the copy was cancelled; the stick works without them, "
                "and you can copy them onto it later."
            )
            copied = []
        if copied:
            progress.phase("playlists", "Copying playlists…")
        for key in copied:
            _copy_playlist(library, key, ref.id, progress, cancelled)
        progress.finish(plan.bytes_needed + merged_bytes, plan.total_files + merged_files)
    return MakeReport(
        target=target, mode=plan.mode, platforms=list(plan.platforms),
        build_id=build_id, label=plan.label, library_id=library_id,
        bytes_copied=copied_bytes, files_copied=copied_files,
        playlists=copied, repaired=repaired, warnings=warnings,
        timings=progress.clock.snapshot(),
    )


def _merge_components(
    plan: MakePlan,
    source: Path,
    progress: _Progress,
    cancelled: Callable[[], bool],
) -> tuple[list[str], int, int]:
    """Carry the newest components onto the stick, after the switch.  YTOP
    itself is in place, so a failure (or a cancel) only becomes a warning:
    the stick keeps the components it had, or the image's own copies.

    Returns the warnings and the bytes and files the copy wrote."""

    def step(_label: str) -> None:
        _check_cancel(cancelled)

    def planned(files: int, size: int) -> None:
        progress.phase(
            "components", "Copying the YouTube parts…",
            total_files=files, total_bytes=size,
        )

    def copied(size: int) -> None:
        _check_cancel(cancelled)
        progress.bytes += size
        progress.file()

    def written() -> tuple[int, int]:
        if progress.phase_name != "components":
            return 0, 0  # nothing to copy
        return progress.bytes, progress.files

    done = "upgraded" if plan.mode == "upgrade" else "copied"
    try:
        merge_newest(
            source, plan.target / ".ytop", list(plan.platforms), step,
            on_plan=planned, on_file=copied,
        )
        if progress.phase_name == "components":
            progress.complete()
    except JobCancelled:
        return [
            f"{NAME} was {done}; copying the YouTube parts was cancelled — "
            f"it uses the versions it has, and they'll be copied the next time "
            f"you upgrade this stick."
        ], *written()
    except (OSError, ValueError) as exc:
        return [
            f"{NAME} itself was {done}, but its YouTube components could not "
            f"be brought up to date ({exc}); it uses the versions it has."
        ], *written()
    return [], *written()


def _copy_playlist(
    library: Library,
    key: str,
    dest_id: str,
    progress: _Progress,
    cancelled: Callable[[], bool],
) -> None:
    view = library.playlist_view(key)
    title = view.title if view is not None else key
    base_bytes, base_files = progress.bytes, progress.files

    def on_copy(update: fsops.MoveProgress) -> None:
        progress.at(
            base_bytes + update.bytes, base_files + update.files,
            f"Copying “{title}”…",
        )

    on_copy(fsops.MoveProgress("copying", 0, 0, 0, 0, ""))
    try:
        fsops.copy_playlist(library, key, dest_id, on_copy, cancel=cancelled)
    except (ValueError, OSError, CorruptDocumentError) as exc:
        raise MakeStickError(
            f"the stick works, but copying the playlist “{title}” failed: {exc}"
        ) from exc
