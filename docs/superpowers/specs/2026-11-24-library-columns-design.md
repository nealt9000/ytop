# Library page: three-column layout — design

Date: 2026-11-24
Status: draft for owner approval

## Intent

Make the library home (`/`) follow YouTube's layout more closely, so it's
quicker to scan as a stick fills up:

- **Left column:** the libraries (sticks) that are plugged in right now. This
  replaces the large summary blocks and the "All libraries / X / Y" switcher
  links in the middle of the page.
- **Middle column:** a single playlist section. "Start something new" and
  "Browse all" currently sit on top of each other and feel redundant, so they
  merge into one.
- **Right column:** "Continue watching" as a vertical list, like YouTube's
  "Up next" column.

The category/tag/channel chip rows get unwieldy as a library grows, so they
are replaced. The owner finds playlists by category/topic, by typing channel
names, and by scrolling, and the new filters are built around those three
habits.

## Goals

- On a big stick, the playlist grid is visible without scrolling past
  summary blocks.
- The filter controls stay one toolbar line plus one chip line, however many
  categories, tags or channels a library has.
- Continue watching is always in view beside the grid on a laptop or desktop
  screen.
- Switching libraries takes one click in the sidebar.

## Non-goals

- Phones. Phones play videos through their own player or file browser, not
  this UI. Narrow windows only need to avoid breaking (see Layout).
- Unplugged sticks. The sidebar lists only libraries that are connected now
  (whatever `libs.libraries()` returns). No new tracking of known-but-absent
  sticks.
- Other pages (`/videos`, `/search`, `/playlist/...`, `/storage`) don't
  change, except for whatever shared CSS they pick up.
- Server-side filtering, or filter state in the URL.

## Anti-goals

- Anything that reloads the page per keystroke or per chip. Pages are served
  from slow FAT sticks.
- A chip row that wraps onto several lines or grows with the library. That
  is the problem being fixed.
- Losing at-a-glance library stats ("how full is it, how much have I
  watched") from the home page.

## Constraints

- Server-rendered Jinja (`library.html`, `_cards.html`) plus the existing
  ES-module JS (`static/js/browse.js`) and `static/app.css`. No framework and
  no new dependencies.
- Filtering, sorting and grouping already run in the browser from
  `data-*` attributes on `.playlist-card`. Build on that.
- POST form routes must be `def` plus the `form_data` dependency (house rule).
  The existing `/storage/{id}/label` edit route is reused unchanged.
- The page must not wait on index writes (v0.4.3/v0.4.4 work). Don't add new
  index reads to the page path beyond what `build_home` already does.

## Approaches considered

1. **Keep the page server-rendered and extend `browse.js` (chosen).** Re-lay
   the template into three columns, add a filter box and a single-line chip
   strip that feed the existing in-browser `matches()` and `render()`, and make
   small view-model additions in `browse.py`. Instant filtering, a small diff,
   and the current tests mostly carry over.
2. **Server-side filtering (a URL per filter or query).** Rejected: a
   round-trip and a page render from a FAT stick on every keystroke is slower
   than what exists now.
3. **Rewrite the page with a JS framework.** Rejected: a lot of churn and new
   tooling for what is a layout change.

The filter style was chosen from browser mockups. The owner picked
**A: a YouTube-style chip strip plus a filter box**. Rejected: B (Category,
Channel and Tag dropdowns, which hide what exists behind menus) and C
(category and channel lists in the left sidebar, which make the sidebar long
and mix "which library" with "what topic").

## Design

### Layout

```
┌ topbar (unchanged) ─────────────────────────────────────────────────────┐
├──────────────┬───────────────────────────────────────┬──────────────────┤
│ Libraries    │ Blue 64GB · Read-only          [Edit] │ Continue watching│
│ ▸ All        │ Cooking and science for the cabin     │ ▢▬▬▬ Synth theory│
│   412 pl     │ 238 playlists · 2,104 videos · 410 h  │      Next: 7 ·…  │
│ ▸ Blue 64GB ◀│ (88 h watched) · 51 GB used · 41 free │ ▢▬── Bread lab   │
│   238 · 41GB │ [Filter this library…] Group Sort L/G │      Resume: 3 ·…│
│ ▸ Red 32GB   │ (All)(Not started)(Music)(Science)…→  │ ▢▬▬▬ Chess…      │
│   174 · 3GB  │ ┌────┐┌────┐┌────┐┌────┐              │ …                │
│              │ │New ││    ││New ││    │  grid…       │                  │
└──────────────┴───────────────────────────────────────┴──────────────────┘
```

- Three-column CSS grid on the library page only: a narrow left column
  (~220px), a fluid middle, and a right column (~320–360px). Exact widths are
  the builder's call.
- The library page can be wider than the global `.page` max-width of 1440px,
  so the middle grid gets room. Width cap is the builder's call (scoped to
  this page, e.g. a body class).
