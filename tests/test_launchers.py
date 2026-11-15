import os
import subprocess
import time
from pathlib import Path

import pytest

LAUNCHERS = Path(__file__).parent.parent / "src/ytop/stick/launchers"
PLAT = {"x86_64": "linux-x86_64", "aarch64": "linux-aarch64"}.get(os.uname().machine)

pytestmark = pytest.mark.skipif(PLAT is None, reason="unsupported host arch")


def make_stick(tmp_path: Path, build_id: str = "202611130000-abc1234") -> Path:
    stick = tmp_path / "stick"
    yt = stick / ".ytop"
    (yt / "app/ytop").mkdir(parents=True)
    (yt / "lib/common").mkdir(parents=True)
    (yt / f"lib/{PLAT}").mkdir(parents=True)
    (yt / f"runtime/{PLAT}/bin").mkdir(parents=True)
    pybin = yt / f"runtime/{PLAT}/python/bin"
    pybin.mkdir(parents=True)
    (yt / "boot.py").write_text("# boot\n")
    (yt / "build.json").write_text(
        '{\n  "build_id": "%s",\n  "platforms": []\n}\n' % build_id
    )
    (pybin / "python3").write_text(
        f'#!/bin/sh\necho "$@" > "{tmp_path}/argv.txt"\npwd > "{tmp_path}/cwd.txt"\n'
    )
    (stick / "start-ytop-linux.sh").write_text(
        (LAUNCHERS / "start-ytop-linux.sh").read_text()
    )
    return stick


def run(stick: Path, tmp_path: Path, env_extra=None, cwd=None):
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path / "home"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
    }
    env.update(env_extra or {})
    return subprocess.run(
        ["sh", str(stick / "start-ytop-linux.sh")],
        env=env,
        capture_output=True,
        text=True,
        cwd=cwd,
    )


def cache_dir(tmp_path: Path, build_id="202611130000-abc1234") -> Path:
    return tmp_path / "cache/ytop/runtime" / build_id


def test_linux_launcher_stages_and_execs(tmp_path):
    stick = make_stick(tmp_path)
    r = run(stick, tmp_path)
    assert r.returncode == 0, r.stderr + r.stdout
    cache = cache_dir(tmp_path)
    assert (cache / ".complete").exists()
    assert (cache / "boot.py").exists()
    assert os.access(cache / f"runtime/{PLAT}/python/bin/python3", os.X_OK)
    argv = (tmp_path / "argv.txt").read_text().strip()
    assert argv == (
        f"-s -E {cache}/boot.py --home {stick} --runtime-root {cache}"
    )


def test_linux_launcher_leaves_the_stick(tmp_path):
    """Started from the stick's folder, Python must not keep it busy."""
    stick = make_stick(tmp_path)
    r = run(stick, tmp_path, cwd=stick)
    assert r.returncode == 0, r.stderr + r.stdout
    cwd = Path((tmp_path / "cwd.txt").read_text().strip())
    assert cwd == Path("/")
    assert not str(cwd).startswith(str(stick))


def test_linux_launcher_reuses_cache(tmp_path):
    stick = make_stick(tmp_path)
    assert run(stick, tmp_path).returncode == 0
    marker = cache_dir(tmp_path) / ".complete"
    before = marker.stat().st_mtime_ns
    time.sleep(0.05)
    assert run(stick, tmp_path).returncode == 0
    assert marker.stat().st_mtime_ns == before


def test_linux_launcher_redoes_partial_cache(tmp_path):
    stick = make_stick(tmp_path)
    cache = cache_dir(tmp_path)
    cache.mkdir(parents=True)
    (cache / "junk").write_text("x")
    leftover = Path(str(cache) + ".partial.999")
    leftover.mkdir()
    r = run(stick, tmp_path)
    assert r.returncode == 0, r.stderr + r.stdout
    assert (cache / ".complete").exists()
    assert not (cache / "junk").exists()
    assert leftover.exists()  # fresh leftovers are left for their owner
    subprocess.run(["touch", "-d", "3 days ago", str(leftover)], check=True)
    import shutil

    shutil.rmtree(cache)
    assert run(stick, tmp_path).returncode == 0
    assert not leftover.exists()  # old leftovers are swept
    assert not Path(str(cache) + ".lock").exists()


