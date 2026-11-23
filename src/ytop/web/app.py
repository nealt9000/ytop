"""FastAPI application for the local, offline YouTube-playlist experience."""

from __future__ import annotations

import errno
import json
import logging
import math
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import weakref
import webbrowser
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.datastructures import FormData
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import __build__ as ytop_build
from .. import __version__ as ytop_version
from .. import autoupdate as autoupdate_mod
from .. import makestick, product
from ..appupdate import AppUpdates, InstallAborted, InstallResult
from ..autoupdate import AutoUpdate
from ..config import (
    SESSION_AUTH,
    Config,
    is_loopback_host,
    load_config,
    save_config,
    set_updates_option,
)
from ..index import index_for
from ..libraries import (
    LibraryRef,
    LibrarySet,
    ReadOnlyLibraryError,
    RecoveryDeferred,
)
from ..publish import PUBLISH_LOCK, recover_rename_journal
from ..runtime import PLATFORMS, UnsupportedPlatform, active_runtime, current_platform
from ..startup import StartupClock, from_environment
from ..stickplatforms import PlatformAdds, platform_label
from ..tags import TagStore, apply_edits, effective_category, normalize_tag
from ..targets import TargetError, parse_target
from ..toolchain import (
    deno_path,
    ffmpeg_path,
    ffprobe_path,
    js_runtime,
    toolchain_source,
)
from ..yt.browsers import firefox_profiles
from ..yt.metadata import (
    CorruptDocumentError,
    fsync_dir,
    human_bytes,
    prune_legacy_lock_dir,
    prune_lock_sidecars,
)
from ..yt.pot import check_browser_spec
from ..yt.probe import use_library_deno_dir
from ..yt.proxies import format_servers, parse_servers, redact
from .browse import (
    build_home,
    build_videos,
    cards_for,
    library_videos,
    search_library,
    storage_usage,
)
from .fsops import STAGING_PREFIX
from .jobs import UPGRADE_HEIGHTS, JobManager
from .library import Library
from .render import (
    format_date,
    format_duration,
    format_finished,
    format_hours,
    format_timestamp,
    media_kind,
)

WEB_ROOT = Path(__file__).resolve().parent
TEMPLATE_DIR = WEB_ROOT / "templates"
STATIC_DIR = WEB_ROOT / "static"

log = logging.getLogger(__name__)
# Control characters other than tab; the server list may also hold line breaks.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
_CONTROL_CHARS_MULTILINE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


TAGS_READ_ONLY_MESSAGE = "This library is read-only, so its tags can't be changed."
# A tag edit refused with EACCES/EPERM is tried again after these pauses: on
# Windows a virus scanner or indexer briefly holding tags.json looks the same.
TAG_EDIT_RETRY_S = (0.1, 0.25)
TAGS_DAMAGED_MESSAGE = "The tags file on this library is damaged; nothing was changed."


class _RevalidatedStatic(StaticFiles):
    """CSS and JS that the browser re-checks on every page load.

    Their URLs never change, so without this a browser keeps the files from
    before a stick upgrade and pairs new pages with old styles and scripts.
    A re-check of an unchanged file is a 304 on localhost.
    """

    async def get_response(self, path: str, scope):  # type: ignore[no-untyped-def]
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


class ProgressUpdate(BaseModel):
    playlist_id: str | None = Field(default=None, max_length=100)
    video_id: str = Field(min_length=1, max_length=64)
    position: float = Field(default=0.0)
    duration: float | None = None
    watched: bool | None = None


SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
AMBIGUOUS_MESSAGE = (
    "This playlist now exists in more than one library — reload the page."
)
# Portable mode exits when no page has been open for this long.
# A stick name shown in the offer-switch banner: a short folder or drive
# name, else a generic wording.
OFFER_LABEL_RE = re.compile(r"[\w .()'&+-]{1,40}")


def offer_label(value: str | None) -> str:
    text = (value or "").strip()
    return text if OFFER_LABEL_RE.fullmatch(text) else "another stick"


# The unfiltered Videos page shows at most this many; a tag, category or
# channel narrows it.
UNFILTERED_VIDEO_LIMIT = 500
IDLE_TIMEOUT_S = 300.0
IDLE_CHECK_S = 30.0
# How long a second Quit waits for a flush already under way.
QUIT_FLUSH_WAIT_S = 120.0
# How long an installed app update waits for its page to show the result
# before the process exits.
APP_UPDATE_SHOW_S = 15.0
# How often the open app asks whether an app re-check is due (the check
# itself waits for ``appupdate.CHECK_EVERY``).
RECHECK_POLL_S = 600


def _host_name(header: str) -> str:
    try:
        return (urlsplit(f"//{header}").hostname or "").lower()
    except ValueError:
        return ""


def _safe_next_url(value: str, default: str = "/downloads") -> str:
    """Only an absolute local path may be used as a redirect target."""
    candidate = (value or "").strip()
    if not candidate.startswith("/") or candidate.startswith("//"):
        return default
    if "\\" in candidate or "\x00" in candidate:
        return default
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return default
    if parsed.scheme or parsed.netloc:
        return default
    return candidate


def _file_response(
    path: Path,
    *,
    media_type: str,
    cache: str = "public, max-age=3600",
) -> FileResponse:
    headers = {
        "Cache-Control": cache,
        "X-Content-Type-Options": "nosniff",
    }
    return FileResponse(path, media_type=media_type, headers=headers)


def _recover_libraries(refs: list[LibraryRef]) -> None:
    """Finish interrupted rename batches and set aside orphaned downloads.

    Runs once at startup, before any job worker exists: the orphan sweep must
    never race a download that is publishing.
    """
    for ref in refs:
        if ref.read_only:
            continue
        try:
            recover_rename_journal(ref.layout)
        except OSError as exc:
            log.warning("could not recover %s: %s", ref.root, exc)


def _tags_writable(ref: LibraryRef) -> bool:
    """Tag edits can be saved: not a newer-schema library, and its data
    folder (where tags.json is replaced) is writable as far as the OS says.
    A locked stick or read-only mount hides the tag controls."""
    if ref.read_only:
        return False
    data_dir = ref.layout.data_dir
    try:
        if not data_dir.is_dir():
            return os.access(ref.layout.ytop_dir, os.W_OK)
        return os.access(data_dir, os.W_OK)
    except OSError:
        return False


def _unplug_needs_quit() -> bool:
    """Windows and macOS run the app from the launch stick itself (Linux
    runs a copy in the host's cache), so that stick stays busy until Quit."""
    return sys.platform in ("win32", "darwin")


def _runs_from_cache() -> bool:
    """A stick runtime that lives outside the stick (Linux's host cache)."""
    runtime = active_runtime()
    if runtime.home is None or runtime.runtime_dir is None:
        return False
    home = Path(os.path.abspath(runtime.home))
    runtime_dir = Path(os.path.abspath(runtime.runtime_dir))
    return not runtime_dir.is_relative_to(home)


def _pending_journal(ref: LibraryRef) -> bool:
    """An interrupted rename batch or playlist move awaits recovery here."""
    try:
        if ref.layout.rename_journal.exists():
            return True
        meta_root = ref.layout.meta_root
        if not meta_root.is_dir():
            return False
        return any(
            entry.name.startswith(STAGING_PREFIX) for entry in meta_root.iterdir()
        )
    except OSError:
        return True  # cannot tell: never claim it is safe


def _flush_library(ref: LibraryRef) -> None:
    layout = ref.layout
    for directory in (
        layout.meta_root, layout.data_dir, layout.media_root, layout.ytop_dir,
        layout.root,
    ):
        fsync_dir(directory)


def _sync_filesystems() -> None:
    """Push every pending write to the drives (POSIX; a no-op elsewhere)."""
    sync = getattr(os, "sync", None)
    if sync is not None:
        sync()


def _with_overrides(cfg: Config, overrides: dict | None) -> Config:
    """Apply CLI flags (e.g. ``--audio-lang``) on top of a library's config."""
    if not overrides:
        return cfg
    unknown = set(overrides) - set(Config.__dataclass_fields__)
    if unknown:
        raise ValueError(f"unknown config overrides: {sorted(unknown)}")
    return replace(cfg, **overrides)


def _index_libraries(libs: LibrarySet, stop: threading.Event) -> None:
    """Bring each writable library's index up to date in the background.

    Start-up never waits for it.  The index works in short batches with its
    lock released in between, so a page opened meanwhile waits at most about
    one batch (and finishes whatever is left itself).  A library that
    disappears (a stick pulled) or any other failure is logged once and
    skipped.
    """
    warned = False
    try:
        refs = [ref for ref in libs.libraries() if not ref.read_only]
    except Exception as exc:  # noqa: BLE001
        refs = []
        warned = True
        print(f"library index: {exc}", file=sys.stderr)
    for ref in refs:
        if stop.is_set():
            return
        try:
            index = index_for(ref.layout)
            index.ensure_fresh()
            # A slice that finds no tags (videos without any) is still
            # progress: carry on until the backfill says it is done.
            while not stop.is_set():
                index.backfill_tags(budget_s=2.0)
                if not index.backfill_pending:
                    break
        except Exception as exc:  # noqa: BLE001 - the index is disposable
            if not warned:
                warned = True
                print(f"library index for {ref.layout.root}: {exc}", file=sys.stderr)


