# Library Page Three-Column Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lay the library home page (`/`) out in three columns, like YouTube.
- **Left:** the connected libraries.
- **Middle:** a header, a filter box, a single scrolling category chip strip,
  and one playlist grid.
- **Right:** a vertical Continue watching list.

**Architecture:** The page stays server-rendered with Jinja. `build_home` in
`browse.py` gains the data for the sidebar, the header, the New badges and the
chip order. `library.html` is re-laid out. `browse.js` swaps its multi-row chip
filters for a chip strip plus a text filter, using the same in-browser
filtering of `data-*` attributes. `app.css` gets the grid layout.

**Tech Stack:** Python 3 / FastAPI / Jinja2, vanilla ES modules, plain CSS,
pytest (TestClient), and Playwright MCP for the visual check.

**Spec:** `docs/superpowers/specs/2026-11-24-library-columns-design.md`. Read
it before starting any task.

## Global Constraints

- No new dependencies and no JS framework. Filtering stays in the browser and
  never reloads the page.
- `build_home` must call `_load` (an index read) at most once per connected
  library per call.
- The sidebar lists only connected libraries (`lib.libraries.libraries()`).
- The chip strip is one line that scrolls sideways and never wraps. It has no
  "More" button.
- Chip order: **All**, **Not started**, then categories by playlist count,
  most first, ties broken alphabetically.
- Category chips are single-select. **Not started** is an independent toggle.
  **All** clears both.
- The filter box matches title, channel, category, tag labels and library
  label. It is case-insensitive, matches substrings, and requires all words
  (AND). It is not persisted.
- The filter box and chips filter only the middle grid. Continue watching is
  never filtered.
- The New badge goes on the `CARD_LIMIT` (12) most recently updated unstarted
  playlists.
- "All libraries" appears only with two or more libraries connected. With one
  library, `/` is that library's own view (its label as the h1, Edit if
  writable).
- Edit goes only in a single, writable library's header. It reuses
  `POST /storage/{id}/label` with `next=/?library=<id>`.
- Run pytest as `TMPDIR=/home/neal/Projects/ytop/.tools/tmp uv run pytest …`.
  `/tmp` is a small tmpfs.
- Run probes and demo servers only inside pytest, or against a scratch library
  whose `.ytop/config.toml` has `[paths]\ndetect_removable = false`. Never let
  a dev build touch a real stick.
- Commits use the noreply author already configured. Commit on
  `feat/library-columns`. Don't push.

## Review Focus

1. **One library connected, at `/`:** a reasonable person expects that
   library's label as the h1, an Edit button, its sidebar entry marked
   current, and no "All libraries" entry. Pinned in Task 2,
   `test_single_library_header_is_that_library`.
2. **An unknown `?library=` id:** a reasonable person expects it to fall back
   to the combined view (two libraries) without crashing, with no sidebar
   entry marked current except "All libraries". Pinned in Task 2,
   `test_unknown_library_falls_back_to_all`.
3. **No playlists in progress:** the Continue watching column is absent
   rather than an empty box. Pinned in Task 2,
   `test_continue_column_absent_when_nothing_in_progress`.
4. **Hostile labels in the new places** (categories in chips, tag labels in
   `data-tag-labels`, library labels in the sidebar): they must be escaped.
   Pinned in Task 2 by extending `test_home_escapes_tag_labels`.
5. **Playlists with no category:** they never get a chip, stay visible under
   **All**, and show under **Not started** when unstarted. Pinned in Task 1
   (`category_counts` skips `""`) and checked in Task 5's browser script.

---

### Task 1: View model — sidebar, header, New keys, category counts

**Files:**
- Modify: `src/ytop/web/browse.py` (`HomeView`, `_load_all`, `_build_cards`,
  `build_home`, new `_combined`)
- Test: `tests/test_browse.py`

