"""The component updater (``ytop.updater``), against a fake release server
on 127.0.0.1: never the network."""

from __future__ import annotations

import hashlib
import json
import marshal
import shutil
import socket
import stat
import subprocess
import sys
import tomllib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fake_releases import FakeReleases, make_wheel
from stick_fakes import code_bytes
from ytop import components as comp
from ytop import updater as up
from ytop.config import Config
from ytop.runtime import current_platform
from ytop.stick.boot import code_paths

ROOT = Path(__file__).resolve().parent.parent
PLATS = ["linux-x86_64", "macos-arm64", "windows-x86_64"]
OLD = {
    "yt-dlp": "2026.10.26",
    "yt-dlp-ejs": "0.8.0",
    "bgutil-plugin": "2.0.2",
    "youtube-transcript-api": "1.2.5",
}
PACKAGES = {
    "yt-dlp": ["yt_dlp/__init__.py", "yt_dlp/version.py"],
    "yt-dlp-ejs": ["yt_dlp_ejs/__init__.py"],
    "bgutil-plugin": ["yt_dlp_plugins/extractor/getpot_bgutil.py"],
    "youtube-transcript-api": ["youtube_transcript_api/__init__.py"],
}
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
CFG = Config()
BGUTIL = "bgutil-server"


def _install(target: Path, name: str, version: str) -> None:
    dist = up.COMPONENT_DISTS[name]
    for rel in PACKAGES[name]:
        (target / rel).parent.mkdir(parents=True, exist_ok=True)
        (target / rel).write_text(f"__version__ = {version!r}\n")
    info = target / f"{dist.replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\n"
    )
    (info / "RECORD").write_text("")


def make_stick(tmp_path: Path, platforms: list[str] = PLATS) -> Path:
    """A stick's ``.ytop``: build.json, lib/common holding the OLD
    versions, and a seeded store (the OLD versions and a bgutil server)."""
    ytop = tmp_path / "stick/.ytop"
    ytop.mkdir(parents=True)
    (ytop / "build.json").write_text(json.dumps({
        "build_id": "b1", "platforms": platforms,
        "sources": {"deno": {"version": "2.9.8"}},
    }))
    for name, version in OLD.items():
        _install(ytop / "lib/common", name, version)
        folder = ytop / "components" / name / version
        _install(folder, name, version)
        (folder / comp.COMPLETE).write_text("")
    server = ytop / "components/bgutil-server/2.0.2"
    (server / "server/src").mkdir(parents=True)
    (server / "server/src/generate_once.ts").write_text("// script 2.0.2\n")
    (server / "server/package.json").write_text(json.dumps({
        "name": "bgutil", "version": "2.0.2",
        "dependencies": {"axios": "^1", "jsdom": "^29"},
    }))
    (server / "server/node_modules/axios").mkdir(parents=True)
    (server / "server/node_modules/axios/index.js").write_text("module.exports = 1\n")
    (server / comp.COMPLETE).write_text("")
    comp.write_current(ytop, comp.Current(current={**OLD, "bgutil-server": "2.0.2"}))
    return ytop


@pytest.fixture
def fake():
    server = FakeReleases()
    yield server
    server.close()


@pytest.fixture
def ytop(tmp_path: Path) -> Path:
    return make_stick(tmp_path)


def publish(fake: FakeReleases) -> None:
    """Upstream: newer yt-dlp, ejs, transcript API and Deno; the bgutil
    plugin is unchanged."""
    fake.ytdlp_release("2026.11.1")
    fake.pypi_release("yt-dlp-ejs", "0.8.0")
    fake.pypi_release("yt-dlp-ejs", "0.9.0")
    fake.pypi_release("bgutil-ytdlp-pot-provider", "2.0.2", {
        "yt_dlp_plugins/extractor/getpot_bgutil.py": "__version__ = '2.0.2'\n",
    })
    fake.pypi_release("youtube-transcript-api", "1.2.6")
    fake.deno_release("2.10.0")


class Smoke:
    """A stand-in smoke test: records each request; passes unless told."""

    def __init__(self, ok=True) -> None:
        self.ok = ok
        self.calls: list[up.SmokeRequest] = []

    def __call__(self, request: up.SmokeRequest) -> up.SmokeOutcome:
        self.calls.append(request)
        ok = self.ok(request) if callable(self.ok) else self.ok
        return up.SmokeOutcome(ok, "" if ok else "the smoke test failed")


def run_update(ytop: Path, fake: FakeReleases, names=None, smoke=None, **kwargs):
    return up.update(
        ytop, CFG, names, http=up.Http(), smoke=smoke or Smoke(),
        progress=None, endpoints=fake.endpoints, **kwargs,
    )


def run_check(ytop: Path, fake_or_endpoints) -> up.CheckResult:
    endpoints = getattr(fake_or_endpoints, "endpoints", fake_or_endpoints)
    return up.check(ytop, CFG, now=NOW, http=up.Http(), endpoints=endpoints)


def folders(ytop: Path, name: str) -> list[str]:
    root = ytop / "components" / name
    return sorted(p.name for p in root.iterdir()) if root.is_dir() else []


def store_files(ytop: Path) -> set[str]:
    return {p.relative_to(ytop).as_posix() for p in (ytop / "components").rglob("*")}


# --- check ---------------------------------------------------------------------


def test_check_lists_newer_versions(ytop: Path, fake: FakeReleases):
    publish(fake)
    result = run_check(ytop, fake)
    assert result.status == "ok", result
    found = {a.name: (a.current, a.latest) for a in result.available}
    assert found == {
        "yt-dlp": ("2026.10.26", "2026.11.1"),
        "yt-dlp-ejs": ("0.8.0", "0.9.0"),
        "youtube-transcript-api": ("1.2.5", "1.2.6"),
        "deno": ("2.9.8", "2.10.0"),
    }
    # Deno is listed, but nothing needs it: an update leaves it alone.
    assert [a.required for a in result.available if a.name == "deno"] == [False]
    current = comp.read_current(ytop)
    assert current.last_check == NOW.isoformat()
    assert current.last_result == "ok"
    assert current.current == {**OLD, "bgutil-server": "2.0.2"}  # nothing switched