def test_linux_launcher_unknown_arch(tmp_path):
    stick = make_stick(tmp_path)
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "uname").write_text("#!/bin/sh\necho riscv64\n")
    (fake / "uname").chmod(0o755)
    r = run(stick, tmp_path, {"PATH": f"{fake}:{os.environ['PATH']}"})
    assert r.returncode == 3
    assert "no runtime" in r.stdout


def test_linux_launcher_prunes_old_builds(tmp_path):
    stick = make_stick(tmp_path)
    root = tmp_path / "cache/ytop/runtime"
    old = root / "old-build"
    new = root / "new-build"
    old.mkdir(parents=True)
    new.mkdir()
    subprocess.run(["touch", "-d", "40 days ago", str(old)], check=True)
    assert run(stick, tmp_path).returncode == 0
    assert not old.exists()
    assert new.exists()
    assert cache_dir(tmp_path).exists()


def test_launchers_static_content():
    cmd = (LAUNCHERS / "Start YTOP (Windows).cmd").read_bytes()
    assert b"\r\n" in cmd and b"\n" not in cmd.replace(b"\r\n", b"")
    assert b'".ytop\\runtime\\windows-x86_64\\python\\python.exe" -s -E ".ytop\\boot.py"' in cmd
    assert b"title YTOP - close this window to stop" in cmd
    assert b"if errorlevel 1 pause" in cmd
    assert b'cd /d "%~dp0"' in cmd
    mac = (LAUNCHERS / "Start YTOP (Mac).command").read_bytes()
    assert mac.startswith(b"#!/bin/sh\n") and b"\r" not in mac
    sh = (LAUNCHERS / "start-ytop-linux.sh").read_bytes()
    assert b"\r" not in sh
    readme = (LAUNCHERS / "README.txt").read_bytes()
    assert b"\n" not in readme.replace(b"\r\n", b"")
    assert b"{label}" in readme and b"{build_id}" in readme


def test_linux_launcher_copy_failure_exits_4(tmp_path):
    stick = make_stick(tmp_path)
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "cp").write_text("#!/bin/sh\nexit 1\n")
    (fake / "cp").chmod(0o755)
    r = run(stick, tmp_path, {"PATH": f"{fake}:{os.environ['PATH']}"})
    assert r.returncode == 4
    assert "Couldn't copy YTOP" in r.stdout
    root = tmp_path / "cache/ytop/runtime"
    assert not (cache_dir(tmp_path) / ".complete").exists()
    assert list(root.glob("*partial*")) == []
    assert list(root.glob("*.lock")) == []


def test_linux_launcher_path_with_spaces_and_parens(tmp_path):
    odd = tmp_path / "My Stick (1)"
    odd.mkdir()
    stick = make_stick(odd)
    r = run(stick, tmp_path)
    assert r.returncode == 0, r.stderr + r.stdout
    assert f"--home {stick} " in (odd / "argv.txt").read_text()


def test_linux_launcher_missing_runtime(tmp_path):
    stick = make_stick(tmp_path)
    (stick / f".ytop/runtime/{PLAT}/python/bin/python3").unlink()
    r = run(stick, tmp_path)
    assert r.returncode == 3
    assert (
        f"This YTOP stick has no runtime for {PLAT}. "
        "Make the stick again with that platform included." in r.stdout
    )


def test_linux_launcher_breaks_stale_lock(tmp_path):
    stick = make_stick(tmp_path)
    lock = Path(str(cache_dir(tmp_path)) + ".lock")
    lock.mkdir(parents=True)
    subprocess.run(["touch", "-d", "1 hour ago", str(lock)], check=True)
    assert run(stick, tmp_path).returncode == 0
    assert (cache_dir(tmp_path) / ".complete").exists()
    assert not lock.exists()


# --- an interrupted upgrade must not leave a mixed cache ----------------------

FIXTURES = Path(__file__).parent / "fixtures"
BOOT_PY = Path(__file__).parent.parent / "src/ytop/stick/boot.py"


# tests/fixtures/start-ytop-linux.plan2.sh is the pre-manifest launcher (8cfef9b),
# byte-identical to the launcher of that day; it guards sticks already in the
# field against future boot.py/launcher changes.

