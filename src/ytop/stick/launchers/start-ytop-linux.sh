#!/bin/sh
# YTOP Linux launcher. Run as: sh start-ytop-linux.sh
# Copies YTOP from the stick into ~/.cache/ytop the first time (FAT sticks
# are usually mounted without exec permission), then starts it from there.

STICK=$(cd "$(dirname "$0")" && pwd) || exit 1
YT="$STICK/.ytop"

ROOT="${XDG_CACHE_HOME:-$HOME/.cache}/ytop/runtime"

build_id() {
    sed -n 's/.*"build_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
        "$YT/build.json" 2>/dev/null | head -n 1
}

# An upgrade cut off half way leaves no .ytop/app, so there is nothing to
# stage.  Let the stick's own boot.py put the old version back (it needs a
# Python: any complete cache's, else a copy of the stick's own), then stop.
if [ -d "$STICK/.ytop.old" ] && [ ! -e "$YT/app" ]; then
    BEFORE=$(build_id)
    case "$(uname -m)" in
        x86_64) RP=linux-x86_64 ;;
        aarch64|arm64) RP=linux-aarch64 ;;
        *) RP= ;;
    esac
    TMP=
    PY=
    # Interrupted here, take the temp copy away too (the shell runs this once
    # boot.py has exited, so a repair under way is never cut short by it).
    repair_cleanup() {
        AFTER=$(build_id)
        # A cache staged around the interruption may mix two builds.
        for b in "$BEFORE" "$AFTER"; do
            [ -n "$b" ] && rm -f "$ROOT/$b/.complete"
        done
        [ -n "$TMP" ] && rm -rf "$TMP"
        TMP=
    }
    trap 'repair_cleanup; exit 1' INT TERM HUP
    if [ -n "$RP" ] && [ -f "$YT/boot.py" ] && mkdir -p "$ROOT" 2>/dev/null; then
        for d in "$ROOT"/*; do
            [ -f "$d/.complete" ] && [ -x "$d/runtime/$RP/python/bin/python3" ] || continue
            if [ -z "$PY" ] || [ "$d/.complete" -nt "$PY/.complete" ]; then
                PY=$d
            fi
        done
        [ -n "$PY" ] && PY="$PY/runtime/$RP/python/bin/python3"
        TMP=$(mktemp -d "$ROOT/repair.XXXXXX" 2>/dev/null)
        if [ -n "$TMP" ] && [ -z "$PY" ] && [ -d "$YT/runtime/$RP/python" ] \
            && cp -R "$YT/runtime/$RP/python" "$TMP/python" 2>/dev/null \
            && chmod -R u+rwX "$TMP/python" 2>/dev/null; then
            chmod +x "$TMP/python/bin/"* 2>/dev/null
            PY="$TMP/python/bin/python3"
        fi
    fi
    if [ -n "$TMP" ] && [ -n "$PY" ] && [ -x "$PY" ]; then
        mkdir -p "$TMP/root"
        (cd / && "$PY" -s -E "$YT/boot.py" --home "$STICK" --runtime-root "$TMP/root")
        CODE=$?
        repair_cleanup
        exit "$CODE"
    fi
    [ -n "$TMP" ] && rm -rf "$TMP"
    echo "YTOP's last upgrade on this stick was interrupted. Open YTOP from another stick and make or upgrade this one again."
    exit 5
fi

BUILD_ID=$(build_id)
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

CACHE="$ROOT/$BUILD_ID"

LOCK="$CACHE.lock"
PART="$CACHE.partial.$$"
HAVE_LOCK=
# The staging lock is a directory; its "owner" file names the launcher
# holding it: "<pid> <start ticks since boot>" (ticks: a reused pid, or a
# wall-clock step, never passes for the holder).
# (Field 22 of /proc/<pid>/stat, the 20th after "pid (comm) "; sed only:
# the launcher uses no tools beyond those tools/smoke_stick.py allows.)
start_ticks() {
    sed -n 's/.*) //; s/^\([^ ]* \)\{19\}\([0-9]*\).*/\2/p' "/proc/$1/stat" 2>/dev/null
}
SELF=
T=$(start_ticks $$)
[ -n "$T" ] && SELF="$$ $T"
release_lock() {
    [ -n "$HAVE_LOCK" ] || return 0
    HAVE_LOCK=
    # Not ours any more (broken and taken by another launcher): leave it.
    if [ -f "$LOCK/owner" ] && [ "$(cat "$LOCK/owner" 2>/dev/null)" != "$SELF" ]; then
        return 0
    fi
    rm -rf "$LOCK"
}
cleanup() {
    rm -rf "$PART"
    release_lock
}
# Is the lock directory $1 stale?  With an owner record: when that process
# is gone (however old the lock; a copy may take long).  Without one (an
# older launcher, or one just taking it): when untouched for 10 minutes.
lock_stale() {
    owner=
    [ -f "$1/owner" ] && owner=$(cat "$1/owner" 2>/dev/null)
    opid=${owner%% *}
    oticks=${owner#* }
    [ "$opid" = "$owner" ] && opid=   # no "<pid> <ticks>" pair
    case "$opid:$oticks" in
        *[!0-9:]*|:*|*:) ;;
        *)
            [ "$(start_ticks "$opid")" != "$oticks" ]
            return
            ;;
    esac
    [ -n "$(find "$1" -maxdepth 0 -mmin +10 2>/dev/null)" ]
}
# Break a stale lock atomically: rename it to a name only this launcher
# uses, then look again.  Another waiter may have broken it first and taken
# a fresh lock under the same name; that one is put back, not removed.
break_lock() {
    aside="$LOCK.stale.$$"
    rm -rf "$aside"
    mv "$LOCK" "$aside" 2>/dev/null || return 0
    if lock_stale "$aside"; then
        rm -rf "$aside"
        return 0
    fi
    [ -e "$LOCK" ] || mv "$aside" "$LOCK" 2>/dev/null
    rm -rf "$aside"
}
fail() {
    cleanup
    echo "Couldn't copy YTOP to $CACHE (is the disk full?)."
    exit 4
}