def test_check_without_a_store_compares_with_lib_common(tmp_path: Path, fake: FakeReleases):
    ytop = make_stick(tmp_path)
    (ytop / "components/current.json").unlink()
    publish(fake)
    found = {a.name: a.current for a in run_check(ytop, fake).available}
    assert found["yt-dlp"] == "2026.10.26"  # lib/common's


def test_prereleases_are_offered_only_to_a_prerelease(ytop: Path, fake: FakeReleases):
    fake.ytdlp_release("2026.11.1", prerelease=True)
    fake.pypi_release("yt-dlp-ejs", "0.9.0rc1")
    fake.pypi_release("youtube-transcript-api", "1.2.6")
    fake.pypi_release("youtube-transcript-api", "1.2.7", yanked=True)
    found = {a.name: a.latest for a in run_check(ytop, fake).available}
    assert found == {"youtube-transcript-api": "1.2.6"}
    current = comp.read_current(ytop)
    shutil.copytree(ytop / "components/yt-dlp-ejs/0.8.0", ytop / "components/yt-dlp-ejs/0.8.1b1")
    current.current["yt-dlp-ejs"] = "0.8.1b1"
    comp.write_current(ytop, current)
    found = {a.name: a.latest for a in run_check(ytop, fake).available}
    assert found["yt-dlp-ejs"] == "0.9.0rc1"


def test_pinned_bad_is_never_offered(ytop: Path, fake: FakeReleases):
    fake.ytdlp_release("2026.10.30")
    fake.ytdlp_release("2026.11.1")
    fake.pypi_release("yt-dlp-ejs", "0.9.0")
    current = comp.read_current(ytop)
    current.pinned_bad = {"yt-dlp": ["2026.11.01"], "yt-dlp-ejs": ["0.9.0"]}
    comp.write_current(ytop, current)
    found = {a.name: a.latest for a in run_check(ytop, fake).available}
    assert found == {"yt-dlp": "2026.10.30"}  # 2026.11.01 is 2026.11.1
    result = run_update(ytop, fake, ["yt-dlp", "yt-dlp-ejs"])
    assert result.updated == {"yt-dlp": ("2026.10.26", "2026.10.30")}
    assert comp.read_current(ytop).current["yt-dlp-ejs"] == "0.8.0"
    assert not (ytop / "components/yt-dlp/2026.11.1").exists()


def test_offline_and_rate_limited_never_raise(ytop: Path, fake: FakeReleases):
    before = (ytop / "components/current.json").read_bytes()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]  # closed again: nothing listens there
    closed = up.Endpoints(
        github_api=f"http://127.0.0.1:{port}", github_web=f"http://127.0.0.1:{port}",
        pypi=f"http://127.0.0.1:{port}",
    )
    assert run_check(ytop, closed).status == "offline"
    result = up.update(ytop, CFG, None, http=up.Http(), smoke=Smoke(), progress=None,
                       endpoints=closed)
    assert result.status == "offline" and not result.updated

    publish(fake)
    fake.add("/api/repos/yt-dlp/yt-dlp/releases", {"message": "API rate limit exceeded"},
             403, {"X-RateLimit-Remaining": "0"})
    assert run_check(ytop, fake).status == "rate_limited"
    assert run_update(ytop, fake).status == "rate_limited"
    publish(fake)
    fake.add("/pypi-host/pypi/yt-dlp-ejs/json", b"slow down", 429, {"Retry-After": "60"})
    assert run_check(ytop, fake).status == "rate_limited"
    assert comp.read_current(ytop).last_result == "rate_limited"
    # A plain 403 or a 500 is an error for that source, not a rate limit.
    publish(fake)
    fake.add("/pypi-host/pypi/yt-dlp-ejs/json", b"oops", 500)
    result = run_check(ytop, fake)
    assert result.status == "ok" and "yt-dlp-ejs" in result.errors
    assert comp.read_current(ytop).current == json.loads(before)["current"]


def test_user_agent_is_generic(ytop: Path, fake: FakeReleases):
    publish(fake)
    run_check(ytop, fake)
    run_update(ytop, fake, ["yt-dlp", "deno"])
    assert fake.requests
    for request in fake.requests:
        headers = {k.lower(): v for k, v in request.headers.items()}
        assert headers["user-agent"] == "YTOP-updater"
        assert not {"authorization", "cookie", "from"} & set(headers)


# --- update --------------------------------------------------------------------


def test_update_switches_verified_components(ytop: Path, fake: FakeReleases):
    publish(fake)
    smoke = Smoke()
    labels: list[str] = []
    result = up.update(ytop, CFG, None, http=up.Http(), smoke=smoke,
                       progress=labels.append, endpoints=fake.endpoints)
    assert result.status == "updated", result
    assert result.updated == {
        "yt-dlp": ("2026.10.26", "2026.11.1"),
        "yt-dlp-ejs": ("0.8.0", "0.9.0"),
        "youtube-transcript-api": ("1.2.5", "1.2.6"),
    }
    current = comp.read_current(ytop)
    assert current.current["yt-dlp"] == "2026.11.1"
    assert current.previous == {
        "yt-dlp": "2026.10.26", "yt-dlp-ejs": "0.8.0", "youtube-transcript-api": "1.2.5",
    }
    folder = ytop / "components/yt-dlp/2026.11.1"
    assert code_bytes(folder, "yt_dlp/__init__.py") is not None
    assert code_bytes(folder, "yt_dlp-2026.11.1.dist-info/RECORD") is not None
    # Packed (#37): one zip; scripts and data are not installed.
    assert sorted(p.name for p in folder.iterdir()) == [comp.COMPLETE, "lib.zip"]
    assert comp.python_paths(ytop)[0] == folder
    # The smoke test saw the staged components first, then the current others.
    # Packed folders (#37) are on the path as their zip, then themselves.
    request = smoke.calls[-1]
    staged = [p for p in request.paths if ".staging" in p.parts]
    assert len(staged) == 6 and request.paths[:6] == staged
    assert [p.name for p in staged[::2]] == ["lib.zip"] * 3
    assert request.paths[6] == ytop / "components/bgutil-plugin/2.0.2"
    assert request.paths[7] == ytop / "lib/common"
    assert request.expect == {
        "yt-dlp": "2026.11.1", "yt-dlp-ejs": "0.9.0", "youtube-transcript-api": "1.2.6",
    }
    assert request.video == "jNQXAC9IVRw"
    assert labels
    assert not (ytop / "components/.staging").exists()
    assert not [p for p in ytop.rglob("*") if comp.TEMP_SUFFIX in p.name]
    # Up to date now.
    again = run_update(ytop, fake)
    assert again.status == "up_to_date" and not again.updated


