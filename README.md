# YTOP

YTOP (YouTube Offline Preserver) downloads YouTube playlists for offline
watching and presents them the way you'd study them on YouTube: original
order, descriptions, chapters, captions, searchable transcripts and watch
progress. It runs locally and serves a web UI on `127.0.0.1`. Browsing,
playback and search work with no network.

A library is one self-contained folder, and a whole USB stick works well. A
**YTOP stick** carries the program as well as the library, so it starts on
Windows, macOS and Linux without installing anything.

YTOP keeps YouTube's own video and audio streams untouched. It saves space by
choosing a rendition, not by re-encoding. The measurements behind that choice
are in [`docs/why-native-streams.md`](docs/why-native-streams.md).

## Status

YTOP is an experimental personal project, shared as is under the MIT licence.
It works day to day on Linux. Windows and macOS sticks are built and
smoke-tested in CI, but have had less real-world use. Expect rough edges, and
expect YouTube-side breakage from time to time. The YouTube-facing components
(yt-dlp and friends) can be updated from the app without a new YTOP release.

Use it for content you have the right to keep offline, and within YouTube's
terms.

## Getting started

There is no installer. You either run YTOP from a source checkout, or build a
stick from one (see [Making and using YTOP sticks](#making-and-using-ytop-sticks)).
After that, a stick updates itself from this repository's releases.

**From source** (Linux or macOS, Python 3.12, [uv](https://docs.astral.sh/uv/),
`ffmpeg` on `PATH`):

```bash
git clone https://github.com/nealt9000/ytop.git && cd ytop
uv sync
uv run ytop doctor                                   # check ffmpeg, yt-dlp, FTS5, web stack
uv run ytop init /media/you/STICK                    # new, empty library
uv run ytop --library /media/you/STICK serve --open  # http://127.0.0.1:8765
```

Then choose **Add → Add playlist** in the browser, or use `ytop fetch` from
the [CLI](#cli). To convert a library made by an older YTOP, see
[Migrating](#migrating-an-old-library).

The launch library is `--library`, else `$YTOP_LIBRARY`, else `~/Videos/YTOP`.
`fetch`, `transcripts`, `index` and `serve` refuse a folder that is not a
library and print how to create one; `init`, `migrate`, `doctor` and `probe` do
not need one. `fetch`, `transcripts` and `index` also refuse a library made by a
newer YTOP (it is read-only here). Everything except `fetch`, `probe` and
`transcripts` works fully offline.

## Library layout

Everything a library needs travels with it:

```text
<root>/
  YTOP Library/                     # readable by any file manager or player
    <Playlist>/
      01 - First video.webm
      01 - First video.en.srt       # captions beside the media
      02 - Second video.mp4
      folder.jpg                    # playlist thumbnail
  .ytop/
    library.json                    # marker: id, label, schema version
    config.toml                     # this library's settings (optional)
    cache/                          # partial downloads, preview frames
    data/
      progress.json                 # watched positions
      titles.json                   # title overrides
      tags.json
      jobs.json                     # download/move jobs
      index.sqlite3                 # derived transcript search index
      playlists/                    # per-playlist/per-video JSON (playlist.json,
                                    #   meta.json, transcript.json, audio tracks)
```

Media is found through each video's `meta.json` (`files.media`, a path relative
to `YTOP Library`), not through folder names, so you can browse and copy the
media folder by hand. Renaming a title or reordering a playlist renames files
(the `NN - ` prefix follows playlist order); renames are journalled, so an
interrupted batch is repaired on the next start. The marker is written last when
a library is created, so a half-made folder is never mistaken for one.

### Libraries

Libraries replace the old "storage locations": every library is
self-contained, with its own media, progress, titles, settings and index. The
app finds libraries in four places:

- the **launch** library (`--library` / `$YTOP_LIBRARY`);
- plugged-in drives holding a library marker under `/media`, `/run/media`,
  `/mnt` or `/Volumes`;
- `~/Videos/YTOP`;
- folders remembered in the host registry, `~/.config/ytop/libraries.json`
  (paths only).

An unplugged stick simply disappears and returns when mounted. **Storage** in the
web UI lists them with free space and file-system type; **Add library** takes a
folder, creating a new library in an empty one or remembering an existing one.
Forgetting a library never deletes files. A library created by a newer YTOP
opens read-only. When two libraries contain the same playlist id, playlist URLs
use a key of the form `id@lib8` (the first eight characters of the library id).

## Making and using YTOP sticks

A **YTOP stick** carries the whole program as well as its library, so it runs on
Windows, macOS (Apple silicon and Intel) and Linux (x86-64 and ARM64) without
installing anything. Nothing is ever formatted: YTOP is copied next to whatever
is already on the drive, into `.ytop/`, plus three launchers and a `README.txt`
at the drive root.

**Make the first stick** from a development checkout. Build the image once, then
copy it onto the drive:

```bash
uv run python tools/build_stick.py --platforms all     # -> dist/stick (~1 GB)
uv run ytop make-stick /run/media/$USER/STICK --image dist/stick/.ytop --label "My stick"
```

`build_stick.py` fetches the pinned runtimes in `stick/versions.toml` (Python,
Deno, a minimal ffmpeg from this repo's `ffmpeg-minimal-*` release) and checks
the result is under 1.5 GB. Copying ~11 000 files onto a FAT32 stick is slow;
on a real FAT32 stick (mounted with `flush`) it took about 27 minutes, with
progress printed every 15 seconds.

**Make more sticks from a running stick:** **Storage → Make a stick** lists the
plugged-in drives. Choose which computers the new stick should start on and
which playlists to copy onto it. Choosing a drive that already has YTOP
upgrades it (or run `ytop make-stick <drive> --upgrade`). An upgrade replaces
the app and never touches the stick's library. It copies only the files that
changed, so an app-only upgrade takes seconds to minutes, not the half hour of
a full copy. The first upgrade of a stick made before this checks every file
once (slower); after that the stick keeps a file list. If an upgrade is
interrupted, starting the stick undoes it. The stick YTOP is running from
cannot be its own target.

**Start it** with the launcher for the computer you're on (the stick's
`README.txt` says the same):

- **Windows:** double-click `Start YTOP (Windows).cmd`. If SmartScreen warns,
  choose *More info → Run anyway*.
- **Mac:** double-click `Start YTOP (Mac).command`. The first time, right-click →
  *Open* if macOS blocks it.
- **Linux:** `sh "/run/media/$USER/<drive>/start-ytop-linux.sh"` from a
  terminal. Linux mounts FAT sticks without execute permission, so the first run
  copies the Linux runtime and app (about 300 MB) into
  `~/.cache/ytop/runtime/<build>/` and runs from there. Builds unused for 30
  days are removed from it, and it is always safe to delete.

The browser opens on the stick's library. Other libraries that are plugged in,
plus `~/Videos/YTOP`, show up too. Downloads, settings, watch progress and the
yt-dlp/Deno caches all stay on the stick. Cookies are never written anywhere.

The launcher window shows where the start spent its time, for example
`Started in 3.8 s: Python 0.4 s, loading YTOP 2.0 s, finding drives 0.3 s, …`.
If a start is slow, that line says which part.

**While it runs:**

- Launching a second stick while one is running just opens the browser on the
  running copy, which sees both libraries.
- **Quit** in the app stops YTOP and says when it is safe to unplug. Closing
  the launcher window stops it too. It also quits on its own about five minutes after the
  last browser tab closes, unless a download or copy is still running.
- If you pull a stick out while it is in use, YTOP keeps running and that
  library goes offline. Downloads to it pause. Plug it back in and it is
  checked and shown again.

**Updating from a terminal** (no browser; also what CI drives). Run these
through the stick's Python, e.g.
`python3 -s -E <stick>/.ytop/boot.py --home <stick> -- <command>`:

- `ytop components update [yt-dlp ...]` updates the YouTube-facing components
  now, like **Update now** (downloaded, checksummed, smoke tested, then switched).
- `ytop app-update check` says whether a newer YTOP release is on offer.
- `ytop app-update install --yes` installs it with the same planned,
  checksummed, journalled install as the Storage page. It refuses without
  `--yes`, which stands for the confirmation the page asks for.

`[updates] smoke_video = ""` turns the updater's online metadata probe off
(hermetic tests). The CI job `self-update-fat32` rehearses all of this against
`tools/fake_release_server.py` (a fake GitHub releases API on 127.0.0.1) with
releases built by `tools/ci_fake_release.py`.

## Releasing

Releases are cut from a pushed tag; the workflow (`.github/workflows/release.yml`)
builds the 5-platform image, runs the OS smoke tests, packages it with
`tools/package_release.py` and publishes a GitHub release.

1. Bump the version in `pyproject.toml` (`[project] version`) and in
   `src/ytop/__init__.py` (`__version__`); a test keeps the two equal.
2. Commit it.
3. Tag with the release notes as the annotated message (they become the
   release notes YTOP shows before an app update):
   `git tag -a vX.Y.Z -m "<notes>"`
4. Push the tag: `git push origin vX.Y.Z`. The workflow fails if the tag is not
   `v` plus the pyproject version.

To rehearse, run the **Release** workflow by hand (`workflow_dispatch`) with
`dry_run` left on: it builds and packages everything, uploads the files as the
`release-files` workflow artifact and creates no release.

Each release holds one `.tar.gz` per piece (`app`, `root`, `lib-common`,
`lib-<platform>`, `runtime-<platform>-python`, `runtime-<platform>-bin`),
`components-seed.tar.gz`, `ytop-release.json` (versions, per-piece checksums and
file lists, the image manifest, minimum component versions) and `SHA256SUMS`.
The archives are reproducible byte for byte.

## Migrating an old library

```bash
ytop migrate <old> <new> [--dry-run]
```

`--dry-run` prints the plan (playlists, bytes to move or copy, partial downloads
that will be dropped, anything unreadable) and changes nothing. A real run moves
files on the same drive and copies across drives; nothing is deleted until a
playlist is fully in place, and a stopped run resumes when you repeat the same
command. Playlists and locations that cannot be migrated are listed in
`<old>/MIGRATED.txt` and stay where they were.

## Settings, cookies and FAT32

- Settings live per library in `.ytop/config.toml` (there is no
  `~/.config/ytop/config.toml`). Downloads take their defaults from the
  destination library's config.
- YTOP never stores cookies. `--cookies-from-browser <browser[:profile]>` (or
  the matching switch in the web UI) applies to the current process or session
  only; `cookies`/`cookies_from_browser` in a config file are ignored.
- Settings > "Age-restricted videos" names a browser profile that YTOP uses to
  retry age-gated videos signed in; make it a dedicated account, not your own.
  Only the browser and profile name are saved, never cookies or passwords.
- Sticks formatted FAT32 cannot hold files of 4 GiB or more. A download whose
  size estimate exceeds ~3.8 GB is refused onto FAT32, and playlist moves refuse
  files over 4 GiB. Use exFAT for very long videos.

## CLI

```bash
ytop doctor                            # check ffmpeg, yt-dlp, FTS5, web stack
ytop init   <path> [--label NAME]      # create a library
ytop migrate <old> <new> [--dry-run]   # old layout -> portable library
ytop probe  <playlist-url-or-id>       # formats + size estimate, no download
ytop fetch  <playlist-url-or-id>       # resumable download/sync
ytop fetch  <playlist> --jobs 2        # parallel downloads
ytop fetch  <playlist> --preserve-audio # keep the best audio (music)
ytop fetch  <playlist> --to <library-id-or-path>  # download into another library
ytop transcripts <playlist-url-or-id>  # captions + FTS index
ytop transcripts --index-only          # rebuild the index from disk
ytop index                             # same, whole library
ytop serve [--open] [--port N]         # local web UI
ytop --library <path> <command>        # pick the launch library
```

`fetch` syncs: new items are added, existing media is not re-downloaded, and
nothing is deleted. Videos already downloaded are not read from YouTube again
(only a changed title is taken from the playlist listing), so a sync costs only
the new videos; `--refresh` re-reads their descriptions, thumbnails, audio
tracks and missing transcripts too. Playlist order, descriptions,
thumbnails and chapters are stored beside each video. A metadata sync preserves
the transcript blocks written by `ytop transcripts`.

`.ytop/config.toml` (all keys optional):

```toml
[download]
jobs = 1
probe_jobs = 4          # parallel metadata probes during review
target_mb_per_hour = 100  # efficient budget; 0 = native maximum
min_height = 240        # legibility floor when the source offers it
max_height = 0          # optional hard cap; 0 = no cap
request_interval = 0.5
quick_analysis = true   # analyse without YouTube's player JS (~40% faster)
audio_language = "en"

[pot]
provider = "auto"

[transcripts]
languages = ["en"]
retries = 4
backoff_s = 2.0
timeout_s = 30
delay_s = 1.0
index = true

[web]
host = "127.0.0.1"
port = 8765
open_browser = false
```

## Transcripts

Captions are fetched once and stored beside the media. The primary route is
YouTube's InnerTube transcript endpoint (`youtube-transcript-api`); the yt-dlp
`timedtext` URL is a retried fallback. The raw timedtext endpoint returned HTTP
429 during development, so the InnerTube route is deliberately first. Missing
captions are recorded as `missing` and never block the library.

Each video gets `transcript.json` and a raw caption file under `subtitles/` in
its metadata folder, plus an `.en.srt` beside the media; each playlist gets
`transcripts.json`. Search is SQLite FTS5 with prefix
matching and diacritic folding, so `godel` finds `Gödel`.

## Web UI

```bash
.venv/bin/ytop serve                 # http://127.0.0.1:8765
.venv/bin/ytop serve --open
```

- **Storage** — every library the app can see: the launch library, plugged-in
  drives, `~/Videos/YTOP` and folders added here. Shows playlist count, free
  space and file-system type per library, renames libraries, adds a folder as a
  new library, forgets a remembered one (files stay) and rescans drives. A
  library from a newer YTOP is marked read-only. Moving a playlist to another
  library shows visible copy progress and carries its progress, titles and tags
  with it. On the same drive the playlist's own folder is renamed instantly;
  media it shares with other playlists there is copied.
- **Library** (home page): **Continue watching** and **Start something new**
  rows, then **Browse all** with a List/Grid toggle, **Group by** category, tag,
  channel or library, sorting (recent, title, length, unwatched remaining,
  size) and category, tag and channel filter chips. Each library gets a summary
  of what it holds. Cards show thumbnail, duration, watched progress and disk
  space. Tags can be renamed, merged or hidden under **Manage tags**. The
  **Add** menu holds **Add playlist**, **Add video** and **New playlist**.
  Playlists appear as soon as a download starts, with finished videos playable
  and the rest filling in as they land.
- **Add playlist** — paste a YouTube playlist link and choose a destination
  library; ytop probes it in parallel and shows the **efficient** size (the default 100 MB/h budget, minimum
  240p, no re-encode), fixed caps (**480p**, **720p**, **1080p**, **1440p**,
  **2160p max**, where they are smaller than native) and the **native** size
  (best rendition YouTube offers), each marked as fitting or not in the
  destination drive's free space. Every video’s chosen format is listed, and
  nothing downloads until you confirm. Already-present playlists are offered as
  a sync, starting from the quality the playlist was last downloaded with.
- **Downloads** — probing and download jobs with per-video status, bytes written
  and a progress bar. The page updates in place from `/api/jobs` while jobs run,
  so scroll and keyboard focus are kept; polling pauses on a hidden tab. Each
  finished video attempts its transcript automatically. Running jobs can be
  cancelled immediately (the job shows *cancelling* until its workers exit);
  finished jobs stay until removed. Job state survives a server restart.
- **Playlist** — thumbnails, durations, per-video quality and size,
  descriptions, chapters, per-item progress and “Play all”. Filter by text or
  watched/missing state. **✎ Edit order**
  enables drag-to-reorder with explicit Cancel/Save; remove a single video, add
  one by URL, rename the playlist or any video, or remove the whole playlist
  from disk. Manual order survives syncing, which only appends new videos.
- **Add video** — paste one watch/shorts/youtu.be link and pick a destination;
  the default destination is the **Un-grouped** playlist.
- **New playlist** — tick videos already in the library to build another
  playlist. A video already stored in one playlist is referenced, not duplicated: its
  media is stored once (no hard links) and shows in the folder view only under
  the playlist that owns it.
- **Music playlists** — tick **Keep high-quality audio** to keep the efficient
  video budget but choose the best available audio track instead of the smallest.
- **Watch** — local `<video>` with range requests, a YouTube-style control bar,
  chapter segments on the timeline with hover time/chapter/frame previews, closed
  captions with a CC button, the stored description, a searchable transcript
  panel, the playlist queue, resume, next/previous and autoplay. Playback speed
  is persisted and applied to both the embedded and alternate audio tracks. The true
  original voice is selected even when YouTube’s language tag is misleading; the
  generated English track is labelled **AI dub** and can be switched in the
  player without a re-encode. The channel name opens that channel on YouTube in
  a new tab; local video quality, audio codec/bitrate and file size are shown
  next to the title.
- **Search** — FTS5 hits with highlighted snippets and timestamp deep links.

Clicking a video from the playlist starts it immediately where the browser
allows autoplay; a large play button remains if the browser blocks it. Progress
is stored in `.ytop/data/progress.json` inside each library. The full route/JSON contract is in
[`docs/web.md`](docs/web.md).

The web UI is local-only by design: `ytop serve` refuses a non-loopback host,
unsafe browser requests are checked against `Origin`/`Sec-Fetch-Site`, and every
destructive form carries the page's CSRF token. Keyboard users get a skip link,
visible focus, accessible tabs/dialogs and status announcements.

## Developing

```bash
uv sync                      # Python 3.12 and the locked dependencies
tools/fetch_ffmpeg.sh        # pinned FFmpeg under .tools/ (tests decode H.264)
uv run pytest -q             # offline suite; never touches your real libraries
uv run ruff check src tests tools
uv run mypy                  # typed modules listed in pyproject.toml
```

The test suite runs offline and isolates `HOME` and the library registry.
`benchmarks/bench_library.py` is a synthetic, offline library read and index
benchmark.

## Docs

- [`docs/web.md`](docs/web.md): web routes, add and download jobs, transcript
  acquisition, the FTS5 schema, and the progress contract
- [`docs/why-native-streams.md`](docs/why-native-streams.md): why YTOP never
  re-encodes
- [`tools/ffmpeg-minimal/README.md`](tools/ffmpeg-minimal/README.md): the
  minimal FFmpeg build shipped on sticks
- [`docs/going-public.md`](docs/going-public.md): how this public repository
  was cut from the private one

## Licence

MIT, see [`LICENSE`](LICENSE). A stick also carries third-party programs and
libraries under their own licences, including the GPL-3.0
bgutil-ytdlp-pot-provider server, shipped unmodified. See
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

YTOP is not affiliated with or endorsed by YouTube or Google.
