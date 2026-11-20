#!/usr/bin/env python3
"""Assemble the YTOP stick image from the sources pinned in stick/versions.toml.

    python tools/build_stick.py [--platforms all|<keys,...>] [--out dist/stick]
                                [--cache .tools/stick-cache] [--offline]

Every artifact is fetched into the cache and checked against its sha256; a
mismatch is refused.  ``--offline`` uses only the cache.  The image is written
under ``<out>/.ytop`` plus the launchers at ``<out>/``; anything else already in
``<out>`` (a library) is left alone.

Linux floor: glibc 2.28, set by the dependency wheels, which are pinned to
``manylinux_2_28`` (see ``UV_TRIPLES``).  The rest needs less: the
python-build-standalone gnu builds glibc 2.17, Deno 2.27, and ffmpeg-minimal
is static.
"""

from __future__ import annotations

import argparse
import compileall
import csv
import functools
import hashlib
import json
import os
import py_compile
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parent.parent
LAUNCHERS = REPO / "src/ytop/stick/launchers"
LAUNCHER_NAMES = (
    "Start YTOP (Windows).cmd",
    "Start YTOP (Mac).command",
    "start-ytop-linux.sh",
    "README.txt",
)
EXECUTABLE_LAUNCHERS = ("Start YTOP (Mac).command", "start-ytop-linux.sh")

from ytop.bgserver import (  # noqa: E402
    ServerError,
    ServerInstall,
    deno_install,
    find_native,
    prepare_server,
)
from ytop.components import (  # noqa: E402
    BGUTIL_SERVER,
    COMPONENT_DISTS,
    PYTHON_COMPONENTS,
    Current,
    deno_exe,
    mark_complete,
    read_current,
    version_dir,
    write_current,
)
from ytop.makestick import image_files  # noqa: E402
from ytop.manifest import (  # noqa: E402
    LEGAL_FILES,
    MANIFEST_HASH_KEY,
    MANIFEST_NAME,
    hash_tree,
    sha256_file,
    write_manifest,
)
from ytop.runtime import UnsupportedPlatform, current_platform  # noqa: E402

PLATFORMS: tuple[str, ...] = (
    "linux-x86_64",
    "linux-aarch64",
    "macos-arm64",
    "macos-x86_64",
    "windows-x86_64",
)
# uv --python-platform per platform.  Linux pins the manylinux_2_28 wheel
# floor explicitly (glibc >= 2.28; Python needs 2.17, Deno 2.27), so the image
# never picks up wheels needing a newer glibc.
UV_TRIPLES = {
    "linux-x86_64": "x86_64-manylinux_2_28",
    "linux-aarch64": "aarch64-manylinux_2_28",
    "macos-arm64": "aarch64-apple-darwin",
    "macos-x86_64": "x86_64-apple-darwin",
    "windows-x86_64": "x86_64-pc-windows-msvc",
}
COMPONENTS = ("python", "deno", "ffmpeg_minimal")
SIZE_BUDGET = 1_500_000_000  # bytes: the whole 5-platform .ytop image

PRUNE_STDLIB_TOP = ("test", "idlelib", "tkinter", "turtledemo", "ensurepip", "lib2to3")
# Further dead weight, as globs relative to the extracted python/ dir.
PRUNE_UNIX = (
    "bin/pip*", "bin/2to3*", "bin/idle*", "bin/pydoc*",
    "lib/python3.12/site-packages/pip*",
    "lib/python3.12/lib-dynload/_test*", "lib/python3.12/lib-dynload/_ctypes_test*",
    "lib/python3.12/lib-dynload/_tkinter*", "lib/python3.12/lib-dynload/xxlimited*",
    "lib/tcl*", "lib/libtcl*", "lib/libtk*", "lib/tk*", "lib/thread*", "lib/itcl*",
    # Man pages and curses terminfo, which YTOP never uses; terminfo also has
    # names differing only by case (p/ and P/) that no stick filesystem holds.
    "share",
    # C headers, only for compiling extensions (214 files a platform).
    "include",
)
PRUNE_WINDOWS = (
    "tcl", "Scripts", "Lib/site-packages/pip*",
    "DLLs/_tkinter.pyd", "DLLs/tcl*.dll", "DLLs/tk*.dll",
    "DLLs/_test*.pyd", "DLLs/_ctypes_test.pyd",
    # C headers and import libraries, only for compiling extensions.
    "include", "libs",
)
# Paths of the image that the builder owns (rebuilt from scratch every run).
IMAGE_PARTS = ("app", "lib", "runtime", "boot.py", *LEGAL_FILES, "build.json")
# The component store the build seeds (not image-owned, but rebuilt too).
COMPONENTS_PART = "components"
# Wheel RECORD entries the store does not need: console scripts and data.
NOT_IMPORTABLE = ("bin", "share")

