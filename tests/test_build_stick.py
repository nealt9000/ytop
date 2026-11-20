import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

from stick_fakes import (
    FAKE_SERVER_FILES,
    KEPT_PYTHON,
    KEPT_STDLIB,
    PRUNED_PYTHON,
    PRUNED_STDLIB,
    STALE_STDLIB_PYC,
    _sha,
    _tar,
    fake_install_deps,
    fake_server_install,
    load_build_stick,
    make_fake_image,
    make_fake_sources,
    os_family,
    stdlib_dir,
)

bs = load_build_stick()
PLATS = ["linux-x86_64", "windows-x86_64"]
LAUNCHERS = [
    "Start YTOP (Windows).cmd",
    "Start YTOP (Mac).command",
    "start-ytop-linux.sh",
    "README.txt",
]


def _unchecked_hash_pyc(path, data: bytes | None = None) -> bool:
    """A CPython 3.12 pyc validated by neither mtime nor hash (PEP 552)."""
    data = path.read_bytes() if data is None else data
    return (
        data[:4] == importlib.util.MAGIC_NUMBER
        and int.from_bytes(data[4:8], "little") == 0b01  # hash-based, unchecked
    )


def build(tmp_path, platforms=PLATS, install=fake_install_deps, **kw):
    kw.setdefault("server_install", fake_server_install)
    versions = make_fake_sources(tmp_path / "src", platforms)
    out = tmp_path / "stick"
    bs.build(
        platforms=platforms,
        out=out,
        cache=tmp_path / "cache",
        versions_path=versions,
        offline=kw.pop("offline", False),
        install=install,
        **kw,
    )
    return out


def test_build_layout(tmp_path):
    out = build(tmp_path)
    yt = out / ".ytop"
    assert (yt / "app/ytop/__init__.py").is_file()
    assert (yt / "app/ytop/cli.py").is_file()
    assert _unchecked_hash_pyc(yt / "app/ytop/__pycache__/cli.cpython-312.pyc")
    assert (yt / "boot.py").read_bytes() == (
        bs.REPO / "src/ytop/stick/boot.py"
    ).read_bytes()
    assert (yt / "lib/common").is_dir()
    lin, win = yt / "runtime/linux-x86_64", yt / "runtime/windows-x86_64"
    # Symlinks are FAT-unsafe: the interpreter is moved to bin/python3 (no
    # duplicate copies); every other link is dropped.
    py3 = lin / "python/bin/python3"
    assert py3.is_file() and not py3.is_symlink()
    assert py3.read_bytes() == b"py-linux-x86_64"
    assert not (lin / "python/bin/python3.12").exists()
    assert not (lin / "python/bin/python").exists()
    assert not (lin / "python/lib/libpython3.12.so").exists()
    assert not [p for p in yt.rglob("*") if p.is_symlink()]
    assert (win / "python/python.exe").is_file()
    for name in ("ffmpeg", "ffprobe", "deno"):
        assert (lin / "bin" / name).read_bytes() == f"{name}-linux-x86_64".encode()
        assert (win / "bin" / f"{name}.exe").is_file()
    assert (lin / "bin/deno").stat().st_mode & 0o111
    for name in LAUNCHERS:
        assert (out / name).read_bytes() == (bs.LAUNCHERS / name).read_bytes()
    # README placeholders stay unfilled for makestick.
    assert "{build_id}" in (out / "README.txt").read_text()
    assert (out / "start-ytop-linux.sh").stat().st_mode & 0o111

    build_json = json.loads((yt / "build.json").read_text())
    assert re.fullmatch(r"\d{12}-\w+(-dirty)?", build_json["build_id"])
    assert build_json["platforms"] == PLATS
    assert build_json["version"] == "0.1.0"
    assert set(build_json["sizes"]) == {*PLATS, "common", "app"}
    assert all(isinstance(v, int) and v > 0 for v in build_json["sizes"].values())
    assert build_json["sources"]["python"]["version"] == "3.12.15"
    assert build_json["sources"]["deno"]["version"] == "2.9.8"
    assert build_json["sources"]["ffmpeg_minimal"]["release"] == "ffmpeg-minimal-1"


