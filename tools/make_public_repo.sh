#!/usr/bin/env bash
# Prepare a fresh, one-commit public copy of YTOP. Never contacts GitHub.
#
# usage: tools/make_public_repo.sh <new-repo-dir> --name "<name>" --email "<noreply email>" [--ref <ref>]
#
# --ref defaults to master. The new repo's branch is "main".
set -euo pipefail

usage() {
  echo 'usage: make_public_repo.sh <new-repo-dir> --name "<name>" --email "<noreply email>" [--ref <ref>]' >&2
  exit 2
}

dir="" name="" email="" ref="master"
while [ $# -gt 0 ]; do
  case "$1" in
    --name) [ $# -ge 2 ] || usage; name="$2"; shift 2 ;;
    --email) [ $# -ge 2 ] || usage; email="$2"; shift 2 ;;
    --ref) [ $# -ge 2 ] || usage; ref="$2"; shift 2 ;;
    -h|--help) usage ;;
    -*) echo "unknown option: $1" >&2; usage ;;
    *) [ -z "$dir" ] || usage; dir="$1"; shift ;;
  esac
done
[ -n "$dir" ] && [ -n "$name" ] && [ -n "$email" ] || usage
case "$email" in
  *@users.noreply.github.com) ;;
  *) echo "refusing: --email must be a GitHub noreply address (…@users.noreply.github.com)" >&2; exit 2 ;;
esac

if [ -e "$dir" ] && [ -n "$(ls -A "$dir" 2>/dev/null)" ]; then
  echo "refusing: $dir exists and is not empty" >&2
  exit 1
fi
src_root="$(cd "$(dirname "$0")/.." && git rev-parse --show-toplevel)"
git -C "$src_root" rev-parse --verify --quiet "$ref^{commit}" >/dev/null \
  || { echo "refusing: no such ref: $ref" >&2; exit 1; }

mkdir -p "$dir"
git -C "$src_root" archive "$ref" | tar -x -C "$dir"

# Privacy check. Patterns are built so this script does not match itself.
# Binary files are scanned too (-a); matching is case-insensitive (-i); a grep
# error (exit 2, e.g. an unreadable file) fails the check instead of passing it.
providers='gmail|googlemail|outlook|hotmail|yahoo|icloud|protonmail|proton|live|msn|aol|pm|fastmail|hey'
fail_patterns=(
  "[A-Za-z0-9._%+-]+@(${providers})\\.[a-z]+"
  '(/home|/Users)/[A-Za-z0-9._-]+'
  'C:\\+Users\\+[A-Za-z0-9._-]+'
  '-----BEGIN [A-Z ]*PRIVATE KEY-----'
  'gh[pousr]_[A-Za-z0-9]{36,}'
  'github_pat_[A-Za-z0-9_]{20,}'
  '(AKIA|ASIA)[A-Z0-9]{16}'
)
# A matched text is a placeholder only if it is exactly one of these.
placeholder_re='^((/home|/Users)/(user|username|you|runner|name|me|example|x)|c:\\+users\\+(user|username|you|name|x))$'

scan() { # scan <grep args...>; prints path:line:match; fails on grep error
  local out rc=0
  out="$(cd "$dir" && grep -rainE --exclude-dir=.git "$@" . 2>&1)" || rc=$?
  if [ "$rc" -ge 2 ]; then
    echo "PRIVACY CHECK FAILED: grep error while scanning:" >&2
    echo "$out" >&2
    exit 1
  fi
  [ -n "$out" ] && printf '%s\n' "$out" | sed 's|^\./||'
  return 0
}

scanout="$(mktemp)"
trap 'rm -f "$scanout"' EXIT
hits=""
for pat in "${fail_patterns[@]}"; do
  scan -o -e "$pat" >"$scanout"
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    rest="${line#*:}"
    match="${rest#*:}"
    lower="$(printf '%s' "$match" | tr 'A-Z' 'a-z')"
    if printf '%s' "$lower" | grep -qE "$placeholder_re"; then
      continue
    fi
    hits="${hits}${line}"$'\n'
  done <"$scanout"
done
if [ -n "$hits" ]; then
  echo "PRIVACY CHECK FAILED. Hits (path:line:match):" >&2
  printf '%s' "$hits" | sort -u >&2
  echo "Nothing was committed. Remove $dir after fixing the source." >&2
  exit 1
fi

# Warn (not fail) on any other email-looking string, excluding the given identity.
scan -o -e '[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}' >"$scanout"
warn="$(grep -viF "$email" "$scanout" || true)"
if [ -n "$warn" ]; then
  echo "WARNING: other email-looking strings, please review (path:line:match):"
  echo "$warn"
fi

# Binary files were scanned, but strings inside images can be encoded; list them.
bins=""
while IFS= read -r -d '' f; do
  if [ -s "$f" ] && ! grep -qI '' "$f"; then bins="${bins}${f#"$dir"/}"$'\n'; fi
done < <(find "$dir" -path "$dir/.git" -prune -o -type f -print0)
if [ -n "$bins" ]; then
  echo "CHECK THESE BY EYE (binary files, e.g. screenshots):"
  printf '%s' "$bins" | sort
fi

# One commit, identity from arguments only; a missing identity must fail, not fall back.
export GIT_AUTHOR_NAME="$name" GIT_AUTHOR_EMAIL="$email"
export GIT_COMMITTER_NAME="$name" GIT_COMMITTER_EMAIL="$email"
git -C "$dir" -c core.hooksPath=/dev/null -c user.useConfigOnly=true init -q -b main
git -C "$dir" add -A
git -C "$dir" -c core.hooksPath=/dev/null -c user.useConfigOnly=true -c commit.gpgsign=false \
  commit -q -m "Initial public release"

cat <<MSG

Exported $ref ($(git -C "$src_root" rev-parse --short "$ref^{commit}")).
Created $dir with one commit on main (author and committer: $name <$email>).
Nothing was pushed. When ready, run these yourself:

  gh repo create <owner>/<name> --public --source $dir --remote origin
  git -C $dir push -u origin main
MSG