**Interfaces:**
- Produces, as new `HomeView` fields (the old fields stay until Task 2):
  - `libraries: list[LibrarySummary]`: one per connected library, in
    `lib.libraries.libraries()` order, whichever library is selected.
  - `header: LibrarySummary`: the shown library's summary when one library is
    shown, otherwise `_combined(libraries)`. Combined is recognised by
    `header.id == ""`.
  - `new_keys: set[str]`: card keys of the first `CARD_LIMIT` unstarted
    playlists, newest `updated_at` first. This is exactly today's `start_new`
    selection.
  - `category_counts: list[tuple[str, int]]`: playlists per non-empty category
    in `all_playlists`, sorted by `(-count, name.casefold())`.
- Produces: `_combined(summaries: list[LibrarySummary]) -> LibrarySummary`,
  with `id=""`, `label="All libraries"`, `description=""`, `read_only=False`,
  and every numeric field summed (`video_count` summed too, so duplicates
  across sticks count twice). `top_categories` and `top_channels` are `[]`.

- [ ] **Step 1: Write the failing tests** (in `tests/test_browse.py`, using the
  existing `_lib` and `_two_libraries` helpers)

```python
def test_home_lists_every_library_when_one_selected(tmp_path):
    lib, _first, second, _ = _two_libraries(tmp_path)
    second_id = read_library(second.root).id
    home = build_home(lib, second_id)
    assert [s.label for s in home.libraries] == ["First", "Second"]
    assert home.header.id == second_id
    assert {c.library_id for c in home.all_playlists} == {second_id}

def test_home_header_combines_libraries(tmp_path):
    lib, *_ = _two_libraries(tmp_path)
    home = build_home(lib)
    assert home.header.id == "" and home.header.label == "All libraries"
    assert home.header.playlist_count == sum(s.playlist_count for s in home.libraries) == 2
    assert home.header.used_bytes == sum(s.used_bytes for s in home.libraries)

def test_home_header_single_library_is_that_library(tmp_path):
    lib, layout = _lib(tmp_path)
    home = build_home(lib)
    assert home.header.id == read_library(layout.root).id
    assert len(home.libraries) == 1

def test_home_loads_each_library_once(tmp_path, monkeypatch):
    import ytop.web.browse as browse
    lib, _first, second, _ = _two_libraries(tmp_path)
    calls = []
    real = browse._load
    monkeypatch.setattr(browse, "_load", lambda l, ref: calls.append(ref.id) or real(l, ref))
    build_home(lib, read_library(second.root).id)
    assert len(calls) == 2

def test_new_keys_are_recent_unstarted(tmp_path):
    lib, layout = _lib(tmp_path)
    ProgressStore(layout).update("v2", 30, duration=60)
    assert build_home(lib).new_keys == {"P2"}
    for n in range(CARD_LIMIT + 2):
        add_playlist(layout, f"N{n}", f"New {n}", [(f"n{n}", "X")])
    assert len(build_home(lib).new_keys) == CARD_LIMIT

def test_category_counts_order(tmp_path):
    lib, layout = _lib(tmp_path)
    add_playlist(layout, "P3", "Three", [("x1", "X")])
    store = TagStore(layout)
    store.set_category("P1", "Music")
    store.set_category("P2", "Cooking")
    store.set_category("P3", "Cooking")
    assert build_home(lib).category_counts == [("Cooking", 2), ("Music", 1)]
```

  Import `CARD_LIMIT` from `ytop.web.browse`.

- [ ] **Step 2: Run them and confirm they fail**

  Run: `TMPDIR=/home/neal/Projects/ytop/.tools/tmp uv run pytest tests/test_browse.py -k "every_library or header or loads_each or new_keys or category_counts" -v`

  Expected: FAIL with `AttributeError: 'HomeView' object has no attribute 'libraries'`.

- [ ] **Step 3: Implement**

  - `_load_all` already loads every library. Make it return the full list as
    well, i.e. `tuple[list[_Loaded], list[_Loaded], Counter]` as
    `(shown, everything, counts)`, and update `storage_usage` and
    `_build_cards` to match.
  - `build_home` builds cards only for `shown`, and builds summaries from
    `everything`.
  - `_summary(item)` must work for a library without cards: its `top_*` comes
    out empty, and that's fine.
  - The header is `_summary(shown[0])` when `len(shown) == 1`, otherwise
    `_combined(libraries)`.

- [ ] **Step 4: Run `tests/test_browse.py` in full and confirm it passes**,
  including the existing `test_home_5000_videos_under_300ms`.

