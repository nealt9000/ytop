# Quality Tiers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Offer 480p/720p/1080p/1440p/2160p caps between Efficient and Native on the download review, with per-option sizes, a fits marker, and per-playlist memory of the choice.

**Architecture:** Tiers reuse the existing `max_height` cap (`select_video` in `src/ytop/yt/formats.py`). The review (`_compact_probe` in `src/ytop/web/jobs.py`) computes each tier's total from already-probed formats; the job policy string becomes `"efficient" | "native" | "<H>p"`; `download_probe` stores the chosen policy in the playlist's `options["quality"]` and the next review preselects it.

**Tech Stack:** Python 3, FastAPI + Jinja2 templates, pytest (run with `TMPDIR` set per the host notes, e.g. `TMPDIR=$PWD/.pytest-tmp uv run pytest`).

**Spec:** `docs/superpowers/specs/2026-11-19-quality-tiers-design.md`

## Global Constraints

- Tiers are exactly `(480, 720, 1080, 1440, 2160)`; policy strings `"480p"`…`"2160p"`.
- Efficient stays the floor option and the default for a playlist with no stored choice; Native stays the top option.
- A tier is offered only when its total is strictly smaller than the Native total.
- No re-encoding; no CLI changes; no "fit to drive" option.
- Existing persisted jobs with `policy` `"efficient"`/`"native"` keep working.
- Sizes are shown with the existing `human_bytes` (GiB units).

## Review Focus

1. **Unknown policy from a forged/old form** (`policy="999p"`, `"720"`) → `start_download` raises `ValueError` (route returns 409), never downloads at native. Test in Task 2.
2. **Playlist whose max height is 720p** → no 720p/1080p/1440p/2160p options (equal to Native), only Efficient, 480p (if smaller), Native. Test in Task 1.
3. **Stored `options["quality"]` that is no longer offered** → a tier at or above the playlist's tallest native height preselects Native (amended after final review); garbage or non-string values preselect Efficient. Test in Task 3.
4. **Single-video add into an existing playlist** must not overwrite that playlist's stored quality. Test in Task 2.
5. **Destination folder not yet created** (new playlist) → free space read from the nearest existing ancestor, not an error/None. Test in Task 1.

---

### Task 1: Tier estimates and free space in the review

**Files:**
- Modify: `src/ytop/fsinfo.py` (add `free_bytes`)
- Modify: `src/ytop/web/jobs.py` — `_compact_probe` (~L273), `Job` dataclass (~L159–206), both review `_update` calls (~L1259 video, ~L1361 playlist)
- Test: `tests/test_jobs.py`, `tests/test_fsinfo.py`

**Interfaces:**
- Produces: `fsinfo.free_bytes(path: Path) -> int | None` — `shutil.disk_usage` of `path` or its nearest existing ancestor; `None` on `OSError`.
- Produces: `jobs.QUALITY_TIERS = (480, 720, 1080, 1440, 2160)`.
- Produces: `_compact_probe(...)` result gains `"estimate_tiers": dict[str, int]` (keys `"480p"`…, only tiers with total < native total) and `"free_bytes": int | None` (from `destination`, `None` when destination is `None`).
- Produces: `Job.estimate_tiers: dict[str, int] = {}`, `Job.free_bytes: int | None = None`, `Job.suggested_policy: str = "efficient"` (set in Task 3), and property `Job.quality_options -> list[dict]` with keys `policy, label, bytes, size_label, fits` where `fits` is `None` when `free_bytes` is `None` or `in_library` is true, else `bytes <= free_bytes`. Order: efficient, tiers ascending, native. Labels: `"Efficient"`, `"720p max"`, `"Native"`.
- Produces: `Job.policy_label` returns `"720p max"` for tier policies (existing efficient/native text unchanged).

- [ ] **Step 1: Write failing tests**