def test_sha_mismatch_refused(tmp_path):
    versions = make_fake_sources(tmp_path / "src", ["linux-x86_64"])
    text = versions.read_text()
    good = re.search(r'sha256 = "([0-9a-f]{64})"', text).group(1)
    versions.write_text(text.replace(good, "0" * 64, 1))
    with pytest.raises(bs.BuildError, match="sha256 mismatch"):
        bs.build(
            platforms=["linux-x86_64"],
            out=tmp_path / "stick",
            cache=tmp_path / "cache",
            versions_path=versions,
            offline=False,
            install=fake_install_deps,
        )
    # A refused download is not left in the cache.
    assert not [p for p in (tmp_path / "cache").iterdir() if p.is_file()]


def test_offline_requires_cache(tmp_path):
    with pytest.raises(bs.BuildError, match="not in the cache"):
        build(tmp_path, offline=True)
    # Once cached, an offline build works even if the sources are gone.
    build(tmp_path)
    for p in (tmp_path / "src").glob("*.gz"):
        p.unlink()
    build(tmp_path, offline=True)


def test_dedupe_common(tmp_path):
    yt = build(tmp_path) / ".ytop"
    # A package identical on every platform moves to common as a whole.
    assert (yt / "lib/common/sharedpkg/__init__.py").is_file()
    assert (yt / "lib/common/sharedpkg/data/x.txt").is_file()
    for plat in PLATS:
        assert not (yt / f"lib/{plat}/sharedpkg").exists()
        native = yt / f"lib/{plat}/nativepkg/_native.txt"
        assert native.read_text() == f"built for {plat}\n"
        # Never split a package across lib/common and lib/<plat>: Python
        # cannot import a regular package from two sys.path entries.
        assert (yt / f"lib/{plat}/nativepkg/__init__.py").is_file()
    assert not (yt / "lib/common/nativepkg").exists()


def _stdlib_entries(yt, plat) -> dict[str, bytes]:
    import zipfile

    with zipfile.ZipFile(bs.stdlib_zip(yt / f"runtime/{plat}/python", plat)) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def test_stdlib_pruned(tmp_path):
    yt = build(tmp_path) / ".ytop"
    for plat in PLATS:
        entries = _stdlib_entries(yt, plat)
        for rel in KEPT_STDLIB:
            assert rel in entries, rel
        for rel in PRUNED_STDLIB:
            assert rel not in entries, rel
        # Every pyc is the build's own (Ruling R12), beside its source where
        # zipimport looks; stale ones from the tarball are replaced.
        assert STALE_STDLIB_PYC == ["__pycache__/os.cpython-312.pyc"]
        assert _unchecked_hash_pyc(None, entries["os.pyc"])
        pycs = [name for name in entries if name.endswith(".pyc")]
        assert pycs and all(_unchecked_hash_pyc(None, entries[n]) for n in pycs)
        assert not [name for name in entries if "__pycache__" in name]
        python = yt / f"runtime/{plat}/python"
        fam = os_family(plat)
        for rel in KEPT_PYTHON[fam]:
            assert (python / rel).is_file(), rel
        for rel in PRUNED_PYTHON[fam]:
            assert not (python / rel).exists(), rel
    assert not (yt / "runtime/windows-x86_64/python/tcl").exists()
    assert not (yt / "runtime/windows-x86_64/python/Scripts").exists()
    assert (yt / "runtime/linux-x86_64/python/bin/python3").is_file()
    assert (yt / "runtime/windows-x86_64/python/python.exe").is_file()


def test_case_only_name_clash_refused(tmp_path):
    (tmp_path / "share/terminfo/p").mkdir(parents=True)
    (tmp_path / "share/terminfo/P").mkdir()
    (tmp_path / "share/Readme").write_text("x")
    with pytest.raises(bs.BuildError, match="share/terminfo/P / share/terminfo/p"):
        bs.check_case_collisions(tmp_path)
    (tmp_path / "share/terminfo/P").rmdir()
    bs.check_case_collisions(tmp_path)