def test_update_verifies_checksums(ytop: Path, fake: FakeReleases):
    fake.ytdlp_release("2026.11.1", tamper=True)
    fake.pypi_release("yt-dlp-ejs", "0.9.0", tamper=True)
    fake.deno_release("2.10.0", tamper="macos-arm64")
    before = (ytop / "components/current.json").read_bytes()
    files = store_files(ytop)
    smoke = Smoke()
    result = run_update(ytop, fake, ["yt-dlp", "yt-dlp-ejs", "deno"], smoke=smoke)
    assert result.status == "failed"
    assert set(result.failed) == {"yt-dlp", "yt-dlp-ejs", "deno"}
    assert all("checksum" in why for why in result.failed.values()), result.failed
    assert not smoke.calls and not result.updated
    assert (ytop / "components/current.json").read_bytes() == before
    assert store_files(ytop) == files  # nothing new, nothing left in staging


def test_update_refuses_a_wheel_with_unsafe_paths(ytop: Path, fake: FakeReleases):
    wheel = make_wheel("yt-dlp", "2026.11.1", {"yt_dlp/__init__.py": ""},
                       extra={"../../evil.py": b"x"})
    sums = f"{hashlib.sha256(wheel).hexdigest()}  yt_dlp-2026.11.1-py3-none-any.whl\n"
    fake.github_release("yt-dlp/yt-dlp", "2026.11.1", {
        "yt_dlp-2026.11.1-py3-none-any.whl": wheel, "SHA2-256SUMS": sums.encode(),
    })
    result = run_update(ytop, fake, ["yt-dlp"])
    assert "yt-dlp" in result.failed
    assert not list(ytop.parent.rglob("evil.py"))
    assert comp.read_current(ytop).current["yt-dlp"] == "2026.10.26"


def test_failed_smoke_switches_nothing(ytop: Path, fake: FakeReleases):
    publish(fake)
    before = (ytop / "components/current.json").read_bytes()
    files = store_files(ytop)
    result = run_update(ytop, fake, smoke=Smoke(ok=False))
    assert result.status == "failed" and not result.updated
    assert set(result.failed) == {"yt-dlp", "yt-dlp-ejs", "youtube-transcript-api"}
    assert json.loads((ytop / "components/current.json").read_bytes())["current"] == (
        json.loads(before)["current"]
    )
    assert store_files(ytop) == files


def test_a_component_failing_its_smoke_test_holds_back_only_itself(
    ytop: Path, fake: FakeReleases
):
    publish(fake)
    smoke = Smoke(ok=lambda request: "yt-dlp-ejs" not in request.expect)
    result = run_update(ytop, fake, smoke=smoke)
    assert set(result.updated) == {"yt-dlp", "youtube-transcript-api"}
    assert set(result.failed) == {"yt-dlp-ejs"}
    assert comp.read_current(ytop).current["yt-dlp-ejs"] == "0.8.0"
    assert folders(ytop, "yt-dlp-ejs") == ["0.8.0"]
    # The switched pair passed a smoke test together.
    assert set(smoke.calls[-1].expect) == {"yt-dlp", "youtube-transcript-api"}


def test_update_keeps_one_previous_python_version_and_no_previous_deno(
    ytop: Path, fake: FakeReleases
):
    publish(fake)
    first = run_update(ytop, fake, ["yt-dlp", "deno"])
    assert set(first.updated) == {"yt-dlp", "deno"}, first
    fake.ytdlp_release("2026.11.5")
    fake.deno_release("2.11.0")
    second = run_update(ytop, fake, ["yt-dlp", "deno"])
    assert second.updated == {
        "yt-dlp": ("2026.11.1", "2026.11.5"), "deno": ("2.10.0", "2.11.0"),
    }
    current = comp.read_current(ytop)
    assert current.previous["yt-dlp"] == "2026.11.1"
    assert "deno" not in current.previous
    assert folders(ytop, "yt-dlp") == ["2026.11.1", "2026.11.5"]
    assert folders(ytop, "deno") == ["2.11.0"]


