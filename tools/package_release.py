#!/usr/bin/env python3
"""Package a built stick image into release files: one deterministic .tar.gz
per piece, the seeded component store, ytop-release.json and SHA256SUMS.

    python tools/package_release.py <image .ytop dir> <out dir>

Pieces partition the image manifest exactly (every manifest path is in one
piece); paths inside an archive are relative to ``.ytop/``.  Archives are
byte-for-byte reproducible: sorted entries, mtime 0, uid/gid 0, normalised
modes, no symlinks, gzip header without name or time."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
import tarfile
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from ytop.manifest import MANIFEST_NAME, read_manifest, sha256_file  # noqa: E402

RELEASE_JSON = "ytop-release.json"
SUMS = "SHA256SUMS"
SEED_FILE = "components-seed.tar.gz"
SCHEMA = 1


class PackageError(Exception):
    pass


def project_version() -> str:
    doc = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return str(doc["project"]["version"])


def piece_of(rel: str, platforms: list[str]) -> str:
    parts = rel.split("/")
    if len(parts) == 1:
        return "root"
    if parts[0] == "app":
        return "app"
    if parts[0] == "lib" and len(parts) > 2:
        if parts[1] == "common":
            return "lib-common"
        if parts[1] in platforms:
            return f"lib-{parts[1]}"
    if parts[0] == "runtime" and len(parts) > 3 and parts[1] in platforms:
        if parts[2] in ("python", "bin"):
            return f"runtime-{parts[1]}-{parts[2]}"
    raise PackageError(f"{rel}: belongs to no release piece")


def _check_plain(path: Path, rel: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise PackageError(f"{rel}: a symlink or not a regular file (FAT32 has no symlinks)")


def write_archive(dest: Path, base: Path, rels: list[str]) -> None:
    """Deterministic tar.gz of ``rels`` (relative to ``base``)."""
    rels = sorted(rels)
    for rel in rels:
        _check_plain(base / rel, rel)
    with open(dest, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0,
                           compresslevel=9) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
                for rel in rels:
                    path = base / rel
                    info = tarfile.TarInfo(rel)
                    info.size = path.stat().st_size
                    info.mtime = 0
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
                    with open(path, "rb") as handle:
                        tar.addfile(info, handle)


def _entry(path: Path) -> dict[str, object]:
    return {"file": path.name, "sha256": sha256_file(path), "size": path.stat().st_size}


def package(image: Path, out: Path) -> dict[str, object]:
    build_path = image / "build.json"
    try:
        build = json.loads(build_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PackageError(f"{image}: no readable build.json ({exc})") from exc
    manifest = read_manifest(image / MANIFEST_NAME)
    if manifest is None:
        raise PackageError(f"{image}: no valid {MANIFEST_NAME}")
    if build.get("manifest_sha256") != sha256_file(image / MANIFEST_NAME):
        raise PackageError("build.json does not vouch for manifest.sha256")
    platforms = [str(p) for p in build["platforms"]]
    if build.get("version") != project_version():
        raise PackageError(
            f"image version {build.get('version')} != pyproject {project_version()}"
        )

    for path in image.rglob("*"):
        if path.is_symlink():
            raise PackageError(
                f"{path.relative_to(image).as_posix()}: a symlink (FAT32 has none)"
            )

    groups: dict[str, list[str]] = {}
    for rel in manifest:
        groups.setdefault(piece_of(rel, platforms), []).append(rel)

    out.mkdir(parents=True, exist_ok=True)
    pieces: dict[str, object] = {}
    for name in sorted(groups):
        rels = sorted(groups[name])
        dest = out / f"{name}.tar.gz"
        write_archive(dest, image, rels)
        pieces[name] = {**_entry(dest), "files": rels}

    store = image / "components"
    current_path = store / "current.json"
    if not current_path.is_file():
        raise PackageError("the image has no components/current.json")
    seed_rels = sorted(
        p.relative_to(image).as_posix() for p in store.rglob("*") if p.is_file() or p.is_symlink()
    )
    seed = out / SEED_FILE
    write_archive(seed, image, seed_rels)
    current = json.loads(current_path.read_text(encoding="utf-8")).get("current")
    # App updates update components older than these first: never publish
    # a release without them.
    if not isinstance(current, dict) or not current or not all(
        isinstance(k, str) and isinstance(v, str) and v for k, v in current.items()
    ):
        raise PackageError("components/current.json lists no versions (min_components)")

    doc = {
        "schema": SCHEMA,
        "version": build["version"],
        "build_id": build["build_id"],
        "platforms": platforms,
        "build": build,
        "manifest": dict(sorted(manifest.items())),
        "pieces": pieces,
        "components_seed": _entry(seed),
        "min_components": dict(sorted(current.items())),
    }
    (out / RELEASE_JSON).write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    names = sorted(p.name for p in out.iterdir() if p.name != SUMS)
    lines = [f"{sha256_file(out / n)}  {n}\n" for n in names]
    (out / SUMS).write_text("".join(lines), encoding="utf-8")
    return doc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("image", type=Path, help="the image's .ytop directory")
    ap.add_argument("out", type=Path)
    opts = ap.parse_args(argv)
    try:
        doc = package(opts.image, opts.out)
    except PackageError as exc:
        print(f"package_release: {exc}", file=sys.stderr)
        return 1
    total = sum(p.stat().st_size for p in opts.out.iterdir())
    print(f"packaged {doc['version']} ({doc['build_id']}): "
          f"{len(doc['pieces'])} pieces, {total / 1e6:.1f} MB -> {opts.out}")  # type: ignore[arg-type]
    return 0


if __name__ == "__main__":
    sys.exit(main())