def test_build_through_symlinked_out_dir(tmp_path):
    # macOS /var -> /private/var, Fedora Atomic /home -> /var/home.
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    versions = make_fake_sources(tmp_path / "src", ["linux-x86_64"])
    bs.build(
        platforms=["linux-x86_64"],
        out=tmp_path / "link/stick",
        cache=tmp_path / "cache",
        versions_path=versions,
        offline=False,
        install=fake_install_deps,
        server_install=fake_server_install,
    )
    py3 = real / "stick/.ytop/runtime/linux-x86_64/python/bin/python3"
    assert py3.is_file() and py3.read_bytes() == b"py-linux-x86_64"


def _repin_python(versions, tarball):
    """Point the (single) python artifact in ``versions`` at ``tarball``."""
    text = versions.read_text()
    block = re.search(
        r'name = "python"\n.*?\nurl = "([^"]+)"\nsha256 = "([0-9a-f]{64})"', text, re.S
    )
    text = text.replace(block.group(1), tarball.resolve().as_uri())
    text = text.replace(block.group(2), _sha(tarball))
    versions.write_text(text)


def test_missing_interpreter_fails_closed(tmp_path):
    versions = make_fake_sources(tmp_path / "src", ["linux-x86_64"])
    # A python tarball whose bin/python3 link points outside the tree.
    broken = tmp_path / "src/broken.tar.gz"
    _tar(
        broken,
        {"python/lib/python3.12/os.py": b"#"},
        {"python/bin/python3": "../../elsewhere/python3.12"},
    )
    _repin_python(versions, broken)
    with pytest.raises(bs.BuildError, match="no interpreter"):
        bs.build(
            platforms=["linux-x86_64"],
            out=tmp_path / "stick",
            cache=tmp_path / "cache",
            versions_path=versions,
            offline=False,
            install=fake_install_deps,
        )


def test_duplicate_artifact_rejected(tmp_path):
    versions = make_fake_sources(tmp_path / "src", ["linux-x86_64"])
    text = versions.read_text()
    first = re.search(r"\[\[artifact\]\]\n(?:[a-z0-9_]+ = .*\n)+", text).group(0)
    versions.write_text(text + first)
    with pytest.raises(bs.BuildError, match="duplicate artifact"):
        bs.load_versions(versions)


def test_git_revision_marks_dirty(monkeypatch):
    outputs = {"rev-parse": "abc1234", "status": " M src/ytop/cli.py"}

    def fake_run(cmd, **kw):
        return bs.subprocess.CompletedProcess(cmd, 0, stdout=outputs[cmd[1]] + "\n")

    monkeypatch.setattr(bs.subprocess, "run", fake_run)
    assert bs.git_revision() == "abc1234-dirty"
    outputs["status"] = ""
    assert bs.git_revision() == "abc1234"


def test_size_budget_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(bs, "SIZE_BUDGET", 1024)
    with pytest.raises(bs.BuildError, match="budget"):
        build(tmp_path)


def test_rebuild_keeps_library_data(tmp_path):
    out = build(tmp_path)
    (out / ".ytop/library.json").write_text("{}")
    (out / "Videos").mkdir()
    build(tmp_path)
    assert (out / ".ytop/library.json").is_file()
    assert (out / "Videos").is_dir()


def test_unknown_platform_rejected(tmp_path):
    with pytest.raises(bs.BuildError, match="unknown platform"):
        build(tmp_path, platforms=["beos-m68k"])


def test_make_fake_image(tmp_path):
    yt = make_fake_image(tmp_path / "img", ["linux-x86_64"])
    assert json.loads((yt / "build.json").read_text())["platforms"] == [
        "linux-x86_64"
    ]
    assert (yt / "runtime/linux-x86_64/bin/ffmpeg").is_file()


def test_real_versions_toml_pins_every_platform():
    sources, artifacts = bs.load_versions(bs.REPO / "stick/versions.toml")
    assert sources["python"] == {"version": "3.12.15", "build": "20261021"}
    pairs = {(a.name, a.platform) for a in artifacts}
    assert pairs == {
        (n, p) for n in ("python", "deno", "ffmpeg_minimal") for p in bs.PLATFORMS
    } | {("bgutil-server", "any")}
    assert all(re.fullmatch(r"[0-9a-f]{64}", a.sha256) for a in artifacts)


