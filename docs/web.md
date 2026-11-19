# Web UI reference: routes, jobs, transcripts and library management

The web app turns the library into the YouTube-playlist experience the project
promised: browse the playlist in its original order, watch, read the original
description, jump through chapters, turn on closed captions, search the
transcript, resume where you stopped, add new playlists after reviewing both
their efficient and native sizes, and manage the library — all from
`127.0.0.1`. Browsing, playback and search need no network.

## Quick start

```bash
# 0. create a library, or migrate an old one (see the README)
.venv/bin/ytop init /media/you/STICK
.venv/bin/ytop migrate <old> <new> [--dry-run]

# 1. fetch a playlist and its captions
.venv/bin/ytop --library /media/you/STICK fetch       <playlist-url-or-id>
.venv/bin/ytop --library /media/you/STICK transcripts <playlist-url-or-id>

# 2. serve the library (local-only by default)
.venv/bin/ytop --library /media/you/STICK serve
#    or: .venv/bin/ytop serve --open --port 8765
```

`ytop transcripts` is idempotent: a video with `transcript.json` is skipped.
`ytop index` rebuilds `.ytop/data/index.sqlite3` from the stored transcripts without using
the network. Every command accepts `--library` (default `$YTOP_LIBRARY`, then
`~/Videos/YTOP`).

The web UI can also fetch a playlist. Open **Add** (`/add`), paste a link, review
the probe, and confirm the download. That flow is described below.

Downloads fetch each video's transcript immediately after its media, so the web
UI and the CLI produce the same library. `ytop transcripts <playlist>` remains
the idempotent backfill/retry command for videos that were downloaded before
caption fetching was enabled, or when YouTube rate-limited the first attempt.

## Transcript acquisition

YouTube exposes captions in three ways, tried in order:

