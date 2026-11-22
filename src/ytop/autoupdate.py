"""When the component updater runs, and retrying an item after it.

Three triggers run :mod:`ytop.updater` on the launch stick's store
(``<YTOP_HOME>/.ytop/components``):

- **at start** (:func:`start_check`): once a day, in a background thread,
  when ``[updates] check_components`` is on and the network answers, and
  never while a download is running for the launch library;
- **on a YouTube-side failure** (:meth:`AutoUpdate.fix`): a probe or video
  that fails with an error :func:`ytop.yt.probe.classify_failure` calls
  ``youtube_changed`` triggers one update, then one retry of that item with
  the new components; if that still fails while the previous components
  succeed on a second retry, the new version is rolled back (and pinned
  bad).  Only when ``[updates] check_components`` is on: off, the job
  suggests **Update now** instead;
- **Update now** on the Storage page (a job, see ``JobManager.start_update``).

The running process imported yt-dlp at start and keeps it until it exits:
a module reload is not safe.  So a retry runs yt-dlp in a child of the
stick's own Python whose ``sys.path`` starts with the chosen component
folders (:class:`ytop.yt.runner.SubprocessRunner`); everything else of the
probe or download runs here as usual.  The components the process itself
imported stay in use for everything else until the next start.

A checkout without a store has nothing to update: every trigger does
nothing, and the Storage page says so.
"""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

from . import updater
from .components import (
    PYTHON_COMPONENTS,
    Current,
    protect,
    read_current,
    remove_in_use,
    runtime_folders,
    store_dir,
    write_in_use,
)
from .config import Config
from .runtime import active_runtime
from .stick.boot import CODE_ZIP, code_paths
from .yt import runner as ytdlp_runner

T = TypeVar("T")

UPDATING = "YouTube changed; updating its parts…"
RETRIED = "Updated {name} to {version} and retried."
GAVE_UP = "Couldn't fix automatically; try again later."
ROLLED_BACK = "The new {name} {version} didn't work; went back to the previous version."
CHECKS_OFF = (
    "This may be YouTube changing. Automatic updates of the YouTube parts are off; "
    "try \"Update now\" on the Storage page."
)
CHECK_EVERY = timedelta(hours=24)
IDLE_POLL_S = 30.0

Progress = Callable[[str], None]


def store_for_runtime() -> Path | None:
    """The launch stick's ``.ytop`` when it has a component store, else None
    (a checkout, or a stick without one)."""
    home = active_runtime().home
    if home is None:
        return None
    ytop = home / ".ytop"
    return ytop if store_dir(ytop).is_dir() else None


def _host_platform() -> str | None:
    from .runtime import UnsupportedPlatform, current_platform

    try:
        return current_platform()
    except UnsupportedPlatform:
        return None


def _under(path: str, folder: Path) -> bool:
    try:
        return Path(os.path.abspath(path)).is_relative_to(os.path.abspath(folder))
    except (OSError, ValueError):
        return False


