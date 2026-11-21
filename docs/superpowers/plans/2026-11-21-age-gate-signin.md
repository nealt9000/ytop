# Age-gated videos: dedicated sign-in retry — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A video that fails with YouTube's age gate is retried once signed in with a browser profile named in Settings (the owner's dedicated YTOP account), through its proxy when it was also region-blocked; otherwise it is reported as age-restricted.

**Architecture:** Mirrors the region-proxy retry. A context variable (`using_signin`, like `using_proxy`) makes `apply_auth_options` add `cookiesfrombrowser` for one call; `analyse_with_fallback` retries age-gated reads inside it and reports `signed_in`; the download re-enters the same context. One persisted setting, `Config.age_gate_browser`, holds a yt-dlp browser spec (no cookies).

**Tech Stack:** Python 3.12, yt-dlp (`cookiesfrombrowser`), FastAPI + Jinja2 Settings page, pytest with fakes.

**Spec:** `docs/superpowers/specs/2026-11-21-age-gate-signin-design.md`

## Global Constraints

- The owner's own Google account must never be used by this path: only `Config.age_gate_browser`, never `SESSION_AUTH`, decides the retry's cookies; the session sign-in stays process-only and unchanged.
- No cookies or credentials written to the stick, logs, job pages or errors. Only the browser spec string (e.g. `firefox`, `firefox:Q6uSzFvx.Profile 1`) is saved.
- Signed in only for videos that are age-gated; every other request stays as today.
- An age-gate retry that hits a bot check must not stop the run (it is that retry failing, like a proxy bot check).
- Error kind string: `"age_restricted"`. Copy: `Age-restricted — needs the YTOP sign-in` (unset/unreadable profile). Settings anchor: `#age-gate`.
- Tests use fakes only; the only live YouTube call in this plan is Task 1's spike.
- Product name in UI copy comes from `{{ product_name }}`, as elsewhere in `settings.html`.

## Review Focus

1. A video both region-blocked and age-gated: the signed-in retry must reuse the proxy that cleared the region block, and the download must run with both. (Task 4 test `test_region_and_age_gated_retries_signed_in_through_the_proxy`, Task 5 test.)
2. Profile set but unreadable on this computer (Firefox not installed, profile renamed, Brave keyring locked): the video is age-restricted with a "could not be read" reason, the run continues, and no cookie-DB path or keyring text containing secrets leaks. (Task 4 `test_unreadable_profile_is_reported_not_raised`.)
3. A signed-in retry that comes back with "Sign in to confirm you're not a bot": the item's error must not match `is_bot_check`, or the whole run stops. (Task 4 `test_signed_in_bot_check_does_not_stop_the_run`.)
4. Session sign-in on *and* `age_gate_browser` set: the signed-in retry uses `age_gate_browser`, not the session browser. (Task 3 `test_signin_context_overrides_session_browser`.)
5. Old configs without the new key, and a saved spec with an unknown browser name: load as unset / reject on save with a clear message. (Task 2 tests.)

---

### Task 1: Live spike — signed-in read with the dedicated Firefox profile

Not code. **Do not start while the `--upgrade` pass (run 4) is still running** (`ps aux | grep '[y]top fetch'` must print nothing); one extra probe on top of a running fetch raises bot-flag risk.

**Files:**
- Create (scratch, not committed): `<scratchpad>/agegate_spike.py`
- Modify: `docs/superpowers/specs/2026-11-21-age-gate-signin-design.md` (add a "Spike result" paragraph)

- [ ] **Step 1:** Write a script that, using the stick config (`load_config(Path("/run/media/neal/USB321FD"))`) for proxy URL/login and never printing it, runs yt-dlp `extract_info(download=False)` with `cookiesfrombrowser=("firefox", None, None, None)` and the repo's `base_options` for:
  - `I2djFnp5h0w` (age-gated, not region-blocked) — direct;
  - `uxggN_9EPqc` (age-gated + US-only) — through the US proxy (`using_proxy(proxy_url(cfg.proxies[0], cfg))`).
  Print per video only: id, OK/FAIL, title or the error passed through `redact()`, and the number of formats.
- [ ] **Step 2:** Run it: `uv run python <scratchpad>/agegate_spike.py 2>&1 | tee <scratchpad>/agegate_spike.log`, then `grep -c "$(python3 -c 'from ytop.config import load_config;from pathlib import Path;print(load_config(Path("/run/media/neal/USB321FD")).proxy_password)')" <scratchpad>/agegate_spike.log` → expected `0` (run the grep without echoing the password to the terminal).
- [ ] **Step 3:** Record the outcome in the spec. **Gate:** if both fail with "The page needs to be reloaded", stop the plan and report to the owner (feature waits on yt-dlp). If only the proxied one fails, record it and continue (the direct case still ships). Commit the spec:

