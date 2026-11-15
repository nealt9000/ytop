"""The image manifest: ``.ytop/manifest.sha256``, one ``sha256sum`` line per
image-owned file (paths relative to ``.ytop/``).  It never lists
``build.json`` or itself.  Upgrades compare manifests to copy only what
changed.

``build.json`` records the manifest's own sha256 (``manifest_sha256``): a
manifest is trusted only when that matches, so one left behind by an older
upgrade (which swaps the files but not the manifest) or edited by hand is
treated as missing and the files are hashed instead."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath

MANIFEST_NAME = "manifest.sha256"
# boot.py, plus the licence files every image ships at the top of .ytop/.
LEGAL_FILES = ("LICENSE", "THIRD_PARTY_NOTICES.md")
OWNED_ROOT_FILES = ("boot.py", *LEGAL_FILES)
OWNED_DIRS = ("app", "lib", "runtime")
# The build.json field holding the sha256 of the manifest beside it.
MANIFEST_HASH_KEY = "manifest_sha256"

_LINE = re.compile(r"([0-9a-f]{64})  (.+)")


def sha256_file(path: Path, on_chunk: Callable[[int], None] | None = None) -> str:
    # Imported here: the web package imports makestick, which imports this.
    from .web.fsops import CHUNK_SIZE

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
            if on_chunk is not None:
                on_chunk(len(chunk))
    return digest.hexdigest()


def read_manifest(path: Path) -> dict[str, str] | None:
    """rel -> hash, or ``None`` if the file is missing or any line is bad."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    entries: dict[str, str] = {}
    for line in text.splitlines():
        match = _LINE.fullmatch(line)
        if match is None:
            return None
        digest, rel = match.groups()
        pure = PurePosixPath(rel)
        if pure.is_absolute() or ".." in pure.parts or "\\" in rel:
            return None
        entries[rel] = digest
    return entries


def read_trusted_manifest(ytop: Path) -> dict[str, str] | None:
    """``ytop``'s manifest, or ``None`` unless its ``build.json`` names the
    manifest's sha256 (module docstring)."""
    try:
        build = json.loads((ytop / "build.json").read_text(encoding="utf-8"))
        recorded = build.get(MANIFEST_HASH_KEY) if isinstance(build, dict) else None
        if not isinstance(recorded, str) or recorded != sha256_file(ytop / MANIFEST_NAME):
            return None
    except (OSError, ValueError):
        return None
    return read_manifest(ytop / MANIFEST_NAME)


def write_manifest(path: Path, entries: dict[str, str]) -> None:
    """Atomic: temp file, fsync, ``os.replace``.  Lines sorted by path."""
    body = "".join(f"{entries[rel]}  {rel}\n" for rel in sorted(entries))
    temp = path.with_name(path.name + ".tmp")
    with open(temp, "wb") as handle:
        handle.write(body.encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def hash_tree(
    root: Path,
    rels: Iterable[str],
    on_file: Callable[[str], None] | None = None,
) -> dict[str, str]:
    entries: dict[str, str] = {}
    for rel in rels:
        if on_file is not None:
            on_file(rel)
        entries[rel] = sha256_file(root / rel)
    return entries


def for_platforms(entries: dict[str, str], platforms: Iterable[str]) -> dict[str, str]:
    prefixes = ["app/", "lib/common/"]
    for plat in platforms:
        prefixes += [f"lib/{plat}/", f"runtime/{plat}/"]
    keep = tuple(prefixes)
    return {
        rel: digest
        for rel, digest in entries.items()
        if rel in OWNED_ROOT_FILES or rel.startswith(keep)
    }
