"""Pure-Python code packed into one zip per folder (zipimport).

A stick copies a handful of small files a second to FAT, and the Python
code is thousands of them: ``lib/common`` alone is ~3,300.  So the build
(``tools/build_stick.py``) and the component updater pack each code folder
(``lib/common``, ``lib/<platform>``, each component version) into
``<folder>/lib.zip``; :func:`ytop.stick.boot.code_paths` puts the zip on
``sys.path`` ahead of the folder.

- Anything holding a native extension (``.so``, ``.pyd``, ...) stays loose:
  Python cannot load one from a zip.  So does a namespace package (a folder
  of code with no ``__init__.py``, such as the bgutil plugin's
  ``yt_dlp_plugins``): zipimport cannot import one.  The unit is a whole
  top-level entry (package, module or ``.dist-info``), so a package is
  never split.
- zipimport never reads ``__pycache__``: each shipped pyc goes in as
  ``<module>.pyc`` beside its source (zipimport tries it first; the build's
  pycs are unchecked-hash, so the source's time is never looked at).
  Sources stay for tracebacks.  Nothing is ever written next to the code,
  and a stick starts fast on its first computer, offline.
- Entries have one fixed time and order, so unchanged code zips to the
  same bytes and an upgrade skips the file.

Packages read their own data through ``importlib.resources``, which reads
from a zip as well (yt-dlp's solver scripts, yt-dlp-ejs, certifi), and
yt-dlp finds plugin packages inside zips on ``sys.path``.
"""

from __future__ import annotations

import os
import shutil
import zipfile
from collections.abc import Iterable
from pathlib import Path

from .stick.boot import CODE_ZIP

__all__ = [
    "CODE_ZIP",
    "NATIVE_SUFFIXES",
    "ZIP_EPOCH",
    "pack_folder",
    "read_code_text",
    "top_level_dirs",
    "write_code_zip",
    "zip_entries",
]

# Entry times: the same zip bytes for the same code (DOS time starts in 1980).
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
# Extension modules and the libraries they load: never importable from a zip.
NATIVE_SUFFIXES = (".so", ".pyd", ".dylib", ".dll")
_PYC_TAG = ".cpython-312"


def _is_native(path: Path) -> bool:
    name = path.name.lower()
    # ``x.cpython-312-x86_64-linux-gnu.so``, ``libfoo.so.1``, ``x.pyd``.
    return name.endswith(NATIVE_SUFFIXES) or ".so." in name


def _has_namespace(entry: Path) -> bool:
    """A folder under ``entry`` (or ``entry`` itself) holding Python
    modules but no ``__init__.py``: a namespace package."""
    if not entry.is_dir():
        return False
    for folder in [entry, *(p for p in entry.rglob("*") if p.is_dir())]:
        if folder.name == "__pycache__":
            continue
        if not (folder / "__init__.py").is_file() and any(folder.glob("*.py")):
            return True
    return False


def zip_entries(root: Path, files: Iterable[Path]) -> dict[str, Path]:
    """Zip names for ``files`` under ``root``: a ``__pycache__`` pyc of
    CPython 3.12 becomes ``<module>.pyc`` beside its source; other pycs
    (optimised, another Python, stray loose ones) are left out."""
    entries: dict[str, Path] = {}
    for path in files:
        rel = path.relative_to(root)
        if rel.parent.name == "__pycache__":
            name, _tag, ext = rel.name.partition(_PYC_TAG)
            if ext != ".pyc":
                continue
            arc = (rel.parent.parent / f"{name}.pyc").as_posix()
        elif rel.suffix == ".pyc":
            continue
        else:
            arc = rel.as_posix()
        entries[arc] = path
    return entries


def write_code_zip(target: Path, entries: dict[str, Path]) -> None:
    """Write ``entries`` (zip name -> file) to ``target``, stored (zipimport
    reads stored members fastest; a stick's cost is files, not bytes), in a
    fixed order with a fixed time.  Written beside and renamed into place,
    so a cut-off write never leaves a half zip under the real name."""
    partial = target.with_name(target.name + ".partial")
    with zipfile.ZipFile(partial, "w", zipfile.ZIP_STORED) as zf:
        for arc in sorted(entries):
            info = zipfile.ZipInfo(arc, date_time=ZIP_EPOCH)
            info.external_attr = 0o644 << 16
            zf.writestr(info, entries[arc].read_bytes())
    os.replace(partial, target)


def _files(entry: Path) -> list[Path]:
    if entry.is_file():
        return [entry]
    return [p for p in sorted(entry.rglob("*")) if p.is_file()]


def pack_folder(folder: Path) -> int:
    """Move ``folder``'s pure-Python top-level entries into
    ``folder/lib.zip``; returns how many files went in (0: nothing to pack,
    and no zip is written).  Dotfiles (a component's ``.complete``),
    entries holding native code and namespace packages stay; so does the
    folder."""
    target = folder / CODE_ZIP
    cache = folder / "__pycache__"
    packed: list[Path] = []
    modules: set[str] = set()  # top-level modules packed (for their pycs)
    for entry in sorted(folder.iterdir()):
        if entry.name.startswith(".") or entry == target or entry == cache:
            continue
        if entry.is_symlink():
            continue
        files = _files(entry)
        if any(_is_native(f) for f in files) or _has_namespace(entry):
            continue
        packed.append(entry)
        if entry.is_file() and entry.suffix == ".py":
            modules.add(entry.stem)
    files = [f for entry in packed for f in _files(entry)]
    pycs = []
    if cache.is_dir():
        pycs = [p for p in sorted(cache.iterdir())
                if p.is_file() and p.name.partition(_PYC_TAG)[0] in modules]
    entries = zip_entries(folder, files + pycs)
    if not entries:
        return 0
    write_code_zip(target, entries)
    for entry in packed:
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    for pyc in pycs:
        pyc.unlink()
    if cache.is_dir() and not any(cache.iterdir()):
        cache.rmdir()
    return len(entries)


def _zip_names(folder: Path) -> list[str]:
    try:
        with zipfile.ZipFile(folder / CODE_ZIP) as zf:
            return zf.namelist()
    except (OSError, zipfile.BadZipFile):
        return []


def top_level_dirs(folder: Path) -> set[str]:
    """Names of the top-level folders (packages, ``.dist-info``) a code
    folder holds, loose or in its zip.  The zip is opened and closed here:
    a zip left open would stop Windows deleting or replacing it."""
    found = {name.split("/", 1)[0] for name in _zip_names(folder) if "/" in name}
    try:
        found.update(p.name for p in folder.iterdir() if p.is_dir())
    except OSError:
        pass
    return found


def read_code_text(folder: Path, rel: str) -> str | None:
    """A text file of a code folder (``rel`` is ``/``-separated), loose or
    in its zip; None when it has none."""
    try:
        return (folder / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    try:
        with zipfile.ZipFile(folder / CODE_ZIP) as zf:
            return zf.read(rel).decode("utf-8", errors="replace")
    except (OSError, KeyError, zipfile.BadZipFile):
        return None