```python
# tests/test_fsinfo.py
def test_free_bytes_walks_up_to_an_existing_folder(tmp_path):
    from ytop.fsinfo import free_bytes
    assert free_bytes(tmp_path / "not" / "yet") == free_bytes(tmp_path) > 0

# tests/test_jobs.py  (build infos with tests/test_formats.py-style video()/audio()
# dicts carrying "filesize"; item A offers 360/720/1080, item B offers 360/720)
def test_review_offers_only_tiers_smaller_than_native():
    review = jobs_module._compact_probe(probe, Config(), False, None)
    assert set(review["estimate_tiers"]) == {"480p", "720p"}
    assert review["estimate_tiers"]["720p"] < review["estimate_native_bytes"]
    assert review["free_bytes"] is None

def test_review_has_no_tiers_when_playlist_tops_out_at_720p():
    # both items max 720p, 360p also offered
    assert set(review["estimate_tiers"]) == {"480p"}

def test_quality_options_order_labels_and_fits():
    job = Job(id="j", estimate_bytes=10, estimate_native_bytes=100,
              estimate_tiers={"720p": 50, "480p": 30}, free_bytes=40)
    opts = job.quality_options
    assert [o["policy"] for o in opts] == ["efficient", "480p", "720p", "native"]
    assert [o["label"] for o in opts] == ["Efficient", "480p max", "720p max", "Native"]
    assert [o["fits"] for o in opts] == [True, True, False, False]
    job.in_library = True
    assert all(o["fits"] is None for o in job.quality_options)
```