InstallDeps = Callable[[str, Path], None]

# The server source is platform-independent: one artifact, no per-OS copy.
ANY_PLATFORM = "any"


class BuildError(RuntimeError):
    """The image could not be built."""


@dataclass(frozen=True)
class Artifact:
    name: str
    platform: str
    url: str
    sha256: str
    auth: str | None = None

    @property
    def filename(self) -> str:
        return urllib.parse.unquote(PurePosixPath(urllib.parse.urlsplit(self.url).path).name)


# --------------------------------------------------------------------- sources


def load_versions(path: Path) -> tuple[dict[str, dict[str, str]], list[Artifact]]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    sources = {name: dict(data[name]) for name in COMPONENTS}
    artifacts: list[Artifact] = []
    seen: set[tuple[str, str]] = set()
    for entry in data.get("artifact", []):
        art = Artifact(
            name=entry["name"],
            platform=entry["platform"],
            url=entry["url"],
            sha256=entry["sha256"].lower(),
            auth=entry.get("auth"),
        )
        known = (art.name in COMPONENTS and art.platform in PLATFORMS) or (
            art.name == BGUTIL_SERVER and art.platform == ANY_PLATFORM
        )
        if not known:
            raise BuildError(f"{path}: bad artifact {art.name}/{art.platform}")
        if (art.name, art.platform) in seen:
            raise BuildError(f"{path}: duplicate artifact {art.name}/{art.platform}")
        seen.add((art.name, art.platform))
        artifacts.append(art)
    return sources, artifacts


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _gh_cli_token() -> str:
    try:
        return subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def github_token() -> str | None:
    """A GitHub token if one is at hand, else None (public releases need none)."""
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or _gh_cli_token() or None


class _StripAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward the GitHub token to the asset storage host."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> urllib.request.Request | None:
        new = super().redirect_request(
            req, fp, code, msg, headers, newurl  # type: ignore[arg-type]
        )
        if new is not None and urllib.parse.urlsplit(newurl).hostname != "api.github.com":
            new.remove_header("Authorization")
        return new


def _github_asset_request(
    url: str, token: str
) -> tuple[urllib.request.OpenerDirector, urllib.request.Request]:
    m = re.fullmatch(
        r"https://github\.com/([^/]+)/([^/]+)/releases/download/([^/]+)/([^/]+)", url
    )
    if not m:
        raise BuildError(f"not a GitHub release asset URL: {url}")
    owner, repo, tag, name = m.groups()
    auth = {"Authorization": f"Bearer {token}"}
    api = f"https://api.github.com/repos/{owner}/{repo}"
    opener = urllib.request.build_opener(_StripAuthOnRedirect)
    release_req = urllib.request.Request(
        f"{api}/releases/tags/{tag}",
        headers={**auth, "Accept": "application/vnd.github+json"},
    )
    with opener.open(release_req, timeout=60) as resp:
        release = json.load(resp)
    for asset in release.get("assets", []):
        if asset.get("name") == urllib.parse.unquote(name):
            return opener, urllib.request.Request(
                f"{api}/releases/assets/{asset['id']}",
                headers={**auth, "Accept": "application/octet-stream"},
            )
    raise BuildError(f"release {tag} has no asset {name}")


