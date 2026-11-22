"""App updates: opt-in per stick, shown with their notes, installed only
after the user confirms, applied incrementally.

**Asking.**  On the first start that finds the network (the component
check at start got an answer, :data:`ONLINE_OUTCOMES`) while the stick's
``[updates] check_app`` is unset, the app asks once whether to check for
updates on this stick; the answer is stored in its ``config.toml``.  "Check
now" works whatever the answer.

**Checking** (:func:`check_app`).  ``repos/<repo>/releases/latest`` from the
releases API; its ``ytop-release.json`` (see ``tools/package_release.py``)
is trusted only once it matches the release's ``SHA256SUMS``.  A release is
offered when its ``build_id`` differs from the stick's, its version is newer
(PEP 440 order) and it carries every platform the stick does.  An offer
is kept in the library's cache (:func:`save_offer`) and shown again at the
next start (for up to :data:`CHECK_EVERY`) even when GitHub cannot be
reached.  Sticks that said yes check at every start and, while the app
stays open, again every :data:`CHECK_EVERY` (6 h; :meth:`AppUpdates.recheck_if_due`).
Every answer but offline restarts that clock, a broken release and a rate
limit included, so GitHub is not asked again at every poll.  A check that
gets no answer (offline, error, rate-limited) leaves a shown offer up.

**Installing** (:func:`install_app`, after the user confirmed):

1. the release manifest (filtered to the stick's platforms) is compared with
   the stick's own (:func:`ytop.makestick.diff_upgrade`); only the release
   *pieces* holding changed files are downloaded, each checked against the
   sha256 that the verified ``ytop-release.json`` lists;
2. they are unpacked into ``<stick>/.ytop.making-src/`` with the release's
   manifest and ``build.json``: an overlay image whose other files are the
   stick's own (never copied);
3. components older than the release's ``min_components`` are updated first
   (:mod:`ytop.updater`); if that fails nothing is switched;
4. the caller stops jobs and flushes, exactly as Quit does (``before_switch``);
5. the stick upgrades itself through the journalled switch (makestick's
   self mode);
6. the caller says "YTOP was updated to <version>. Start it again." and
   exits.  No code is swapped in a running process.

**Reverting** (:func:`revert_app`).  The switch keeps what it replaced in
``.ytop/previous/`` (:func:`ytop.makestick.read_previous`).  A revert is an
install whose source is local: ``previous/`` overlaid on the stick's own
files is staged (and hash-checked against ``previous/manifest.sha256``)
in ``.ytop.making-src/``, then steps 4-6 run as above ("YTOP was reverted
to <version>").  The files it reverted from become the new ``previous/``,
so reverting twice returns to the update.  Components are not image-owned
and are never reverted.

Library data (``data/``, ``library.json``, ``YTOP Library/``, the rest of
``config.toml``) is never touched, and no library schema is upgraded.
Network steps never raise to callers.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tarfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, TypeGuard

from . import makestick as ms
from . import product, updater
from .components import _version_key
from .config import Config, set_updates_option
from .manifest import (
    MANIFEST_HASH_KEY,
    MANIFEST_NAME,
    for_platforms,
    sha256_file,
    write_manifest,
)
from .runtime import active_runtime
from .stick.boot import MAKING_SRC, remove_tree_best_effort, switch_interrupted
from .updater import JSON_LIMIT, Http, Offline, RateLimited, UpdateError, UpdateResult

log = logging.getLogger(__name__)

RELEASE_JSON = "ytop-release.json"
SUMS = "SHA256SUMS"
DOWNLOADS = ".downloads"  # inside the staging folder, while fetching
NOTES_LIMIT = 20_000
# The start-up check, once it found nothing to offer, waits this long.
CHECK_EVERY = timedelta(hours=6)
# What the start-up component check returns when the network answered
# (``recent``: it answered within the last day, see autoupdate.check_due).
# AppCheck statuses that say nothing about the release: a kept offer stays.
NO_ANSWER = frozenset({"offline", "error", "rate_limited"})

ONLINE_OUTCOMES = frozenset({
    "up to date", "up_to_date", "updated", "failed", "rate_limited", "recent",
})

UPDATED = "{name} was updated to {version}. Start it again."
UNDONE = (
    "The update couldn't be installed, so it was undone: {name} is as it "
    "was. Start it again."
)
UNDONE_NEXT_START = (
    "The update couldn't be installed. Start {name} again: it puts the "
    "previous version back first."
)
REVERTED = "{name} was reverted to {version}. Start it again."
REVERT_UNDONE = (
    "{name} couldn't be reverted, so nothing changed: {name} is as it "
    "was. Start it again."
)
REVERT_UNDONE_NEXT_START = (
    "{name} couldn't be reverted. Start {name} again: it puts the version "
    "you had back first."
)

_HEX64 = re.compile(r"[0-9a-f]{64}")

Progress = Callable[[str], None]


class InstallError(Exception):
    """The update cannot be installed (nothing was changed)."""


class InstallAborted(InstallError):
    """``before_switch`` declined: jobs could not stop (nothing changed)."""


@dataclass(frozen=True)
class AppRelease:
    version: str
    build_id: str
    tag: str
    notes: str
    doc: dict[str, Any] = field(repr=False)  # the verified ytop-release.json
    assets: dict[str, str] = field(repr=False)  # asset name -> download URL


@dataclass(frozen=True)
class AppCheck:
    # available | up_to_date | unsupported | offline | rate_limited | error
    status: str
    release: AppRelease | None = None
    message: str = ""


@dataclass(frozen=True)
class Previous:
    """The version a revert would put back (``.ytop/previous``)."""

    version: str
    build_id: str


@dataclass(frozen=True)
class InstallResult:
    status: str  # updated | reverted | undone | failed
    version: str
    message: str
    # Jobs were stopped for the switch: the app must exit now.
    stopped: bool = False


# --- the stick ---------------------------------------------------------------------


def stick_image() -> Path | None:
    """The running stick's ``.ytop`` (None on a checkout)."""
    home = active_runtime().home
    if home is None:
        return None
    ytop = home / ".ytop"
    return ytop if (ytop / "build.json").is_file() else None


