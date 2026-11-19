"""The component updater: keeps a stick's YouTube-facing parts working.

It fetches upstream releases, verifies them against their published
checksums, smoke-tests them with the stick's own Python, and only then
switches them in, with one atomic ``current.json`` write per batch (see
:mod:`ytop.components` for the store).

Sources (``SOURCES``):

- **yt-dlp**: the GitHub release's ``yt_dlp-<v>-py3-none-any.whl``,
  verified by the release's ``SHA2-256SUMS``.
- **yt-dlp-ejs**, **youtube-transcript-api**, **bgutil plugin**: PyPI wheels,
  verified by PyPI's sha256 digests.
- **bgutil server**: the GitHub source tarball at the plugin's tag.  A
  generated tag archive has no published checksum; it is taken over TLS and
  must say it is the plugin's version (``server/package.json``).  If the
  release ever carries the tarball as an asset with a GitHub digest, that
  asset is used and verified instead.
- **Deno**: ``denoland/deno`` release zips for every platform the stick
  carries, verified by their ``.sha256sum`` assets.  Deno is updated only
  when the newest yt-dlp-ejs (``Requires-External: deno (>=X)`` in its
  metadata) or yt-dlp (``DenoJsRuntime.MIN_SUPPORTED_VERSION``) needs a newer
  one than the stick has, or when asked for explicitly.

Network steps never raise to callers: no network is ``"offline"``, a
GitHub/PyPI rate-limit answer is ``"rate_limited"``.  Requests are anonymous,
with a generic ``User-Agent``.  Triggers and UI are elsewhere (Task 5).
"""

from __future__ import annotations

import compileall
import hashlib
import http.client
import json
import os
import py_compile
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from . import product, sticklock
from .bgserver import ServerError, ServerInstall, deno_install, prepare_server
from .components import (
    BGUTIL_SERVER,
    COMPONENT_DISTS,
    DENO,
    PYTHON_COMPONENTS,
    Current,
    _clear_temps,
    _component_dir,
    _image_deno_version,
    _prune,
    _usable,
    _version_key,
    deno_exe,
    is_pinned,
    is_prerelease,
    mark_complete,
    move_in,
    read_current,
    server_home,
    store_dir,
    version_dir,
    write_current,
)
from .config import Config
from .stick.boot import COMPONENTS_DIR, LOCK

__all__ = [
    "COMPONENT_DISTS",
    "DENO_TRIPLES",
    "SOURCES",
    "Available",
    "CheckResult",
    "Endpoints",
    "Http",
    "Release",
    "SmokeOutcome",
    "SmokeRequest",
    "UpdateResult",
    "check",
    "deno_minimum",
    "rollback",
    "run_smoke",
    "update",
]

USER_AGENT = "YTOP-updater"
CONNECT_TIMEOUT = 10  # seconds
READ_TIMEOUT = 60
JSON_LIMIT = 20 * 1024 * 1024
DOWNLOAD_LIMIT = 600 * 1024 * 1024
UNPACK_LIMIT = 400 * 1024 * 1024  # one wheel, uncompressed

STAGING = ".staging"
YTDLP_REPO = "yt-dlp/yt-dlp"
DENO_REPO = "denoland/deno"
SERVER_REPO = "Brainicism/bgutil-ytdlp-pot-provider"
PYPI = "https://pypi.org"
GITHUB_WEB = "https://github.com"
# Deno's release asset per stick platform: deno-<triple>.zip (the same
# artifacts stick/versions.toml pins for the image's runtime).
DENO_TRIPLES = {
    "linux-x86_64": "x86_64-unknown-linux-gnu",
    "linux-aarch64": "aarch64-unknown-linux-gnu",
    "macos-arm64": "aarch64-apple-darwin",
    "macos-x86_64": "x86_64-apple-darwin",
    "windows-x86_64": "x86_64-pc-windows-msvc",
}
# What the smoke test imports for each Python component.
MODULES = {
    "yt-dlp": "yt_dlp",
    "yt-dlp-ejs": "yt_dlp_ejs",
    "bgutil-plugin": "yt_dlp_plugins.extractor.getpot_bgutil",
    "youtube-transcript-api": "youtube_transcript_api",
}
# Components whose change the online probe (a video's metadata) can judge.
PROBED = ("yt-dlp", "yt-dlp-ejs", "bgutil-plugin")
SMOKE_TIMEOUT = 120
PROBE_TIMEOUT = 180

_HEX64 = re.compile(r"\b[0-9a-fA-F]{64}\b")
_FAT_BAD = set('<>:"|?*\\') | {chr(c) for c in range(32)}

Progress = Callable[[str], None]


# --- errors ------------------------------------------------------------------------


class UpdateError(Exception):
    """A step failed (reported, never raised to callers of check/update)."""


class Offline(UpdateError):
    """No network (or it went away)."""


class RateLimited(UpdateError):
    """GitHub or PyPI asked us to slow down."""


class HttpError(UpdateError):
    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class ChecksumError(UpdateError):
    """A download does not match its published checksum."""


# --- HTTP ----------------------------------------------------------------------------


class Http:
    """A small urllib client: anonymous, ``User-Agent: YTOP-updater``, 10 s
    to connect (and get an answer), 60 s for each read after that."""

    def __init__(self, *, connect_timeout: float = CONNECT_TIMEOUT,
                 read_timeout: float = READ_TIMEOUT) -> None:
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self._opener = urllib.request.build_opener(_SafeRedirect())
        self._opener.addheaders = []  # no "Python-urllib/3.12"

    def _open(self, url: str) -> http.client.HTTPResponse:
        _require_https(url)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            response = self._opener.open(request, timeout=self.connect_timeout)
        except urllib.error.HTTPError as exc:
            error = _http_error(exc, url)
            exc.close()
            raise error from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ssl.SSLError):
                raise UpdateError(f"{url}: secure connection failed: {exc.reason}") from None
            raise Offline(f"{url}: {exc.reason}") from None
        except (OSError, http.client.HTTPException) as exc:
            raise Offline(f"{url}: {exc}") from None
        try:  # the socket under the buffered reader
            response.fp.raw._sock.settimeout(self.read_timeout)
        except AttributeError:
            pass
        return response

    @staticmethod
    def _read(response: http.client.HTTPResponse, size: int) -> bytes:
        try:
            return response.read(size)
        except (OSError, http.client.HTTPException) as exc:
            raise Offline(f"the connection was cut off: {exc}") from None

    def get_bytes(self, url: str, limit: int = JSON_LIMIT) -> bytes:
        with self._open(url) as response:
            data = self._read(response, limit + 1)
        if len(data) > limit:
            raise UpdateError(f"{url}: answer too large")
        return data

    def get_json(self, url: str) -> Any:
        try:
            return json.loads(self.get_bytes(url))
        except ValueError:
            raise UpdateError(f"{url}: not JSON") from None

    def download(self, url: str, dest: Path, limit: int = DOWNLOAD_LIMIT) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        total = 0
        with self._open(url) as response, open(dest, "wb") as out:
            while chunk := self._read(response, 1 << 20):
                total += len(chunk)
                if total > limit:
                    raise UpdateError(f"{url}: download too large")
                out.write(chunk)


