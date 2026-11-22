# Automatic Releases Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every push to `main` that changes shipped code becomes a SemVer-numbered, smoke-tested GitHub release, and sticks notice it within hours and show an Update button.

**Architecture:** A stdlib-only `tools/next_version.py` decides whether and how to bump from conventional commits since the last `vX.Y.Z` tag, and rewrites the version strings. A new `auto-release.yml` runs after CI succeeds on `main`, makes the bump commit in the runner only, builds and smoke-tests the stick from it (`stick.yml` gains a bundle input), and only then pushes the commit and tag and publishes the release. On the app side, `AppUpdates` checks at every start and every 6 h, and the offer banner gains an Update button.

**Tech Stack:** Python 3.12 stdlib, GitHub Actions, pytest, Jinja2.

**Spec:** `docs/superpowers/specs/2026-11-22-auto-release-design.md`

## Global Constraints

- Only noreply identities in commits and tags. Bot: `github-actions[bot]` <`41898282+github-actions[bot]@users.noreply.github.com`>.
- Bump commits use the subject `chore(release): vX.Y.Z`. Tags are annotated `vX.Y.Z`, and the tag message is the release notes.
- Shipped paths: `src/`, `stick/`, `tools/build_stick.py`, `tools/package_release.py`, `pyproject.toml`, `uv.lock`.
- Bump rules: `!` after the type or a `BREAKING CHANGE:` footer gives minor while the major is 0 (major from 1.0); any `feat` gives minor; anything else gives patch.
- No tag yet: base `0.1.0`, computed from all history (the first release is `0.2.0`).
- The version lives in three places, always equal: `pyproject.toml` `[project] version`, `src/ytop/__init__.py` `__version__`, and the `ytop` package entry in `uv.lock`.
- `CHECK_EVERY = timedelta(hours=6)`.
- No GitHub-side PR merges; the owner's branch is merged locally and pushed.
- Run pytest with `TMPDIR=$PWD/.tools/tmp` and deselect `tests/test_components.py::test_interrupted_prune_never_leaves_a_partial_folder_under_its_name` (fails on this host only).

## Review Focus

1. **Two pushes land minutes apart.** The run for the older push must not release; the newer run releases both. The plan job skips when `head_sha` is no longer `origin/main`, and the publish push is a non-fast-forward rejection otherwise (Task 3, Step 1).
2. **A merge commit's body lists the branch's commits.** Merges are skipped and their bodies ignored, so nothing is counted twice and a merge body containing `BREAKING CHANGE` never bumps (Task 1 test `test_merge_commits_are_ignored`).
3. **A docs/test-only push after a release** must not release, even though its commits are `docs:`/`test:` (Task 1 test `test_no_release_without_shipped_changes`).
4. **A failed publish re-run.** If the push already landed, a re-run must not push again or fail; it publishes the release for the existing tag (Task 3, Step 1, publish job).
5. **A stick started offline with a kept offer.** It still shows the kept offer, and the offer from a check older than 6 h is dropped (Task 4 test `test_a_kept_offer_shows_at_start_and_is_rechecked`).

---

### Task 1: `tools/next_version.py` — decide, number, note, write

