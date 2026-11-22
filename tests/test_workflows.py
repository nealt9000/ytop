"""Text checks on the release workflows (no PyYAML: split on the job keys).

actionlint checks the syntax; these pin the safety properties a refactor could
silently lose.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
AUTO = REPO / ".github" / "workflows" / "auto-release.yml"


def _jobs(path: Path) -> dict[str, str]:
    """Each top-level job's text, keyed by name (the `  <job>:` lines under `jobs:`)."""
    text = path.read_text()
    _, sep, body = text.partition("\njobs:\n")
    assert sep, f"{path.name} has no top-level jobs:"
    jobs: dict[str, str] = {}
    name = None
    for line in body.splitlines():
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            name = m.group(1)
            jobs[name] = ""
        elif re.match(r"^\S", line):
            break  # the next top-level key ends jobs:
        elif name:
            jobs[name] += line + "\n"
    return jobs


def _needs(job: str) -> set[str]:
    m = re.search(r"^    needs:\s*\[([^\]]*)\]", job, re.MULTILINE)
    if m:
        return {n.strip() for n in m.group(1).split(",") if n.strip()}
    m = re.search(r"^    needs:\s*(\S+)\s*$", job, re.MULTILINE)
    return {m.group(1)} if m else set()


def _writes_contents(job: str) -> bool:
    return re.search(r"^\s+contents:\s*write\b", job, re.MULTILINE) is not None


def test_auto_release_pushes_only_after_the_stick_passes():
    jobs = _jobs(AUTO)
    assert {"plan", "bump", "stick", "publish"} <= jobs.keys()
    assert _needs(jobs["publish"]) >= {"plan", "bump", "stick"}
    assert [n for n, j in jobs.items() if _writes_contents(j)] == ["publish"]
    top = AUTO.read_text().partition("\njobs:\n")[0]
    assert not _writes_contents(top)
    stick = jobs["stick"]
    assert re.search(r"^    uses: \./\.github/workflows/stick\.yml\s*$", stick, re.M)
    assert re.search(r"^      bump_artifact: bump\s*$", stick, re.M)
    # Only publish pushes, and it does so once.
    pushes = [n for n, j in jobs.items() if "git push" in j]
    assert pushes == ["publish"]


def test_auto_release_skips_its_own_commits():
    plan = _jobs(AUTO)["plan"]
    cond = re.search(r"^    if:(.*?)^    \S", plan, re.MULTILINE | re.DOTALL)
    assert cond, "plan job has no if:"
    assert "chore(release):" in cond.group(1)


def test_bot_identity_is_noreply():
    emails = re.findall(r"user\.email\s+['\"]?([^'\"\s]+)", AUTO.read_text())
    assert emails, "auto-release.yml sets no user.email"
    for email in emails:
        assert email.endswith("@users.noreply.github.com"), email


def test_publish_rerun_reuses_a_tag_that_already_landed():
    # checkout (fetch-depth 0) brings the pushed tag back on a re-run; the tag
    # step must accept it at the bump commit instead of failing on `git tag`.
    publish = _jobs(AUTO)["publish"]
    tag_step = publish.index('git tag -a "$TAG"')
    guard = publish.find('git rev-parse -q --verify "refs/tags/$TAG"')
    assert 0 <= guard < tag_step
    assert 'git ls-remote origin "refs/tags/$TAG^{}"' in publish
