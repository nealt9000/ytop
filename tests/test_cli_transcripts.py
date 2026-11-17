from __future__ import annotations

import json
from pathlib import Path

from factories import add_playlist
from factories import make_library as make_layout
from ytop.cli import main
from ytop.layout import LibraryLayout
from ytop.transcripts.captions import CaptionData, Segment
from ytop.transcripts.store import index_video_dir, read_transcript, write_transcript
from ytop.yt.metadata import read_json

PLAYLIST_ID = "PLCLITEST000000000000000000000001"


def make_library(tmp_path: Path, *, transcript: bool = False) -> LibraryLayout:
    layout = make_layout(tmp_path)
    add_playlist(layout, PLAYLIST_ID, "List", [("vid1", "Video One")])
    if transcript:
        video_dir = layout.video_meta_dir(PLAYLIST_ID, "vid1")
        write_transcript(
            video_dir, video_id="vid1", title="Video One",
            language="en", kind="auto",
            segments=[Segment(0, 5, "A stored transcript about recursion.")],
            raw_file="subtitles/en.auto.json",
        )
        index_video_dir(layout, video_dir, playlist_id=PLAYLIST_ID,
                        title="Video One")
    return layout


def test_index_command_rebuilds_the_database(tmp_path: Path, capsys):
    layout = make_library(tmp_path, transcript=True)
    code = main(["index", "--library", str(layout.root), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["indexed"] == 1
    assert payload["database"] == str(layout.index_db)
    assert layout.index_db.is_file()


def test_transcripts_index_only_does_not_touch_the_network(tmp_path: Path,
                                                           monkeypatch):
    layout = make_library(tmp_path, transcript=True)

    def fail(*args, **kwargs):
        raise AssertionError("network must not be used")

    monkeypatch.setattr("ytop.transcripts.fetch.fetch_transcript_api", fail)
    monkeypatch.setattr("ytop.transcripts.fetch.extract_video_info", fail)
    code = main([
        "transcripts", "--library", str(layout.root), "--index-only", "--json",
    ])
    assert code == 0


def test_missing_captions_are_not_a_library_failure(tmp_path: Path,
                                                    monkeypatch):
    layout = make_library(tmp_path)
    monkeypatch.setattr(
        "ytop.transcripts.fetch.fetch_transcript_api",
        lambda video_id, languages: None,
    )
    monkeypatch.setattr(
        "ytop.transcripts.fetch.extract_video_info",
        lambda video_id, config, cache_root=None: {"id": video_id, "automatic_captions": {}},
    )
    code = main([
        "transcripts", PLAYLIST_ID, "--library", str(layout.root),
        "--delay", "0",
    ])
    assert code == 0
    summary = read_json(
        layout.playlist_meta_dir(PLAYLIST_ID) / "transcripts.json"
    )
    assert summary["videos"][0]["status"] == "missing"
    assert summary["totals"]["missing"] == 1


def test_transcripts_are_written_and_indexed(tmp_path: Path, monkeypatch):
    layout = make_library(tmp_path)
    monkeypatch.setattr(
        "ytop.transcripts.fetch.fetch_transcript_api",
        lambda video_id, languages: CaptionData(
            language="en", kind="auto", ext="json",
            segments=[Segment(0.0, 4.0, "Recursion and self reference.")],
            raw=b'[{"text": "Recursion and self reference."}]',
        ),
    )
    code = main([
        "transcripts", PLAYLIST_ID, "--library", str(layout.root),
        "--delay", "0", "--json",
    ])
    assert code == 0
    video_dir = layout.video_meta_dir(PLAYLIST_ID, "vid1")
    doc = read_transcript(video_dir)
    assert doc["segments"][0]["text"] == "Recursion and self reference."
    meta = read_json(video_dir / "meta.json")
    assert meta["transcript"]["segments"] == 1
    assert (video_dir / "subtitles" / "en.auto.json").is_file()
    srt = layout.media_root / "List" / "01 - Video One.en.srt"
    assert "Recursion and self reference." in srt.read_text(encoding="utf-8")

    from ytop.transcripts.store import search

    rows = search(layout, "recursion")
    assert len(rows) == 1
    assert rows[0]["video_id"] == "vid1"

    # A second run is a no-op and reports the transcript as current.
    code = main([
        "transcripts", PLAYLIST_ID, "--library", str(layout.root),
        "--delay", "0", "--json",
    ])
    assert code == 0


def test_transient_caption_api_failure_is_retried(tmp_path: Path,
                                                  monkeypatch):
    layout = make_library(tmp_path)
    calls = []

    def flaky(video_id, languages):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("429 Too Many Requests")
        return CaptionData(
            language="en", kind="auto", ext="json",
            segments=[Segment(0.0, 2.0, "Recovered after a retry.")],
            raw=b"[]",
        )

    monkeypatch.setattr("ytop.transcripts.fetch.fetch_transcript_api", flaky)
    monkeypatch.setattr("ytop.transcripts.fetch.time.sleep", lambda _s: None)
    code = main([
        "transcripts", PLAYLIST_ID, "--library", str(layout.root), "--delay", "0",
    ])
    assert code == 0
    assert len(calls) == 2
    video_dir = layout.video_meta_dir(PLAYLIST_ID, "vid1")
    assert read_transcript(video_dir)["segments"][0]["text"] == (
        "Recovered after a retry."
    )


def test_rename_during_the_fetch_keeps_the_new_media_ref(tmp_path: Path,
                                                         monkeypatch):
    """The fetch rewrites meta.json after a network call; a folder rename
    that happened meanwhile must survive it."""
    from ytop.publish import rename_folder

    layout = make_library(tmp_path)

    def fetch_while_renaming(video_id, languages):
        rename_folder(layout, PLAYLIST_ID, "Renamed")
        return CaptionData(
            language="en", kind="auto", ext="json",
            segments=[Segment(0.0, 4.0, "Recursion and self reference.")],
            raw=b'[{"text": "Recursion and self reference."}]',
        )

    monkeypatch.setattr(
        "ytop.transcripts.fetch.fetch_transcript_api", fetch_while_renaming
    )
    code = main([
        "transcripts", PLAYLIST_ID, "--library", str(layout.root),
        "--delay", "0", "--json",
    ])
    assert code == 0
    meta = read_json(layout.video_meta_dir(PLAYLIST_ID, "vid1") / "meta.json")
    assert meta["files"]["media"].startswith("Renamed/")
    assert layout.resolve_media(meta["files"]["media"]).is_file()
    assert meta["transcript"]["segments"] == 1


def _caption(video_id, languages):
    return CaptionData(
        language="en", kind="auto", ext="json",
        segments=[Segment(0.0, 4.0, f"Words for {video_id}.")],
        raw=b"[]",
    )


def test_playlist_transcripts_sync_the_files_once(tmp_path: Path, monkeypatch):
    import ytop.publish as publish

    layout = make_layout(tmp_path)
    add_playlist(layout, PLAYLIST_ID, "List",
                 [("vid1", "One"), ("vid2", "Two"), ("vid3", "Three")])
    monkeypatch.setattr("ytop.transcripts.fetch.fetch_transcript_api", _caption)
    calls: list[str] = []
    real = publish.sync_playlist_files

    def counting(lay, playlist_id):
        calls.append(playlist_id)
        return real(lay, playlist_id)

    monkeypatch.setattr(publish, "sync_playlist_files", counting)
    code = main([
        "transcripts", PLAYLIST_ID, "--library", str(layout.root),
        "--delay", "0", "--json",
    ])
    assert code == 0
    assert calls == [PLAYLIST_ID]
    for stem in ("01 - One", "02 - Two", "03 - Three"):
        assert (layout.media_root / "List" / f"{stem}.en.srt").is_file()


def test_playlist_summary_is_written_under_the_publish_lock(
    tmp_path: Path, monkeypatch
):
    import threading

    from ytop.publish import PUBLISH_LOCK
    from ytop.transcripts import fetch as fetch_mod

    layout = make_library(tmp_path)
    monkeypatch.setattr(fetch_mod, "fetch_transcript_api", _caption)
    held: list[bool] = []
    real = fetch_mod.write_json

    def checking(path, data, **kwargs):
        if Path(path).name == "playlist.json":
            result: list[bool] = []

            def probe():
                got = PUBLISH_LOCK.acquire(blocking=False)
                if got:
                    PUBLISH_LOCK.release()
                result.append(got)

            thread = threading.Thread(target=probe)
            thread.start()
            thread.join()
            held.append(not result[0])
        return real(path, data, **kwargs)

    monkeypatch.setattr(fetch_mod, "write_json", checking)
    code = main([
        "transcripts", PLAYLIST_ID, "--library", str(layout.root),
        "--delay", "0", "--json",
    ])
    assert code == 0
    assert held and all(held)
