"""The versioned component store (``.ytop/components``)."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from stick_fakes import FAKE_COMPONENTS
from ytop import components as comp
from ytop import toolchain

PLATS = ["linux-x86_64", "windows-x86_64"]


def deno_name(plat: str) -> str:
    return "deno.exe" if plat.startswith("windows") else "deno"


def add_component(
    ytop: Path,
    name: str,
    version: str,
    *,
    platforms: list[str] | None = None,
    make_current: bool = True,
) -> Path:
    """A version folder of ``name`` (Deno: one binary per platform), made
    current unless told otherwise."""
    folder = ytop / "components" / name / version
    if name == "deno":
        for plat in platforms or PLATS:
            exe = folder / plat / deno_name(plat)
            exe.parent.mkdir(parents=True, exist_ok=True)
            exe.write_bytes(f"deno {version} {plat}".encode())
    else:
        package = name.replace("-", "_")
        (folder / package).mkdir(parents=True, exist_ok=True)
        (folder / package / "__init__.py").write_text(f"VERSION = {version!r}\n")
        (folder / comp.COMPLETE).write_text("")
    if make_current:
        current = comp.read_current(ytop)
        current.current[name] = version
        comp.write_current(ytop, current)
    return folder


# --- current.json ------------------------------------------------------------


def test_current_json_round_trip(tmp_path: Path):
    ytop = tmp_path / ".ytop"
    current = comp.Current(
        current={"yt-dlp": "2026.10.26"},
        previous={"yt-dlp": "2026.09.01"},
        pinned_bad={"yt-dlp": ["2026.10.01"]},
        last_check="2026-11-14T10:00:00+00:00",
        last_result="ok",
    )
    comp.write_current(ytop, current)
    doc = json.loads((ytop / "components/current.json").read_text())
    assert doc == {
        "schema": 1,
        "current": {"yt-dlp": "2026.10.26"},
        "previous": {"yt-dlp": "2026.09.01"},
        "pinned_bad": {"yt-dlp": ["2026.10.01"]},
        "last_check": "2026-11-14T10:00:00+00:00",
        "last_result": "ok",
    }
    assert comp.read_current(ytop) == current


@pytest.mark.parametrize(
    "content", [None, b"{not json", b"[]", b"\xff\xfe", b'{"current": 5}'],
)
def test_read_current_missing_or_garbled_is_empty(tmp_path: Path, content):
    ytop = tmp_path / ".ytop"
    if content is not None:
        (ytop / "components").mkdir(parents=True)
        (ytop / "components/current.json").write_bytes(content)
    assert comp.read_current(ytop) == comp.Current()


def test_read_current_drops_bad_entries(tmp_path: Path):
    ytop = tmp_path / ".ytop"
    (ytop / "components").mkdir(parents=True)
    (ytop / "components/current.json").write_text(json.dumps({
        "schema": 1,
        "current": {"yt-dlp": "2026.10.26", "deno": 7, "../x": "1"},
        "pinned_bad": {"yt-dlp": ["1", 2]},
    }))
    current = comp.read_current(ytop)
    assert current.current == {"yt-dlp": "2026.10.26"}
    assert current.pinned_bad == {"yt-dlp": ["1"]}


def test_current_json_write_is_atomic(tmp_path: Path, monkeypatch):
    ytop = tmp_path / ".ytop"
    comp.write_current(ytop, comp.Current(current={"yt-dlp": "1.0"}))
    before = (ytop / "components/current.json").read_bytes()

    def crash(src, dst):
        raise OSError("power cut between the temp write and the replace")

    monkeypatch.setattr(comp.os, "replace", crash)
    with pytest.raises(OSError):
        comp.write_current(ytop, comp.Current(current={"yt-dlp": "2.0"}))
    assert (ytop / "components/current.json").read_bytes() == before
    assert comp.read_current(ytop).current == {"yt-dlp": "1.0"}


@pytest.mark.parametrize(
    "version, folder",
    [
        ("2026.10.26", "2026.10.26"),
        ("1.2.5+local", "1.2.5+local"),
        ("2.0 beta/1", "2.0_beta_1"),
        ('a:b*c?"<>|\\', "a_b_c______"),
        ("..", None),
        (".", None),
        ("", None),
        ("CON", "_CON"),
        ("1.0.", "1.0_"),
    ],
)
def test_version_folder_names_are_fat_safe(version, folder):
    assert comp.version_dir(version) == folder


# --- lookups -----------------------------------------------------------------


def test_python_paths_in_fixed_order(tmp_path: Path):
    ytop = tmp_path / ".ytop"
    for name in reversed(comp.PYTHON_COMPONENTS):
        add_component(ytop, name, "1.0")
    assert comp.python_paths(ytop) == [
        ytop / "components" / name / "1.0" for name in comp.PYTHON_COMPONENTS
    ]
    assert comp.PYTHON_COMPONENTS == (
        "yt-dlp", "yt-dlp-ejs", "bgutil-plugin", "youtube-transcript-api",
    )


def test_python_paths_skip_a_missing_version_folder(tmp_path: Path):
    ytop = tmp_path / ".ytop"
    add_component(ytop, "yt-dlp", "1.0")
    add_component(ytop, "yt-dlp-ejs", "2.0", make_current=False)
    current = comp.read_current(ytop)
    current.current["yt-dlp-ejs"] = "9.9"  # named, never arrived
    comp.write_current(ytop, current)
    assert comp.python_paths(ytop) == [ytop / "components/yt-dlp/1.0"]


def test_deno_for_prefers_the_store(tmp_path: Path, monkeypatch):
    ytop = tmp_path / "stick" / ".ytop"
    assert comp.deno_for(ytop, "linux-x86_64") is None
    add_component(ytop, "deno", "2.10.0")
    for plat in PLATS:
        assert comp.deno_for(ytop, plat) == (
            ytop / "components/deno/2.10.0" / plat / deno_name(plat)
        )
    assert comp.deno_for(ytop, "macos-arm64") is None  # not on this stick

    # toolchain.deno_path(): the store first, then the runtime's bin/deno.
    plat = toolchain.current_platform()
    runtime = ytop / "runtime" / plat
    (runtime / "bin").mkdir(parents=True)
    bundled = runtime / "bin" / ("deno.exe" if sys.platform == "win32" else "deno")
    bundled.write_bytes(b"bundled deno")
    bundled.chmod(0o755)
    monkeypatch.setenv("YTOP_HOME", str(tmp_path / "stick"))
    monkeypatch.setenv("YTOP_RUNTIME", str(runtime))
    assert toolchain.deno_path() == bundled  # the store has no deno for plat yet
    add_component(ytop, "deno", "2.10.0", platforms=[plat])
    store = comp.deno_for(ytop, plat)
    assert store is not None
    store.chmod(0o755)
    assert toolchain.deno_path() == store
    # A garbled current.json: the runtime's own Deno.
    (ytop / "components/current.json").write_text("{oops")
    assert toolchain.deno_path() == bundled


@pytest.mark.skipif(sys.platform == "win32", reason="exec bits are POSIX")
def test_linux_deno_on_a_noexec_stick_runs_from_the_cache(
    tmp_path: Path, monkeypatch
):
    """FAT sticks are mounted ``showexec`` on Linux: nothing on them can be
    run.  A store Deno newer than the cache's is copied into the runtime
    cache (made executable) once; one the same as the cache's is not."""
    plat = "linux-x86_64"
    stick = tmp_path / "stick"
    ytop = stick / ".ytop"
    cache = tmp_path / "cache" / "b123"  # the launcher's --runtime-root
    runtime = cache / "runtime" / plat
    (runtime / "bin").mkdir(parents=True)
    (runtime / "bin/deno").write_bytes(b"deno 2.9.8 cache")
    (runtime / "bin/deno").chmod(0o755)
    (cache / "build.json").write_text(json.dumps(
        {"build_id": "b123", "sources": {"deno": {"version": "2.9.8"}}}
    ))
    monkeypatch.setattr(comp, "_executable", lambda path: False)  # showexec

    # The store holds the cache's own version: the cache's binary is used.
    add_component(ytop, "deno", "2.9.8", platforms=[plat])
    assert comp.executable_deno(ytop, plat, runtime) == runtime / "bin/deno"

    # A newer store Deno: copied into the cache, executable, and reused.
    add_component(ytop, "deno", "2.10.0", platforms=[plat])
    got = comp.executable_deno(ytop, plat, runtime)
    assert got == cache / "components/deno/2.10.0" / plat / "deno"
    assert got.read_bytes() == b"deno 2.10.0 linux-x86_64"
    assert got.stat().st_mode & stat.S_IXUSR
    stamp = got.stat().st_mtime_ns
    assert comp.executable_deno(ytop, plat, runtime) == got
    assert got.stat().st_mtime_ns == stamp  # not copied again

    # The copy cannot be made (a full disk): the cache's own Deno.
    os.unlink(got)

    def full(*_args, **_kwargs):
        raise OSError("no space")

    monkeypatch.setattr(comp, "_copy_hashed", full)
    assert comp.executable_deno(ytop, plat, runtime) == runtime / "bin/deno"
    assert not [p for p in cache.rglob("*") if comp.TEMP_SUFFIX in p.name]


