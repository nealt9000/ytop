# ffmpeg-minimal

A small static `ffmpeg` + `ffprobe` for YTOP sticks: just enough to probe,
merge and remux what yt-dlp downloads (AV1/VP9/H.264/HEVC + Opus/AAC/Vorbis in
WebM/MP4) and to cut JPEG preview frames. ffmpeg + dav1d are pinned in
`build.sh` (ffmpeg `n7.1.5`, dav1d `1.5.4`, both checked against their commit).

| File | Purpose |
| --- | --- |
| `build.sh <platform> <outdir>` | Builds and writes `ffmpeg-minimal-<platform>.tar.gz` (+ `.sha256`) holding `bin/ffmpeg[.exe]` and `bin/ffprobe[.exe]` |
| `Containerfile.linux` | Debian bookworm, native gcc: `linux-x86_64`, `linux-aarch64` |
| `Containerfile.mingw` | Debian bookworm, mingw-w64 cross: `windows-x86_64` |
| `verify.py <ffmpeg> <ffprobe> <fixtures_dir>` | Exit 0 only if probing, `-c copy` merges, mp4 remux, preview JPEGs and the 30 MiB size cap all pass |
| `../../.github/workflows/ffmpeg-minimal.yml` | Builds all 5 platforms, verifies each on a native runner, publishes release `ffmpeg-minimal-<n>` |

## Building locally

```sh
podman build -t ytop-ffmpeg-linux -f tools/ffmpeg-minimal/Containerfile.linux tools/ffmpeg-minimal
podman run --rm -v "$PWD/dist:/out:Z" ytop-ffmpeg-linux linux-x86_64 /out

podman build -t ytop-ffmpeg-mingw -f tools/ffmpeg-minimal/Containerfile.mingw tools/ffmpeg-minimal
podman run --rm -v "$PWD/dist:/out:Z" ytop-ffmpeg-mingw windows-x86_64 /out

mkdir -p dist/check && tar -xzf dist/ffmpeg-minimal-linux-x86_64.tar.gz -C dist/check
python3 tools/ffmpeg-minimal/verify.py dist/check/bin/ffmpeg dist/check/bin/ffprobe tests/fixtures/media
```

macOS builds run natively (`brew install nasm meson ninja pkg-config`, then
`tools/ffmpeg-minimal/build.sh macos-arm64 dist`); `build.sh` sets
`MACOSX_DEPLOYMENT_TARGET=11.0`. In CI `macos-arm64` builds on `macos-15` and
`macos-x86_64` on `macos-15-intel` (GitHub retired the `macos-13` and `macos-14` images).

## Releasing

Push a tag `ffmpeg-minimal-<n>` (first unused `n`), or run the workflow by hand
with `release: true`. The release job runs only after all five verifies pass.

## Test fixtures

`tests/fixtures/media/` holds 1-second 160x90 clips (each < 50 KB) made with
the dev ffmpeg in `.tools/ffmpeg` (`tools/fetch_ffmpeg.sh`), from the repo root:

```sh
cd tests/fixtures/media
F=../../../.tools/ffmpeg/bin/ffmpeg
V="-f lavfi -i testsrc2=size=160x90:rate=24:duration=1"
A="-f lavfi -i sine=frequency=440:sample_rate=48000:duration=1"
O="-map_metadata -1 -fflags +bitexact"
$F -y $V $A -c:v libaom-av1 -cpu-used 8 -crf 50 -b:v 0 -g 24 -pix_fmt yuv420p -c:a libopus -b:a 24k -ac 1 $O av1_opus.webm
$F -y $V $A -c:v libvpx-vp9 -deadline realtime -cpu-used 8 -crf 50 -b:v 0 -g 24 -c:a libopus -b:a 24k -ac 1 $O vp9_opus.webm
$F -y $V $A -c:v libx264 -preset veryfast -crf 35 -g 24 -pix_fmt yuv420p -c:a aac -b:a 32k -ac 1 -movflags +faststart $O h264_aac.mp4
$F -y $V -c:v libaom-av1 -cpu-used 8 -crf 50 -b:v 0 -g 24 -pix_fmt yuv420p $O av1.webm
$F -y $A -c:a libopus -b:a 24k -ac 1 $O opus.webm
$F -y $V -c:v libx264 -preset veryfast -crf 35 -g 24 -pix_fmt yuv420p -movflags +faststart $O h264.mp4
$F -y $A -c:a aac -b:a 32k -ac 1 -movflags +faststart $O aac.m4a
```

`av1.webm` + `opus.webm` and `h264.mp4` + `aac.m4a` are the separate-stream
pairs used for the merge checks.
