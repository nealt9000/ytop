#!/bin/sh
cd "$(dirname "$0")" || exit 1

case "$(uname -m)" in
    arm64) PLAT=macos-arm64 ;;
    x86_64) PLAT=macos-x86_64 ;;
    *)
        echo "This YTOP stick has no runtime for this computer ($(uname -m))."
        exit 3
        ;;
esac

if [ ! -f ".ytop/runtime/$PLAT/python/bin/python3" ]; then
    echo "This YTOP stick has no runtime for $PLAT. Make the stick again with that platform included."
    exit 3
fi

xattr -dr com.apple.quarantine ".ytop/runtime/$PLAT" 2>/dev/null || true
exec ".ytop/runtime/$PLAT/python/bin/python3" -s -E ".ytop/boot.py"
