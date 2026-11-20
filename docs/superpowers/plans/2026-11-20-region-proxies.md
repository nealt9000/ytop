# Region-blocked videos via proxies — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A video YouTube refuses as "not available in your country" is retried through the user's proxies until one works; what is still blocked is reported as region-blocked with its allowed countries.

**Architecture:** A new `ytop/yt/proxies.py` holds the pure helpers (detection, ordering, URL building, redaction, messages, country cover) and an *active proxy* context variable, mirroring how `ytop/yt/runner.py` carries the active runner. `base_options` adds `opts["proxy"]` from that variable, so every yt-dlp call made inside `using_proxy(p)` goes through `p` with no signature threading. Probing wraps `analyse_video` in a fallback loop; downloading and transcripts run each item inside `using_proxy(item.proxy)` because YouTube's stream URLs are bound to the fetching IP.

**Tech Stack:** Python 3.12, yt-dlp (`proxy` option; `http`/`https`/`socks5(h)` proxy schemes via its requests handler), youtube-transcript-api 1.2.5 (`GenericProxyConfig`), FastAPI + Jinja (Settings page), pytest.

**Spec:** `docs/superpowers/specs/2026-11-20-region-proxy-design.md`

## Global Constraints

- Normal traffic stays direct; a proxy is used only for a video that failed as region-blocked (and then for that video's download, audio tracks and transcript).
- Proxy passwords never appear in logs, job pages, CLI output, error text or rendered HTML.
- A bot check through a proxy counts as that proxy failing for that video; it never sets the playlist-wide stop (`blocked` in `probe_playlist`).
- Tests use fakes only. No live YouTube requests in tests. Run pytest with `TMPDIR=$PWD/.tools/tmp`; deselect the known host-only failure `tests/test_components.py::test_interrupted_prune_never_leaves_a_partial_folder_under_its_name`.
- Live runs (Tasks 1 and 7 only): exactly the runs listed there. Never a back-to-back full-playlist probe; never touch `~/Videos/YTOP`; on the stick, never format or delete.
- Region-blocked detection strings (case-insensitive): `not made this video available in your country`, `not available from your location`, `not available in your country`.
- `error_kind` value: `"region_blocked"`.
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; work on branch `feat/region-proxies`; merge locally, never `gh pr merge`.

## Review Focus

1. **A proxy that is down or refuses the login** (connection refused, 407): treated as that proxy failing, try the next; the final message must not contain the password. → test in Task 4.
2. **Every proxy labelled with a country outside a known allowed list**: no proxy is tried, message says which countries would work. → test in Task 3 (`order_proxies`) and Task 4.
3. **Region-blocked message from the subprocess runner** (component-retry path) has no exception type, only text, and countries passed separately. → test in Task 2.
4. **Saving Settings with the masked password field left empty** keeps the stored password; with the textarea showing a masked embedded password, saving keeps the real one. → test in Task 6.
5. **Re-sync of a playlist whose region-blocked videos are now downloaded**: they are `existing`, never re-probed, never proxied. → test in Task 4.

---

### Task 1: Spike — Nord HTTPS proxy works with yt-dlp (gate)

Throwaway; nothing committed except a note in the spec. If it fails, stop and return to the owner before Task 2.

**Files:** scratch only (`$SCRATCH` = session scratchpad).

- [ ] **Step 1: Owner provides service credentials** — ask the owner to create `$SCRATCH/nord.cred` (one line `username:password`, from Nord dashboard → manual setup → service credentials) and `chmod 600` it. Never print or `cat` it.
- [ ] **Step 2: Find one Nord HTTPS proxy host** — `curl -s 'https://api.nordvpn.com/v1/servers/recommendations?filters[servers_technologies][identifier]=proxy_ssl&limit=1'` and read `hostname`; record also its country. Expected: a host like `us1234.nordvpn.com`.
- [ ] **Step 3: One extraction through it** — a script in `$SCRATCH` (run with `uv run python -I`, isolated `HOME`/`XDG_CACHE_HOME`) reads the cred file and runs `YoutubeDL({"proxy": f"https://{cred}@{host}:89", "skip_download": True, "quiet": True}).extract_info("https://www.youtube.com/watch?v=jNQXAC9IVRw", download=False)`, printing only the title and `len(formats)`. Expected: title "Me at the zoo", formats > 0. If port 89 fails, try the SOCKS5 host list Nord documents (port 1080, `socks5h://`) and record which works.
- [ ] **Step 4: Record the outcome** — append a "Spike result (2026-11-20)" line to the spec's "Proxy provider" section: scheme, port, API filter that worked. Commit the spec.

---

### Task 2: Region-blocked detection and countries

**Files:**
- Create: `src/ytop/yt/proxies.py`
- Modify: `src/ytop/yt/probe.py` (`ProbeError`, `_extract`, `classify_failure`)
- Modify: `src/ytop/yt/runner.py` (`_CHILD`, `_run`, `_download_error`)
- Test: `tests/test_proxies.py` (new), `tests/test_runner.py` if present else `tests/test_proxies.py`

**Interfaces:**
- Produces: `proxies.is_region_blocked(error: BaseException | str | None) -> bool` (walks `__cause__`/`__context__` like `classify_failure`; true for `GeoRestrictedError` anywhere in the chain or a detection string in any text). `ProbeError(message, countries: list[str] | None = None)` with attribute `.countries`. `classify_failure` returns `"region_blocked"` before checking `YOUTUBE_CHANGED`. `RegionBlockedError(DownloadError)` raised by `SubprocessRunner` when the child reports `countries`.

- [ ] **Step 1: Write the failing tests**

```python
def test_region_blocked_from_text_and_type():
    from yt_dlp.utils import GeoRestrictedError, DownloadError
    assert is_region_blocked("ERROR: [youtube] x: The uploader has not made this video available in your country")
    assert is_region_blocked(GeoRestrictedError("blocked", countries=["US"]))
    wrapped = DownloadError("ERROR: x"); wrapped.__cause__ = GeoRestrictedError("m", countries=["US"])
    assert is_region_blocked(wrapped)
    assert not is_region_blocked("Private video")
    assert not is_region_blocked(None)

def test_extract_keeps_allowed_countries(monkeypatch):
    # fake YoutubeDL.extract_info raising DownloadError whose exc_info[1] is GeoRestrictedError(countries=["US","CA"])
    with pytest.raises(ProbeError) as e:
        probe_mod._extract("https://www.youtube.com/watch?v=x", {})
    assert e.value.countries == ["US", "CA"]
    assert classify_failure(e.value) == "region_blocked"

def test_subprocess_runner_carries_countries(fake_child):
    # child outputs {"error": "...not made this video available in your country", "type": "GeoRestrictedError", "countries": ["DE"]}
    with pytest.raises(RegionBlockedError) as e:
        runner.extract("u", {})
    assert e.value.countries == ["DE"]
```

- [ ] **Step 2: Run** `TMPDIR=$PWD/.tools/tmp uv run pytest tests/test_proxies.py -v` — Expected: FAIL (module missing).
- [ ] **Step 3: Implement.** In `_extract`, read countries from `exc.exc_info[1].countries` (in-process `DownloadError`), `exc.countries` (`GeoRestrictedError` or `RegionBlockedError`); pass to `ProbeError`. In `_CHILD`, add `"countries": getattr(exc, "countries", None)` (also from `exc.exc_info[1]` when present) to the error document; in `_run`, raise `RegionBlockedError(message, countries)` when `countries` is set or the message is region-blocked. Uppercase country codes.
- [ ] **Step 4: Run** the tests plus `tests/test_download.py` — Expected: PASS.
- [ ] **Step 5: Commit** `feat: recognise region-blocked videos and keep their allowed countries`.

---

### Task 3: Proxy config and pure helpers

**Files:**
- Modify: `src/ytop/config.py` (new `ProxyServer`, `Config` fields, `load_config`, `save_config`)
- Modify: `src/ytop/yt/proxies.py`
- Modify: `src/ytop/yt/probe.py` (`base_options`)
- Test: `tests/test_config.py`, `tests/test_proxies.py`

**Interfaces:**
- Produces (config): `@dataclass(frozen=True) class ProxyServer: url: str; country: str = ""`. `Config.proxies: tuple[ProxyServer, ...] = ()`, `Config.proxy_username: str = ""`, `Config.proxy_password: str = ""`. TOML shape:
  ```toml
  [proxies]
  username = "..."
  password = "..."
  [[proxies.servers]]
  url = "https://us1234.nordvpn.com:89"
  country = "US"
  ```
  `[proxies]` is omitted from the file when there are no servers and no login.
- Produces (proxies.py):
  - `proxy_url(server: ProxyServer, config: Config) -> str` — inserts `quote(username):quote(password)@` when the URL has no userinfo and a username is set; otherwise the URL unchanged.
  - `order_proxies(servers: Sequence[ProxyServer], countries: list[str] | None) -> list[ProxyServer]` — countries known: servers whose country is in the list (config order), then unlabelled; labelled servers outside the list dropped. Countries unknown: all servers, config order.
  - `redact(text: str) -> str` — replaces every `scheme://user:pass@` or `scheme://user@` with `scheme://***@`.
  - `region_blocked_message(countries: list[str] | None, tried: int) -> str` — exact copy: `"Region-blocked: available in US, CA, GB"` (first 8 codes, then `" and N more"`), or `"Region-blocked: available only in other countries"` when unknown; suffix `" (tried N proxies)"` when `tried > 0`, `" (no proxies set up)"` when the config has none — the caller passes `tried=-1` for that case.
  - `covering_countries(blocked: list[list[str] | None], offered: set[str] | None = None) -> list[str]` — greedy set cover over the known lists (restricted to `offered` when given); videos with unknown countries are ignored.
  - `using_proxy(url: str | None) -> ContextManager`, `active_proxy() -> str | None` — `contextvars` like `runner.using`/`runner.active`; worker pools already copy context through `runner.spawn`.
- `base_options` sets `opts["proxy"] = active_proxy()` when not None.

- [ ] **Step 1: Write the failing tests**

```python
def test_proxies_round_trip(tmp_path):
    cfg = replace(Config(), proxies=(ProxyServer("https://us1.example:89", "US"), ProxyServer("socks5h://h:1080")),
                  proxy_username="u@x", proxy_password='p"w')
    save_config(tmp_path, cfg)
    assert load_config(tmp_path).proxies == cfg.proxies
    assert load_config(tmp_path).proxy_password == 'p"w'

def test_no_proxies_section_when_unused(tmp_path):
    save_config(tmp_path, Config()); assert "[proxies]" not in config_text(tmp_path)

def test_proxy_url_adds_login():
    cfg = replace(Config(), proxy_username="u@x", proxy_password="p:w")
    assert proxy_url(ProxyServer("https://h:89"), cfg) == "https://u%40x:p%3Aw@h:89"
    assert proxy_url(ProxyServer("https://a:b@h:89"), cfg) == "https://a:b@h:89"

def test_order_proxies():
    us, de, any_ = ProxyServer("a", "US"), ProxyServer("b", "DE"), ProxyServer("c")
    assert order_proxies([any_, de, us], ["US"]) == [us, any_]
    assert order_proxies([any_, de, us], None) == [any_, de, us]
    assert order_proxies([de], ["US"]) == []

def test_redact():
    assert redact("Unable to connect to proxy https://u:secret@h:89/") == "Unable to connect to proxy https://***@h:89/"

def test_region_blocked_message():
    assert region_blocked_message(["US", "CA"], 2) == "Region-blocked: available in US, CA (tried 2 proxies)"
    assert region_blocked_message(None, -1) == "Region-blocked: available only in other countries (no proxies set up)"
    assert region_blocked_message([f"C{i}" for i in range(10)], 0).endswith("C7 and 2 more")

def test_covering_countries():
    assert covering_countries([["US", "CA"], ["CA", "GB"], ["DE"], None]) == ["CA", "DE"]
    assert covering_countries([["US", "CA"]], offered={"US"}) == ["US"]

def test_base_options_uses_active_proxy():
    assert "proxy" not in base_options(Config())
    with using_proxy("https://h:89"):
        assert base_options(Config())["proxy"] == "https://h:89"
```

- [ ] **Step 2: Run** `uv run pytest tests/test_config.py tests/test_proxies.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** the config fields and helpers as specified. `save_config` writes `[[proxies.servers]]` after `[proxies]`; `_toml_value` already escapes strings. Greedy cover tie-break: alphabetical.
- [ ] **Step 4: Run** the full suite — Expected: PASS.
- [ ] **Step 5: Commit** `feat: proxy settings and helpers for region-blocked videos`.

---

### Task 4: Retry region-blocked videos through proxies while probing

**Files:**
- Modify: `src/ytop/yt/probe.py` (`ProbedItem`, new `analyse_with_fallback`, `probe_one` in `probe_playlist`, `probe_video`)
- Test: `tests/test_download.py` (existing fakes monkeypatch `probe_mod.extract_video_info`)

**Interfaces:**
- Consumes: Task 2 `ProbeError.countries`, `is_region_blocked`; Task 3 `order_proxies`, `proxy_url`, `using_proxy`, `redact`, `region_blocked_message`.
- Produces: `ProbedItem.proxy: str | None = None` (full URL incl. login; never rendered), `ProbedItem.countries: list[str] | None = None`, `ProbedItem.error_kind: str | None = None`. `analyse_with_fallback(video_id, config, cache_root, *, max_height, preserve_audio) -> tuple[dict, Selection, str | None]` — returns the proxy URL used, or None for direct; raises `ProbeError` with `region_blocked_message(...)` text and `.countries` when every proxy fails or none is set, and re-raises the direct error unchanged when it is not region-blocked.

- [ ] **Step 1: Write the failing tests**

```python
def test_region_blocked_video_is_read_through_a_matching_proxy(monkeypatch, tmp_path):
    seen = []
    def video(vid, config, cache_root=None, *, quick=False):
        seen.append(active_proxy())
        if active_proxy() is None:
            raise ProbeError("The uploader has not made this video available in your country", ["US"])
        return fake_info(vid)
    monkeypatch.setattr(probe_mod, "extract_video_info", video)
    cfg = replace(Config(), proxies=(ProxyServer("https://de:89", "DE"), ProxyServer("https://us:89", "US")))
    item = probe_video("abc", cfg).items[0]
    assert item.error is None and item.proxy == "https://us:89"
    assert "https://de:89" not in seen

def test_bot_check_through_a_proxy_tries_the_next_and_does_not_stop_the_playlist(monkeypatch): ...
    # proxy A raises "Sign in to confirm you're not a bot"; proxy B succeeds;
    # probe_playlist of 3 entries: all 3 read, result.bot_blocked is False

def test_all_proxies_fail(monkeypatch):
    # direct region-blocked ["US"]; the single US proxy raises "407 Proxy Authentication Required https://u:secret@us:89"
    # item.error == "Region-blocked: available in US (tried 1 proxies)"; item.error_kind == "region_blocked"
    # "secret" not in item.error; item.countries == ["US"]

def test_no_proxies_set_up(monkeypatch):
    # item.error == "Region-blocked: available in US (no proxies set up)"

def test_only_wrong_country_proxies(monkeypatch):
    # proxies DE only, allowed ["US"]: no proxied call made; message "(tried 0 proxies)"

def test_resync_does_not_reprobe_existing_region_blocked_videos(monkeypatch):
    # have() returns True: extract_video_info never called, item.proxy is None

def test_non_region_errors_are_not_retried(monkeypatch):
    # direct raises "Private video": exactly one call, error unchanged, error_kind None
```

- [ ] **Step 2: Run** `uv run pytest tests/test_download.py -k "proxy or region" -v` — Expected: FAIL.
- [ ] **Step 3: Implement** `analyse_with_fallback`; call it from `probe_one` and `probe_video` in place of `analyse_video`, storing `proxy`, `countries`, `error_kind`. The playlist-wide `blocked.set()` keeps reacting only to the direct attempt's error (the fallback swallows proxy bot checks). Errors from proxied attempts pass through `redact` before being kept anywhere.
- [ ] **Step 4: Run** the full suite — Expected: PASS.
- [ ] **Step 5: Commit** `feat: retry region-blocked videos through proxies`.

---

### Task 5: Download, audio and transcripts through the item's proxy; report the kind

**Files:**
- Modify: `src/ytop/yt/download.py` (`_process_item`, the `run` closure's `ItemResult`, CLI summary print)
- Modify: `src/ytop/transcripts/captions.py` (`YouTubeTranscriptApi()` construction)
- Modify: `src/ytop/cli.py` (fetch summary)
- Test: `tests/test_download.py`, `tests/test_captions.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 4 `ProbedItem.proxy/countries/error_kind`; Task 3 `using_proxy`, `active_proxy`, `covering_countries`.
- Produces: `ItemResult.error_kind == "region_blocked"` for items whose probe ended region-blocked (the early-return path in `_process_item` passes `item.error_kind`). Fetch CLI prints, when any are region-blocked, `"{n} region-blocked; countries that would cover them: CA, DE"` (or `"…: unknown"` when no country lists).

- [ ] **Step 1: Write the failing tests**

```python
def test_proxied_item_downloads_through_its_proxy(monkeypatch, tmp_path):
    # ProbedItem(proxy="https://us:89") with a selection and one alternate audio;
    # fake YoutubeDL download records opts["proxy"] for media and audio
    assert recorded == ["https://us:89", "https://us:89"]

def test_direct_item_downloads_direct(monkeypatch, tmp_path):
    assert recorded == [None]

def test_transcript_api_uses_active_http_proxy(monkeypatch):
    # with using_proxy("https://us:89"): YouTubeTranscriptApi called with
    # proxy_config GenericProxyConfig(http_url="https://us:89", https_url="https://us:89")
    # with a socks5h proxy: proxy_config is None and the API route is skipped (requests has no SOCKS here)

def test_region_blocked_result_kind_and_cli_summary(...):
    # ItemResult.error_kind == "region_blocked"; CLI output contains "1 region-blocked; countries that would cover them: US"
```

- [ ] **Step 2: Run** the selected tests — Expected: FAIL.
- [ ] **Step 3: Implement.** Wrap the body of `_process_item` from media download through transcript fetch in `with using_proxy(item.proxy):` (the re-probe on attempt 2 inherits it). In captions, build `GenericProxyConfig` from `active_proxy()` when its scheme is `http`/`https`; for SOCKS skip the API route so the existing yt-dlp fallback (which goes through `base_options`) is used.
- [ ] **Step 4: Run** the full suite — Expected: PASS.
- [ ] **Step 5: Commit** `feat: download proxied videos through the proxy that read them`.

---

### Task 6: Settings section and region-blocked labels in the UI

**Files:**
- Modify: `src/ytop/web/app.py` (`_render_settings`, new `POST /settings/proxies`)
- Modify: `src/ytop/web/templates/settings.html` (new `<section id="proxies">` after `#downloads`)
- Modify: `src/ytop/web/templates/add.html` (failed item row), `src/ytop/web/jobs.py` (row keeps `error_kind` — already copied; check the probe/review rows too)
- Modify: `src/ytop/yt/proxies.py` (form helpers)
- Test: `tests/test_settings_web.py`, `tests/test_proxies.py`

**Interfaces:**
- Produces (proxies.py): `format_servers(servers) -> str` — one line per server, `"US https://h:89"` or `"https://h:89"`, embedded passwords shown as `•••`. `parse_servers(text: str, previous: Sequence[ProxyServer]) -> tuple[ProxyServer, ...]` — each non-blank line is `[CC ]url`; `CC` is two letters (uppercased); URL scheme must be `http`, `https`, `socks5`, `socks5h`, `socks4`, else `ValueError("line N: …")`; a URL whose password is `•••` takes the password of the previous server with the same scheme+user+host+port.
- Form fields: `servers` (textarea), `username` (text), `password` (password input, never pre-filled; blank keeps the stored one), `clear_password` checkbox.
- Section copy: heading "Region-blocked videos"; one paragraph: "When YouTube says a video is not available in your country, YTOP retries it through these proxies. Everything else downloads directly." Show "Login saved" when a password is stored.
- `add.html`: a failed row with `error_kind == "region_blocked"` shows its error and, when no proxies are set, a link `/settings#proxies` "Set up proxies".

- [ ] **Step 1: Write the failing tests**

```python
def test_proxy_settings_saved_and_password_never_rendered(home):
    r = client.post("/settings/proxies", data={"servers": "us https://h:89\nsocks5h://a:sekret@s:1080", "username": "u", "password": "pw"})
    assert r.status_code == 303
    cfg = load_config(root); assert cfg.proxies[0] == ProxyServer("https://h:89", "US") and cfg.proxy_password == "pw"
    page = client.get("/settings").text
    assert "pw" not in page and "sekret" not in page and "Login saved" in page

def test_blank_password_keeps_the_stored_one_and_masked_url_round_trips(home): ...
def test_bad_proxy_line_is_rejected_with_its_line_number(home):  # "ftp://x" -> 400, "line 1"
def test_region_blocked_row_links_to_settings_when_no_proxies(home): ...
```

- [ ] **Step 2: Run** `uv run pytest tests/test_settings_web.py tests/test_proxies.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** the route (mirror `settings_downloads`: `load_config` → `replace` → `save_config`, 400 with message on `ValueError`), template section and helpers.
- [ ] **Step 4: Run** the full suite; then check the page in headless Chromium (Playwright MCP) once at phone width. Expected: PASS; section readable, no password visible.
- [ ] **Step 5: Commit** `feat: Settings section for region-blocked video proxies`.

---

### Task 7: Acceptance run on the plugged-in stick (live)

Uses the repo build writing into the stick's library (the stick's own app is updated with the next release). Owner present for credentials and sign-in.

- [ ] **Step 1: Identify the stick and library** — `lsblk -o NAME,SERIAL,LABEL,MOUNTPOINTS`; `ls "/run/media/neal/USB321FD/YTOP Library"`; record serial, library name, playlists present, free space. Expected: a YTOP library; stop and ask if it holds anything unexpected. Optional: owner renames the FAT label.
- [ ] **Step 2: Pre-flight** — owner confirms the NordVPN app is **disconnected** (proxies work without it); `curl -s https://ipinfo.io/country` shows the home connection. Pick the browser for sign-in.
- [ ] **Step 3: Run 1 — direct, no proxies** — with no `[proxies]` in the stick library config: `uv run ytop fetch PL30RAv-0lkxG17cdEX9qM5Nnl9eSvfjgR --library "/run/media/neal/USB321FD/YTOP Library" --cookies-from-browser <browser>` in the background, defaults otherwise. Save output to `$SCRATCH/run1.log`. Expected: completes; summary lists N region-blocked and the covering countries. If it bot-checks, stop and report; do not rerun.
- [ ] **Step 4: Choose countries and endpoints** — fetch Nord's countries (`https://api.nordvpn.com/v1/servers/countries`) and, per covering country, one `proxy_ssl` recommendation (the filter recorded in Task 1); recompute the cover with `offered=` the countries Nord serves. Write the hosts with country labels via `POST /settings/proxies` (or the Settings page in an isolated `ytop serve` on the stick library), no credentials. Owner enters the service username/password in Settings.
- [ ] **Step 5: Pause** — at least 30 minutes after run 1 ended.
- [ ] **Step 6: Run 2 — with proxies** — same command, `$SCRATCH/run2.log`. Only videos not yet on the stick are probed.
- [ ] **Step 7: Verify success** — from run 2's output and the playlist document: zero items with `error_kind == "region_blocked"`; list every remaining failure with its reason (private, deleted, other). `grep -c` the owner's password in both logs is 0 (check with a script reading `nord.cred`, printing only the count). Report counts: listed, downloaded, region-blocked fixed, still failing by reason.
- [ ] **Step 8: Clean up** — stop any YTOP started; delete `$SCRATCH/nord.cred` after confirming with the owner; update memory with the result.