- [ ] **Step 5: Commit** with the message `feat(home): view data for library sidebar, header, New badges, chip order`.

---

### Task 2: Page markup — three columns, header, chip strip, Continue column

**Files:**
- Modify: `src/ytop/web/templates/library.html` (rewrite the content block)
- Modify: `src/ytop/web/templates/_cards.html`:
  - add the `data-started`, `data-tag-labels` and `data-new` attributes to
    `playlist_card`
  - add the New badge
  - add the new `continue_row` macro
- Modify: `src/ytop/web/app.py` `library_page` (drop the `switcher` and
  `several` arguments, add a body class)
- Modify: `src/ytop/web/browse.py`:
  - remove `HomeView.summaries`, `start_new`, `categories`, `tags` and
    `channels`
  - remove `LibrarySummary.top_categories` and `top_channels`, plus `_top` and
    `TOP_LIMIT` if they become unused
- Test: `tests/test_browse_page.py`, `tests/test_browse.py`,
  `tests/test_ui_consistency.py`

**Interfaces:**
- Consumes: Task 1's `HomeView.libraries`, `header`, `new_keys` and
  `category_counts`.
- Produces: the DOM contract that Task 3 (JS) and Task 4 (CSS) rely on.

| Element | Selector / attributes |
|---|---|
| Layout wrapper | `div.home-layout` containing `nav.home-libraries`, `div.home-main`, `aside.home-continue` (the aside is omitted when `view.continue_watching` is empty) |
| Library link | `a.library-link[href="/?library=<id>"]`, or `href="/"` for "All libraries". The current one has `aria-current="page"`. The second line is `span.library-link-meta` ("N playlists · X free") |
| Header | `header.home-header`, with `h1`, an optional `.tag` "Read-only", `p.summary-description`, `p.home-stats`, and `details.summary-edit` (same form fields as today) |
| Filter box | `input#browse-filter[type=search]`, placeholder `Filter this library…`, labelled via `aria-label="Filter playlists"` |
| Chip strip | `div.chip-strip#browse-chips[role=group][aria-label="Filter by category"]`. It contains `button.chip[data-chip="all"][aria-pressed="true"]`, `button.chip[data-chip="not-started"][aria-pressed="false"]`, then one `button.chip[data-chip="category"][data-value="<name>"][aria-pressed="false"]` per `category_counts` entry, with the count in `span.chip-count` |
| Card | `article.playlist-card` gains `data-started="0\|1"`, `data-new` (present only when the key is in `new_keys`) and `data-tag-labels="<labels joined by \|>"`. A New card shows `span.new-badge` "New" inside `.playlist-card-art` |
| Continue row | `continue_row(card, multi)` macro: `li.continue-row > a[href=card.next_url]` holding the thumb (`img` or placeholder initial), `span.thumb-progress > span[style="width: P%"]` where P is `card.progress * 100`, the title, `next_label · next_title`, and the library label when `multi`. The rows sit inside `ol.continue-list` |

  - The grid section keeps `#browse`, `#browse-results`, `#browse-count`,
    `#browse-empty`, the view toggle, `#browse-group`, `#browse-sort` and
    "Manage tags".
  - The "Library" group option shows when `view.libraries|length > 1`.
  - `<h1 class="sr-only">Library</h1>` goes away, because the header always
    has the h1.
  - The empty state (no playlists) renders inside `.home-main`, below the
    header. The sidebar still shows.
  - `library_page` passes `body_class="home"`. `base.html` already has the
    `body_class` block, so `library.html` sets `{% block body_class %}home{% endblock %}`
    instead. In that case no route change is needed beyond removing
    `switcher` and `several`.

- [ ] **Step 1: Rewrite the failing tests in `tests/test_browse_page.py`**

  Replace `test_home_sections_present`,
  `test_library_switcher_only_with_two_libraries`, and the summary assertions
  in `test_read_only_library_has_no_edit_button`. Keep
  `test_summary_edit_redirects_back` as is: the form fields are unchanged.