```bash
git add docs/superpowers/specs/2026-11-21-age-gate-signin-design.md
git commit -m "docs: record age-gate sign-in spike result"
```

### Task 2: `Config.age_gate_browser`

**Files:**
- Modify: `src/ytop/config.py` (field on `Config`; `load_config`; `save_config`)
- Modify: `src/ytop/yt/pot.py` (validator next to `cookies_from_browser`)
- Test: `tests/test_config.py`, `tests/test_pot.py`

**Interfaces:**
- Produces: `Config.age_gate_browser: str = ""` (empty = unset), persisted as `[signin] age_gate_browser = "..."` (section written only when non-empty); `ytop.yt.pot.check_browser_spec(value: str) -> str` returns the stripped spec or raises `ValueError("unknown browser 'x'; use one of: brave, chrome, ...")` using `yt_dlp.cookies.SUPPORTED_BROWSERS`.

- [ ] **Step 1: Failing tests**

```python
def test_age_gate_browser_round_trips(tmp_path):
    save_config(tmp_path, replace(Config(), age_gate_browser="firefox:Q6uSzFvx.Profile 1"))
    assert load_config(tmp_path).age_gate_browser == "firefox:Q6uSzFvx.Profile 1"
    assert "[signin]" in LibraryLayout(tmp_path).config_file.read_text()

def test_age_gate_browser_absent_is_unset(tmp_path):
    save_config(tmp_path, Config())
    text = LibraryLayout(tmp_path).config_file.read_text()
    assert "[signin]" not in text
    assert load_config(tmp_path).age_gate_browser == ""

def test_save_never_writes_session_cookies(tmp_path):
    save_config(tmp_path, replace(Config(), cookies_from_browser="brave", age_gate_browser="firefox"))
    text = LibraryLayout(tmp_path).config_file.read_text()
    assert "brave" not in text and "cookies" not in text
```

```python
# tests/test_pot.py
def test_check_browser_spec():
    assert check_browser_spec(" firefox:Q6uSzFvx.Profile 1 ") == "firefox:Q6uSzFvx.Profile 1"
    with pytest.raises(ValueError, match="unknown browser 'netscape'"):
        check_browser_spec("netscape")
```

- [ ] **Step 2:** `TMPDIR=$PWD/.tmp uv run pytest tests/test_config.py tests/test_pot.py -q` → the new tests FAIL.
- [ ] **Step 3:** Implement the field, load (`_section(data, "signin")`), save, and `check_browser_spec`. `load_config` does **not** validate (a stick moved to a host without that browser must still load).
- [ ] **Step 4:** Same command → PASS.
- [ ] **Step 5:** Commit `feat: age_gate_browser setting for age-gated videos`.

### Task 3: Age-gate detection and the sign-in context

**Files:**
- Create: `src/ytop/yt/signin.py`
- Modify: `src/ytop/yt/pot.py` (`apply_auth_options`)
- Modify: `src/ytop/yt/probe.py` (`classify_failure`)
- Test: `tests/test_signin.py` (new), `tests/test_proxies.py` (classify ordering)

**Interfaces:**
- Produces in `ytop.yt.signin`:
  - `AGE_GATED: re.Pattern` matching `sign in to confirm your age|age-restricted|inappropriate for some users` (case-insensitive).
  - `is_age_gated(error: BaseException | str | None) -> bool` — walks the cause chain like `is_region_blocked`.
  - `using_signin(browser: str | None)` context manager and `active_signin() -> str | None` (ContextVar, default None).
  - `AGE_RESTRICTED_NEEDS_SIGNIN = "Age-restricted — needs the YTOP sign-in"`.
- `apply_auth_options`: when `active_signin()` is set, `opts["cookiesfrombrowser"] = cookies_from_browser(active_signin())` **instead of** the session browser.
- `classify_failure` returns `"age_restricted"` when `is_age_gated` and not region-blocked; `"region_blocked"` still wins when both texts are present (the region message is the one that decides the next step).

- [ ] **Step 1: Failing tests**

