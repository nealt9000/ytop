"""PO-token support: the bgutil server under the bundled Deno."""

from __future__ import annotations

from pathlib import Path

import pytest

from ytop import components as comp
from ytop.config import Config
from ytop.yt import pot
from ytop.yt.probe import base_options


def make_server(ytop: Path, version: str = "2.0.2") -> Path:
    server = ytop / "components/bgutil-server" / version / "server"
    (server / "src").mkdir(parents=True)
    (server / "src/generate_once.ts").write_text("// script\n")
    (server / "node_modules").mkdir()
    current = comp.read_current(ytop)
    current.current["bgutil-server"] = version
    comp.write_current(ytop, current)
    return server


@pytest.fixture
def stick(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "stick"
    (home / ".ytop").mkdir(parents=True)
    monkeypatch.setenv("YTOP_HOME", str(home))
    monkeypatch.delenv("YTOP_RUNTIME", raising=False)
    monkeypatch.delenv("DENO_DIR", raising=False)
    return home / ".ytop"


def test_server_home_is_passed_to_yt_dlp(stick: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server = make_server(stick)
    opts: dict = {}
    pot.apply_auth_options(opts, Config())
    assert opts["extractor_args"] == {
        "youtubepot-bgutilscript": {"server_home": [str(server)]}
    }
    # base_options goes through the same builder.
    assert base_options(Config())["extractor_args"] == opts["extractor_args"]


def test_no_server_no_extractor_args(stick: Path) -> None:
    opts: dict = {}
    pot.apply_auth_options(opts, Config())
    assert "extractor_args" not in opts
    # A current.json naming a server whose folder is gone: still none.
    current = comp.read_current(stick)
    current.current["bgutil-server"] = "9.9.9"
    comp.write_current(stick, current)
    pot.apply_auth_options(opts, Config())
    assert "extractor_args" not in opts


def test_existing_extractor_args_are_kept(stick: Path) -> None:
    server = make_server(stick)
    opts: dict = {"extractor_args": {"youtube": {"player_client": ["web"]}}}
    pot.apply_auth_options(opts, Config())
    assert opts["extractor_args"]["youtube"] == {"player_client": ["web"]}
    assert opts["extractor_args"]["youtubepot-bgutilscript"] == {"server_home": [str(server)]}


def test_provider_off_passes_no_server(stick: Path) -> None:
    make_server(stick)
    opts: dict = {}
    pot.apply_auth_options(opts, Config(pot_provider="off"))
    assert "extractor_args" not in opts
    assert opts["plugin_dirs"] == []


def test_pot_status_reports_the_deno_server(
    stick: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    make_server(stick)
    deno = tmp_path / "deno"
    deno.write_text("x")
    monkeypatch.setattr(pot, "deno_path", lambda: deno)
    monkeypatch.setattr(pot, "plugin_available", lambda: True)
    status, evidence = pot.pot_status()
    assert status == "ok"
    assert "Deno" in evidence and "2.0.2" in evidence
    assert "node" not in evidence.replace("Deno", "").lower().replace("node_modules", "")


def test_pot_status_without_the_server_or_deno(
    stick: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(pot, "plugin_available", lambda: True)
    monkeypatch.setattr(pot, "deno_path", lambda: tmp_path / "deno")
    status, evidence = pot.pot_status()
    assert status == "warn"
    assert "server" in evidence and "node" not in evidence.lower()
    make_server(stick)
    monkeypatch.setattr(pot, "deno_path", lambda: None)
    status, evidence = pot.pot_status()
    assert status == "warn" and "Deno" in evidence


def test_library_cache_keeps_the_pot_script_off_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from ytop.layout import LibraryLayout
    from ytop.yt.probe import use_library_deno_dir

    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv("DENO_DIR", raising=False)
    lib = tmp_path / "lib"
    use_library_deno_dir(lib)
    cache = LibraryLayout(lib).cache_dir
    assert os.environ["XDG_CACHE_HOME"] == str(cache)
    assert os.environ["DENO_DIR"] == str(cache / "deno")
    # The plugin's own rule (getpot_bgutil_script._script_cache_dir):
    # <XDG_CACHE_HOME>/bgutil-ytdlp-pot-provider, inside the library.
    plugin_cache = Path(os.environ["XDG_CACHE_HOME"]) / "bgutil-ytdlp-pot-provider"
    assert plugin_cache.is_relative_to(lib)


def test_base_options_pins_the_cache_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv("DENO_DIR", raising=False)
    base_options(Config(), tmp_path / "lib")
    assert Path(os.environ["XDG_CACHE_HOME"]).is_relative_to(tmp_path / "lib")
    # An explicit choice is respected (first library wins, like DENO_DIR).
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "mine"))
    base_options(Config(), tmp_path / "other")
    assert os.environ["XDG_CACHE_HOME"] == str(tmp_path / "mine")


def test_ytop_does_not_otherwise_read_xdg_cache_home() -> None:
    src = Path(pot.__file__).resolve().parents[1]
    users = [
        p.relative_to(src).as_posix() for p in src.rglob("*.py")
        if "XDG_CACHE_HOME" in p.read_text(encoding="utf-8")
    ]
    assert users == ["yt/probe.py"]


def test_linux_cache_layout_finds_the_server_on_the_stick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # boot.py on Linux: runtime_root is the cache copy, --home the stick.
    stick = tmp_path / "stick"
    cache_root = tmp_path / "home/.cache/ytop/runtime/build1"
    (cache_root / "runtime/linux-x86_64/bin").mkdir(parents=True)
    server = make_server(stick / ".ytop")
    monkeypatch.setenv("YTOP_HOME", str(stick))
    monkeypatch.setenv("YTOP_RUNTIME", str(cache_root / "runtime/linux-x86_64"))
    assert pot.current_server_home() == server
    opts: dict = {}
    pot.apply_auth_options(opts, Config())
    assert opts["extractor_args"]["youtubepot-bgutilscript"]["server_home"] == [str(server)]
    assert not (cache_root / "components").exists()


def test_check_browser_spec():
    assert pot.check_browser_spec(" firefox:Q6uSzFvx.Profile 1 ") == "firefox:Q6uSzFvx.Profile 1"
    with pytest.raises(ValueError, match="unknown browser 'netscape'"):
        pot.check_browser_spec("netscape")


def test_check_browser_spec_lowercases_only_the_browser():
    assert pot.check_browser_spec("Firefox:My Profile") == "firefox:My Profile"
    assert pot.check_browser_spec("BRAVE") == "brave"


@pytest.mark.parametrize("value", ["", "   ", ":profile"])
def test_check_browser_spec_rejects_empty_browser(value):
    with pytest.raises(ValueError, match="unknown browser ''"):
        pot.check_browser_spec(value)
