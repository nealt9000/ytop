"""Where a start spends its time: one line once the server is ready.

``Started in 3.8 s: Python 0.4 s, loading YTOP 2.0 s, finding drives 0.3 s,
...``.  Each phase is the time since the previous mark, so the parts add up
to the total.  The stick's ``boot.py`` notes when it began in
``YTOP_BOOT_T0``; the operating system's process start time (Linux and
Windows) gives the "Python" phase before it.  Without a boot time there is
no line (a development ``ytop serve``).
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable

BOOT_T0_ENV = "YTOP_BOOT_T0"


class StartupClock:
    """Named phases between marks, on a wall clock (``time.time``)."""

    def __init__(
        self,
        start: float | None,
        *,
        clock: Callable[[], float] = time.time,
        process_start: float | None = None,
    ):
        self.clock = clock
        self.phases: list[tuple[str, float]] = []
        self._last = start
        if start is not None and process_start is not None and process_start < start:
            self.phases.append(("Python", start - process_start))

    def mark(self, name: str) -> None:
        now = self.clock()
        if self._last is not None:
            self.phases.append((name, max(0.0, now - self._last)))
        self._last = now

    def summary(self) -> str | None:
        if not self.phases:
            return None
        total = sum(seconds for _name, seconds in self.phases)
        parts = ", ".join(f"{name} {seconds:.1f} s" for name, seconds in self.phases)
        return f"Started in {total:.1f} s: {parts}"


def from_environment(clock: Callable[[], float] = time.time) -> StartupClock:
    """The clock for a stick start: from ``boot.py``'s note, if any."""
    try:
        boot = float(os.environ.get(BOOT_T0_ENV, ""))
    except ValueError:
        boot = None
    return StartupClock(boot, clock=clock, process_start=process_start_time())


def process_start_time(pid: int | None = None) -> float | None:
    """When this process (or process ``pid``) was created, in seconds since
    the epoch, or None (unknown, e.g. on macOS, or no such process)."""
    try:
        if sys.platform.startswith("linux"):
            return _linux_start(pid)
        if sys.platform == "win32":
            return _windows_start(pid)
    except Exception:  # timing is a diagnostic: it never stops a start
        return None
    return None


def _linux_start(pid: int | None = None) -> float:
    with open(f"/proc/{pid or 'self'}/stat", encoding="ascii") as handle:
        stat = handle.read()
    # The command name (field 2) may hold spaces; fields restart after ')'.
    fields = stat[stat.rindex(")") + 2:].split()
    start_ticks = int(fields[19])  # field 22, starttime
    with open("/proc/stat", encoding="ascii") as handle:
        btime = next(
            (line.split()[1] for line in handle if line.startswith("btime ")), None
        )
    if btime is None:
        raise ValueError("/proc/stat has no btime")
    # btime is whole seconds, so the Python phase can be off by up to ~1 s.
    return int(btime) + start_ticks / os.sysconf("SC_CLK_TCK")


def _windows_start(pid: int | None = None) -> float:
    import ctypes
    from ctypes import wintypes

    created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = []
    filetime_p = ctypes.POINTER(wintypes.FILETIME)
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [filetime_p] * 4
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    handle = kernel32.GetCurrentProcess()
    opened = None
    if pid is not None and pid != os.getpid():
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        opened = handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED
        if not opened:
            raise OSError(f"no process {pid}")
    try:
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created), ctypes.byref(exited),
            ctypes.byref(kernel), ctypes.byref(user),
        ):
            raise OSError("GetProcessTimes failed")
    finally:
        if opened:
            kernel32.CloseHandle(opened)
    ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
    # FILETIME counts 100 ns since 1601-01-01.
    return ticks / 10_000_000 - 11_644_473_600