def test_bytecode_for_app_lib_and_stdlib(tmp_path):
    yt = build(tmp_path) / ".ytop"
    assert _unchecked_hash_pyc(yt / "lib/common/sharedpkg/__pycache__/__init__.cpython-312.pyc")
    for plat in PLATS:
        assert _unchecked_hash_pyc(
            yt / f"lib/{plat}/nativepkg/__pycache__/__init__.cpython-312.pyc"
        )
        entries = _stdlib_entries(yt, plat)
        assert _unchecked_hash_pyc(None, entries["json/__init__.pyc"])
        assert entries["json/__init__.py"]  # sources stay for tracebacks


def test_bytecode_needs_cpython_312(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "version_info", (3, 13, 0, "final", 0))
    with pytest.raises(bs.BuildError, match="CPython 3.12"):
        build(tmp_path)
    assert not (tmp_path / "stick").exists()  # refused before any work


def test_build_writes_manifest(tmp_path):
    from ytop.manifest import MANIFEST_NAME, read_manifest, sha256_file

    yt = make_fake_image(tmp_path / "stick", PLATS)
    entries = read_manifest(yt / MANIFEST_NAME)
    assert entries is not None
    expected = {"boot.py", "LICENSE", "THIRD_PARTY_NOTICES.md"} | {
        p.relative_to(yt).as_posix()
        for top in ("app", "lib", "runtime")
        for p in (yt / top).rglob("*")
        if p.is_file()
    }
    assert set(entries) == expected
    for rel, digest in entries.items():
        assert digest == sha256_file(yt / rel)
    assert "build.json" not in entries
    assert MANIFEST_NAME not in entries


def test_builds_are_reproducible_across_folders(tmp_path):
    from ytop.manifest import MANIFEST_NAME, read_manifest

    one = make_fake_image(tmp_path / "one", PLATS)
    two = make_fake_image(tmp_path / "deeper" / "two", PLATS)
    first = read_manifest(one / MANIFEST_NAME)
    assert first is not None and first == read_manifest(two / MANIFEST_NAME)
    pycs = [p.relative_to(one) for p in one.rglob("*.pyc")]
    assert pycs
    for rel in pycs:
        assert (one / rel).read_bytes() == (two / rel).read_bytes(), rel


def test_build_json_names_its_manifest(tmp_path):
    """build.json records the manifest's own sha256, so a manifest left
    behind by an older YTOP's upgrade (or edited by hand) is never trusted."""
    from ytop.manifest import MANIFEST_NAME, sha256_file

    yt = make_fake_image(tmp_path / "stick", PLATS)
    build_json = json.loads((yt / "build.json").read_text())
    assert build_json["manifest_sha256"] == sha256_file(yt / MANIFEST_NAME)


def test_build_seeds_the_store(tmp_path):
    from stick_fakes import FAKE_COMPONENTS
    from ytop import components as comp
    from ytop.manifest import MANIFEST_NAME, read_manifest

    yt = make_fake_image(tmp_path / "stick", PLATS)
    current = comp.read_current(yt)
    # No Deno in the store (ruling R-B4: the spec's size budget): the
    # runtime's bin/deno is the one used until the updater adds a newer one.
    assert current.current == {
        name: version for name, (_d, version, _f) in FAKE_COMPONENTS.items()
    } | {"bgutil-server": "2.0.2"}
    assert current.previous == {} and current.pinned_bad == {}
    for name, (dist, version, files) in FAKE_COMPONENTS.items():
        folder = yt / "components" / name / version
        info = f"{dist.replace('-', '_')}-{version}.dist-info"
        for rel in [*files, f"{info}/METADATA", f"{info}/RECORD"]:
            assert (folder / rel).read_bytes() == (yt / "lib/common" / rel).read_bytes()
        # Only importable parts: no console scripts or data files.
        assert not (folder / "share").exists() and not (folder / "bin").exists()
        # The always-present fallback stays in the image.
        assert (yt / "lib/common" / files[0]).is_file()
        py = [f for f in files if f.endswith(".py")][0]
        pyc = folder / py.rsplit("/", 1)[0] / "__pycache__" / (
            py.rsplit("/", 1)[1][:-3] + ".cpython-312.pyc"
        )
        assert _unchecked_hash_pyc(pyc)
    assert comp.python_paths(yt) == [
        yt / "components" / name / FAKE_COMPONENTS[name][1]
        for name in comp.PYTHON_COMPONENTS
    ]
    assert not (yt / "components/deno").exists()
    for plat in PLATS:
        exe = "deno.exe" if plat.startswith("windows") else "deno"
        assert comp.deno_for(yt, plat) is None
        assert (yt / "runtime" / plat / "bin" / exe).is_file()  # the one used
    # Not image-owned: never in the manifest.
    entries = read_manifest(yt / MANIFEST_NAME)
    assert entries is not None
    assert not [rel for rel in entries if rel.startswith("components/")]
    # Rebuilt from scratch: a stray from an earlier build is gone.
    (yt / "components/stray.txt").write_text("x")
    make_fake_image(tmp_path / "stick", PLATS)
    assert not (yt / "components/stray.txt").exists()


