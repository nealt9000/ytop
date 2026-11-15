#!/bin/sh
# YTOP Linux launcher. Run as: sh start-ytop-linux.sh
# Copies YTOP from the stick into ~/.cache/ytop the first time (FAT sticks
# are usually mounted without exec permission), then starts it from there.

STICK=$(cd "$(dirname "$0")" && pwd) || exit 1
YT="$STICK/.ytop"

BUILD_ID=$(sed -n 's/.*"build_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
    "$YT/build.json" 2>/dev/null | head -n 1)
if [ -z "$BUILD_ID" ]; then
    echo "This YTOP stick looks incomplete (no build information found)."
    exit 2
fi

case "$(uname -m)" in
    x86_64) PLAT=linux-x86_64 ;;
    aarch64|arm64) PLAT=linux-aarch64 ;;
    *)
        echo "This YTOP stick has no runtime for this computer ($(uname -m))."
        exit 3
        ;;
esac

if [ ! -f "$YT/runtime/$PLAT/python/bin/python3" ]; then
    echo "This YTOP stick has no runtime for $PLAT. Make the stick again with that platform included."
    exit 3
fi

ROOT="${XDG_CACHE_HOME:-$HOME/.cache}/ytop/runtime"
CACHE="$ROOT/$BUILD_ID"

LOCK="$CACHE.lock"
PART="$CACHE.partial.$$"
HAVE_LOCK=
cleanup() {
    rm -rf "$PART"
    [ -n "$HAVE_LOCK" ] && rmdir "$LOCK" 2>/dev/null
    HAVE_LOCK=
}
fail() {
    cleanup
    echo "Couldn't copy YTOP to $CACHE (is the disk full?)."
    exit 4
}

if [ ! -f "$CACHE/.complete" ]; then
    echo "First run on this computer: copying YTOP (this can take a minute)..."
    trap 'cleanup; exit 1' INT TERM HUP
    mkdir -p "$ROOT" || fail
    # Serialise staging: only one launcher copies at a time.
    tries=0
    while :; do
        if mkdir "$LOCK" 2>/dev/null; then
            HAVE_LOCK=1
            break
        fi
        [ -f "$CACHE/.complete" ] && break
        if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +10 2>/dev/null)" ]; then
            rmdir "$LOCK" 2>/dev/null   # stale lock from a dead launcher
            continue
        fi
        tries=$((tries + 1))
        if [ "$tries" -gt 120 ]; then
            echo "Another YTOP launcher is still copying to $CACHE; try again in a minute."
            exit 4
        fi
        sleep 1
    done
    if [ -n "$HAVE_LOCK" ] && [ ! -f "$CACHE/.complete" ]; then
        # Remove leftovers from interrupted runs (older than a day).
        find "$ROOT" -maxdepth 1 -name "$BUILD_ID.partial.*" -mmin +1440 \
            -exec rm -rf {} + 2>/dev/null
        rm -rf "$PART"
        mkdir -p "$PART/lib" "$PART/runtime" || fail
        cp -R "$YT/boot.py" "$YT/build.json" "$YT/app" "$PART/" || fail
        cp -R "$YT/lib/common" "$PART/lib/" || fail
        if [ -d "$YT/lib/$PLAT" ]; then
            cp -R "$YT/lib/$PLAT" "$PART/lib/" || fail
        fi
        cp -R "$YT/runtime/$PLAT" "$PART/runtime/" || fail
        chmod -R u+rwX "$PART" || fail
        for f in "$PART/runtime/$PLAT/python/bin/"* "$PART/runtime/$PLAT/bin/"*; do
            [ -f "$f" ] && chmod +x "$f"
        done
        touch "$PART/.complete" || fail
        # Never remove a cache that is complete; only an incomplete leftover.
        if [ ! -f "$CACHE/.complete" ]; then
            rm -rf "$CACHE"
            mv "$PART" "$CACHE" || fail
        fi
        [ -f "$CACHE/.complete" ] || fail
    fi
    cleanup
    trap - INT TERM HUP
fi

# Mark this build as recently used, then remove builds unused for 30 days.
touch "$CACHE" 2>/dev/null
for d in "$ROOT"/*; do
    [ -d "$d" ] || continue
    [ "$d" = "$CACHE" ] && continue
    if [ -n "$(find "$d" -maxdepth 0 -mtime +30 2>/dev/null)" ]; then
        rm -rf "$d"
    fi
done

# Leave the stick: a cwd on it keeps it "busy" and blocks ejecting it.
cd / || exit 1
exec "$CACHE/runtime/$PLAT/python/bin/python3" -s -E "$CACHE/boot.py" \
    --home "$STICK" --runtime-root "$CACHE"