@pytest.mark.skipif(sys.platform == "win32", reason="exec bits are POSIX")
def test_executable_store_deno_is_used_in_place(tmp_path: Path):
    """Mac/Windows run from the stick, where the store Deno can run."""
    plat = "macos-arm64"
    ytop = tmp_path / ".ytop"
    runtime = ytop / "runtime" / plat
    (runtime / "bin").mkdir(parents=True)
    add_component(ytop, "deno", "2.10.0", platforms=[plat])
    store = comp.deno_for(ytop, plat)
    assert store is not None
    store.chmod(0o755)
    assert comp.executable_deno(ytop, plat, runtime) == store


def test_no_store_deno_means_the_runtime_one(tmp_path: Path):
    plat = "linux-x86_64"
    ytop = tmp_path / ".ytop"
    runtime = ytop / "runtime" / plat
    (runtime / "bin").mkdir(parents=True)
    (runtime / "bin/deno").write_bytes(b"x")
    assert comp.executable_deno(ytop, plat, runtime) == runtime / "bin/deno"
    (runtime / "bin/deno").unlink()
    assert comp.executable_deno(ytop, plat, runtime) is None


# --- merge -------------------------------------------------------------------


def test_merge_takes_the_newer_version_of_each(tmp_path: Path):
    source, target = tmp_path / "src/.ytop", tmp_path / "dst/.ytop"
    add_component(source, "yt-dlp", "2026.11.01")
    add_component(source, "yt-dlp-ejs", "0.7.0")
    add_component(target, "yt-dlp", "2026.10.26")
    add_component(target, "yt-dlp-ejs", "0.8.0")
    add_component(target, "youtube-transcript-api", "1.2.5")
    labels: list[str] = []
    comp.merge_newest(source, target, PLATS, labels.append)
    current = comp.read_current(target)
    assert current.current == {
        "yt-dlp": "2026.11.01",
        "yt-dlp-ejs": "0.8.0",
        "youtube-transcript-api": "1.2.5",
    }
    # The replaced Python component stays for rollback.
    assert current.previous == {"yt-dlp": "2026.10.26"}
    assert (target / "components/yt-dlp/2026.11.01/yt_dlp/__init__.py").is_file()
    assert (target / "components/yt-dlp/2026.10.26").is_dir()
    assert labels and all("yt-dlp" in label for label in labels)
    # Nothing is left half-copied.
    assert not [p for p in target.rglob("*") if comp.TEMP_SUFFIX in p.name]


