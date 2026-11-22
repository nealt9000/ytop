# Automatic releases on every code push

Date: 2026-11-22. Status: draft for owner approval.

## Why

The owner wants to see at a glance which version a stick runs and whether
it is behind the repo, and wants sticks to offer the update themselves.
Today that cannot work: the version has been `0.1.0` since the start and no
app release has ever been published, so the update check (which reads
GitHub Releases and offers only a *newer* version) never finds anything.
The footer version and the Settings page already exist on `main`; the
owner's stick (build `1c13615`, 2026-11-20 10:42) predates them by three
minutes.

## Goals

1. Every push to `main` that changes shipped code gets a new version and a
   published GitHub release, without anyone tagging by hand.
2. Versions follow SemVer, derived from the conventional-commit prefixes
   the repo already uses.
3. A stick learns of a new release soon after it is published (at start,
   and periodically while open), and shows a banner with an Update button.
4. The footer shows the running version (and build) — already on `main`.

## Non-goals

- Pre-releases, release channels or picking which versions ship.
- Pruning old releases or their assets.
- Changing how an update installs (incremental pieces, journalled switch,
  confirm first, Revert) — that machinery stays as is.
- Installing without the user's confirmation.

## Anti-goals

- A release that skipped the tests or the stick smoke tests.
- Any commit or tag on the public repo carrying a non-noreply email.
- Two releases racing and producing the same or out-of-order versions.

## Approaches considered

**When to release** (owner chose): *every code push releases* — over
"every push gets a number, the owner picks what ships", which keeps
"latest on the repo" and "latest offered" apart.

