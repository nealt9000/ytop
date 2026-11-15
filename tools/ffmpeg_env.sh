# shellcheck shell=bash
# Source this from shell scripts so `ffmpeg` and `ffprobe` resolve to the
# project-local static build instead of the crippled system ffmpeg-free.
_ytop_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
if [ -x "$_ytop_root/.tools/ffmpeg/bin/ffmpeg" ]; then
  export YTOP_FFMPEG="$_ytop_root/.tools/ffmpeg/bin/ffmpeg"
  export YTOP_FFPROBE="$_ytop_root/.tools/ffmpeg/bin/ffprobe"
  export YTOP_FFPLAY="$_ytop_root/.tools/ffmpeg/bin/ffplay"
  export PATH="$_ytop_root/.tools/ffmpeg/bin:$PATH"
fi
unset _ytop_root