def test_merge_compares_versions_numerically(tmp_path: Path):
    source, target = tmp_path / "src/.ytop", tmp_path / "dst/.ytop"
    add_component(source, "yt-dlp-ejs", "0.10.0")
    add_component(target, "yt-dlp-ejs", "0.9.0")
    comp.merge_newest(source, target, PLATS, None)
    assert comp.read_current(target).current["yt-dlp-ejs"] == "0.10.0"


def test_merge_into_an_empty_target(tmp_path: Path):
    source, target = tmp_path / "src/.ytop", tmp_path / "dst/.ytop"
    target.mkdir(parents=True)
    for name in comp.PYTHON_COMPONENTS:
        add_component(source, name, "1.0")
    add_component(source, "deno", "2.9.8")
    comp.merge_newest(source, target, PLATS, None)
    assert comp.read_current(target).current == comp.read_current(source).current
    assert comp.python_paths(target) == [
        target / "components" / name / "1.0" for name in comp.PYTHON_COMPONENTS
    ]
    for plat in PLATS:
        assert comp.deno_for(target, plat) is not None


def test_merge_skips_a_source_folder_that_is_missing_or_pinned_bad(tmp_path: Path):
    source, target = tmp_path / "src/.ytop", tmp_path / "dst/.ytop"
    add_component(source, "yt-dlp", "9.0", make_current=False)
    add_component(source, "yt-dlp-ejs", "9.0")
    current = comp.read_current(source)
    current.current["yt-dlp"] = "9.0"
    current.current["youtube-transcript-api"] = "9.0"  # no folder
    comp.write_current(source, current)
    add_component(target, "yt-dlp", "1.0")
    add_component(target, "yt-dlp-ejs", "1.0")
    add_component(target, "youtube-transcript-api", "1.0")
    current = comp.read_current(target)
    current.pinned_bad = {"yt-dlp-ejs": ["9.0"]}
    comp.write_current(target, current)
    comp.merge_newest(source, target, PLATS, None)
    assert comp.read_current(target).current == {
        "yt-dlp": "9.0", "yt-dlp-ejs": "1.0", "youtube-transcript-api": "1.0",
    }


