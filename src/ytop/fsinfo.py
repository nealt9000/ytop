"""Filesystem type detection for library destinations."""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any

FAT32_MAX_FILE = 4 * 1024**3 - 1
FAT32_SAFE_ESTIMATE = 3_800_000_000


@dataclass(frozen=True)
class FsInfo:
    fstype: str
    max_file_bytes: int | None


def _read_mounts() -> str:
    try:
        return Path("/proc/self/mounts").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _unescape(field: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def _linux_fs_info(path: Path) -> FsInfo:
    target = str(path.resolve())
    best_len = -1
    best_type = "unknown"
    for line in _read_mounts().splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mount = _unescape(parts[1]).rstrip("/") or "/"
        if mount == "/" or target == mount or target.startswith(mount + "/"):
            # >=: a later entry on the same mount point shadows earlier ones.
            if len(mount) >= best_len:
                best_len = len(mount)
                best_type = parts[2]
    if best_type.lower() in ("msdos", "vfat"):
        return FsInfo("vfat", FAT32_MAX_FILE)
    return FsInfo(best_type, None)


class _Statfs(ctypes.Structure):
    _fields_ = [
        ("f_bsize", ctypes.c_uint32),
        ("f_iosize", ctypes.c_int32),
        ("f_blocks", ctypes.c_uint64),
        ("f_bfree", ctypes.c_uint64),
        ("f_bavail", ctypes.c_uint64),
        ("f_files", ctypes.c_uint64),
        ("f_ffree", ctypes.c_uint64),
        ("f_fsid", ctypes.c_int32 * 2),
        ("f_owner", ctypes.c_uint32),
        ("f_type", ctypes.c_uint32),
        ("f_flags", ctypes.c_uint32),
        ("f_fssubtype", ctypes.c_uint32),
        ("f_fstypename", ctypes.c_char * 16),
        ("f_mntonname", ctypes.c_char * 1024),
        ("f_mntfromname", ctypes.c_char * 1024),
        ("f_flags_ext", ctypes.c_uint32),
        ("f_reserved", ctypes.c_uint32 * 7),
    ]


def _statfs_func(libc: Any) -> Any:
    """x86_64 macOS needs the 64-bit-inode entry point; arm64 has only statfs."""
    func = getattr(libc, "statfs$INODE64", None) or libc.statfs
    func.argtypes = [ctypes.c_char_p, ctypes.POINTER(_Statfs)]
    func.restype = ctypes.c_int
    return func


def _darwin_fstype(path: Path) -> str | None:
    """Filesystem type name from libc statfs, or None on any error."""
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib", use_errno=True)
        func = _statfs_func(libc)
        buf = _Statfs()
        if func(os.fsencode(path), ctypes.byref(buf)) != 0:
            return None
        return bytes(buf.f_fstypename).decode("ascii", "replace").lower() or None
    except Exception:
        return None


def _windows_fstype(path: Path) -> str | None:
    """Filesystem name for the volume holding path, or None on any error."""
    if sys.platform != "win32":
        return None
    try:
        from ctypes import wintypes

        windll = getattr(ctypes, "windll")
        kernel32 = windll.kernel32
        target = str(path.resolve())
        root = ctypes.create_unicode_buffer(260)
        if not kernel32.GetVolumePathNameW(target, root, len(root)):
            return None
        name = ctypes.create_unicode_buffer(64)
        ok = kernel32.GetVolumeInformationW(
            root.value,
            None,
            0,
            None,
            ctypes.byref(wintypes.DWORD()),
            ctypes.byref(wintypes.DWORD()),
            name,
            len(name),
        )
        return name.value or None if ok else None
    except Exception:
        return None


def _classify(fstype: str | None) -> FsInfo:
    if not fstype:
        return FsInfo("unknown", None)
    low = fstype.lower()
    if low in ("msdos", "fat32", "fat", "vfat"):
        return FsInfo("vfat", FAT32_MAX_FILE)
    return FsInfo(low, None)


def fs_info(path: Path) -> FsInfo:
    if sys.platform.startswith("linux"):
        return _linux_fs_info(path)
    try:
        if sys.platform == "darwin":
            return _classify(_darwin_fstype(path))
        if sys.platform == "win32":
            return _classify(_windows_fstype(path))
    except Exception:
        pass
    return FsInfo("unknown", None)


def fits_on(path: Path, estimated_bytes: int | None) -> bool:
    if estimated_bytes is None:
        return True
    return not (fs_info(path).fstype == "vfat" and estimated_bytes > FAT32_SAFE_ESTIMATE)


def system_drive() -> str:
    """The Windows system drive (``C:``), upper-cased, from %SystemDrive%."""
    return (os.environ.get("SystemDrive") or "C:").rstrip("\\/").upper()


def refused_root(path: PurePath, windows: bool | None = None) -> bool:
    """``path`` is a filesystem root no library or stick may take over.

    POSIX ``/`` is always refused.  On Windows a drive root such as ``E:\\``
    is how a USB stick is reached, so it is allowed unless it is the system
    drive (or has no drive letter at all).  ``windows`` defaults to the
    running OS; tests pass ``PureWindowsPath`` with ``windows=True``.
    """
    if path.parent != path:
        return False
    if windows is None:
        windows = sys.platform == "win32"
    if not windows:
        return True
    drive = path.drive.rstrip("\\/").upper()
    if len(drive) != 2 or drive[1] != ":":
        return True  # UNC share roots and driveless roots stay refused
    return drive == system_drive()