class AutoUpdate:
    """The updater as the app uses it: one at a time, on one store.

    ``check``/``update``/``rollback`` default to :mod:`ytop.updater`'s;
    tests pass fakes.  ``loaded`` is the component folders this process
    imported (default: the ``sys.path`` entries inside the store) and
    ``base_path`` the rest of a child's ``sys.path`` (default: this
    process's, without them)."""

    def __init__(
        self,
        ytop: Path | None,
        config: Callable[[], Config],
        *,
        check: Callable[..., updater.CheckResult] | None = None,
        update: Callable[..., updater.UpdateResult] | None = None,
        rollback: Callable[[Path, str], None] | None = None,
        make_runner: Callable[..., Any] = ytdlp_runner.SubprocessRunner,
        loaded: Sequence[Path] | None = None,
        base_path: Sequence[Path] | None = None,
        python: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.ytop = ytop
        self.config = config
        self._check = check or updater.check
        self._update = update or updater.update
        self._rollback = rollback or updater.rollback
        self._make_runner = make_runner
        self._python = python or sys.executable
        self.clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        store = store_dir(ytop) if ytop is not None else None
        if loaded is None:
            # Version folders: a packed one is on sys.path as its zip and
            # itself (ytop.stick.boot.code_paths).
            loaded = []
            for p in sys.path:
                if p and store is not None and _under(p, store):
                    folder = Path(p).parent if Path(p).name == CODE_ZIP else Path(p)
                    if folder not in loaded:
                        loaded.append(folder)
        if base_path is None:
            base_path = [Path(p) for p in sys.path
                         if p and not (store is not None and _under(p, store))]
        self.loaded = [Path(p) for p in loaded]
        self.base_path = [Path(p) for p in base_path]
        # Pruning never deletes what this process imported, nor the Deno and
        # bgutil server it found at start (see ytop.components.protect);
        # claim() tells other apps on the stick.
        self.in_use = [*self.loaded]
        if ytop is not None:
            self.in_use += runtime_folders(ytop, _host_platform())
        protect(self.in_use)

    def claim(self) -> None:
        """Leave this process's in-use marker in the store (app start)."""
        if self.ytop is not None and self.available:
            write_in_use(self.ytop, self.in_use)

    def release(self) -> None:
        """Remove the in-use marker (clean exit)."""
        if self.ytop is not None:
            remove_in_use(self.ytop)

    @classmethod
    def for_runtime(cls, config: Callable[[], Config]) -> AutoUpdate:
        return cls(store_for_runtime(), config)

    @property
    def available(self) -> bool:
        return self.ytop is not None and store_dir(self.ytop).is_dir()

    # -- the store -----------------------------------------------------------
    def current(self) -> Current:
        return read_current(self.ytop) if self.ytop is not None else Current()

    def folders(self, current: Current) -> list[Path]:
        """The Python component folders a run with ``current`` imports."""
        if self.ytop is None:
            return []
        found = updater._current_folders(self.ytop, current)
        return [found[name] for name in PYTHON_COMPONENTS if name in found]

    def runs_here(self, current: Current) -> bool:
        """This process already imported what ``current`` names."""
        def norm(paths: list[Path]) -> list[str]:
            return [os.path.normcase(os.path.abspath(p)) for p in paths]

        return norm(self.folders(current)) == norm(self.loaded)

    def runner(self, current: Current, cancel: Callable[[], bool] | None = None) -> Any:
        """A yt-dlp runner for ``current``'s components (None: this process
        already runs them)."""
        if self.runs_here(current):
            return None
        paths = [p for folder in self.folders(current) for p in code_paths(folder)]
        return self._make_runner([*paths, *self.base_path],
                                 python=self._python, cancel=cancel)

    def status(self) -> dict[str, Any]:
        """What the Storage page shows."""
        if not self.available or self.ytop is None:
            return {"available": False}
        current = self.current()
        try:
            versions = updater.installed_versions(self.ytop)
        except OSError:
            versions = {}
        return {
            "available": True,
            "versions": dict(sorted(versions.items())),
            "last_check": current.last_check,
            "last_result": current.last_result,
            "pinned_bad": {k: list(v) for k, v in sorted(current.pinned_bad.items()) if v},
            "restart_needed": not self.runs_here(current),
            "check_components": self.config().updates.check_components,
        }

    # -- the updater, one at a time ------------------------------------------
    def check(self) -> updater.CheckResult:
        if self.ytop is None or not self.available:
            return updater.CheckResult("error", message="no component store")
        # One at a time with update/rollback: a check writes current.json too
        # and must never put back what an update just switched away from.
        with self._lock:
            return self._check(self.ytop, self.config(), now=self.clock())

    def update(
        self, progress: Progress | None = None, names: list[str] | None = None,
    ) -> updater.UpdateResult:
        """Update every component (or only ``names``, e.g. what an app update
        needs first)."""
        if self.ytop is None or not self.available:
            return updater.UpdateResult("error", message="no component store")
        extra: dict[str, Any] = {"names": names} if names is not None else {}
        with self._lock:
            return self._update(self.ytop, self.config(), progress=progress, **extra)

    def rollback(self, names: Sequence[str]) -> None:
        if self.ytop is None:
            return
        with self._lock:
            for name in names:
                self._rollback(self.ytop, name)

    # -- a YouTube-side failure ----------------------------------------------
    def fix(
        self,
        items: Sequence[T],
        attempt: Callable[[T], bool],
        *,
        say: Callable[[str], None],
        cancel: Callable[[], bool] | None = None,
    ) -> list[bool]:
        """Update once, then retry each item once with the new components
        (``attempt(item)`` runs it under the runner and says whether it
        worked).  If none works and the previous components make one work
        on a second retry, roll back what this update switched in.  Returns
        whether each item now worked; tells the job what happened through
        ``say``."""
        ok = [False] * len(items)
        if not items or not self.available:
            return ok
        if not self.config().updates.check_components:
            say(CHECKS_OFF)  # the user turned automatic updates off
            return ok
        say(UPDATING)
        before = self.current()
        result = self.update()
        after = self.current()
        if self.runs_here(after):
            say(GAVE_UP)  # nothing new to try
            return ok
        new = self.runner(after, cancel)
        ok = [self._attempt(new, attempt, item) for item in items]
        if not any(ok) and result.status == "updated":
            old = self.runner(before, cancel)
            again = [self._attempt(old, attempt, item) for item in items]
            if any(again):
                self.rollback(list(result.updated))
                name, version = self._named(result, after)
                say(ROLLED_BACK.format(name=name, version=version))
                return again
        if any(ok):
            name, version = self._named(result, after)
            say(RETRIED.format(name=name, version=version))
        else:
            say(GAVE_UP)
        return ok

    @staticmethod
    def _attempt(runner: Any, attempt: Callable[[T], bool], item: T) -> bool:
        with ytdlp_runner.using(runner):
            return bool(attempt(item))

    @staticmethod
    def _named(result: updater.UpdateResult, after: Current) -> tuple[str, str]:
        """The component a message names: yt-dlp unless this update switched
        only others."""
        if result.updated and "yt-dlp" not in result.updated:
            name = next(iter(result.updated))
            return name, result.updated[name][1]
        if "yt-dlp" in result.updated:
            return "yt-dlp", result.updated["yt-dlp"][1]
        return "yt-dlp", after.current.get("yt-dlp", "the newest version")


# --- at start ------------------------------------------------------------------------


def check_due(current: Current, now: datetime) -> bool:
    """No check yet, the last one more than a day ago, or it found no
    network (so the next start tries again)."""
    if not current.last_check or current.last_result == "offline":
        return True
    try:
        last = datetime.fromisoformat(current.last_check)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return now - last > CHECK_EVERY


def _wait_idle(downloading: Callable[[], bool], stop: threading.Event, poll_s: float) -> bool:
    while downloading():
        if stop.wait(poll_s):
            return False
    return not stop.is_set()


def start_check(
    auto: AutoUpdate,
    *,
    downloading: Callable[[], bool],
    stop: threading.Event,
    poll_s: float = IDLE_POLL_S,
) -> str:
    """The start-up check: ``check`` then, if anything is newer, ``update``.
    Returns what happened (for the log and tests)."""
    if not auto.available:
        return "no store"
    if not auto.config().updates.check_components:
        return "off"
    if not check_due(auto.current(), auto.clock()):
        return "recent"
    if not _wait_idle(downloading, stop, poll_s):
        return "stopped"
    found = auto.check()
    if found.status != "ok":
        return found.status
    if not found.available:
        return "up to date"
    if not _wait_idle(downloading, stop, poll_s):
        return "stopped"
    return auto.update().status


def start_check_thread(
    auto: AutoUpdate,
    *,
    downloading: Callable[[], bool],
    stop: threading.Event,
    poll_s: float = IDLE_POLL_S,
    done: Callable[[str], None] | None = None,
) -> threading.Thread | None:
    """Run :func:`start_check` in a daemon thread (never delays a page)."""
    if not auto.available:
        return None

    def run() -> None:
        try:
            outcome = start_check(auto, downloading=downloading, stop=stop, poll_s=poll_s)
        except Exception as exc:  # noqa: BLE001 - a check never breaks the app
            outcome = f"error: {type(exc).__name__}: {exc}"
        if done is not None:
            done(outcome)

    thread = threading.Thread(target=run, name="ytop-component-check", daemon=True)
    thread.start()
    return thread