def _download(art: Artifact, dest: Path) -> None:
    token = github_token() if art.auth == "github" else None
    if token:
        opener, req = _github_asset_request(art.url, token)
    elif art.auth in (None, "github"):
        opener = urllib.request.build_opener()
        req = urllib.request.Request(art.url, headers={"User-Agent": "ytop-build-stick"})
    else:
        raise BuildError(f"unknown auth {art.auth!r} for {art.url}")
    with opener.open(req, timeout=300) as resp, dest.open("wb") as f:
        shutil.copyfileobj(resp, f, 1 << 20)


def fetch(art: Artifact, cache: Path, offline: bool) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / art.filename
    if path.is_file():
        if _sha256(path) == art.sha256:
            return path
        if offline:
            raise BuildError(f"{path.name}: cached copy has the wrong sha256")
        path.unlink()
    if offline:
        raise BuildError(f"{art.filename} is not in the cache ({cache})")
    print(f"fetching {art.filename}", flush=True)
    part = path.with_name(path.name + ".part")
    try:
        _download(art, part)
        got = _sha256(part)
        if got != art.sha256:
            raise BuildError(
                f"{art.filename}: sha256 mismatch (expected {art.sha256}, got {got})"
            )
        part.replace(path)
    except Exception as exc:
        part.unlink(missing_ok=True)
        if isinstance(exc, BuildError):
            raise
        raise BuildError(f"download failed for {art.filename}: {exc}") from exc
    return path


# ------------------------------------------------------------------ extraction


def _make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def extract_python(tarball: Path, dest: Path, plat: str) -> None:
    """Unpack the tarball's ``python/`` tree to ``dest`` without symlinks.

    Sticks are FAT/exFAT, which cannot hold symlinks.  ``bin/python3`` (the
    name the launchers run) is a link to ``python3.12``: the interpreter is
    moved to ``bin/python3`` rather than copied (it is ~30 MB).  Every other
    link (``bin/python``, dev and man aliases) is dropped.
    """
    with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
        with tarfile.open(tarball) as tf:
            tf.extractall(tmp, filter="data")
        # Resolved, like the link targets below: a symlinked parent of --out
        # (macOS /var, Fedora Atomic /home) must not defeat is_relative_to.
        root = (Path(tmp) / "python").resolve()
        if not root.is_dir():
            raise BuildError(f"{tarball.name}: no python/ directory")
        links = {p: p.resolve() for p in root.rglob("*") if p.is_symlink()}
        for link in links:
            link.unlink()
        python3 = root / "bin/python3"
        target = links.get(python3)
        if target is not None and target.is_file() and target.is_relative_to(root):
            target.replace(python3)
        shutil.move(str(root), dest)
    check_interpreter(dest, plat)


def interpreter(python_dir: Path, plat: str) -> Path:
    if plat.startswith("windows"):
        return python_dir / "python.exe"
    return python_dir / "bin/python3"


def check_interpreter(python_dir: Path, plat: str) -> None:
    """Fail closed: an image without its interpreter must never be built."""
    exe = interpreter(python_dir, plat)
    if exe.is_symlink() or not exe.is_file():
        raise BuildError(f"{plat}: no interpreter at {exe} after extraction")