```python
def test_home_sections_present(tmp_path):
    layout = _layout(tmp_path)
    ProgressStore(layout).update("v1", 60, duration=60, watched=True)
    ProgressStore(layout).update("v2", 30, duration=60)
    client = _client(layout)
    main = _main(client.get("/").text)
    assert 'class="home-libraries"' in main and 'class="home-continue"' in main
    assert "Start something new" not in main and "Browse all" not in main
    continue_part = main.split('class="home-continue"')[1]
    assert "Video 2 of 3" in continue_part and 'href="/watch/P1/v2"' in continue_part
    assert 'id="browse-filter"' in main and 'data-chip="not-started"' in main
    assert re.search(r'data-key="P2"[^>]*data-started="0"[^>]*data-new', main)
    assert re.search(r'data-key="P1"[^>]*data-started="1"', main)
    client.app.state.jobs.close()

def test_continue_column_absent_when_nothing_in_progress(tmp_path):
    client = _client(_layout(tmp_path))
    assert 'class="home-continue"' not in client.get("/").text
    client.app.state.jobs.close()

def test_single_library_header_is_that_library(tmp_path):
    layout = _layout(tmp_path)
    library_id = read_library(layout.root).id
    main = _main(_client(layout).get("/").text)
    assert "All libraries" not in main
    assert re.search(r"<h1[^>]*>\s*" + re.escape(read_library(layout.root).label), main)
    assert re.search(rf'href="/\?library={library_id}"[^>]*aria-current="page"', main)
    assert f'action="/storage/{library_id}/label"' in main
    # close the client's jobs as the other tests do

def test_two_libraries_sidebar_and_combined_header(tmp_path):
    layout = _layout(tmp_path)
    libs = library_set(layout)
    client = _client(layout, libs)
    usb_root = tmp_path / "usb" / "YTOP"
    init_library(usb_root, "Blue stick")
    add_playlist(LibraryLayout(usb_root), "PU", "Stick list", [("u1", "U")])
    libs.add_folder(usb_root, "Blue stick")
    usb_id = read_library(usb_root).id
    text = _main(client.get("/").text)
    assert re.search(r'<a[^>]*class="library-link"[^>]*href="/"[^>]*aria-current="page"', text)
    assert "Blue stick" in text.split('class="home-libraries"')[1].split("</nav>")[0]
    assert re.search(r"<h1[^>]*>\s*All libraries", text)
    assert "/label\"" not in text  # no Edit in the combined view
    only = _main(client.get("/", params={"library": usb_id}).text)
    assert re.search(rf'href="/\?library={usb_id}"[^>]*aria-current="page"', only)
    assert "Stick list" in only and ">One<" not in only
    assert "All libraries" in only  # still in the sidebar

def test_unknown_library_falls_back_to_all(tmp_path):
    # Same two-library setup as the test above (factor it into a helper).
    main = _main(client.get("/", params={"library": "nope"}).text)
    assert re.search(r"<h1[^>]*>\s*All libraries", main)
```

  - Attribute order inside `<article>` is up to the builder. Write the
    regexes to match whatever order the macro uses, but keep the assertions.
  - Extend `test_home_escapes_tag_labels`: assert that `<script>alert` appears
    nowhere in the page. The evil label already flows into the category, so
    this now covers the chip and `data-tag-labels`.
  - In `test_read_only_library_has_no_edit_button`, keep the `'/label"' not in`
    assertion.
  - In `tests/test_ui_consistency.py`, replace
    `test_long_chip_row_renders_more_toggle` with
    `test_chip_strip_has_no_more_toggle`. Use the same 15-category setup as
    the old test, but via `set_category`. Assert there are 15
    `data-chip="category"` buttons and that `chip-more` does not appear.
  - In `tests/test_browse.py`:
    - drop the `start_new` assertions from
      `test_continue_and_start_new_split` and
      `test_fully_watched_playlist_is_in_neither_list`, and assert `new_keys`
      instead (`{"P2"}` and `{"P1"}`)
    - change `test_unplugged_library_absent` and
      `test_summary_counts_shared_media_once` to use `home.libraries`

- [ ] **Step 2: Run the three test files and confirm the new tests fail**

  Run: `TMPDIR=/home/neal/Projects/ytop/.tools/tmp uv run pytest tests/test_browse_page.py tests/test_browse.py tests/test_ui_consistency.py -v`

  Expected: the new and rewritten tests FAIL. The others pass.