def test_deno_updated_for_every_stick_platform_only_when_required(
    ytop: Path, fake: FakeReleases
):
    publish(fake)  # ejs 0.9.0 declares no minimum Deno
    result = run_update(ytop, fake)
    assert "deno" not in result.updated and result.deno_minimum is None
    assert "deno" not in comp.read_current(ytop).current
    # A newer ejs needs a newer Deno than the stick's (2.9.8, its runtime's).
    fake.pypi_release("yt-dlp-ejs", "0.10.0",
                      metadata="Requires-External: deno (>=2.10)\n")
    smoke = Smoke()
    result = run_update(ytop, fake, smoke=smoke)
    assert result.deno_minimum == "2.10"
    assert result.updated == {"yt-dlp-ejs": ("0.9.0", "0.10.0"), "deno": ("2.9.8", "2.10.0")}
    for plat in PLATS:
        exe = comp.deno_for(ytop, plat)
        assert exe is not None, plat
    assert sorted(p.name for p in (ytop / "components/deno/2.10.0").iterdir()) == sorted(PLATS)
    # This host's Deno from the staged version was smoke tested.
    if current_platform() in PLATS:
        request = smoke.calls[-1]
        assert request.deno is not None and ".staging" in request.deno.parts
        assert request.deno_version == "2.10.0"
    # Asked for explicitly, Deno is updated even when nothing needs it.
    fake.deno_release("2.11.0")
    assert "deno" not in run_update(ytop, fake).updated
    assert run_update(ytop, fake, ["deno"]).updated == {"deno": ("2.10.0", "2.11.0")}


def test_deno_minimum_from_metadata_and_yt_dlp(tmp_path: Path):
    ejs = tmp_path / "ejs"
    (ejs / "yt_dlp_ejs-0.9.0.dist-info").mkdir(parents=True)
    (ejs / "yt_dlp_ejs-0.9.0.dist-info/METADATA").write_text(
        "Metadata-Version: 2.1\nName: yt-dlp-ejs\nVersion: 0.9.0\n"
        "Requires-External: deno (>=2.4.1)\n\nDescription mentions deno (>=9)\n"
    )
    ytdlp = tmp_path / "ytdlp"
    (ytdlp / "yt_dlp/utils").mkdir(parents=True)
    (ytdlp / "yt_dlp/utils/_jsruntime.py").write_text(
        "class DenoJsRuntime(JsRuntime):\n    MIN_SUPPORTED_VERSION = (2, 3, 0)\n\n"
        "class BunJsRuntime(JsRuntime):\n    MIN_SUPPORTED_VERSION = (9, 9, 9)\n"
    )
    assert up.deno_minimum({"yt-dlp-ejs": ejs}) == "2.4.1"
    assert up.deno_minimum({"yt-dlp": ytdlp}) == "2.3.0"
    assert up.deno_minimum({"yt-dlp": ytdlp, "yt-dlp-ejs": ejs}) == "2.4.1"
    assert up.deno_minimum({}) is None
    # The real yt-dlp-ejs this project pins declares none (recorded).
    import yt_dlp_ejs

    real = Path(yt_dlp_ejs.__file__).parent.parent
    assert up.deno_minimum({"yt-dlp-ejs": real}) is None


def test_stick_pulled_mid_update_keeps_old_current(
    ytop: Path, fake: FakeReleases, monkeypatch
):
    publish(fake)
    before = (ytop / "components/current.json").read_bytes()
    paths = comp.python_paths(ytop)

    def pulled(*_args, **_kwargs):
        raise OSError("the stick was pulled before current.json was replaced")

    monkeypatch.setattr(up, "write_current", pulled)
    result = run_update(ytop, fake)
    assert result.status == "error" and not result.updated
    assert (ytop / "components/current.json").read_bytes() == before
    assert comp.python_paths(ytop) == paths
    monkeypatch.undo()
    # The next update finishes the job over whatever was left.
    result = run_update(ytop, fake)
    assert set(result.updated) == {"yt-dlp", "yt-dlp-ejs", "youtube-transcript-api"}
    assert comp.python_paths(ytop)[0] == ytop / "components/yt-dlp/2026.11.1"
    assert not [p for p in ytop.rglob("*") if comp.TEMP_SUFFIX in p.name]
    assert not (ytop / "components/.staging").exists()


def test_one_current_json_write_per_batch(ytop: Path, fake: FakeReleases, monkeypatch):
    publish(fake)
    writes: list[comp.Current] = []
    real = up.write_current

    def count(ytop_: Path, current: comp.Current) -> None:
        writes.append(current)
        real(ytop_, current)

    monkeypatch.setattr(up, "write_current", count)
    run_update(ytop, fake, ["yt-dlp", "yt-dlp-ejs", "deno"])
    assert len(writes) == 1


# --- rollback ------------------------------------------------------------------


def test_rollback_switches_back_and_pins(ytop: Path, fake: FakeReleases):
    publish(fake)
    run_update(ytop, fake, ["yt-dlp", "deno"])
    up.rollback(ytop, "yt-dlp")
    current = comp.read_current(ytop)
    assert current.current["yt-dlp"] == "2026.10.26"
    assert current.pinned_bad["yt-dlp"] == ["2026.11.1"]
    assert "yt-dlp" not in current.previous
    assert folders(ytop, "yt-dlp") == ["2026.10.26"]
    assert "yt-dlp" not in {a.name for a in run_check(ytop, fake).available}
    # Deno keeps no previous: back to the runtime's own.
    up.rollback(ytop, "deno")
    current = comp.read_current(ytop)
    assert "deno" not in current.current and current.pinned_bad["deno"] == ["2.10.0"]
    assert folders(ytop, "deno") == []
    up.rollback(ytop, "deno")  # nothing current: nothing to do


# --- the bgutil plugin and its server ----------------------------------------------


def publish_plugin(fake: FakeReleases, version: str = "2.1.0", **server) -> None:
    fake.pypi_release("bgutil-ytdlp-pot-provider", version, {
        "yt_dlp_plugins/extractor/getpot_bgutil.py": f"__version__ = {version!r}\n",
    })
    fake.server_archive(version, **server)