def test_merge_carries_a_store_deno_for_every_platform(tmp_path: Path):
    """Built sticks start with no store Deno (ruling R-B4); one the updater
    put on the source is carried for every platform the target has."""
    source, target = tmp_path / "src/.ytop", tmp_path / "dst/.ytop"
    add_component(source, "deno", "2.10.0", platforms=[*PLATS, "macos-arm64"])
    add_component(target, "yt-dlp", "1.0")  # a store, but no Deno in it
    assert all(comp.deno_for(target, plat) is None for plat in PLATS)
    comp.merge_newest(source, target, PLATS, None)
    assert comp.read_current(target).current["deno"] == "2.10.0"
    for plat in PLATS:
        exe = comp.deno_for(target, plat)
        assert exe == target / "components/deno/2.10.0" / plat / deno_name(plat)
        assert exe.read_bytes() == f"deno 2.10.0 {plat}".encode()
    assert not (target / "components/deno/2.10.0/macos-arm64").exists()


def test_merge_keeps_deno_for_every_platform(tmp_path: Path):
    """A newer Deno that the source has for only some of the target's
    platforms is not taken: every platform keeps a store Deno."""
    source, target = tmp_path / "src/.ytop", tmp_path / "dst/.ytop"
    add_component(source, "deno", "2.10.0", platforms=["linux-x86_64"])
    add_component(target, "deno", "2.9.8", platforms=PLATS)
    comp.merge_newest(source, target, PLATS, None)
    assert comp.read_current(target).current["deno"] == "2.9.8"
    for plat in PLATS:
        assert comp.deno_for(target, plat) is not None

    # Given for every platform, it is taken for all of them, and the old
    # version goes (Deno keeps no rollback copy).
    add_component(source, "deno", "2.10.0", platforms=PLATS)
    comp.merge_newest(source, target, PLATS, None)
    current = comp.read_current(target)
    assert current.current["deno"] == "2.10.0"
    assert "deno" not in current.previous
    for plat in PLATS:
        exe = comp.deno_for(target, plat)
        assert exe == target / "components/deno/2.10.0" / plat / deno_name(plat)
        assert exe.read_bytes() == f"deno 2.10.0 {plat}".encode()
    assert not (target / "components/deno/2.9.8").exists()


