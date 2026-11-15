# Why YTOP keeps YouTube's own streams

YTOP stores the video and audio exactly as YouTube serves them. It never
downscales, drops frames or re-encodes. Before that was settled, it was
measured. This note summarises the measurements.

## The test

The test case was a lecture course of 12 videos and 12.6 hours, mostly
chalkboard lectures at 4:3, 320×240 and 15 fps. Small handwriting is the
hardest content to keep readable. Each candidate was scored with a stroke
legibility metric and then watched side by side at full size by a person.

| candidate | video + audio | course total | verdict |
|---|---|---|---|
| YouTube 144p | 38.3 MB/h | 482 MB | rejected: loses 30–50% of the readability to save 23% |
| **YouTube 240p, untouched** | **49.4 MB/h** | **623 MB** | **kept** |
| our 240p re-encode, 15 fps | 43.0 MB/h | 543 MB | rejected: visibly worse on review |
| our 240p re-encode, 5 fps | 31.5 MB/h | 397 MB | rejected: stutter; paused frames miss strokes |

## What it showed

- **Re-encoding loses to the original.** The denoising that made our re-encode
  smaller also removed fine chalk detail. The metric scored our re-encode
  higher, but the person watching saw that it was worse. The metric only
  measured contrast against noise, so it missed the lost detail.
- **Low frame rates hurt study.** At 5 fps a paused frame can land up to
  100 ms from the moment you want, and small strokes that are still being
  written disappear. The stutter also distracts.
- **144p is not worth it.** It saves too few bytes for what it costs in
  readability.
- **Audio is a large share.** It is 22.5 MB/h, which is 33–45% of the stored
  bytes depending on the video codec.
- **Frames are rarely static.** The camera pans and zooms all the time
  (`mpdecimate` keeps 99.6% of frames), so variable-frame-rate tricks gain
  little.

## What YTOP does instead

YTOP saves space by choosing which rendition to download, not by changing it.
The default **efficient** policy picks the best native rendition that fits a
playlist-wide budget of 100 MB per hour, with a 240p floor where the source
offers it. On a 121-video, 90-hour lecture playlist that comes to about
8.4 GiB instead of 30.2 GiB at native maximum, with no post-processing. The
review page shows both sizes before anything downloads, and **native** is one
click away.

Board snapshots (aligned, averaged frames for reading the chalk) were also
built and measured. They were removed because they did not earn their
complexity.
