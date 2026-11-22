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


STICK = REPO / ".github" / "workflows" / "stick.yml"


def test_bump_artifact_is_downloaded_outside_the_checkout():
    # An untracked bump.bundle in the workspace makes git_revision() "-dirty".
    text = STICK.read_text()
    m = re.search(
        r"uses: actions/download-artifact@v4\s*\n\s+with:\s*\n"
        r"\s+name: \$\{\{ inputs\.bump_artifact \}\}\s*\n\s+path: (.+)",
        text,
    )
    assert m, "the bump artifact download has no path:"
    assert m.group(1).strip().startswith("${{ runner.temp }}/"), m.group(1)
    assert "git fetch ./bump.bundle" not in text
    assert '"$RUNNER_TEMP/bump/bump.bundle"' in text


def test_publish_refuses_a_dirty_or_mismatched_build_id():
    publish = _jobs(AUTO)["publish"]
    guard = publish.find("dist/stick/.ytop/build.json")
    assert guard >= 0, "publish does not check the build id"
    assert "jq -r .build_id" in publish
    assert "git rev-parse --short bump/release" in publish
    assert "dirty" in publish
    assert guard < publish.index('git tag -a "$TAG"')


def test_dry_runs_do_not_share_the_real_release_concurrency_group():
    text = AUTO.read_text()
    m = re.search(r"^concurrency:\s*\n\s+group: (.+)$", text, re.MULTILINE)
    assert m
    group = m.group(1)
    assert "inputs.dry_run" in group and "'dry'" in group and "'real'" in group
    assert "cancel-in-progress: false" in text