```python
def test_is_age_gated_text_and_chain():
    assert is_age_gated("ERROR: [youtube] x: Sign in to confirm your age. This video may be inappropriate")
    assert not is_age_gated("Sign in to confirm you're not a bot")
    try:
        try: raise ValueError("Sign in to confirm your age")
        except ValueError as e: raise ProbeError("wrapped") from e
    except ProbeError as outer:
        assert is_age_gated(outer)

def test_signin_context_adds_cookies_only_inside():
    opts = {}; apply_auth_options(opts, Config(pot_provider="off"))
    assert "cookiesfrombrowser" not in opts
    with using_signin("firefox:Q6uSzFvx.Profile 1"):
        opts = {}; apply_auth_options(opts, Config(pot_provider="off"))
    assert opts["cookiesfrombrowser"] == ("firefox", "Q6uSzFvx.Profile 1", None, None)

def test_signin_context_overrides_session_browser(monkeypatch):
    monkeypatch.setattr(SESSION_AUTH, "browser", "brave")
    with using_signin("firefox"):
        opts = {}; apply_auth_options(opts, Config(pot_provider="off"))
    assert opts["cookiesfrombrowser"][0] == "firefox"

def test_classify_failure_age_restricted():
    assert classify_failure("Sign in to confirm your age") == "age_restricted"
    assert classify_failure("not made this video available in your country\nSign in to confirm your age") == "region_blocked"
```

- [ ] **Step 2:** `TMPDIR=$PWD/.tmp uv run pytest tests/test_signin.py tests/test_proxies.py -q` → FAIL.
- [ ] **Step 3:** Implement. The subprocess runner already forwards `cookiesfrombrowser` (`runner.py:49,107`); no change there.
- [ ] **Step 4:** Same command → PASS; also `tests/test_pot.py`.
- [ ] **Step 5:** Commit `feat: recognise age-gated videos; scoped sign-in context`.

### Task 4: Signed-in retry in the probe

**Files:**
- Modify: `src/ytop/yt/probe.py` (`ProbedItem`, `analyse_with_fallback`, `_record_failure`, both call sites at the `probe_video` and `probe_playlist` item loops)
- Modify: `src/ytop/web/jobs.py:1590` (copy `signed_in` alongside `proxy` on re-probe)
- Test: `tests/test_probe_signin.py` (new; fake `analyse_video` via monkeypatch that inspects `active_proxy()` / `active_signin()`)

**Interfaces:**
- Consumes: Task 2 `Config.age_gate_browser`; Task 3 `using_signin`, `active_signin`, `is_age_gated`, `AGE_RESTRICTED_NEEDS_SIGNIN`.
- Produces:
  - `ProbedItem.signed_in: bool = False` (read with the age-gate profile; the download must use it too).
  - `analyse_with_fallback(...) -> tuple[dict, Selection, str | None, bool]` — 4th element `signed_in`. Update both callers to `info, item.selection, item.proxy, item.signed_in = ...`.
  - Age-restricted failures raise `ProbeError` whose message starts with `"Age-restricted"`; `_record_failure` sets `item.error_kind = "age_restricted"` for them (region-blocked keeps priority as in `classify_failure`).

Behaviour to implement (`analyse_with_fallback`):
1. Direct read. Age-gated failure → if `config.age_gate_browser`: retry once `with using_signin(config.age_gate_browser)` (config with `retries=1`); success → `(info, sel, None, True)`. Unset → `ProbeError(AGE_RESTRICTED_NEEDS_SIGNIN)`.
2. Region-blocked → proxy loop as today; when a proxy's attempt fails age-gated: with a profile set, retry that same proxy signed in (success → `(info, sel, url, True)`); without one, stop the loop and raise `ProbeError(AGE_RESTRICTED_NEEDS_SIGNIN + " (also region-blocked; a proxy cleared that)", countries)`.
3. Signed-in retry failure → `ProbeError("Age-restricted — signed-in retry failed: <reason>")`, where `<reason>` is: `"the YTOP sign-in (<browser name>) could not be read on this computer"` when the exception is a cookie-loading failure (yt-dlp raises before any request: match `could not find .* cookies database|failed to decrypt|keyring|secretstorage` case-insensitively), `"YouTube asked to confirm it is not a bot"` for `is_bot_check(text)`, else `redact(text)[:80]` with any "Sign in to confirm" phrase removed. Show only the browser name (part before `:`), never the profile path.

- [ ] **Step 1: Failing tests** (names and core assertions)