@pytest.mark.parametrize(
    "launcher", [LAUNCHERS / "start-ytop-linux.sh", FIXTURES / "start-ytop-linux.plan2.sh"],
    ids=["current", "plan2"],
)
def test_linux_launcher_with_a_cache_after_interrupted_upgrade_is_not_mixed(
    tmp_path, launcher
):
    import shutil
    import sys

    stick = make_stick(tmp_path)
    (stick / "start-ytop-linux.sh").write_text(launcher.read_text())
    yt = stick / ".ytop"
    shutil.copy(BOOT_PY, yt / "boot.py")
    (yt / "build.json").write_text(
        '{"build_id": "202611130000-abc1234", "platforms": ["%s"]}' % PLAT
    )
    (yt / "app/ytop/__init__.py").write_text("")
    # A python that runs the real boot.py (the repo interpreter).
    (yt / f"runtime/{PLAT}/python/bin/python3").write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "$@"\n'
    )
    # First launch stages the cache from the stick (boot.py fails after:
    # there is no cli, that is fine here).
    run(stick, tmp_path)
    cache = cache_dir(tmp_path)
    assert (cache / ".complete").exists()
    # Then the upgrade is cut off just before the new app goes in.
    old = stick / ".ytop.old"
    old.mkdir()
    (old / "upgrade-journal.txt").write_text("")
    shutil.move(str(yt / "app"), str(old / "app"))
    r = run(stick, tmp_path)
    assert r.returncode == 5, r.stderr + r.stdout
    assert "interrupted" in r.stderr
    assert (yt / "app/ytop/__init__.py").exists()
    assert not old.exists()
    completes = list((tmp_path / "cache/ytop/runtime").glob("*/.complete"))
    assert completes == []


def _wrapper(path: Path) -> None:
    import sys

    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    path.chmod(0o755)


def _genuine_interrupted_stick(tmp_path, monkeypatch):
    from test_boot import interrupted_stick
    from test_makestick import _owned_state

    stick, before = interrupted_stick(tmp_path, monkeypatch)
    assert not (stick / ".ytop/app").exists()
    # The fake image's python is not runnable; make the stick's one so.
    py_rel = f"runtime/{PLAT}/python/bin/python3"
    _wrapper(stick / ".ytop" / py_rel)
    (stick / "start-ytop-linux.sh").write_text(
        (LAUNCHERS / "start-ytop-linux.sh").read_text()
    )
    before.pop(py_rel, None)

    def owned():
        state = _owned_state(stick)
        state.pop(py_rel, None)
        return state

    return stick, before, owned


@pytest.mark.parametrize("warm", [True, False], ids=["warm-cache", "empty-cache"])
def test_linux_launcher_repairs_a_genuinely_interrupted_stick(
    tmp_path, monkeypatch, warm
):
    if PLAT != "linux-x86_64":
        pytest.skip("fake image is built for x86_64")
    stick, before, owned = _genuine_interrupted_stick(tmp_path, monkeypatch)
    runtime = tmp_path / "cache/ytop/runtime"
    if warm:
        other = runtime / "some-other-build"
        _wrapper(other / f"runtime/{PLAT}/python/bin/python3")
        (other / ".complete").write_text("")
    r = run(stick, tmp_path)
    assert r.returncode == 5, r.stderr + r.stdout
    assert "put back" in r.stderr
    assert "disk" not in r.stdout + r.stderr
    assert owned() == before
    assert not (stick / ".ytop.old").exists()
    completes = {p.parent.name for p in runtime.glob("*/.complete")}
    assert completes == ({"some-other-build"} if warm else set())
    assert not list(runtime.glob("repair.*"))


def test_linux_launcher_without_any_python_says_what_to_do(tmp_path, monkeypatch):
    if PLAT != "linux-x86_64":
        pytest.skip("fake image is built for x86_64")
    import shutil

    stick, _before, _owned = _genuine_interrupted_stick(tmp_path, monkeypatch)
    shutil.rmtree(stick / f".ytop/runtime/{PLAT}/python")
    r = run(stick, tmp_path)
    assert r.returncode == 5
    assert (
        "Open YTOP from another stick and make or upgrade this one again." in r.stdout
    )
    assert "disk" not in r.stdout