1. **The InnerTube transcript endpoint**, used by
   [`youtube-transcript-api`](https://pypi.org/project/youtube-transcript-api/).
   This is the primary route. It selected all 12 videos in the GEB playlist and
   returned 17 797 timed segments in about 20 seconds.
2. **Signed `timedtext` URLs** from yt-dlp's extractor metadata. This is the
   second route. During development the timedtext endpoint returned **HTTP 429** for this
   machine even though extraction worked; the fallback therefore retries, but is
   not required for a healthy library.
3. **`youtubei/v1/get_panel`**, the endpoint the current YouTube transcript
   panel uses. It returns the whole default transcript in one response without a
   botguard PO token, and is the reliable fallback when timedtext is blocked. It
   carries only the video id, so it returns the video's default caption language;
   callers use the language chosen from the extractor metadata for the filename.

All routes normalise to the same `transcript.json`. Missing captions are
recorded as `missing` in `transcripts.json` and never mark the playlist as
failed.

### Retry and backoff

- The InnerTube call is retried `transcripts.retries` times with exponential
  backoff; a genuinely absent track returns immediately.
- The timedtext fallback honours `Retry-After`, retries 408/425/429/5xx, and
  backs off exponentially.
- Automatic fetching during a download uses a single attempt per video, so a
  blocked caption endpoint cannot turn a long playlist download into an hour of
  retry sleeps. The explicit `ytop transcripts` command keeps the full policy.
- A single video's failure never stops the playlist. Partial runs merge with the
  previous summary. If YouTube returns 429/IP-blocked for every video, the
  playlist keeps its media and `transcripts.json` records the failures; retry
  later or configure cookies.

Config:

```toml
[transcripts]
languages = ["en"]
retries = 4
backoff_s = 2.0
timeout_s = 30
delay_s = 1.0
index = true
```

## Search index

`ytop.sqlite3` at the library root is a disposable FTS5 index:

```sql
CREATE VIRTUAL TABLE transcript_fts USING fts5(
    text,
    video_id UNINDEXED,
    playlist_id UNINDEXED,
    title UNINDEXED,
    start_ms UNINDEXED,
    end_ms UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);
```

- Every user token is quoted, prefix-matched, and joined with `AND`, so
  punctuation cannot inject FTS syntax and `isomorph` finds `isomorphism`.
- Diacritics are folded: `godel` finds `Gödel`.
- Results carry the segment timestamp, so a hit links straight to
  `/watch/<playlist>/<video>?t=<seconds>#transcript`.
- Snippets are escaped before `<mark>` is re-inserted; stored transcript text is
  never trusted as HTML.
- `transcript_meta` is keyed by `(playlist_id, video_id)`, so a video shared by
  two playlists keeps both rows. Removing one playlist
  deletes only its rows; the other playlist stays searchable without a manual
  reindex.
- A full rebuild clears the FTS tables once and inserts through one shared
  connection and one transaction per playlist. Incremental indexing still
  replaces just the affected `(playlist_id, video_id)` rows.

## On-disk contract

Metadata lives under `.ytop/data/playlists/<playlist-id>/` (the examples below
use paths relative to that folder); media lives separately under
`YTOP Library/<Playlist>/NN - Title.<ext>`, with `NN - Title.en.srt` beside it
and `folder.jpg` as the playlist thumbnail. Each video's `meta.json` records
`files.media`, the media path relative to `YTOP Library`; that is how media is
found. Renames (title edits, reordering) are journalled in
`.ytop/data/rename-journal.json` and repaired on the next start.

Per video:

```json
// videos/<id>/transcript.json
{
  "schema": 1,
  "video_id": "lWZ2Bz0tS-s",
  "title": "MIT Godel Escher Bach Lecture 1",
  "language": "en",
  "kind": "auto",
  "source": "youtube",
  "fetched_at": "2026-11-12T10:15:00+00:00",
  "raw_file": "subtitles/en.auto.json",
  "segments": [
    {"start": 0.4, "end": 4.32, "text": "The following content is provided under"}
  ]
}
```

The raw caption payload stays in `videos/<id>/subtitles/<language>.<kind>.<ext>`.
`meta.json` gains a small `transcript` block (`language`, `kind`, `segments`,
`file`, `raw_file`, `fetched_at`, `source`), so a client can discover the
transcript without parsing the full document. A metadata-only sync preserves both
this block and the playlist-level summary.

Per playlist, `transcripts.json` summarises the run and merges partial runs:
`videos[]` plus `totals` (`videos`, `available`, `new`, `missing`, `failed`,
`segments`). `playlist.json` gains a `transcripts` block.

### Audio tracks

The original performance is the primary audio and is merged into the video file.
Downloads record the chosen track in `meta.json` (`format.audio_codec`,
`audio_bitrate`, `audio_label`, `audio_original`), so the library card, playlist
row and watch page can show both the video rendition and the audio rendition
(for example `240p AV1 · Opus 50k`). Alternate tracks store their own `bitrate`
and appear in the player menu as `Opus 108k`.
When YouTube also offers a preferred-language (usually English) track, it is
stored separately under `videos/<id>/audio/<format-id>.<ext>` and listed in
`meta.json`:

```json
"audio_tracks": [
  {"id": "139-2", "label": "Spanish (US) original (default)",
   "language": "es-US", "codec": "mp4a", "embedded": true,
   "file": null, "original": true, "auto_dubbed": false,
   "default": true, "size": 0},
  {"id": "139-1", "label": "English (US)",
   "language": "en-US", "codec": "mp4a", "embedded": false,
   "file": "audio/139-1.m4a", "original": false, "auto_dubbed": true,
   "default": false, "size": 16332222}
]
```

Selection uses YouTube's explicit `xtags=acont=original` marker, not the
language preference or the display label. YouTube sometimes reports the
original voice with a misleading language code (the Sugrue lectures are marked
`es-US` even though the speaker is English); `acont=dubbed-auto` identifies the
generated dub. The player labels the former **Original audio** and the latter
**AI dub**, and keeps the standalone dub in sync so switching does not re-encode.
Videos with no extra track simply show no audio menu.

Watched progress is a separate, small contract at the library root:

```json
// progress.json
{
  "schema": 1,
  "videos": {
    "lWZ2Bz0tS-s": {
      "position": 1445.0,
      "duration": 3753.841,
      "watched": false,
      "updated_at": "2026-11-12T10:20:00+00:00"
    }
  }
}
```

A position at ≥95% of the duration, or within 10 seconds of the end, marks the
video watched unless the client explicitly says otherwise. Once marked watched,
a later partial rewatch does not clear the mark. Writes are atomic (temp file +
rename) and serialised by a process lock.

User-edited titles live beside progress in `titles.json`, so a metadata sync
cannot overwrite them:

```json
{
  "schema": 1,
  "playlists": {"PL...": "My playlist name"},
  "videos": {"lWZ2Bz0tS-s": "Lecture 1 — recursion"}
}
```

An empty title removes the override and restores the extracted title. The web UI
writes this file; any other client can read it without forking the library
contract.

### Playlist shape, order and local playlists

Every playlist folder keeps its ordered `entries` list and a small `options`
table:

```json
// .ytop/data/playlists/<id>/playlist.json
{
  "schema": 1,
  "id": "PL...",
  "title": "Michael Sugrue Lectures",
  "kind": "youtube",              // youtube | local | ungrouped
  "entries": [
    {"id": "...", "index": 1, "title": "...", "path": "videos/...",
     "video": "video.webm", "available": true, "duration": 3753.8}
  ],
  "options": {"preserve_audio": false, "quality": "720p"},
  "order": "manual"               // absent until the user reorders
}
```

The entries list **is** the playback order; `index` is renumbered whenever it
changes. Reordering (`order: "manual"`) is local and survives syncs: a sync
keeps every existing entry where it is and appends videos that are new to the
playlist. It never drops an entry that YouTube no longer returns, so removing a
video from the playlist is an explicit local action.

`kind: "local"` marks a playlist built from videos already in the library. A
shared video is stored once (there are no hard links): the playlist that owns
the file keeps it in the folder view and the local playlist references it, so
creating one costs no extra media bytes. Removing the owning playlist hands the
file to a referencing playlist instead of deleting it.

`kind: "ungrouped"` is the single playlist with id `un-grouped`. It is created on
demand and holds single videos added without a destination. It behaves like any
other playlist (reorder, remove, rename).

`options.preserve_audio` remembers the music-playlist policy. Efficient video
selection is unchanged, but the audio track is chosen for quality instead of
size: the highest-bitrate original-language rendition in the container-native
codec family (Opus for AV1/VP9, AAC for H.264), so the result still plays in the
browser without a transcode. A later sync or single-video add inherits the flag.

`options.quality` is the review choice the playlist was last downloaded with:
`efficient`, `native`, or a fixed cap `480p`, `720p`, `1080p`, `1440p` or
`2160p` (the best rendition at or below that height for every video). The next
sync's review preselects it; a remembered cap that is no longer smaller than
native preselects `native`. Adding a single video never changes it.

Web download jobs are stored in the library as `.ytop/data/jobs.json`:

```json
{
  "schema": 1,
  "jobs": [
    {
      "id": "8f55e7f4d187",
      "status": "ready",
      "url": "https://www.youtube.com/playlist?list=...",
      "playlist_id": "PL...",
      "title": "Gödel, Escher, Bach: An Eternal Golden Braid",
      "video_count": 12,
      "probe_done": 12,
      "estimate_bytes": 9025000000,
      "estimate_native_bytes": 32418375244,
      "policy": "efficient",        // efficient | native | 480p … 2160p
      "target_mb_per_hour": 100.0,
      "in_library": true,
      "items": [
        {"index": 1, "id": "lWZ2Bz0tS-s", "height": 240,
         "video_codec": "av01", "container": "webm", "size": 51499578,
         "status": "pending"}
      ]
    }
  ]
}
```

Statuses are `probing`, `cancelling`, `ready`, `queued`, `running`, `done`,
`cancelled` and `failed`. Probes and downloads use separate single-worker
executors, so a review never waits behind a long download. `probe_done` and
`probe_started` drive the review progress (“checked” and “in progress”). Raw
extractor payloads are not persisted; an interrupted job is marked failed on the
next server start. Active jobs can be cancelled and inactive jobs removed from
`jobs.json`; a cancelled or failed job can be resumed, and a completed job can be
synced again.

A terminal status is a completion barrier: cancellation first moves the job to
`cancelling`, joins every item worker, and only then writes `cancelled`. Ready
jobs revalidate their playlist and destination before starting, so a deleted or
moved target is rejected instead of recreated. Nonterminal `jobs.json` writes
are coalesced; terminal transitions flush synchronously, and server shutdown
cancels and joins workers through the FastAPI lifespan.

## Libraries

Every library is a self-contained folder, usually a whole USB stick:

```text
<root>/YTOP Library/<Playlist>/NN - Title.ext (+ .en.srt, folder.jpg)
<root>/.ytop/library.json            marker (id, label, schema version)
<root>/.ytop/config.toml             per-library settings
<root>/.ytop/cache/                  partial downloads, preview frames
<root>/.ytop/data/                   progress.json, titles.json, tags.json,
                                     jobs.json, index.sqlite3, playlists/
```

A library carries its own watch history, titles, settings and index, so it can
be plugged into any machine. Libraries come from four sources, in this order
(the first sighting of a library id wins):

- **launch** — the library the app was started from (`--library`,
  `$YTOP_LIBRARY`, else `~/Videos/YTOP`).
- **detected** — mounted volumes (or a `YTOP` folder on them) holding a marker,
  under `/media/<user>`, `/run/media/<user>`, `/mnt` or `/Volumes`. Detection is
  cached for a few seconds and scans only those mount parents, never the whole
  filesystem.
- **well-known** — `~/Videos/YTOP`.
- **registered** — folders added on the Storage page, remembered in the host
  registry `~/.config/ytop/libraries.json` (paths only; nothing else lives
  outside a library).

Only online libraries are listed: an unplugged stick disappears and comes back
when mounted. Every probe has a timeout, so a hung mount cannot stall a page.
A library whose schema is newer than this YTOP opens **read-only** with a
message to update the app. When two libraries hold the same playlist id, URLs
use the key `<id>@<lib8>` (first eight characters of the library id); otherwise
the plain id is used. A media/audio/thumbnail request is served only from a
file inside an online library.

Settings are per library (`.ytop/config.toml`); there is no global config file.
Download defaults come from the destination library. YTOP never stores cookies:
`--cookies-from-browser` and the web login switch apply to the current process
or session only.

FAT32 sticks: a download whose estimate exceeds ~3.8 GB is refused onto FAT32,
and playlist moves refuse files over 4 GiB. exFAT is recommended for very long
videos.

### Moving a playlist

A playlist's Manage panel offers **Move playlist** with every other online
library as a destination. Moving between libraries on the same filesystem is a
rename: instant and space-free. Moving to another drive copies the folder into a
hidden, job-specific staging directory, verifies the relative paths and file
sizes, renames that into place, and only then removes the original. Progress,
current file and cancel are shown in Downloads (and the move page). A failure or
cancel removes only that job's staging copy and leaves the source untouched. The
phase (`staging`, `copying`, `published`, `cleaning_source`, `done`) and the
source/target/temp paths are persisted; a restart reconciles an interrupted move
and reports an uncleaned source honestly instead of claiming the original is
intact. The move carries the playlist's progress, titles and tags into the
target library. A video shared with another playlist is stored once; the move
takes the shared file with it and keeps the references working.

## Pages

### Library (`/`)

Every playlist in every online library. Playlists on a removable drive appear while it is connected
and disappear when it is unplugged; nothing is deleted.
Shows thumbnail, title, channel, video count, total duration, watched progress,
transcript count, the **downloaded quality summary**, how many entries are already
available, and **the disk space that playlist uses**. “Add playlist” is a
separate page (`/add`), not a form embedded in the library. The header also
offers **Add video** (`/add/video`) and **New playlist** (`/playlist/new`).
Library-wide disk usage counts each stored file once.

A playlist being downloaded is listed while the download runs: `download_probe`
publishes `playlist.json` before the first item and refreshes it after each item.
The card shows a **Downloading x/y** badge and a progress bar. The page polls
`/api/jobs/{id}` with one in-flight request, pauses while the tab is hidden, and
reloads once (preserving scroll) when the job reaches a terminal state. The
Downloads page instead updates its cards in place from `/api/jobs`; focus and
scroll survive, and a status region announces completions. Finished videos are
immediately playable; pending entries appear as not-yet-available.

The playlist page has a **Manage** panel: rename the playlist, toggle the
**Keep high-quality audio** policy, or remove the playlist and its media from
disk. Every row also has **Rename** and **Remove**; the header's **✎ Edit order**
button switches the list into drag mode (see below). A playlist stored outside
the launch library shows its library label next to the channel, linking to
**Storage**.
Removing a video drops its entry, deletes its media and chapter-preview cache,
and removes it from the transcript index; watch progress is kept so re-adding it
resumes where you left off. Removing a playlist is blocked while a download for
it is active, and also drops its rows from the derived FTS index so search never
links to deleted media.

### Reorder mode (`✎ Edit order`)

The list is drag-to-reorder rather than a set of per-row move buttons. The
toolbar shows **Cancel** and **Save order** while editing; Cancel restores the
pre-edit order, Save posts the full id order to `/playlist/{id}/reorder`.

### Add a single video (`/add/video`)

Paste a `watch`, `youtu.be`, `shorts`, `embed` or `live` link, choose a
destination playlist (default **Un-grouped**), optionally tick **Keep
high-quality audio**, and ytop runs a normal one-item probe/download. The review
page uses single-video wording, shows the destination and the audio rendition,
and the button reads **Add to &lt;playlist&gt;**.

Pasting a single-video link into the main `/add` form does the same thing and
sends it to Un-grouped. The review page uses single-video wording and shows the
destination instead of the playlist-sync note.

### New playlist from the library (`/playlist/new`)

Lists every video in the library with thumbnail, source playlist, duration,
video quality, audio codec/bitrate, size and watched state. Tick the videos you want, give the playlist a
title, and Create. The new playlist references the existing files (no hard links; shared videos
are stored once), so it needs no download and no extra media space. Its entries can then be reordered and
removed like any other playlist.

### Add and review (`/add`, `/add/<job-id>`)

The start form also has a **Store downloaded media on** picker listing every
online library with its free space. The selected library is copied into the
job (`location_id`) and used when the download starts, with its own config
supplying the download defaults. Syncing a playlist that already exists ignores
the form default and keeps the playlist in its current library. A download that
cannot fit (not enough free space, or an estimate above ~3.8 GB onto FAT32) is
refused up front. If a drive is unplugged between probing and downloading, the
job fails with “not connected” rather than silently writing elsewhere.

`POST /add` starts a background probe with parallel metadata fetches. The page
refreshes while probing and then shows:

- playlist title, channel, description, video count and total duration;
- **two size estimates**: the default efficient budget and the native maximum;
- every video with duration, the chosen efficient resolution/codec/container, its
  efficient size and its native size;
- an “already in your library” notice when the playlist id is present, with the
  button relabelled **Sync playlist**;
- a policy choice (**Efficient** or **Native**), an optional **Keep
  high-quality audio** tick for music playlists, then **Download playlist**.

The audio tick leaves the efficient video budget untouched but selects the best
available original-language audio rendition instead of the smallest one. It is
stored in the playlist document, so later syncs and single-video adds inherit it.
A single video URL pasted into this form skips playlist probing and is added to
Un-grouped.

The efficient policy is a playlist-wide budget, not a blanket height cap. Every
video starts at the smallest legible rendition (at least 240p when available),
then the remaining budget is spent on the upgrades that buy the most height per
extra byte. `av01` is preferred, then `vp9`, then `avc1`. For the 121-video,
90-hour Michael Sugrue lecture playlist this estimates **≈8.4 GiB efficient vs
30.2 GiB native**. GEB stays at its native 240p (≈1.1 GiB), so the validation
corpus is unaffected.

This deliberately leaves a **mix of resolutions and codecs**: YouTube does not
offer every rendition for every video, and the budget is shared across the whole
playlist. Forcing one resolution would either overspend on videos that gain
little or throw away the cheap upgrades that make the playlist more legible.
Forcing one codec would require a re-encode, which the project does not do.

No bytes are written until the button is pressed, and **no re-encode happens**.
Efficient mode downloads a smaller native rendition, so its estimate is also the
final on-disk size. The native policy downloads the best rendition YouTube
offers and is likewise not post-processed.

### Storage (`/storage`)

Lists every library the app can see (launch, detected, `~/Videos/YTOP` and
added folders) with its label and description, folder, file-system type,
playlist count and free space, tagged *running from*, *detected*, *this
computer*, *added here*, *read-only* or *in use*. **Rename** edits a library's
label and description. **Add library** takes a folder: an empty folder becomes a
new library, a folder that already holds one is remembered on this computer.
**Forget** drops a remembered folder without touching its files and is disabled
while a download or move uses it. **Rescan drives** bypasses the detection
cache. The page also shows the total media size across online libraries.

### Downloads (`/downloads`)

Lists recent probe/download jobs with status, progress, per-video success/skip/
failure counts and bytes on disk. Running probes and downloads refresh every two
seconds and continue if you leave the page. Active jobs have a **Cancel** button;
finished, cancelled and failed jobs have **Remove**. The list is persisted in
`jobs.json`. Completed jobs do **not** expire on a timer: they stay until you
press Remove. The file keeps the 50 newest jobs, and the page list is trimmed to
the same limit when a 51st job is created.

### Playlist (`/playlist/<id>`)

YouTube-style ordered list: index, local thumbnail, duration, title, channel,
the **downloaded video rendition** (e.g. `240p H.264`), the **audio rendition**
(e.g. `Opus 50k`) and **media size**, description
snippet, chapter count, transcript badge, per-video watched progress, and a
“Play all / Play next” button that starts the first unwatched available item
with autoplay. The channel name opens the YouTube channel in a new tab. An active
download for this playlist shows a progress banner and the page refreshes as
items complete.

The Manage panel also moves the playlist to another connected library
(see [Moving a playlist](#moving-a-playlist)).

**✎ Edit order** turns the list into drag mode: the button becomes **Cancel**,
a **Save order** button appears next to it, drag handles show on every row, and
the row actions and playback links are disabled so a drag cannot open a video.
Dragging moves an entry; **Save order** commits the new order and leaves edit
mode; **Cancel** restores the order from before editing without a request.
Arrow keys also move the focused row. A saved order persists and survives syncs.
**Add video** stays in the header and the Manage panel has the **Keep
high-quality audio** toggle. Local and Un-grouped playlists are badged in the
header; `kind` drives the badge.

### Watch (`/watch/<playlist>/<video>`)

- `<video>` streams the original file from the library with HTTP range support.
  Controls are custom so chapters and captions can live in the player:
  play/pause, seek, volume/mute, time, **CC**, audio track, fullscreen.
- **Audio track** appears when the video has a stored alternate. The original
  performance is selected by default even when YouTube's language metadata or
  `audio.language` points elsewhere; it is labelled **Original audio**, and the
  generated dub is labelled **AI dub**. Switching keeps a separate `<audio>`
  element in sync with the video, so no transcode is needed.

- Clicking a video in the playlist starts playback immediately where the browser
  permits it; a large play button remains when autoplay is blocked.
- The channel name links to `https://www.youtube.com/channel/<channel_id>` in a
  new tab. The line under the title shows the **downloaded rendition**, fps,
  container and **media file size** (e.g. `360p VP9 · 15 fps · WebM · 54.4 MiB`),
  and **Edit title** writes a local override to `titles.json`.
- **Chapters** come from yt-dlp metadata, with a conservative fallback to
  timestamp lines in the description (at least two are required). Chapters appear
  as alternating segments with clear separators on the timeline, and the active
  segment is highlighted. Hovering the bar shows a YouTube-style tooltip with a
  **frame preview at the chapter start**, the hovered time in larger text, the
  chapter title and its time range. Preview frames are extracted on demand with
  `ffmpeg` and cached under `<library>/.ytop/cache/previews/`; if extraction fails the
  stored poster is shown. The Chapters tab seeks too.
- **Closed captions** are the stored transcript rendered over the video, toggled
  by the CC button or `c`, with the preference remembered in `localStorage`.
- **Description** is the stored YouTube text, line breaks preserved, URLs opened
  in a new tab, and timestamps (`1:23`) turned into seek links.
- **Transcript** is the stored timed text. It highlights the active line, can be
  filtered inline, and every timestamp seeks. The panel opens automatically from
  a search result (`#transcript`).
- **Playlist queue** is always in original order and highlights the current item.
- **Resume** seeks to the saved position and offers “Start over”; `?t=` overrides
  it. The resume notice dismisses itself after a few seconds of playback.
- **Autoplay next** continues to the next available video on `ended`, and the
  toggle is persisted in `localStorage`.
- **Mark watched** writes progress immediately; playback writes every ~5 s, on
  pause/seek/ended, and via `sendBeacon` on page hide. A watched mark survives a
  later partial rewatch.
- Keyboard: space play/pause, `←`/`→` seek 5 s, `n`/`p` next/previous, `c` CC,
  `f` fullscreen, `m` mute.

### Search (`/search?q=`)

Searches the FTS5 index across the whole library (optionally filtered by
playlist), shows highlighted snippets and timestamps, and links into the watch
page at the matching moment.

## HTTP surface

Pages: `/`, `/add`, `/add/{job_id}`, `/add/video`, `/downloads`, `/storage`,
`/playlist/new`, `/playlist/{playlist_id}`, `/move/{job_id}`,
`/watch/{playlist_id}/{video_id}`, `/search`.

JSON:

| Route | Purpose |
|---|---|
| `POST /add` | Form: `url`, optional `preserve_audio=1`, `location=<id>` → start a probe; a single-video URL goes to Un-grouped |
| `GET /add/video` | Single-video form with a destination playlist picker |
| `POST /add/video` | Form: `url`, `playlist` (empty = Un-grouped), optional `preserve_audio=1` → start one video |
| `POST /jobs/{job_id}/download` | Form: `policy=efficient|native|480p|720p|1080p|1440p|2160p`, optional `preserve_audio=1`; start or resume a job |
| `POST /jobs/{job_id}/cancel` | Cancel a queued/probing/running job |
| `POST /jobs/{job_id}/remove` | Remove an inactive job from `jobs.json` |
| `POST /playlist/{playlist_id}/title` | Form: `title`; set or clear a playlist title override |
| `POST /playlist/{playlist_id}/videos/{video_id}/title` | Form: `title`; set or clear a video title override |
| `POST /playlist/{playlist_id}/delete` | Remove the playlist folder from disk (blocked while downloading) |
| `POST /playlist/{playlist_id}/videos/{video_id}/remove` | Drop an entry; deletes media unless `keep_media=1` |
| `POST /playlist/{playlist_id}/reorder` | Form: `order=<comma-separated video ids>`; persists a manual order |
| `POST /playlist/{playlist_id}/options` | Form: `preserve_audio=1`; remember the music audio policy |
| `POST /playlist/{playlist_id}/move` | Form: `location=<id>`; queue a move to another connected library |
| `GET /move/{job_id}` | Move job progress, cancel and result |
| `GET /playlist/new` | Pick library videos for a new playlist |
| `POST /playlist/new` | Form: `title`, optional `location=<id>`, repeated `video=<playlist-id>:<video-id>`; reference existing media in a new local playlist (stored once, no hard links) |
| `GET /storage` | Libraries, usage and detected drives (`?refresh=1` rescans) |
| `POST /storage/add` | Form: `path`, `label`; create a library in an empty folder or remember an existing one |
| `POST /storage/{id}/label` | Form: `label`, `description`; rename a library |
| `POST /storage/{id}/remove` | Forget a remembered library; rejected while a job uses it, and its files stay on disk |
| `GET /api/jobs` | All recent jobs, with derived `active` and `percent` |
| `GET /api/jobs/{job_id}` | One job, including review items, progress, `active` and `percent` |
| `GET /api/transcript/{playlist}/{video}` | Transcript segments + language/kind |
| `POST /api/progress` | `{video_id, position, duration?, watched?}` |
| `GET /healthz` | Liveness |

Media:

| Route | Purpose |
|---|---|
| `GET /media/{playlist}/{video}` | Video with `Range` support |
| `GET /media/{playlist}/{video}/audio/{track}` | Stored alternate audio track |
| `GET /media/{playlist}/{video}/thumb` | Stored YouTube thumbnail |
| `GET /media/{playlist}/{video}/preview?t=` | Cached chapter-hover frame at `t` seconds |
| `GET /media/{playlist}/thumb` | Playlist thumbnail |

## Offline and security

- No CDN, web font, analytics, or external script. CSS and JS are served from
  `src/ytop/web/static/` as small dependency-free ES modules.
- Binds `127.0.0.1:8765` by default; `--host`/`--port` and `[web]` change the
  port, but a non-loopback host is rejected with an actionable error.
- Unsafe requests are checked against the `Host` allowlist and browser
  `Origin`/`Sec-Fetch-Site` metadata. Browser forms and fetches must carry the
  per-process CSRF token rendered into the page; tokenless non-browser script
  requests remain usable for tests and the CLI.
- Redirect targets accept only local paths; protocol-relative and absolute URLs
  are replaced with `/downloads`.
- `progress.json`, `titles.json`, `tags.json`, `jobs.json` and playlist
  documents are written through same-directory temporary files plus
  `fsync`/`os.replace`, guarded by an in-process lock and a cross-process
  `flock` sidecar. Corrupt user-owned JSON is quarantined, never silently reset.
- Every playlist/video/file lookup is validated against the discovered library:
  ids must exist in `playlist.json`, relative paths must stay inside the playlist
  directory, and media/thumbnail files are constrained to the library.
- Outbound network is used only by `ytop fetch`, `ytop probe`, `ytop transcripts`
  and a web probe/download job; browsing, playback, search and progress are fully
  local.
- The add form accepts only a playlist or single-video target that yt-dlp
  resolves; a failed probe is shown as a job error, not a server error.
- Local playlists reference media instead of linking or copying it, so they
  work on any filesystem (including FAT32/exFAT sticks) with no extra bytes.

## Validation

Against the real GEB library:

| Check | Result |
|---|---|
| Transcripts fetched | **12/12 videos, 0 failures, 17 797 segments** |
| Transcript fallback | `get_panel` backfilled **Sugrue 121/121, Music As A Mirror 20/20, Bernstein 5/6** (one video has no captions) while timedtext returned HTTP 429 |
| Fetch wall time | **~20 s** with the InnerTube route |
| FTS index | 12 transcripts, `ytop index` in **0.7 s** |
| Playlist order | 12 entries in YouTube order |
| Video playback | Chromium: duration 3753.841 s, `readyState 4`, no media error |
| Chapter seek | chapter 4 → 1188.37 s; 16 timeline segments |
| Autoplay | clicking a playlist row starts playback; `ended` → next video with `?autoplay=1` |
| Captions | CC overlay shows the current segment; toggle persists |
| Add/review (GEB) | probe returned **12 videos, 1.1 GiB estimated** |
| Add/review (121 videos) | **8.4 GiB efficient / 30.2 GiB native**, 0 probe errors, ~2 min |
| Audio originals | 4 Sugrue videos with Spanish originals now default to the Spanish track; English is a stored alternate and switches in the player with no re-encode |
| Download transcripts | every downloaded/skipped video attempts its transcript automatically; `transcripts.json` is written with the job |
| Cancel / remove | a probing job cancelled in ~3 s; a ready job removed from `jobs.json` |
| Probe independent of downloads | probes and downloads use separate workers; a previously stalled 20-video probe completes in ~40 s and cancels in ~3 s |
| Mid-download playlist | `playlist.json` written before the first byte; the first item is marked available while the second is still downloading |
| Library management | playlist and video titles round-trip through `titles.json`; removing a playlist deletes its folder and returns 404 |
| Quality detail | library/playlist/watch show `240p H.264` and the media size from `meta.json` |
| Chapter hover | Chromium hover shows the chapter title, time range and a frame preview; no console errors |
| Channel link | watch/playlist channel links point at the YouTube channel and open in a new tab |
| Sync | a real sync skipped all 12 existing media files, 0 failures |
| Reorder + sync | a manual order survives a sync; new videos are appended, removed entries stay removed |
| Edit order | Playwright: ✎ Edit order switches to Cancel/Save with drag handles; Cancel restores the original order; Save drags row 2 above row 1 and persists it |
| Audio detail | library cards, playlist rows and watch pages show the audio rendition (e.g. `Opus 50k`, `AAC 49k`) next to the video rendition; the player menu shows alternate-track bitrates |
| Remove video | entry, media and preview cache deleted; progress kept; search stops linking to it |
| Single video | `/add/video` probes one video into the chosen playlist; a bare watch URL goes to Un-grouped |
| Local playlist | videos from two playlists linked into one new playlist with no extra media bytes; deleting it leaves the sources playable |
| Music audio | with `preserve_audio`, a 50 kbps original track becomes the 160 kbps original track while video stays efficient |
| Search | `strange loop` → 3 timestamped hits; `godel` matches `Gödel` |
| Progress | POST → `progress.json`; watched state appears on the playlist page |
| Broken links / console errors | none in the Playwright runs |
| Libraries | Playwright/TestClient: add a USB folder, download a video into a playlist there, serve its media, then rename the folder and confirm the playlist disappears and returns when restored |
| Storage page | add, rename, forget and rescan round-trip; an unplugged library disappears without breaking the rest |
| Move playlist | same-filesystem move renames the folder, keeps media playable and updates the location badge; target collisions and offline drives are rejected; progress/cancel page round-trips; a copy helper is tested for byte/file fidelity |
| Move progress | job reports moved bytes/files and renders on Downloads and `/move/{id}` while queued, running, done, cancelled or failed |
| Detected drives | a fake mounted volume holding a library marker appears automatically and leaves when unmounted |
| Tests | **270 passed** |

## Limitations

- Once a playlist is in the library, its entry order is owned locally. A sync
  refreshes metadata and appends new videos, but it does not apply a reorder made
  on YouTube, and it does not drop a local entry that YouTube no longer returns.
  Use **✎ Edit order** to drag entries into place and **Remove** to delete one.
- Local playlists (`/playlist/new`) reference media; a shared video is stored
  once and appears in the folder view only under the playlist that owns it.
  Removing the owner hands the file to a referencing playlist.
- Transcript search is keyed by `(playlist_id, video_id)`. A local playlist
  keeps its own rows, and removing one playlist leaves the other
  searchable without a manual reindex.
- Interrupted moves are reconciled on the next start. The job reports whether the
  destination was published and whether the source cleanup finished; it never
  deletes the only complete copy. If cleanup failed, the job says both copies
  exist and leaves them for you to inspect.
- Single videos with no destination go to the launch library's **Un-grouped**
  playlist. To keep a single video in another library, add it to a playlist
  stored there.
- `preserve_audio` changes the policy for new downloads and syncs. It does not
  transcode or replace audio in media that is already on disk; delete and re-add
  the video (or force a sync) if an existing download used the smaller track.
- Transcripts are YouTube's automatic captions for the MIT lectures; there are
  no speaker labels and the ASR errors are preserved verbatim.
- The transcript page embeds every segment once in the HTML; captions read their
  timings from that DOM instead of a second JSON copy. A 1-hour lecture is still
  ~600 KiB, which is fine on localhost; a later version can render it from the
  transcript API in bounded batches.
- Probing a large playlist performs one full metadata extraction per video. It is
  parallelised (`probe_jobs`, default 4) and runs in the background with progress,
  but it is intentionally not instant (~2 minutes for 121 videos here).
- The efficient policy trades resolution for storage. It keeps a 240p floor and
  shows both estimates before download; choose **Native** for the best available
  rendition. No re-encoding is performed in either mode.
- Chapter hover previews are generated on the first hover of a chapter. The frame
  is cached, so later hovers are instant; a cold ffmpeg extraction takes a fraction
  of a second on local media. If ffmpeg cannot read a file, the stored poster is
  shown instead.
- Web downloads run one at a time; a job uses the configured download worker
  count (`[download].jobs`, default 1). Playlist probes use a separate worker, so
  they never wait behind a download. The CLI still supports `--jobs N` directly.
- No authentication, accounts, comments, or recommendations — by design.
- `youtube-transcript-api` can still be rate-limited or blocked; the yt-dlp
  fallback and `missing` status keep the library usable. When both timedtext and
  the API are blocked, ytop uses YouTube's current `get_panel` transcript
  endpoint, which returns the default transcript without a PO token. Automatic
  fetching still uses one API attempt per video, so failures are recorded quickly
  instead of stalling the download. Retry later with `ytop transcripts`; a cookie
  jar or proxy may be required if YouTube blocks the machine's IP entirely.
- Audio alternates are stored only for the preferred UI language (`audio.language`)
  when it differs from the original. Downloading every dub would multiply audio
  storage for no browsing benefit; the original and one alternate cover the
  common case.