def _stick_build(ytop: Path) -> dict[str, Any]:
    try:
        build = json.loads((ytop / "build.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise UpdateError("this is not a YTOP stick (no readable build.json)") from None
    platforms = build.get("platforms") if isinstance(build, dict) else None
    if not isinstance(platforms, list) or not all(isinstance(p, str) for p in platforms):
        raise UpdateError("the stick's build.json lists no platforms")
    return build


def _newer(version: str, than: str) -> bool:
    return _version_key(version) > _version_key(than or "0")


# --- checking ----------------------------------------------------------------------


def _safe_rel(rel: object) -> bool:
    if not isinstance(rel, str) or not rel or "\\" in rel:
        return False
    pure = PurePosixPath(rel)
    return not pure.is_absolute() and ".." not in pure.parts and updater._safe_member(rel)


def _validated(doc: Any) -> dict[str, Any]:
    """``ytop-release.json`` with the fields an install relies on checked."""
    if not isinstance(doc, dict):
        raise UpdateError(f"{RELEASE_JSON}: not an object")
    for key in ("version", "build_id"):
        if not isinstance(doc.get(key), str) or not doc[key]:
            raise UpdateError(f"{RELEASE_JSON}: no {key}")
    platforms = doc.get("platforms")
    if not isinstance(platforms, list) or not all(isinstance(p, str) for p in platforms):
        raise UpdateError(f"{RELEASE_JSON}: no platforms")
    build = doc.get("build")
    if not isinstance(build, dict) or build.get("build_id") != doc["build_id"]:
        raise UpdateError(f"{RELEASE_JSON}: its build.json does not match it")
    manifest = doc.get("manifest")
    if not isinstance(manifest, dict) or not manifest or not all(
        _safe_rel(rel) and isinstance(h, str) and _HEX64.fullmatch(h)
        for rel, h in manifest.items()
    ):
        raise UpdateError(f"{RELEASE_JSON}: no usable manifest")
    pieces = doc.get("pieces")
    if not isinstance(pieces, dict) or not pieces:
        raise UpdateError(f"{RELEASE_JSON}: no pieces")
    for name, piece in pieces.items():
        if not (
            isinstance(piece, dict)
            and isinstance(piece.get("file"), str)
            and isinstance(piece.get("sha256"), str) and _HEX64.fullmatch(piece["sha256"])
            and isinstance(piece.get("size"), int) and piece["size"] >= 0
            and isinstance(piece.get("files"), list)
            and all(_safe_rel(rel) and rel in manifest for rel in piece["files"])
        ):
            raise UpdateError(f"{RELEASE_JSON}: piece {name!r} is malformed")
    minimum = doc.get("min_components")
    if not isinstance(minimum, dict) or not minimum or not all(
        isinstance(k, str) and isinstance(v, str) and v for k, v in minimum.items()
    ):
        # The components gate relies on it: a release without it is refused.
        raise UpdateError(f"{RELEASE_JSON}: no min_components")
    return doc


NO_RELEASE = "no published release"


def _is_app_release(release: object) -> TypeGuard[dict[str, Any]]:
    """A published (not draft, not pre-release) YTOP app release."""
    return (
        isinstance(release, dict)
        and not release.get("draft") and not release.get("prerelease")
        and RELEASE_JSON in updater._gh_assets(release)
    )


def _latest_app_release(http: Http, api: str, repo: str) -> dict[str, Any] | None:
    """The newest published release that carries ``ytop-release.json``, or
    None when there is none (or the repo cannot be seen: GitHub answers a
    private or missing repo with 404).

    The repo's "latest" release can be another kind (the ``ffmpeg-minimal``
    build input lives in the same repo), so the release list is searched
    when "latest" is not an app release.
    """
    try:
        latest = http.get_json(f"{api}/repos/{repo}/releases/latest")
    except updater.HttpError as exc:
        if exc.status != 404:
            raise
        latest = None
    if _is_app_release(latest):
        return latest
    try:
        listed = http.get_json(f"{api}/repos/{repo}/releases")
    except updater.HttpError as exc:
        if exc.status == 404:
            return None
        raise
    for release in listed if isinstance(listed, list) else []:
        if _is_app_release(release):
            return release
    return None


def _fetch(ytop: Path, config: Config, http: Http) -> AppCheck:
    stick = _stick_build(ytop)
    latest = _latest_app_release(http, product.api_base(config).rstrip("/"),
                                 product.repo(config))
    if latest is None:
        return AppCheck("up_to_date", message=NO_RELEASE)
    return _judge(stick, verified_release(http, latest))


def verified_release(http: Http, latest: dict[str, Any]) -> AppRelease:
    """The GitHub release ``latest`` as an :class:`AppRelease`, once its
    ``ytop-release.json`` matches its ``SHA256SUMS`` (raises otherwise)."""
    assets = {
        name: str(entry["browser_download_url"])
        for name, entry in updater._gh_assets(latest).items()
    }
    if RELEASE_JSON not in assets or SUMS not in assets:
        raise UpdateError(f"the latest release has no {RELEASE_JSON} or {SUMS}")
    sums = http.get_bytes(assets[SUMS], limit=1 << 20).decode("utf-8", "replace")
    data = http.get_bytes(assets[RELEASE_JSON], limit=JSON_LIMIT)
    expected = updater._sum_for(sums, RELEASE_JSON, single=False)
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise updater.ChecksumError(f"{RELEASE_JSON} does not match {SUMS}")
    try:
        doc = _validated(json.loads(data))
    except ValueError:
        raise UpdateError(f"{RELEASE_JSON}: not JSON") from None
    notes = latest.get("body")
    return AppRelease(
        version=doc["version"], build_id=doc["build_id"],
        tag=str(latest.get("tag_name") or ""),
        notes=(notes if isinstance(notes, str) else "")[:NOTES_LIMIT],
        doc=doc, assets=assets,
    )


def _judge(stick: dict[str, Any], release: AppRelease) -> AppCheck:
    """Whether the stick whose ``build.json`` is ``stick`` is offered the
    verified ``release``."""
    doc = release.doc
    if doc["build_id"] == stick.get("build_id") or not _newer(
        doc["version"], str(stick.get("version") or "")
    ):
        return AppCheck("up_to_date")
    lacking = [p for p in stick["platforms"] if p not in doc["platforms"]]
    if lacking:
        return AppCheck(
            "unsupported",
            message=f"{product.NAME} {release.version} has no {', '.join(lacking)}, "
                    "which this stick carries",
        )
    return AppCheck("available", release)


def check_app_result(ytop: Path, config: Config, http: Http | None = None) -> AppCheck:
    """Look for a newer release for the stick at ``ytop``; never raises."""
    try:
        return _fetch(ytop, config, http or Http())
    except Offline as exc:
        return AppCheck("offline", message=str(exc))
    except RateLimited as exc:
        return AppCheck("rate_limited", message=str(exc))
    except (UpdateError, OSError, ValueError) as exc:
        return AppCheck("error", message=str(exc))


def check_app(ytop: Path, config: Config, http: Http | None = None) -> AppRelease | None:
    """The newer, verified release to offer, or None."""
    return check_app_result(ytop, config, http).release


# --- the offer, kept between starts -----------------------------------------------
#
# A found release is kept in the library's cache (``<library>/.ytop/cache/
# app-offer.json``, never image-owned) with the time it was found and the
# build it was found for, so a start within ``CHECK_EVERY`` shows it again
# without asking GitHub.  It is used only while it still is an offer for the
# stick as it is (its ``ytop-release.json`` is checked again, as at the
# check); otherwise the start-up check looks again.

OFFER_FILE = "app-offer.json"


def _offer_path(library_root: Path) -> Path:
    from .layout import LibraryLayout  # lazy: layout -> yt -> config

    return LibraryLayout(Path(library_root)).cache_dir / OFFER_FILE


def save_offer(library_root: Path, ytop: Path, release: AppRelease, when: datetime) -> None:
    """Keep ``release`` (an offer for the stick at ``ytop``); atomic."""
    path = _offer_path(library_root)
    doc = {
        "schema": 1,
        "checked_at": when.isoformat(),
        "stick_build_id": str(_stick_build(ytop).get("build_id") or ""),
        "version": release.version, "build_id": release.build_id,
        "tag": release.tag, "notes": release.notes,
        "doc": release.doc, "assets": release.assets,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with open(temp, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(doc) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def drop_offer(library_root: Path) -> None:
    try:
        _offer_path(library_root).unlink(missing_ok=True)
    except OSError:
        pass


def load_offer(library_root: Path, ytop: Path, now: datetime) -> AppRelease | None:
    """The kept offer, if it was found less than ``CHECK_EVERY`` ago for
    the build on the stick now and still is an offer for it; never raises."""
    try:
        saved = json.loads(_offer_path(library_root).read_text(encoding="utf-8"))
        checked = datetime.fromisoformat(saved["checked_at"])
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=UTC)
        if not timedelta(0) <= now - checked < CHECK_EVERY:
            return None
        stick = _stick_build(ytop)
        if saved["stick_build_id"] != str(stick.get("build_id") or ""):
            return None
        doc = _validated(saved["doc"])
        assets = saved["assets"]
        if not isinstance(assets, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in assets.items()
        ):
            return None
        notes = saved.get("notes")
        release = AppRelease(
            version=doc["version"], build_id=doc["build_id"],
            tag=str(saved.get("tag") or ""),
            notes=(notes if isinstance(notes, str) else "")[:NOTES_LIMIT],
            doc=doc, assets=dict(assets),
        )
        return release if _judge(stick, release).status == "available" else None
    except (OSError, ValueError, KeyError, TypeError, AttributeError, UpdateError):
        return None


# --- installing --------------------------------------------------------------------


def _pieces_for(platforms: list[str]) -> set[str]:
    """The release pieces a stick with ``platforms`` may need: never another
    platform's."""
    names = {"app", "root", "lib-common"}
    for plat in platforms:
        names |= {f"lib-{plat}", f"runtime-{plat}-python", f"runtime-{plat}-bin"}
    return names


def _say(progress: Progress | None, label: str) -> None:
    if progress is not None:
        progress(label)


def _needed_pieces(ytop: Path, doc: dict[str, Any], platforms: list[str]) -> tuple[
    dict[str, str], list[str]
]:
    """(wanted manifest, pieces to download)."""
    wanted = for_platforms(doc["manifest"], platforms)
    piece_of = {
        rel: name for name, piece in doc["pieces"].items() for rel in piece["files"]
    }
    allowed = _pieces_for(platforms)
    for rel in wanted:
        if piece_of.get(rel) not in allowed:
            raise InstallError(f"{rel} is in no release piece for this stick")
    diff = ms.diff_upgrade(None, ytop, wanted, verify=False)
    if diff.checked_by_hash:  # no file list the stick can trust: hash it
        diff = ms.diff_upgrade(None, ytop, wanted, verify=True)
    need = {piece_of[rel] for rel in diff.copy}
    if any(rel.startswith("app/") for rel in diff.copy + diff.remove):
        need.add("app")  # app/ is switched whole
    return wanted, sorted(need)


def _free(path: Path) -> int:
    try:
        return int(shutil.disk_usage(path).free)
    except OSError:
        return 0


def _extract(archive: Path, dest: Path, allowed: set[str], cluster: int) -> None:
    """Unpack a verified piece: plain files it lists, nowhere else."""
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        total = 0
        for member in members:
            if not member.isfile():
                raise InstallError(f"{archive.name}: {member.name!r} is not a plain file")
            if not _safe_rel(member.name) or member.name not in allowed:
                raise InstallError(f"{archive.name}: unexpected {member.name!r}")
            total += member.size + cluster
        free = _free(dest)
        if free and total + ms.SPACE_MARGIN > free:
            raise InstallError("not enough space on the stick for the update")
        tar.extractall(dest, members=members, filter="data")


def _stage(
    ytop: Path, release: AppRelease, http: Http, progress: Progress | None,
) -> Path:
    """Steps 1-2: the overlay image in ``<stick>/.ytop.making-src``."""
    target = ytop.parent
    doc = release.doc
    platforms = list(_stick_build(ytop)["platforms"])
    _say(progress, "Checking what changed…")
    wanted, need = _needed_pieces(ytop, doc, platforms)
    staging = target / MAKING_SRC
    if not remove_tree_best_effort(staging):
        raise InstallError(
            f"files of an earlier update are still in use; start {product.NAME} again"
        )
    staging.mkdir()
    pieces = doc["pieces"]
    size = sum(int(pieces[name]["size"]) for name in need)
    free = _free(target)
    if free and size + ms.SPACE_MARGIN > free:
        raise InstallError("not enough space on the stick for the update")
    downloads = staging / DOWNLOADS
    cluster = ms._cluster_size(target)
    for name in need:
        piece = pieces[name]
        filename = updater._safe_filename(piece["file"])
        url = release.assets.get(filename)
        if url is None:
            raise InstallError(f"the release has no {filename}")
        _say(progress, f"Downloading {filename}…")
        path = downloads / filename
        http.download(url, path, limit=max(int(piece["size"]), 1))
        if path.stat().st_size != piece["size"] or sha256_file(path) != piece["sha256"]:
            raise updater.ChecksumError(f"{filename} does not match the release's checksum")
        _say(progress, f"Unpacking {filename}…")
        _extract(path, staging, set(piece["files"]), cluster)
        path.unlink()
        # Every unpacked file must be the one the release's manifest names,
        # before anything on the stick (or a running job) is touched.
        for rel in piece["files"]:
            if wanted.get(rel) is None or sha256_file(staging / rel) != wanted[rel]:
                raise InstallError(
                    f"{filename}: {rel} differs from the release's file list"
                )
    if downloads.is_dir():
        downloads.rmdir()
    # The release's own file list and build.json, so the switch writes them
    # like any upgrade (and self mode trusts the staged image).
    write_manifest(staging / MANIFEST_NAME, wanted)
    build = dict(doc["build"])
    build[MANIFEST_HASH_KEY] = sha256_file(staging / MANIFEST_NAME)
    with open(staging / "build.json", "w", encoding="utf-8") as handle:
        handle.write(json.dumps(build, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return staging


def _behind(ytop: Path, minimum: dict[str, str]) -> list[str]:
    have = updater.installed_versions(ytop)
    return sorted(
        name for name, version in minimum.items()
        if name not in have or _version_key(have[name]) < _version_key(version)
    )


def _components(
    ytop: Path, release: AppRelease,
    update_components: Callable[[list[str]], UpdateResult],
    progress: Progress | None,
) -> None:
    """Step 3: the components the release needs, first."""
    behind = _behind(ytop, release.doc["min_components"])
    if not behind:
        return
    _say(progress, f"Updating {', '.join(behind)}…")
    result = update_components(behind)
    still = _behind(ytop, release.doc["min_components"])
    if still:
        why = result.message or result.status
        raise InstallError(
            f"{product.NAME} {release.version} needs a newer {', '.join(still)}, "
            f"which couldn't be updated ({why}); nothing was changed"
        )


def install_app(
    ytop: Path,
    config: Config,
    release: AppRelease,
    *,
    http: Http | None = None,
    update_components: Callable[[list[str]], UpdateResult] | None = None,
    before_switch: Callable[[], None] | None = None,
    progress: Progress | None = None,
) -> InstallResult:
    """Install ``release`` on the stick at ``ytop`` (module docstring).

    Only call this after the user confirmed.  ``before_switch`` stops jobs
    and flushes (it may raise :class:`InstallAborted`); once it has run the
    result says ``stopped`` and the caller must exit, whatever happened.
    Never raises (a crash is repaired by the stick's next start)."""
    if not _INSTALL_LOCK.acquire(blocking=False):
        return InstallResult("failed", release.version, "An update is already being installed.")
    try:
        return _install(
            ytop, config, release, http=http or Http(),
            update_components=update_components or (
                lambda names: updater.update(ytop, config, names, http=http)
            ),
            before_switch=before_switch, progress=progress,
        )
    finally:
        _INSTALL_LOCK.release()


_INSTALL_LOCK = threading.Lock()


def _install(
    ytop: Path, config: Config, release: AppRelease, *, http: Http,
    update_components: Callable[[list[str]], UpdateResult],
    before_switch: Callable[[], None] | None, progress: Progress | None,
) -> InstallResult:
    target = ytop.parent
    staging = target / MAKING_SRC
    name, version = product.NAME, release.version
    locked = False
    try:
        try:
            if _stick_build(ytop).get("build_id") == release.build_id:
                raise InstallError(f"{name} {version} is already on this stick")
            # The stick's lock from staging on: a second start of this stick
            # neither clears the staged files nor "repairs" the switch.
            ms._acquire_lock(target)
            locked = True
            _stage(ytop, release, http, progress)
            _components(ytop, release, update_components, progress)
            # Planned before anything stops: a plan that cannot work fails
            # here, with the app still running.
            plan = ms.plan_self_upgrade(target, staging)
            _say(progress, "Stopping downloads and saving…")
            if before_switch is not None:
                before_switch()
        except InstallAborted as exc:
            remove_tree_best_effort(staging)
            return InstallResult("failed", version, str(exc))
        except (InstallError, UpdateError, ms.MakeStickError, OSError, ValueError,
                tarfile.TarError) as exc:
            remove_tree_best_effort(staging)
            log.warning("app update to %s not installed: %s", version, exc)
            return InstallResult(
                "failed", version, f"The update couldn't be installed: {exc}",
            )
        return _switch(ytop, release, plan, progress)
    finally:
        if locked:
            ms._release_lock(target)


def _switch(
    ytop: Path, release: AppRelease, plan: ms.MakePlan, progress: Progress | None,
) -> InstallResult:
    """Step 5: jobs are stopped, so the app exits whatever happens here."""
    return _apply(ytop, plan, progress, version=release.version,
                  build_id=release.build_id, revert=False)


def _apply(
    ytop: Path, plan: ms.MakePlan, progress: Progress | None, *,
    version: str, build_id: str, revert: bool,
) -> InstallResult:
    """Switch the staged image in (the app exits whatever happens here)."""
    target = ytop.parent
    staging = target / MAKING_SRC
    name = product.NAME
    _say(progress, f"Putting {name} {version} in place…")
    try:
        ms.run_make(plan, staging, None, lambda update: None, lock_held=True)
    except Exception as exc:  # noqa: BLE001 - reported; the stick is repaired
        log.exception("app %s switch failed", "revert" if revert else "update")
        remove_tree_best_effort(staging)
        return _after_failure(ytop, version, build_id, exc, revert=revert)
    remove_tree_best_effort(staging)
    if revert:
        return InstallResult("reverted", version,
                             REVERTED.format(name=name, version=version), stopped=True)
    return InstallResult("updated", version, UPDATED.format(name=name, version=version),
                         stopped=True)


def _after_failure(
    ytop: Path, version: str, build_id: str, exc: Exception, *, revert: bool = False,
) -> InstallResult:
    name = product.NAME
    try:
        have = _stick_build(ytop).get("build_id")
    except UpdateError:
        have = None
    if switch_interrupted(ytop.parent):
        message = REVERT_UNDONE_NEXT_START if revert else UNDONE_NEXT_START
        return InstallResult("undone", version, message.format(name=name), stopped=True)
    if have == build_id:  # in place; only tidying up failed
        log.warning("app %s in place, but: %s", "revert" if revert else "update", exc)
        status, done = ("reverted", REVERTED) if revert else ("updated", UPDATED)
        return InstallResult(status, version, done.format(name=name, version=version),
                             stopped=True)
    message = REVERT_UNDONE if revert else UNDONE
    return InstallResult("undone", version, message.format(name=name), stopped=True)


# --- reverting ---------------------------------------------------------------------


def previous_version(ytop: Path) -> Previous | None:
    """The version a revert puts back, if the stick keeps a valid one."""
    build = ms.read_previous(ytop)
    if build is None:
        return None
    return Previous(str(build.get("version") or ""), str(build.get("build_id") or ""))


def keeps_previous(ytop: Path) -> bool:
    """An update of the stick at ``ytop`` keeps a copy to revert to: only
    a stick whose own file list can be trusted gets one (the switch checks
    the copy against it)."""
    return ms.read_trusted_manifest(ytop) is not None


def previous_verdict(ytop: Path) -> str:
    """``"valid"``, ``"invalid"`` or ``"unknown"`` for ``ytop/previous``
    (``"none"`` when there is none).  Read only.  ``"invalid"`` only on a
    definitive verdict: no mark (it is written last, so the copy was never
    finished), or a mark that reads fine naming another ``build.json`` than
    the stick's, which reads fine too.  Anything that cannot be read for
    another reason (a slow stick, a file an antivirus holds) is
    ``"unknown"``, and an unknown copy is never deleted."""
    previous = ytop / ms.PREVIOUS
    if not os.path.lexists(previous):
        return "none"
    if not previous.is_dir() or previous.is_symlink():
        return "invalid"
    try:
        text = (previous / ms.PREVIOUS_MARK).read_text(encoding="utf-8")
    except FileNotFoundError:
        return "invalid"
    except OSError:
        return "unknown"
    try:
        mark = json.loads(text)
        have = sha256_file(ytop / "build.json")
    except (OSError, ValueError):
        return "unknown"
    named = mark.get("build_sha256") if isinstance(mark, dict) else None
    if not isinstance(named, str):
        return "unknown"
    if named != have:
        return "invalid"
    return "valid" if ms.read_previous(ytop) is not None else "unknown"


def tidy_previous(ytop: Path) -> bool:
    """Delete what a crash after a switch's commit point can leave inside
    ``.ytop``: a ``previous.del`` and a ``previous/`` that is definitely
    no valid rollback copy (:func:`previous_verdict`; it is never
    offered).  Only under the stick's lock, never while a switch is still
    to be repaired; best effort, never raises.  True if it tidied."""
    previous, drop = ytop / ms.PREVIOUS, ytop / ms.PREVIOUS_DROP

    def stale() -> bool:
        return os.path.lexists(drop) or previous_verdict(ytop) == "invalid"

    target = ytop.parent
    try:
        if not stale() or switch_interrupted(target):
            return False
        if not _INSTALL_LOCK.acquire(blocking=False):
            return False
        try:
            try:
                ms._acquire_lock(target)
            except ms.MakeStickError:
                return False  # another YTOP is updating or making this stick
            try:
                if not stale():  # looked at again under the lock
                    return False
                if previous_verdict(ytop) == "invalid":
                    remove_tree_best_effort(previous)
                remove_tree_best_effort(drop)
            finally:
                ms._release_lock(target)
        finally:
            _INSTALL_LOCK.release()
    except OSError:
        log.warning("could not tidy the stick's old rollback copy", exc_info=True)
        return False
    return True


def _stage_revert(ytop: Path, progress: Progress | None) -> Path:
    """``previous/`` overlaid on the stick, staged in ``.ytop.making-src``:
    every file the switch writes, each checked against the previous
    manifest, plus that manifest and ``build.json``.  ``previous/`` itself
    is only read (it stays valid until the switch's commit point)."""
    target = ytop.parent
    previous = ytop / ms.PREVIOUS
    _say(progress, "Checking the previous version…")
    platforms = list(_stick_build(ytop)["platforms"])
    wanted = ms.image_manifest(previous, platforms)
    diff = ms.diff_upgrade(
        None, ytop, wanted, verify=ms.read_trusted_manifest(ytop) is None,
    )
    rels = sorted(ms._app_rels(diff, wanted))

    def source(rel: str) -> Path:
        return previous / rel if (previous / rel).is_file() else ytop / rel

    sizes: dict[str, int] = {}
    for rel in rels:
        try:
            sizes[rel] = os.stat(source(rel)).st_size
        except OSError:
            raise InstallError(
                f"the previous version lacks {rel}; nothing was changed"
            ) from None
    staging = target / MAKING_SRC
    if not remove_tree_best_effort(staging):
        raise InstallError(
            f"files of an earlier update are still in use; start {product.NAME} again"
        )
    cluster = ms._cluster_size(target)
    need = sum(sizes.values()) + cluster * (len(rels) + 2)
    free = _free(target)
    if free and need + ms.SPACE_MARGIN > free:
        raise InstallError("not enough space on the stick to revert")
    staging.mkdir()
    _say(progress, "Preparing the previous version…")
    for rel in rels:
        dest = staging / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source(rel), dest)
        if sha256_file(dest) != wanted[rel]:
            raise InstallError(
                f"the previous version's {rel} is damaged; nothing was changed"
            )
    for name in (MANIFEST_NAME, "build.json"):
        shutil.copyfile(previous / name, staging / name)
    return staging


def revert_app(
    ytop: Path,
    *,
    before_switch: Callable[[], None] | None = None,
    progress: Progress | None = None,
) -> InstallResult:
    """Put the previous version back (module docstring).  Only after the
    user confirmed; ``before_switch`` and the result as for
    :func:`install_app`.  Never raises."""
    if not _INSTALL_LOCK.acquire(blocking=False):
        return InstallResult("failed", "", "An update is already being installed.")
    try:
        return _revert(ytop, before_switch=before_switch, progress=progress)
    finally:
        _INSTALL_LOCK.release()


def _revert(
    ytop: Path, *, before_switch: Callable[[], None] | None, progress: Progress | None,
) -> InstallResult:
    target = ytop.parent
    staging = target / MAKING_SRC
    name = product.NAME
    found = previous_version(ytop)
    if found is None:
        return InstallResult(
            "failed", "", f"This stick keeps no previous {name} version to go back to.",
        )
    version = found.version
    locked = False
    try:
        try:
            ms._acquire_lock(target)
            locked = True
            if previous_version(ytop) != found:  # changed while waiting
                raise InstallError("the previous version changed; look again")
            _stage_revert(ytop, progress)
            plan = ms.plan_self_upgrade(target, staging)
            _say(progress, "Stopping downloads and saving…")
            if before_switch is not None:
                before_switch()
        except InstallAborted as exc:
            remove_tree_best_effort(staging)
            return InstallResult("failed", version, str(exc))
        except (InstallError, UpdateError, ms.MakeStickError, OSError, ValueError) as exc:
            remove_tree_best_effort(staging)
            log.warning("revert to %s not done: %s", version, exc)
            return InstallResult(
                "failed", version, f"{name} couldn't be reverted: {exc}",
            )
        return _apply(ytop, plan, progress, version=version,
                      build_id=found.build_id, revert=True)
    finally:
        if locked:
            ms._release_lock(target)


# --- the app's view ----------------------------------------------------------------


class AppUpdates:
    """App updates as the web app uses them: the question, the offer, one
    install at a time.  ``check``/``install`` default to this module's;
    tests pass fakes."""

    def __init__(
        self,
        ytop: Path | None,
        config: Callable[[], Config],
        library_root: Path | None,
        *,
        check: Callable[..., AppCheck] | None = None,
        install: Callable[..., InstallResult] | None = None,
        revert: Callable[..., InstallResult] | None = None,
        update_components: Callable[[list[str]], UpdateResult] | None = None,
        http: Callable[[], Http] = Http,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.ytop = ytop if ytop is not None and (ytop / "build.json").is_file() else None
        self.config = config
        self.library_root = library_root
        self._check = check or check_app_result
        self._install = install or install_app
        self._revert = revert or revert_app
        self.update_components = update_components
        self._http = http
        self.clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self.online = False
        self.last: AppCheck | None = None
        self.offer: AppRelease | None = None
        self.phase = "idle"  # idle | installing | updated | reverted | undone | failed
        self.kind = "update"  # what is (or was) installing: update | revert
        self.target_version = ""  # the version being put in place
        self.message = ""
        self.progress = ""
        self.result: InstallResult | None = None
        # Set once the final state was shown, so the app may stop.
        self.shown = threading.Event()

    @classmethod
    def for_runtime(cls, config: Callable[[], Config], library_root: Path | None) -> AppUpdates:
        updates = cls(stick_image(), config, library_root)
        ytop = updates.ytop
        if ytop is not None:  # in the background: never delays the start
            threading.Thread(target=tidy_previous, args=(ytop,),
                             name="ytop-tidy-previous", daemon=True).start()
        return updates

    @property
    def available(self) -> bool:
        return self.ytop is not None

    @property
    def finished(self) -> bool:
        return self.phase in ("updated", "reverted", "undone")

    @property
    def busy(self) -> bool:
        return self.phase == "installing" or self.finished

    def version(self) -> str:
        if self.ytop is None:
            return ""
        try:
            return str(_stick_build(self.ytop).get("version") or "")
        except UpdateError:
            return ""

    def asking(self) -> bool:
        """Ask now: on a stick, online, and never answered."""
        return (
            self.available and self.online and self.library_root is not None
            and self.config().updates.check_app is None
        )

    def answer(self, yes: bool) -> None:
        if self.library_root is None:
            raise ValueError("no library is connected")
        set_updates_option(self.library_root, "check_app", yes)
        if yes and self.online:
            self._record_check(self.check())

    def on_start_outcome(self, outcome: str) -> None:
        """The start-up component check finished with ``outcome``: the
        network answered, so ask (once), or look for a release when the
        stick said yes.  With component checks off there is no answer to go
        by; a stick that said yes is checked anyway (that is its consent)."""
        self.online = outcome in ONLINE_OUTCOMES
        if not self.available or self.config().updates.check_app is not True:
            return
        self._restore_offer()  # a kept offer shows at once, even offline
        if self.online or outcome == "off":
            self._record_check(self.check())

    def recheck_if_due(self) -> None:
        """Look again while the app stays open, once ``CHECK_EVERY`` has
        passed since the last answer.  Never raises."""
        try:
            if (not self.available or self.config().updates.check_app is not True
                    or self.busy or not self._check_due()):
                return
            self._record_check(self.check())
        except Exception:
            log.warning("the app update re-check failed", exc_info=True)

    def _record_check(self, result: AppCheck) -> None:
        """GitHub answered (a broken release or a rate limit is an answer
        too): the next check is due ``CHECK_EVERY`` from now."""
        if result.status == "offline":
            return
        self.online = True
        if self.library_root is None:
            return
        try:
            set_updates_option(self.library_root, "last_check", self.clock().isoformat())
        except (OSError, ValueError):
            log.warning("could not record the app update check", exc_info=True)

    def _check_due(self) -> bool:
        raw = self.config().updates.last_check
        if not raw:
            return True
        try:
            last = datetime.fromisoformat(raw)
        except ValueError:
            return True
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        now = self.clock()
        return not (timedelta(0) <= now - last < CHECK_EVERY)

    def check(self) -> AppCheck:
        """Look for a release now ("Check now" too; never while installing)."""
        if self.busy:
            raise ValueError("An update is being installed.")
        if self.ytop is None:
            result = AppCheck("error", message=f"{product.NAME} is not running from a stick")
        else:
            result = self._check(self.ytop, self.config(), self._http())
        with self._lock:
            self.last = result
            if not self.busy and result.status not in NO_ANSWER:
                self.offer = result.release  # no answer leaves a shown offer up
        self._keep(result)
        return result

    def _keep(self, result: AppCheck) -> None:
        """Keep an offer for the next starts; forget it once the check says
        there is none.  (No answer from GitHub changes nothing.)"""
        if self.ytop is None or self.library_root is None:
            return
        try:
            if result.status == "available" and result.release is not None:
                save_offer(self.library_root, self.ytop, result.release, self.clock())
            elif result.status in ("up_to_date", "unsupported"):
                drop_offer(self.library_root)
        except (OSError, ValueError, UpdateError):
            log.warning("could not keep the app update offer", exc_info=True)

    def _restore_offer(self) -> bool:
        """Show the offer a check kept less than ``CHECK_EVERY`` ago."""
        if self.ytop is None or self.library_root is None or self.busy:
            return False
        release = load_offer(self.library_root, self.ytop, self.clock())
        if release is None:
            return False
        with self._lock:
            if self.busy:
                return False
            self.last = AppCheck("available", release)
            self.offer = release
        return True

    def previous(self) -> Previous | None:
        """The version "Revert to previous version" puts back, if any."""
        return previous_version(self.ytop) if self.ytop is not None else None

    def start_install(
        self,
        build_id: str,
        *,
        before_switch: Callable[[], None],
        finished: Callable[[InstallResult], None],
    ) -> None:
        """Install the offered release whose ``build_id`` the user confirmed,
        in a thread; ``finished`` gets the result."""
        with self._lock:
            if self.busy:
                raise ValueError("An update is already being installed.")
            offer = self.offer
            if self.ytop is None or offer is None or offer.build_id != build_id:
                raise ValueError("That update is no longer offered; check again.")
            self._begin("update", offer.version)
        ytop = self.ytop
        extra: dict[str, Any] = {}
        if self.update_components is not None:
            extra["update_components"] = self.update_components

        def work(stop: Callable[[], None], say: Progress) -> InstallResult:
            return self._install(ytop, self.config(), offer,
                                 before_switch=stop, progress=say, **extra)

        self._run(work, before_switch, offer.version, finished,
                  "The update couldn't be installed", "ytop-app-update")

    def start_revert(
        self,
        build_id: str,
        *,
        before_switch: Callable[[], None],
        finished: Callable[[InstallResult], None],
    ) -> None:
        """Put back the previous version whose ``build_id`` the user
        confirmed, in a thread; ``finished`` gets the result."""
        with self._lock:
            if self.busy:
                raise ValueError("An update is already being installed.")
            found = self.previous()
            if self.ytop is None or found is None or found.build_id != build_id:
                raise ValueError("That version is no longer on this stick.")
            self._begin("revert", found.version)
        ytop = self.ytop

        def work(stop: Callable[[], None], say: Progress) -> InstallResult:
            return self._revert(ytop, before_switch=stop, progress=say)

        self._run(work, before_switch, found.version, finished,
                  f"{product.NAME} couldn't be reverted", "ytop-app-revert")

    def _begin(self, kind: str, version: str) -> None:
        self.phase, self.message, self.progress = "installing", "", ""
        self.kind, self.target_version = kind, version

    def _run(
        self,
        work: Callable[[Callable[[], None], Progress], InstallResult],
        before_switch: Callable[[], None],
        version: str,
        finished: Callable[[InstallResult], None],
        failed: str,
        thread_name: str,
    ) -> None:
        stopped = {"done": False}

        def stop() -> None:
            before_switch()
            stopped["done"] = True

        def say(label: str) -> None:
            self.progress = label

        def run() -> None:
            try:
                result = work(stop, say)
            except Exception as exc:  # noqa: BLE001 - never kills the thread silently
                log.exception("app %s failed", self.kind)
                result = InstallResult("failed", version, f"{failed}: {exc}",
                                       stopped=stopped["done"])
            if stopped["done"] and not result.stopped:
                result = InstallResult(result.status, result.version, result.message, True)
            with self._lock:
                self.result = result
                self.phase = result.status if result.status in (
                    "updated", "reverted", "undone",
                ) else ("undone" if result.stopped else "failed")
                self.message = result.message
            finished(result)

        threading.Thread(target=run, name=thread_name, daemon=True).start()

    def status(self) -> dict[str, Any]:
        """What the pages show."""
        last = self.last
        return {
            "available": self.available,
            "version": self.version(),
            "check_app": self.config().updates.check_app,
            "offer": self.offer,
            "last_status": last.status if last is not None else "",
            "last_message": last.message if last is not None else "",
            "phase": self.phase,
            "kind": self.kind,
            "previous": self.previous() if not self.busy else None,
            # Whether installing the offer keeps a copy to revert to.
            "revertible": self.ytop is not None and keeps_previous(self.ytop),
            "message": self.message,
            "progress": self.progress,
        }
