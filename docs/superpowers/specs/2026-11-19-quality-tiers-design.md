# Fixed resolution tiers on the download review

## Why

Efficient (100 MB/h budget) and Native (highest offered) are far apart. A real
case: a 348-video documentary playlist is 29.1 GB efficient and 162.2 GB native;
the owner wants it on a 128 GB stick (~119 GiB usable) at the best predictable
quality — 720p should fit. There is no way to pick that today.

## Goals

- Offer YouTube's standard resolutions as caps between Efficient and Native.
- Show each option's size before downloading, and whether it fits on the
  destination drive.
- A playlist keeps the quality it was downloaded with across syncs.

## Non-goals

- "Best quality that fits this drive" (budget = free space). Rejected: mixed
  resolutions per video and no headroom left for syncs.
- CLI changes (`--max-height` already exists).
- Re-encoding of any kind.

## Approaches

1. **Tiers as more options on the existing `max_height` cap (picked).**
   `select_video` already honours a cap, prefers AV1 > VP9 > H.264 at the chosen
   height, and keeps lower-resolution videos native. Only the review, the job
   policy and persistence need work.
2. A separate per-tier selection policy. Rejected: duplicates the codec and
   fallback logic for no gain.
3. Fit-to-drive budget via `allocate_efficient`. Rejected by the owner in favour
   of predictable fixed tiers.

## Design

- **Tiers:** 480p, 720p, 1080p, 1440p, 2160p. Efficient stays the floor option
  and the default for a new playlist; Native stays the top. A tier is listed
  only if its total is smaller than Native for this playlist (no tier at or
  above the playlist's maximum height).
- **Estimates:** computed in the review from the formats already probed, like
  the Native total: per item, `select_format(info, lang, H, preserve_audio)`.
  No extra network.
- **Selection under a cap:** existing `select_video` behaviour — best rendition
  at or below the cap; a video offered only above the cap falls back to its
  smallest rendition. Download with `target_mb_per_hour = 0`, `max_height = H`.
- **Review page:** one radio per option with its size and a fits / doesn't-fit
  marker against the destination's free space, e.g.
  `720p max — 98.4 GB  ✓ fits (118 GB free)`. Per-item table unchanged.
- **Persistence:** the chosen policy is stored in the playlist's options (like
  `preserve_audio`); a sync's review preselects it. Job/playlist labels read
  e.g. "720p max".
- **Single videos:** the add-video review offers the same options.

## Calls made by Claude (owner to check)

- Floor tier is 480p (no 360p tier).
- Sync remembers the last choice.
- "Fits" compares the shown total against the drive's current free space.

## Left to the builder

- Policy encoding on the job and form (e.g. `policy=cap` + height, or
  `policy=720p`), validated server-side against the tier list.
- Exact free-space source for a library (existing `fsinfo` helpers) and how a
  sync's already-downloaded items are counted in the fits check.
- Live-update (`live.js`) text for tier estimates.
