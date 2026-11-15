"""The going-public script: privacy check, one-commit export, refusals. No network."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REAL_SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "make_public_repo.sh"
NAME = "YTOP Bot"
EMAIL = "ytop-bot@users.noreply.github.com"
# Built at runtime so this file itself passes the privacy check.
PERSONAL = "someone@" + "gmail.com"
HOME_LOOKALIKE = "/home/" + "user-someone"
WIN_LOWER = "C:\\" + "users\\someone"


def _env(tmp: Path) -> dict[str, str]:
    home = tmp / "home"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        HOME=str(home),
        GIT_CONFIG_GLOBAL=str(home / "gitconfig"),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="Source Author",
        GIT_AUTHOR_EMAIL="source@example.invalid",
        GIT_COMMITTER_NAME="Source Author",
        GIT_COMMITTER_EMAIL="source@example.invalid",
    )
    return env


def _git(cwd: Path, env: dict[str, str], *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def source(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    env = _env(tmp_path)
    repo = tmp_path / "src"
    repo.mkdir()
    _git(repo, env, "init", "-q", "-b", "master")
    (repo / "README.md").write_text("hello\n")
    (repo / "a.py").write_text("print('x')\n")
    _git(repo, env, "add", "-A")
    _git(repo, env, "commit", "-q", "-m", "one")
    (repo / "b.py").write_text("print('y')\n")
    _git(repo, env, "add", "-A")
    _git(repo, env, "commit", "-q", "-m", "two")
    tools = repo / "tools"
    tools.mkdir()
    shutil.copy(REAL_SCRIPT, tools / "make_public_repo.sh")
    return repo, env


def _run(repo: Path, env: dict[str, str], target: Path):
    return subprocess.run(
        [
            "bash",
            str(repo / "tools" / "make_public_repo.sh"),
            str(target),
            "--name",
            NAME,
            "--email",
            EMAIL,
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )


def test_planted_personal_email_fails_and_commits_nothing(source, tmp_path):
    repo, env = source
    (repo / "notes.txt").write_text(f"\ncontact {PERSONAL}\n")
    _git(repo, env, "add", "-A")
    _git(repo, env, "commit", "-q", "-m", "oops")
    target = tmp_path / "out"
    r = _run(repo, env, target)
    assert r.returncode != 0
    assert f"notes.txt:2:{PERSONAL}" in r.stderr
    assert not (target / ".git").exists()


def test_clean_tree_makes_one_commit_with_given_identity(source, tmp_path):
    repo, env = source
    target = tmp_path / "out"
    r = _run(repo, env, target)
    assert r.returncode == 0, r.stderr
    assert (target / "b.py").exists()
    log = _git(target, env, "log", "--format=%an <%ae>|%cn <%ce>").splitlines()
    assert log == [f"{NAME} <{EMAIL}>|{NAME} <{EMAIL}>"]
    assert _git(target, env, "branch", "--show-current").strip() == "main"
    assert "gh repo create" in r.stdout and "push -u origin main" in r.stdout
    assert "source@example.invalid" not in _git(target, env, "log", "--format=fuller")


def test_non_empty_target_is_refused(source, tmp_path):
    repo, env = source
    target = tmp_path / "out"
    target.mkdir()
    (target / "keep.txt").write_text("x")
    r = _run(repo, env, target)
    assert r.returncode != 0
    assert "not empty" in r.stderr
    assert not (target / ".git").exists()


def _commit_file(repo, env, name, data):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data)
    _git(repo, env, "add", "-A")
    _git(repo, env, "commit", "-q", "-m", "add " + name)


def test_uppercase_personal_email_fails(source, tmp_path):
    repo, env = source
    _commit_file(repo, env, "n.txt", "x " + PERSONAL.upper() + "\n")
    r = _run(repo, env, tmp_path / "out")
    assert r.returncode != 0 and "n.txt:1:" in r.stderr


def test_home_path_fails_including_lookalike_placeholder(source, tmp_path):
    repo, env = source
    _commit_file(repo, env, "n.txt", HOME_LOOKALIKE + "/x\n" + WIN_LOWER + "\n")
    r = _run(repo, env, tmp_path / "out")
    assert r.returncode != 0
    assert "n.txt:1:" + HOME_LOOKALIKE in r.stderr
    assert "n.txt:2:" in r.stderr


def test_binary_file_with_personal_email_fails(source, tmp_path):
    repo, env = source
    _commit_file(repo, env, "img.bin", b"\x00\x01\x02" + PERSONAL.encode() + b"\x00")
    r = _run(repo, env, tmp_path / "out")
    assert r.returncode != 0 and "img.bin" in r.stderr


def test_placeholder_path_allowed_and_binaries_listed(source, tmp_path):
    repo, env = source
    _commit_file(repo, env, "n.txt", "see /home/user/x and /home/runner/y\n")
    _commit_file(repo, env, "pic.png", b"\x89PNG\x00\x00")
    r = _run(repo, env, tmp_path / "out")
    assert r.returncode == 0, r.stderr
    assert "CHECK THESE BY EYE" in r.stdout and "pic.png" in r.stdout


def test_non_noreply_email_is_refused(source, tmp_path):
    repo, env = source
    target = tmp_path / "out"
    r = subprocess.run(
        [
            "bash",
            str(repo / "tools" / "make_public_repo.sh"),
            str(target),
            "--name",
            NAME,
            "--email",
            "a@example.com",
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0 and "noreply" in r.stderr
    assert not (target / ".git").exists()


def test_country_code_personal_email_fails(source, tmp_path):
    repo, env = source
    _commit_file(
        repo, env, "n.txt", "x someone@" + "yahoo.co.uk\ny someone@" + "hotmail.fr\n"
    )
    r = _run(repo, env, tmp_path / "out")
    assert r.returncode != 0
    assert "n.txt:1:" in r.stderr and "n.txt:2:" in r.stderr


def test_grep_error_fails_the_check(source, tmp_path):
    repo, env = source
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "grep").write_text("#!/bin/sh\nexit 2\n")
    (shim / "grep").chmod(0o755)
    env = {**env, "PATH": f"{shim}{os.pathsep}{env['PATH']}"}
    target = tmp_path / "out"
    r = _run(repo, env, target)
    assert r.returncode != 0 and "grep error" in r.stderr
    assert not (target / ".git").exists()


def test_the_kit_passes_its_own_privacy_check(source, tmp_path):
    repo, env = source
    root = Path(__file__).resolve().parent.parent
    for rel in ("tests/test_make_public_repo.py", "docs/going-public.md"):
        _commit_file(repo, env, rel, (root / rel).read_text())
    r = _run(repo, env, tmp_path / "out")
    assert r.returncode == 0, r.stderr