_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


def _require_https(url: str) -> None:
    """Only https, so checksums and the files they vouch for are both
    authentic; plain http only to this machine (test servers)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return
    if parts.scheme == "http" and (parts.hostname or "").lower() in _LOOPBACK:
        return
    raise UpdateError(f"refusing {url}: updates are fetched over https only")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to an address ``_require_https`` allows, and
    never from https down to http."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = urllib.parse.urljoin(req.full_url, newurl)
        if req.full_url.startswith("https:") and not new.startswith("https:"):
            raise UpdateError(f"refusing a redirect from https to {new}: https only")
        _require_https(new)
        return super().redirect_request(req, fp, code, msg, headers, new)


def _http_error(exc: urllib.error.HTTPError, url: str) -> UpdateError:
    headers = exc.headers
    remaining = headers.get("X-RateLimit-Remaining") if headers else None
    retry = headers.get("Retry-After") if headers else None
    if exc.code in (403, 429) and (remaining == "0" or retry):
        return RateLimited(f"{url}: rate limited (HTTP {exc.code})")
    return HttpError(f"{url}: HTTP {exc.code}", exc.code)


# --- releases ------------------------------------------------------------------------


@dataclass(frozen=True)
class Endpoints:
    github_api: str = product.API_BASE
    github_web: str = GITHUB_WEB
    pypi: str = PYPI


def endpoints_for(config: Config) -> Endpoints:
    """``[updates] api_base`` (a fork or test server) is the GitHub API."""
    return Endpoints(github_api=product.api_base(config).rstrip("/"))


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    sha256: str | None = None  # published digest
    sums_url: str | None = None  # a checksum file listing it


@dataclass(frozen=True)
class Release:
    name: str
    version: str
    assets: dict[str, Asset]  # "wheel", "tarball", or a platform key
    verified_by: str


def choose(versions: Iterable[str], current: str | None, pinned: Iterable[str]) -> str | None:
    """The newest version newer than ``current``: never one pinned bad, and
    never a pre-release unless ``current`` is one."""
    allow_pre = current is not None and is_prerelease(current)
    bad = {_version_key(v) for v in pinned}
    ok = [
        v for v in versions
        if _version_key(v) not in bad and (allow_pre or not is_prerelease(v))
    ]
    if not ok:
        return None
    best = max(ok, key=_version_key)
    if current is not None and _version_key(best) <= _version_key(current):
        return None
    return best


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _gh_assets(release: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        a["name"]: a for a in _dicts(release.get("assets"))
        if isinstance(a.get("name"), str) and isinstance(a.get("browser_download_url"), str)
    }


def _gh_releases(http: Http, ep: Endpoints, repo: str, current: str | None) -> list[dict[str, Any]]:
    doc = http.get_json(f"{ep.github_api}/repos/{repo}/releases?per_page=15")
    if not isinstance(doc, list):
        raise UpdateError(f"{repo}: unexpected releases answer")
    allow_pre = current is not None and is_prerelease(current)
    return [
        r for r in _dicts(doc)
        if not r.get("draft") and (allow_pre or not r.get("prerelease"))
        and isinstance(r.get("tag_name"), str)
    ]


@dataclass(frozen=True)
class Source:
    name: str

    def latest(self, http: Http, ep: Endpoints, *, current: str | None,
               pinned: list[str], platforms: list[str]) -> Release | None:
        raise NotImplementedError


_YTDLP_WHEEL = re.compile(r"yt_dlp-([^-]+)-py3-none-any\.whl")


@dataclass(frozen=True)
class YtDlpSource(Source):
    def latest(self, http: Http, ep: Endpoints, *, current: str | None,
               pinned: list[str], platforms: list[str]) -> Release | None:
        options: dict[str, Release] = {}
        for release in _gh_releases(http, ep, YTDLP_REPO, current):
            assets = _gh_assets(release)
            wheels = [m for m in map(_YTDLP_WHEEL.fullmatch, assets) if m]
            sums = assets.get("SHA2-256SUMS")
            if len(wheels) != 1 or sums is None:
                continue
            name, version = wheels[0].group(0), wheels[0].group(1)
            options.setdefault(version, Release(self.name, version, {
                "wheel": Asset(name, assets[name]["browser_download_url"],
                               sums_url=sums["browser_download_url"]),
            }, "the release's SHA2-256SUMS"))
        version = choose(options, current, pinned)
        return None if version is None else options[version]


@dataclass(frozen=True)
class PyPISource(Source):
    dist: str = ""

    def latest(self, http: Http, ep: Endpoints, *, current: str | None,
               pinned: list[str], platforms: list[str]) -> Release | None:
        doc = http.get_json(f"{ep.pypi}/pypi/{self.dist}/json")
        releases = doc.get("releases") if isinstance(doc, dict) else None
        if not isinstance(releases, dict):
            raise UpdateError(f"{self.dist}: unexpected PyPI answer")
        options: dict[str, Release] = {}
        for version, files in releases.items():
            for item in _dicts(files):
                filename, url = item.get("filename"), item.get("url")
                digests = item.get("digests")
                sha = digests.get("sha256") if isinstance(digests, dict) else None
                if (
                    isinstance(filename, str) and isinstance(url, str)
                    and filename.endswith(("-py3-none-any.whl", "-py2.py3-none-any.whl"))
                    and not item.get("yanked")
                    and isinstance(sha, str) and _HEX64.fullmatch(sha)
                ):
                    options[version] = Release(self.name, version, {
                        "wheel": Asset(filename, url, sha256=sha),
                    }, "PyPI's sha256 digest")
                    break
        version = choose(options, current, pinned)
        return None if version is None else options[version]


@dataclass(frozen=True)
class DenoSource(Source):
    def latest(self, http: Http, ep: Endpoints, *, current: str | None,
               pinned: list[str], platforms: list[str]) -> Release | None:
        unknown = [p for p in platforms if p not in DENO_TRIPLES]
        if unknown:
            raise UpdateError(f"no Deno release asset is known for {', '.join(unknown)}")
        options: dict[str, Release] = {}
        for release in _gh_releases(http, ep, DENO_REPO, current):
            version = release["tag_name"].removeprefix("v")
            assets = _gh_assets(release)
            chosen: dict[str, Asset] = {}
            for plat in platforms:
                name = f"deno-{DENO_TRIPLES[plat]}.zip"
                if name in assets and name + ".sha256sum" in assets:
                    chosen[plat] = Asset(
                        name, assets[name]["browser_download_url"],
                        sums_url=assets[name + ".sha256sum"]["browser_download_url"],
                    )
            if len(chosen) == len(platforms):  # every platform the stick has
                options.setdefault(version, Release(
                    self.name, version, chosen, "each zip's .sha256sum",
                ))
        version = choose(options, current, pinned)
        return None if version is None else options[version]


SOURCES: dict[str, Source] = {
    "yt-dlp": YtDlpSource("yt-dlp"),
    "yt-dlp-ejs": PyPISource("yt-dlp-ejs", dist="yt-dlp-ejs"),
    "bgutil-plugin": PyPISource("bgutil-plugin", dist="bgutil-ytdlp-pot-provider"),
    "youtube-transcript-api": PyPISource("youtube-transcript-api", dist="youtube-transcript-api"),
    DENO: DenoSource(DENO),
}


def server_release(http: Http, ep: Endpoints, version: str) -> Release:
    """The bgutil server source at the plugin's tag (see the module doc)."""
    tarball = Asset(
        f"{version}.tar.gz",
        f"{ep.github_web}/{SERVER_REPO}/archive/refs/tags/{version}.tar.gz",
    )
    verified = "the plugin's PyPI version match (server/package.json) over TLS"
    try:
        release = http.get_json(f"{ep.github_api}/repos/{SERVER_REPO}/releases/tags/{version}")
    except HttpError:
        release = None
    if isinstance(release, dict):
        names = (f"{version}.tar.gz", f"bgutil-ytdlp-pot-provider-{version}.tar.gz")
        for name, asset in _gh_assets(release).items():
            digest = asset.get("digest")
            if name in names and isinstance(digest, str) and digest.startswith("sha256:"):
                tarball = Asset(name, asset["browser_download_url"],
                                sha256=digest.removeprefix("sha256:"))
                verified = "GitHub's asset digest"
                break
    return Release(BGUTIL_SERVER, version, {"tarball": tarball}, verified)