def test_plugin_and_server_update_together(ytop: Path, fake: FakeReleases):
    publish_plugin(fake)
    installs: list[Path] = []
    result = run_update(ytop, fake, ["bgutil-plugin"],
                        server_install=lambda deno, server: installs.append(server))
    assert result.updated == {
        "bgutil-plugin": ("2.0.2", "2.1.0"), "bgutil-server": ("2.0.2", "2.1.0"),
    }
    assert not installs  # same dependencies: node_modules came from 2.0.2
    home = comp.server_home(ytop)
    assert home == ytop / "components/bgutil-server/2.1.0/server"
    assert (home / "src/generate_once.ts").read_text() == "// script 2.1.0\n"
    assert (home / "node_modules/axios/index.js").is_file()
    assert "devDependencies" not in json.loads((home / "package.json").read_text())
    assert (home.parent / "LICENSE").is_file()
    # The server keeps no previous copy (size); the plugin does.
    current = comp.read_current(ytop)
    assert current.previous == {"bgutil-plugin": "2.0.2"}
    assert folders(ytop, "bgutil-server") == ["2.1.0"]


def test_a_bundled_server_is_never_copied_from(ytop: Path, fake: FakeReleases):
    """A bundled server's node_modules holds only what its bundle reads:
    the new server is installed (and bundled) even with the same
    dependencies."""
    from ytop.bgserver import BUNDLE_MARKER

    (ytop / "components/bgutil-server/2.0.2/server" / BUNDLE_MARKER).write_text("{}")
    publish_plugin(fake)
    installs: list[Path] = []

    def install(deno: Path, server: Path) -> None:
        installs.append(server)
        fill_modules(deno, server)

    result = run_update(ytop, fake, ["bgutil-plugin"], server_install=install)
    assert "bgutil-server" in result.updated, result
    assert len(installs) == 1


def test_server_with_new_dependencies_is_installed(ytop: Path, fake: FakeReleases):
    publish_plugin(fake, dependencies={"axios": "^2"})

    def install(deno: Path, server: Path) -> None:
        (server / "node_modules/axios").mkdir(parents=True)
        (server / "node_modules/axios/index.js").write_text("v2\n")

    result = run_update(ytop, fake, ["bgutil-plugin"], server_install=install)
    assert "bgutil-server" in result.updated, result
    home = comp.server_home(ytop)
    assert home is not None and (home / "node_modules/axios/index.js").read_text() == "v2\n"


def fill_modules(deno: Path, server: Path) -> None:
    (server / "node_modules/axios").mkdir(parents=True, exist_ok=True)
    (server / "node_modules/axios/index.js").write_text("module.exports = 1\n")


def test_rolling_back_the_plugin_keeps_a_po_token_server(ytop: Path, fake: FakeReleases):
    """AutoUpdate.fix rolls back everything the update switched (the plugin
    and its server); the server has no previous copy, so it stays, unpinned,
    and the next automatic update brings one matching the plugin back."""
    publish(fake)
    publish_plugin(fake)
    fake.server_archive("2.0.2")
    result = run_update(ytop, fake, ["bgutil-plugin"])
    assert set(result.updated) == {"bgutil-plugin", BGUTIL}
    for name in list(result.updated):  # what AutoUpdate.rollback does
        up.rollback(ytop, name)
    current = comp.read_current(ytop)
    assert current.current["bgutil-plugin"] == "2.0.2"
    assert current.pinned_bad == {"bgutil-plugin": ["2.1.0"]}
    assert current.current[BGUTIL] == "2.1.0"  # kept: no previous to go back to
    assert comp.server_home(ytop) is not None
    assert folders(ytop, BGUTIL) == ["2.1.0"]
    # The next automatic update brings the server back in step with the plugin.
    result = run_update(ytop, fake, server_install=fill_modules)
    assert result.updated.get(BGUTIL) == ("2.1.0", "2.0.2"), result
    assert "bgutil-plugin" not in result.updated
    home = comp.server_home(ytop)
    assert home == ytop / "components/bgutil-server/2.0.2/server"
    assert folders(ytop, BGUTIL) == ["2.0.2"]


def test_a_stick_without_a_server_gets_one_on_the_next_update(
    ytop: Path, fake: FakeReleases,
):
    shutil.rmtree(ytop / "components" / BGUTIL)
    current = comp.read_current(ytop)
    del current.current[BGUTIL]
    comp.write_current(ytop, current)
    assert comp.server_home(ytop) is None
    publish(fake)
    fake.server_archive("2.0.2")
    result = run_update(ytop, fake, server_install=fill_modules)
    assert result.updated.get(BGUTIL) == (None, "2.0.2"), result
    assert comp.server_home(ytop) == ytop / "components/bgutil-server/2.0.2/server"


def test_prune_never_removes_the_current_server(ytop: Path):
    comp._prune(ytop, comp.read_current(ytop), [BGUTIL])
    assert folders(ytop, BGUTIL) == ["2.0.2"]


def test_a_server_that_cannot_be_had_holds_back_the_plugin(ytop: Path, fake: FakeReleases):
    fake.pypi_release("bgutil-ytdlp-pot-provider", "2.1.0", {
        "yt_dlp_plugins/extractor/getpot_bgutil.py": "__version__ = '2.1.0'\n",
    })  # no server archive at that tag
    result = run_update(ytop, fake, ["bgutil-plugin"])
    assert not result.updated and set(result.failed) == {"bgutil-plugin", "bgutil-server"}
    assert comp.read_current(ytop).current["bgutil-plugin"] == "2.0.2"


# --- bytecode and the real smoke test ---------------------------------------------


