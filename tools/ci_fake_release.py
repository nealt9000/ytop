#!/usr/bin/env python3
"""Build the fake releases the self-update CI job serves (test-only).

    ci_fake_release.py app-release  <image .ytop> <work dir> <serve root> [--repo test/ytop]
    ci_fake_release.py ytdlp-release <image .ytop> <serve root> [--version 2099.1.1]

``app-release`` packages the image as release A (``<work>/release-a``) and a
copy of it with ONE app file changed (``app/ytop/__init__.py``: version
bumped, its bytecode, the file list and ``build.json`` regenerated with the
project's own helpers) as release B, which it puts in
``<serve root>/<repo>/v<version>/`` for ``fake_release_server.py``.  Only
the ``app`` piece may differ between A and B.

``ytdlp-release`` re-packs the image's seeded yt-dlp as a wheel with its
version bumped and re-hashed, plus ``SHA2-256SUMS``, as a yt-dlp release."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import py_compile
import shutil
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import package_release  # noqa: E402

from ytop.manifest import (  # noqa: E402
    MANIFEST_HASH_KEY,
    MANIFEST_NAME,
    hash_tree,
    read_manifest,
    sha256_file,
    write_manifest,
)

CHANGED = "app/ytop/__init__.py"
YTDLP_REPO = "yt-dlp/yt-dlp"


def bump(version: str) -> str:
    parts = version.split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)


def _link_or_copy(src: str, dst: str) -> None:
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _replace(path: Path, data: bytes) -> None:
    """Write ``path`` without touching a hard-linked original."""
    temp = path.with_name(path.name + ".ci-tmp")
    temp.write_bytes(data)
    os.replace(temp, path)


def make_second_build(image: Path, work: Path) -> tuple[Path, str]:
    """A copy of ``image`` (``<work>/b/.ytop``) that differs in one app file;
    returns it and its version."""
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("ci_fake_release: the image's bytecode is CPython 3.12's")
    build = json.loads((image / "build.json").read_text(encoding="utf-8"))
    version = bump(str(build["version"]))
    out = work / "b" / ".ytop"
    if out.exists():
        shutil.rmtree(out)
    out.parent.mkdir(parents=True)
    shutil.copytree(image, out, copy_function=_link_or_copy)

    source = out / CHANGED
    text = source.read_text(encoding="utf-8")
    old = f'__version__ = "{build["version"]}"'
    if old not in text:
        raise SystemExit(f"ci_fake_release: {CHANGED} has no {old}")
    _replace(source, (text.replace(old, f'__version__ = "{version}"')
                      + "# ci: second build\n").encode("utf-8"))
    # Unchecked-hash bytecode is used without looking at the source: a stale
    # one would keep running the old code.
    pyc = out / "app/ytop/__pycache__/__init__.cpython-312.pyc"
    if pyc.is_file():
        py_compile.compile(
            str(source), cfile=str(pyc), dfile=".ytop/" + CHANGED, doraise=True,
            invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
        )

    manifest = read_manifest(out / MANIFEST_NAME)
    if manifest is None:
        raise SystemExit("ci_fake_release: the image has no valid file list")
    write_manifest(out / MANIFEST_NAME, hash_tree(out, sorted(manifest)))
    build.update({
        "version": version,
        "build_id": f"{build['build_id']}-b",
        MANIFEST_HASH_KEY: sha256_file(out / MANIFEST_NAME),
    })
    _replace(out / "build.json", (json.dumps(build, indent=2) + "\n").encode("utf-8"))
    return out, version


def _package(image: Path, dest: Path, version: str) -> dict[str, object]:
    # package_release checks the image against the checkout's pyproject.
    real = package_release.project_version
    package_release.project_version = lambda: version
    try:
        return package_release.package(image, dest)
    finally:
        package_release.project_version = real


def app_releases(image: Path, work: Path, serve_root: Path, repo: str) -> tuple[Path, Path]:
    """Release A (the image itself) in ``<work>/release-a``; release B in the
    serve root.  Returns both folders."""
    work.mkdir(parents=True, exist_ok=True)
    build = json.loads((image / "build.json").read_text(encoding="utf-8"))
    a_out = work / "release-a"
    if a_out.exists():
        shutil.rmtree(a_out)
    a_doc = _package(image, a_out, str(build["version"]))
    second, version = make_second_build(image, work)
    b_out = serve_root / repo / f"v{version}"
    if b_out.exists():
        shutil.rmtree(b_out)
    b_doc = _package(second, b_out, version)
    a_pieces, b_pieces = a_doc["pieces"], b_doc["pieces"]
    differing = sorted(
        name for name in a_pieces  # type: ignore[attr-defined]
        if a_pieces[name]["sha256"] != b_pieces[name]["sha256"]  # type: ignore[index]
    )
    if differing != ["app"] or set(a_pieces) != set(b_pieces):  # type: ignore[call-overload]
        raise SystemExit(f"ci_fake_release: pieces that differ: {differing}, expected ['app']")
    shutil.rmtree(second.parent)
    return a_out, b_out


def _record_line(rel: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{rel},sha256={digest},{len(data)}\n"


def ytdlp_release(image: Path, serve_root: Path, version: str = "2099.1.1") -> Path:
    """The image's seeded yt-dlp, re-packed as a wheel named ``version``."""
    doc = json.loads((image / "components/current.json").read_text(encoding="utf-8"))
    old = doc["current"]["yt-dlp"]
    folder = image / "components/yt-dlp" / old
    old_info = f"yt_dlp-{old}.dist-info"
    new_info = f"yt_dlp-{version}.dist-info"
    if not (folder / old_info).is_dir():
        raise SystemExit(f"ci_fake_release: {folder} has no {old_info}")
    entries: dict[str, bytes] = {}
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder).as_posix()
        if (not path.is_file() or path.name == ".complete" or path.suffix == ".pyc"
                or "__pycache__" in rel or rel == f"{old_info}/RECORD"):
            continue
        data = path.read_bytes()
        if path.suffix == ".py" or rel.startswith(f"{old_info}/"):
            data = data.replace(old.encode(), version.encode())
        entries[new_info + rel[len(old_info):] if rel.startswith(old_info) else rel] = data
    out = serve_root / YTDLP_REPO / version
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    name = f"yt_dlp-{version}-py3-none-any.whl"
    record = "".join(_record_line(rel, data) for rel, data in entries.items())
    with zipfile.ZipFile(out / name, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, data in entries.items():
            zf.writestr(rel, data)
        zf.writestr(f"{new_info}/RECORD", record + f"{new_info}/RECORD,,\n")
    (out / "SHA2-256SUMS").write_text(
        f"{sha256_file(out / name)}  {name}\n", encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("app-release")
    p.add_argument("image", type=Path)
    p.add_argument("work", type=Path)
    p.add_argument("serve_root", type=Path)
    p.add_argument("--repo", default="test/ytop")
    p = sub.add_parser("ytdlp-release")
    p.add_argument("image", type=Path)
    p.add_argument("serve_root", type=Path)
    p.add_argument("--version", default="2099.1.1")
    opts = ap.parse_args(argv)
    if opts.command == "app-release":
        a, b = app_releases(opts.image, opts.work, opts.serve_root, opts.repo)
        print(f"release A: {a}\nrelease B: {b}")
    else:
        print(f"yt-dlp release: {ytdlp_release(opts.image, opts.serve_root, opts.version)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