# Newest complete cache other than $CACHE, by the mtime of its .complete file
# (the launcher touches the cache dir on use, but .complete is set only once
# per build, so it is a stable, comparable choice).  Prints its path.
previous_cache() {
    best=
    for d in "$ROOT"/*/; do
        d=${d%/}
        case "$d" in
            "$CACHE"|*.partial.*|*.lock|*.lock.stale.*|*/repair.*) continue ;;
        esac
        [ -f "$d/.complete" ] || continue
        if [ -z "$best" ] || [ "$d/.complete" -nt "$best/.complete" ]; then
            best=$d
        fi
    done
    [ -n "$best" ] && printf '%s\n' "$best"
}

# Fill $PART from an earlier cache $1, copying from the stick only the files
# whose checksum differs from the stick's manifest.  Returns non-zero on any
# problem; the caller then falls back to a full copy.
stage_incremental() {
    PREV=$1
    cp -R "$PREV/." "$PART/" || return 1
    rm -f "$PART/.complete"
    # The manifest lines for the files this cache holds.
    grep -E "^[0-9a-f]{64}  (boot\.py\$|app/|lib/common/|lib/$PLAT/|runtime/$PLAT/)" \
        "$YT/manifest.sha256" > "$PART/.want.sha256" || return 1
    # LC_ALL=C: sha256sum translates "FAILED" in other locales.
    (cd "$PART" && LC_ALL=C sha256sum -c --quiet .want.sha256 2>/dev/null) \
        | LC_ALL=C sed -n 's/: FAILED\( open or read\)\{0,1\}$//p' > "$PART/.changed" || return 1
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        mkdir -p "$PART/$(dirname "$f")" || return 1
        cp "$YT/$f" "$PART/$f" || return 1
    done < "$PART/.changed"
    rm -f "$PART/.changed"
    # Drop files the new build no longer has.
    (cd "$PART" && find . -type f | sed 's|^\./||' | sort) > "$PART/.have"
    sed 's/^[0-9a-f]*  //' "$PART/.want.sha256" | sort > "$PART/.wantnames"
    comm -23 "$PART/.have" "$PART/.wantnames" | while IFS= read -r f; do
        case "$f" in
            build.json|.want.sha256|.complete|.have|.wantnames) ;;
            *) rm -f "$PART/$f" ;;
        esac
    done
    rm -f "$PART/.have" "$PART/.wantnames"
    # -depth: a dropped package's __pycache__/ goes first, then the package
    # (an empty folder would import as a namespace package).
    find "$PART" -mindepth 1 -depth -type d -empty -exec rmdir {} \; 2>/dev/null
    cp "$YT/build.json" "$PART/build.json" || return 1
    chmod -R u+rwX "$PART" || return 1
    # LC_ALL=C: sha256sum translates "FAILED" in other locales.
    (cd "$PART" && LC_ALL=C sha256sum -c --quiet .want.sha256 >/dev/null 2>&1) || return 1
    return 0
}