def test_build_refuses_an_install_without_a_component(tmp_path):
    def partial(plat, target):
        fake_install_deps(plat, target)
        import shutil

        shutil.rmtree(target / "yt_dlp_ejs")
        for info in target.glob("yt_dlp_ejs-*.dist-info"):
            shutil.rmtree(info)

    with pytest.raises(bs.BuildError, match="yt-dlp-ejs"):
        build(tmp_path, install=partial)


def _server_folder(yt):
    return yt / "components/bgutil-server/2.0.2/server"


def test_build_seeds_bgutil_server(tmp_path):
    from ytop import components as comp

    yt = make_fake_image(tmp_path / "stick", PLATS)
    server = _server_folder(yt)
    assert (server / "src/generate_once.ts").read_text() == "// script\n"
    assert (server / "node_modules/axios/index.js").is_file()
    # Declarations, source maps and docs are pruned; code and licences stay.
    axios = server / "node_modules/axios"
    assert sorted(p.name for p in axios.iterdir()) == ["History.js", "LICENSE", "index.js"]
    # The WebAssembly side module is not a native binary.
    assert (server / "node_modules/quickjs-wasi/base64.so").is_file()
    # Only what the script needs, and the licence travels with it.
    assert not (server / "Dockerfile").exists()
    assert not (server.parent / "plugin").exists()
    assert (server.parent / "LICENSE").read_text() == "GPL text\n"
    # No symlinks anywhere (FAT), including the links install left.
    assert not [p for p in server.rglob("*") if p.is_symlink()]
    assert not (server / "node_modules/.bin").exists()
    # The server is built without the dev tools and the optional native
    # canvas; Deno must not run install scripts.
    pkg = json.loads((server / "package.json").read_text())
    assert pkg["dependencies"] == {"axios": "^1", "jsdom": "^29"}
    assert "devDependencies" not in pkg and "allowScripts" not in pkg
    assert json.loads((server / "deno.json").read_text()) == {"nodeModulesDir": "manual"}
    current = comp.read_current(yt)
    assert current.current["bgutil-server"] == "2.0.2"
    assert current.current["bgutil-plugin"] == "2.0.2"
    assert comp.server_home(yt) == server
    from ytop.manifest import MANIFEST_NAME, read_manifest
    entries = read_manifest(yt / MANIFEST_NAME)
    assert entries is not None
    assert not [rel for rel in entries if rel.startswith("components/")]


@pytest.mark.parametrize(
    "name, data",
    [
        ("addon/build/x.node", b"anything"),
        ("lib/libx.so.1", b"\x7fELF\x02\x01\x01"),
        ("lib/libx.dylib", b"\xcf\xfa\xed\xfe\x07"),
        ("bin/tool.exe", b"MZ\x90\x00"),
        ("lib/x.dll", b"MZ\x90\x00"),
        ("bin/tool", b"MZ\x90\x00\x03"),  # PE without an extension
        ("bin/fat", b"\xca\xfe\xba\xbe\x00\x00\x00\x02"),  # universal Mach-O
    ],
)
def test_build_rejects_native_node_modules(tmp_path, name, data):
    def native(deno, server):
        fake_server_install(deno, server)
        path = server / "node_modules" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    with pytest.raises(bs.BuildError, match=re.escape(name)):
        build(tmp_path, server_install=native)