def test_merge_fills_in_a_missing_platform_of_the_same_deno(tmp_path: Path):
    source, target = tmp_path / "src/.ytop", tmp_path / "dst/.ytop"
    add_component(source, "deno", "2.9.8", platforms=PLATS)
    add_component(target, "deno", "2.9.8", platforms=["linux-x86_64"])
    comp.merge_newest(source, target, PLATS, None)
    for plat in PLATS:
        assert comp.deno_for(target, plat) is not None


def test_merge_only_copies_the_targets_platforms(tmp_path: Path):
    source, target = tmp_path / "src/.ytop", tmp_path / "dst/.ytop"
    add_component(source, "deno", "2.9.8", platforms=[*PLATS, "macos-arm64"])
    target.mkdir(parents=True)
    comp.merge_newest(source, target, ["linux-x86_64"], None)
    assert sorted(p.name for p in (target / "components/deno/2.9.8").iterdir()) == [
        "linux-x86_64"
    ]


def test_merge_size_counts_what_would_be_copied(tmp_path: Path):
    source, target = tmp_path / "src/.ytop", tmp_path / "dst/.ytop"
    add_component(source, "yt-dlp", "2.0")
    add_component(target, "yt-dlp", "1.0")
    size = comp.merge_size(source, target, PLATS)
    assert size == (source / "components/yt-dlp/2.0/yt_dlp/__init__.py").stat().st_size
    comp.merge_newest(source, target, PLATS, None)
    assert comp.merge_size(source, target, PLATS) == 0


def test_merge_without_a_source_store_changes_nothing(tmp_path: Path):
    source, target = tmp_path / "src/.ytop", tmp_path / "dst/.ytop"
    source.mkdir(parents=True)
    add_component(target, "yt-dlp", "1.0")
    before = (target / "components/current.json").read_bytes()
    comp.merge_newest(source, target, PLATS, None)
    assert (target / "components/current.json").read_bytes() == before


def test_fake_components_cover_every_python_component():
    assert set(FAKE_COMPONENTS) == set(comp.PYTHON_COMPONENTS)


# --- R-B5: partial folders, version order, pins, the Deno copy ----------------


@pytest.mark.parametrize(
    "older, newer",
    [
        ("1.0.0rc1", "1.0.0"),
        ("1.0.0a1", "1.0.0b1"),
        ("1.0.0b2", "1.0.0rc1"),
        ("1.0.0.dev1", "1.0.0a1"),
        ("1.0.0", "1.0.0.post1"),
        ("1.0.0", "1.0.1rc1"),
        ("0.9.0", "0.10.0"),
        ("2026.10.26", "2026.11.1"),
        ("2026.10.26", "2026.11.01"),
        ("2026.10.26", "2026.10.26.232815"),  # a nightly is newer
        ("v2.9.8", "2.10.0"),
    ],
)
def test_version_key_is_pep440_aware(older: str, newer: str):
    assert comp._version_key(older) < comp._version_key(newer)
    assert comp._version_key(newer) > comp._version_key(older)


def test_version_key_equal_spellings():
    assert comp._version_key("1.0") == comp._version_key("1.0.0")
    assert comp._version_key("2026.11.01") == comp._version_key("2026.11.1")


@pytest.mark.parametrize(
    "version, pre",
    [("1.0.0rc1", True), ("1.0a2", True), ("1.0.dev3", True), ("1.0.0", False),
     ("1.0.post1", False), ("2026.10.26.232815", False), ("garbage", False)],
)
def test_is_prerelease(version: str, pre: bool):
    assert comp.is_prerelease(version) is pre


def test_interrupted_prune_never_leaves_a_partial_folder_under_its_name(
    tmp_path: Path, monkeypatch
):
    ytop = tmp_path / ".ytop"
    add_component(ytop, "yt-dlp", "1.0")
    add_component(ytop, "yt-dlp", "2.0")
    add_component(ytop, "yt-dlp", "3.0")
    real_rmtree = comp.shutil.rmtree
    seen: list[str] = []

    def cut_off(path, *args, **kwargs):
        seen.append(Path(path).name)
        # The stick is pulled half way through: one file goes, then nothing.
        next(Path(path).rglob("*.py")).unlink()

    monkeypatch.setattr(comp.shutil, "rmtree", cut_off)
    comp._prune(ytop, comp.read_current(ytop), ["yt-dlp"])
    monkeypatch.setattr(comp.shutil, "rmtree", real_rmtree)
    # Deleted under a temp name: never a broken folder under a real one.
    old = ".old" + comp.TEMP_SUFFIX
    assert seen == ["1.0" + old, "2.0" + old]
    names = sorted(p.name for p in (ytop / "components/yt-dlp").iterdir())
    assert names == ["1.0" + old, "2.0" + old, "3.0"]
    comp._clear_temps(ytop)
    assert sorted(p.name for p in (ytop / "components/yt-dlp").iterdir()) == ["3.0"]


