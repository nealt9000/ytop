# Age-gated videos: retry signed in with a dedicated account — design

Date: 2026-11-21 · Size: small change (short design) · Builds on
`2026-11-20-region-proxy-design.md`

## Intent

Some playlist videos fail with "Sign in to confirm your age" (on the test
playlist: 6 videos, plus 2 that are also region-blocked). YouTube only
clears the gate for a signed-in, age-verified account; no proxy or client
trick gets around it. The owner wants these on the stick too, but signed in
with a **separate Google account made for YTOP**, never the owner's own.

## Goals

- Settings names one browser profile to use for age-gated videos (browser +
  profile, e.g. Firefox's default profile). Only that name is saved, never
  cookies or passwords.
- A video whose analysis fails as age-gated is retried once, signed in with
  that profile. A video that is region-blocked *and* age-gated is retried
  signed in **through the proxy that cleared its region block**, and its
  download, alternate audio and transcript keep both (same stickiness rule
  as proxies).
- Without a profile set, or when the profile can't be read on this
  computer, the video is shown as **age-restricted** (its own kind, not
  "other"), with a pointer to the Settings section, and retried next run.

## Non-goals

- Creating or age-verifying the account (the owner does that).
- Signing in for any video that isn't age-gated.
- Private or removed videos.

## Anti-goals

- The owner's own Google account must never be used by this path.
- No cookies or credentials written to the stick, logs, job pages or errors.
- An age-gate retry that hits a bot check must not stop the run; it counts
  as the retry failing (same rule as a proxy bot check).

## Constraints

- **Multi-account browser profiles use the wrong account.** With several
  Google accounts signed into one browser profile, YouTube (and yt-dlp)
  use the profile's first/default account. The owner's main Brave profile
  has the owner's account first, so the dedicated account must live in a
  browser profile where it is the only account. **Owner's setup:** Firefox
  (its default profile) holds only the YTOP account; Brave keeps the owner's
  own accounts.
- Signed-in reading currently fails for every video with "The page needs
  to be reloaded" (yt-dlp 2026.10.26; upstream #17389 is closed). It was
  seen with the owner's multi-account Brave profile, so the cause may be
  that profile; the first plan task is a one-video spike with the Firefox
  profile. If it still fails, the feature waits on yt-dlp.
- Brave/Chrome cookies on Linux need `secretstorage` (added on
  `feat/region-proxies`).
- The existing whole-session sign-in (`--cookies-from-browser`, web
  toggle) stays as it is and is never persisted; this is a separate,
  narrower setting.

## Approaches

1. **Retry age-gated videos signed in with a named browser profile** —
   chosen. Account sees only the few gated videos; nothing secret on the
   stick. Cost: works only on computers that have that profile; elsewhere
   the video is labelled and retried later.
2. Same retry, cookies exported to a file on the stick — portable, but the
   stick carries a live login and the cookies expire. Rejected.
3. Whole-run sign-in with the new account — no code, but every request is
   signed in (more account and bot-flag exposure). Rejected.

## Design

- **Config:** `Config.age_gate_browser`: `None` or a yt-dlp
  `cookiesfrombrowser` spec (`brave:Profile 2`, `firefox:ytop`), saved in
  the library config.
- **Detection:** `classify_failure` kind `age_restricted`, matched on
  "confirm your age" / "age-restricted" / yt-dlp's age-gate exception, in
  both in-process and subprocess paths.
- **Retry (probe stage):** after the direct try, and after the proxy retry
  when the region block was cleared but the proxy read failed on the age
  gate, re-run the analysis once with the profile's cookies (plus the
  winning proxy, if any). `ProbedItem` records that it was read signed in;
  the download uses the same cookies and proxy.
- **Settings:** a "Age-restricted videos" section: pick browser + profile
  from the profiles found on this computer; short note that it should be a
  profile holding only the YTOP account.
- **Reporting:** `error_kind = "age_restricted"`; "Age-restricted — needs
  the YTOP sign-in" with a link to Settings when unset or unreadable.

## Left to the builder

- Exact Settings copy and how browser profiles are listed.
- Test shape (fakes only; the only live call is the one-video spike).
