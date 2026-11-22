"""Decide whether a push releases, and number and describe the release.

Reads conventional-commit subjects since the last vX.Y.Z tag, computes the
next SemVer, writes release notes, and rewrites the three version strings.
Stdlib only.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

SHIPPED = (
    "src/",
    "stick/",
    "tools/build_stick.py",
    "tools/package_release.py",
    "pyproject.toml",
    "uv.lock",
)
TAG_GLOB = "v[0-9]*.[0-9]*.[0-9]*"
_TYPE = re.compile(r"^(\w+)(?:\([^)]*\))?(!)?:\s")
_BREAKING_FOOTER = re.compile(r"^BREAKING[ -]CHANGE:", re.MULTILINE)
_SECTIONS = (
    ("feat", "Features"),
    ("fix", "Fixes"),
    ("perf", "Performance"),
)


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str
    body: str


@dataclass(frozen=True)
class Plan:
    release: bool
    version: str
    notes: str
    reason: str


def commit_type(subject: str) -> str:
    m = _TYPE.match(subject)
    return m.group(1).lower() if m else ""


def is_breaking(commit: Commit) -> bool:
    m = _TYPE.match(commit.subject)
    return bool(m and m.group(2)) or bool(_BREAKING_FOOTER.search(commit.body))


def bump_level(commits: list[Commit]) -> str | None:
    if not commits:
        return None
    if any(is_breaking(c) for c in commits):
        return "major"
    if any(commit_type(c.subject) == "feat" for c in commits):
        return "minor"
    return "patch"


def bump(version: str, level: str) -> str:
    major, minor, patch = (int(p) for p in version.split("."))
    if level == "major" and major == 0:
        level = "minor"
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def ships(paths: list[str]) -> bool:
    return any(p.startswith(SHIPPED) for p in paths)


def _title(subject: str) -> str:
    return _TYPE.sub("", subject, count=1)


def release_notes(version: str, commits: list[Commit]) -> str:
    groups: dict[str, list[str]] = {title: [] for _, title in _SECTIONS}
    groups["Other"] = []
    by_type = dict(_SECTIONS)
    for c in commits:
        title = by_type.get(commit_type(c.subject), "Other")
        mark = "**Breaking:** " if is_breaking(c) else ""
        groups[title].append(f"- {mark}{_title(c.subject)} ({c.sha[:7]})")
    lines = [f"YTOP v{version}"]
    for title, items in groups.items():
        if items:
            lines += ["", f"## {title}", *items]
    return "\n".join(lines) + "\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _last_tag(repo: Path) -> str | None:
    try:
        return _git(
            repo, "describe", "--tags", "--abbrev=0", "--match", TAG_GLOB
        ).strip()
    except subprocess.CalledProcessError:
        return None


def _commits(repo: Path, rev_range: str) -> list[Commit]:
    out = _git(repo, "log", "--no-merges", "--format=%H%x1f%s%x1f%b%x1e", rev_range)
    commits = []
    for rec in out.split("\x1e"):
        if rec.strip():
            sha, subject, body = rec.lstrip("\n").split("\x1f", 2)
            commits.append(Commit(sha, subject, body.strip()))
    return commits


def plan(repo: Path) -> Plan:
    tag = _last_tag(repo)
    if tag:
        base = tag[1:]
        commits = _commits(repo, f"{tag}..HEAD")
        paths = _git(repo, "diff", "--name-only", f"{tag}..HEAD").splitlines()
    else:
        pyproject = tomllib.loads((repo / "pyproject.toml").read_text())
        base = pyproject["project"]["version"]
        commits = _commits(repo, "HEAD")
        paths = _git(repo, "ls-files").splitlines()
    if not commits:
        return Plan(False, base, "", f"no commits since {tag or 'the start'}")
    if not ships(paths):
        return Plan(False, base, "", "no shipped files changed")
    level = bump_level(commits)
    assert level is not None
    version = bump(base, level)
    return Plan(
        True, version, release_notes(version, commits), f"{level} bump from {base}"
    )


def _sub(text: str, pattern: str, version: str, what: str) -> str:
    new, n = re.subn(
        pattern, rf"\g<1>{version}\2", text, count=1, flags=re.MULTILINE | re.DOTALL
    )
    if n != 1:
        raise SystemExit(f"next_version: could not find the version in {what}")
    return new


_VERSION_PATTERNS = (
    ("pyproject.toml", r'(\[project\](?:(?!^\[).)*?^version = ")[^"]*(")'),
    ("src/ytop/__init__.py", r'(^__version__ = ")[^"]*(")'),
    ("uv.lock", r'(^name = "ytop"\nversion = ")[^"]*(")'),
)


def write_version(repo: Path, version: str) -> None:
    # Compute all three first so a missing pattern leaves every file untouched.
    texts = {
        rel: _sub((repo / rel).read_text(), pattern, version, rel)
        for rel, pattern in _VERSION_PATTERNS
    }
    for rel, text in texts.items():
        (repo / rel).write_text(text)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--repo", type=Path, default=Path("."))
    p.add_argument("--github-output", type=Path)
    p.add_argument("--notes-file", type=Path)
    w = sub.add_parser("write")
    w.add_argument("version")
    w.add_argument("--repo", type=Path, default=Path("."))
    args = ap.parse_args()
    if args.cmd == "write":
        write_version(args.repo, args.version)
        return
    result = plan(args.repo)
    lines = [
        f"release={'true' if result.release else 'false'}",
        f"version={result.version}",
    ]
    print("\n".join(lines))
    print(f"# {result.reason}")
    if args.github_output:
        with args.github_output.open("a") as f:
            f.write("\n".join(lines) + "\n")
    if args.notes_file:
        args.notes_file.write_text(result.notes)


if __name__ == "__main__":
    main()
