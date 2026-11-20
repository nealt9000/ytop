# Region-blocked videos: retry through proxies — design

Date: 2026-11-20 · Size: small change (short design)

## Intent

Large playlists (e.g. a free-documentary playlist) lose many videos to
"The uploader has not made this video available in your country". The
location YouTube assigns is often wrong (VPNs, carrier NAT, stale geo-IP
databases), and the browser gets it wrong too. The goal is that the whole
intended playlist ends up on the stick, whatever location YouTube thinks
the host is in.

YTOP has no geo logic today. YouTube decides from the requesting IP;
yt-dlp raises `GeoRestrictedError` (YouTube extractor, with
`availableCountries` when the page lists them); YTOP files it as a generic
"other" failure. yt-dlp's `geo_bypass` (fake X-Forwarded-For) does not work
on YouTube, so the only lever is which IP the request leaves from.

## Goals

- The user lists one or more proxies (optional country label each) in
  Settings.
- Normal traffic stays direct. A video that fails as region-blocked is
  retried through the proxies, one at a time, until one works.
- A video still blocked after every proxy is shown as **region-blocked**
  (with the allowed countries when known), not a generic failure, and is
  retried on the next run like any other failure.

## Non-goals

- Routing all traffic through a proxy (option 2, rejected: slower, more
  bot checks, one country still leaves gaps).
- Finding, buying or managing proxies or VPN apps; free-proxy lists.
- Bypassing age gates, private or removed videos.

## Anti-goals

- A proxy that bot-checks must not halt the rest of the playlist.
- Proxy passwords must never appear in logs, job pages or error text.
- No extra traffic for videos that are not region-blocked.

## Approaches considered

1. **Retry blocked videos through user-listed proxies** — chosen. Fast
   direct path for the bulk; several countries cover more videos; small
   change because yt-dlp takes a `proxy` option and YTOP's in-process and
   subprocess runners already pass options through.
2. **Everything through one proxy** — simplest, but slow, triggers more bot
   checks on shared VPN IPs, and one country still blocks some videos.
3. **Label and fill in later from another location** — no setup, but the
   playlist is not whole now. Its labelling is folded into option 1.

## Design

- **Config:** `Config.proxies`: a list of `{url, country}` (`country` an
  optional ISO-3166 alpha-2 code), saved in the library's config file like
  other settings. `url` is any proxy yt-dlp accepts
  (`socks5://`, `http://`, `https://`).
- **Settings page:** a "Region-blocked videos" section to add, remove and
  reorder proxies; passwords masked when shown.
- **Detection:** a new `classify_failure` kind `region_blocked`, matched on
  the exception type when in-process and on yt-dlp's message text
  ("not made this video available in your country", "not available from
  your location") when it came through `SubprocessRunner`. The subprocess
  child also reports `countries` so both paths have them.
- **Retry (probe stage):** when a video's analysis fails as region-blocked,
  try proxies in this order: those whose country is in the allowed list;
  then unlabelled ones. Labelled proxies outside a known allowed list are
  skipped. Stop at the first success.
- **Stickiness:** YouTube's stream URLs are bound to the IP that fetched
  them, so the probed item remembers the proxy that worked
  (`ProbedItem.proxy`), and that video's media download, alternate audio
  tracks and transcript fetch use the same proxy.
- **Bot check through a proxy:** counts as that proxy failing for that
  video; move on to the next proxy. It does not trigger the existing
  stop-the-whole-run bot-check behaviour, which stays for the direct path.
- **Reporting:** `error_kind = "region_blocked"`; job and playlist pages
  say "Region-blocked — available in: …" (or "in other countries") and,
  when no proxies are set, link to the Settings section.
- **Secrets:** a helper redacts `user:pass@` from proxy URLs wherever they
  could reach logs, job text or exception messages.

## Proxy provider: NordVPN

The owner uses NordVPN (app installed on this host; not logged in until
the next restart). What we know, to be verified first in the plan:

- Nord's SOCKS5 proxies cover only a few countries (reported: US, NL, SE).
- Nord servers in many more countries also run an HTTPS proxy (the one
  its browser extension uses; reported port 89), found per country through
  Nord's public server-recommendation API (`proxy_ssl` technology).
- Both use Nord **service credentials** (Nord account dashboard → manual
  setup), not the account login. The owner enters them in Settings; they
  never pass through Claude.

So `url` must accept `https://` proxy URLs as well as `socks5://` and
`http://`; the plan's first task is a spike confirming yt-dlp can use a
Nord HTTPS proxy. The design stays provider-neutral; Nord appears only in
the acceptance run, not in the code. While the acceptance run happens the
Nord app must stay **disconnected** (or YTOP split-tunnelled), otherwise
the "direct" path is also through Nord.

## Acceptance run (final test)

On the plugged-in 128 GB stick (`USB321FD`, PNY; same FAT label as the
owner's other new sticks, renaming is fine; identify by serial and its
YTOP library, and check its contents before writing; never format or
delete):

1. One full sync of the owner's test playlist
   `PL30RAv-0lkxG17cdEX9qM5Nnl9eSvfjgR` with **no proxies**, at default
   speed, browser sign-in on. Record every region-blocked video and its
   allowed countries.
2. From that list pick the smallest set of countries covering every
   region-blocked video; look up one Nord proxy endpoint per country and
   write them (host, country, no credentials) into the stick's Settings.
   The owner adds the service credentials.
3. One more sync of the same playlist. **Success:** zero videos end as
   region-blocked; every remaining failure is private, deleted or another
   non-geo reason, listed in the report.

Exactly these two live runs, not back to back without a pause; no other
full-playlist probes (this IP was bot-flagged by three back-to-back full
probes on 2026-11-20).

## Next (separate design)

Long-term bot-flag avoidance (pacing, backoff instead of stopping,
spreading load across exits, sign-in/PO-token posture). The owner asked
for it; it gets its own brainstorm after this plan, since it touches every
YouTube request, not only region-blocked ones.

## Left to the builder

- Exact Settings layout and copy; the reorder control.
- Whether transcripts use the proxy via youtube-transcript-api's proxy
  config or are skipped for proxied videos if that proves awkward.
- Test shape. Tests use fakes only; no live probes (bot flag risk).