**Where the version lives** (Claude's call; the owner handed over the how):

- **A. CI writes the bump back (chosen).** After CI passes on a push to
  `main`, a job computes the next version, commits `pyproject.toml` +
  `src/ytop/__init__.py` as `chore(release): vX.Y.Z` (author
  `github-actions[bot]`, a noreply address), creates an annotated tag
  whose message is the release notes, pushes both, then runs the existing
  release pipeline on that tag. Everything that reads the version today
  (`build_stick.py`, `check-tag`, `ci_fake_release.py`, the footer) keeps
  working unchanged, and the repo files always say what the latest release
  is. Cost: `main` gains one bot commit per release, so a local checkout
  must pull before its next push (the existing merge routine already does
  `git pull --ff-only` first).
- B. Version only in tags, injected at build time (setuptools-scm style).
  No bot commits, but every reader of `pyproject.toml`/`__version__` must
  change, local builds need `git describe` logic, and the source tree no
  longer says its own version. More moving parts for the same result.
- C. Bump locally before each push (a script or hook). Easy to forget or
  bypass, so "every push" would not reliably hold.

## Design

### Release workflow (`.github/workflows/auto-release.yml`, new)

- Trigger: `push` to `main`. `concurrency: auto-release` with
  `cancel-in-progress: false`, so releases run one at a time in order.
- Skip when the head commit is a `chore(release):` commit (the bot's own
  push) so it does not loop. (Pushes made with `GITHUB_TOKEN` do not start
  workflows anyway; the check is a second guard.)
- Job `test`: calls `ci.yml` as a reusable workflow (add `workflow_call`
  to it). A failure stops the release.
- Job `version`: runs `tools/next_version.py` (new, stdlib only), which
  - finds the latest `vX.Y.Z` tag (none yet → base `0.1.0`, treated as
    unreleased so the first release is computed from all history);
  - lists commits since that tag (`git log <tag>..HEAD`, merges skipped,
    merge-branch bodies ignored, subjects parsed);
  - decides whether to release: only when the range changes shipped files
    — `src/`, `stick/`, `tools/build_stick.py`, `tools/package_release.py`,
    `pyproject.toml`, `uv.lock` (docs-, tests- or CI-only pushes do not
    release);
  - picks the bump: any `!` after the type or a `BREAKING CHANGE:` footer
    → minor while the major version is 0 (major from 1.0 on); any `feat`
    → minor; otherwise patch (`fix`, `perf`, `refactor`, `build`, untyped
    subjects…);
  - writes release notes: commit subjects grouped as Features, Fixes,
    Performance and Other, with short SHAs;
  - prints `release=true|false`, `version=X.Y.Z` and the notes file path
    for the next step.
- **Nothing is pushed until the release is proven** (Claude's call, from
  the builder check): a version tag must never exist without its release,
  or the repo files would stop saying what the latest release is. So:
  - Job `bump` (needs `test` and `version`, only when `release=true`):
    rewrites the two version strings and commits as `github-actions[bot]`
    *in the runner only*, then hands the commit on (as a bundle artifact or
    by building in the same job — builder's choice).
  - Job `stick`: builds the image from that bump commit and runs the
    existing smoke tests (`stick.yml`, given a ref/artifact input).
  - Job `publish` (needs `stick`; `contents: write`): pushes the bump
    commit to `main` and the annotated tag `vX.Y.Z` (message = the notes),
    then packages and publishes the release from the image `stick` built
    (`tools/package_release.py`, `gh release create --verify-tag`). If
    `main` moved meanwhile, the push is rejected before any tag exists and
    the job fails; the next push's run releases everything since the last
    tag.
  - Any failure before `publish` leaves no commit, tag or release; a
    re-run of the workflow retries the same version.
- `release.yml` keeps its `push: tags` trigger for a hand-pushed tag; the
  packaging steps it shares with `publish` may move into a reusable
  workflow or stay duplicated (builder's choice). The pieces that assume
  `github.ref` is the tag (`check-tag`, the notes step, the checkout ref)
  take the tag/ref as an input when called.
- Repo settings checked 2026-11-22: `main` has no branch protection and
  the repo has no rulesets; the default token is read-only, so jobs that
  push declare `permissions: contents: write`. No PAT or secret needed,
  and since `GITHUB_TOKEN` pushes start no workflows, the bump commit does
  not re-trigger the release (the `chore(release):` skip stays as a guard).

### Sticks notice sooner (`src/ytop/appupdate.py`)

- `CHECK_EVERY` drops from 24 h to 6 h, and a check also runs at every
  start (not just when the cached result is older than `CHECK_EVERY`).
  Cached offers are refreshed by each check instead of being shown for a
  day. While the app stays open, a background re-check runs every
  `CHECK_EVERY`. Each check is one or two API calls plus two small asset
  downloads — far below GitHub's 60 unauthenticated calls per hour.
- Unchanged: checks only on sticks that answered Yes to "Check for YTOP
  updates on this stick?"; offers need a newer version, a different build
  and every platform the stick carries.

### Banner and footer (`base.html`)

- The existing offer banner gains an **Update** button that goes to
  `/app-update`, where the notes and the install confirmation already
  are. No one-click install from the banner: installing stops downloads
  and restarts YTOP, so it stays behind the confirm page.
- The footer already shows `YTOP <version> (build <id>)`; no change.

## Constraints

- Public repo: only noreply identities in commits and tags (the bot's is
  `41898282+github-actions[bot]@users.noreply.github.com`). No GitHub-side
  PR merges.
- The stick build runs on `ubuntu-latest` for all five platforms (~8 min
  for the release workflow); CI is ~5 min.
- The first automatic release will be `0.2.0` (history contains `feat`
  commits). The owner's current stick (`0.1.0`, opted in) should be
  offered it on its next start; its update code predates this change but
  already reads GitHub Releases.

## Left to the builder

- Exact YAML structure, job names and how outputs pass between jobs.
- The notes format beyond the grouping above.
- Tests: `tools/next_version.py` gets unit tests (bump rules, shipped-file
  filter, no-tag base, merge handling); the app-side cadence change and
  banner button get tests alongside the existing app-update tests.
