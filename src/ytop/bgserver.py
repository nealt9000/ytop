"""The bgutil PO-token server: built from the upstream tag tarball.

One implementation for the stick build (``tools/build_stick.py``) and the
component updater (:mod:`ytop.updater`).  The plugin's Deno provider runs
``server/src/generate_once.ts``; ``server/node_modules`` must be pure
JavaScript (one server for every platform a stick carries) with no symlinks
(FAT).

The real install (:func:`install_and_bundle`) also bundles the script and
its ~6,000 npm files into ``src/generate_once.js`` (``bgbundle.mjs``, run by
the same Deno): a stick pays per file far more than per byte.  The bundled
server is about a dozen files: the bundle, a one-line ``generate_once.ts``
that imports it, the few files packages read beside their code, one licence
file and the ``bundled.json`` marker."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Callable
from importlib import resources
from pathlib import Path

__all__ = [
    "BUNDLE_MARKER",
    "ServerError",
    "ServerInstall",
    "deno_bundle",
    "deno_install",
    "install_and_bundle",
    "is_bundled",
    "find_native",
    "prepare_server",
    "prune_node_modules",
]

# ``deno install`` fetches npm packages: never let it hang an update.
DENO_INSTALL_TIMEOUT = 300  # seconds
# The bundler fetches esbuild-wasm (once per cache) and bundles ~15 MB.
DENO_BUNDLE_TIMEOUT = 300  # seconds

# In a bundled ``server/``: what was bundled (and that it was).
BUNDLE_MARKER = "bundled.json"
# Written by ``bgbundle.mjs`` into ``<server>/bundle/``.
BUNDLE_DIR = "bundle"
BUNDLE_SCRIPT = "generate_once.js"
BUNDLE_ASSETS = "assets.json"
BUNDLE_LICENCES = "THIRD_PARTY_LICENSES.txt"
# The plugin runs ``src/generate_once.ts``.  Deno would transpile a 15 MB
# .ts on every new cache; a static import of plain JavaScript is not
# transpiled (and needs no --allow-read).
BUNDLE_STUB = 'import "./generate_once.js";\n'

# Fills ``<server>/node_modules`` and may write ``<server>/bundle/`` (see
# :func:`install_and_bundle`): (a Deno that runs here, the server folder).
ServerInstall = Callable[[Path, Path], None]

SERVER_KEEP = ("src", "package.json")
# Extensions that are always native code; other files are told by their header.
NATIVE_SUFFIXES = (".node", ".exe", ".dll", ".dylib")
NATIVE_MAGIC = (
    b"\x7fELF",
    b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf",  # Mach-O
    b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe",
)


# Files ``deno run`` never loads: type declarations, source maps and docs.
# They are ~43% of the server's files (2,576 of 6,010 for 2.0.2), and every
# file costs a stick copy far more than its bytes do.  Matched by suffix
# only: a module such as ``History.js`` must never be caught by a name.
PRUNE_SUFFIXES = (".d.ts", ".d.mts", ".d.cts", ".map", ".md", ".markdown")


class ServerError(RuntimeError):
    """The server could not be prepared."""


def find_native(root: Path) -> list[str]:
    """Paths under ``root`` that are native (per-platform) binaries: by
    extension, or by an ELF / Mach-O header (so a WebAssembly module that
    happens to be named ``.so`` is fine)."""
    found: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        native = path.suffix.lower() in NATIVE_SUFFIXES
        if not native:
            with path.open("rb") as handle:
                head = handle.read(8)
            native = (
                head[:4] in NATIVE_MAGIC
                or head[:2] == b"MZ"  # PE (Windows)
                # Fat/universal Mach-O: cafebabe then a small architecture
                # count (a Java class file has a version number there, >= 45).
                or (head[:4] == b"\xca\xfe\xba\xbe" and len(head) == 8
                    and 0 < int.from_bytes(head[4:], "big") < 20)
            )
        if native:
            found.append(path.relative_to(root).as_posix())
    return found


def prune_node_modules(node_modules: Path) -> int:
    """Delete the files under ``node_modules`` that are never loaded at run
    time (see ``PRUNE_SUFFIXES``); licences stay.  Returns how many went."""
    removed = 0
    for path in sorted(node_modules.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if path.name.lower().endswith(PRUNE_SUFFIXES):
            path.unlink()
            removed += 1
    return removed


def deno_install(deno: Path, server: Path) -> None:
    """``deno install`` in ``server``: a hoisted ``node_modules`` (the default
    layout is a tree of symlinks), no lifecycle scripts, a private cache."""
    with tempfile.TemporaryDirectory() as cache:
        env = _deno_env(cache)
        try:
            run = subprocess.run(
                [str(deno), "install", "--node-modules-dir=manual",
                 "--node-modules-linker=hoisted"],
                cwd=server, env=env, capture_output=True, text=True,
                timeout=DENO_INSTALL_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise ServerError(
                f"deno install for the bgutil server timed out after "
                f"{DENO_INSTALL_TIMEOUT} s"
            ) from None
        except OSError as exc:
            raise ServerError(f"deno install could not run: {exc}") from None
    if run.returncode:
        raise ServerError(f"deno install failed for the bgutil server:\n{run.stderr}")


def _deno_env(cache: str) -> dict[str, str]:
    return {
        **os.environ,
        "DENO_DIR": cache,
        "DENO_NO_UPDATE_CHECK": "1",
        "DENO_NO_PROMPT": "1",
        "NO_COLOR": "1",
    }


def deno_bundle(deno: Path, server: Path) -> None:
    """Run ``bgbundle.mjs`` over ``server`` (its ``node_modules`` installed):
    writes ``server/bundle/``.  The script is copied out of the package
    first, so this works when ``ytop`` itself is imported from a zip."""
    script = resources.files("ytop").joinpath("bgbundle.mjs").read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        bundler = Path(tmp) / "bgbundle.mjs"
        bundler.write_text(script, encoding="utf-8")
        cache = Path(tmp) / "deno"
        try:
            run = subprocess.run(
                # esbuild-wasm runs its service as `node`: Deno runs that itself.
                [str(deno), "run", "--allow-read", f"--allow-write={server}",
                 f"--allow-run=node,{deno}",
                 "--allow-env", "--allow-net=registry.npmjs.org",
                 str(bundler), str(server)],
                cwd=server, env=_deno_env(str(cache)), capture_output=True,
                text=True, timeout=DENO_BUNDLE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise ServerError(
                f"bundling the bgutil server timed out after {DENO_BUNDLE_TIMEOUT} s"
            ) from None
        except OSError as exc:
            raise ServerError(f"the bgutil server bundler could not run: {exc}") from None
    if run.returncode:
        raise ServerError(f"bundling the bgutil server failed:\n{run.stderr}")


def install_and_bundle(deno: Path, server: Path) -> None:
    """The real :data:`ServerInstall`: ``deno install``, then the bundle
    (:func:`prepare_server` swaps it in)."""
    deno_install(deno, server)
    deno_bundle(deno, server)


def is_bundled(server: Path) -> bool:
    """Whether ``server/`` is a bundled server (its ``node_modules`` holds
    only the files the bundle reads, so it cannot seed another build)."""
    return (server / BUNDLE_MARKER).is_file()


def _asset_path(node_modules: Path, rel: object) -> Path:
    if (
        not isinstance(rel, str) or not rel or "\\" in rel
        or rel.startswith("/") or ":" in rel
        or any(part in ("", ".", "..") for part in rel.split("/"))
    ):
        raise ServerError(f"the bundler named a bad asset path: {rel!r}")
    path = node_modules / rel
    if path.is_symlink() or not path.is_file():
        raise ServerError(f"the bundler named a missing asset: {rel}")
    return path


def _apply_bundle(server: Path) -> int:
    """Swap ``server/bundle/`` in: ``src/`` becomes the bundle and its stub,
    ``node_modules`` only the files the bundle reads.  Returns how many
    assets were kept."""
    bundle = server / BUNDLE_DIR
    script = bundle / BUNDLE_SCRIPT
    if not script.is_file():
        raise ServerError(f"the bundler wrote no {BUNDLE_SCRIPT}")
    try:
        assets = json.loads((bundle / BUNDLE_ASSETS).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ServerError(f"the bundler's {BUNDLE_ASSETS} is unreadable: {exc}") from None
    if not isinstance(assets, list):
        raise ServerError(f"the bundler's {BUNDLE_ASSETS} is not a list")
    node_modules = server / "node_modules"
    slim = server / "node_modules.slim"
    slim.mkdir()
    for rel in assets:
        src = _asset_path(node_modules, rel)
        dest = slim / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
    licences = bundle / BUNDLE_LICENCES
    if licences.is_file():
        shutil.copyfile(licences, slim / BUNDLE_LICENCES)
    shutil.rmtree(node_modules)
    slim.rename(node_modules)
    shutil.rmtree(server / "src")
    (server / "src").mkdir()
    shutil.move(str(script), server / "src" / BUNDLE_SCRIPT)
    (server / "src" / "generate_once.ts").write_text(BUNDLE_STUB, encoding="utf-8")
    shutil.rmtree(bundle)
    (server / "deno.lock").unlink(missing_ok=True)
    (server / BUNDLE_MARKER).write_text(
        json.dumps({"assets": sorted(assets)}, indent=2) + "\n", encoding="utf-8"
    )
    return len(assets)


def prepare_server(
    tarball: Path, version: str, deno: Path, dest: Path, install: ServerInstall,
) -> None:
    """Unpack the upstream tag tarball and build ``dest`` (the version's
    folder) as ``server/`` plus the licence.

    Only ``src`` and ``package.json`` are kept.  The package file loses the
    dev tools and ``canvas`` (an optional native peer of jsdom the script
    never imports), so ``node_modules`` is pure JavaScript: every platform
    runs the same files.  When the install wrote a bundle, the bundled
    server replaces the installed one.  The build fails, listing them, if a
    native binary or a symlink is left."""
    with tempfile.TemporaryDirectory(dir=dest.parent if dest.parent.is_dir() else None) as tmp:
        with tarfile.open(tarball) as tf:
            links = [m.name for m in tf.getmembers() if m.issym() or m.islnk()]
            if links:
                raise ServerError(
                    f"{tarball.name}: contains symlinks: {', '.join(links[:5])}"
                )
            tf.extractall(tmp, filter="data")
        roots = [p for p in Path(tmp).iterdir() if (p / "server" / "src").is_dir()]
        if len(roots) != 1:
            raise ServerError(f"{tarball.name}: no server/src directory")
        root = roots[0]
        work = Path(tmp) / "work"
        server = work / "server"
        server.mkdir(parents=True)
        for name in SERVER_KEEP:
            src = root / "server" / name
            if src.is_dir():
                shutil.copytree(src, server / name)
            elif src.is_file():
                shutil.copy2(src, server / name)
            else:
                raise ServerError(f"{tarball.name}: server/{name} is missing")
        lock = root / "server" / "deno.lock"
        if lock.is_file():
            shutil.copy2(lock, server / "deno.lock")
        package = json.loads((server / "package.json").read_text(encoding="utf-8"))
        for key in ("devDependencies", "scripts", "allowScripts"):
            package.pop(key, None)
        package.get("dependencies", {}).pop("canvas", None)
        if package.get("version") != version:
            raise ServerError(
                f"{tarball.name}: server is {package.get('version')}, plugin is {version}"
            )
        (server / "package.json").write_text(
            json.dumps(package, indent=2) + "\n", encoding="utf-8"
        )
        # Use node_modules as it stands (no install scripts, no auto-install).
        (server / "deno.json").write_text(
            json.dumps({"nodeModulesDir": "manual"}) + "\n", encoding="utf-8"
        )
        install(deno, server)
        shutil.rmtree(server / "node_modules" / ".bin", ignore_errors=True)
        if (server / BUNDLE_DIR).is_dir():
            _apply_bundle(server)
        elif (server / "node_modules").is_dir():
            prune_node_modules(server / "node_modules")
        native = find_native(server)
        if native:
            raise ServerError(
                "the bgutil server needs native (per-platform) binaries, so one "
                "server cannot run on every stick platform:\n  "
                + "\n  ".join(native[:20]) + (" ..." if len(native) > 20 else "")
            )
        links = [p.relative_to(server).as_posix() for p in server.rglob("*") if p.is_symlink()]
        if links:
            raise ServerError(
                "the bgutil server has symlinks (a stick cannot hold them): "
                + ", ".join(links[:20])
            )
        licence = root / "LICENSE"
        if licence.is_file():
            shutil.copy2(licence, work / "LICENSE")
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(work), dest)