stage_full() {
    rm -rf "$PART"
    mkdir -p "$PART/lib" "$PART/runtime" || fail
    cp -R "$YT/boot.py" "$YT/build.json" "$YT/app" "$PART/" || fail
    cp -R "$YT/lib/common" "$PART/lib/" || fail
    if [ -d "$YT/lib/$PLAT" ]; then
        cp -R "$YT/lib/$PLAT" "$PART/lib/" || fail
    fi
    cp -R "$YT/runtime/$PLAT" "$PART/runtime/" || fail
}

if [ ! -f "$CACHE/.complete" ]; then
    PREV=
    if [ -f "$YT/manifest.sha256" ] && command -v sha256sum >/dev/null 2>&1; then
        PREV=$(previous_cache)
    fi
    if [ -n "$PREV" ]; then
        echo "Updating YTOP's copy on this computer (only what changed)..."
    else
        echo "First run on this computer: copying YTOP (this can take a minute)..."
    fi
    trap 'cleanup; exit 1' INT TERM HUP
    mkdir -p "$ROOT" || fail
    # Serialise staging: only one launcher copies at a time.
    tries=0
    while :; do
        # Taking the lock and recording that we hold it are one step: an
        # interrupt in between would orphan the lock, so it waits.
        INTR=
        trap 'INTR=1' INT TERM HUP
        if mkdir "$LOCK" 2>/dev/null; then
            HAVE_LOCK=1
            if [ -n "$SELF" ]; then
                printf '%s\n' "$SELF" > "$LOCK/owner.$$" \
                    && mv "$LOCK/owner.$$" "$LOCK/owner"
            fi
        fi
        trap 'cleanup; exit 1' INT TERM HUP
        if [ -n "$INTR" ]; then
            cleanup
            exit 1
        fi
        [ -n "$HAVE_LOCK" ] && break
        [ -f "$CACHE/.complete" ] && break
        tries=$((tries + 1))
        if [ "$tries" -gt 120 ]; then
            echo "Another YTOP launcher is still copying to $CACHE; try again in a minute."
            exit 4
        fi
        if lock_stale "$LOCK"; then
            break_lock   # left by a launcher that is gone
            continue
        fi
        sleep 1
    done
    if [ -n "$HAVE_LOCK" ] && [ ! -f "$CACHE/.complete" ]; then
        # Remove leftovers from interrupted runs (older than a day).
        find "$ROOT" -maxdepth 1 -name "$BUILD_ID.partial.*" -mmin +1440 \
            -exec rm -rf {} + 2>/dev/null
        rm -rf "$PART"
        mkdir -p "$PART" || fail
        if [ -n "$PREV" ] && stage_incremental "$PREV"; then
            :
        else
            stage_full
        fi
        rm -f "$PART/.want.sha256"
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
PYTHON="$CACHE/runtime/$PLAT/python/bin/python3"
[ -x "$PYTHON" ] || chmod +x "$PYTHON" 2>/dev/null
if [ ! -x "$PYTHON" ]; then
    # Typically a home or cache disk mounted noexec: exec would only say 126.
    echo "Couldn't start YTOP from $CACHE: that disk doesn't allow running programs (mounted noexec?). Set XDG_CACHE_HOME to a folder on another disk and start YTOP again."
    exit 126
fi
exec "$PYTHON" -s -E "$CACHE/boot.py" \
    --home "$STICK" --runtime-root "$CACHE"