def test_a_component_folder_without_its_marker_is_not_used(tmp_path: Path):
    ytop = tmp_path / ".ytop"
    folder = add_component(ytop, "yt-dlp", "1.0")
    assert comp.python_paths(ytop) == [folder]
    (folder / comp.COMPLETE).unlink()
    assert comp.python_paths(ytop) == []  # lib/common's copy instead


def test_merge_replaces_a_partial_folder_of_the_chosen_version(tmp_path: Path):
    source, target = tmp_path / "src/.ytop", tmp_path / "dst/.ytop"
    add_component(source, "yt-dlp", "2.0")
    add_component(target, "yt-dlp", "1.0")
    partial = add_component(target, "yt-dlp", "2.0", make_current=False)
    (partial / "yt_dlp/__init__.py").unlink()
    (partial / comp.COMPLETE).unlink()
    comp.merge_newest(source, target, PLATS, None)
    assert comp.read_current(target).current["yt-dlp"] == "2.0"
    assert (target / "components/yt-dlp/2.0/yt_dlp/__init__.py").is_file()
    assert comp.python_paths(target) == [target / "components/yt-dlp/2.0"]
    assert not [p for p in target.rglob("*") if comp.TEMP_SUFFIX in p.name]


def test_merge_never_makes_a_pinned_bad_version_current(tmp_path: Path):
    """Pins are per stick and merge as a union; a version either side
    pinned is never taken, and a current one the other side pinned gives
    way to a good one."""
    source, target = tmp_path / "src/.ytop", tmp_path / "dst/.ytop"
    add_component(source, "yt-dlp", "1.0")
    add_component(source, "yt-dlp-ejs", "0.9.0")
    current = comp.read_current(source)
    current.pinned_bad = {"yt-dlp": ["2.0"]}
    comp.write_current(source, current)
    add_component(target, "yt-dlp", "2.0")
    add_component(target, "yt-dlp-ejs", "0.8.0")
    current = comp.read_current(target)
    current.pinned_bad = {"yt-dlp-ejs": ["0.9.0"]}
    comp.write_current(target, current)
    comp.merge_newest(source, target, PLATS, None)
    merged = comp.read_current(target)
    assert merged.current == {"yt-dlp": "1.0", "yt-dlp-ejs": "0.8.0"}
    assert merged.pinned_bad == {"yt-dlp": ["2.0"], "yt-dlp-ejs": ["0.9.0"]}
    assert "yt-dlp" not in merged.previous  # never roll back onto a pinned one


@pytest.mark.skipif(sys.platform == "win32", reason="exec bits are POSIX")
def test_cached_deno_copy_is_checked_against_the_store(tmp_path: Path, monkeypatch):
    plat = "linux-x86_64"
    ytop = tmp_path / "stick/.ytop"
    cache = tmp_path / "cache/b1"
    runtime = cache / "runtime" / plat
    (runtime / "bin").mkdir(parents=True)
    (runtime / "bin/deno").write_bytes(b"deno 2.9.8 cache")
    (runtime / "bin/deno").chmod(0o755)
    monkeypatch.setattr(comp, "_executable", lambda path: False)
    add_component(ytop, "deno", "2.10.0", platforms=[plat])
    got = comp.executable_deno(ytop, plat, runtime)
    assert got is not None and got.read_bytes() == b"deno 2.10.0 linux-x86_64"
    # The store's binary changes (same size): the copy is made again.
    store = comp.deno_for(ytop, plat)
    assert store is not None
    store.write_bytes(b"DENO 2.10.0 LINUX-X86_64")
    os.utime(store, ns=(1, 1))
    got = comp.executable_deno(ytop, plat, runtime)
    assert got is not None and got.read_bytes() == b"DENO 2.10.0 LINUX-X86_64"
    # A copy whose content was damaged (same size, stamp gone): made again.
    got.write_bytes(b"xxxx 2.10.0 linux-x86_64")
    for stamp in got.parent.glob("*.stamp"):
        stamp.unlink()
    got = comp.executable_deno(ytop, plat, runtime)
    assert got is not None and got.read_bytes() == b"DENO 2.10.0 LINUX-X86_64"


