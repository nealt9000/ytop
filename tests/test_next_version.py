import importlib.util
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "tools" / "next_version.py"


def _load():
    spec = importlib.util.spec_from_file_location("next_version", TOOL)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["next_version"] = module
    spec.loader.exec_module(module)
    return module


nv = _load()


def C(subject, body=""):
    return nv.Commit(sha="abc1234def", subject=subject, body=body)


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def commit(repo, subject, path=None, body=""):
    if path:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(target.read_text() + "x\n" if target.exists() else "x\n")
        git(repo, "add", "-A")
    msg = subject + (f"\n\n{body}" if body else "")
    git(repo, "commit", "--allow-empty", "-m", msg)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.name", "t")
    git(tmp_path, "config", "user.email", "t@example.invalid")
    git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "ytop"\nversion = "0.1.0"\n'
    )
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "chore: init")
    return tmp_path


@pytest.fixture
def released(repo):
    commit(repo, "feat: a", "src/a.py")
    commit(repo, "fix: b")
    return repo


def test_bump_rules():
    assert nv.bump("0.1.0", "minor") == "0.2.0"
    assert nv.bump("0.2.3", "patch") == "0.2.4"
    assert nv.bump("0.2.3", "major") == "0.3.0"
    assert nv.bump("1.4.2", "major") == "2.0.0"
    assert nv.bump_level([C("feat: x"), C("fix: y")]) == "minor"
    assert nv.bump_level([C("fix: y"), C("perf: z")]) == "patch"
    assert nv.bump_level([C("refactor(web): z")]) == "patch"
    assert nv.bump_level([C("Update readme")]) == "patch"
    assert nv.bump_level([C("feat!: x")]) == "major"
    assert nv.bump_level([C("fix: y", "BREAKING CHANGE: old flag gone")]) == "major"
    assert nv.bump_level([]) is None


def test_ships_only_for_shipped_paths():
    assert (
        nv.ships(
            [
                "docs/x.md",
                "tests/t.py",
                ".github/workflows/ci.yml",
                "tools/smoke_stick.py",
            ]
        )
        is False
    )
    assert nv.ships(["src/ytop/app.py"])
    assert nv.ships(["uv.lock"])
    assert nv.ships(["tools/build_stick.py"])


def test_first_release_from_untagged_history(released):
    p = nv.plan(released)
    assert p.release is True
    assert p.version == "0.2.0"
    assert "## Features" in p.notes
    assert "a (" in p.notes
    assert "## Fixes" in p.notes


def test_counts_only_commits_since_the_last_tag(released):
    git(released, "tag", "-a", "v0.2.0", "-m", "n")
    commit(released, "fix: c", "src/c.py")
    p = nv.plan(released)
    assert p.version == "0.2.1"
    assert "c (" in p.notes
    assert "a (" not in p.notes


def test_no_release_without_shipped_changes(released):
    git(released, "tag", "-a", "v0.2.0", "-m", "n")
    commit(released, "docs: d", "README.md")
    commit(released, "test: e", "tests/e.py")
    p = nv.plan(released)
    assert p.release is False
    assert "no shipped" in p.reason


def test_merge_commits_are_ignored(released):
    git(released, "tag", "-a", "v0.2.0", "-m", "n")
    git(released, "checkout", "-q", "-b", "x")
    commit(released, "fix: f", "src/f.py")
    git(released, "checkout", "-q", "main")
    git(
        released,
        "merge",
        "--no-ff",
        "x",
        "-m",
        "Merge branch 'x'\n\nBREAKING CHANGE: not really",
    )
    assert nv.plan(released).version == "0.2.1"


def test_write_version_updates_all_three(tmp_path):
    (tmp_path / "src" / "ytop").mkdir(parents=True)
    for rel in ("pyproject.toml", "src/ytop/__init__.py", "uv.lock"):
        (tmp_path / rel).write_text((REPO / rel).read_text())

    def others(path):
        pkgs = tomllib.loads(path.read_text())["package"]
        return {p["name"]: p.get("version") for p in pkgs if p["name"] != "ytop"}

    before = others(tmp_path / "uv.lock")
    nv.write_version(tmp_path, "9.8.7")
    assert (
        tomllib.loads((tmp_path / "pyproject.toml").read_text())["project"]["version"]
        == "9.8.7"
    )
    assert '__version__ = "9.8.7"' in (tmp_path / "src/ytop/__init__.py").read_text()
    lock = tomllib.loads((tmp_path / "uv.lock").read_text())["package"]
    assert next(p for p in lock if p["name"] == "ytop")["version"] == "9.8.7"
    assert others(tmp_path / "uv.lock") == before


def test_write_version_missing_pattern_exits(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    with pytest.raises(SystemExit):
        nv.write_version(tmp_path, "1.0.0")


def test_github_output_lines(released):
    out, notes = released / "out.txt", released / "notes.md"
    r = subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "plan",
            "--repo",
            str(released),
            "--github-output",
            str(out),
            "--notes-file",
            str(notes),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "release=true" in r.stdout
    text = out.read_text()
    assert "release=true" in text and "version=0.2.0" in text
    assert notes.read_text().strip()