def extract_bin(archive: Path, names: tuple[str, ...], bin_dir: Path) -> None:
    """Copy the named executables (matched by basename) into ``bin_dir``."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    found: set[str] = set()
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                base = PurePosixPath(info.filename).name
                if base in names and not info.is_dir():
                    (bin_dir / base).write_bytes(zf.read(info))
                    found.add(base)
    else:
        with tarfile.open(archive) as tf:
            for member in tf.getmembers():
                base = PurePosixPath(member.name).name
                if base in names and member.isfile():
                    src = tf.extractfile(member)
                    assert src is not None
                    (bin_dir / base).write_bytes(src.read())
                    found.add(base)
    missing = set(names) - found
    if missing:
        raise BuildError(f"{archive.name}: missing {', '.join(sorted(missing))}")
    for name in names:
        _make_executable(bin_dir / name)


def stdlib_dir(python_dir: Path, plat: str) -> Path:
    if plat.startswith("windows"):
        return python_dir / "Lib"
    return python_dir / "lib/python3.12"


def prune_stdlib(python_dir: Path, plat: str) -> None:
    """Drop what YTOP never runs: tests, Tk, IDLE, pip, helper scripts.

    ``boot.py`` and ytop need only the core stdlib, ``ssl``/``sqlite3``/
    ``ctypes`` etc., which all stay.
    """
    std = stdlib_dir(python_dir, plat)
    for name in PRUNE_STDLIB_TOP:
        shutil.rmtree(std / name, ignore_errors=True)
    if plat.startswith("windows"):
        globs = list(PRUNE_WINDOWS)
    else:
        globs = list(PRUNE_UNIX)
    for pattern in globs:
        for path in sorted(python_dir.glob(pattern), reverse=True):
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
    for cache_dir in sorted(python_dir.rglob("__pycache__"), reverse=True):
        shutil.rmtree(cache_dir, ignore_errors=True)
    for pyc in python_dir.rglob("*.pyc"):
        pyc.unlink()
    check_interpreter(python_dir, plat)


def build_runtime(plat: str, files: dict[str, Path], rt: Path) -> None:
    exe = ".exe" if plat.startswith("windows") else ""
    rt.mkdir(parents=True)
    extract_python(files["python"], rt / "python", plat)
    prune_stdlib(rt / "python", plat)
    extract_bin(files["deno"], (f"deno{exe}",), rt / "bin")
    extract_bin(files["ffmpeg_minimal"], (f"ffmpeg{exe}", f"ffprobe{exe}"), rt / "bin")


# ---------------------------------------------------------------- dependencies


def export_requirements(dest: Path) -> Path:
    """Write the locked runtime requirements (no dev deps, no ytop) once."""
    req = dest / "requirements.txt"
    subprocess.run(
        ["uv", "export", "--frozen", "--no-dev", "--no-hashes",
         "--no-emit-project", "-o", str(req)],
        cwd=REPO, check=True, stdout=subprocess.DEVNULL,
    )
    return req


def install_deps(plat: str, target: Path, *, requirements: Path) -> None:
    """Install ``requirements`` for ``plat`` into ``target`` with uv."""
    subprocess.run(
        ["uv", "pip", "install", "-r", str(requirements), "--target", str(target),
         "--python-platform", UV_TRIPLES[plat], "--python-version", "3.12",
         "--only-binary", ":all:"],
        cwd=REPO, check=True,
    )
    # Console-script wrappers point at the build host's python; useless here.
    shutil.rmtree(target / "bin", ignore_errors=True)


def _tree_digest(path: Path) -> dict[str, str]:
    if path.is_file():
        return {"": _sha256(path)}
    return {
        p.relative_to(path).as_posix(): _sha256(p)
        for p in path.rglob("*")
        if p.is_file()
    }


def dedupe(lib: Path, platforms: list[str]) -> None:
    """Move top-level entries identical in every platform's lib to ``lib/common``.

    The unit is a whole top-level entry (package dir, module, ``.dist-info``),
    not a single file: a regular package split across ``lib/common`` and
    ``lib/<plat>`` would not import, since Python takes its ``__path__`` from
    the first ``sys.path`` entry that holds the package's ``__init__.py``.
    """
    common = lib / "common"
    common.mkdir(parents=True, exist_ok=True)
    first = lib / platforms[0]
    for entry in sorted(first.iterdir()):
        copies = [lib / plat / entry.name for plat in platforms]
        if not all(c.exists() for c in copies):
            continue
        digest = _tree_digest(entry)
        if any(_tree_digest(c) != digest for c in copies[1:]):
            continue
        entry.replace(common / entry.name)
        for c in copies[1:]:
            if c.is_dir():
                shutil.rmtree(c)
            else:
                c.unlink()


# ------------------------------------------------------------------- assembly


def _dist_info(common: Path, dist: str) -> Path:
    norm = re.sub(r"[-_.]+", "_", dist).lower()
    found = [
        p for p in common.glob("*.dist-info")
        if p.is_dir() and p.name.lower().startswith(norm + "-")
        and "-" not in p.name[len(norm) + 1:-len(".dist-info")]
    ]
    if len(found) != 1:
        raise BuildError(
            f"lib/common holds {len(found)} installs of {dist}, not one"
        )
    return found[0]


def seed_components(yt: Path) -> dict[str, str]:
    """Fill ``.ytop/components`` with the versions this build installed.

    Each pure-Python component gets the files its wheel's RECORD lists
    (packages and dist-info; no console scripts or data), copied from
    ``lib/common``, which keeps them as the fallback.  ``current.json``
    points at all of them.

    Deno is not seeded (ruling R-B4: the spec's size budget).  The store
    starts without it and ``current.json`` names no ``deno`` until the
    updater adds a newer one; until then ``deno_for`` is None and the
    runtime's ``bin/deno`` (the version ``versions.toml`` pins) is used."""
    store = yt / COMPONENTS_PART
    if store.exists():
        shutil.rmtree(store)
    common = yt / "lib" / "common"
    current: dict[str, str] = {}
    for name in PYTHON_COMPONENTS:
        dist = COMPONENT_DISTS[name]
        try:
            info = _dist_info(common, dist)
        except BuildError as exc:
            raise BuildError(f"cannot seed the component {name}: {exc}") from exc
        version = info.name[: -len(".dist-info")].split("-", 1)[1]
        folder_name = version_dir(version)
        if folder_name is None:
            raise BuildError(f"{info.name}: no usable version")
        folder = store / name / folder_name
        record = (info / "RECORD").read_text(encoding="utf-8").splitlines()
        for row in csv.reader(record):
            if not row:
                continue
            rel = row[0]
            parts = PurePosixPath(rel).parts
            if (
                not parts or parts[0] in NOT_IMPORTABLE or ".." in parts
                or PurePosixPath(rel).is_absolute() or "__pycache__" in parts
            ):
                continue
            src = common / rel
            if not src.is_file():
                continue
            (folder / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, folder / rel)
        mark_complete(folder)  # boot imports only a complete folder
        current[name] = version
    # No timestamp: the seeded store is the same in every build of it.
    write_current(yt, Current(current=current, last_result="seeded by the build"))
    return current


# ------------------------------------------------------------- bgutil server


def seed_server(
    yt: Path, tarball: Path, url: str, deno: Path, install: ServerInstall,
) -> str:
    """Seed ``components/bgutil-server/<version>/`` and name it in
    ``current.json``.  The version is the seeded plugin's: the server and the
    plugin must be the same version (the plugin refuses a different major)."""
    current = read_current(yt)
    version = current.current.get("bgutil-plugin")
    if version is None:
        raise BuildError("cannot seed the bgutil server: no bgutil-plugin seeded")
    if not url.endswith(f"/{version}.tar.gz"):
        raise BuildError(
            f"versions.toml pins the bgutil server at {url}, but the plugin is "
            f"{version}: re-pin the server tag to {version}"
        )
    folder = version_dir(version)
    assert folder is not None
    dest = yt / COMPONENTS_PART / BGUTIL_SERVER / folder
    try:
        prepare_server(tarball, version, deno, dest, install)
    except ServerError as exc:
        raise BuildError(str(exc)) from exc
    mark_complete(dest)
    current.current[BGUTIL_SERVER] = version
    write_current(yt, current)
    return version


def host_deno(
    by_key: dict[tuple[str, str], Artifact],
    files: dict[str, dict[str, Path]],
    cache: Path,
    offline: bool,
    into: Path,
) -> Path:
    """The pinned Deno for the machine running the build (it runs
    ``deno install``), unpacked into ``into``."""
    try:
        host = current_platform()
    except UnsupportedPlatform as exc:
        raise BuildError(f"cannot run Deno here to build the bgutil server: {exc}") from exc
    archive = files.get(host, {}).get("deno")
    if archive is None:
        art = by_key.get(("deno", host))
        if art is None:
            raise BuildError(f"versions.toml pins no deno for the build host {host}")
        archive = fetch(art, cache, offline)
    extract_bin(archive, (deno_exe(host),), into)
    return into / deno_exe(host)


def require_bytecode_compiler() -> None:
    """The build must run on CPython 3.12: its pycs are the stick's."""
    if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 12):
        raise BuildError(
            "the stick's bytecode must be compiled by CPython 3.12 (the "
            f"stick's Python); this build runs {sys.implementation.name} "
            f"{sys.version.split()[0]} - use the project's .venv python"
        )