**Files:**
- Create: `tools/next_version.py`
- Test: `tests/test_next_version.py`. Import the tool the way `tests/test_package_release.py` does (`importlib.util.spec_from_file_location`). Build throwaway git repos in `tmp_path` with `git init`, local `user.name`/`user.email` set to `t`/`t@example.invalid`, and commits made through `subprocess`.

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Commit: sha: str; subject: str; body: str`
  - `def commit_type(subject: str) -> str`: the lowercase conventional type, or `""`.
  - `def is_breaking(commit: Commit) -> bool`
  - `def bump_level(commits: list[Commit]) -> str | None`: `"major" | "minor" | "patch"`, or `None` for no commits.
  - `def bump(version: str, level: str) -> str`: applies the 0.x rule (major becomes minor while the major is 0).
  - `def ships(paths: list[str]) -> bool`
  - `def release_notes(version: str, commits: list[Commit]) -> str`
  - `def plan(repo: Path) -> Plan` with `@dataclass(frozen=True) class Plan: release: bool; version: str; notes: str; reason: str`
  - `def write_version(repo: Path, version: str) -> None`: rewrites all three places. Raises `SystemExit` with a message if any of the three patterns is missing.
  - CLI:
    - `python tools/next_version.py plan [--repo .] [--github-output PATH] [--notes-file PATH]` prints `release=…` and `version=…`; when `--github-output` is given it appends the same lines to that file and writes the notes to `--notes-file`. Exit code 0 either way.
    - `python tools/next_version.py write X.Y.Z [--repo .]`.

- [ ] **Step 1: Write the failing tests** in `tests/test_next_version.py`:
  - `test_bump_rules`:
    - `bump("0.1.0","minor")=="0.2.0"`, `bump("0.2.3","patch")=="0.2.4"`, `bump("0.2.3","major")=="0.3.0"`, `bump("1.4.2","major")=="2.0.0"`.
    - `bump_level` of `[feat: x, fix: y]` is `"minor"`; of `[fix: y, perf: z]` is `"patch"`; of `[refactor(web): z]` is `"patch"`; of `[Update readme]` (untyped) is `"patch"`; of `[feat!: x]` is `"major"`; of `[fix: y]` with body `"BREAKING CHANGE: old flag gone"` is `"major"`; of `[]` is `None`.
  - `test_ships_only_for_shipped_paths`: `ships(["docs/x.md","tests/t.py",".github/workflows/ci.yml","tools/smoke_stick.py"]) is False`; `ships(["src/ytop/app.py"])`, `ships(["uv.lock"])` and `ships(["tools/build_stick.py"])` are each `True`.
  - `test_first_release_from_untagged_history`: a repo with `pyproject.toml` at version `0.1.0` and commits `feat: a` (touching `src/a.py`) and `fix: b`. `plan()` gives `release=True`, `version=="0.2.0"`, and notes containing `"## Features"`, `"a ("` and `"## Fixes"`.
  - `test_counts_only_commits_since_the_last_tag`: after `git tag -a v0.2.0 -m n` add `fix: c` touching `src/c.py`; `plan().version == "0.2.1"`, and the notes contain `c` but not `a`.
  - `test_no_release_without_shipped_changes`: after the tag, add `docs: d` touching `README.md` and `test: e` touching `tests/e.py`; `plan().release is False` and `"no shipped" in plan().reason`.
  - `test_merge_commits_are_ignored`: a branch with `fix: f` (touching `src/f.py`) merged with `--no-ff` and the merge message `"Merge branch 'x'\n\nBREAKING CHANGE: not really"`; `plan().version == "0.2.1"`.
  - `test_write_version_updates_all_three`: copy the real `pyproject.toml`, `src/ytop/__init__.py` and `uv.lock` into `tmp_path`, run `write_version(tmp_path,"9.8.7")`, then assert `tomllib` reads `9.8.7`, `__init__.py` contains `__version__ = "9.8.7"`, and the `uv.lock` block whose `name = "ytop"` has `version = "9.8.7"` while no other package's version changed (compare the parsed lock before and after, excluding `ytop`).
  - `test_github_output_lines`: run the CLI `plan --github-output out.txt --notes-file notes.md` and check that `out.txt` contains `release=true` and `version=0.2.0`, and that `notes.md` is non-empty.

- [ ] **Step 2: Run them to verify they fail**
  Run: `TMPDIR=$PWD/.tools/tmp uv run pytest -q tests/test_next_version.py`
  Expected: errors, because `tools/next_version.py` does not exist.

- [ ] **Step 3: Implement `tools/next_version.py`**
  - The last tag: `git describe --tags --abbrev=0 --match "v[0-9]*.[0-9]*.[0-9]*"`. A failure means none: base is the version in `pyproject.toml`, and the range is all of `HEAD`.
  - Commits: `git log --no-merges --format=%H%x1f%s%x1f%b%x1e <range>`.
  - Changed paths: `git diff --name-only <tag>..HEAD`. With no tag, every tracked file counts.
  - Notes: a first line `YTOP vX.Y.Z`, then the sections `## Features` (feat), `## Fixes` (fix), `## Performance` (perf) and `## Other` (the rest, including `docs`/`test` commits in a shipped push). Each line is `- <subject without the type prefix> (<short sha>)`. Breaking commits get a `**Breaking:**` prefix.
  - `write_version`: replace `version = "…"` under `[project]` in `pyproject.toml` (first match only), the `__version__ = "…"` line, and the `version = "…"` line directly after `name = "ytop"` in `uv.lock`, each via an anchored regex with `count=1`.