- Both side columns are `position: sticky` under the topbar. Each scrolls
  on its own if it overflows. (My call.)
- **Narrow windows** (my call; breakpoints are approximate):
  - Below ~1100px, the right column moves above the grid as a short list of
    the first 3–4 items, with a "show all" toggle at the builder's discretion.
  - Below ~800px, the left sidebar becomes a compact library `<select>` or
    links row above the header.
  - Nothing needs to look good on a phone. It only must not overflow
    horizontally.

### Left column: libraries

- A `<nav aria-label="Libraries">` list. Each entry links to
  `/?library=<id>`, and the current one has `aria-current="page"`.
- Each entry shows the label, plus a muted second line:
  `N playlists · X free` (free space only when `free_bytes` is known), and a
  "Read-only" marker where applicable.
- "All libraries" is the first entry (links to `/`), with its total playlist
  count. It appears only when two or more libraries are connected.
- With a single library connected, the sidebar still shows that one entry,
  so the layout doesn't change when a second stick is plugged in. (My call.)
- Data: the summaries `build_home` already returns, plus the refs from
  `libs.libraries()`. Today `build_home(lib, active)` narrows the summaries to
  the active library. The sidebar needs counts for every connected library,
  so the route must also have the per-library summaries when one is selected.
  The builder decides how, but must not double the index reads. For example,
  build all libraries once and filter the cards in Python for the active one,
  or compute the light sidebar numbers (playlist count, free bytes) without
  building full cards.

### Middle column

**Header (replaces the summary blocks).**
- `h1`: the library label, or "All libraries" in the combined view. Add the
  "Read-only" tag when it applies.
- The description, if set.
- One stats line: `N playlists · N videos · T total (W watched) · U used ·
  F free`. In the combined view this is the sum across connected libraries,
  which needs a new combined summary in `browse.py`. It adds up each
  library's own numbers, so a video stored on two sticks counts twice. That
  is fair, because there are two copies. (My call.)
- With only one library connected, `/` is that library's own view, not the
  combined view: its label is the h1, Edit shows if it's writable, and its
  sidebar entry is marked current. This matches today. (My call.)
- `[Edit]` opens the existing name/description `<details>` form, posting to
  `/storage/{id}/label` with `next` set back to this view. Shown only for a
  single, writable library. No Edit in the combined view.
- "Top categories" and "Top channels" are dropped. The chip strip covers
  categories, and the filter box covers channels.

**Toolbar (one line, wraps only on narrow windows).**
- A filter box: `<input type="search">` with the placeholder
  "Filter this library…" and an accessible label.
- The existing Group by, Sort and List/Grid controls, the "Manage tags" link,
  and the live playlist count ("N of M playlists").

**Chip strip (one line, scrolls sideways, never wraps).**
- The order is: **All**, **Not started**, then categories ordered by
  playlist count (most first), with ties broken alphabetically.
- Category chips are single-select, YouTube style: clicking one selects it
  and clicking it again returns to All. **Not started** is an independent
  toggle that combines with a category. **All** clears both. (My call.)
- Overflow: horizontal scroll with a fade on the edge. Small ‹ › scroll
  buttons are optional, the builder's call. No "More" button.