def precompile(yt: Path, platforms: list[str]) -> None:
    """Ship bytecode (Ruling R12) for ``app/``, ``lib/``, the seeded
    ``components/`` and each stdlib.

    Windows and macOS run straight from the stick, so without it Python
    would write ``__pycache__`` onto the stick and every first start would
    be slow.  ``unchecked-hash`` pycs are used without looking at the
    source's mtime (copies to a stick or the Linux cache change mtimes); the
    image is upgraded file by file, a pyc always together with its source,
    so they never go stale.  ``stripdir``/``prependdir`` make ``co_filename``
    read ``.ytop/...`` whatever folder the image is built in, so unchanged
    pycs are byte-identical between builds and an upgrade skips them.
    CPython 3.12 bytecode is the same on every OS and CPU, so the build
    host's 3.12 compiles it for all platforms.
    """
    require_bytecode_compiler()
    dirs = [yt / "app", yt / "lib" / "common", yt / COMPONENTS_PART]
    dirs += [yt / "lib" / plat for plat in platforms]
    dirs += [stdlib_dir(yt / "runtime" / plat / "python", plat) for plat in platforms]
    for folder in dirs:
        if not folder.is_dir():
            continue
        # quiet=1 still prints files that fail to compile (they then run
        # from source, as before); they never fail the build.
        compileall.compile_dir(
            folder, quiet=1, workers=1,
            invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
            stripdir=str(yt), prependdir=".ytop",
        )