@pytest.mark.skipif(sys.platform == "win32", reason="exec bits are POSIX")
def test_cached_deno_copy_uses_a_per_process_temp_name(tmp_path: Path, monkeypatch):
    plat = "linux-x86_64"
    ytop = tmp_path / "stick/.ytop"
    runtime = tmp_path / "cache/b1/runtime" / plat
    (runtime / "bin").mkdir(parents=True)
    monkeypatch.setattr(comp, "_executable", lambda path: False)
    add_component(ytop, "deno", "2.10.0", platforms=[plat])
    temps: list[str] = []
    real_replace = comp.os.replace

    def spy(src, dst):
        temps.append(Path(src).name)
        return real_replace(src, dst)

    monkeypatch.setattr(comp.os, "replace", spy)
    assert comp.executable_deno(ytop, plat, runtime) is not None
    assert temps and all(str(os.getpid()) in name for name in temps)


def test_merge_compares_pins_by_version_not_spelling(tmp_path: Path):
    source, target = tmp_path / "src/.ytop", tmp_path / "dst/.ytop"
    add_component(source, "yt-dlp", "2026.11.1")
    add_component(target, "yt-dlp", "2026.10.26")
    current = comp.read_current(target)
    current.pinned_bad = {"yt-dlp": ["2026.11.01"]}
    comp.write_current(target, current)
    comp.merge_newest(source, target, PLATS, None)
    assert comp.read_current(target).current["yt-dlp"] == "2026.10.26"
    # A current version pinned under another spelling gives way.
    source2, target2 = tmp_path / "s2/.ytop", tmp_path / "d2/.ytop"
    add_component(source2, "yt-dlp", "1.0")
    add_component(target2, "yt-dlp", "2.0")
    current = comp.read_current(target2)
    current.pinned_bad = {"yt-dlp": ["2.0.0"]}
    comp.write_current(target2, current)
    comp.merge_newest(source2, target2, PLATS, None)
    merged = comp.read_current(target2)
    assert merged.current["yt-dlp"] == "1.0" and "yt-dlp" not in merged.previous
    # Deno too.
    source3, target3 = tmp_path / "s3/.ytop", tmp_path / "d3/.ytop"
    add_component(source3, "deno", "2.10.0")
    add_component(target3, "deno", "2.9.8")
    current = comp.read_current(target3)
    current.pinned_bad = {"deno": ["2.10"]}
    comp.write_current(target3, current)
    comp.merge_newest(source3, target3, PLATS, None)
    assert comp.read_current(target3).current["deno"] == "2.9.8"
    assert comp.is_pinned("2026.11.1", ["2026.11.01"]) and not comp.is_pinned("1.0", ["1.1"])


def test_in_use_marker_liveness_compares_start_ticks(monkeypatch):
    """A wall-clock step must not make a running app's marker look dead
    (its folders would be pruned under it); a recycled pid is dead (#9)."""
    import ytop.startup as startup

    monkeypatch.setattr(comp, "pid_alive", lambda pid: True)
    monkeypatch.setattr(startup, "process_start_time", lambda pid=None: 4600.0)
    monkeypatch.setattr(comp, "process_start_ticks", lambda pid=None: 777)
    assert comp.marker_live(4242, 1000.0, 777)  # the clock jumped an hour
    assert not comp.marker_live(4242, 4600.0, 778)  # same time, other process
    assert not comp.marker_live(4242, 1000.0)  # an old marker: the time decides