```python
def test_age_gated_retries_signed_in(fake_analyse):  # fails direct with age gate, succeeds when active_signin()=="firefox"
    info, sel, proxy, signed_in = analyse_with_fallback("vid", Config(age_gate_browser="firefox"))
    assert (proxy, signed_in) == (None, True)
    assert fake_analyse.calls == [(None, None), (None, "firefox")]  # (active_proxy, active_signin)

def test_age_gated_without_profile_is_age_restricted(fake_analyse):
    with pytest.raises(ProbeError, match="^Age-restricted — needs the YTOP sign-in$"):
        analyse_with_fallback("vid", Config())
    assert len(fake_analyse.calls) == 1

def test_region_and_age_gated_retries_signed_in_through_the_proxy(fake_analyse):
    # direct: region-blocked (US); via proxy unsigned: age gate; via proxy + firefox: OK
    cfg = Config(age_gate_browser="firefox", proxies=(ProxyServer("https://us1.example:89", "US"),))
    _, _, proxy, signed_in = analyse_with_fallback("vid", cfg)
    assert proxy == "https://us1.example:89" and signed_in
    assert fake_analyse.calls[-1] == ("https://us1.example:89", "firefox")

def test_unreadable_profile_is_reported_not_raised(fake_analyse):  # signed-in attempt raises "could not find firefox cookies database in /home/x/.mozilla/..."
    item = probe_video("vid", Config(age_gate_browser="firefox:Secret Profile")).items[0]
    assert item.error_kind == "age_restricted"
    assert "could not be read on this computer" in item.error
    assert "Secret Profile" not in item.error and ".mozilla" not in item.error

def test_signed_in_bot_check_does_not_stop_the_run(fake_analyse):  # signed-in attempt: "Sign in to confirm you're not a bot"
    item = probe_video("vid", Config(age_gate_browser="firefox")).items[0]
    assert item.error_kind == "age_restricted" and not is_bot_check(item.error)

def test_plain_videos_never_signed_in(fake_analyse):  # direct succeeds
    assert analyse_with_fallback("vid", Config(age_gate_browser="firefox"))[3] is False
    assert fake_analyse.calls == [(None, None)]
```

Also one `probe_playlist` test: of two items, the age-gated one fails without a profile, the other still probes, and `result.bot_blocked` is False.

- [ ] **Step 2:** `TMPDIR=$PWD/.tmp uv run pytest tests/test_probe_signin.py -q` → FAIL.
- [ ] **Step 3:** Implement per the behaviour list.
- [ ] **Step 4:** `TMPDIR=$PWD/.tmp uv run pytest tests/test_probe_signin.py tests/test_proxies.py tests/test_jobs.py -q` → PASS.
- [ ] **Step 5:** Commit `feat: retry age-gated videos signed in with the age-gate profile`.

### Task 5: Download with the same sign-in

**Files:**
- Modify: `src/ytop/yt/download.py` (`_process_item`, `_failure_kind`)
- Test: `tests/test_download.py`

**Interfaces:**
- Consumes: `ProbedItem.signed_in`, `using_signin`, `Config.age_gate_browser`.
- `_process_item` enters `using_signin(config.age_gate_browser if item.signed_in and config.age_gate_browser else None)` inside the existing `using_proxy(item.proxy)`. `config` is `_process_item_body`'s 4th positional argument; take it from `args[2]` or make `config` an explicit parameter of `_process_item` and pass it through.
- `_failure_kind`: `"age_restricted"` passes through; a signed-in item is never `"youtube_changed"` (same rule as proxied).
- Transcripts: run inside the same context. If the captions path cannot use cookies, it fetches as today (left to the builder; note it in the commit message).

- [ ] **Step 1: Failing tests**

```python
def test_signed_in_item_downloads_inside_signin_and_proxy(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(download, "_process_item_body",
                        lambda item, *a, **k: seen.append((active_proxy(), active_signin())) or (None, {}))
    item = ProbedItem(1, "vid", "u", proxy="https://us1.example:89", signed_in=True)
    download._process_item(item, {}, layout, replace(Config(), age_gate_browser="firefox"), False, threading.Lock())
    assert seen == [("https://us1.example:89", "firefox")]

def test_unsigned_item_downloads_without_signin(...):  # signed_in=False → active_signin() is None

def test_signed_in_failure_is_not_youtube_changed():
    item = ProbedItem(1, "vid", "u", signed_in=True)
    assert download._failure_kind(DownloadError("nsig extraction failed"), item) is None
```

- [ ] **Step 2:** `TMPDIR=$PWD/.tmp uv run pytest tests/test_download.py -q` → new tests FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Same → PASS.
- [ ] **Step 5:** Commit `feat: download age-gated videos with the sign-in that read them`.

### Task 6: Settings section and reporting

