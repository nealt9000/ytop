"""Where yt-dlp runs: in this process, or in a child Python on chosen paths.

yt-dlp is imported once per process and cannot be swapped safely while the
app runs, so a component update only reaches this process at its next start.
To retry an item with the components just switched in (or to tell whether
the previous ones still work), the job sets a :class:`SubprocessRunner` for
the retry with :func:`using`: every yt-dlp call the probe and download code
makes then runs in a fresh child of the stick's own Python whose ``sys.path``
starts with those component folders.  Everything else (format choice,
publishing, playlist documents) stays in this process, unchanged.

The runner is a context variable: it follows the job's worker thread, and
:func:`spawn` carries it into the item pools that probe and download use.
"""

from __future__ import annotations

import contextvars
import json
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

EXTRACT_TIMEOUT = 300.0  # seconds; a download has no limit but cancel
_MARK = "\x1eYTOP-RUNNER "
# The progress fields yt-dlp's hook dictionaries carry that YTOP reads.
_HOOK_KEYS = ("status", "downloaded_bytes", "total_bytes", "total_bytes_estimate",
              "filename", "elapsed", "eta", "speed")

_CHILD = r"""
import json, sys
MARK = "\x1eYTOP-RUNNER "
args = json.loads(sys.stdin.readline())
sys.path[0:0] = args["paths"]

def out(doc):
    sys.stdout.write(MARK + json.dumps(doc, default=str) + "\n")
    sys.stdout.flush()

try:
    from yt_dlp import YoutubeDL
    opts = args["opts"]
    if isinstance(opts.get("cookiesfrombrowser"), list):
        opts["cookiesfrombrowser"] = tuple(opts["cookiesfrombrowser"])
    if args["op"] == "download":
        keys = args["hook_keys"]
        opts["progress_hooks"] = [lambda d: out({"hook": {k: d.get(k) for k in keys}})]
        with YoutubeDL(opts) as ydl:
            ydl.download([args["url"]])
        out({"result": None})
    else:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(args["url"], download=False)
            out({"result": ydl.sanitize_info(info)})
except BaseException as exc:
    out({"error": str(exc), "type": type(exc).__name__})
"""


class Runner(Protocol):
    def extract(self, url: str, opts: dict) -> dict | None: ...

    def download(self, url: str, opts: dict) -> None: ...


_ACTIVE: contextvars.ContextVar[Runner | None] = contextvars.ContextVar(
    "ytop_ytdlp_runner", default=None
)


def active() -> Runner | None:
    """The runner set for this thread's work, or None: run in-process."""
    return _ACTIVE.get()


@contextmanager
def using(runner: Runner | None) -> Iterator[None]:
    token = _ACTIVE.set(runner)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def spawn(pool: ThreadPoolExecutor, fn: Callable[..., Any], *args: Any) -> Future:
    """``pool.submit`` that keeps the caller's runner."""
    return pool.submit(contextvars.copy_context().run, fn, *args)


class RunnerError(RuntimeError):
    """The child could not run yt-dlp at all (no output, crashed)."""


def _plain(opts: dict) -> dict:
    """The JSON-safe part of yt-dlp options (hooks stay in this process)."""
    out = {k: v for k, v in opts.items() if k != "progress_hooks"}
    for key in ("cookiesfrombrowser",):
        if isinstance(out.get(key), tuple):
            out[key] = list(out[key])
    return json.loads(json.dumps(out, default=str))


class SubprocessRunner:
    """Runs each yt-dlp call in ``python -I -S -B`` with ``paths`` first on
    ``sys.path``.  ``cancel`` (polled) kills the child and raises
    :class:`JobCancelled`.  A yt-dlp failure in the child is raised here as
    this process's ``yt_dlp.utils.DownloadError`` with the child's message,
    so callers treat it exactly like an in-process one."""

    def __init__(self, paths: list[Path], *, python: str | None = None,
                 cancel: Callable[[], bool] | None = None,
                 extract_timeout: float = EXTRACT_TIMEOUT) -> None:
        self.paths = [Path(p) for p in paths]
        self.python = python or sys.executable
        self.cancel = cancel
        self.extract_timeout = extract_timeout

    def __repr__(self) -> str:
        return f"SubprocessRunner({[str(p) for p in self.paths]})"

    def extract(self, url: str, opts: dict) -> dict | None:
        result = self._run("extract", url, opts, [], self.extract_timeout)
        return result if isinstance(result, dict) else None

    def download(self, url: str, opts: dict) -> None:
        self._run("download", url, opts, list(opts.get("progress_hooks") or []), None)

    def _run(self, op: str, url: str, opts: dict, hooks: list[Callable[[dict], None]],
             timeout: float | None) -> Any:
        args = {"op": op, "url": url, "opts": _plain(opts),
                "paths": [str(p) for p in self.paths], "hook_keys": list(_HOOK_KEYS)}
        with tempfile.TemporaryFile() as errors:
            proc = subprocess.Popen(
                [self.python, "-I", "-S", "-B", "-c", _CHILD],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                text=True, encoding="utf-8", errors="replace",
                # No console window flashing up on Windows.
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            done = threading.Event()
            why: list[str] = []

            def watch() -> None:
                waited = 0.0
                while not done.wait(0.2):
                    waited += 0.2
                    if self.cancel is not None and self.cancel():
                        why.append("cancelled")
                    elif timeout is not None and waited > timeout:
                        why.append("timeout")
                    else:
                        continue
                    proc.kill()
                    return

            watcher = threading.Thread(target=watch, name="ytop-runner-watch", daemon=True)
            watcher.start()
            final: dict | None = None
            try:
                assert proc.stdin is not None and proc.stdout is not None
                proc.stdin.write(json.dumps(args) + "\n")
                proc.stdin.close()
                for line in proc.stdout:
                    if not line.startswith(_MARK):
                        continue
                    try:
                        doc = json.loads(line[len(_MARK):])
                    except ValueError:
                        continue
                    if not isinstance(doc, dict):
                        continue
                    if "hook" in doc:
                        for hook in hooks:
                            hook(dict(doc["hook"]))  # may raise JobCancelled
                    else:
                        final = doc
                proc.wait()
            except BaseException:
                proc.kill()
                proc.wait()
                raise
            finally:
                done.set()
                watcher.join(timeout=5)
            if "cancelled" in why:
                from .probe import JobCancelled  # probe imports this module

                raise JobCancelled("cancelled by the user")
            if "timeout" in why:
                raise _download_error(f"yt-dlp took longer than {timeout:.0f} s")
            if final is None:
                errors.seek(0)
                tail = errors.read().decode("utf-8", "replace").strip().splitlines()
                raise RunnerError(tail[-1] if tail else f"yt-dlp exited with {proc.returncode}")
            if "error" in final:
                raise _download_error(str(final.get("error") or final.get("type")))
            return final.get("result")


def _download_error(message: str) -> Exception:
    from yt_dlp.utils import DownloadError

    return DownloadError(message)