# Stdlib folders that stay loose: extension modules and the (empty) site dir.
STDLIB_LOOSE = ("lib-dynload", "site-packages")
# Zip entries get one fixed time so an unchanged stdlib zips to the same bytes
# and an upgrade skips it.
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


def stdlib_zip(python_dir: Path, plat: str) -> Path:
    """Where CPython looks for its zipped stdlib: beside python312.dll on
    Windows (as the embeddable distribution has it), ``lib/`` elsewhere."""
    if plat.startswith("windows"):
        return python_dir / "python312.zip"
    return python_dir / "lib/python312.zip"


def zip_stdlib(yt: Path, platforms: list[str]) -> None:
    """Move each runtime's pure-Python stdlib into ``python312.zip``.

    A stick copies ~8 small files a second to FAT, and the stdlib is ~1,050
    files a platform; one zip is one file.  CPython puts the zip on
    ``sys.path`` itself.  zipimport never reads ``__pycache__``, so each
    build pyc goes in as ``<module>.pyc`` beside its source (sources stay
    for tracebacks): start-up is as fast as from loose files.  Run after
    :func:`precompile`."""
    for plat in platforms:
        python_dir = yt / "runtime" / plat / "python"
        std = stdlib_dir(python_dir, plat)
        if not std.is_dir():
            continue
        files = [
            path for path in sorted(std.rglob("*"))
            if path.is_file() and path.relative_to(std).parts[0] not in STDLIB_LOOSE
        ]
        target = stdlib_zip(python_dir, plat)
        entries: dict[str, Path] = {}
        for path in files:
            rel = path.relative_to(std)
            if rel.parent.name == "__pycache__":
                name, _tag, ext = rel.name.partition(".cpython-312")
                if ext != ".pyc":
                    continue  # an optimised or foreign pyc: never imported
                arc = (rel.parent.parent / f"{name}.pyc").as_posix()
            elif rel.suffix == ".pyc":
                continue
            else:
                arc = rel.as_posix()
            entries[arc] = path
        with zipfile.ZipFile(target, "w", zipfile.ZIP_STORED) as zf:
            for arc in sorted(entries):
                info = zipfile.ZipInfo(arc, date_time=ZIP_EPOCH)
                info.external_attr = 0o644 << 16
                zf.writestr(info, entries[arc].read_bytes())
        for path in files:
            path.unlink()
        for folder in sorted((p for p in std.rglob("*") if p.is_dir()), reverse=True):
            if not any(folder.iterdir()):
                folder.rmdir()