- [ ] **Step 3: Implement the template, macros, view-model removals and route change** to the contract above.

- [ ] **Step 4: Run the same three files, then the full suite, and confirm all pass**

  Run: `TMPDIR=/home/neal/Projects/ytop/.tools/tmp uv run pytest -q`

  Expected: all pass. The page is unstyled and the chips are inert until
  Tasks 3–4, which is fine.

- [ ] **Step 5: Commit** with the message `feat(home): three-column library page markup`.

---

### Task 3: browse.js — chip strip and filter box

**Files:**
- Modify: `src/ytop/web/static/js/browse.js`

**Interfaces:**
- Consumes: Task 2's DOM contract.
- Produces: no exports beyond `initBrowse()`, which is unchanged.

There is no JS test runner in this repo. Behaviour is verified in a browser in
Task 5, and pytest still guards the storage rule (`test_browse_js_wraps_storage`).

- [ ] **Step 1: Replace the filtering state and `matches(card)`**

  The new state is `{ category: string | null, notStarted: boolean, words:
  string[] }`. A card matches when all three hold:
  - `category` is null, or equals `card.dataset.category`.
  - `notStarted` is false, or `card.dataset.started === "0"`.
  - Every word is a substring of the card's haystack. The haystack is built
    once per card and cached: title, channel, category, tag labels and
    library label, joined and lowercased with
    `toLocaleLowerCase()`.

  Words come from the filter value, trimmed, lowercased and split on
  whitespace, with empty strings dropped.

- [ ] **Step 2: Wire up the controls**

  - A chip click (delegated on `#browse-chips`):
    - `all` clears category and notStarted.
    - `not-started` toggles notStarted.
    - `category` selects that value, or clears it if it's already selected.
  - After every click, sync `aria-pressed` on all chips. "All" is pressed
    exactly when category is null and notStarted is false.
  - The `input` event on `#browse-filter` updates the words and calls
    `render()`.
  - Neither control is persisted.

- [ ] **Step 3: Remove the code that no longer applies**

  - the `FACETS` and `selected` sets
  - `chipRows`, `syncRow` and the More/Fewer logic
  - the `others` loop that hid cards in other sections
  - the tag-label map built from the chip row. Build it instead from each
    card's `data-tags` paired with `data-tag-labels` (same order, split on
    `|`), so that Group by → Tag keeps its labels.

  Keep `initSlashSearch`, the view, group and sort handling, the count text
  and the empty message as they are.

- [ ] **Step 4: Run the full test suite and confirm it passes**

  Run: `TMPDIR=/home/neal/Projects/ytop/.tools/tmp uv run pytest -q`

  Expected: all pass, including `test_browse_js_wraps_storage`.

- [ ] **Step 5: Commit** with the message `feat(home): chip strip and filter box replace chip rows`.

---

### Task 4: CSS — layout, sidebar, chip strip, Continue rows

**Files:**
- Modify: `src/ytop/web/static/app.css`

**Interfaces:**
- Consumes: Task 2's class names, and `body.home`.

- [ ] **Step 1: Write the layout rules** (exact sizes are the builder's call
  within these limits)

  - **The grid:**
    - `body.home .page` widens past 1440px (cap around 1920px).
    - `.home-layout` is a grid of `~220px | minmax(0,1fr) | ~340px`.
    - When `.home-continue` is absent, the middle column takes the freed
      space. Use `:has()` or a modifier class from the template.
  - **The side columns:** `.home-libraries` and `.home-continue` are
    `position: sticky`, with `top` set to the topbar height,
    `max-height: calc(100vh - top)` and `overflow-y: auto`.
  - **Library links:**
    - The `.library-link` block shows two lines.
    - The `aria-current="page"` link uses the accent tint (the existing
      `--accent`/red family tokens in `app.css`).
  - **The chip strip:**
    - `.chip-strip` uses `display: flex`, `flex-wrap: nowrap`,
      `overflow-x: auto`, `scrollbar-width: thin` and an edge fade via
      `mask-image`.
    - `.chip[aria-pressed="true"]` gets a filled style.
  - **Continue rows:**
    - `.continue-row` shows a ~120px 16:9 thumbnail on the left and two text
      lines, clamped.
    - `.thumb-progress` is a red bar along the thumbnail's bottom edge. Reuse
      the existing `.thumb-progress` rule from the video cards if it fits.
  - **The New badge:** `.new-badge` sits top-left on the card art in the
    brand red.
  - **The header:** `.home-header` is compact, with the stats line muted.
