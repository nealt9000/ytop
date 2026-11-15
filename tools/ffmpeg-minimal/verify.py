"""Check that a minimal ffmpeg/ffprobe pair does everything YTOP needs.

Exits 0 only when every check passes. Uses only the standard library so it
runs on a bare CI runner (including Windows) without the project installed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

MAX_COMBINED_BYTES = 30 * 1024 * 1024

PROBE_FIXTURES = [
    "av1_opus.webm",
    "vp9_opus.webm",
    "h264_aac.mp4",
    "av1.webm",
    "opus.webm",
    "h264.mp4",
    "aac.m4a",
]
MERGES = [
    ("av1.webm", "opus.webm", "merged.webm"),
    ("h264.mp4", "aac.m4a", "merged.mp4"),
]
PREVIEW_FIXTURES = ["av1_opus.webm", "vp9_opus.webm", "h264_aac.mp4"]


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def probe_streams(ffprobe: str, path: Path) -> list[dict]:
    result = run(
        [ffprobe, "-v", "error", "-show_streams", "-of", "json", str(path)]
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffprobe failed")
    streams = json.loads(result.stdout).get("streams", [])
    if not streams:
        raise RuntimeError("no streams")
    return streams


def preview_cmd(ffmpeg: str, media: Path, out: Path, t: float = 0.5) -> list[str]:
    # Same arguments as the /media/.../preview route in src/ytop/web/app.py.
    return [
        ffmpeg, "-nostdin", "-loglevel", "error",
        "-ss", f"{max(0.0, float(t)):.3f}",
        "-i", str(media), "-frames:v", "1", "-an", "-sn",
        "-vf", "scale=160:-2", "-q:v", "4", "-y", str(out),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ffmpeg", help="path to the ffmpeg binary")
    parser.add_argument("ffprobe", help="path to the ffprobe binary")
    parser.add_argument("fixtures_dir", help="directory with the media fixtures")
    args = parser.parse_args(argv)

    ffmpeg, ffprobe = args.ffmpeg, args.ffprobe
    fixtures = Path(args.fixtures_dir)
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"{'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
        if not ok:
            failures.append(name)

    for name in PROBE_FIXTURES:
        try:
            streams = probe_streams(ffprobe, fixtures / name)
            codecs = ",".join(s.get("codec_name", "?") for s in streams)
            check(f"probe {name}", True, codecs)
        except Exception as exc:
            check(f"probe {name}", False, str(exc))

    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)

        for video, audio, out_name in MERGES:
            out = tmp / out_name
            result = run([
                ffmpeg, "-nostdin", "-loglevel", "error", "-y",
                "-i", str(fixtures / video), "-i", str(fixtures / audio),
                "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", str(out),
            ])
            label = f"merge {video}+{audio} -> {out_name}"
            if result.returncode != 0:
                check(label, False, result.stderr.strip())
                continue
            try:
                count = len(probe_streams(ffprobe, out))
                check(label, count == 2, f"{count} streams")
            except Exception as exc:
                check(label, False, str(exc))

        out = tmp / "remux.mp4"
        result = run([
            ffmpeg, "-nostdin", "-loglevel", "error", "-y",
            "-i", str(fixtures / "h264_aac.mp4"), "-c", "copy", "-f", "mp4",
            str(out),
        ])
        ok = result.returncode == 0 and out.is_file() and out.stat().st_size > 0
        check("remux h264_aac.mp4 -f mp4 -c copy", ok, result.stderr.strip())

        for name in PREVIEW_FIXTURES:
            out = tmp / (Path(name).stem + ".jpg")
            result = run(preview_cmd(ffmpeg, fixtures / name, out))
            ok = result.returncode == 0 and out.is_file()
            if ok:
                ok = out.read_bytes()[:3] == b"\xff\xd8\xff"
            detail = result.stderr.strip() or (
                f"{out.stat().st_size} bytes" if out.is_file() else "no output"
            )
            check(f"preview jpeg {name}", ok, detail)

    sizes = {p: Path(p).stat().st_size for p in (ffmpeg, ffprobe)}
    for path, size in sizes.items():
        print(f"size  {path}: {size} bytes ({size / 1048576:.2f} MiB)")
    total = sum(sizes.values())
    check(
        "combined size <= 30 MiB",
        total <= MAX_COMBINED_BYTES,
        f"{total} bytes ({total / 1048576:.2f} MiB)",
    )

    if failures:
        print(f"{len(failures)} check(s) failed", file=sys.stderr)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