def dir_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def check_case_collisions(root: Path) -> None:
    """Refuse names that differ only by case: FAT, exFAT, NTFS and default
    APFS are case-insensitive, so one of the two would be lost on a stick."""
    seen: dict[str, str] = {}
    clashes: list[str] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        other = seen.setdefault(rel.casefold(), rel)
        if other != rel:
            clashes.append(f"{other} / {rel}")
    if clashes:
        shown = "; ".join(clashes[:5]) + (" ..." if len(clashes) > 5 else "")
        raise BuildError(f"names differing only by case under {root}: {shown}")


def git_revision() -> str:
    """``<short sha>``, plus ``-dirty`` when the tree has uncommitted changes."""
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=REPO, capture_output=True, text=True, check=True
        ).stdout.strip()

    try:
        sha = git("rev-parse", "--short", "HEAD")
        dirty = git("status", "--porcelain", "--", ".", ":!dist", ":!.tools")
    except (OSError, subprocess.CalledProcessError):
        return "nogit"
    return f"{sha}-dirty" if dirty else sha


def ytop_version() -> str:
    """The release version: ``pyproject.toml``'s ``[project] version``."""
    doc = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    version = doc.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise BuildError("no [project] version in pyproject.toml")
    return version


def locked_version(package: str) -> str | None:
    lock = tomllib.loads((REPO / "uv.lock").read_text(encoding="utf-8"))
    for pkg in lock.get("package", []):
        if pkg.get("name") == package:
            return str(pkg.get("version"))
    return None


def copy_app(yt: Path) -> None:
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(REPO / "src/ytop", yt / "app/ytop", ignore=ignore)
    shutil.copy2(REPO / "src/ytop/stick/boot.py", yt / "boot.py")
    for name in LEGAL_FILES:
        shutil.copy2(REPO / name, yt / name)


def copy_launchers(out: Path) -> None:
    for name in LAUNCHER_NAMES:
        shutil.copyfile(LAUNCHERS / name, out / name)
        if name in EXECUTABLE_LAUNCHERS:
            (out / name).chmod(0o755)


def print_size_table(sizes: dict[str, int], total: int) -> None:
    width = max(len(k) for k in sizes) + 2
    for key, value in sizes.items():
        print(f"  {key:<{width}}{value / 1e6:>10.1f} MB")
    print(f"  {'total':<{width}}{total / 1e6:>10.1f} MB "
          f"(budget {SIZE_BUDGET / 1e6:.0f} MB)")


