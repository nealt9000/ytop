"""A stick's cross-process lock (``.ytop.lock``): who holds it, taking it,
breaking a stale one and letting go.

The lock is a file created with ``O_EXCL`` that names its process
(``boot.lock_payload``: pid and start, never the computer).  Making or
upgrading a stick, an app update and a component update all take it, so
two YTOPs (a server and a CLI, or two servers) never work on one stick at
once.  A lock is held while its process lives (``boot.lock_holder``); one
naming this very process is held only if this process took it (a leftover
whose pid was recycled to ours is stale).  A doubtful lock (unreadable, or
being written) counts as held.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .stick.boot import (
    LOCK_WRITE_GRACE_S,
    break_stale_lock,
    lock_holder,
    lock_payload,
    read_lock,
)

# Lock files this process holds (normalised paths).
_HELD: set[str] = set()
_HELD_GUARD = threading.Lock()


def _key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def held(path: Path) -> bool:
    """This process holds the lock at ``path``."""
    with _HELD_GUARD:
        return _key(path) in _HELD


def _seen_live(
    path: Path, seen: tuple[bytes, int], alive: Callable[[int], bool] | None,
) -> bool:
    data, mtime_ns = seen
    try:
        doc = json.loads(data.decode("utf-8"))
        pid = int(doc["pid"])
    except (ValueError, KeyError, TypeError):
        # Being written right now, or torn by a crash long ago.
        return time.time() - mtime_ns / 1e9 < LOCK_WRITE_GRACE_S
    if pid == os.getpid():
        return held(path)
    return lock_holder(doc, alive=alive) != "dead"


def live(path: Path, alive: Callable[[int], bool] | None = None) -> bool:
    """A live process (this one included) holds the lock at ``path``."""
    try:
        seen = read_lock(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True  # there but unreadable: doubtful, so held
    return _seen_live(path, seen, alive)


def acquire(
    path: Path,
    busy: Callable[[], Exception],
    alive: Callable[[int], bool] | None = None,
    before_break: Callable[[], None] | None = None,
) -> None:
    """Take the lock at ``path`` or raise ``busy()``.

    A stale lock is broken atomically (``boot.break_stale_lock``): when two
    processes break it at once, one wins and the other sees a held lock.
    ``before_break`` runs just before a break (tests)."""
    payload = json.dumps(lock_payload()).encode("utf-8")  # no host name
    for _attempt in range(3):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            try:
                seen = read_lock(path)
            except FileNotFoundError:
                continue  # released meanwhile
            except OSError:
                raise busy() from None
            if _seen_live(path, seen, alive):
                raise busy() from None
            if not break_stale_lock(path, seen, before=before_break):
                raise busy() from None
            continue
        try:
            os.write(fd, payload)
        except BaseException:
            os.close(fd)
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        os.close(fd)
        with _HELD_GUARD:
            _HELD.add(_key(path))
        return
    raise busy()


def release(path: Path) -> None:
    """Let go of our lock at ``path``; a lock that is not ours (another
    process took it after ours was broken) is left alone."""
    with _HELD_GUARD:
        _HELD.discard(_key(path))
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        mine = int(doc["pid"]) == os.getpid()
    except FileNotFoundError:
        return
    except (OSError, ValueError, KeyError, TypeError):
        return
    if mine:
        try:
            os.unlink(path)
        except OSError:
            pass
