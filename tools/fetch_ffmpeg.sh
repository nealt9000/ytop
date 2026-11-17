#!/usr/bin/env bash
# Fetch the pinned static FFmpeg build into .tools/ffmpeg.
#
# The distro ffmpeg on this machine (ffmpeg-free) has H.264 decoding disabled.
# This build is GPL, includes libx264/libx265/libvpx/libsvtav1/libaom/libopus
# and libvmaf, and can decode H.264/HEVC/AV1/VP9.
#
# Pinned by release tag + SHA-256 so every session gets the same binary.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
DEST="${1:-$ROOT/.tools/ffmpeg}"
CACHE="$ROOT/.tools/dl/archive.tar.xz"
ASSET="ffmpeg-N-128790-g9bee1e603c-linux64-gpl.tar.xz"
SHA256="f5752bf3597cbc9fc7a60fe5cea3522797a5ea2493d91cffb6f13e5b4c861233"
# The same file mirrored as an asset of this repo's release ci-ffmpeg-1
# (FFmpeg-Builds autobuilds expire), then the upstream original.  Both are
# checked against SHA256, so the fallback cannot change the binary.
URLS=(
  "https://github.com/nealt9000/ytop/releases/download/ci-ffmpeg-1/$ASSET"
  "https://github.com/FFmpeg-Builds/FFmpeg-Builds/releases/download/autobuild-2026-11-11-12-39/$ASSET"
)

if [ -x "$DEST/bin/ffmpeg" ] && [ "${FORCE:-0}" != "1" ]; then
  echo "already present: $DEST/bin/ffmpeg"
  "$DEST/bin/ffmpeg" -version | head -1
  exit 0
fi

mkdir -p "$(dirname "$CACHE")" "$DEST"
if [ ! -f "$CACHE" ]; then
  for url in "${URLS[@]}"; do
    echo "downloading $url"
    if curl -fL --retry 3 --retry-delay 2 -o "$CACHE.part" "$url"; then
      mv "$CACHE.part" "$CACHE"
      break
    fi
    rm -f "$CACHE.part"
  done
  if [ ! -f "$CACHE" ]; then
    echo "could not download $ASSET from any source" >&2
    exit 1
  fi
fi

echo "$SHA256  $CACHE" | sha256sum -c -

if [ -n "$(find "$DEST" -mindepth 1 -maxdepth 1 2>/dev/null)" ]; then
  echo "refusing to overwrite non-empty $DEST (set FORCE=1 to replace)" >&2
  exit 1
fi
tar -xJf "$CACHE" -C "$DEST" --strip-components=1

echo "installed: $DEST/bin/ffmpeg"
"$DEST/bin/ffmpeg" -version | head -1
"$DEST/bin/ffmpeg" -hide_banner -buildconf | grep -oE -- '--enable-(gpl|libx264|libx265|libvpx|libsvtav1|libaom|libopus|libvmaf)' | sort -u