def build(
    *,
    platforms: list[str],
    out: Path,
    cache: Path,
    versions_path: Path,
    offline: bool,
    install: InstallDeps | None = None,
    server_install: ServerInstall | None = None,
) -> dict[str, object]:
    for plat in platforms:
        if plat not in PLATFORMS:
            raise BuildError(f"unknown platform {plat!r} (known: {', '.join(PLATFORMS)})")
    require_bytecode_compiler()
    sources, artifacts = load_versions(versions_path)
    by_key = {(a.name, a.platform): a for a in artifacts}
    files: dict[str, dict[str, Path]] = {}
    for plat in platforms:
        files[plat] = {}
        for name in COMPONENTS:
            art = by_key.get((name, plat))
            if art is None:
                raise BuildError(f"versions.toml pins no {name} for {plat}")
            files[plat][name] = fetch(art, cache, offline)

    server_art = by_key.get((BGUTIL_SERVER, ANY_PLATFORM))
    if server_art is None:
        raise BuildError(f"versions.toml pins no {BGUTIL_SERVER}")
    server_tarball = fetch(server_art, cache, offline)

    yt = out / ".ytop"
    yt.mkdir(parents=True, exist_ok=True)
    for part in IMAGE_PARTS:
        p = yt / part
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()

    with tempfile.TemporaryDirectory() as tmp:
        if install is None:
            install = functools.partial(
                install_deps, requirements=export_requirements(Path(tmp))
            )
        for plat in platforms:
            print(f"assembling {plat}", flush=True)
            build_runtime(plat, files[plat], yt / "runtime" / plat)
            target = yt / "lib" / plat
            target.mkdir(parents=True)
            install(plat, target)
    dedupe(yt / "lib", platforms)
    seeded = seed_components(yt)
    with tempfile.TemporaryDirectory() as tools:
        if server_install is None:
            deno = host_deno(by_key, files, cache, offline, Path(tools))
            server_install = deno_install
        else:
            deno = Path(tools) / "deno"  # the injected install does not run it
        seeded[BGUTIL_SERVER] = seed_server(
            yt, server_tarball, server_art.url, deno, server_install
        )
    copy_app(yt)
    precompile(yt, platforms)
    zip_stdlib(yt, list(platforms))
    copy_launchers(out)
    check_case_collisions(yt)

    sizes: dict[str, int] = {
        plat: dir_size(yt / "runtime" / plat) + dir_size(yt / "lib" / plat)
        for plat in platforms
    }
    sizes["common"] = dir_size(yt / "lib/common")
    sizes["app"] = dir_size(yt / "app") + dir_size(yt / "boot.py")
    ejs = locked_version("yt-dlp-ejs")
    manifest: dict[str, object] = {
        "build_id": f"{datetime.now(UTC):%Y%m%d%H%M}-{git_revision()}",
        "version": ytop_version(),
        "platforms": list(platforms),
        "sizes": sizes,
        "sources": {**sources, **({"yt_dlp_ejs": {"version": ejs}} if ejs else {})},
    }
    rels = [rel for rel, _size in image_files(yt, list(platforms))]
    write_manifest(yt / MANIFEST_NAME, hash_tree(yt, rels))
    # build.json vouches for the manifest beside it (ytop.manifest).
    manifest[MANIFEST_HASH_KEY] = sha256_file(yt / MANIFEST_NAME)
    (yt / "build.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    total = sum(dir_size(yt / part) for part in IMAGE_PARTS)
    print_size_table(sizes, total)
    # The store is not image-owned (an upgrade never changes it), so it is
    # outside the image budget.  It holds no Deno (ruling R-B4).
    store_size = dir_size(yt / COMPONENTS_PART)
    print(f"  components (outside the image): {store_size / 1e6:.1f} MB "
          f"({', '.join(f'{k} {v}' for k, v in seeded.items())})")
    if total > SIZE_BUDGET:
        raise BuildError(
            f"image is {total / 1e6:.1f} MB, over the {SIZE_BUDGET / 1e6:.0f} MB budget"
        )
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--platforms", default="all", help="all, or comma-separated keys")
    ap.add_argument("--out", type=Path, default=REPO / "dist/stick")
    ap.add_argument("--cache", type=Path, default=REPO / ".tools/stick-cache")
    ap.add_argument("--versions", type=Path, default=REPO / "stick/versions.toml")
    ap.add_argument("--offline", action="store_true", help="use only the cache")
    opts = ap.parse_args(argv)
    platforms = (
        list(PLATFORMS)
        if opts.platforms == "all"
        else [p.strip() for p in opts.platforms.split(",") if p.strip()]
    )
    try:
        manifest = build(
            platforms=platforms,
            out=opts.out,
            cache=opts.cache,
            versions_path=opts.versions,
            offline=opts.offline,
        )
    except (BuildError, subprocess.CalledProcessError) as exc:
        print(f"build_stick: {exc}", file=sys.stderr)
        return 1
    print(f"built {manifest['build_id']} -> {opts.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