**Files:**
- Modify: `src/ytop/web/app.py` (new `POST /settings/age-gate`; settings render context; `age_gate_set` alongside each `proxies_set`)
- Modify: `src/ytop/web/templates/settings.html` (section after `#proxies`)
- Modify: `src/ytop/web/templates/add.html`, `src/ytop/web/templates/playlist.html` (age-restricted line + link)
- Create: `src/ytop/yt/browsers.py` — `firefox_profiles() -> list[str]`
- Test: `tests/test_settings_web.py`, `tests/test_browsers.py` (new)

**Interfaces:**
- `firefox_profiles(home: Path | None = None) -> list[str]`: specs `firefox:<Path>` for each `[ProfileN]` in `profiles.ini` under `~/.config/mozilla/firefox/` then `~/.mozilla/firefox/` (Linux), `~/Library/Application Support/Firefox/` (macOS), `%APPDATA%/Mozilla/Firefox/` (Windows); `[]` when none. Reads only `profiles.ini`.
- Form field `browser` (text, `list=` a datalist of `["firefox", *firefox_profiles(), "brave", "chrome"]`), `clear=1` button. Save: `check_browser_spec`, reject control chars (reuse `_CONTROL_CHARS`), `save_config`, redirect `/settings#age-gate`.
- Section copy (heading "Age-restricted videos"): "When YouTube asks to confirm a viewer's age, {{ product_name }} tries the video once more signed in with this browser profile. Use a profile signed in only to an account made for {{ product_name }}, not your own." Current value shown as browser name + profile.
- Reporting: where the templates handle `region_blocked`, add `age_restricted`: show `item.error` / `entry.error`, and when `not age_gate_set` a link "Set up the age sign-in" to `/settings#age-gate`. Playlist page: errors starting with `"Age-restricted"` get the same treatment as `"Region-blocked"`.

- [ ] **Step 1: Failing tests**

```python
def test_firefox_profiles_reads_profiles_ini(tmp_path):
    ini = tmp_path / ".config/mozilla/firefox/profiles.ini"; ini.parent.mkdir(parents=True)
    ini.write_text("[Profile0]\nName=default-release\nPath=Q6uSzFvx.Profile 1\n[General]\n")
    assert firefox_profiles(tmp_path) == ["firefox:Q6uSzFvx.Profile 1"]

def test_settings_age_gate_saves_and_shows(client, library):
    r = client.post("/settings/age-gate", data={"browser": "firefox"}, headers=SAME_ORIGIN, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings#age-gate"
    assert load_config(library).age_gate_browser == "firefox"
    assert 'id="age-gate"' in client.get("/settings").text

def test_settings_age_gate_rejects_unknown_browser(client, library):
    r = client.post("/settings/age-gate", data={"browser": "netscape"}, headers=SAME_ORIGIN)
    assert r.status_code == 400 and "unknown browser" in r.text

def test_settings_age_gate_clear(client, library): ...  # clear=1 → ""

def test_add_page_links_age_gate_settings_when_unset(...):  # an item with error_kind "age_restricted" renders href="/settings#age-gate"
```

Use the existing fixtures/helpers in `tests/test_settings_web.py` for the client, library and same-origin headers.

- [ ] **Step 2:** `TMPDIR=$PWD/.tmp uv run pytest tests/test_settings_web.py tests/test_browsers.py -q` → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Same → PASS, then the full suite `TMPDIR=$PWD/.tmp uv run pytest -q` (check the exit code, not a tail; `test_interrupted_prune` is a known order-dependent failure on base).
- [ ] **Step 5:** README: one sentence in the Settings/sign-in part naming the "Age-restricted videos" setting and that it should be a dedicated account. Commit `feat: Settings section and job/playlist reporting for age-restricted videos`.

### Task 7: Acceptance on the stick (owner-approved live run)

Only after Tasks 2–6 are merged into the branch, the full suite is green, and ≥30 min since the last full-playlist run.

- [ ] **Step 1:** Save `age_gate_browser = "firefox"` into the stick's config via `save_config(root, replace(load_config(root), age_gate_browser="firefox"))` (back up `config.toml` to the scratchpad first).
- [ ] **Step 2:** `uv run ytop fetch PL30RAv-0lkxG17cdEX9qM5Nnl9eSvfjgR --library /run/media/neal/USB321FD > <scratchpad>/run5.log 2>&1` (signed out session; only age-gated videos sign in).
- [ ] **Step 3: Success:** the 8 age-gated videos (6 direct + `jLBFp2UCACk`, `uxggN_9EPqc` via the US proxy) download; no item fails as age-restricted; already-present videos are kept; proxy password count in the log is 0; no bot-check stop. Report counts to the owner.
