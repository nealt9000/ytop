#!/usr/bin/env bash
# Build a minimal static ffmpeg + ffprobe for YTOP.
#
#   build.sh <platform> <outdir>
#
# platform: linux-x86_64 | linux-aarch64 | windows-x86_64 | macos-arm64 | macos-x86_64
#
# Produces <outdir>/ffmpeg-minimal-<platform>.tar.gz (bin/ffmpeg[.exe],
# bin/ffprobe[.exe]) and <outdir>/ffmpeg-minimal-<platform>.tar.gz.sha256.
# Linux builds run in Containerfile.linux, Windows in Containerfile.mingw
# (cross), macOS natively on a macOS runner with nasm/meson/ninja from brew.
set -euo pipefail

FFMPEG_TAG=n7.1.5
FFMPEG_COMMIT=3a0867c2bfda4a4d4309ca1a8cbdc6175e67f587
FFMPEG_REPO=https://git.ffmpeg.org/ffmpeg.git
DAV1D_TAG=1.5.4
DAV1D_COMMIT=54706fc6bc0cdecab7e9593974a4039cc038fca7
DAV1D_REPO=https://code.videolan.org/videolan/dav1d.git

usage() {
    echo "usage: $0 <platform> <outdir>" >&2
    echo "platforms: linux-x86_64 linux-aarch64 windows-x86_64 macos-arm64 macos-x86_64" >&2
    exit 2
}

[ $# -eq 2 ] || usage
PLATFORM=$1
mkdir -p "$2"
OUTDIR=$(cd "$2" && pwd)
WORK=${WORK:-$(mktemp -d "${TMPDIR:-/tmp}/ffmpeg-minimal.XXXXXX")}
PREFIX=$WORK/prefix
STAGE=$WORK/stage
EXE=""
JOBS=${JOBS:-$( (nproc || sysctl -n hw.ncpu) 2>/dev/null || echo 2)}

case "$PLATFORM" in
    linux-x86_64|linux-aarch64)
        PLATFORM_FLAGS=(--enable-pthreads --extra-ldflags=-static)
        MESON_CROSS=()
        ;;
    windows-x86_64)
        EXE=.exe
        CROSS=x86_64-w64-mingw32
        PLATFORM_FLAGS=(
            --enable-cross-compile --target-os=mingw32 --arch=x86_64
            --cross-prefix=$CROSS- --enable-w32threads
            "--extra-ldflags=-static -static-libgcc"
        )
        MESON_CROSS=(--cross-file "$WORK/mingw.meson")
        ;;
    macos-arm64|macos-x86_64)
        export MACOSX_DEPLOYMENT_TARGET=11.0
        PLATFORM_FLAGS=(--cc=clang --enable-pthreads)
        MESON_CROSS=()
        ;;
    *)
        usage
        ;;
esac

fetch() {  # fetch <repo> <tag> <commit> <dir>
    git -c advice.detachedHead=false clone --quiet --depth 1 --branch "$2" "$1" "$4"
    local got
    got=$(git -C "$4" rev-parse HEAD)
    if [ "$got" != "$3" ]; then
        echo "error: $1 $2 is $got, expected $3" >&2
        exit 1
    fi
}

echo "==> building ffmpeg $FFMPEG_TAG + dav1d $DAV1D_TAG for $PLATFORM in $WORK"
mkdir -p "$PREFIX" "$STAGE/bin"
fetch "$DAV1D_REPO" "$DAV1D_TAG" "$DAV1D_COMMIT" "$WORK/dav1d"
fetch "$FFMPEG_REPO" "$FFMPEG_TAG" "$FFMPEG_COMMIT" "$WORK/ffmpeg"

if [ "$PLATFORM" = windows-x86_64 ]; then
    cat > "$WORK/mingw.meson" <<EOF
[binaries]
c = '$CROSS-gcc'
ar = '$CROSS-ar'
strip = '$CROSS-strip'
windres = '$CROSS-windres'
pkgconfig = 'pkg-config'

[host_machine]
system = 'windows'
cpu_family = 'x86_64'
cpu = 'x86_64'
endian = 'little'
EOF
fi

echo "==> dav1d"
meson setup "$WORK/dav1d-build" "$WORK/dav1d" ${MESON_CROSS[@]+"${MESON_CROSS[@]}"} \
    --prefix="$PREFIX" --libdir=lib --buildtype=release \
    -Ddefault_library=static -Denable_tools=false -Denable_tests=false \
    -Denable_examples=false
ninja -C "$WORK/dav1d-build" install

