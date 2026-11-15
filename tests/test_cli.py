from __future__ import annotations

from pathlib import Path

from factories import make_library as factory_library
from ytop.cli import main
from ytop.layout import read_library


def test_init_prints_id(tmp_path: Path, capsys):
    root = tmp_path / "lib"
    code = main(["init", str(root), "--label", "Stick", "--description", "usb"])
    assert code == 0
    printed = capsys.readouterr().out.strip()
    info = read_library(root)
    assert info is not None
    assert printed == info.id
    assert info.label == "Stick"
    assert info.description == "usb"


def test_init_then_fetch_requires_library(tmp_path: Path, capsys):
    root = tmp_path / "nothing"
    code = main(["fetch", "PL1", "--library", str(root)])
    assert code == 2
    assert (
        f"No YTOP library at {root}. Run 'ytop init {root}' "
        f"or 'ytop migrate <old> {root}'."
    ) in capsys.readouterr().err

    assert main(["init", str(root)]) == 0
    capsys.readouterr()
    # Now the library exists, so the missing-library gate no longer trips.
    assert main(["fetch", "PL1", "--library", str(root), "--limit", "0"]) == 2
    assert "--limit must be at least 1" in capsys.readouterr().err


def test_commands_needing_a_library_exit_2(tmp_path: Path, capsys):
    root = tmp_path / "nothing"
    for argv in (["index"], ["transcripts", "--index-only"], ["serve"]):
        assert main([*argv, "--library", str(root)]) == 2
        assert "No YTOP library at" in capsys.readouterr().err


def test_global_library_flag_and_env(tmp_path: Path, monkeypatch, capsys):
    root = tmp_path / "envlib"
    main(["init", str(root)])
    capsys.readouterr()
    monkeypatch.setenv("YTOP_LIBRARY", str(root))
    assert main(["index"]) == 0
    assert main(["--library", str(root), "index"]) == 0


def test_fetch_to_must_be_a_library(tmp_path: Path, capsys):
    main(["init", str(tmp_path / "home")])
    capsys.readouterr()
    code = main(["fetch", "PL1", "--library", str(tmp_path / "home"),
                 "--to", str(tmp_path / "elsewhere")])
    assert code == 2
    assert "--to" in capsys.readouterr().err


def test_fetch_to_resolves_library_id_and_path(tmp_path: Path, monkeypatch):
    from ytop import cli

    home, other = tmp_path / "home", tmp_path / "other"
    main(["init", str(home)])
    main(["init", str(other)])
    info = read_library(other)
    assert info is not None
    monkeypatch.setattr("ytop.libraries.registry_path_default",
                        lambda: tmp_path / "registry.json")
    assert cli._resolve_destination(str(other), home) == other
    assert cli._resolve_destination("nope", home) is None


def test_serve_uses_launch_library(tmp_path: Path, monkeypatch):
    root = tmp_path / "lib"
    main(["init", str(root)])
    seen = {}

    def fake_run(app, host, port, sock=None):
        seen["app"] = app

    monkeypatch.setattr("ytop.web.app._run_server", fake_run)
    code = main(["serve", "--library", str(root), "--no-open"])
    assert code == 0
    libs = seen["app"].state.library.libraries
    assert libs.default().layout.root == root


def test_doctor_reports_missing_library(tmp_path: Path, capsys):
    from ytop.doctor import run_checks

    d = run_checks(tmp_path / "none")
    check = next(c for c in d.checks if c.name == "library")
    assert check.status == "warn"
    assert "ytop init" in check.evidence


def test_doctor_reports_library_details(tmp_path: Path):
    from ytop.doctor import run_checks

    main(["init", str(tmp_path / "lib")])
    d = run_checks(tmp_path / "lib")
    check = next(c for c in d.checks if c.name == "library")
    assert check.status == "ok"
    assert "schema" in check.evidence and "free" in check.evidence


def _newer_library(root: Path) -> Path:
    """A library whose marker was written by a newer YTOP (read-only here)."""
    import json

    from ytop.layout import LibraryLayout

    main(["init", str(root)])
    marker = LibraryLayout(root).marker
    data = json.loads(marker.read_text())
    data["schema"] = 99
    marker.write_text(json.dumps(data))
    return root


def test_write_commands_refuse_a_newer_library(tmp_path: Path, capsys):
    from ytop.libraries import READ_ONLY_MESSAGE

    newer = _newer_library(tmp_path / "newer")
    capsys.readouterr()
    for argv in (
        ["fetch", "PL1", "--dry-run"],
        ["transcripts", "--index-only"],
        ["index"],
    ):
        assert main([*argv, "--library", str(newer)]) == 2, argv
        assert READ_ONLY_MESSAGE in capsys.readouterr().err, argv
    from ytop.layout import LibraryLayout

    assert not LibraryLayout(newer).index_db.exists()


def test_fetch_to_a_newer_library_is_refused(tmp_path: Path, capsys):
    from ytop.libraries import READ_ONLY_MESSAGE

    home = tmp_path / "home"
    main(["init", str(home)])
    newer = _newer_library(tmp_path / "newer")
    capsys.readouterr()
    code = main(["fetch", "PL1", "--dry-run", "--library", str(home),
                 "--to", str(newer)])
    assert code == 2
    assert READ_ONLY_MESSAGE in capsys.readouterr().err


def test_migrate_into_a_newer_library_is_refused(tmp_path: Path, capsys):
    from ytop.libraries import READ_ONLY_MESSAGE

    old = tmp_path / "old"
    (old / "playlists").mkdir(parents=True)
    newer = _newer_library(tmp_path / "newer")
    before = sorted(p.relative_to(newer) for p in newer.rglob("*"))
    capsys.readouterr()
    assert main(["migrate", str(old), str(newer)]) == 2
    assert READ_ONLY_MESSAGE in capsys.readouterr().err
    assert sorted(p.relative_to(newer) for p in newer.rglob("*")) == before


def test_launch_root_falls_back_to_ytop_home(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from ytop.cli import _launch_root

    monkeypatch.delenv("YTOP_LIBRARY", raising=False)
    monkeypatch.delenv("YTOP_HOME", raising=False)
    assert _launch_root(SimpleNamespace(library=None)) == (
        Path("~/Videos/YTOP").expanduser()
    )
    monkeypatch.setenv("YTOP_HOME", str(tmp_path / "stick"))
    assert _launch_root(SimpleNamespace(library=None)) == tmp_path / "stick"
    monkeypatch.setenv("YTOP_LIBRARY", str(tmp_path / "env"))
    assert _launch_root(SimpleNamespace(library=None)) == tmp_path / "env"
    flag = SimpleNamespace(library=str(tmp_path / "flag"))
    assert _launch_root(flag) == tmp_path / "flag"


def test_cli_index_rebuilds_both(tmp_path: Path, capsys):
    import json

    from factories import add_playlist

    layout = factory_library(tmp_path)
    add_playlist(layout, "P1", "First", [("a1", "A"), ("a2", "B"), ("a3", "C")])
    add_playlist(layout, "P2", "Second", [("b1", "A"), ("b2", "B"), ("b3", "C")])
    assert main(["--library", str(layout.root), "index", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["playlists"] == 2
    assert out["videos"] == 6
    assert out["database"].endswith("index.sqlite3")