def test_build_refuses_a_server_that_does_not_match_the_plugin(tmp_path):
    versions = make_fake_sources(tmp_path / "src", PLATS)
    text = versions.read_text().replace("2.0.2.tar.gz", "2.0.1.tar.gz")
    versions.write_text(text)
    (tmp_path / "src/2.0.2.tar.gz").rename(tmp_path / "src/2.0.1.tar.gz")
    with pytest.raises(bs.BuildError, match="2.0.2"):
        bs.build(
            platforms=PLATS, out=tmp_path / "stick", cache=tmp_path / "cache",
            versions_path=versions, offline=False, install=fake_install_deps,
            server_install=fake_server_install,
        )


def test_build_refuses_a_server_tarball_with_a_symlink(tmp_path):
    versions = make_fake_sources(tmp_path / "src", PLATS)
    tarball = tmp_path / "src/2.0.2.tar.gz"
    files = dict(FAKE_SERVER_FILES)
    _tar(tarball, files, {"bgutil-ytdlp-pot-provider-2.0.2/server/src/link.ts": "generate_once.ts"})
    text = versions.read_text()
    text = re.sub(
        r'(2\.0\.2\.tar\.gz"\nsha256 = ")[0-9a-f]{64}', rf"\g<1>{_sha(tarball)}", text
    )
    versions.write_text(text)
    with pytest.raises(bs.BuildError, match="symlink"):
        bs.build(
            platforms=PLATS, out=tmp_path / "stick", cache=tmp_path / "cache",
            versions_path=versions, offline=False, install=fake_install_deps,
            server_install=fake_server_install,
        )


def test_a_java_class_file_is_not_native(tmp_path):
    (tmp_path / "A.class").write_bytes(b"\xca\xfe\xba\xbe\x00\x00\x00\x34rest")
    (tmp_path / "t.txt").write_bytes(b"plain text")
    assert bs.find_native(tmp_path) == []


def test_github_asset_without_a_token_is_fetched_as_a_plain_download(
    monkeypatch, tmp_path
):
    # A public release needs no token: with none available, the build falls
    # back to the plain asset URL instead of refusing.
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(bs, "_gh_cli_token", lambda: "")
    seen = []

    class _Resp:
        def __init__(self):
            self._chunks = [b"data", b""]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            return self._chunks.pop(0)

    class _Opener:
        def open(self, req, timeout=None):
            seen.append(req)
            return _Resp()

    monkeypatch.setattr(bs.urllib.request, "build_opener", lambda *a: _Opener())
    url = "https://github.com/o/r/releases/download/t/a.tar.gz"
    art = bs.Artifact(name="x", platform="p", url=url, sha256="0" * 64, auth="github")
    dest = tmp_path / "a.tar.gz"
    bs._download(art, dest)
    assert [r.full_url for r in seen] == [url]
    assert "Authorization" not in seen[0].headers
    assert dest.read_bytes() == b"data"


def test_prune_node_modules_matches_suffixes_only(tmp_path):
    from ytop.bgserver import prune_node_modules

    keep = ["a/map.js", "a/History.js", "a/types.ts", "a/LICENSE", "a/data.json",
            "a/README", "a/markdown.js"]
    drop = ["a/index.d.ts", "a/x.d.mts", "a/x.d.cts", "a/index.js.map",
            "a/README.md", "a/CHANGELOG.MD", "a/notes.markdown"]
    for rel in keep + drop:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x")
    assert prune_node_modules(tmp_path) == len(drop)
    left = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file())
    assert left == sorted(keep)


def test_stdlib_is_one_reproducible_zip(tmp_path):
    first = build(tmp_path / "a") / ".ytop"
    second = build(tmp_path / "b") / ".ytop"
    for plat in PLATS:
        python = first / f"runtime/{plat}/python"
        std = python / stdlib_dir(plat)
        # Only the loose parts are left as files.
        left = {p.relative_to(std).parts[0] for p in std.rglob("*") if p.is_file()}
        assert left <= set(bs.STDLIB_LOOSE), left
        zip_a = bs.stdlib_zip(python, plat)
        zip_b = bs.stdlib_zip(second / f"runtime/{plat}/python", plat)
        assert zip_a.read_bytes() == zip_b.read_bytes()  # upgrades skip it
    assert bs.stdlib_zip(Path("p"), "windows-x86_64") == Path("p/python312.zip")
    assert bs.stdlib_zip(Path("p"), "linux-x86_64") == Path("p/lib/python312.zip")