- [ ] **Step 4: Run the tests to verify they pass**
  Run: `TMPDIR=$PWD/.tools/tmp uv run pytest -q tests/test_next_version.py && uv run ruff check tools tests`
  Expected: all pass, and ruff reports no errors.

- [ ] **Step 5: Commit**
  `git add tools/next_version.py tests/test_next_version.py && git commit -m "build: next_version.py decides and writes the next SemVer from commits"`

---

### Task 2: Tests and the stick workflow stop assuming version 0.1.0; `stick.yml` builds from a bump bundle

**Files:**
- Modify: `tests/test_ci_fake_release.py:92,102,179`, `tests/test_build_stick.py:96`
- Modify: `.github/workflows/stick.yml` (the `on.workflow_call.inputs`, `concurrency`, the `build` job's checkout, and the `self-update-fat32` version assertion at line 211)

**Interfaces:**
- Produces: `stick.yml` `workflow_call` input `bump_artifact: string, default ''`. When set, the `build` job downloads that artifact (it holds `bump.bundle`), fetches its single commit and builds from it detached. The `stick` artifact is unchanged.

- [ ] **Step 1: Replace the literals.**
  - `test_build_stick.py:96`: compare to `ytop.__version__`.
  - `test_ci_fake_release.py`: `doc_a["version"] == ytop.__version__` and `doc_b["version"] == bump(ytop.__version__)`, using `bump` from the tool module the test already loads. Lines 102 and 179 use the same expression.
  - `stick.yml:211`: compare against `jq -r .version serve/test/ytop/*/ytop-release.json` instead of `"0.1.1"`.

- [ ] **Step 2: Prove the tests are version-agnostic.** Temporarily run `uv run python tools/next_version.py write 0.7.3`, run `TMPDIR=$PWD/.tools/tmp uv run pytest -q tests/test_ci_fake_release.py tests/test_build_stick.py tests/test_package_release.py`, then `git checkout -- pyproject.toml src/ytop/__init__.py uv.lock`.
  Expected: all pass at 0.7.3 (any test that is skipped for lacking a built image stays skipped; say which).

- [ ] **Step 3: Add the bundle input to `stick.yml`.**
  - The input is as described above.
  - The `build` job: after `actions/checkout@v4`, run `actions/download-artifact@v4` with `name: ${{ inputs.bump_artifact }}`, then `git fetch ./bump.bundle 'refs/heads/*:refs/remotes/bump/*' && git checkout --detach bump/release`, all under `if: inputs.bump_artifact != ''`.
  - Concurrency: `group: stick-${{ github.ref }}-${{ inputs.bump_artifact || 'plain' }}`, so a manual run never cancels a release build.
  - The smoke jobs keep checking out the triggering ref (they only need `tools/`).

- [ ] **Step 4: Lint the workflow**
  Run: `uvx --from actionlint-py actionlint .github/workflows/stick.yml`
  Expected: no output.

- [ ] **Step 5: Commit**
  `git commit -am "test: version checks follow the project version; stick.yml can build a bump bundle"`

---

### Task 3: `auto-release.yml`, and the README's Releasing section

**Files:**
- Create: `.github/workflows/auto-release.yml`
- Modify: `README.md` "## Releasing" (lines 190–210)
- Test: `tests/test_workflows.py` (new). PyYAML is not a dependency and must not be added: the test reads the file as text and checks it per job, splitting on the top-level `  <job>:` keys under `jobs:`. Syntax is actionlint's job.

**Interfaces:**
- Consumes: Task 1's CLI (`plan`, `write`) and Task 2's `bump_artifact` input.

- [ ] **Step 1: Write `auto-release.yml`**
  - Triggers:
    - `workflow_run: {workflows: [CI], types: [completed], branches: [main]}`
    - `workflow_dispatch: {inputs: {dry_run: {type: boolean, default: true}}}`
  - `concurrency: {group: auto-release, cancel-in-progress: false}`; top-level `permissions: contents: read`.
  - `plan` job:
    - Runs if `workflow_dispatch`, or if `workflow_run.conclusion == 'success'`, `workflow_run.event == 'push'`, and `!startsWith(workflow_run.head_commit.message, 'chore(release):')`.
    - Check out the SHA (`workflow_run.head_sha` or `github.sha`) with `fetch-depth: 0`.
    - Skip with `release=false` when `git rev-parse origin/main` differs from that SHA; the reason is "superseded".
    - Otherwise run `tools/next_version.py plan --github-output "$GITHUB_OUTPUT" --notes-file notes.md`.
    - Outputs: `release`, `version`, `sha`.
  - `bump` job (needs plan; if `release == 'true'`):
    - Check out the SHA and run `next_version.py write`.
    - Commit as the bot with subject `chore(release): vX.Y.Z` and body = the notes, on a local branch `release`.
    - `git bundle create bump.bundle release ^<sha>`.
    - Upload the `bump` artifact containing `bump.bundle` and `notes.md`.
  - `stick` job: `uses: ./.github/workflows/stick.yml` with `bump_artifact: bump` and `secrets: inherit`.
  - `publish` job:
    - Needs plan, bump and stick; `permissions: contents: write`.
    - Fetch the bundle and the stick artifact, and run `tools/package_release.py dist/stick/.ytop release`.
    - Create the tag: `git tag -a vX.Y.Z -F notes.md bump/release`.
    - If `dry_run` is set: print the versions and `ls release/`, then stop.
    - If the remote already has the tag at this commit: skip the push.
    - Otherwise: `git push --atomic origin bump/release:refs/heads/main refs/tags/vX.Y.Z`. A rejection fails the job with a message saying `main` moved and the next CI run will release.
    - Then `gh release create vX.Y.Z release/* --title "YTOP vX.Y.Z" --notes-file notes.md --verify-tag`.
    - The bot identity is set with `git config user.name/user.email` in the job (the Global Constraints values).
  - Leave `release.yml` as is (it is the hand-tag path).

- [ ] **Step 2: Write `tests/test_workflows.py`**
  - `test_auto_release_pushes_only_after_the_stick_passes`: `publish.needs ⊇ {plan, bump, stick}`; only `publish` has `contents: write`; the `stick` job uses `./.github/workflows/stick.yml` with `bump_artifact: bump`.
  - `test_auto_release_skips_its_own_commits`: the `plan` job's `if` contains `chore(release):`.
  - `test_bot_identity_is_noreply`: every `user.email` in the file ends with `@users.noreply.github.com`.

- [ ] **Step 3: Run the tests and lint**
  Run: `TMPDIR=$PWD/.tools/tmp uv run pytest -q tests/test_workflows.py && uvx --from actionlint-py actionlint .github/workflows/auto-release.yml .github/workflows/stick.yml`
  Expected: pass, with no actionlint output.

- [ ] **Step 4: Rewrite README "## Releasing".**
  - Releases are automatic: every push to `main` that changes shipped files releases after CI passes. The bump rules and the shipped paths are as in Global Constraints.
  - The bot commits `chore(release): vX.Y.Z` to `main`, so pull before the next push.
  - Hand-pushed tags still work through `release.yml`.
  - A dry run: dispatch **Auto release** with `dry_run`.

- [ ] **Step 5: Commit**
  `git add .github/workflows/auto-release.yml tests/test_workflows.py README.md && git commit -m "build: release automatically after CI passes on main"`

---

### Task 4: Sticks check at every start and every 6 h; the banner gets an Update button

**Files:**
- Modify: `src/ytop/appupdate.py` (`CHECK_EVERY` line 88; `AppUpdates.on_start_outcome` 983–1007; new `recheck_if_due`; the module docstring's "Checking" paragraph)
- Modify: `src/ytop/web/app.py` (lifespan around line 505: start the re-check thread)
- Modify: `src/ytop/web/templates/base.html` (the `app_offer` banner)
- Test: `tests/test_appupdate.py`

**Interfaces:**
- Produces:
  - `AppUpdates.recheck_if_due(self) -> None`: checks only when available, `check_app is True`, not busy and `_check_due()`. Never raises.
  - `RECHECK_POLL_S = 600` in `ytop/web/app.py`.

- [ ] **Step 1: Write the failing tests**
  - Replace `test_start_up_app_check_runs_once_a_day` with `test_start_up_app_check_runs_at_every_start`. With FakeUpdater and the clock from the old test: two `on_start_outcome("up to date")` calls 5 h apart make `fake.checks == 2`; `"offline"` adds none; `"off"` adds one.
  - `test_recheck_runs_every_six_hours`:
    - After a start check at T, `recheck_if_due()` at T+5h leaves `fake.checks` unchanged, and at T+6h01m increments it.
    - With `check_app` set to False it never checks.
    - While `updates.phase = "installing"` it never checks.
  - Rewrite `test_an_offer_is_kept_between_starts_for_a_day` as `test_a_kept_offer_shows_at_start_and_is_rechecked`, against the existing fake server:
    - A start 5 h later shows the kept offer and also asks GitHub (`_latest_requests == 2`).
    - A start with outcome `"offline"` 1 h after that still shows the kept offer.
    - At +7 h after the last check, an `"offline"` start shows no offer (the kept one is too old).
    - The other-build and "Check now forgets it" assertions stay.
  - In the existing banner test (around line 718), also assert that `'href="/app-update"'` and `">Update<"` are in the page.

- [ ] **Step 2: Run them to verify they fail**
  Run: `TMPDIR=$PWD/.tools/tmp uv run pytest -q tests/test_appupdate.py -k "every_start or six_hours or rechecked or answer"`
  Expected: FAIL (the start check is gated daily, `recheck_if_due` doesn't exist, and the button is missing).

- [ ] **Step 3: Implement**
  - `CHECK_EVERY = timedelta(hours=6)`.
  - `on_start_outcome`, after the existing consent/online gates:
    - When online or `"off"`: call `self._restore_offer()` (shows a kept offer at once), then `self.check()` unconditionally.
    - When offline: only `_restore_offer()`.
    - Record `last_check` (a new private `_record_check(result)`, extracted from today's code) for any status other than `"offline"`/`"error"`.
  - `recheck_if_due` reuses `_check_due` and `_record_check`.
  - Rewrite the "Checking" paragraph of the module docstring to match.
  - `app.py`: start a daemon thread `ytop-app-recheck` beside `start_check_thread`, looping `while not idle_stop.wait(RECHECK_POLL_S): releases.recheck_if_due()` inside `try/except Exception: log.exception(...)`.
  - `base.html`: inside the `app_offer` banner, replace the "See what's new" link with `<a href="/app-update" class="btn tiny primary">Update</a>` after the sentence `{{ product_name }} {{ app_offer.version }} is available.`

- [ ] **Step 4: Run the app-update and web tests**
  Run: `TMPDIR=$PWD/.tools/tmp uv run pytest -q tests/test_appupdate.py tests/test_settings_web.py tests/test_web.py && uv run ruff check src tests`
  Expected: all pass, with no lint errors.

- [ ] **Step 5: Commit**
  `git commit -am "feat: sticks look for releases at every start and every 6 hours; Update button on the banner"`

---

### Task 5: Full suite, merge, push, and confirm the first release

**Files:** none new.

- [ ] **Step 1: Full suite**
  Run: `TMPDIR=$PWD/.tools/tmp uv run pytest -q -p no:cacheprovider --deselect tests/test_components.py::test_interrupted_prune_never_leaves_a_partial_folder_under_its_name` (in the background; about 11 min).
  Expected: all pass. Also run `uv run mypy` and compare with the error count on `main`: no new errors.

- [ ] **Step 2: Merge locally and push** (the owner approved merging this plan's work as part of "go ahead"; confirm once before pushing if anything in Steps 1–2 surprised you).
  `git checkout main && git pull --ff-only && git merge --no-ff feat/auto-release && git push origin main`

- [ ] **Step 3: Watch the release**
  - Run: `gh run watch` on CI, then on the **Auto release** run it triggers.
  - Expected: `plan` gives `version=0.2.0`; `stick` and `publish` succeed; `gh release view v0.2.0` lists `ytop-release.json`, `SHA256SUMS` and the piece archives; `main` gains `chore(release): v0.2.0`.
  - If anything fails, report the failing job's log; nothing is tagged before `publish`.

- [ ] **Step 4: Pull the bot commit**
  Run: `git pull --ff-only && git log --oneline -2`.
  Expected: `chore(release): v0.2.0` on top, and `src/ytop/__init__.py` says `0.2.0`.