def test_installed_component_has_bytecode_and_imports_without_writing(
    ytop: Path, fake: FakeReleases, tmp_path: Path
):
    publish(fake)
    run_update(ytop, fake, ["yt-dlp"])
    folder = ytop / "components/yt-dlp/2026.11.1"
    data = code_bytes(folder, "yt_dlp/__init__.pyc")
    assert data is not None
    assert int.from_bytes(data[4:8], "little") == 0b01  # unchecked hash
    code = marshal.loads(data[16:])
    assert code.co_filename == ".ytop/components/yt-dlp/2026.11.1/yt_dlp/__init__.py"
    # A read-only copy (a write-protected stick): the packed pyc is used as
    # it is and nothing is written.
    copy = tmp_path / "ro"
    shutil.copytree(folder, copy)
    for path in sorted(copy.rglob("*"), reverse=True):
        path.chmod(stat.S_IREAD | (stat.S_IEXEC if path.is_dir() else 0))
    copy.chmod(stat.S_IREAD | stat.S_IEXEC)
    before = sorted(p.as_posix() for p in copy.rglob("*"))
    paths = [str(p) for p in code_paths(copy)]
    try:
        out = subprocess.run(
            [sys.executable, "-I", "-S", "-c",
             f"import sys; sys.path[0:0] = {paths!r}; import yt_dlp; "
             "print(yt_dlp.__version__, yt_dlp.__file__)"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        version, path = out.split(" ", 1)
        assert version == "2026.11.1"
        assert path.startswith(str(copy / "lib.zip"))
        assert sorted(p.as_posix() for p in copy.rglob("*")) == before
    finally:
        for path in [copy, *copy.rglob("*")]:
            path.chmod(0o755)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="runs the fake Linux Deno")
def test_real_smoke_test_imports_the_staged_versions(ytop: Path, fake: FakeReleases,
                                                     monkeypatch):
    """``run_smoke`` in a subprocess with this Python, offline (no probe)."""
    if current_platform() not in PLATS:
        pytest.skip("this host is not one of the stick's platforms")
    monkeypatch.delenv("YTOP_RUNTIME", raising=False)
    publish(fake)

    def offline_smoke(request: up.SmokeRequest) -> up.SmokeOutcome:
        assert request.python == sys.executable
        return up.run_smoke(replace(request, online=False))

    result = run_update(ytop, fake, ["yt-dlp", "yt-dlp-ejs", "deno"], smoke=offline_smoke)
    assert set(result.updated) == {"yt-dlp", "yt-dlp-ejs", "deno"}, result
    # A wheel that does not import fails the real smoke test.
    broken = make_wheel("yt-dlp", "2026.12.1", {"yt_dlp/__init__.py": "raise ImportError('no')\n"})
    sums = f"{hashlib.sha256(broken).hexdigest()}  yt_dlp-2026.12.1-py3-none-any.whl\n"
    fake.github_release("yt-dlp/yt-dlp", "2026.12.1", {
        "yt_dlp-2026.12.1-py3-none-any.whl": broken, "SHA2-256SUMS": sums.encode(),
    })
    result = run_update(ytop, fake, ["yt-dlp"], smoke=offline_smoke)
    assert not result.updated and "ImportError" in result.failed["yt-dlp"]
    assert comp.read_current(ytop).current["yt-dlp"] == "2026.11.1"


def test_smoke_deno_version_must_match(tmp_path: Path):
    if sys.platform == "win32":
        pytest.skip("a shell-script Deno")
    deno = tmp_path / "deno"
    deno.write_text('#!/bin/sh\necho "deno 2.9.8 (stable)"\n')
    deno.chmod(0o644)  # like a noexec stick: run from a copy
    lib = tmp_path / "lib"
    for name, version in OLD.items():
        _install(lib, name, version)
    request = up.SmokeRequest(
        python=sys.executable, paths=[lib], expect={}, deno=deno,
        deno_version="2.10.0", video="x", online=False, baseline=[],
    )
    outcome = up.run_smoke(request)
    assert not outcome.ok and "2.9.8" in outcome.message
    assert up.run_smoke(replace(request, deno_version="2.9.8")).ok


# --- the Deno asset names ---------------------------------------------------------


def test_deno_triples_match_the_pinned_artifacts():
    pins = tomllib.loads((ROOT / "stick/versions.toml").read_text(encoding="utf-8"))
    deno = {a["platform"]: a["url"] for a in pins["artifact"] if a["name"] == "deno"}
    assert set(deno) == set(up.DENO_TRIPLES)
    for plat, url in deno.items():
        assert url.endswith(f"/deno-{up.DENO_TRIPLES[plat]}.zip")


def test_component_dists_match_the_build():
    from stick_fakes import load_build_stick

    assert load_build_stick().COMPONENT_DISTS == up.COMPONENT_DISTS
    assert set(up.COMPONENT_DISTS) == set(comp.PYTHON_COMPONENTS)


def test_http_timeouts(fake: FakeReleases):
    http = up.Http()
    assert (http.connect_timeout, http.read_timeout) == (10, 60)
    fake.add("/x", b"hi")
    with http._open(fake.base + "/x") as response:  # 10 s to answer, then 60 s reads
        assert response.fp.raw._sock.gettimeout() == 60
        assert response.read() == b"hi"


# --- fix round 1: transport, timeouts, odd wheels, pin spellings ---------------------


@pytest.mark.parametrize("url", [
    "http://example.com/x", "http://10.0.0.1/x", "ftp://example.com/x",
    "http://127.0.0.1.example.com/x",
])
def test_plain_http_is_refused_except_on_loopback(url: str):
    with pytest.raises(up.UpdateError, match="https"):
        up.Http().get_bytes(url)  # refused before any connection


def test_loopback_http_is_allowed(fake: FakeReleases):
    fake.add("/x", b"hi")
    assert up.Http().get_bytes(fake.base + "/x") == b"hi"
    port = fake.base.rsplit(":", 1)[1]
    assert up.Http().get_bytes(f"http://localhost:{port}/x") == b"hi"


def test_an_http_api_base_is_an_error_not_a_fetch(ytop: Path):
    plain = up.Endpoints(github_api="http://example.com", github_web="http://example.com",
                         pypi="http://example.com")
    result = run_check(ytop, plain)
    assert result.status == "error" and "https" in result.message
    update = up.update(ytop, CFG, None, http=up.Http(), smoke=Smoke(), progress=None,
                       endpoints=plain)
    assert update.status == "failed" and not update.updated
    # A config pointing the API at plain http gets exactly these refusals.
    cfg = Config(updates=replace(CFG.updates, api_base="http://example.com/"))
    api = up.endpoints_for(cfg).github_api
    with pytest.raises(up.UpdateError, match="https"):
        up.Http().get_json(f"{api}/repos/yt-dlp/yt-dlp/releases")


def test_redirects_never_downgrade_or_leave_https(fake: FakeReleases):
    import urllib.request

    handler = up._SafeRedirect()
    request = urllib.request.Request("https://api.github.com/x")
    with pytest.raises(up.UpdateError, match="https"):
        handler.redirect_request(request, None, 302, "Found", {}, "http://evil.example/x")
    with pytest.raises(up.UpdateError, match="https"):  # even to loopback
        handler.redirect_request(request, None, 302, "Found", {}, "http://127.0.0.1/x")
    ok = handler.redirect_request(request, None, 302, "Found", {},
                                  "https://objects.githubusercontent.com/x")
    assert ok is not None and ok.full_url == "https://objects.githubusercontent.com/x"
    # Through the client: a loopback server redirecting to plain http elsewhere.
    fake.add("/go", b"", 302, {"Location": "http://example.com/wheel"})
    with pytest.raises(up.UpdateError, match="https"):
        up.Http().get_bytes(fake.base + "/go")
    fake.add("/here", b"ok")
    fake.add("/go2", b"", 302, {"Location": fake.base + "/here"})
    assert up.Http().get_bytes(fake.base + "/go2") == b"ok"


def test_deno_install_times_out(tmp_path: Path, monkeypatch):
    from ytop import bgserver

    def hang(*args, **kwargs):
        assert kwargs["timeout"] == bgserver.DENO_INSTALL_TIMEOUT == 300
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(bgserver.subprocess, "run", hang)
    with pytest.raises(bgserver.ServerError, match="timed out"):
        bgserver.deno_install(Path("deno"), tmp_path)


def test_a_hanging_deno_install_fails_the_server_and_returns(
    ytop: Path, fake: FakeReleases, monkeypatch
):
    from ytop import bgserver

    publish_plugin(fake, dependencies={"axios": "^2"})  # new deps: deno install
    monkeypatch.setattr(up, "_host_deno", lambda: Path("deno"))

    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(bgserver.subprocess, "run", hang)
    before = (ytop / "components/current.json").read_bytes()
    result = run_update(ytop, fake, ["bgutil-plugin"])
    assert not result.updated
    assert set(result.failed) == {"bgutil-plugin", "bgutil-server"}
    assert "timed out" in result.failed[BGUTIL]
    assert (ytop / "components/current.json").read_bytes() == before
    assert up._LOCK.acquire(blocking=False)  # released
    up._LOCK.release()


@pytest.mark.parametrize("error", [NotImplementedError("compression type 99"),
                                   RuntimeError("File is encrypted")])
def test_a_wheel_zipfile_cannot_read_is_a_failed_component(
    ytop: Path, fake: FakeReleases, monkeypatch, error
):
    publish(fake)

    def refuse(self, *args, **kwargs):
        raise error

    monkeypatch.setattr(up.zipfile.ZipFile, "read", refuse)
    result = run_update(ytop, fake, ["yt-dlp"])
    assert result.status == "failed" and "yt-dlp" in result.failed
    assert comp.read_current(ytop).current["yt-dlp"] == "2026.10.26"


def test_pins_match_any_spelling_of_the_version(ytop: Path, fake: FakeReleases):
    # The current version is pinned under another spelling: never kept as
    # the rollback copy.
    current = comp.read_current(ytop)
    current.pinned_bad = {"yt-dlp": ["2026.10.26.0"]}
    comp.write_current(ytop, current)
    publish(fake)
    run_update(ytop, fake, ["yt-dlp"])
    assert "yt-dlp" not in comp.read_current(ytop).previous
    # Rollback: no duplicate pin, and no switch back onto a pinned previous.
    fake.ytdlp_release("2026.11.5")
    current = comp.read_current(ytop)
    current.pinned_bad = {"yt-dlp": ["2026.11.05"]}
    comp.write_current(ytop, current)
    run_update(ytop, fake, ["yt-dlp"])  # 2026.11.5 is pinned: nothing newer
    current = comp.read_current(ytop)
    current.previous["yt-dlp"] = "2026.10.26"
    shutil.copytree(ytop / "components/yt-dlp/2026.11.1", ytop / "components/yt-dlp/2026.10.26",
                    dirs_exist_ok=True)
    current.pinned_bad = {"yt-dlp": ["2026.11.01", "2026.10.26.0"]}
    comp.write_current(ytop, current)
    up.rollback(ytop, "yt-dlp")
    current = comp.read_current(ytop)
    assert current.pinned_bad["yt-dlp"] == ["2026.11.01", "2026.10.26.0"]
    assert "yt-dlp" not in current.current  # lib/common's copy


# --- edge cases (#12) -------------------------------------------------------------


def test_check_reports_a_server_out_of_step_with_its_plugin(ytop: Path, fake: FakeReleases):
    """After a plugin rollback the newer server stays; the check says it
    needs to go back in step, though no newer plugin is offered."""
    publish(fake)
    publish_plugin(fake)
    fake.server_archive("2.0.2")
    result = run_update(ytop, fake, ["bgutil-plugin"])
    for name in list(result.updated):
        up.rollback(ytop, name)
    found = run_check(ytop, fake)
    assert found.status == "ok"
    servers = [a for a in found.available if a.name == BGUTIL]
    assert servers == [up.Available(BGUTIL, "2.1.0", "2.0.2")], found.available
    assert "bgutil-plugin" not in {a.name for a in found.available}


def test_check_reports_a_missing_server_once(ytop: Path, fake: FakeReleases):
    """A store that names a server whose folder is gone; and a newer plugin
    lists its server only once."""
    shutil.rmtree(ytop / "components" / BGUTIL)
    publish(fake)
    found = run_check(ytop, fake)
    assert [a for a in found.available if a.name == BGUTIL] == [
        up.Available(BGUTIL, None, "2.0.2"),
    ]
    publish_plugin(fake)
    found = run_check(ytop, fake)
    assert [a for a in found.available if a.name == BGUTIL] == [
        up.Available(BGUTIL, None, "2.1.0"),
    ]


def test_check_is_quiet_about_a_server_in_step(ytop: Path, fake: FakeReleases):
    publish(fake)
    found = run_check(ytop, fake)
    assert BGUTIL not in {a.name for a in found.available}


def test_a_rate_limit_at_the_server_step_keeps_what_was_staged(
    ytop: Path, fake: FakeReleases,
):
    publish(fake)
    publish_plugin(fake)
    fake.add("/api/repos/Brainicism/bgutil-ytdlp-pot-provider/releases/tags/2.1.0",
             {"message": "API rate limit exceeded"}, 403, {"X-RateLimit-Remaining": "0"})
    result = run_update(ytop, fake)
    assert result.status == "updated", result
    assert result.updated["yt-dlp"] == ("2026.10.26", "2026.11.1")
    # The plugin waits for its server; the rest is not fetched any more.
    assert "bgutil-plugin" not in result.updated
    assert "rate limited" in result.failed[BGUTIL]
    assert "bgutil-plugin" in result.failed
    current = comp.read_current(ytop).current
    assert current["yt-dlp"] == "2026.11.1" and current["bgutil-plugin"] == "2.0.2"


def test_a_dropped_connection_after_staging_skips_the_remaining_steps(
    ytop: Path, fake: FakeReleases, monkeypatch,
):
    publish(fake)
    real = up.SOURCES["youtube-transcript-api"]

    class Gone(up.Source):
        def latest(self, *args, **kwargs):
            raise up.Offline("the connection was cut off")

    monkeypatch.setitem(up.SOURCES, "youtube-transcript-api", Gone(real.name))
    result = run_update(ytop, fake, ["yt-dlp", "youtube-transcript-api", "deno"])
    assert result.status == "updated", result
    assert "yt-dlp" in result.updated
    assert "cut off" in result.failed["youtube-transcript-api"]
    assert "cut off" in result.failed["deno"]  # never fetched once offline


def test_a_network_failure_before_anything_was_staged_is_still_offline(
    ytop: Path, fake: FakeReleases,
):
    publish(fake)
    fake.add("/api/repos/yt-dlp/yt-dlp/releases", {"message": "API rate limit exceeded"},
             403, {"X-RateLimit-Remaining": "0"})
    assert run_update(ytop, fake, ["yt-dlp"]).status == "rate_limited"


def test_a_stick_whose_server_cannot_be_built_is_up_to_date(
    ytop: Path, fake: FakeReleases,
):
    """No server, and none can be built here: with nothing newer, "Update
    now" says up to date, not "failed: bgutil-server"."""
    shutil.rmtree(ytop / "components" / BGUTIL)
    current = comp.read_current(ytop)
    del current.current[BGUTIL]
    comp.write_current(ytop, current)
    fake.ytdlp_release(OLD["yt-dlp"])
    fake.pypi_release("yt-dlp-ejs", OLD["yt-dlp-ejs"])
    fake.pypi_release("bgutil-ytdlp-pot-provider", OLD["bgutil-plugin"])
    fake.pypi_release("youtube-transcript-api", OLD["youtube-transcript-api"])
    # no server archive: it cannot be had
    result = run_update(ytop, fake)
    assert result.status == "up_to_date", result
    assert not result.updated
    assert BGUTIL in result.message  # still said, as a note
    # Asked for by name, it is a failure.
    result = run_update(ytop, fake, [BGUTIL])
    assert result.status == "failed" and BGUTIL in result.failed


# --- the stick's lock (#9) -----------------------------------------------------------


def test_update_takes_the_sticks_lock_and_leaves_another_process_alone(
    ytop: Path, fake: FakeReleases, monkeypatch,
):
    """A CLI ``components update`` and a server on one stick: the second
    one finds the stick locked, says busy and leaves the other's staging."""
    import os

    from ytop.stick import boot

    publish(fake)
    lock = ytop.parent / boot.LOCK
    other = {"pid": 4242, "started": None, "ticks": None}
    lock.write_text(json.dumps(other))
    monkeypatch.setattr(boot, "pid_alive", lambda pid: pid == 4242)
    staging = ytop / "components" / up.STAGING / "yt-dlp-2026.11.1"
    staging.mkdir(parents=True)
    result = run_update(ytop, fake)
    assert result.status == "busy" and "another YTOP" in result.message
    assert staging.is_dir()  # the other process's staging is left alone
    assert json.loads(lock.read_text()) == other

    monkeypatch.setattr(boot, "pid_alive", lambda pid: False)  # it is gone
    seen: list[bool] = []
    real = up._update

    def spy(*args, **kwargs):
        seen.append(json.loads(lock.read_text())["pid"] == os.getpid())
        return real(*args, **kwargs)

    monkeypatch.setattr(up, "_update", spy)
    assert run_update(ytop, fake).status == "updated"
    assert seen == [True]  # held while updating
    assert not lock.exists()  # and let go


def test_update_inside_an_app_update_reuses_its_lock(ytop: Path, fake: FakeReleases):
    """The app update holds the stick's lock and updates components in it."""
    from ytop import sticklock
    from ytop.stick import boot

    publish(fake)
    lock = ytop.parent / boot.LOCK
    sticklock.acquire(lock, lambda: AssertionError("busy"))
    try:
        assert run_update(ytop, fake).status == "updated"
        assert sticklock.held(lock) and lock.exists()  # still the app update's
    finally:
        sticklock.release(lock)
    assert not lock.exists()
