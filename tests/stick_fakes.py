"""Tiny fake stick sources and images for tests (no network, no real binaries).

``make_fake_sources`` writes fake python/deno/ffmpeg archives plus a
``versions.toml`` pointing at them via ``file://`` URLs.  ``make_fake_image``
runs the real ``tools/build_stick.py`` over those sources with a fake
dependency installer and returns the image's ``.ytop`` directory.
"""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import io
import json
import sys
import tarfile
import zipfile
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parent.parent

# Stdlib paths the builder must prune (relative to the stdlib dir).
PRUNED_STDLIB = [
    "test/test_os.py",
    "idlelib/idle.py",
    "tkinter/__init__.py",
    "turtledemo/demo.py",
    "ensurepip/__init__.py",
    "lib2to3/main.py",
    "json/__pycache__/decoder.cpython-312.pyc",  # no source: not rebuilt
    "stray.pyc",
]
# Bytecode in the source tarball that the build replaces with its own
# unchecked-hash pyc (Ruling R12).
STALE_STDLIB_PYC = ["__pycache__/os.cpython-312.pyc"]
KEPT_STDLIB = ["os.py", "json/__init__.py"]

# Other python/ paths (relative to python/) pruned or kept, per OS family.
PRUNED_PYTHON = {
    "unix": [
        "bin/pip3", "bin/2to3-3.12", "bin/idle3.12", "bin/pydoc3.12",
        "lib/python3.12/site-packages/pip/__init__.py",
        "lib/python3.12/lib-dynload/_testcapi.cpython-312.so",
        "lib/python3.12/lib-dynload/_tkinter.cpython-312.so",
        "lib/tcl9.0/init.tcl", "lib/libtcl9.0.so", "lib/libtcl9tk9.0.so",
        "lib/tk9.0/tk.tcl", "lib/thread3.0.6/x.tcl", "lib/itcl4.3.8/x.tcl",
        "share/man/man1/python3.12.1",
        # Case-only twins, as in python-build-standalone's terminfo.
        "share/terminfo/p/putty", "share/terminfo/P/P12",
    ],
    "windows": [
        "Scripts/pip.exe", "Lib/site-packages/pip/__init__.py",
        "DLLs/_tkinter.pyd", "DLLs/tcl86t.dll", "DLLs/tk86t.dll",
        "DLLs/_testcapi.pyd", "DLLs/_ctypes_test.pyd", "tcl/tcl8.6/init.tcl",
    ],
}
KEPT_PYTHON = {
    "unix": [
        "lib/python3.12/lib-dynload/_ssl.cpython-312.so",
        "lib/libpython3.12.so.1.0",
    ],
    "windows": ["DLLs/_ssl.pyd", "DLLs/sqlite3.dll", "python312.dll"],
}


def os_family(plat: str) -> str:
    return "windows" if plat.startswith("windows") else "unix"


def load_build_stick() -> ModuleType:
    name = "build_stick"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, REPO / "tools/build_stick.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def stdlib_dir(plat: str) -> str:
    """The stdlib dir inside an extracted ``python/`` tree."""
    return "Lib" if plat.startswith("windows") else "lib/python3.12"


def _tar(
    path: Path, files: dict[str, bytes], links: dict[str, str] | None = None
) -> None:
    # Deterministic bytes (fixed gzip/zip timestamps) so rebuilding the fake
    # sources keeps the same sha256 as copies already in a cache.
    with gzip.GzipFile(path, "wb", mtime=0) as gz, tarfile.open(
        fileobj=gz, mode="w"
    ) as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(data))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tf.addfile(info)