- The tag and channel chip rows are removed. Tags and channels are reached
  through the filter box. Tag chips on cards still link to `/videos?tag=…`.

**Filter box behaviour.**
- Live on every `input` event (a few hundred cards, so no debounce needed).
- Case-insensitive substring match. Several words must all match (AND).
- Matches against title, channel, category, tag labels and library label.
  Tag labels come from the server, e.g. a new `data-tag-labels` attribute,
  rather than from the old tag chip row that `browse.js` reads today.
- Combines (AND) with the chip selection.
- Not persisted across reloads, matching today's chips. Sort, group and view
  stay persisted in localStorage as now.
- The `/` shortcut still focuses the topbar search, unchanged.

**Grid.**
- The current "Browse all" grid, grouping and list view, with filtering
  applied.
- Unstarted playlists carry a new `data-started="0|1"` attribute, used by the
  Not started chip.
- **New badge:** shown on the playlists that "Start something new" shows
  today, meaning the `CARD_LIMIT` most recently updated unstarted playlists.
  This avoids a badge on every card of a mostly-unwatched stick. (My call.)
  Mark them in `browse.py`, e.g. a set of keys or a card flag.
- The "Start something new" section is removed.
- Empty states: the existing "No playlists match these filters" message,
  and the existing no-playlists empty state when the library is empty.

### Right column: Continue watching

- An `<aside>` with the heading "Continue watching". Same data and ordering
  as today (`view.continue_watching`, newest watched first,
  `CARD_LIMIT` = 12).
- A compact row per item:
  - a thumbnail with a thin red progress bar along its bottom edge
  - the playlist title
  - `next_label · next_title` as the second line
  - the library label in multi-library view
- Each row links to `card.next_url` (resume), as `continue-card` does now.
  A new compact macro in `_cards.html` is the expected route, rather than
  restyling `playlist_card`.
- Not affected by the filter box or chips. (My call.) `browse.js` stops
  hiding cards in non-grid sections.
- No in-progress playlists → the column isn't rendered, and the middle
  column takes its width. (My call.)
- A download badge in a row is optional. Live download progress on the
  grid cards keeps working as today.

### Code touched (expected)

- `src/ytop/web/templates/library.html`: rewritten layout.
- `src/ytop/web/templates/_cards.html`: a compact continue-row macro, plus
  `data-started` and `data-tag-labels` on `playlist_card`.
- `src/ytop/web/static/js/browse.js`:
  - remove the multi-row chip and "More" logic
  - add the chip strip and the filter box
  - stop filtering other sections
- `src/ytop/web/static/app.css`:
  - add the three-column layout, sidebar, header, chip strip and
    continue-row styles
  - remove the dead `.library-summary*`, `.library-switcher`, `.card-rail`
    and `.chip-filter*` rules
- `src/ytop/web/browse.py`:
  - a combined summary for "All libraries"
  - category counts for chip ordering
  - New-badge keys
  - drop `start_new` (or keep it only to compute the New keys)
  - drop `top_categories` and `top_channels` if nothing else uses them
- `src/ytop/web/app.py` (`library_page`): pass what the sidebar needs, and
  drop the `switcher` and `several` arguments if they become unused.
- Tests: `tests/test_browse_page.py` and `tests/test_browse.py` assert on the
  switcher, the summary headings and edit placement, and the sections. Update
  them to match the new structure (sidebar present, header h1, edit form only
  for a single writable library, no "Start something new").

## Left to the builder

- Exact column widths, breakpoints, spacing and the page width cap.
- Whether the chip strip gets ‹ › buttons.
- How the sidebar gets counts for all libraries without extra index reads.
- Markup details: element choice and class names, within the accessibility
  notes above (nav landmarks, `aria-current`, `aria-pressed` on chips,
  labelled filter input, live count).
- A visual check in the real app via Playwright at desktop (~1440px) and
  narrow (~1000px, ~760px) widths, with one and with two libraries. Use a
  local demo server with `detect_removable=false` so it doesn't pick up real
  sticks.