def create_app(
    launch_root: Path | None,
    *,
    libraries: LibrarySet | None = None,
    config_overrides: dict | None = None,
    instance_token: str | None = None,
    portable: bool = False,
    clock: Callable[[], float] = time.monotonic,
    idle_timeout_s: float = IDLE_TIMEOUT_S,
    idle_check_s: float = IDLE_CHECK_S,
    startup: StartupClock | None = None,
    autoupdate: AutoUpdate | None = None,
    update_poll_s: float = autoupdate_mod.IDLE_POLL_S,
    app_updates: AppUpdates | None = None,
    platform_adds: PlatformAdds | None = None,
) -> FastAPI:
    """Build the app.

    ``instance_token`` authenticates the ``/api/instance`` control endpoints
    (a random, unpublished one is used when omitted).  ``portable`` shows the
    running-from banner with Quit and exits after ``idle_timeout_s`` without
    a page heartbeat (checked every ``idle_check_s`` against ``clock``).
    ``startup`` times the start; its line prints once the server is ready.
    ``autoupdate`` runs the component updater on the launch stick's store
    (default: the running stick's, none on a checkout); ``app_updates``
    checks for and installs app releases on the running stick.
    """
    launch = Path(launch_root).expanduser() if launch_root else None

    def app_config(root: Path | None) -> Config:
        return _with_overrides(load_config(root), config_overrides)

    if libraries is None:
        libraries = LibrarySet(launch, detect=app_config(launch).detect_removable)
    libs = libraries

    def mark(phase: str) -> None:
        if startup is not None:
            startup.mark(phase)

    refs = libs.libraries(refresh=True)
    mark("finding drives")
    _recover_libraries(refs)
    mark("opening libraries")
    lib = Library(libs)
    # The job manager, once it exists; until then it reconciles the moves
    # of every library it reads itself.
    job_managers: list[JobManager] = []

    def recover_new_library(ref: LibraryRef) -> None:
        """A library first seen mid-session (a stick plugged in late) is
        recovered before any page, job or edit is handed it.

        All of it runs under PUBLISH_LOCK, so it never finishes or rolls
        back the rename batch or move of an edit still in flight."""
        if ref.read_only:
            return
        manager = job_managers[0] if job_managers else None
        if manager is not None and manager.busy_with(ref.id):
            # Only missed by one scan (a slow probe) while a job writes to
            # it: its rename batch may be the job's own, still running.
            # Checked before PUBLISH_LOCK too, so a listing never waits
            # behind a long same-device move for a stick that move uses.
            return
        if not PUBLISH_LOCK.acquire(blocking=False):
            if manager is not None and manager.holds_lock():
                # The lock order is PUBLISH_LOCK, then the job lock (a move
                # reports progress while publishing): waiting here could
                # deadlock.  Hidden until a listing outside the job lock.
                raise RecoveryDeferred(ref.id)
            PUBLISH_LOCK.acquire()
        try:
            if manager is not None and manager.busy_with(ref.id):
                return  # a job started on it while we waited
            try:
                recover_rename_journal(ref.layout)
            except OSError as exc:
                log.warning("could not recover %s: %s", ref.root, exc)
            if manager is not None:
                manager.recover_library(ref)
        finally:
            PUBLISH_LOCK.release()

    libs.on_new = recover_new_library

    def home_library() -> LibraryRef | None:
        try:
            return libs.default()
        except LookupError:
            return None

    home = home_library()
    cfg = app_config(home.root if home is not None else None)
    # The component store belongs to the launch stick, so its settings are
    # the launch library's.
    update_root = home.root if home is not None else None

    def _age_gate_set() -> bool:
        return bool(update_root is not None and load_config(update_root).age_gate_browser)

    if autoupdate is None:
        autoupdate = AutoUpdate.for_runtime(lambda: app_config(update_root))
    auto = autoupdate
    if app_updates is None:
        app_updates = AppUpdates.for_runtime(lambda: app_config(update_root), update_root)
        app_updates.update_components = lambda names: auto.update(names=names)
    releases = app_updates
    if platform_adds is None:
        platform_adds = PlatformAdds.for_runtime(lambda: app_config(update_root))
    adds = platform_adds
    csrf_token = secrets.token_urlsafe(32)
    control_token = instance_token or secrets.token_urlsafe(32)

    async def guard(request: Request) -> None:
        """Reject non-loopback hosts, cross-site writes and missing CSRF tokens.

        Requests without browser metadata (CLI clients, tests) still work, but
        a browser always supplies ``Origin``/``Sec-Fetch-Site`` on unsafe
        requests, so those paths must carry the token rendered into the page.
        """
        host_header = request.headers.get("host", "")
        hostname = _host_name(host_header)
        if hostname != "testserver" and not is_loopback_host(hostname):
            raise HTTPException(status_code=400, detail="invalid Host header")
        if request.method.upper() in SAFE_METHODS:
            return
        origin = request.headers.get("origin")
        site = request.headers.get("sec-fetch-site")
        if origin:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or parsed.netloc.lower() != host_header.lower()
            ):
                raise HTTPException(
                    status_code=403, detail="cross-origin request rejected"
                )
        if site and site not in {"same-origin", "same-site", "none"}:
            raise HTTPException(
                status_code=403, detail="cross-site request rejected"
            )
        if not (origin or site):
            # Not a browser form/fetch: keep the API usable from scripts.
            return
        if request.url.path.startswith("/api/instance"):
            # Authenticated by the instance token instead (require_token).
            return
        presented = request.headers.get("x-csrf-token") or ""
        content_type = request.headers.get("content-type", "")
        if not presented and content_type.startswith(
            ("application/x-www-form-urlencoded", "multipart/form-data")
        ):
            form = await request.form()
            presented = str(form.get("csrf_token") or "")
        if not presented or not secrets.compare_digest(presented, csrf_token):
            raise HTTPException(
                status_code=403, detail="missing or invalid CSRF token"
            )

    async def form_data(request: Request) -> FormData:
        """The posted form, read on the event loop.

        Form routes take it as a dependency so they can stay plain ``def``:
        FastAPI runs those in a worker thread, so their disk work (renaming a
        playlist's files on a slow stick can take minutes) never stalls the
        loop and every other request with it.
        """
        return await request.form()

    def require_token(request: Request) -> None:
        presented = request.headers.get("x-ytop-token") or ""
        if not secrets.compare_digest(
            presented.encode("utf-8"), control_token.encode("utf-8")
        ):
            raise HTTPException(status_code=403, detail="invalid instance token")

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.last_heartbeat = clock()
        nonlocal startup
        if startup is not None:
            startup.mark("starting the server")
            line = startup.summary()
            startup = None  # printed once, even if the lifespan runs again
            if line:
                try:
                    print(line, flush=True)
                except OSError:
                    pass  # no console to show it on
        idle_thread: threading.Thread | None = None
        auto.claim()  # the components this process imported stay on the stick
        # Once a day, in the background: never delays the first page.
        update_library = home.id if home is not None else ""

        def start_checked(outcome: str) -> None:
            log.info("component check at start: %s", outcome)
            try:  # the network answered: ask about (or look for) app updates
                releases.on_start_outcome(outcome)
            except Exception:  # noqa: BLE001 - a check never breaks the app
                log.exception("app update check at start failed")

        autoupdate_mod.start_check_thread(
            auto,
            downloading=lambda: application.state.jobs.downloading(update_library),
            stop=idle_stop,
            poll_s=update_poll_s,
            done=start_checked,
        )
        def recheck_loop() -> None:
            while not idle_stop.wait(RECHECK_POLL_S):
                try:
                    releases.recheck_if_due()
                except Exception:  # noqa: BLE001 - a check never breaks the app
                    log.exception("app update re-check failed")

        threading.Thread(target=recheck_loop, name="ytop-app-recheck", daemon=True).start()
        if portable:
            idle_thread = threading.Thread(
                target=watch_idle, name="ytop-idle", daemon=True
            )
            idle_thread.start()
        threading.Thread(
            target=_index_libraries, args=(libs, idle_stop),
            name="ytop-index", daemon=True,
        ).start()
        try:
            yield
        finally:
            idle_stop.set()
            if idle_thread is not None:
                idle_thread.join(timeout=5)
            wait_for_flush()
            # Workers must have exited before the process can disappear, so a
            # restart never sees a half-written playlist or a stale job.
            application.state.jobs.close()
            auto.release()
            for hook in application.state.shutdown_hooks:
                try:
                    hook()
                except Exception:
                    log.exception("shutdown hook failed")

    app = FastAPI(
        title="ytop",
        description="Offline YouTube playlist library",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
        dependencies=[Depends(guard)],
    )
    app.state.library = lib
    app.state.config = cfg
    app.state.csrf_token = csrf_token
    # A newer YTOP on another stick.  Its launcher says so over the
    # token-checked rescan call, and only that is remembered for every page;
    # a plain ``?offer_switch=1`` link (anyone can make one) shows the banner
    # on that one page.  Either way the stick name must look like one.
    app.state.switch_offer = ""
    # The running uvicorn.Server (set by ``serve``) and the one graceful stop
    # path: the instance shutdown endpoint (and Quit/idle exit) call
    # ``app.state.request_stop()``.
    app.state.server = None
    app.state.stop_requested = False

    def request_stop() -> None:
        # Remembered first: a stop asked for before ``serve`` assigns the
        # server (SIGHUP right after launch) is applied by ``_run_server``.
        app.state.stop_requested = True
        server = app.state.server
        if server is not None:
            server.should_exit = True

    app.state.request_stop = request_stop
    # Called after jobs close on lifespan shutdown (e.g. drop instance file).
    app.state.shutdown_hooks = []
    app.state.instance_version = (ytop_version, ytop_build)
    # Each job reads its destination library's config, with the CLI
    # overrides and this session's browser login on top.
    app.state.jobs = JobManager(
        lib, config_for=lambda root: SESSION_AUTH.apply(app_config(root)),
        autoupdate=auto,
    )
    app.state.autoupdate = auto
    app.state.app_updates = releases
    job_managers.append(app.state.jobs)
    mark("loading jobs")
    app.state.last_heartbeat = clock()
    app.state.stopping = False
    idle_stop = threading.Event()
    quit_lock = threading.Lock()
    quit_done = threading.Event()  # set once the final flush has finished
    # A final flush (Quit's or an app update's) has started / finished.
    flush_started = threading.Event()
    flush_done = threading.Event()

    def active_job_count() -> int:
        return sum(1 for job in app.state.jobs.list() if job.active)

    def quit_now(force: bool = True, background: bool = False) -> bool:
        """Quit, idle exit and takeover: stop the workers, flush every
        library to its drive, then stop uvicorn; the shutdown hooks release
        the instance file.

        Without ``force`` nothing stops while a job is active (``False``);
        the job manager closes in the same step as that check, so no job can
        start in between.  With ``background`` the decision is made here but
        the flush (a slow stick's final sync can take many seconds) runs on
        its own thread, so the caller can answer at once.
        """
        with quit_lock:
            already = app.state.stopping
            if not already:
                if not force and not app.state.jobs.close(only_if_idle=True):
                    return False
                app.state.stopping = True
        if already:
            # Another quit (a takeover's background flush) is under way: a
            # synchronous caller answers only once the drives are flushed.
            if not background:
                quit_done.wait(QUIT_FLUSH_WAIT_S)
            return True
        idle_stop.set()
        if background:
            threading.Thread(
                target=finish_quit, name="ytop-quit", daemon=True
            ).start()
        else:
            finish_quit()
        return True

    def finish_quit() -> None:
        flush_started.set()
        try:
            app.state.jobs.close()
            for ref in libs.libraries():
                if not ref.read_only:
                    _flush_library(ref)
            _sync_filesystems()
        except Exception:
            log.exception("flushing before quit failed")
        finally:
            flush_done.set()
            quit_done.set()
            stop_server()

    def stop_server() -> None:
        try:
            app.state.request_stop()
        except Exception:
            log.exception("could not stop the server")

    def wait_for_flush() -> None:
        """Hold the lifespan shutdown (and so the process exit) until a
        final flush under way has finished: SIGINT/SIGTERM during a
        takeover's background flush must not cut it short.  uvicorn's
        force quit (a second Ctrl+C) stops waiting."""
        if not flush_started.is_set():
            return
        while not flush_done.wait(0.1):
            server = app.state.server
            if server is not None and getattr(server, "force_exit", False):
                log.warning("force quit before the drives were flushed")
                return

    app.state.quit = quit_now

    def stop_for_install() -> None:
        """An app update's step 4: stop the workers and flush every library,
        exactly as Quit does, but keep serving until the switch is done
        (``install_finished`` stops the server)."""
        with quit_lock:
            if app.state.stopping:
                raise InstallAborted(f"{product.NAME} is quitting; nothing was changed.")
            if not app.state.jobs.close(only_if_idle=True):
                raise InstallAborted(
                    "A download started; let it finish, then install the update."
                )
            app.state.stopping = True
        idle_stop.set()
        # From here on the app's own files may be moved: pages render from
        # the templates already loaded, never re-read from disk.
        templates.env.auto_reload = False
        flush_started.set()
        try:
            for ref in libs.libraries():
                if not ref.read_only:
                    _flush_library(ref)
            _sync_filesystems()
        except Exception:
            log.exception("flushing before the update failed")
        finally:
            flush_done.set()

    def install_finished(result: InstallResult) -> None:
        """The update is done (or undone): if jobs were stopped for it, say
        so on the console and exit once the page has shown it."""
        if not result.stopped:
            return
        try:
            print(result.message, flush=True)
        except OSError:
            pass  # no console
        releases.shown.wait(APP_UPDATE_SHOW_S)
        quit_done.set()
        stop_server()

    def watch_idle() -> None:
        while not idle_stop.wait(idle_check_s):
            if app.state.stopping:
                return
            silent = clock() - app.state.last_heartbeat
            if silent > idle_timeout_s and active_job_count() == 0:
                # Not forced: a job that started since the count wins.
                if quit_now(force=False):
                    log.info("no page open for %.0f s; stopped", silent)
                    return

    launch_at_start = home if home is not None and home.is_launch else None
    runs_from_cache = portable and _runs_from_cache()
    app.mount("/static", _RevalidatedStatic(directory=str(STATIC_DIR)), name="static")
    # Two FFmpeg jobs may run at once; identical requests share one lock so a
    # burst of scrubbing never launches duplicate decodes for the same frame.
    preview_slots = threading.BoundedSemaphore(2)
    preview_locks: weakref.WeakValueDictionary[Path, threading.Lock] = (
        weakref.WeakValueDictionary()
    )
    preview_locks_guard = threading.Lock()

    def preview_lock(path: Path) -> threading.Lock:
        with preview_locks_guard:
            return preview_locks.setdefault(path, threading.Lock())
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    templates.env.filters["duration"] = format_duration
    templates.env.filters["timestamp"] = format_timestamp
    templates.env.filters["hours"] = format_hours
    templates.env.filters["date"] = format_date
    templates.env.filters["finished"] = format_finished
    templates.env.filters["bytes"] = human_bytes
    templates.env.filters["platform"] = platform_label
    # Job pages link to a playlist by its key within the job's library.
    templates.env.globals["playlist_key"] = lib.key_for
    # The product name has one source of truth (ytop.product).
    templates.env.globals["product_name"] = product.NAME

    # Loaded (compiled) now: it renders from memory while an app update
    # moves this app's own files, templates included, aside.
    app_update_status = templates.get_template("app_update_status.html")

    def library_name() -> str:
        ref = home_library()
        return ref.label if ref is not None else "library"

    version_label = f"{product.NAME} {ytop_version}" + (
        f" (build {ytop_build})" if ytop_build else ""
    )

    def chrome_context() -> dict:
        """What base.html shows on every page (the portable banner, the
        version in the footer)."""
        if not portable:
            return {"portable": False, "version_label": version_label}
        launch = next((ref for ref in libs.libraries() if ref.is_launch), None)
        label = (
            launch.label if launch is not None
            else launch_at_start.label if launch_at_start is not None
            else ""
        )
        return {
            "portable": True,
            "version_label": version_label,
            "launch_label": label,
            "launch_connected": launch is not None,
            "runs_from_cache": runs_from_cache,
        }

    def render(request: Request, template: str, **context):
        switch_offer = app.state.switch_offer
        if not switch_offer and request.query_params.get("offer_switch") == "1":
            switch_offer = offer_label(request.query_params.get("offer_from"))
        base = {
            "request": request,
            "q": "",
            "library_name": library_name(),
            "playlist_count": len(lib.playlist_dirs()),
            "multi_library": len(libs.libraries()) > 1,
            "live_job": None,
            "job_by_playlist": {},
            "csrf_token": csrf_token,
            "switch_offer": switch_offer,
            # The one-time question and the offer (any page).
            "ask_app_updates": releases.asking(),
            "app_offer": releases.offer if releases.phase in ("idle", "failed") else None,
            **chrome_context(),
        }
        base.update(context)
        return templates.TemplateResponse(request, template, base)

    def storage_context() -> dict:
        """Only pages with a destination picker pay for library discovery."""
        return {
            "libraries": [ref for ref in libs.libraries() if not ref.read_only],
            "default_library": home_library(),
        }

    def refuse_ambiguous(key: str) -> None:
        """Write routes never guess: a bare id that a second library now
        holds too (a page loaded before it was plugged in) is refused."""
        if lib.is_ambiguous(key):
            raise HTTPException(status_code=409, detail=AMBIGUOUS_MESSAGE)

    def require_idle(playlist_id: str, action: str) -> None:
        """A running download caches the playlist's folder and names; editing
        them underneath it would be reverted or collide."""
        if active_job_for(playlist_id) is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Cancel the download before {action}.",
            )

    def active_job_in(library_id: str, playlist_id: str, jobs=None):
        """The active job for one playlist of one library, if any."""
        for job in jobs if jobs is not None else app.state.jobs.list():
            if job.active and job.playlist_id == playlist_id and (
                job.library_id in ("", library_id)
                or (job.kind == "move" and job.source_library_id == library_id)
            ):
                return job
        return None

    def active_job_for(key: str):
        resolved = lib.resolve_playlist(key)
        if resolved is None:
            return None
        return active_job_in(resolved[0].id, str(resolved[2].get("id") or ""))

    def job_json(job) -> dict:
        """Serialize a job plus the derived values the browser updates with."""
        payload = asdict(job)
        payload["active"] = job.active
        payload["percent"] = job.percent
        return payload

    def not_found(request: Request, message: str = "Not found",
                  status_code: int = 404):
        return templates.TemplateResponse(
            request,
            "not_found.html",
            {
                "request": request,
                "q": "",
                "library_name": library_name(),
                "playlist_count": len(lib.playlist_dirs()),
                "multi_library": False,
                "message": message,
                "csrf_token": csrf_token,
                **chrome_context(),
            },
            status_code=status_code,
        )

    # -- pages -------------------------------------------------------------
    @app.get("/")
    def library_page(
        request: Request,
        library: str = Query(default="", max_length=100),
    ):
        refs = libs.libraries()
        active = library if any(ref.id == library for ref in refs) else ""
        view = build_home(lib, active or None)
        jobs = sorted(app.state.jobs.list(), key=lambda item: item.created_at)
        job_by_playlist = {}
        for card in view.all_playlists:
            job = active_job_in(card.library_id, card.playlist_id, jobs)
            if job is not None:
                job_by_playlist[card.key] = job
        live_job = next(
            (job for job in jobs
             if job.active and job.status in {"queued", "running"}),
            None,
        )
        return render(
            request,
            "library.html",
            view=view,
            switcher=refs if len(refs) > 1 else [],
            active_library=active,
            several=len(view.summaries) > 1,
            job_by_playlist=job_by_playlist,
            live_job=live_job,
        )

    @app.get("/videos")
    def videos_page(
        request: Request,
        library: str = Query(default="", max_length=100),
        tag: str = Query(default="", max_length=200),
        category: str = Query(default="", max_length=200),
        channel: str = Query(default="", max_length=200),
    ):
        refs = libs.libraries()
        active = library if any(ref.id == library for ref in refs) else ""
        groups = build_videos(
            lib, library_id=active or None, tag=tag, category=category,
            channel=channel,
        )
        if tag:
            label = next(
                (t.label for _card, videos in groups for v in videos
                 for t in v.tags if t.key == tag),
                tag,
            )
            heading = f"Videos tagged “{label}”"
        elif category:
            heading = f"Videos in “{category}”"
        elif channel:
            heading = f"Videos from “{channel}”"
        else:
            heading = "All videos"
        library_label = next((ref.label for ref in refs if ref.id == active), "")
        video_total = sum(len(videos) for _card, videos in groups)
        shown_total, playlist_total = video_total, len(groups)
        if not (tag or category or channel) and video_total > UNFILTERED_VIDEO_LIMIT:
            # Unfiltered, a big library would be one enormous page.
            capped, room = [], UNFILTERED_VIDEO_LIMIT
            for card, videos in groups:
                if room <= 0:
                    break
                capped.append((card, videos[:room]))
                room -= len(videos[:room])
            groups, shown_total = capped, UNFILTERED_VIDEO_LIMIT
        return render(
            request,
            "videos.html",
            groups=groups,
            heading=heading,
            active_library=active,
            library_label=library_label,
            video_total=video_total,
            shown_total=shown_total,
            playlist_total=playlist_total,
        )

    @app.get("/playlist/new")
    def new_playlist_page(request: Request):
        return render(
            request,
            "new_playlist.html",
            videos=library_videos(lib),
            error="",
            **storage_context(),
        )

    @app.post("/playlist/new")
    def new_playlist_submit(request: Request, form: FormData = Depends(form_data)):
        title = str(form.get("title") or "").strip()
        selections: list[tuple[str, str]] = []
        for raw in form.getlist("video"):
            playlist_id, _, video_id = str(raw).partition(":")
            if playlist_id and video_id:
                selections.append((playlist_id, video_id))
        if not title:
            return render(
                request,
                "new_playlist.html",
                videos=library_videos(lib),
                error="Give the new playlist a title.",
                **storage_context(),
            )
        library_id = str(form.get("location") or "").strip()
        try:
            playlist_id = lib.create_playlist(
                title, selections, library_id=library_id or None
            )
        except ReadOnlyLibraryError:
            raise  # 409 from the handler, not a form error
        except (OSError, ValueError) as exc:
            return render(
                request,
                "new_playlist.html",
                videos=library_videos(lib),
                error=f"Could not create the playlist: {exc}",
                **storage_context(),
            )
        if playlist_id is None:
            return render(
                request,
                "new_playlist.html",
                videos=library_videos(lib),
                error="That playlist title is not usable.",
                **storage_context(),
            )
        return RedirectResponse(f"/playlist/{playlist_id}", status_code=303)

    @app.get("/playlist/{playlist_id}")
    def playlist_page(request: Request, playlist_id: str):
        playlist = lib.playlist_view(playlist_id)
        if playlist is None:
            return not_found(request, "That playlist is not in this library.")
        live_job = active_job_for(playlist_id)
        move_locations: list[LibraryRef] = [] if live_job is not None else [
            ref for ref in libs.libraries()
            if not ref.read_only and ref.id != playlist.library_id
        ]
        ref = libs.get(playlist.library_id)
        effective_tags: list = []
        effective_cat = ""
        vocabulary: list = []
        category_names: list[str] = []
        if ref is not None:
            index = index_for(ref.layout, read_only=ref.read_only)
            index.refresh_for_page()  # a tag added on disk since the last page
            derived = index.derived(page=True)
            edits = TagStore(ref.layout).read()
            pid = playlist.id
            row = derived.get(pid)
            effective_tags = apply_edits(pid, list(row.tags) if row else [], edits)
            effective_cat = effective_category(pid, row.category if row else "", edits)
            # Suggest every tag the library shows, as shown: derived and
            # user-added ones, renamed, without hidden ones.
            seen: dict = {}
            for other in set(derived) | set(edits.playlists):
                item = derived.get(other)
                for tag in apply_edits(other, list(item.tags) if item else [], edits):
                    seen.setdefault(tag.key, tag)
            vocabulary = sorted(seen.values(), key=lambda tag: tag.label.casefold())
            category_names = sorted(
                {effective_category(k, d.category, edits) for k, d in derived.items()} - {""}
            )
        return render(
            request,
            "playlist.html",
            playlist=playlist,
            live_job=live_job,
            move_locations=move_locations,
            effective_tags=effective_tags,
            effective_category=effective_cat,
            tag_vocabulary=vocabulary,
            category_names=category_names,
            can_edit_tags=ref is not None and _tags_writable(ref),
            proxies_set=bool(update_root is not None and load_config(update_root).proxies),
            age_gate_set=_age_gate_set(),
            upgrade_reads=(
                playlist.upgrade_reads(UPGRADE_HEIGHTS)
                if playlist.kind == "youtube" and live_job is None else []
            ),
        )

    @app.post("/playlist/{playlist_id}/upgrade")
    def playlist_upgrade(playlist_id: str, form: FormData = Depends(form_data)):
        """Start an upgrade pass (#54): a review of only the videos stored
        below the chosen height, then the same download as a sync."""
        playlist = lib.playlist_view(playlist_id)
        if playlist is None or playlist.kind != "youtube":
            raise HTTPException(status_code=404, detail="playlist not found")
        try:
            height = int(str(form.get("height") or ""))
            job = app.state.jobs.create_probe(
                f"https://www.youtube.com/playlist?list={playlist.id}",
                library_id=playlist.library_id,
                upgrade_height=height,
            )
        except ReadOnlyLibraryError:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(f"/add/{job.id}", status_code=303)

    @app.post("/playlist/{playlist_id}/title")
    def playlist_title(
        request: Request,
        playlist_id: str,
        form: FormData = Depends(form_data),
    ):
        refuse_ambiguous(playlist_id)
        if lib.resolve_playlist(playlist_id) is None:
            raise HTTPException(status_code=404, detail="playlist not found")
        require_idle(playlist_id, "renaming this playlist")
        lib.set_playlist_title(playlist_id, str(form.get("title") or ""))
        return RedirectResponse(f"/playlist/{playlist_id}", status_code=303)

    def tag_store_for(ref: LibraryRef) -> TagStore:
        if ref.read_only:
            raise HTTPException(status_code=409, detail=TAGS_READ_ONLY_MESSAGE)
        return TagStore(ref.layout)

    def edit_tags(change: Callable[[], None]) -> None:
        # Every tag edit is idempotent and saved atomically, so a refused
        # attempt can simply be repeated.
        for pause in (*TAG_EDIT_RETRY_S, None):
            try:
                change()
                return
            except CorruptDocumentError as exc:
                raise HTTPException(
                    status_code=409, detail=TAGS_DAMAGED_MESSAGE
                ) from exc
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EPERM) and pause is not None:
                    # Perhaps a brief lock (Windows), not a locked stick.
                    time.sleep(pause)
                    continue
                # A locked stick, a read-only mount or a folder without
                # write permission: the same answer as a newer-schema
                # library.
                if exc.errno in (errno.EROFS, errno.EACCES, errno.EPERM):
                    raise HTTPException(
                        status_code=409, detail=TAGS_READ_ONLY_MESSAGE
                    ) from exc
                raise

    def tag_target(playlist_id: str) -> tuple[LibraryRef, str]:
        refuse_ambiguous(playlist_id)
        resolved = lib.resolve_playlist(playlist_id)
        if resolved is None:
            raise HTTPException(status_code=404, detail="playlist not found")
        ref, _meta_dir, doc = resolved
        return ref, str(doc.get("id") or "")

    @app.post("/playlist/{playlist_id}/tags")
    def playlist_tags(
        request: Request,
        playlist_id: str,
        form: FormData = Depends(form_data),
    ):
        ref, pid = tag_target(playlist_id)
        action = str(form.get("action") or "")
        tag = str(form.get("tag") or "")
        # A malformed request is refused as such, whatever the library.
        if action not in ("add", "remove"):
            raise HTTPException(status_code=400, detail="unknown tag action")
        store = tag_store_for(ref)
        if action == "add":
            edit_tags(lambda: store.add(pid, tag))
        else:
            edit_tags(lambda: store.remove(pid, tag))
        return RedirectResponse(f"/playlist/{playlist_id}", status_code=303)

    @app.post("/playlist/{playlist_id}/category")
    def playlist_category(
        request: Request,
        playlist_id: str,
        form: FormData = Depends(form_data),
    ):
        ref, pid = tag_target(playlist_id)
        store = tag_store_for(ref)
        category = str(form.get("category") or "").strip()
        edit_tags(lambda: store.set_category(pid, category or None))
        return RedirectResponse(f"/playlist/{playlist_id}", status_code=303)

    @app.get("/tags")
    def tags_page(
        request: Request,
        library: str = Query(default="", max_length=100),
    ):
        ref = libs.get(library) or home_library()
        if ref is None:
            return not_found(request, "No library is connected.", status_code=409)
        counts: dict[str, int] = {}
        labels: dict[str, str] = {}
        for card in cards_for(lib, ref.id):
            for tag in card.tags:
                counts[tag.key] = counts.get(tag.key, 0) + 1
                labels.setdefault(tag.key, tag.label)
        tags = sorted(
            ((key, labels[key], count) for key, count in counts.items()),
            key=lambda row: row[1].casefold(),
        )
        vocabulary = index_for(ref.layout, read_only=ref.read_only).vocabulary(page=True)
        edits = TagStore(ref.layout).read()
        # A tag's own name before its rename: derived, or as the user added it.
        names = {key: tag.label for key, tag in vocabulary.items()}
        for row in edits.playlists.values():
            for added in row.add:
                names.setdefault(normalize_tag(added), added)
        # A hidden tag by the name it was shown with: a rename's label, else
        # its own (a user-added tag has no derived label).
        shown = {normalize_tag(label): label for label in edits.renames.values()}
        hidden = [
            (key, shown.get(key) or names.get(key, key))
            for key in sorted(edits.hidden)
        ]
        renamed = [
            (key, names.get(key, key), label)
            for key, label in sorted(edits.renames.items())
        ]
        return render(
            request,
            "tags.html",
            tag_library=ref,
            libraries_all=libs.libraries(),
            tags=tags,
            hidden_tags=hidden,
            renamed_tags=renamed,
            can_edit_tags=_tags_writable(ref),
        )

    @app.post("/tags/{library_id}/{action}")
    def tags_edit(
        request: Request,
        library_id: str,
        action: str,
        form: FormData = Depends(form_data),
    ):
        if action not in ("rename", "hide", "unhide"):
            raise HTTPException(status_code=404)
        ref = libs.get(library_id)
        if ref is None:
            raise HTTPException(status_code=404, detail="library not found")
        store = tag_store_for(ref)
        key = str(form.get("key") or "")
        if action == "rename":
            label = str(form.get("label") or "")
            edit_tags(lambda: store.rename(normalize_tag(key), label))
        elif action == "hide":
            edit_tags(lambda: store.hide(key))
        else:
            edit_tags(lambda: store.unhide(key))
        return RedirectResponse(f"/tags?library={library_id}", status_code=303)

    @app.post("/playlist/{playlist_id}/videos/{video_id}/title")
    def video_title(
        request: Request,
        playlist_id: str,
        video_id: str,
        form: FormData = Depends(form_data),
    ):
        refuse_ambiguous(playlist_id)
        video = lib.video_view(playlist_id, video_id)
        if video is None:
            raise HTTPException(status_code=404, detail="video not found")
        # The new title renames the video in every playlist that holds it.
        for ref, held_in in lib.playlists_holding(playlist_id, video_id):
            if active_job_in(ref.id, held_in) is not None:
                raise HTTPException(
                    status_code=409,
                    detail="Cancel the download before renaming this video; "
                           "a playlist holding it is downloading.",
                )
        lib.set_video_title(playlist_id, video_id, str(form.get("title") or ""))
        return RedirectResponse(
            f"/watch/{playlist_id}/{video_id}", status_code=303
        )

    @app.post("/playlist/{playlist_id}/delete")
    def playlist_delete(playlist_id: str):
        refuse_ambiguous(playlist_id)
        job = active_job_for(playlist_id)
        if job is not None:
            raise HTTPException(
                status_code=409,
                detail="Cancel the download before removing this playlist.",
            )
        if not lib.remove_playlist(playlist_id):
            raise HTTPException(status_code=404, detail="playlist not found")
        return RedirectResponse("/", status_code=303)

    @app.post("/playlist/{playlist_id}/videos/{video_id}/remove")
    def video_remove(
        request: Request,
        playlist_id: str,
        video_id: str,
        form: FormData = Depends(form_data),
    ):
        refuse_ambiguous(playlist_id)
        job = active_job_for(playlist_id)
        if job is not None:
            raise HTTPException(
                status_code=409,
                detail="Cancel the download before editing this playlist.",
            )
        if not lib.remove_video(playlist_id, video_id):
            raise HTTPException(status_code=404, detail="video not found")
        return RedirectResponse(f"/playlist/{playlist_id}", status_code=303)

    @app.post("/playlist/{playlist_id}/reorder")
    def playlist_reorder(
        request: Request,
        playlist_id: str,
        form: FormData = Depends(form_data),
    ):
        refuse_ambiguous(playlist_id)
        require_idle(playlist_id, "reordering this playlist")
        video_id = str(form.get("video_id") or "")
        direction = str(form.get("direction") or "")
        order = str(form.get("order") or "")
        if order:
            changed = lib.set_order(
                playlist_id,
                [raw for raw in order.split(",") if raw],
            )
        elif video_id and direction in {"up", "down"}:
            changed = lib.reorder_video(playlist_id, video_id, direction)
        else:
            raise HTTPException(status_code=400, detail="nothing to reorder")
        if not changed:
            raise HTTPException(
                status_code=409, detail="the playlist order could not be saved"
            )
        return RedirectResponse(f"/playlist/{playlist_id}", status_code=303)

    @app.post("/playlist/{playlist_id}/move")
    def playlist_move(
        request: Request,
        playlist_id: str,
        form: FormData = Depends(form_data),
    ):
        refuse_ambiguous(playlist_id)
        if active_job_for(playlist_id) is not None:
            raise HTTPException(
                status_code=409,
                detail="Finish or cancel the active job for this playlist first.",
            )
        location_id = str(form.get("location") or "").strip()
        if not location_id:
            raise HTTPException(
                status_code=400, detail="choose a destination location"
            )
        try:
            job = app.state.jobs.move_playlist(playlist_id, location_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="playlist not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(f"/move/{job.id}", status_code=303)

    @app.post("/playlist/{playlist_id}/options")
    def playlist_options(
        request: Request,
        playlist_id: str,
        form: FormData = Depends(form_data),
    ):
        refuse_ambiguous(playlist_id)
        if lib.resolve_playlist(playlist_id) is None:
            raise HTTPException(status_code=404, detail="playlist not found")
        if active_job_for(playlist_id) is not None:
            raise HTTPException(
                status_code=409,
                detail="Cancel the download before changing playlist options.",
            )
        lib.set_preserve_audio(
            playlist_id, str(form.get("preserve_audio") or "") == "1"
        )
        return RedirectResponse(f"/playlist/{playlist_id}", status_code=303)

    @app.get("/watch/{playlist_id}/{video_id}")
    def watch_page(
        request: Request,
        playlist_id: str,
        video_id: str,
        t: float | None = Query(default=None, ge=0),
        autoplay: int = Query(default=0, ge=0, le=1),
    ):
        video = lib.video_view(playlist_id, video_id)
        if video is None:
            return not_found(request, "That video is not in this playlist.")

        duration = video.duration or 0.0
        saved = video.progress.position
        start_at = 0.0
        if t is not None:
            start_at = min(float(t), duration) if duration else float(t)
        elif saved and duration and 5 < saved < duration - 5:
            start_at = saved

        watch_data = {
            "videoId": video.entry.id,
            "playlistId": video.playlist.key,
            "title": video.title,
            "duration": video.duration,
            "savedPosition": saved,
            "watched": video.progress.watched,
            "startAt": start_at,
            "autoplay": bool(autoplay),
            "progressUrl": "/api/progress",
            "previewBase": (
                f"/media/{playlist_id}/{video_id}/preview?t="
            ),
            "posterUrl": video.thumb_url,
            "nextUrl": (
                f"/watch/{playlist_id}/{video.next_entry.id}"
                if video.next_entry else None
            ),
            "prevUrl": (
                f"/watch/{playlist_id}/{video.prev_entry.id}"
                if video.prev_entry else None
            ),
            "chapters": [
                {"start": chapter.start, "end": chapter.end,
                 "title": chapter.title}
                for chapter in video.chapters
            ],
            # The transcript is rendered once in the page; captions read their
            # timings from that DOM instead of duplicating every segment here.
            "audioTracks": video.audio_tracks,
        }
        payload = json.dumps(watch_data, ensure_ascii=False).replace(
            "</", "<\\/"
        )
        return render(
            request,
            "watch.html",
            video=video,
            playlist=video.playlist,
            watch_json=payload,
            autoplay=bool(autoplay),
            autoplay_param=1 if autoplay else 0,
        )

    @app.get("/search")
    def search_page(
        request: Request,
        q: str = Query(default="", max_length=200),
        playlist: str | None = Query(default=None, max_length=100),
    ):
        groups = search_library(lib, q, playlist=playlist) if q.strip() else []
        return render(
            request,
            "search.html",
            q=q,
            playlist_id=playlist,
            groups=groups,
            hit_count=sum(
                len(g.video_hits) + len(g.transcript_hits) + len(g.tag_matches)
                + (1 if g.playlist_matched else 0)
                for g in groups
            ),
            searched=bool(q.strip()),
        )

    PLAYLIST_LINK_NOTICE = (
        "That was a playlist link, so the whole playlist is being added."
    )

    # -- add / downloads ---------------------------------------------------
    @app.get("/add")
    def add_page(request: Request):
        return render(request, "add.html", job=None, **storage_context())

    @app.post("/add")
    def add_submit(request: Request, form: FormData = Depends(form_data)):
        url = str(form.get("url") or "").strip()
        if not url:
            return not_found(request, "Paste a playlist or video URL.", status_code=400)
        if len(url) > 500:
            return not_found(request, "That URL is too long.", status_code=400)
        preserve = str(form.get("preserve_audio") or "") == "1"
        location_id = str(form.get("location") or "").strip()
        try:
            target = parse_target(url)
        except TargetError as exc:
            return not_found(request, str(exc), status_code=400)
        try:
            if target.kind == "video":
                job = app.state.jobs.create_video_probe(
                    target.id, preserve_audio=preserve,
                    library_id=location_id or None,
                )
            else:
                job = start_playlist_probe(url, preserve, location_id or None)
        except Exception as exc:  # noqa: BLE001 - shown on the page
            return not_found(request, f"Could not start the probe: {exc}",
                             status_code=400)
        return RedirectResponse(f"/add/{job.id}", status_code=303)

    def start_playlist_probe(url: str, preserve: bool, library_id: str | None):
        return app.state.jobs.create_probe(
            url,
            preserve_audio=(True if preserve else None),
            library_id=library_id,
        )

    # -- storage -----------------------------------------------------------
    def active_storage_ids() -> set[str]:
        ids = set()
        for job in app.state.jobs.list():
            if not job.active:
                continue
            ids.update(
                library_id for library_id in (job.library_id, job.source_library_id)
                if library_id
            )
            if job.kind == "make_stick" and job.target_path:
                # An upgrade writes into the target library too.
                target = libs.ref_for_path(Path(job.target_path))
                if target is not None:
                    ids.add(target.id)
        return ids

    def unplug_states(refs: list[LibraryRef], active: set[str]) -> dict[str, str]:
        """Per library: ``safe``, ``busy`` (a job or pending journal) or
        ``quit`` (the stick this process runs from)."""
        needs_quit = portable and _unplug_needs_quit()
        states = {}
        for ref in refs:
            if ref.id in active or _pending_journal(ref):
                states[ref.id] = "busy"
            elif ref.is_launch and needs_quit:
                states[ref.id] = "quit"
            else:
                states[ref.id] = "safe"
        return states

    def _render_storage(request: Request, error: str = "", refresh: bool = False):
        refs = libs.libraries(refresh=refresh)
        counts, storage_total = storage_usage(lib)
        active = active_storage_ids()
        return render(
            request,
            "storage.html",
            libraries=refs,
            playlist_counts=counts,
            storage_total=storage_total,
            refresh=refresh,
            error=error,
            active_libraries=active,
            unplug=unplug_states(refs, active),
            components=auto.status(),
            app_updates=releases.status(),
            update_running=any(
                job.active and job.kind == "update" for job in app.state.jobs.list()
            ),
        )

    @app.get("/storage")
    def storage_page(
        request: Request,
        refresh: int = Query(default=0, ge=0, le=1),
    ):
        return _render_storage(request, refresh=bool(refresh))

    @app.post("/storage/add")
    def storage_add(request: Request, form: FormData = Depends(form_data)):
        path = str(form.get("path") or "").strip()
        label = str(form.get("label") or "").strip()
        if not path:
            return _render_storage(request, "Enter the folder for the new library.")
        if len(path) > 1000:
            return _render_storage(request, "That path is too long.")
        try:
            libs.add_folder(Path(path), label)
        except (OSError, ValueError) as exc:
            return _render_storage(request, str(exc))
        return RedirectResponse("/storage", status_code=303)

    @app.post("/storage/{library_id}/label")
    def storage_label(
        request: Request,
        library_id: str,
        form: FormData = Depends(form_data),
    ):
        try:
            libs.set_info(
                library_id,
                label=str(form.get("label") or ""),
                description=str(form.get("description") or ""),
            )
        except LookupError as exc:
            raise HTTPException(
                status_code=404, detail="That library is not connected."
            ) from exc
        return RedirectResponse(
            _safe_next_url(str(form.get("next") or ""), default="/storage"),
            status_code=303,
        )

    @app.post("/storage/{library_id}/remove")
    def storage_remove(library_id: str):
        if library_id in active_storage_ids():
            raise HTTPException(
                status_code=409,
                detail="A download or move is using that library. "
                       "Wait for it to finish before forgetting it.",
            )
        if not libs.forget(library_id):
            raise HTTPException(
                status_code=409,
                detail="Only libraries added on this computer can be forgotten.",
            )
        return RedirectResponse("/storage", status_code=303)

    # -- settings ----------------------------------------------------------
    def _render_settings(request: Request, error: str = "", status_code: int = 200):
        home_ref = home_library()
        have = adds.platforms()
        build: dict = {}
        if adds.ytop is not None:
            try:
                build = json.loads((adds.ytop / "build.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                build = {}
        sizes = build.get("sizes") if isinstance(build.get("sizes"), dict) else {}
        response = render(
            request,
            "settings.html",
            error=error,
            stick=adds.available,
            stick_ref=home_ref,
            platforms=[
                {"key": plat, "label": platform_label(plat),
                 "size": int(sizes.get(plat) or 0)}
                for plat in have
            ],
            missing_platforms=[
                {"key": plat, "label": platform_label(plat)}
                for plat in PLATFORMS if plat not in have
            ],
            adding=adds.status(),
            components=auto.status(),
            app_updates=releases.status(),
            update_running=any(
                job.active and job.kind == "update" for job in app.state.jobs.list()
            ),
            download_config=load_config(update_root) if update_root is not None else None,
            download_library=home_ref,
            # Explicit profiles only: a bare browser name means its Default
            # profile, usually the owner's own account.
            age_gate_choices=firefox_profiles(),
            js_runtime_missing=js_runtime() is None,
            proxy_servers_text=(format_servers(load_config(update_root).proxies)
                                if update_root is not None else ""),
            height_choices=[0, 480, 720, 1080, 1440, 2160],
            floor_choices=[0, 240, 360, 480],
        )
        response.status_code = status_code
        return response

    @app.get("/settings")
    def settings_page(request: Request):
        return _render_settings(request)

    @app.post("/settings/platforms")
    def settings_add_platforms(request: Request, form: FormData = Depends(form_data)):
        wanted = [str(value) for value in form.getlist("platforms")]
        try:
            adds.start(wanted)
        except ValueError as exc:
            return _render_settings(request, str(exc), status_code=409)
        return RedirectResponse("/settings#this-stick", status_code=303)

    @app.post("/settings/downloads")
    def settings_downloads(request: Request, form: FormData = Depends(form_data)):
        if update_root is None:
            raise HTTPException(status_code=409, detail="No library is connected.")

        def number(name: str, kind: type, default: float) -> float:
            raw = str(form.get(name) or "").strip()
            try:
                return kind(raw) if raw else default
            except ValueError:
                raise ValueError(f"{name.replace('_', ' ')}: {raw!r} is not a number") from None

        try:
            current = load_config(update_root)
            updated = replace(
                current,
                target_mb_per_hour=float(number("target_mb_per_hour", float,
                                                current.target_mb_per_hour)),
                max_height=int(number("max_height", int, 0)),
                min_height=int(number("min_height", int, 0)),
                jobs=int(number("jobs", int, current.jobs)),
                audio_language=str(form.get("audio_language") or "").strip()[:20],
                quick_analysis=form.get("quick_analysis") == "1",
            )
            if not 1 <= updated.jobs <= 4:
                raise ValueError("parallel downloads must be between 1 and 4")
            save_config(update_root, updated)
        except (OSError, ValueError) as exc:
            return _render_settings(request, f"Could not save the settings: {exc}",
                                    status_code=400)
        return RedirectResponse("/settings#downloads", status_code=303)

    @app.post("/settings/proxies")
    def settings_proxies(request: Request, form: FormData = Depends(form_data)):
        if update_root is None:
            raise HTTPException(status_code=409, detail="No library is connected.")
        for field_name in ("servers", "username", "password"):
            # Never echo the value: it may be the password.
            pattern = _CONTROL_CHARS_MULTILINE if field_name == "servers" else _CONTROL_CHARS
            if pattern.search(str(form.get(field_name) or "")):
                return _render_settings(
                    request,
                    f"Could not save the proxies: the {field_name} contains "
                    "a control character.",
                    status_code=400,
                )
        try:
            current = load_config(update_root)
            password = str(form.get("password") or "")
            if form.get("clear_password") == "1":
                password = ""
            elif not password:
                password = current.proxy_password
            updated = replace(
                current,
                proxies=parse_servers(str(form.get("servers") or ""), current.proxies),
                proxy_username=str(form.get("username") or "").strip(),
                proxy_password=password,
            )
            save_config(update_root, updated)
        except (OSError, ValueError) as exc:
            return _render_settings(request, f"Could not save the proxies: {redact(str(exc))}",
                                    status_code=400)
        return RedirectResponse("/settings#proxies", status_code=303)

    @app.post("/settings/age-gate")
    def settings_age_gate(request: Request, form: FormData = Depends(form_data)):
        if update_root is None:
            raise HTTPException(status_code=409, detail="No library is connected.")
        raw = str(form.get("browser") or "")
        if _CONTROL_CHARS.search(raw):
            return _render_settings(
                request,
                "Could not save the sign-in: the browser contains a control character.",
                status_code=400,
            )
        try:
            # Saving an empty field clears the sign-in, as Clear does.
            spec = "" if form.get("clear") == "1" or not raw.strip() else check_browser_spec(raw)
            save_config(update_root, replace(load_config(update_root), age_gate_browser=spec))
        except (OSError, ValueError) as exc:
            return _render_settings(request, f"Could not save the sign-in: {exc}",
                                    status_code=400)
        return RedirectResponse("/settings#age-gate", status_code=303)

    @app.post("/storage/components/update")
    def components_update():
        try:
            app.state.jobs.start_update()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse("/downloads", status_code=303)

    @app.post("/storage/components/settings")
    def components_settings(request: Request, form: FormData = Depends(form_data)):
        if update_root is None:
            raise HTTPException(status_code=409, detail="No library is connected.")
        try:
            set_updates_option(
                update_root, "check_components", form.get("check_components") == "1"
            )
        except (OSError, ValueError) as exc:
            return _render_settings(request, f"Could not save the setting: {exc}")
        return RedirectResponse("/settings", status_code=303)

    # -- app updates -------------------------------------------------------
    @app.post("/app-update/answer")
    def app_update_answer(request: Request, form: FormData = Depends(form_data)):
        yes = str(form.get("answer") or "") == "yes"
        try:
            releases.answer(yes)
        except (OSError, ValueError) as exc:
            raise HTTPException(
                status_code=409, detail=f"Could not save the answer: {exc}"
            ) from exc
        return RedirectResponse(
            _safe_next_url(str(form.get("next") or ""), "/"), status_code=303
        )

    @app.post("/storage/app/settings")
    def app_update_settings(request: Request, form: FormData = Depends(form_data)):
        if update_root is None:
            raise HTTPException(status_code=409, detail="No library is connected.")
        try:
            set_updates_option(update_root, "check_app", form.get("check_app") == "1")
        except (OSError, ValueError) as exc:
            return _render_settings(request, f"Could not save the setting: {exc}")
        return RedirectResponse("/settings", status_code=303)

    @app.post("/app-update/check")
    def app_update_check():
        try:
            releases.check()
        except ValueError as exc:  # installing
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse("/app-update", status_code=303)

    @app.get("/app-update")
    def app_update_page(request: Request):
        if releases.busy:
            page = app_update_status.render(
                product_name=product.NAME, phase=releases.phase,
                message=releases.message, progress=releases.progress,
                version=releases.target_version, kind=releases.kind,
            )
            if releases.finished:
                releases.shown.set()  # the result is on screen: the app may stop
            return Response(page, media_type="text/html")
        status = releases.status()
        response = render(request, "app_update.html", updates=status)
        if releases.finished:
            releases.shown.set()  # the result is on screen: the app may stop
        return response

    @app.post("/app-update/install")
    def app_update_install(request: Request, form: FormData = Depends(form_data)):
        build_id = str(form.get("build_id") or "")
        if active_job_count() > 0:
            raise HTTPException(
                status_code=409,
                detail="Downloads are running; let them finish (or cancel them), "
                       "then install the update.",
            )
        try:
            releases.start_install(
                build_id, before_switch=stop_for_install, finished=install_finished,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse("/app-update", status_code=303)

    @app.post("/app-update/revert")
    def app_update_revert(request: Request, form: FormData = Depends(form_data)):
        build_id = str(form.get("build_id") or "")
        if active_job_count() > 0:
            raise HTTPException(
                status_code=409,
                detail="Downloads are running; let them finish (or cancel them), "
                       "then revert.",
            )
        try:
            releases.start_revert(
                build_id, before_switch=stop_for_install, finished=install_finished,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse("/app-update", status_code=303)

    # -- make a stick ------------------------------------------------------
    def _make_target(raw: str) -> Path:
        value = (raw or "").strip()
        if not value or len(value) > 1000:
            raise makestick.MakeStickError("choose the drive or folder for the stick")
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise makestick.MakeStickError("the stick needs an absolute folder path")
        return path

    def _render_make_stick(
        request: Request, raw_target: str = "", error: str = "",
        status_code: int = 200, platforms: list[str] | None = None,
    ):
        try:
            image: Path | None = makestick.source_image()
            build: dict | None = makestick.read_build(image)
            image_error = ""
        except makestick.MakeStickError as exc:
            image, build, image_error = None, None, str(exc)
        info = None
        candidates: list[makestick.TargetInfo] = []
        if raw_target:
            try:
                info = makestick.inspect_target(
                    _make_target(raw_target), source=image, cleanup=False
                )
            except makestick.MakeStickError as exc:
                error = error or str(exc)
            except OSError as exc:
                error = error or makestick.drive_problem(_make_target(raw_target), exc)
        if info is None:
            candidates = makestick.candidate_targets(
                libs, image, timeout=libs.probe_timeout
            )
        sizes = (build or {}).get("sizes") or {}
        playlists = (
            lib.playlists_and_bytes()[0]
            if info is not None and info.mode == "new" else []
        )
        available = [str(plat) for plat in (build or {}).get("platforms") or []]
        # Ticked platforms: the ones asked for, else (upgrading) the ones the
        # stick already starts on, else this computer's and Windows (more
        # can be added later from Settings), else all of them.  The upgrade
        # summary is counted for exactly these, not for every platform the
        # image has.
        checked = [plat for plat in available if plat in set(platforms or [])]
        if not checked and info is not None and info.mode == "upgrade":
            have = (info.image_build or {}).get("platforms") or []
            checked = [plat for plat in available if plat in set(map(str, have))]
        if not checked:
            try:
                here = current_platform()
            except UnsupportedPlatform:
                here = ""
            checked = [plat for plat in available if plat in (here, "windows-x86_64")]
        if not checked:
            checked = available
        upgrade_line = ""
        if info is not None and info.mode == "upgrade" and image is not None:
            try:
                upgrade_line = makestick.upgrade_summary(makestick.plan_make(
                    info.path, image, lib, mode="upgrade", platforms=checked,
                ))
            except (makestick.MakeStickError, OSError):
                upgrade_line = ""
        response = render(
            request,
            "make_stick.html",
            job=None,
            upgrade_line=upgrade_line,
            checked_platforms=checked,
            image_error=image_error,
            build=build,
            platform_sizes={
                plat: int(sizes.get(plat) or 0)
                for plat in (build or {}).get("platforms") or []
            },
            base_size=int(sizes.get("common") or 0) + int(sizes.get("app") or 0),
            target=info,
            raw_target=raw_target,
            candidates=candidates,
            playlists=playlists,
            error=error,
        )
        response.status_code = status_code
        return response

    @app.get("/make-stick")
    def make_stick_page(
        request: Request, target: str = Query(default="", max_length=1000),
        platforms: list[str] = Query(default=[]),
    ):
        return _render_make_stick(request, target, platforms=platforms)

    @app.post("/make-stick")
    def make_stick_submit(request: Request, form: FormData = Depends(form_data)):
        raw_target = str(form.get("target") or "").strip()
        platforms = [str(value) for value in form.getlist("platforms")]
        playlists = [str(value) for value in form.getlist("playlists")]
        label = str(form.get("label") or "")

        def plan():
            image = makestick.source_image()
            return image, makestick.plan_make(
                _make_target(raw_target), image, lib,
                platforms=platforms, playlists=playlists, label=label,
            )

        try:
            image, stick_plan = plan()
        except ValueError as exc:
            return _render_make_stick(
                request, raw_target, str(exc), 400, platforms=platforms,
            )
        except OSError as exc:
            return _render_make_stick(
                request, raw_target,
                makestick.drive_problem(_make_target(raw_target), exc), 400,
                platforms=platforms,
            )
        try:
            job = app.state.jobs.make_stick(stick_plan, image)
        except ValueError as exc:  # a busy target or playlist
            return _render_make_stick(
                request, raw_target, str(exc), 409, platforms=platforms,
            )
        return RedirectResponse(f"/make-stick/{job.id}", status_code=303)

    @app.get("/make-stick/{job_id}")
    def make_stick_status(request: Request, job_id: str):
        job = app.state.jobs.get(job_id)
        if job is None or job.kind != "make_stick":
            return not_found(request, "That stick job does not exist.")
        return render(request, "make_stick.html", job=job)

    @app.get("/add/video")
    def add_video_page(request: Request):
        return render(
            request,
            "add_video.html",
            playlists=cards_for(lib),
            selected=request.query_params.get("playlist", ""),
            error="",
            **storage_context(),
        )

    @app.post("/add/video")
    def add_video_submit(request: Request, form: FormData = Depends(form_data)):
        url = str(form.get("url") or "").strip()
        playlist_id = str(form.get("playlist") or "").strip()
        preserve = str(form.get("preserve_audio") or "") == "1"

        def reject(message: str):
            return render(
                request,
                "add_video.html",
                playlists=cards_for(lib),
                selected=playlist_id,
                error=message,
                **storage_context(),
            )

        if not url:
            return reject("Paste a single video URL or id.")
        if len(url) > 500:
            return reject("That URL is too long.")
        try:
            target = parse_target(url, prefer_video=True)
        except TargetError as exc:
            return reject(str(exc))
        # "library:<id>" picks that library's Un-grouped playlist.
        library_id = None
        if playlist_id.startswith("library:"):
            library_id, playlist_id = playlist_id[len("library:"):], ""
        if target.kind != "video":
            # A pure playlist link: add the whole playlist, as /add does.  A
            # chosen destination playlist can't hold it; say so instead of
            # dropping the choice silently, and keep to its library.
            notice = PLAYLIST_LINK_NOTICE
            if playlist_id:
                refuse_ambiguous(playlist_id)
                chosen = lib.resolve_playlist(playlist_id)
                if chosen is not None:
                    library_id = library_id or chosen[0].id
                    title = lib.playlist_titles().get(playlist_id, playlist_id)
                    notice = (
                        "That was a playlist link, so the whole playlist is "
                        f"being added as its own playlist, not into {title}."
                    )
            try:
                job = start_playlist_probe(
                    url, preserve,
                    library_id or str(form.get("location") or "").strip() or None,
                )
            except ReadOnlyLibraryError:
                raise
            except Exception as exc:  # noqa: BLE001 - shown on the page
                return reject(f"Could not start the probe: {exc}")
            app.state.jobs.set_notice(job.id, notice)
            return RedirectResponse(f"/add/{job.id}", status_code=303)
        elif playlist_id:
            refuse_ambiguous(playlist_id)
            if lib.resolve_playlist(playlist_id) is None:
                raise HTTPException(status_code=404, detail="playlist not found")
        try:
            job = app.state.jobs.create_video_probe(
                target.id,
                target_playlist_id=playlist_id,
                preserve_audio=preserve,
                library_id=library_id,
            )
        except ReadOnlyLibraryError:
            raise
        except Exception as exc:  # noqa: BLE001 - shown on the page
            return reject(f"Could not start the probe: {exc}")
        return RedirectResponse(f"/add/{job.id}", status_code=303)

    @app.get("/add/{job_id}")
    def add_status(request: Request, job_id: str):
        job = app.state.jobs.get(job_id)
        if job is None:
            return not_found(request, "That download job does not exist.")
        if job.kind == "move":
            return RedirectResponse(f"/move/{job_id}", status_code=303)
        if job.kind == "make_stick":
            return RedirectResponse(f"/make-stick/{job_id}", status_code=303)
        return render(request, "add.html", job=job, proxies_set=bool(
            update_root is not None and load_config(update_root).proxies),
            age_gate_set=_age_gate_set())

    @app.get("/move/{job_id}")
    def move_status(request: Request, job_id: str):
        job = app.state.jobs.get(job_id)
        if job is None or job.kind != "move":
            return not_found(request, "That move job does not exist.")
        return render(request, "move.html", job=job)

    @app.post("/jobs/{job_id}/download")
    def job_download(
        request: Request,
        job_id: str,
        form: FormData = Depends(form_data),
    ):
        policy = str(form.get("policy") or "efficient").strip()
        raw_preserve = form.get("preserve_audio")
        preserve = (
            None if raw_preserve is None
            else str(raw_preserve) == "1"
        )
        refresh = str(form.get("refresh_existing") or "") == "1"
        try:
            app.state.jobs.start_download(
                job_id, policy=policy, preserve_audio=preserve,
                refresh_existing=refresh,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(f"/add/{job_id}", status_code=303)

    def _job_action(form: FormData, job_id: str, action: str):
        next_url = _safe_next_url(str(form.get("next") or "/downloads"))
        try:
            if action == "cancel":
                app.state.jobs.cancel(job_id)
            else:
                app.state.jobs.remove(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(next_url, status_code=303)

    @app.post("/jobs/{job_id}/cancel")
    def job_cancel(job_id: str, form: FormData = Depends(form_data)):
        return _job_action(form, job_id, "cancel")

    @app.post("/jobs/{job_id}/remove")
    def job_remove(job_id: str, form: FormData = Depends(form_data)):
        return _job_action(form, job_id, "remove")

    @app.get("/downloads")
    def downloads_page(request: Request):
        # Stable sort keeps newest first inside each group while active work
        # stays at the top of the page.
        jobs = sorted(
            app.state.jobs.list(), key=lambda job: not job.active
        )
        return render(
            request,
            "downloads.html",
            jobs=jobs,
            active=any(job.active for job in jobs),
        )

    @app.get("/api/jobs")
    def jobs_api():
        return {"jobs": [job_json(job) for job in app.state.jobs.list()]}

    @app.get("/api/jobs/{job_id}")
    def job_api(job_id: str):
        job = app.state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return job_json(job)

    # -- json api ----------------------------------------------------------
    @app.get("/api/transcript/{playlist_id}/{video_id}")
    def transcript_api(playlist_id: str, video_id: str):
        video = lib.video_view(playlist_id, video_id)
        if video is None:
            raise HTTPException(status_code=404, detail="video not found")
        if video.transcript is None:
            return {"video_id": video_id, "transcript": None}
        return {
            "video_id": video_id,
            "language": video.transcript.get("language"),
            "kind": video.transcript.get("kind"),
            "segments": video.transcript_segments,
        }

    @app.post("/api/progress")
    def progress_api(update: ProgressUpdate):
        if not math.isfinite(update.position) or update.position < 0:
            raise HTTPException(status_code=422, detail="invalid position")
        if update.duration is not None and (
            not math.isfinite(update.duration) or update.duration < 0
        ):
            raise HTTPException(status_code=422, detail="invalid duration")
        if update.playlist_id:
            refuse_ambiguous(update.playlist_id)
        located = (
            lib.locate_video(update.playlist_id, update.video_id)
            if update.playlist_id
            else None
        ) or lib.locate_video(None, update.video_id)
        if located is None:
            raise HTTPException(status_code=404, detail="video not found")
        ref = located[0]
        ref.require_writable()
        progress = lib.progress_for(ref).update(
            update.video_id,
            update.position,
            duration=update.duration,
            watched=update.watched,
        )
        return {
            "ok": True,
            "video_id": update.video_id,
            "position": progress.position,
            "duration": progress.duration,
            "watched": progress.watched,
            "fraction": progress.fraction,
        }

    @app.post("/api/heartbeat", status_code=204)
    def heartbeat() -> Response:
        app.state.last_heartbeat = clock()
        return Response(status_code=204)

    @app.post("/api/quit")
    def quit_api(request: Request, form: FormData = Depends(form_data)):
        force = str(form.get("force") or "") == "1"
        # The Quit form posted without JavaScript carries its token as a
        # field; the script sends it as a header and wants JSON back.
        plain_form = "csrf_token" in form and "x-csrf-token" not in request.headers

        def busy(count: int):
            if plain_form:
                response = render(request, "quit_busy.html", active_jobs=count)
                response.status_code = 409
                return response
            return JSONResponse({"active_jobs": count}, status_code=409)

        count = active_job_count()
        if count and not force:
            return busy(count)
        # Rendered before anything stops: the page needs nothing afterwards.
        stopped = templates.get_template("stopped.html")
        page, saving = stopped.render(), stopped.render(saving=True)
        if not quit_now(force):
            return busy(max(active_job_count(), 1))
        # A flush already under way that outlasted the wait: not safe yet.
        done = quit_done.is_set()
        return Response(page if done else saving, media_type="text/html")

    @app.get("/api/instance", dependencies=[Depends(require_token)])
    def instance_status():
        version, build_id = app.state.instance_version
        return {
            "token_ok": True,
            "pid": os.getpid(),
            "version": version,
            "build_id": build_id,
            "active_jobs": active_job_count(),
            **_tool_paths(),
        }

    @app.post("/api/instance/rescan", dependencies=[Depends(require_token)],
              status_code=204)
    def instance_rescan(
        offer_from: str | None = Query(default=None, max_length=200),
    ) -> Response:
        libs.refresh()
        if offer_from is not None:  # a launcher with a newer YTOP
            app.state.switch_offer = offer_label(offer_from)
        return Response(status_code=204)

    @app.post("/api/instance/shutdown", dependencies=[Depends(require_token)])
    def instance_shutdown():
        busy = JSONResponse({"detail": "downloads are running"}, status_code=409)
        if active_job_count() > 0 or not quit_now(force=False, background=True):
            return busy
        return JSONResponse({"detail": "stopping"}, status_code=202)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "playlists": len(lib.playlist_dirs())}

    # -- media -------------------------------------------------------------
    @app.api_route(
        "/media/{playlist_id}/thumb", methods=["GET", "HEAD"]
    )
    def playlist_thumb(playlist_id: str):
        path = lib.playlist_thumbnail_path(playlist_id)
        if path is None:
            raise HTTPException(status_code=404)
        return _file_response(path, media_type="image/jpeg")

    @app.api_route(
        "/media/{playlist_id}/{video_id}/thumb", methods=["GET", "HEAD"]
    )
    def video_thumb(playlist_id: str, video_id: str):
        path = lib.thumbnail_path(playlist_id, video_id)
        if path is None:
            raise HTTPException(status_code=404)
        return _file_response(path, media_type="image/jpeg")

    @app.get("/media/{playlist_id}/{video_id}/preview")
    def video_preview(
        playlist_id: str,
        video_id: str,
        t: float = Query(default=0, ge=0),
    ):
        """A cached frame at ``t`` for the player's chapter scrubber."""
        if not math.isfinite(t) or t < 0:
            raise HTTPException(status_code=422, detail="invalid time")
        t = min(float(t), 24 * 3600.0)
        media = lib.media_path(playlist_id, video_id)
        if media is None:
            raise HTTPException(status_code=404, detail="media is missing")
        cache = lib.preview_cache_path(playlist_id, video_id, t)
        if cache is None:
            raise HTTPException(status_code=404, detail="preview is unavailable")
        if cache.is_file():
            return _file_response(
                cache, media_type="image/jpeg", cache="public, max-age=604800"
            )
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_name(cache.stem + ".tmp.jpg")
        with preview_lock(cache):
            if cache.is_file():
                return _file_response(
                    cache, media_type="image/jpeg",
                    cache="public, max-age=604800",
                )
            with preview_slots:
                # Re-check after waiting for a slot: another request may have
                # produced the frame while this one was queued.
                if cache.is_file():
                    return _file_response(
                        cache, media_type="image/jpeg",
                        cache="public, max-age=604800",
                    )
                try:
                    result = subprocess.run(
                        [
                            str(ffmpeg_path()), "-nostdin", "-loglevel", "error",
                            "-ss", f"{max(0.0, float(t)):.3f}",
                            "-i", str(media), "-frames:v", "1", "-an", "-sn",
                            "-vf", "scale=160:-2", "-q:v", "4", "-y", str(tmp),
                        ],
                        capture_output=True,
                        timeout=20,
                    )
                except (OSError, RuntimeError, subprocess.SubprocessError):
                    result = None
                if result is None or result.returncode != 0 or not tmp.is_file():
                    tmp.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=404, detail="preview is unavailable"
                    )
                tmp.replace(cache)
        return _file_response(
            cache, media_type="image/jpeg", cache="public, max-age=604800"
        )

    @app.api_route(
        "/media/{playlist_id}/{video_id}", methods=["GET", "HEAD"]
    )
    def media(playlist_id: str, video_id: str):
        path = lib.media_path(playlist_id, video_id)
        if path is None:
            raise HTTPException(status_code=404, detail="media is missing")
        return _file_response(path, media_type=media_kind(path),
                              cache="public, max-age=86400")

    @app.api_route(
        "/media/{playlist_id}/{video_id}/audio/{track_id}",
        methods=["GET", "HEAD"],
    )
    def audio_track(playlist_id: str, video_id: str, track_id: str):
        path = lib.audio_path(playlist_id, video_id, track_id)
        if path is None:
            raise HTTPException(status_code=404, detail="audio is missing")
        return _file_response(path, media_type=media_kind(path),
                              cache="public, max-age=86400")

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        message = (
            "That page is not in this library."
            if exc.status_code == 404 else str(exc.detail)
        )
        return not_found(request, message, status_code=exc.status_code)

    @app.exception_handler(ReadOnlyLibraryError)
    async def read_only_handler(request: Request, exc: ReadOnlyLibraryError):
        detail = str(exc)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": detail}, status_code=409)
        return not_found(request, detail, status_code=409)

    @app.exception_handler(CorruptDocumentError)
    async def corrupt_document_handler(request: Request, exc: CorruptDocumentError):
        # Refuse the write, say which file needs repair, and leave it untouched.
        detail = str(exc)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": detail}, status_code=409)
        return not_found(request, detail, status_code=409)

    return app


def _bind_first_free(host: str, preferred: int) -> socket.socket:
    """A listening socket on the first bindable port: ``preferred``,
    8766..8799, then any free one.  Binding here (not probing) means the port
    reported to ``on_bound`` is already ours."""
    candidates = [preferred] + [p for p in range(8766, 8800) if p != preferred] + [0]
    for candidate in candidates:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name == "nt":
            # Never share a port with another process on Windows.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, candidate))
            sock.listen(2048)
        except OSError:
            sock.close()
            continue
        return sock
    raise OSError("no free port")


def _run_server(
    app: FastAPI, host: str, port: int, sock: socket.socket | None = None
) -> None:
    """Run uvicorn, keeping the server on ``app.state.server`` so
    ``app.state.request_stop()`` can end it gracefully."""
    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(
            app, host=host, port=port, log_level="info",
            timeout_graceful_shutdown=5,
        )
    )
    app.state.server = server
    if getattr(app.state, "stop_requested", False):
        server.should_exit = True  # asked for before the server existed
    server.run(sockets=[sock] if sock is not None else None)


def _tool_paths() -> dict[str, str | None]:
    """The interpreter and tools this server runs (diagnostics; token-gated)."""

    def resolve(find: Callable[[], Path | None]) -> str | None:
        try:
            path = find()
        except RuntimeError:
            return None
        return str(path) if path is not None else None

    return {
        "python": sys.executable,
        "ffmpeg": resolve(ffmpeg_path),
        "ffprobe": resolve(ffprobe_path),
        "deno": resolve(deno_path),
        "toolchain_source": toolchain_source(),
    }


# ctypes console handlers stay referenced while installed (a collected
# callback would crash the console's handler thread).
_console_handlers: list[object] = []


def _install_console_close(
    app: FastAPI, stopped: threading.Event
) -> Callable[[], None]:
    """Make closing the console window stop YTOP the way Quit does (stop
    the jobs, flush and sync every library, release the instance file)
    instead of killing it mid-write.  Returns the undo function.

    POSIX terminals send SIGHUP when their window closes.  Windows calls a
    console control handler on its own thread and ends the process as soon
    as the handler returns (or ~5 s after a close), so for a close the
    handler waits for the server to finish.
    """

    def quit_in_background() -> None:
        threading.Thread(
            target=app.state.quit, name="ytop-console-quit", daemon=True
        ).start()

    if sys.platform == "win32":
        return _install_windows_ctrl_handler(quit_in_background, stopped)
    import signal

    sighup = getattr(signal, "SIGHUP", None)
    if sighup is None or threading.current_thread() is not threading.main_thread():
        return lambda: None
    previous = signal.getsignal(sighup)
    signal.signal(sighup, lambda _signum, _frame: quit_in_background())
    return lambda: signal.signal(sighup, previous)


def _install_windows_ctrl_handler(
    quit_in_background: Callable[[], None], stopped: threading.Event
) -> Callable[[], None]:
    if sys.platform != "win32":
        return lambda: None
    import ctypes
    from ctypes import wintypes

    ctrl_c, ctrl_break, ctrl_close = 0, 1, 2
    kernel32 = getattr(ctypes, "windll").kernel32
    handler_type = getattr(ctypes, "WINFUNCTYPE")(wintypes.BOOL, wintypes.DWORD)

    def on_ctrl(event: int) -> bool:
        if event not in (ctrl_c, ctrl_break, ctrl_close):
            return False
        quit_in_background()
        if event == ctrl_close:
            stopped.wait(4.5)  # Windows kills the process ~5 s after a close
        return True

    handler = handler_type(on_ctrl)
    if not kernel32.SetConsoleCtrlHandler(handler, True):
        return lambda: None
    _console_handlers.append(handler)

    def remove() -> None:
        kernel32.SetConsoleCtrlHandler(handler, False)
        _console_handlers.remove(handler)

    return remove


def serve(
    library: Path,
    config: Config | None = None,
    *,
    host: str | None = None,
    port: int | None = None,
    open_browser: bool | None = None,
    config_overrides: dict | None = None,
    portable: bool = False,
    on_bound: Callable[[int], None] | None = None,
    instance_token: str | None = None,
    on_shutdown: Callable[[], None] | None = None,
) -> None:
    """Run the web UI.

    ``portable`` binds 127.0.0.1 only and picks the first free port from the
    configured one, then 8766..8799, then an OS-chosen one.  ``on_bound`` is
    called with the port once it is bound (portable) or chosen, just before
    the server starts.  ``on_shutdown`` runs on lifespan shutdown.
    """
    startup = from_environment()
    startup.mark("loading YTOP")
    cfg = config or Config()
    sock: socket.socket | None = None
    stopped = threading.Event()
    uninstall: Callable[[], None] = lambda: None  # noqa: E731
    if portable:
        if host:
            raise ValueError("portable mode always binds 127.0.0.1")
        host = "127.0.0.1"
        sock = _bind_first_free(host, port or cfg.web.port)
        port = int(sock.getsockname()[1])
    host = host or cfg.web.host
    port = port if portable else (port or cfg.web.port)
    from ..config import WebConfig

    try:
        # Re-validate here too: ``serve`` is public and may be called directly.
        web = WebConfig(host=host, port=port)
        host, port = web.host, web.port
        should_open = cfg.web.open_browser if open_browser is None else open_browser
        root = Path(library).expanduser()
        if portable:
            # Deno (yt-dlp's JS runtime) must not cache on the host.
            use_library_deno_dir(root)
        try:
            prune_lock_sidecars()  # empty files left by earlier runs
            prune_legacy_lock_dir()  # and old builds' <tmp>/ytop-locks
        except OSError:
            log.warning("could not tidy the document lock dir", exc_info=True)
        app = create_app(
            root,
            libraries=LibrarySet(root, detect=cfg.detect_removable),
            config_overrides=config_overrides,
            instance_token=instance_token,
            portable=portable,
            startup=startup,
        )
        if on_shutdown is not None:
            app.state.shutdown_hooks.append(on_shutdown)

        url = f"http://{host}:{port}/"
        if portable:
            print(f"YTOP is running at {url}  "
                  "(close this window or use Quit in the app to stop)",
                  flush=True)
        else:
            print(f"ytop web UI: {url}", flush=True)
            print(f"library:     {Path(library).expanduser()}", flush=True)
        if on_bound is not None:
            on_bound(port)
        if should_open:
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        if portable:
            uninstall = _install_console_close(app, stopped)
        _run_server(app, host, port, sock)
    finally:
        stopped.set()
        uninstall()
        if sock is not None:
            sock.close()