def _zip(path: Path, files: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            zf.writestr(info, data)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# The upstream tag tarball of the bgutil server: the plugin beside server/.
FAKE_SERVER_FILES = {
    "bgutil-ytdlp-pot-provider-2.0.2/LICENSE": b"GPL text\n",
    "bgutil-ytdlp-pot-provider-2.0.2/plugin/x.py": b"# plugin\n",
    "bgutil-ytdlp-pot-provider-2.0.2/server/src/generate_once.ts": b"// script\n",
    "bgutil-ytdlp-pot-provider-2.0.2/server/package.json": json.dumps({
        "name": "bgutil",
        "version": "2.0.2",
        "dependencies": {"axios": "^1", "canvas": "^3", "jsdom": "^29"},
        "devDependencies": {"eslint": "^10"},
        "allowScripts": {"canvas@3.2.3": True},
    }).encode(),
    "bgutil-ytdlp-pot-provider-2.0.2/server/deno.json": b'{"allowScripts": ["npm:canvas@3.2.3"]}',
    "bgutil-ytdlp-pot-provider-2.0.2/server/deno.lock": b"{}",
    "bgutil-ytdlp-pot-provider-2.0.2/server/Dockerfile": b"FROM x\n",
}


def fake_server_install(deno: Path, server: Path) -> None:
    """Stand-in for ``deno install``: a small pure-JS node_modules, with a
    ``.bin`` of links the build must drop (FAT holds no symlinks)."""
    pkg = server / "node_modules" / "axios"
    pkg.mkdir(parents=True)
    (pkg / "index.js").write_text("module.exports = 1\n")
    (server / "node_modules/quickjs-wasi").mkdir()
    # A WebAssembly side module named .so: not a native binary.
    (server / "node_modules/quickjs-wasi/base64.so").write_bytes(b"\0asm\x01\0\0\0")
    (server / "node_modules/.bin").mkdir()
    (server / "node_modules/.bin/axios").symlink_to("../axios/index.js")


def make_fake_sources(src: Path, platforms: list[str]) -> Path:
    """Write fake archives for ``platforms`` under ``src``; return versions.toml."""
    src.mkdir(parents=True, exist_ok=True)
    lines = [
        "[python]",
        'version = "3.12.15"',
        'build = "20261021"',
        "[deno]",
        'version = "2.9.8"',
        "[ffmpeg_minimal]",
        'release = "ffmpeg-minimal-1"',
    ]
    for plat in platforms:
        win = plat.startswith("windows")
        exe = ".exe" if win else ""
        std = stdlib_dir(plat)
        # Like python-build-standalone: unix bin/python3 is a symlink.
        py_files = {
            ("python/python.exe" if win else "python/bin/python3.12"): b"py-"
            + plat.encode()
        }
        py_links = {} if win else {
            "python/bin/python3": "python3.12",
            "python/bin/python": "python3.12",
            "python/lib/libpython3.12.so": "libpython3.12.so.1.0",
        }
        for rel in KEPT_STDLIB + PRUNED_STDLIB + STALE_STDLIB_PYC:
            py_files[f"python/{std}/{rel}"] = f"# {rel}\n".encode()
        fam = os_family(plat)
        for rel in PRUNED_PYTHON[fam] + KEPT_PYTHON[fam]:
            py_files[f"python/{rel}"] = f"# {rel}\n".encode()
        archives: dict[str, tuple[str, dict[str, bytes]]] = {
            "python": (f"python-{plat}.tar.gz", py_files),
            "deno": (f"deno-{plat}.zip", {f"deno{exe}": b"deno-" + plat.encode()}),
            "ffmpeg_minimal": (
                f"ffmpeg-minimal-{plat}.tar.gz",
                {
                    f"bin/ffmpeg{exe}": b"ffmpeg-" + plat.encode(),
                    f"bin/ffprobe{exe}": b"ffprobe-" + plat.encode(),
                },
            ),
        }
        for name, (filename, files) in archives.items():
            path = src / filename
            if filename.endswith(".zip"):
                _zip(path, files)
            else:
                _tar(path, files, py_links if name == "python" else None)
            lines += [
                "[[artifact]]",
                f'name = "{name}"',
                f'platform = "{plat}"',
                f'url = "{path.resolve().as_uri()}"',
                f'sha256 = "{_sha(path)}"',
            ]
    server = src / "2.0.2.tar.gz"
    _tar(server, FAKE_SERVER_FILES)
    lines += [
        "[[artifact]]",
        'name = "bgutil-server"',
        'platform = "any"',
        f'url = "{server.resolve().as_uri()}"',
        f'sha256 = "{_sha(server)}"',
    ]
    versions = src / "versions.toml"
    versions.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return versions


# The YouTube-facing distributions the fake install puts in (name ->
# (distribution, version, package files)), as the build seeds them into
# ``.ytop/components``.
FAKE_COMPONENTS = {
    "yt-dlp": ("yt-dlp", "2026.10.26", ["yt_dlp/__init__.py", "yt_dlp/version.py"]),
    "yt-dlp-ejs": ("yt-dlp-ejs", "0.8.0", ["yt_dlp_ejs/__init__.py"]),
    "bgutil-plugin": (
        "bgutil-ytdlp-pot-provider", "2.0.2",
        ["yt_dlp_plugins/extractor/getpot_bgutil.py"],
    ),
    "youtube-transcript-api": (
        "youtube-transcript-api", "1.2.5", ["youtube_transcript_api/__init__.py"],
    ),
}


def _fake_dist(target: Path, dist: str, version: str, files: list[str]) -> None:
    """A wheel install: package files plus a dist-info whose RECORD lists
    them (and, like yt-dlp's, a console script and a data file outside)."""
    info = f"{dist.replace('-', '_')}-{version}.dist-info"
    for rel in files:
        (target / rel).parent.mkdir(parents=True, exist_ok=True)
        (target / rel).write_text(f"# {dist} {version}\nVERSION = {version!r}\n")
    (target / info).mkdir()
    (target / info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\n"
    )
    extra = [f"share/doc/{dist}/README.txt"]
    for rel in extra:
        (target / rel).parent.mkdir(parents=True, exist_ok=True)
        (target / rel).write_text("doc\n")
    record = files + extra + [f"bin/{dist}", f"{info}/METADATA", f"{info}/RECORD"]
    (target / info / "RECORD").write_text("".join(f"{rel},,\n" for rel in record))


def fake_install_deps(plat: str, target: Path) -> None:
    """Stand-in for the uv install.

    ``sharedpkg`` is identical everywhere; ``nativepkg`` has an identical
    ``__init__.py`` but a per-platform extension, like pydantic_core.  The
    YouTube-facing distributions (``FAKE_COMPONENTS``) are pure Python.
    """
    for dist, version, files in FAKE_COMPONENTS.values():
        _fake_dist(target, dist, version, files)
    for pkg in ("sharedpkg", "nativepkg"):
        (target / pkg).mkdir(parents=True, exist_ok=True)
        (target / pkg / "__init__.py").write_text("X = 1\n", encoding="utf-8")
    (target / "sharedpkg/data/x.txt").parent.mkdir()
    (target / "sharedpkg/data/x.txt").write_text("same\n", encoding="utf-8")
    (target / "nativepkg/_native.txt").write_text(
        f"built for {plat}\n", encoding="utf-8"
    )


def make_fake_image(out: Path, platforms: list[str]) -> Path:
    """Build a fake stick image into ``out``; return its ``.ytop`` directory."""
    bs = load_build_stick()
    work = out.parent / f".{out.name}-fake-src"
    versions = make_fake_sources(work / "src", platforms)
    bs.build(
        platforms=platforms,
        out=out,
        cache=work / "cache",
        versions_path=versions,
        offline=False,
        install=fake_install_deps,
        server_install=fake_server_install,
    )
    return out / ".ytop"