echo "==> ffmpeg"
export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig"
export PKG_CONFIG_LIBDIR="$PREFIX/lib/pkgconfig"
cd "$WORK/ffmpeg"
./configure \
    --prefix="$PREFIX" \
    --pkg-config=pkg-config --pkg-config-flags=--static \
    --extra-cflags="-I$PREFIX/include" --extra-ldflags="-L$PREFIX/lib" \
    "${PLATFORM_FLAGS[@]}" \
    --disable-everything --disable-autodetect --disable-doc --disable-network \
    --disable-ffplay --enable-ffmpeg --enable-ffprobe --enable-small \
    --enable-libdav1d --enable-protocol=file,pipe \
    --enable-demuxer=mov,matroska,ogg,aac,mp3,image2 \
    --enable-muxer=mp4,mov,ipod,matroska,webm,ogg,opus,mjpeg,image2 \
    --enable-parser=h264,hevc,vp8,vp9,av1,opus,aac,vorbis,mjpeg \
    --enable-bsf=aac_adtstoasc,h264_mp4toannexb,hevc_mp4toannexb,vp9_superframe,av1_frame_merge,extract_extradata \
    --enable-decoder=h264,hevc,vp8,vp9,libdav1d,opus,aac,vorbis,mjpeg,png \
    --enable-encoder=mjpeg \
    --enable-filter=scale,format,null,anull,aresample
make -j"$JOBS"
cp "ffmpeg$EXE" "ffprobe$EXE" "$STAGE/bin/"

# Post-build assertions: the binaries must be self-contained for the platform.
check_binaries() {
    local b out bad minos
    case "$PLATFORM" in
    linux-*)
        for b in ffmpeg ffprobe; do
            out=$(ldd "$STAGE/bin/$b" 2>&1 || true)
            case "$out" in
                *"not a dynamic executable"*|*"statically linked"*) ;;
                *) echo "error: $b is not static:" >&2; echo "$out" >&2; exit 1 ;;
            esac
        done
        ;;
    windows-*)
        for b in ffmpeg ffprobe; do
            # Capture unmasked: a missing/failing objdump must fail the build.
            out=$("$CROSS-objdump" -p "$STAGE/bin/$b.exe") \
                || { echo "error: $CROSS-objdump failed on $b.exe" >&2; exit 1; }
            bad=$(printf '%s\n' "$out" | sed -n 's/^[[:space:]]*DLL Name: *//p' | tr -d '\r' \
                | grep -viE '^(KERNEL32|msvcrt|bcrypt|SHELL32|ADVAPI32|USER32|ole32|WS2_32)\.dll$' || true)
            if [ -n "$bad" ]; then
                echo "error: $b.exe imports DLLs outside the allowlist:" >&2
                echo "$bad" >&2
                exit 1
            fi
        done
        ;;
    macos-*)
        for b in ffmpeg ffprobe; do
            out=$(otool -L "$STAGE/bin/$b") \
                || { echo "error: otool -L failed on $b" >&2; exit 1; }
            bad=$(printf '%s\n' "$out" | tail -n +2 | awk '{print $1}' \
                | grep -vE '^(/usr/lib/|/System/Library/)' || true)
            if [ -n "$bad" ]; then
                echo "error: $b links non-system libraries:" >&2
                echo "$bad" >&2
                exit 1
            fi
            out=$(otool -l "$STAGE/bin/$b") \
                || { echo "error: otool -l failed on $b" >&2; exit 1; }
            minos=$(printf '%s\n' "$out" | awk '
                $1=="cmd" {cur=$2}
                cur=="LC_BUILD_VERSION" && $1=="minos" && !bv {bv=$2}
                cur=="LC_VERSION_MIN_MACOSX" && $1=="version" && !mv {mv=$2}
                END {print (bv != "" ? bv : mv)}')
            if [ -z "$minos" ]; then
                echo "error: $b has no LC_BUILD_VERSION/LC_VERSION_MIN_MACOSX" >&2
                exit 1
            fi
            # numeric compare against 11.0 (accepts 11, 11.0, 11.0.0)
            if ! awk -v v="$minos" 'BEGIN {
                    n = split(v, a, "."); a[1] += 0; a[2] += 0; a[3] += 0
                    exit !(a[1] < 11 || (a[1] == 11 && a[2] == 0 && a[3] == 0)) }'; then
                echo "error: $b minos $minos is above 11.0" >&2
                exit 1
            fi
        done
        ;;
    esac
    echo "==> binary checks passed ($PLATFORM)"
}
check_binaries

NAME=ffmpeg-minimal-$PLATFORM.tar.gz
tar -czf "$OUTDIR/$NAME" -C "$STAGE" bin
cd "$OUTDIR"
if command -v sha256sum >/dev/null; then
    sha256sum "$NAME" > "$NAME.sha256"
else
    shasum -a 256 "$NAME" > "$NAME.sha256"
fi
ls -l "$STAGE/bin"
cat "$NAME.sha256"