# --- what the stick has -----------------------------------------------------------


def stick_platforms(ytop: Path) -> list[str]:
    """The platforms the stick carries (its ``build.json``)."""
    try:
        build = json.loads((ytop / "build.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise UpdateError("this is not a YTOP stick (no readable build.json)") from None
    platforms = build.get("platforms") if isinstance(build, dict) else None
    if not isinstance(platforms, list) or not all(isinstance(p, str) for p in platforms):
        raise UpdateError("build.json lists no platforms")
    return list(platforms)


def _norm(dist: str) -> str:
    return re.sub(r"[-_.]+", "_", dist).lower()


def _dist_infos(folder: Path, dist: str) -> list[tuple[Path, str]]:
    """(dist-info folder, version) for ``dist`` directly in ``folder``."""
    prefix = _norm(dist) + "-"
    found = []
    try:
        entries = list(folder.iterdir())
    except OSError:
        return []
    for entry in entries:
        name = entry.name
        if name.lower().endswith(".dist-info") and name.lower().startswith(prefix):
            version = name[len(prefix):-len(".dist-info")]
            if version and "-" not in version and entry.is_dir():
                found.append((entry, version))
    return found


def _lib_common(ytop: Path) -> Path:
    return ytop / "lib" / "common"


def _current_folders(ytop: Path, current: Current) -> dict[str, Path]:
    """Each Python component's usable current store folder."""
    out = {}
    for name in PYTHON_COMPONENTS:
        version = current.current.get(name)
        folder = _component_dir(ytop, name, version) if version else None
        if folder is not None and _usable(name, folder):
            out[name] = folder
    return out


def installed_versions(ytop: Path) -> dict[str, str]:
    """What the stick runs now: the store's current versions, else the
    image's (``lib/common``, the runtime's Deno)."""
    current = read_current(ytop)
    folders = _current_folders(ytop, current)
    out: dict[str, str] = {}
    for name in PYTHON_COMPONENTS:
        if name in folders:
            out[name] = current.current[name]
        else:
            infos = _dist_infos(_lib_common(ytop), COMPONENT_DISTS[name])
            if len(infos) == 1:
                out[name] = infos[0][1]
    deno = current.current.get(DENO) or _image_deno_version(ytop)
    if deno:
        out[DENO] = deno
    if BGUTIL_SERVER in current.current and server_home(ytop) is not None:
        out[BGUTIL_SERVER] = current.current[BGUTIL_SERVER]
    return out


def _source_folder(ytop: Path, current: Current, name: str) -> Path:
    return _current_folders(ytop, current).get(name, _lib_common(ytop))


_REQUIRES_DENO = re.compile(
    r"^deno\s*\(?\s*>=\s*([0-9][0-9A-Za-z.+!-]*)\s*\)?\s*(?:;.*)?$", re.IGNORECASE,
)
_DENO_CLASS = re.compile(r"^class DenoJsRuntime\b.*?(?=^class |\Z)", re.MULTILINE | re.DOTALL)
_MIN_VERSION = re.compile(r"MIN_SUPPORTED_VERSION\s*=\s*\(\s*([0-9][0-9,\s]*)\)")


def deno_minimum(folders: dict[str, Path]) -> str | None:
    """The newest minimum Deno that yt-dlp-ejs (its metadata's
    ``Requires-External: deno (>=X)``) or yt-dlp (``DenoJsRuntime.
    MIN_SUPPORTED_VERSION`` in ``yt_dlp/utils/_jsruntime.py``) declares, in
    the given install folders; None when neither declares one."""
    found: list[str] = []
    ejs = folders.get("yt-dlp-ejs")
    if ejs is not None:
        for info, _version in _dist_infos(ejs, "yt-dlp-ejs"):
            try:
                text = (info / "METADATA").read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.split("\n\n", 1)[0].splitlines():  # the headers
                key, _, value = line.partition(":")
                if key.strip().lower() == "requires-external":
                    match = _REQUIRES_DENO.match(value.strip())
                    if match:
                        found.append(match.group(1))
    ytdlp = folders.get("yt-dlp")
    if ytdlp is not None:
        try:
            text = (ytdlp / "yt_dlp/utils/_jsruntime.py").read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            text = ""
        block = _DENO_CLASS.search(text)
        match = _MIN_VERSION.search(block.group(0)) if block else None
        if match:
            numbers = [n.strip() for n in match.group(1).split(",") if n.strip()]
            found.append(".".join(numbers))
    return max(found, key=_version_key) if found else None


# --- check -------------------------------------------------------------------------


@dataclass(frozen=True)
class Available:
    name: str
    current: str | None
    latest: str
    # Deno: whether yt-dlp/ejs need it (an update without names takes it
    # only then).  Always True for the others.
    required: bool = True


@dataclass
class CheckResult:
    status: str  # ok | offline | rate_limited | error
    available: list[Available] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    checked_at: str = ""
    message: str = ""


def _deno_needed(minimum: str | None, have: str | None) -> bool:
    if minimum is None:
        return False
    return have is None or _version_key(minimum) > _version_key(have)


def check(
    ytop: Path,
    config: Config,
    *,
    now: datetime | None = None,
    http: Http | None = None,
    endpoints: Endpoints | None = None,
) -> CheckResult:
    """The updates available for the stick whose ``.ytop`` is ``ytop``.

    Never raises; records the time and outcome in ``current.json``."""
    now = now or datetime.now(UTC)
    http = http or Http()
    ep = endpoints or endpoints_for(config)
    result = CheckResult("ok", checked_at=now.isoformat())
    try:
        current = read_current(ytop)
        installed = installed_versions(ytop)
        try:
            platforms = stick_platforms(ytop)
        except UpdateError:
            platforms = []
        names = [*PYTHON_COMPONENTS, *([DENO] if platforms else [])]
        for name in names:
            try:
                release = SOURCES[name].latest(
                    http, ep, current=installed.get(name),
                    pinned=current.pinned_bad.get(name, []), platforms=platforms,
                )
            except (Offline, RateLimited):
                raise
            except UpdateError as exc:
                result.errors[name] = str(exc)
                continue
            if release is None:
                continue
            required = True
            if name == DENO:
                folders = {n: _source_folder(ytop, current, n) for n in ("yt-dlp", "yt-dlp-ejs")}
                required = _deno_needed(deno_minimum(folders), installed.get(DENO))
            result.available.append(Available(name, installed.get(name), release.version, required))
            if name == "bgutil-plugin" and BGUTIL_SERVER in current.current:
                result.available.append(Available(
                    BGUTIL_SERVER, installed.get(BGUTIL_SERVER), release.version,
                ))
        # A server out of step with the plugin that runs (a plugin rollback
        # keeps the newer server; a server folder can go missing): the next
        # update brings back one matching the plugin, so say so.
        plugin = installed.get("bgutil-plugin")
        if (
            BGUTIL_SERVER in current.current and plugin
            and installed.get(BGUTIL_SERVER) != plugin
            and not any(a.name == BGUTIL_SERVER for a in result.available)
        ):
            result.available.append(Available(
                BGUTIL_SERVER, installed.get(BGUTIL_SERVER), plugin,
            ))
        if result.errors and len(result.errors) == len(names):
            result.status = "error"
            result.message = "; ".join(result.errors.values())
    except Offline as exc:
        result = CheckResult("offline", checked_at=now.isoformat(), message=str(exc))
    except RateLimited as exc:
        result = CheckResult("rate_limited", checked_at=now.isoformat(), message=str(exc))
    except (UpdateError, OSError) as exc:
        result = CheckResult("error", checked_at=now.isoformat(), message=str(exc))
    try:
        record = read_current(ytop)
        record.last_check, record.last_result = result.checked_at, result.status
        write_current(ytop, record)
    except OSError:
        pass  # a read-only stick: the check still stands
    return result


# --- staging -----------------------------------------------------------------------


def _staging(ytop: Path) -> Path:
    return store_dir(ytop) / STAGING


def _clear_staging(ytop: Path) -> None:
    shutil.rmtree(_staging(ytop), ignore_errors=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _sum_for(text: str, name: str, *, single: bool) -> str:
    """``name``'s sha256 in a checksum file (``<hash>  <name>`` lines; a
    single-file ``.sha256sum`` may be in another layout: its hash)."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and _HEX64.fullmatch(parts[0]) and parts[-1].lstrip("*") == name:
            return parts[0].lower()
    if single:
        match = _HEX64.search(text)
        if match:
            return match.group(0).lower()
    raise ChecksumError(f"{name}: no published checksum")


def _verify(http: Http, asset: Asset, path: Path, *, single: bool = False) -> None:
    if asset.sha256 is not None:
        expected = asset.sha256.lower()
    elif asset.sums_url is not None:
        text = http.get_bytes(asset.sums_url, limit=1 << 20).decode("utf-8", "replace")
        expected = _sum_for(text, asset.name, single=single)
    else:
        raise ChecksumError(f"{asset.name}: no published checksum")
    actual = _sha256(path)
    if actual != expected:
        raise ChecksumError(
            f"{asset.name}: checksum mismatch (published {expected[:12]}…, got {actual[:12]}…)"
        )


def _safe_filename(name: str) -> str:
    base = PurePosixPath(name).name
    if not base or base in (".", "..") or set(base) & _FAT_BAD:
        raise UpdateError(f"unusable file name {name!r}")
    return base


def _safe_member(name: str) -> bool:
    if name.startswith("/") or "\\" in name:
        return False
    parts = name.split("/")
    for part in parts:
        if part in ("", ".", "..") or set(part) & _FAT_BAD or part[-1] in ". ":
            return False
    return True


def unpack_wheel(wheel: Path, dest: Path, dist: str, version: str) -> None:
    """Install a pure-Python wheel's importable files (packages and its
    dist-info; no ``.data`` scripts or data, no bytecode) into ``dest``."""
    try:
        with zipfile.ZipFile(wheel) as zf:
            members: list[zipfile.ZipInfo] = []
            lower: set[str] = set()
            total = 0
            for info in zf.infolist():
                name = info.filename
                if name.endswith("/"):
                    continue
                if not _safe_member(name):
                    raise UpdateError(f"{wheel.name}: unsafe path {name!r}")
                parts = name.split("/")
                if parts[0].endswith(".data") or "__pycache__" in parts or name.endswith(".pyc"):
                    continue
                if name.lower() in lower:
                    raise UpdateError(f"{wheel.name}: names differ only by case: {name!r}")
                lower.add(name.lower())
                total += info.file_size
                if total > UNPACK_LIMIT:
                    raise UpdateError(f"{wheel.name}: too large unpacked")
                members.append(info)
            infos = {m.filename.split("/")[0] for m in members
                     if m.filename.split("/")[0].endswith(".dist-info")}
            prefix = _norm(dist) + "-"
            matching = [
                i for i in infos if i.lower().startswith(prefix)
                and _version_key(i[len(prefix):-len(".dist-info")]) == _version_key(version)
            ]
            if len(infos) != 1 or len(matching) != 1:
                raise UpdateError(f"{wheel.name}: not a wheel of {dist} {version}")
            for info in members:
                target = dest / info.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(info))
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError,
            NotImplementedError, RuntimeError) as exc:
        # NotImplementedError: a compression zipfile lacks; RuntimeError:
        # an encrypted member.
        raise UpdateError(f"{wheel.name}: unusable wheel: {exc}") from None


def compile_bytecode(folder: Path, name: str, version_folder: str) -> None:
    """Ship bytecode, as the build does: unchecked-hash pycs whose
    ``co_filename`` reads ``.ytop/components/<name>/<version>/...`` (the
    folder's path once switched in), so Python never writes ``__pycache__``
    onto the stick.  The stick's Python is CPython 3.12, which runs this."""
    if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 12):
        return  # not the stick's Python: its pycs would not be used
    compileall.compile_dir(
        folder, quiet=2, workers=1,
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
        stripdir=str(folder),
        prependdir=f".ytop/{COMPONENTS_DIR}/{name}/{version_folder}",
    )


@dataclass
class _Staged:
    name: str
    version: str
    folder: Path  # ready to be renamed into the store


def _stage_dir(ytop: Path, name: str, version: str) -> Path:
    folder = version_dir(version)
    if folder is None:
        raise UpdateError(f"{name}: unusable version {version!r}")
    stage = _staging(ytop) / f"{name}-{folder}"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    return stage


def _say(progress: Progress | None, label: str) -> None:
    if progress is not None:
        progress(label)


def _stage_python(ytop: Path, http: Http, release: Release, progress: Progress | None) -> _Staged:
    name, version = release.name, release.version
    stage = _stage_dir(ytop, name, version)
    asset = release.assets["wheel"]
    _say(progress, f"Downloading {name} {version}…")
    wheel = stage / "download" / _safe_filename(asset.name)
    http.download(asset.url, wheel)
    _verify(http, asset, wheel)
    ready = stage / "ready"
    unpack_wheel(wheel, ready, COMPONENT_DISTS[name], version)
    wheel.unlink()
    folder = version_dir(version)
    assert folder is not None
    compile_bytecode(ready, name, folder)
    mark_complete(ready)
    return _Staged(name, version, ready)


def _stage_deno(ytop: Path, http: Http, release: Release, platforms: list[str],
                progress: Progress | None) -> _Staged:
    stage = _stage_dir(ytop, DENO, release.version)
    ready = stage / "ready"
    for plat in platforms:
        asset = release.assets[plat]
        _say(progress, f"Downloading Deno {release.version} for {plat}…")
        archive = stage / "download" / _safe_filename(asset.name)
        http.download(asset.url, archive)
        _verify(http, asset, archive, single=True)
        exe = deno_exe(plat)
        try:
            with zipfile.ZipFile(archive) as zf:
                info = zf.getinfo(exe)
                if info.file_size > DOWNLOAD_LIMIT:
                    raise UpdateError(f"{asset.name}: too large unpacked")
                (ready / plat).mkdir(parents=True)
                with zf.open(info) as src, open(ready / plat / exe, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
        except KeyError:
            raise UpdateError(f"{asset.name}: holds no {exe}") from None
        except (zipfile.BadZipFile, EOFError, NotImplementedError, RuntimeError) as exc:
            raise UpdateError(f"{asset.name}: unusable zip: {exc}") from None
        archive.unlink()  # the stick has little room to spare
        if not plat.startswith("windows"):
            try:
                os.chmod(ready / plat / exe, 0o755)
            except OSError:
                pass  # FAT: the Mac runs it anyway, Linux from a cache copy
    return _Staged(DENO, release.version, ready)


def _host_deno() -> Path | None:
    from .toolchain import deno_path  # lazy: toolchain imports components

    try:
        return deno_path()
    except Exception:
        return None


def _dependencies(server: Path) -> object:
    try:
        package = json.loads((server / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return package.get("dependencies") if isinstance(package, dict) else None


def _server_installer(ytop: Path, install: ServerInstall | None) -> ServerInstall:
    """Fills a new server's ``node_modules``: copied from the current
    server when the dependencies are the same, else by ``deno install``
    (``install``, default :func:`ytop.bgserver.deno_install`) with a Deno
    that runs here."""

    def fill(_unused: Path, server: Path) -> None:
        current = server_home(ytop)
        if (
            current is not None and (current / "node_modules").is_dir()
            and _dependencies(current) == _dependencies(server)
        ):
            shutil.copytree(current / "node_modules", server / "node_modules",
                            copy_function=shutil.copyfile)
            return
        deno = _host_deno()
        if install is not None:
            install(deno or Path("deno"), server)
            return
        if deno is None:
            raise ServerError("no Deno that runs here to fetch the PO-token server's packages")
        deno_install(deno, server)

    return fill


def _stage_server(ytop: Path, http: Http, ep: Endpoints, version: str,
                  install: ServerInstall | None, progress: Progress | None) -> _Staged:
    release = server_release(http, ep, version)
    stage = _stage_dir(ytop, BGUTIL_SERVER, version)
    asset = release.assets["tarball"]
    _say(progress, f"Downloading the PO-token server {version}…")
    tarball = stage / "download" / "server.tar.gz"
    http.download(asset.url, tarball)
    if asset.sha256 is not None:
        _verify(http, asset, tarball)
    ready = stage / "ready"
    try:
        prepare_server(tarball, version, Path("deno"), ready, _server_installer(ytop, install))
    except ServerError as exc:
        raise UpdateError(str(exc)) from None
    except (OSError, ValueError, tarfile.TarError) as exc:
        raise UpdateError(f"the server could not be prepared: {exc}") from None
    tarball.unlink()
    mark_complete(ready)
    return _Staged(BGUTIL_SERVER, version, ready)


# --- the smoke test ---------------------------------------------------------------


@dataclass(frozen=True)
class SmokeRequest:
    python: str  # the stick's own Python
    paths: list[Path]  # sys.path: staged, then the current others, then lib/
    expect: dict[str, str]  # staged Python component -> version
    deno: Path | None  # this host's Deno (staged, or the current one)
    deno_version: str | None  # what it must say (staged), or None
    video: str  # [updates] smoke_video
    online: bool
    baseline: list[Path]  # sys.path of what runs now (for the probe)


@dataclass(frozen=True)
class SmokeOutcome:
    ok: bool
    message: str = ""


Smoke = Callable[[SmokeRequest], SmokeOutcome]

_IMPORT_SCRIPT = r"""
import importlib, importlib.metadata, json, sys
args = json.loads(sys.argv[1])
sys.path[0:0] = args["paths"]
errors, found = [], {}
for name, module in args["modules"].items():
    try:
        importlib.import_module(module)
    except BaseException as exc:
        errors.append(f"{name}: import {module} failed: {type(exc).__name__}: {exc}")
for name, dist in args["dists"].items():
    try:
        found[name] = importlib.metadata.version(dist)
    except Exception as exc:
        errors.append(f"{name}: no version: {type(exc).__name__}: {exc}")
if "yt-dlp" in args["modules"] and not errors:
    import yt_dlp.version
    found["yt_dlp.version"] = yt_dlp.version.__version__
print(json.dumps({"errors": errors, "found": found}))
"""

_PROBE_SCRIPT = r"""
import json, sys
args = json.loads(sys.argv[1])
sys.path[0:0] = args["paths"]
import yt_dlp
opts = {"quiet": True, "no_warnings": True, "skip_download": True,
        "noplaylist": True, "cachedir": False, "socket_timeout": 20}
if args["deno"]:
    opts["js_runtimes"] = {"deno": {"path": args["deno"]}}
with yt_dlp.YoutubeDL(opts) as ydl:
    info = ydl.extract_info("https://www.youtube.com/watch?v=" + args["video"], download=False)
print(json.dumps({"ok": bool(info) and info.get("id") == args["video"]
                  and bool(info.get("formats"))}))
"""


def _run_python(python: str, script: str, args: dict[str, Any], timeout: float) -> tuple[dict[str, Any] | None, str]:
    """Run ``script`` isolated (no site, no env, no bytecode writes)."""
    try:
        run = subprocess.run(
            [python, "-I", "-S", "-B", "-c", script, json.dumps(args)],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    lines = run.stdout.strip().splitlines()
    try:
        doc = json.loads(lines[-1]) if lines else None
    except ValueError:
        doc = None
    if run.returncode or not isinstance(doc, dict):
        tail = (run.stderr.strip().splitlines() or ["(no output)"])[-1]
        return None, tail
    return doc, ""


@contextmanager
def _runnable(deno: Path | None) -> Iterator[Path | None]:
    """``deno`` itself if it can be run, else a temporary executable copy
    (a Linux stick is mounted without exec)."""
    if deno is None or os.access(deno, os.X_OK) or sys.platform == "win32":
        yield deno
        return
    folder = tempfile.mkdtemp(prefix="ytop-deno-")
    try:
        copy = Path(folder) / deno.name
        try:
            shutil.copyfile(deno, copy)
            os.chmod(copy, 0o755)
        except OSError:
            yield None
            return
        yield copy
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _deno_says(deno: Path) -> tuple[str | None, str]:
    try:
        run = subprocess.run([str(deno), "--version"], capture_output=True, text=True,
                             timeout=60, encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    match = re.search(r"^deno (\S+)", run.stdout, re.MULTILINE)
    if run.returncode or not match:
        return None, (run.stderr.strip() or run.stdout.strip() or "no version")[-200:]
    return match.group(1), ""


def run_smoke(request: SmokeRequest) -> SmokeOutcome:
    """The default smoke test: every Python component imports (the staged
    ones in their new versions) in a subprocess of the stick's Python;
    this host's Deno answers ``--version``; and, online, yt-dlp reads a
    public video's metadata.  A probe that fails with what runs now too
    says nothing about the new versions (YouTube may be refusing this
    network) and is not held against them."""
    paths = [str(p) for p in request.paths]
    doc, error = _run_python(request.python, _IMPORT_SCRIPT, {
        "paths": paths, "modules": MODULES,
        "dists": {n: COMPONENT_DISTS[n] for n in request.expect},
    }, SMOKE_TIMEOUT)
    if doc is None:
        return SmokeOutcome(False, f"the import test did not run: {error}")
    errors = [str(e) for e in doc.get("errors", [])]
    found = doc.get("found", {})
    for name, want in request.expect.items():
        got = found.get(name)
        if isinstance(got, str) and _version_key(got) != _version_key(want):
            errors.append(f"{name}: {got} imported, {want} expected")
    if errors:
        return SmokeOutcome(False, "; ".join(errors))
    with _runnable(request.deno) as deno:
        if request.deno is not None:
            if deno is None:
                return SmokeOutcome(False, "Deno could not be made runnable here")
            said, error = _deno_says(deno)
            if said is None:
                return SmokeOutcome(False, f"deno --version failed: {error}")
            if request.deno_version and _version_key(said) != _version_key(request.deno_version):
                return SmokeOutcome(False, f"Deno says {said}, {request.deno_version} expected")
        probed = request.deno_version is not None or any(n in request.expect for n in PROBED)
        if request.online and request.video and probed:
            args = {"deno": str(deno) if deno else None, "video": request.video}
            new, error = _run_python(request.python, _PROBE_SCRIPT,
                                     {**args, "paths": paths}, PROBE_TIMEOUT)
            if not (new and new.get("ok")):
                old, _ = _run_python(request.python, _PROBE_SCRIPT,
                                     {**args, "paths": [str(p) for p in request.baseline]},
                                     PROBE_TIMEOUT)
                if old and old.get("ok"):
                    return SmokeOutcome(False, f"the new versions cannot read video "
                                               f"{request.video}: {error or 'no formats'}")
                return SmokeOutcome(True, "probe inconclusive: it fails with the current versions too")
    return SmokeOutcome(True)


# --- update ------------------------------------------------------------------------


@dataclass
class UpdateResult:
    # updated | up_to_date | failed | offline | rate_limited | busy | error
    status: str
    updated: dict[str, tuple[str | None, str]] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    message: str = ""
    deno_minimum: str | None = None


_LOCK = threading.Lock()
STICK_BUSY = (
    "another YTOP is updating or upgrading this stick; try again when it is done"
)


class _StickBusy(Exception):
    """Another process holds the stick's lock."""


def update(
    ytop: Path,
    config: Config,
    names: list[str] | None = None,
    *,
    http: Http | None = None,
    smoke: Smoke = run_smoke,
    progress: Progress | None = None,
    endpoints: Endpoints | None = None,
    server_install: ServerInstall | None = None,
    python: str | None = None,
    now: datetime | None = None,
) -> UpdateResult:
    """Update the stick's components (``names``, default: every Python
    component, the bgutil server with its plugin, and Deno when required).

    Each is downloaded into ``components/.staging/<name>-<version>/``,
    verified, unpacked (Deno for every platform the stick has) and smoke
    tested; then those that passed are moved into the store and named in
    one atomic ``current.json`` write, and versions beyond each one's
    ``previous`` (Python components keep one; Deno and the server none)
    are deleted.  Never raises for the network or a bad download: when the
    network goes away (or rate-limits) part way, what was staged before is
    still switched in and the rest is reported failed."""
    if not _LOCK.acquire(blocking=False):
        return UpdateResult("busy", message="an update is already running")
    try:
        # The stick's lock too, unless this process holds it already (an app
        # update installing its components): a CLI update and a server's on
        # one stick must not clear each other's staging.
        stick_lock = ytop.parent / LOCK
        locked = False
        if not sticklock.held(stick_lock):
            try:
                sticklock.acquire(stick_lock, lambda: _StickBusy())
            except _StickBusy:
                return UpdateResult("busy", message=STICK_BUSY)
            except OSError as exc:
                return UpdateResult("error", message=f"could not lock the stick: {exc}")
            locked = True
        try:
            return _update(
                ytop, config, names, http=http or Http(), smoke=smoke, progress=progress,
                ep=endpoints or endpoints_for(config), server_install=server_install,
                python=python or sys.executable, now=now or datetime.now(UTC),
            )
        except Offline as exc:
            return UpdateResult("offline", message=str(exc))
        except RateLimited as exc:
            return UpdateResult("rate_limited", message=str(exc))
        except (UpdateError, ServerError, OSError) as exc:
            return UpdateResult("error", message=str(exc))
        finally:
            try:
                _clear_staging(ytop)
            finally:
                if locked:
                    sticklock.release(stick_lock)
    finally:
        _LOCK.release()


def _host_platform() -> str | None:
    from .runtime import UnsupportedPlatform, current_platform

    try:
        return current_platform()
    except UnsupportedPlatform:
        return None


def _update(
    ytop: Path, config: Config, names: list[str] | None, *, http: Http, smoke: Smoke,
    progress: Progress | None, ep: Endpoints, server_install: ServerInstall | None,
    python: str, now: datetime,
) -> UpdateResult:
    platforms = stick_platforms(ytop)
    current = read_current(ytop)
    installed = installed_versions(ytop)
    explicit = names is not None
    wanted = list(names) if names is not None else list(PYTHON_COMPONENTS)
    failed: dict[str, str] = {
        n: "not a component YTOP updates" for n in wanted
        if n not in SOURCES and n != BGUTIL_SERVER
    }
    _clear_staging(ytop)
    _clear_temps(ytop)
    staged: dict[str, _Staged] = {}
    # The network went away (or rate-limited us) during a step: that step
    # fails, the later ones are not fetched, and what was already staged is
    # still tested and switched in.  With nothing staged it is the run's
    # outcome ("offline" / "rate_limited"), as before.
    network: list[UpdateError] = []
    # Failures that do not make the run "failed" (see the server step).
    soft: set[str] = set()

    def pins(name: str) -> list[str]:
        return current.pinned_bad.get(name, [])

    for name in PYTHON_COMPONENTS:
        if name not in wanted:
            continue
        if network:
            failed[name] = str(network[0])
            continue
        try:
            release = SOURCES[name].latest(http, ep, current=installed.get(name),
                                           pinned=pins(name), platforms=platforms)
            if release is not None:
                staged[name] = _stage_python(ytop, http, release, progress)
        except (Offline, RateLimited) as exc:
            network.append(exc)
            failed[name] = str(exc)
        except (UpdateError, OSError) as exc:
            failed[name] = str(exc)

    # The PO-token server goes with its plugin (same version, or the plugin
    # refuses it).  It is staged whenever it is not the plugin's version, or
    # missing while the plugin is there (e.g. after a rollback of the plugin,
    # or a stick that lost its server): every update heals that.
    had_server = BGUTIL_SERVER in installed
    plugin = (staged["bgutil-plugin"].version if "bgutil-plugin" in staged
              else installed.get("bgutil-plugin"))
    server_version = None
    if (
        plugin and plugin != installed.get(BGUTIL_SERVER)
        and (not explicit or BGUTIL_SERVER in wanted or "bgutil-plugin" in wanted)
    ):
        server_version = plugin
    if server_version is not None:
        try:
            if network:
                raise network[0]
            staged[BGUTIL_SERVER] = _stage_server(ytop, http, ep, server_version,
                                                  server_install, progress)
        except (UpdateError, OSError) as exc:
            if isinstance(exc, (Offline, RateLimited)) and not network:
                network.append(exc)
            failed[BGUTIL_SERVER] = str(exc)
            # A stick that had no server keeps getting its plugin updates.
            if "bgutil-plugin" in staged and had_server:
                del staged["bgutil-plugin"]
                failed["bgutil-plugin"] = (
                    f"its PO-token server {server_version} could not be prepared: {exc}"
                )
            elif not had_server and BGUTIL_SERVER not in wanted:
                # A stick without a server that cannot get one here is as
                # up to date as it can be (unless the server was asked for).
                soft.add(BGUTIL_SERVER)

    folders = {
        n: staged[n].folder if n in staged else _source_folder(ytop, current, n)
        for n in ("yt-dlp", "yt-dlp-ejs")
    }
    minimum = deno_minimum(folders)
    deno_wanted = (DENO in wanted) if explicit else _deno_needed(minimum, installed.get(DENO))
    if deno_wanted and network:
        failed[DENO] = str(network[0])
    elif deno_wanted:
        try:
            release = SOURCES[DENO].latest(http, ep, current=installed.get(DENO),
                                           pinned=pins(DENO), platforms=platforms)
            if release is not None and minimum is not None and not explicit and (
                _version_key(release.version) < _version_key(minimum)
            ):
                release = None
            if release is not None:
                staged[DENO] = _stage_deno(ytop, http, release, platforms, progress)
            elif not explicit:
                failed[DENO] = f"yt-dlp needs Deno {minimum}, and no such release is available"
        except (Offline, RateLimited) as exc:
            network.append(exc)
            failed[DENO] = str(exc)
        except (UpdateError, OSError) as exc:
            failed[DENO] = str(exc)

    if not staged:
        if network:
            raise network[0]
        hard = {n: w for n, w in failed.items() if n not in soft}
        return UpdateResult("failed" if hard else "up_to_date", failed=hard,
                            message="; ".join(f"{n}: {w}" for n, w in failed.items()),
                            deno_minimum=minimum)

    _say(progress, "Testing the new versions…")
    groups = _groups(staged)
    request = _request_builder(ytop, config, current, staged, platforms, python)
    passing, why = _smoke_groups(groups, smoke, request)
    for group in groups:
        if group not in passing:
            for name in group:
                failed[name] = f"smoke test failed: {why.get(name, '')}".rstrip(": ")
    switch = [n for group in passing for n in group]
    if not switch:
        return UpdateResult("failed", failed=failed, deno_minimum=minimum,
                            message="; ".join(f"{n}: {w}" for n, w in failed.items()))

    _say(progress, "Switching to the new versions…")
    for name in switch:
        dest = _component_dir(ytop, name, staged[name].version)
        assert dest is not None
        dest.parent.mkdir(parents=True, exist_ok=True)
        move_in(staged[name].folder, dest)
    new = Current(
        current=dict(current.current),
        previous=dict(current.previous),
        pinned_bad={k: list(v) for k, v in current.pinned_bad.items()},
    )
    for name in switch:
        old, version = current.current.get(name), staged[name].version
        new.current[name] = version
        old_dir = _component_dir(ytop, name, old) if old else None
        if (
            name in PYTHON_COMPONENTS and old is not None and old != version
            and not is_pinned(old, pins(name)) and _usable(name, old_dir)
        ):
            new.previous[name] = old  # one rollback copy
        else:
            new.previous.pop(name, None)
    new.last_check = now.isoformat()
    new.last_result = "updated " + ", ".join(f"{n} {staged[n].version}" for n in switch)
    write_current(ytop, new)  # the switch: one atomic write
    _prune(ytop, new, switch)
    return UpdateResult(
        "updated",
        updated={n: (installed.get(n), staged[n].version) for n in switch},
        failed=failed, deno_minimum=minimum,
        message=new.last_result,
    )


def _groups(staged: dict[str, _Staged]) -> list[list[str]]:
    """Components switched only together: the plugin with its server."""
    order = [*PYTHON_COMPONENTS, BGUTIL_SERVER, DENO]
    names = [n for n in order if n in staged]
    groups: list[list[str]] = []
    pair = [n for n in ("bgutil-plugin", BGUTIL_SERVER) if n in staged]
    for name in names:
        if name in pair:
            if name == pair[0]:
                groups.append(pair)
        else:
            groups.append([name])
    return groups


def _request_builder(
    ytop: Path, config: Config, current: Current, staged: dict[str, _Staged],
    platforms: list[str], python: str,
) -> Callable[[list[str]], SmokeRequest]:
    """Builds the smoke request for a subset of the staged components."""
    plat = _host_platform()
    lib = [_lib_common(ytop), *([ytop / "lib" / plat] if plat else [])]
    lib = [p for p in lib if p.is_dir()]
    now_folders = _current_folders(ytop, current)
    baseline = [now_folders[n] for n in PYTHON_COMPONENTS if n in now_folders] + lib
    current_deno: Path | None = None
    runtime = os.environ.get("YTOP_RUNTIME")
    if plat is not None and runtime:
        from .components import executable_deno

        current_deno = executable_deno(ytop, plat, Path(runtime))

    def build(subset: list[str]) -> SmokeRequest:
        chosen = [n for n in PYTHON_COMPONENTS if n in subset]
        paths = [staged[n].folder for n in chosen]
        paths += [now_folders[n] for n in PYTHON_COMPONENTS
                  if n in now_folders and n not in chosen]
        deno, deno_version = current_deno, None
        if DENO in subset and plat is not None and plat in platforms:
            deno = staged[DENO].folder / plat / deno_exe(plat)
            deno_version = staged[DENO].version
        return SmokeRequest(
            python=python, paths=paths + lib,
            expect={n: staged[n].version for n in chosen},
            deno=deno, deno_version=deno_version,
            video=config.updates.smoke_video, online=True, baseline=baseline,
        )

    return build


def _smoke_groups(
    groups: list[list[str]], smoke: Smoke, request: Callable[[list[str]], SmokeRequest],
) -> tuple[list[list[str]], dict[str, str]]:
    """The groups that pass a smoke test, and why the others did not: all
    together first; if that fails, each alone, then the passing ones
    together again (or none)."""

    def flat(gs: list[list[str]]) -> list[str]:
        return [n for g in gs for n in g]

    def run(gs: list[list[str]]) -> SmokeOutcome:
        try:
            return smoke(request(flat(gs)))
        except Exception as exc:  # a smoke test that breaks is a failure
            return SmokeOutcome(False, f"{type(exc).__name__}: {exc}")

    why: dict[str, str] = {}
    outcome = run(groups)
    if outcome.ok:
        return groups, why
    if len(groups) == 1:
        return [], {n: outcome.message for n in groups[0]}
    passing = []
    for group in groups:
        alone = run([group])
        if alone.ok:
            passing.append(group)
        else:
            why.update({n: alone.message for n in group})
    if len(passing) > 1:
        together = run(passing)
        if not together.ok:
            why.update({n: together.message for n in flat(passing)})
            passing = []
    return passing, why


# --- rollback ------------------------------------------------------------------------


def rollback(ytop: Path, name: str) -> None:
    """Switch ``name`` back to its ``previous`` version (or, without one,
    to the image's own copy: ``lib/common``, the runtime's Deno) and pin the
    failing version bad, so it is never offered or merged again.

    The PO-token server has no copy in the image: without a usable previous
    it stays as it is and is not pinned (only its plugin is); the next
    update brings back a server matching the plugin."""
    current = read_current(ytop)
    failing = current.current.get(name)
    if failing is None:
        return
    previous = current.previous.get(name)
    back = _component_dir(ytop, name, previous) if previous else None
    pins = current.pinned_bad.get(name, [])
    usable = previous is not None and not is_pinned(previous, pins) and _usable(name, back)
    if name == BGUTIL_SERVER and not usable:
        return  # never leave the stick without a server
    pins = current.pinned_bad.setdefault(name, [])
    if not is_pinned(failing, pins):
        pins.append(failing)
    current.previous.pop(name, None)
    if usable:
        assert previous is not None
        current.current[name] = previous
    else:
        del current.current[name]
    write_current(ytop, current)
    _prune(ytop, current, [name])
