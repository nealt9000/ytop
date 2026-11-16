"""The set of portable libraries this app can see.

A library is any folder with ``.ytop/library.json``.  Libraries are found in
four places, in this order (the first sighting of a library id wins):

* ``launch``     - the library the app was started from;
* ``detected``   - a mounted volume (or its ``YTOP`` folder) holding a marker;
* ``well-known`` - ``~/Videos/YTOP``;
* ``registered`` - folders remembered in the host registry
  (``~/.config/ytop/libraries.json``), which holds only paths.

Only online libraries are listed: an unplugged stick simply disappears and
comes back when it is mounted again.  Every probe runs in a thread with a
timeout, so a hung network mount or a dying stick cannot stall a page.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from .fsinfo import fs_info, refused_root, system_drive
from .layout import (
    SCHEMA_VERSION,
    LibraryInfo,
    LibraryLayout,
    init_library,
    read_library,
    write_library_info,
)
from .yt.metadata import human_bytes, read_json, write_json

log = logging.getLogger(__name__)

DETECT_TTL_S = 5.0
PROBE_TIMEOUT_S = 2.0
READ_ONLY_MESSAGE = (
    "This library was made by a newer YTOP. Update this app or launch from "
    "that stick."
)

Source = Literal["launch", "detected", "registered", "well-known"]


class RecoveryDeferred(Exception):
    """Raised by an ``on_new`` hook that cannot recover a library right now
    without risking a deadlock.  The library stays hidden and its recovery
    is retried by the next listing."""


class ReadOnlyLibraryError(PermissionError):
    """A write was attempted on a library made by a newer YTOP."""

    def __init__(self, message: str = READ_ONLY_MESSAGE):
        super().__init__(message)


def registry_path_default() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return base / "ytop" / "libraries.json"
    return Path.home() / ".config" / "ytop" / "libraries.json"


def _path_key(path: Path) -> str:
    try:
        return str(Path(path).expanduser().resolve())
    except OSError:
        return os.path.abspath(str(path))


def _is_within(path: Path, root: Path) -> bool:
    try:
        Path(_path_key(path)).relative_to(_path_key(root))
        return True
    except ValueError:
        return False


def _mount_parents() -> list[Path]:
    """Places where removable media usually mounts."""
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    return [
        Path("/media") / user if user else Path("/media"),
        Path("/run/media") / user if user else Path("/run/media"),
        Path("/mnt"),
        Path("/Volumes"),  # macOS
    ]


def _windows_drives() -> list[tuple[str, int]]:
    """(root, drive type) for every logical drive; does not touch the drives."""
    if sys.platform != "win32":
        return []
    import ctypes

    kernel32 = getattr(ctypes, "windll").kernel32
    buf = ctypes.create_unicode_buffer(512)
    if not kernel32.GetLogicalDriveStringsW(len(buf), buf):
        return []
    roots = buf[:].split("\0")
    return [(r, int(kernel32.GetDriveTypeW(r))) for r in roots if r]


def _windows_candidates() -> list[Path]:
    system = system_drive()
    out: list[Path] = []
    for root, kind in _windows_drives():
        if kind not in (2, 3):
            continue
        if root.rstrip("\\/").upper() == system:
            continue
        out.append(Path(root))
    return out


def _mount_candidates() -> list[Path]:
    """Children of the mount parents, listed without touching them.

    Only the parents are read here; stat-ing or resolving a child would block
    on a dead network mount or a dying stick, so every per-child check
    happens inside the timed probe thread.
    """
    if sys.platform == "win32":
        return _windows_candidates()
    candidates: list[Path] = []
    for parent in _mount_parents():
        try:
            children = sorted(parent.iterdir())
        except OSError:
            continue
        candidates.extend(children)
    return candidates


def _well_known_roots() -> list[Path]:
    return [Path.home() / "Videos" / "YTOP"]


@dataclass
class LibraryRef:
    layout: LibraryLayout
    info: LibraryInfo
    source: Source
    online: bool = True
    read_only: bool = False
    free_bytes: int = 0
    total_bytes: int = 0
    fstype: str = "unknown"

    @property
    def id(self) -> str:
        return self.info.id

    @property
    def label(self) -> str:
        return self.info.label or self.layout.root.name or "Library"

    @property
    def root(self) -> Path:
        return self.layout.root

    @property
    def is_launch(self) -> bool:
        return self.source == "launch"

    @property
    def free_label(self) -> str:
        return human_bytes(self.free_bytes) if self.free_bytes else ""

    @property
    def total_label(self) -> str:
        return human_bytes(self.total_bytes) if self.total_bytes else ""

    def require_writable(self) -> None:
        if self.read_only:
            raise ReadOnlyLibraryError()


def _probe(root: Path, source: Source) -> LibraryRef | None:
    info = read_library(root)
    if info is None:
        return None
    layout = LibraryLayout(Path(root))
    try:
        usage = shutil.disk_usage(root)
        free, total = int(usage.free), int(usage.total)
    except OSError:
        free = total = 0
    try:
        fstype = fs_info(Path(root)).fstype
    except OSError:
        fstype = "unknown"
    return LibraryRef(
        layout=layout,
        info=info,
        source=source,
        online=True,
        read_only=info.schema > SCHEMA_VERSION,
        free_bytes=free,
        total_bytes=total,
        fstype=fstype,
    )


def _probe_all(
    candidates: list[tuple[Path, Source]], timeout: float = PROBE_TIMEOUT_S
) -> list[LibraryRef | None]:
    """Probe every candidate in parallel; slow or failing ones yield None."""
    results: list[LibraryRef | None] = [None] * len(candidates)

    def run(index: int, root: Path, source: Source) -> None:
        try:
            results[index] = _probe(root, source)
        except OSError:
            results[index] = None
        except Exception:  # noqa: BLE001 - one bad stick must not break the scan
            log.exception("probing %s failed", root)
            results[index] = None

    threads = []
    for index, (root, source) in enumerate(candidates):
        thread = threading.Thread(
            target=run, args=(index, root, source), daemon=True,
            name="ytop-library-probe",
        )
        thread.start()
        threads.append(thread)
    deadline = time.monotonic() + timeout
    finished: list[LibraryRef | None] = []
    for index, thread in enumerate(threads):
        thread.join(max(0.0, deadline - time.monotonic()))
        if thread.is_alive():
            log.warning("skipping %s: it did not answer in time", candidates[index][0])
            finished.append(None)
        else:
            finished.append(results[index])
    return finished


class LibrarySet:
    """Discover, cache and edit the libraries this app can see."""

    def __init__(
        self,
        launch_root: Path | None,
        registry_path: Path | None = None,
        detect: bool = True,
        probe_timeout: float = PROBE_TIMEOUT_S,
    ):
        self.launch_root = Path(launch_root).expanduser() if launch_root else None
        self.registry_path = (
            Path(registry_path) if registry_path is not None
            else registry_path_default()
        )
        self.detect = detect
        # How long one scan waits for a drive to answer before skipping it.
        self.probe_timeout = probe_timeout
        self._lock = threading.Lock()
        self._cache: list[LibraryRef] = []
        self._cached_at = 0.0
        # Called for each library first seen after this is set (e.g. a
        # stick plugged in mid-session, or plugged back in after a scan
        # missed it), before any caller is handed it, so
        # interrupted renames and moves are finished before anything uses
        # it.  Libraries seen while it is unset count as seen.  A hook that
        # raises RecoveryDeferred is called again by a later listing.
        self.on_new: Callable[[LibraryRef], None] | None = None
        self._seen: set[str] = set()
        # Libraries whose on_new is still running: hidden from other callers.
        self._recovering: set[str] = set()
        # Libraries whose on_new raised RecoveryDeferred: hidden until a
        # later listing recovers them.
        self._deferred: set[str] = set()

    # -- registry ----------------------------------------------------------
    def _registry(self) -> list[str]:
        doc = read_json(self.registry_path) or {}
        rows = doc.get("libraries")
        return [str(row) for row in rows if isinstance(row, str) and row] if isinstance(
            rows, list
        ) else []

    def _write_registry(self, paths: list[str]) -> None:
        write_json(self.registry_path, {"schema": 1, "libraries": paths})

    # -- discovery ---------------------------------------------------------
    def _candidates(self) -> list[tuple[Path, Source]]:
        candidates: list[tuple[Path, Source]] = []
        if self.launch_root is not None:
            candidates.append((self.launch_root, "launch"))
        if self.detect:
            for mount in _mount_candidates():
                candidates.append((mount, "detected"))
                candidates.append((mount / "YTOP", "detected"))
            for root in _well_known_roots():
                candidates.append((root, "well-known"))
        for raw in self._registry():
            candidates.append((Path(raw).expanduser(), "registered"))
        return candidates

    def _scan(self) -> list[LibraryRef]:
        candidates = self._candidates()
        refs: list[LibraryRef] = []
        seen_ids: set[str] = set()
        for ref in _probe_all(candidates, self.probe_timeout):
            if ref is None or ref.id in seen_ids:
                continue
            seen_ids.add(ref.id)
            refs.append(ref)
        return refs

    def libraries(self, refresh: bool = False) -> list[LibraryRef]:
        """Online libraries, the launch library first, deduped by id."""
        with self._lock:
            now = time.monotonic()
            if refresh or not self._cached_at or now - self._cached_at >= DETECT_TTL_S:
                self._cache = self._scan()
                self._cached_at = time.monotonic()
                # A library that went away (a stick unplugged) is new again
                # when it comes back: it may have been changed elsewhere.
                online = {ref.id for ref in self._cache}
                self._seen &= online
                self._deferred &= online
            refs = list(self._cache)
            fresh = [
                ref for ref in refs
                if ref.id not in self._seen and ref.id not in self._recovering
            ]
            on_new = self.on_new
            if on_new is None:
                self._seen.update(ref.id for ref in fresh)
                return refs
            self._recovering.update(ref.id for ref in fresh)
        # Run outside the lock: the hook may itself list libraries.
        for ref in fresh:
            deferred = False
            try:
                on_new(ref)
            except RecoveryDeferred:
                deferred = True
            except Exception:  # noqa: BLE001 - a bad stick must not break pages
                log.exception("recovering %s failed", ref.root)
            finally:
                with self._lock:
                    self._recovering.discard(ref.id)
                    if deferred:
                        self._deferred.add(ref.id)
                    else:
                        self._deferred.discard(ref.id)
                        self._seen.add(ref.id)
        with self._lock:
            hidden = self._recovering | self._deferred
        if not hidden:
            return refs
        return [ref for ref in refs if ref.id not in hidden]

    def recovering(self) -> set[str]:
        """Ids of online libraries whose ``on_new`` recovery is running or
        was deferred to a later listing."""
        with self._lock:
            return self._recovering | self._deferred

    def refresh(self) -> None:
        with self._lock:
            self._cached_at = 0.0

    def get(self, library_id: str | None) -> LibraryRef | None:
        if not library_id:
            return None
        for ref in self.libraries():
            if ref.id == library_id:
                return ref
        # A stick plugged in within the cache window must still resolve.
        for ref in self.libraries(refresh=True):
            if ref.id == library_id:
                return ref
        return None

    def default(self) -> LibraryRef:
        """The launch library, else the first online one."""
        refs = self.libraries()
        if not refs:
            raise LookupError("no YTOP library is connected")
        for ref in refs:
            if ref.source == "launch":
                return ref
        return refs[0]

    def ref_for_path(self, path: Path) -> LibraryRef | None:
        """The library whose root is or contains ``path`` (most specific)."""
        best: LibraryRef | None = None
        for ref in self.libraries():
            if _is_within(path, ref.root) and (
                best is None or len(_path_key(ref.root)) > len(_path_key(best.root))
            ):
                best = ref
        return best

    # -- edits -------------------------------------------------------------
    def add_folder(self, path: Path, label: str) -> LibraryRef:
        """Create (or adopt) a library at ``path`` and remember it."""
        candidate = Path(str(path)).expanduser()
        if not candidate.is_absolute():
            raise ValueError("the library must be an absolute folder path")
        if str(candidate).strip() in {"", "/"} or refused_root(candidate):
            raise ValueError(
                "choose a USB drive or a folder, not the system's root"
            )
        if candidate.exists() and not candidate.is_dir():
            raise ValueError("that path is a file, not a folder")
        for ref in self.libraries(refresh=True):
            same = _path_key(ref.root) == _path_key(candidate)
            if not same and (
                _is_within(candidate, ref.root) or _is_within(ref.root, candidate)
            ):
                raise ValueError(
                    f"that folder overlaps the library {ref.label}; choose a "
                    "separate folder"
                )
        clean = " ".join((label or "").split()).strip()[:80] or candidate.name
        init_library(candidate, clean)
        key = _path_key(candidate)
        with self._lock:
            paths = self._registry()
            if key not in {_path_key(Path(raw)) for raw in paths}:
                paths.append(key)
                self._write_registry(paths)
            self._cached_at = 0.0
        added = self.ref_for_path(candidate)
        if added is None:
            raise ValueError("the new library could not be read back")
        return added

    def forget(self, library_id: str) -> bool:
        """Remove a library from the host registry; files are untouched."""
        ref = self.get(library_id)
        with self._lock:
            paths = self._registry()
            keep = []
            for raw in paths:
                if ref is not None and _path_key(Path(raw)) == _path_key(ref.root):
                    continue
                keep.append(raw)
            if len(keep) == len(paths):
                return False
            self._write_registry(keep)
            self._cached_at = 0.0
        return True

    def set_info(
        self, library_id: str, *, label: str, description: str = ""
    ) -> LibraryRef:
        ref = self.get(library_id)
        if ref is None:
            raise LookupError("that library is not connected")
        ref.require_writable()
        clean = " ".join((label or "").split()).strip()[:80] or ref.root.name
        info = replace(
            ref.info, label=clean, description=" ".join(description.split())[:500]
        )
        write_library_info(ref.layout, info)
        self.refresh()
        return self.get(library_id) or ref
