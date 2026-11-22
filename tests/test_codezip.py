"""Packing code folders into lib.zip (#37): what goes in, and that Python
imports it (pycs, data via importlib.resources, metadata, namespaces)."""

from __future__ import annotations

import compileall
import json
import py_compile
import subprocess
import sys
import zipfile
from pathlib import Path

from ytop.codezip import pack_folder, read_code_text, top_level_dirs
from ytop.stick.boot import code_paths


def _tree(root: Path, files: dict[str, str | bytes]) -> None:
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(data)


def _compile(folder: Path) -> None:
    # As the build does: co_filename never holds the build folder.
    compileall.compile_dir(
        folder, quiet=2,
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
        stripdir=str(folder), prependdir=".ytop/x",
    )


FILES = {
    "pkg/__init__.py": "from importlib.resources import files\n"
                       "DATA = files(__name__).joinpath('data/x.txt').read_text()\n",
    "pkg/data/x.txt": "hello\n",
    "pkg-1.2.dist-info/METADATA": "Metadata-Version: 2.1\nName: pkg\nVersion: 1.2\n",
    "pkg-1.2.dist-info/RECORD": "",
    "single.py": "VALUE = 7\n",
    # A namespace package (as the bgutil plugin's yt_dlp_plugins): zipimport
    # cannot import one, so it stays loose.
    "ns_plugins/extractor/here.py": "WHERE = 'here'\n",
    # A regular package with a namespace subpackage: loose as a whole.
    "mixed/__init__.py": "",
    "mixed/sub/mod.py": "X = 1\n",
    "native/__init__.py": "",
    "native/_speed.cpython-312-x86_64-linux-gnu.so": b"\x7fELF",
    ".complete": "",
}


def test_pack_keeps_native_and_dotfiles_loose(tmp_path):
    _tree(tmp_path, FILES)
    _compile(tmp_path)
    assert pack_folder(tmp_path) > 0
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        ".complete", "lib.zip", "mixed", "native", "ns_plugins",
    ]
    with zipfile.ZipFile(tmp_path / "lib.zip") as zf:
        names = zf.namelist()
        assert names == sorted(names)
        assert {i.date_time for i in zf.infolist()} == {(1980, 1, 1, 0, 0, 0)}
    # Each pyc beside its source (zipimport ignores __pycache__).
    assert "pkg/__init__.pyc" in names and "pkg/__init__.py" in names
    assert "single.pyc" in names and "single.py" in names
    assert not [n for n in names if "__pycache__" in n
                or n.startswith(("native", "ns_plugins", "mixed"))]
    assert "pkg/data/x.txt" in names


def test_pack_is_deterministic(tmp_path):
    for name in ("a", "b"):
        _tree(tmp_path / name, FILES)
        _compile(tmp_path / name)
        pack_folder(tmp_path / name)
    assert (tmp_path / "a/lib.zip").read_bytes() == (tmp_path / "b/lib.zip").read_bytes()


def test_pack_with_nothing_to_pack_writes_no_zip(tmp_path):
    _tree(tmp_path, {".complete": "", "native/x.so": b"\x7fELF"})
    assert pack_folder(tmp_path) == 0
    assert not (tmp_path / "lib.zip").exists()


def test_python_imports_a_packed_folder(tmp_path):
    folder = tmp_path / "code"
    _tree(folder, FILES)
    _compile(folder)
    pack_folder(folder)
    other = tmp_path / "other"
    _tree(other, {"ns_plugins/extractor/loose.py": "WHERE = 'loose'\n"})
    _compile(other)
    paths = [str(p) for p in [*code_paths(folder), other]]
    script = (
        "import sys, json; sys.path[0:0] = " + json.dumps(paths) + "\n"
        "import importlib.metadata as md, pkg, single\n"
        "import ns_plugins.extractor.here as z, ns_plugins.extractor.loose as lo\n"
        "print(json.dumps([pkg.DATA, single.VALUE, md.version('pkg'), z.WHERE, lo.WHERE,"
        " pkg.__file__, type(pkg.__loader__).__name__]))\n"
    )
    before = sorted(p.as_posix() for p in tmp_path.rglob("*"))
    out = subprocess.run([sys.executable, "-I", "-S", "-c", script],
                         capture_output=True, text=True, check=True).stdout
    data, value, version, zipped, loose, path, loader = json.loads(out)
    assert (data, value, version, zipped, loose) == ("hello\n", 7, "1.2", "here", "loose")
    assert path.startswith(str(folder / "lib.zip")) and loader == "zipimporter"
    assert sorted(p.as_posix() for p in tmp_path.rglob("*")) == before  # nothing written


def test_readers_see_loose_and_packed(tmp_path):
    _tree(tmp_path, FILES)
    pack_folder(tmp_path)
    assert top_level_dirs(tmp_path) >= {"pkg", "pkg-1.2.dist-info", "native", "ns_plugins"}
    assert read_code_text(tmp_path, "pkg-1.2.dist-info/METADATA").startswith("Metadata")
    assert read_code_text(tmp_path, "native/__init__.py") == ""
    assert read_code_text(tmp_path, "missing.py") is None
    # A folder that was never packed reads the same way.
    loose = tmp_path / "loose"
    _tree(loose, {"pkg/__init__.py": "x = 1\n"})
    assert top_level_dirs(loose) == {"pkg"}
    assert read_code_text(loose, "pkg/__init__.py") == "x = 1\n"
    assert code_paths(loose) == [loose]
    assert code_paths(tmp_path) == [tmp_path / "lib.zip", tmp_path]


def test_dist_infos_and_deno_minimum_read_packed_components(tmp_path):
    from ytop import updater as up

    ejs = tmp_path / "ejs"
    _tree(ejs, {
        "yt_dlp_ejs/__init__.py": "",
        "yt_dlp_ejs-0.9.0.dist-info/METADATA":
            "Name: yt-dlp-ejs\nRequires-External: deno (>=2.3.0)\n\nbody\n",
    })
    ytdlp = tmp_path / "ytdlp"
    _tree(ytdlp, {
        "yt_dlp/utils/_jsruntime.py":
            "class DenoJsRuntime:\n    MIN_SUPPORTED_VERSION = (2, 5, 0)\n",
    })
    pack_folder(ejs)
    pack_folder(ytdlp)
    assert up._dist_infos(ejs, "yt-dlp-ejs") == [("yt_dlp_ejs-0.9.0.dist-info", "0.9.0")]
    assert up.deno_minimum({"yt-dlp-ejs": ejs, "yt-dlp": ytdlp}) == "2.5.0"