def test_plan2_launcher_with_an_empty_cache_cannot_repair(tmp_path, monkeypatch):
    """The pre-manifest launcher stages before it runs boot.py; with no .ytop/app
    that fails (exit 4) and nothing is marked complete.  Make-stick repairs
    that stick; the current launcher repairs it itself."""
    if PLAT != "linux-x86_64":
        pytest.skip("fake image is built for x86_64")
    stick, _before, _owned = _genuine_interrupted_stick(tmp_path, monkeypatch)
    (stick / "start-ytop-linux.sh").write_text(
        (FIXTURES / "start-ytop-linux.plan2.sh").read_text()
    )
    r = run(stick, tmp_path)
    assert r.returncode == 4
    assert not list((tmp_path / "cache/ytop/runtime").glob("*/.complete"))


# --- the cache reuses the previous build ------------------------------------

def _write_manifest(stick: Path) -> None:
    import hashlib

    yt = stick / ".ytop"
    files = [yt / "boot.py"]
    for sub in ("app", "lib/common", f"lib/{PLAT}", f"runtime/{PLAT}"):
        files += [p for p in (yt / sub).rglob("*") if p.is_file()]
    lines = [
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(yt).as_posix()}\n"
        for p in sorted(files)
    ]
    (yt / "manifest.sha256").write_text("".join(lines))


def _switch_build(stick: Path, build_id: str) -> None:
    (stick / ".ytop/build.json").write_text(
        '{\n  "build_id": "%s",\n  "platforms": []\n}\n' % build_id
    )


def _cp_logger(tmp_path: Path) -> Path:
    import shutil

    real = shutil.which("cp")
    fake = tmp_path / "fakebin"
    fake.mkdir()
    log = tmp_path / "cp.log"
    (fake / "cp").write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexec "{real}" "$@"\n')
    (fake / "cp").chmod(0o755)
    return fake


def _stage_a_then_b(tmp_path: Path, stick: Path, mutate) -> subprocess.CompletedProcess:
    (stick / ".ytop/app/ytop/main.py").write_text("print('a')\n")
    _write_manifest(stick)
    assert run(stick, tmp_path).returncode == 0
    mutate(stick)
    _switch_build(stick, "202611140000-def5678")
    _write_manifest(stick)
    fake = _cp_logger(tmp_path)
    return run(stick, tmp_path, {"PATH": f"{fake}:{os.environ['PATH']}"})


def test_linux_launcher_reuses_previous_build_cache(tmp_path):
    stick = make_stick(tmp_path)
    (stick / f".ytop/runtime/{PLAT}/bin/tool").write_text("tool\n")

    def mutate(s):
        (s / ".ytop/app/ytop/main.py").write_text("print('b')\n")

    r = _stage_a_then_b(tmp_path, stick, mutate)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "Updating YTOP's copy on this computer (only what changed)..." in r.stdout
    assert "First run" not in r.stdout
    cache = cache_dir(tmp_path, "202611140000-def5678")
    assert (cache / ".complete").exists()
    assert (cache / "app/ytop/main.py").read_text() == "print('b')\n"
    assert '"202611140000-def5678"' in (cache / "build.json").read_text()
    assert (cache / f"runtime/{PLAT}/bin/tool").read_text() == "tool\n"
    log = (tmp_path / "cp.log").read_text()
    assert f"{stick}/.ytop/runtime" not in log
    assert "main.py" in log


def test_linux_launcher_falls_back_without_manifest(tmp_path):
    stick = make_stick(tmp_path)
    assert run(stick, tmp_path).returncode == 0
    (stick / ".ytop/app/ytop/main.py").write_text("print('b')\n")
    _switch_build(stick, "202611140000-def5678")
    r = run(stick, tmp_path)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "First run on this computer" in r.stdout
    cache = cache_dir(tmp_path, "202611140000-def5678")
    assert (cache / ".complete").exists()
    assert (cache / "app/ytop/main.py").read_text() == "print('b')\n"