- [ ] **Step 2: Write the narrow-window rules**
  - At `max-width: ~1100px`:
    - `.home-continue` moves above the grid in the middle column (grid areas)
      and stops being sticky.
    - The list shows its first 4 rows, with the rest hidden behind a native
      `<details>` "Show all" or a `max-height` with scroll. Builder's call;
      if a toggle is needed, add it to the template.
  - At `max-width: ~800px`:
    - `.home-libraries` becomes a single horizontal scrolling row of links
      above the header. The spec allows a `<select>` or a row of links; a row
      needs no JS.
    - Nothing may cause horizontal page scroll.

- [ ] **Step 3: Delete the dead rules**

  Remove `.library-switcher*`, `.library-summaries`, `.library-summary*`,
  `.summary-head`, `.summary-title`, `.summary-stats`, `.summary-tops`,
  `.summary-top*`, `.card-rail`, `.chip-filters`, `.chip-filter-*`,
  `.chip-scroll` and `.chip-more`.

  Before removing each one, check that it isn't used elsewhere:

  ```bash
  grep -rn "<class>" src/ytop/web/templates src/ytop/web/static/js
  ```

  Keep `.summary-edit*` and `.summary-description`, which are still used.

- [ ] **Step 4: Run the full suite and confirm it passes**

  Run: `TMPDIR=/home/neal/Projects/ytop/.tools/tmp uv run pytest -q`

  Expected: all pass (`tests/test_static_cache.py` covers the static asset
  hashing).

- [ ] **Step 5: Commit** with the message `style(home): three-column layout, sidebar, chip strip, continue list`.

---

### Task 5: Visual and behaviour check in a real browser

**Files:** none, unless fixes are needed (commit fixes under the task they
belong to).

- [ ] **Step 1: Build two scratch libraries with fake data**

  Do this inside a throwaway pytest test, or a script in the scratchpad that
  imports `tests/factories.py` with `sys.path`. Put them in the session
  scratchpad, not `/tmp`.
  - About 40 playlists across 6 categories, some with no category.
  - Several started playlists and several fully watched ones.
  - A second library with about 5 playlists.

  Write `[paths]\ndetect_removable = false` into each library's
  `.ytop/config.toml`. Then start `uv run ytop serve --library <first>` in
  the background and register the second through `/storage` → Add folder.

- [ ] **Step 2: Check with Playwright MCP at 1440×900, and take a screenshot**
  - The three columns are visible.
  - Sidebar: "All libraries" is current, and both libraries are listed with
    their counts.
  - The header h1 reads "All libraries" with the combined stats.
  - The chip strip is one line and scrolls sideways.
  - New badges appear on at most 12 cards.

- [ ] **Step 3: Check the behaviour with `browser_evaluate` or clicks**
  - Typing a channel name narrows the grid, and the count shows "N of M".
  - Two words, one matching the title and one the channel, still match.
  - A category chip filters, and clicking it again restores All.
  - "Not started" combined with a category intersects the two.
  - "All" resets both.
  - Group by Tag still shows tag labels, not keys.
  - Continue watching never changes while filtering.
  - Clicking a library in the sidebar shows its own header with Edit.

- [ ] **Step 4: Resize to 1000×800 and 760×800, and take screenshots**
  - At 1000px, Continue watching sits above the grid.
  - At 760px, the libraries form a row above the header.
  - At both widths, `document.documentElement.scrollWidth <= innerWidth`.

- [ ] **Step 5: Stop the server.** Show the owner the screenshots. Run the
  full suite one last time:

  `TMPDIR=/home/neal/Projects/ytop/.tools/tmp uv run pytest -q`

  Expected: all pass.
