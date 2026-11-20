"""Run many small file operations a few at a time.

A USB stick on FAT spends most of a small file's copy waiting for the drive
(Linux mounts it with ``flush``); overlapping those waits is what makes a
stick quick to fill.  Measured on a PNY 128 GB stick with 3,282 real files:
8 files/s one at a time, 28 with 4 at once, 33 with 8."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TypeVar

T = TypeVar("T")

# Copies at once (see the module docstring).
COPY_WORKERS = 8


def run_all(
    items: Iterable[T],
    work: Callable[[T, Callable[[], bool]], None],
    *,
    workers: int = COPY_WORKERS,
) -> None:
    """Call ``work(item, stopping)`` for every item, ``workers`` at a time.

    The first exception is raised once nothing is running any more: items
    not yet started never start, and ``stopping()`` turns true so running
    ones can stop early (e.g. at their next chunk).  When this returns or
    raises, no ``work`` call is still running."""
    items = list(items)
    failed = threading.Event()
    stopping = failed.is_set
    if workers < 2 or len(items) < 2:
        for item in items:
            work(item, stopping)
        return

    def one(item: T) -> None:
        if not failed.is_set():
            work(item, stopping)

    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ytop-copy")
    futures = [pool.submit(one, item) for item in items]
    try:
        for future in as_completed(futures):
            try:
                future.result()
            except BaseException:
                failed.set()
                raise
    finally:
        # The barrier: nothing is running once this returns.
        pool.shutdown(wait=True, cancel_futures=True)
