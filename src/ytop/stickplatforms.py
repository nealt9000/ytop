"""Adding a platform to the running stick, from its own published release.

A stick carries the runtimes of the platforms it was made for.  Another can
be added later without remaking it: the release whose ``build_id`` is the
stick's own (found by its tag, ``v<version>``, and verified exactly as an app
update is, see :func:`ytop.appupdate.verified_release`) holds each platform
as separate pieces (``lib-<plat>``, ``runtime-<plat>-python``,
``runtime-<plat>-bin``).  Only those are downloaded, each checked against the
release's sha256, unpacked into ``<stick>/.ytop.platform-src``, and every file
checked against the release's manifest before anything on the stick changes.

Then the new ``runtime/<plat>`` and ``lib/<plat>`` folders are renamed in
(new folders, nothing replaced), and last the stick's manifest and
``build.json`` are rewritten (each written whole, then renamed over the old
one).  Until ``build.json`` names the platform, the stick neither starts on
it nor counts its files, so a crash part way leaves a stick that works as
before (the next add clears the leftovers).  Nothing the running app uses is
touched: no restart is needed.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tarfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import appupdate, product, updater
from . import makestick as ms
from .config import Config
from .manifest import (
    MANIFEST_HASH_KEY,
    MANIFEST_NAME,
    read_manifest,
    sha256_file,
    write_manifest,
)
from .runtime import PLATFORMS
from .updater import Http, Offline, RateLimited, UpdateError

log = logging.getLogger(__name__)

STAGING = ".ytop.platform-src"

# What people call each platform.
PLATFORM_LABELS = {
    "windows-x86_64": "Windows",
    "macos-arm64": "Mac (Apple silicon)",
    "macos-x86_64": "Mac (Intel)",
    "linux-x86_64": "Linux (Intel/AMD)",
    "linux-aarch64": "Linux (ARM)",
}

NOT_PUBLISHED = (
    "this copy of {name} ({build}) is not a published release, so its "
    "platforms can't be downloaded; make the stick again with them instead"
)

Progress = Callable[[str], None]


def platform_label(key: str) -> str:
    return PLATFORM_LABELS.get(key, key)


def platform_pieces(plat: str) -> tuple[str, ...]:
    return (f"lib-{plat}", f"runtime-{plat}-python", f"runtime-{plat}-bin")


@dataclass(frozen=True)
class AddResult:
    status: str  # added | failed
    message: str
    platforms: tuple[str, ...] = ()


def _say(progress: Progress | None, label: str) -> None:
    if progress is not None:
        progress(label)


def stick_platforms(ytop: Path) -> list[str]:
    """The platforms the stick at ``ytop`` starts on (its build.json)."""
    return list(appupdate._stick_build(ytop)["platforms"])


def release_for_build(ytop: Path, config: Config, http: Http) -> appupdate.AppRelease:
    """The verified release that *is* the stick's build (raises
    :class:`UpdateError` when there is none)."""
    build = appupdate._stick_build(ytop)
    version = str(build.get("version") or "")
    build_id = str(build.get("build_id") or "")
    not_published = UpdateError(NOT_PUBLISHED.format(name=product.NAME, build=build_id))
    if not version or not build_id:
        raise not_published
    api = product.api_base(config).rstrip("/")
    try:
        found = http.get_json(f"{api}/repos/{product.repo(config)}/releases/tags/v{version}")
    except updater.HttpError as exc:
        if exc.status == 404:
            raise not_published from None
        raise
    if not appupdate._is_app_release(found):
        raise not_published
    release = appupdate.verified_release(http, found)
    if release.build_id != build_id:
        raise not_published
    return release


def _write_whole(path: Path, data: bytes) -> None:
    temp = path.with_name(path.name + ".ytop-tmp")
    with open(temp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def add_platforms(
    ytop: Path,
    config: Config,
    wanted: list[str],
    *,
    http: Http | None = None,
    progress: Progress | None = None,
) -> AddResult:
    """Add ``wanted`` platforms to the stick at ``ytop`` (module docstring).
    Never raises."""
    http = http or Http()
    target = ytop.parent
    locked = False
    staging = target / STAGING
    try:
        have = stick_platforms(ytop)
        new = [plat for plat in wanted if plat not in have]
        unknown = [plat for plat in new if plat not in PLATFORMS]
        if unknown:
            raise UpdateError(f"unknown platform {', '.join(unknown)}")
        if not new:
            return AddResult("added", "The stick already starts on those.", ())
        ms._acquire_lock(target)
        locked = True
        _say(progress, "Finding this version's download…")
        release = release_for_build(ytop, config, http)
        added = _add(ytop, release, have, new, http, staging, progress)
    except Offline:
        return AddResult("failed", "No network: connect to the internet and try again.")
    except RateLimited as exc:
        return AddResult("failed", f"GitHub is busy ({exc}); try again later.")
    except (UpdateError, appupdate.InstallError, ms.MakeStickError, OSError, ValueError,
            tarfile.TarError) as exc:
        log.warning("adding %s failed: %s", ", ".join(wanted), exc)
        return AddResult("failed", f"The platform couldn't be added: {exc}")
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if locked:
            ms._release_lock(target)
    names = ", ".join(platform_label(plat) for plat in added)
    return AddResult("added", f"The stick now also starts on {names}.", tuple(added))


def _add(
    ytop: Path, release: appupdate.AppRelease, have: list[str], new: list[str],
    http: Http, staging: Path, progress: Progress | None,
) -> list[str]:
    target = ytop.parent
    doc: dict[str, Any] = release.doc
    lacking = [plat for plat in new if plat not in doc["platforms"]]
    if lacking:
        raise UpdateError(f"the release has no {', '.join(lacking)}")
    pieces = doc["pieces"]
    names = [name for plat in new for name in platform_pieces(plat) if name in pieces]
    files = {rel for name in names for rel in pieces[name]["files"]}
    # Only the new platforms' own folders: never a file the stick already has.
    roots = {f"{part}/{plat}" for plat in new for part in ("runtime", "lib")}
    for rel in files:
        if "/".join(rel.split("/")[:2]) not in roots:
            raise UpdateError(f"the release's piece holds {rel}, outside the new platform")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    size = sum(int(pieces[name]["size"]) for name in names)
    free = appupdate._free(target)
    if free and 2 * size + ms.SPACE_MARGIN > free:
        raise UpdateError("not enough space on the stick")
    cluster = ms._cluster_size(target)
    downloads = staging / appupdate.DOWNLOADS
    for name in names:
        piece = pieces[name]
        filename = updater._safe_filename(piece["file"])
        url = release.assets.get(filename)
        if url is None:
            raise UpdateError(f"the release has no {filename}")
        _say(progress, f"Downloading {filename}…")
        path = downloads / filename
        http.download(url, path, limit=max(int(piece["size"]), 1))
        if path.stat().st_size != piece["size"] or sha256_file(path) != piece["sha256"]:
            raise updater.ChecksumError(f"{filename} does not match the release's checksum")
        _say(progress, f"Unpacking {filename}…")
        appupdate._extract(path, staging, set(piece["files"]), cluster)
        path.unlink()
    for rel in sorted(files):
        if sha256_file(staging / rel) != doc["manifest"][rel]:
            raise UpdateError(f"{rel} differs from the release's file list")

    _say(progress, "Putting it on the stick…")
    for root in sorted(roots):
        if not (staging / root).is_dir():
            continue
        if os.path.lexists(ytop / root):
            shutil.rmtree(ytop / root)  # left by an add that was cut off
        (ytop / root).parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging / root, ytop / root)

    manifest = read_manifest(ytop / MANIFEST_NAME) or {}
    manifest.update({rel: doc["manifest"][rel] for rel in files})
    write_manifest(ytop / MANIFEST_NAME, manifest)  # whole, then renamed in

    build = json.loads((ytop / "build.json").read_text(encoding="utf-8"))
    # The release's order, as a stick made with all of them would list them.
    order = list(doc["platforms"])
    build["platforms"] = (
        [plat for plat in order if plat in have or plat in new]
        + [plat for plat in have if plat not in order]
    )
    sizes = build.get("sizes") if isinstance(build.get("sizes"), dict) else {}
    release_sizes = doc["build"].get("sizes") if isinstance(doc["build"].get("sizes"), dict) else {}
    for plat in new:
        if plat in release_sizes:
            sizes[plat] = release_sizes[plat]
    build["sizes"] = sizes
    build[MANIFEST_HASH_KEY] = sha256_file(ytop / MANIFEST_NAME)
    _write_whole(ytop / "build.json", (json.dumps(build, indent=2) + "\n").encode("utf-8"))
    return new


class PlatformAdds:
    """Runs one add at a time in the background for the web page."""

    def __init__(
        self,
        config: Callable[[], Config],
        ytop: Path | None,
        http: Callable[[], Http] = Http,
    ) -> None:
        self._config = config
        self.ytop = ytop
        self._http = http
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {"running": False, "platforms": [], "label": "",
                                       "result": None}

    @classmethod
    def for_runtime(cls, config: Callable[[], Config]) -> PlatformAdds:
        return cls(config, appupdate.stick_image())

    @property
    def available(self) -> bool:
        return self.ytop is not None

    @property
    def running(self) -> bool:
        with self._lock:
            return bool(self._state["running"])

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def platforms(self) -> list[str]:
        if self.ytop is None:
            return []
        try:
            return stick_platforms(self.ytop)
        except UpdateError:
            return []

    def start(self, platforms: list[str]) -> None:
        if self.ytop is None:
            raise ValueError(f"{product.NAME} is not running from a stick")
        wanted = [plat for plat in platforms if plat in PLATFORMS]
        if not wanted:
            raise ValueError("choose a platform to add")
        with self._lock:
            if self._state["running"]:
                raise ValueError("a platform is already being added")
            self._state = {"running": True, "platforms": wanted,
                           "label": "Starting…", "result": None}
        ytop = self.ytop
        self._thread = threading.Thread(
            target=self._run, args=(ytop, wanted), name="ytop-add-platform", daemon=True,
        )
        self._thread.start()

    def _say(self, label: str) -> None:
        with self._lock:
            self._state["label"] = label

    def _run(self, ytop: Path, wanted: list[str]) -> None:
        try:
            result = add_platforms(ytop, self._config(), wanted, http=self._http(),
                                   progress=self._say)
        except Exception as exc:  # noqa: BLE001 - never leave it "running"
            log.exception("adding a platform failed")
            result = AddResult("failed", f"The platform couldn't be added: {exc}")
        with self._lock:
            self._state = {"running": False, "platforms": wanted, "label": "",
                           "result": {"status": result.status, "message": result.message}}

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)