def test_linux_launcher_reuse_removes_files_the_new_build_dropped(tmp_path):
    stick = make_stick(tmp_path)
    (stick / ".ytop/app/ytop/old.py").write_text("old\n")
    (stick / ".ytop/lib/common/dropped.txt").write_text("x\n")

    def mutate(s):
        (s / ".ytop/app/ytop/old.py").unlink()
        (s / ".ytop/lib/common/dropped.txt").unlink()
        (s / ".ytop/app/ytop/new.py").write_text("new\n")

    r = _stage_a_then_b(tmp_path, stick, mutate)
    assert r.returncode == 0, r.stderr + r.stdout
    cache = cache_dir(tmp_path, "202611140000-def5678")
    assert not (cache / "app/ytop/old.py").exists()
    assert not (cache / "lib/common/dropped.txt").exists()
    assert (cache / "app/ytop/new.py").read_text() == "new\n"
    assert (cache / "build.json").exists()
    assert (cache / ".complete").exists()


def test_linux_launcher_reuse_handles_paths_with_spaces(tmp_path):
    odd = tmp_path / "My Stick (1)"
    odd.mkdir()
    stick = make_stick(odd)
    (stick / ".ytop/app/ytop/my file.py").write_text("a\n")
    (stick / ".ytop/app/ytop/gone file.py").write_text("g\n")

    def mutate(s):
        (s / ".ytop/app/ytop/my file.py").write_text("b\n")
        (s / ".ytop/app/ytop/gone file.py").unlink()

    (stick / ".ytop/app/ytop/main.py").write_text("print('a')\n")
    _write_manifest(stick)
    assert run(stick, tmp_path).returncode == 0
    mutate(stick)
    _switch_build(stick, "202611140000-def5678")
    _write_manifest(stick)
    r = run(stick, tmp_path)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "only what changed" in r.stdout
    cache = cache_dir(tmp_path, "202611140000-def5678")
    assert (cache / "app/ytop/my file.py").read_text() == "b\n"
    assert not (cache / "app/ytop/gone file.py").exists()


def test_linux_launcher_reuse_works_in_a_non_english_locale(tmp_path):
    import shutil

    real = shutil.which("sha256sum")
    fake = _cp_logger(tmp_path)
    (fake / "sha256sum").write_text(
        f'#!/bin/sh\nif [ "$LC_ALL" = C ]; then exec "{real}" "$@"; fi\n'
        f'"{real}" "$@" | sed "s/FAILED/FEHLER/"\nexit 1\n'
    )
    (fake / "sha256sum").chmod(0o755)
    stick = make_stick(tmp_path)
    (stick / ".ytop/app/ytop/main.py").write_text("print('a')\n")
    _write_manifest(stick)
    env = {"PATH": f"{fake}:{os.environ['PATH']}", "LANG": "de_DE.UTF-8", "LC_ALL": "de_DE.UTF-8"}
    assert run(stick, tmp_path, env).returncode == 0
    (stick / ".ytop/app/ytop/main.py").write_text("print('b')\n")
    _switch_build(stick, "202611140000-def5678")
    _write_manifest(stick)
    (tmp_path / "cp.log").unlink(missing_ok=True)
    r = run(stick, tmp_path, env)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "only what changed" in r.stdout
    assert f"{stick}/.ytop/runtime" not in (tmp_path / "cp.log").read_text()


def test_linux_launcher_reuse_removes_a_dropped_package_folder(tmp_path):
    """A dropped package always holds __pycache__/: the folder must go too,
    or Python imports the empty folder as a namespace package (yt-dlp then
    believes an optional dependency is there)."""
    stick = make_stick(tmp_path)
    pkg = stick / ".ytop/lib/common/brotli"
    (pkg / "__pycache__").mkdir(parents=True)
    (pkg / "__init__.py").write_text("x\n")
    (pkg / "__pycache__/__init__.cpython-312.pyc").write_bytes(b"pyc")
    (stick / ".ytop/lib/common/kept.py").write_text("kept\n")

    def mutate(s):
        import shutil

        shutil.rmtree(s / ".ytop/lib/common/brotli")

    r = _stage_a_then_b(tmp_path, stick, mutate)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "only what changed" in r.stdout
    cache = cache_dir(tmp_path, "202611140000-def5678")
    assert (cache / ".complete").exists()
    assert not (cache / "lib/common/brotli").exists()
    assert (cache / "lib/common").is_dir()