(480p appears in the first test because item A's 480p cap falls back to 360p, which is smaller than native.)

- [ ] **Step 2: Run** `TMPDIR=$PWD/.pytest-tmp uv run pytest tests/test_jobs.py tests/test_fsinfo.py -k "tier or free_bytes or quality_options" -v` — expect FAIL (missing names).

- [ ] **Step 3: Implement** `free_bytes`, `QUALITY_TIERS`, the `_compact_probe` additions (per item per tier: `select_format(item.info, config.audio_language, H, preserve_audio).estimated_bytes`, skipping items whose `info` is None or raise; sum per tier; drop tiers with total `>= native_total`), the new `Job` fields/properties, and pass `estimate_tiers`/`free_bytes` into both review `_update` calls.

- [ ] **Step 4: Run** the same command plus the full `tests/test_jobs.py` — expect PASS.

- [ ] **Step 5: Commit** `feat: estimate fixed resolution tiers on the review`

---

### Task 2: Download with a tier policy and remember it

**Files:**
- Modify: `src/ytop/web/jobs.py` — `start_download` (~L1825–1877), main download call (playlist branch passing `download_probe` args)
- Modify: `src/ytop/yt/download.py` — `download_probe` signature (~L613) and options block (~L662)
- Test: `tests/test_jobs.py`, `tests/test_download.py`

**Interfaces:**
- Consumes: `QUALITY_TIERS` (Task 1).
- Produces: `jobs.policy_settings(policy: str, config: Config) -> tuple[float, int]` returning `(target_mb_per_hour, max_height)`: efficient → `(config.target_mb_per_hour, config.max_height)`, native → `(0.0, 0)`, `"<H>p"` with H in `QUALITY_TIERS` → `(0.0, H)`; anything else → `ValueError("unknown download quality: ...")`. `start_download` uses it in place of the current two-value check.
- Produces: `download_probe(..., quality: str | None = None)`; when not None, sets `playlist_options["quality"] = quality` before publishing. Jobs pass `quality=job.policy` for playlist jobs and `None` for `kind == "video"`.

- [ ] **Step 1: Write failing tests**

```python
def test_policy_settings():
    cfg = Config()
    assert policy_settings("720p", cfg) == (0.0, 720)
    assert policy_settings("native", cfg) == (0.0, 0)
    assert policy_settings("efficient", cfg) == (cfg.target_mb_per_hour, cfg.max_height)
    for bad in ("999p", "720", "", "cap"):
        with pytest.raises(ValueError):
            policy_settings(bad, cfg)

def test_start_download_with_a_tier(tmp_path, monkeypatch):
    # fake_probe + fake download capturing kwargs, as in
    # test_a_cancelled_job_can_be_resumed
    manager.start_download(job.id, policy="720p")
    job = wait_for(manager, job.id, {"done"})
    assert (job.policy, job.max_height, job.target_mb_per_hour) == ("720p", 720, 0)
    assert captured["max_height"] == 720 and captured["quality"] == "720p"
    assert job.policy_label == "720p max"

def test_start_download_rejects_unknown_tier(...):
    with pytest.raises(ValueError):
        manager.start_download(job.id, policy="999p")

def test_video_job_does_not_pass_quality(...):
    # video job via fake_probe_video; captured["quality"] is None

# tests/test_download.py — following existing download_probe tests there
def test_download_probe_records_quality_and_keeps_it_when_none(...):
    # first call quality="720p" → playlist.json options["quality"] == "720p"
    # second call quality=None → still "720p"; preserve_audio option unchanged
```

- [ ] **Step 2: Run** `TMPDIR=$PWD/.pytest-tmp uv run pytest tests/test_jobs.py tests/test_download.py -k "policy or tier or quality" -v` — expect FAIL.

- [ ] **Step 3: Implement** `policy_settings`, use it in `start_download`, add and thread the `quality` parameter.

- [ ] **Step 4: Run** full `tests/test_jobs.py tests/test_download.py` — expect PASS.

- [ ] **Step 5: Commit** `feat: download at a fixed resolution cap and remember it per playlist`

---

### Task 3: Review page choices and preselection

**Files:**
- Modify: `src/ytop/web/jobs.py` — playlist review (~L1342: read `resolved[2]["options"].get("quality")` into `suggested_policy`)
- Modify: `src/ytop/web/templates/add.html` (~L85–95 size cards, ~L124–134 radios; add a sentence to the explainer that fixed caps are available)
- Test: `tests/test_jobs.py`, `tests/test_web.py`

**Interfaces:**
- Consumes: `Job.quality_options`, `Job.suggested_policy`, `Job.free_bytes` (Task 1); policy strings accepted by `start_download` (Task 2). The `/jobs/{id}/download` route already forwards `policy` unchanged.
- Produces: review sets `suggested_policy` to the stored quality only if it is one of the job's offered `quality_options` policies, else `"efficient"`.

- [ ] **Step 1: Write failing tests**

```python
def test_review_preselects_the_stored_quality(tmp_path, monkeypatch):
    # playlist.json options {"quality": "native"} (pattern of
    # test_a_new_probe_inherits_the_playlist_audio_policy)
    assert job.suggested_policy == "native"

def test_review_ignores_a_stored_quality_that_is_not_offered(...):
    # options {"quality": "1080p"} with fake_probe (240p only) → "efficient"
    # options {"quality": "bogus"} → "efficient"

# tests/test_web.py — extend a ready-review flow
def test_review_page_lists_tiers_with_fits_marker(client, monkeypatch):
    page = client.get(f"/add/{job_id}").text
    assert 'value="720p"' in page and "720p max" in page
    assert "fits" in page and "free" in page
    assert 'value="efficient" checked' in page
```

- [ ] **Step 2: Run** `TMPDIR=$PWD/.pytest-tmp uv run pytest tests/test_jobs.py tests/test_web.py -k "quality or tiers" -v` — expect FAIL.

- [ ] **Step 3: Implement.** Replace the two hard-coded radios with a loop over `job.quality_options`; each label reads `<strong>{label}</strong> — {size_label}` then `✓ fits` / `✗ doesn't fit` with `({free_bytes|bytes} free)` when `fits` is not None; for in-library jobs show only `({free} free)` when known. Check the radio whose policy equals `job.suggested_policy`. Replace the two size cards with the same options list (or drop them in favour of the radios — implementer's call, keep one place showing sizes).

- [ ] **Step 4: Run** the full suite `TMPDIR=$PWD/.pytest-tmp uv run pytest -q` — expect PASS. Then launch the app (time via `/healthz`) and look at a review page in the browser.

- [ ] **Step 5: Commit** `feat: offer resolution tiers on the download review`
