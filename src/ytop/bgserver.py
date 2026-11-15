"""The bgutil PO-token server: built from the upstream tag tarball.

One implementation for the stick build (``tools/build_stick.py``) and the
component updater (:mod:`ytop.updater`).  The plugin's Deno provider runs
``server/src/generate_once.ts``; ``server/node_modules`` must be pure
JavaScript (one server for every platform a stick carries) with no symlinks
(FAT)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path

__all__ = [
    "ServerError",
    "ServerInstall",
    "deno_install",
    "find_native",
    "prepare_server",
]

# ``deno install`` fetches npm packages: never let it hang an update.
DENO_INSTALL_TIMEOUT = 300  # seconds

# Fills ``<server>/node_modules``: (a Deno that runs here, the server folder).
ServerInstall = Callable[[Path, Path], None]

SERVER_KEEP = ("src", "package.json")
# Extensions that are always native code; other files are told by their header.
NATIVE_SUFFIXES = (".node", ".exe", ".dll", ".dylib")
NATIVE_MAGIC = (
    b"\x7fELF",
    b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf",  # Mach-O
    b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe",
)


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


def deno_install(deno: Path, server: Path) -> None:
    """``deno install`` in ``server``: a hoisted ``node_modules`` (the default
    layout is a tree of symlinks), no lifecycle scripts, a private cache."""
    with tempfile.TemporaryDirectory() as cache:
        env = {
            **os.environ,
            "DENO_DIR": cache,
            "DENO_NO_UPDATE_CHECK": "1",
            "DENO_NO_PROMPT": "1",
            "NO_COLOR": "1",
        }
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


def prepare_server(
    tarball: Path, version: str, deno: Path, dest: Path, install: ServerInstall,
) -> None:
    """Unpack the upstream tag tarball and build ``dest`` (the version's
    folder) as ``server/`` plus the licence.

    Only ``src`` and ``package.json`` are kept.  The package file loses the
    dev tools and ``canvas`` (an optional native peer of jsdom the script
    never imports), so ``node_modules`` is pure JavaScript: every platform
    runs the same files.  The build fails, listing them, if a native binary
    or a symlink is left."""
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
