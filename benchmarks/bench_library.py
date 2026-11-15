"""Synthetic, offline benchmark for the library read paths.

Run from the repository root with the project virtualenv::

    .venv/bin/python benchmarks/bench_library.py

It never touches a real library or the network. Use ``--json`` for a machine
readable result that can be compared across revisions.
"""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient

from ytop.config import Config
from ytop.transcripts.store import index_library
from ytop.web import library as library_module
from ytop.yt.metadata import human_bytes, write_json

BIG_ID = "PLBENCH00000000000000000000001"


def build_library(
    root: Path, *, playlists: int = 12, videos: int = 12
) -> None:
    for playlist_index in range(playlists):
        playlist_id = (
            BIG_ID if playlist_index == 0
            else f"PLBENCH{playlist_index:019d}"
        )
        count = videos if playlist_index else max(videos, 121)
        playlist_dir = root / "playlists" / f"Bench {playlist_index} [{playlist_id}]"
        entries = []
        for video_index in range(count):
            video_id = f"v{video_index:03d}"
            video_dir = playlist_dir / "videos" / video_id
            video_dir.mkdir(parents=True)
            (video_dir / "video.mp4").write_bytes(b"bench")
            write_json(video_dir / "meta.json", {
                "schema": 1,
                "id": video_id,
                "title": f"Video {video_index}",
                "duration": 600,
                "channel": "Bench Channel",
                "files": {"video": "video.mp4"},
            })
            write_json(video_dir / "transcript.json", {
                "schema": 1,
                "video_id": video_id,
                "title": f"Video {video_index}",
                "language": "en",
                "kind": "auto",
                "segments": [
                    {
                        "start": segment * 3,
                        "end": segment * 3 + 3,
                        "text": f"segment {segment} of video {video_index}",
                    }
                    for segment in range(500)
                ],
            })
            entries.append({
                "id": video_id,
                "index": video_index + 1,
                "title": f"Video {video_index}",
                "path": f"videos/{video_id}",
                "video": "video.mp4",
                "available": True,
                "duration": 600,
            })
        write_json(playlist_dir / "playlist.json", {
            "schema": 1,
            "id": playlist_id,
            "title": f"Bench {playlist_index}",
            "channel": "Bench Channel",
            "entries": entries,
        })


def _counted(name: str):
    calls = {"n": 0}
    original = getattr(library_module, name)

    def wrapper(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    setattr(library_module, name, wrapper)
    return calls, lambda: setattr(library_module, name, original)


def measure(client: TestClient, path: str, repeats: int = 3) -> dict:
    # Warm caches and imports before timing.
    client.get(path)
    timings = []
    response = None
    for _ in range(repeats):
        start = time.perf_counter()
        response = client.get(path)
        timings.append((time.perf_counter() - start) * 1000)
    assert response is not None and response.status_code == 200
    return {
        "median_ms": round(statistics.median(timings), 2),
        "min_ms": round(min(timings), 2),
        "html_bytes": len(response.content),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="ytop-bench-") as raw:
        root = Path(raw)
        build_library(root)
        config = Config(library=root, detect_removable=False)
        from ytop.web.app import create_app

        app = create_app(root, config)
        client = TestClient(app)

        results: dict[str, object] = {}
        json_calls, restore_json = _counted("read_json")
        transcript_calls, restore_transcript = _counted("read_transcript")
        try:
            for label, path in (
                ("home", "/"),
                ("playlist_121", f"/playlist/{BIG_ID}"),
                ("watch", f"/watch/{BIG_ID}/v000"),
            ):
                json_calls["n"] = 0
                transcript_calls["n"] = 0
                results[label] = measure(client, path)
                results[label]["json_reads"] = json_calls["n"]
                results[label]["transcript_parses"] = transcript_calls["n"]
        finally:
            restore_json()
            restore_transcript()

        start = time.perf_counter()
        index_library(root)
        results["index_library_ms"] = round(
            (time.perf_counter() - start) * 1000, 2
        )
        results["library_bytes_label"] = human_bytes(
            sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
        )

        if args.json:
            print(json.dumps(results, indent=2, sort_keys=True))
        else:
            for label, row in results.items():
                if isinstance(row, dict):
                    print(
                        f"{label:14} {row['median_ms']:8.2f} ms  "
                        f"{row['html_bytes']:8d} B  "
                        f"json={row['json_reads']:4d}  "
                        f"transcripts={row['transcript_parses']:4d}"
                    )
                else:
                    print(f"{label:14} {row}")


if __name__ == "__main__":
    main()
