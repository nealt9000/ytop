from __future__ import annotations

import json
from pathlib import Path

import pytest
from yt_dlp.utils import DownloadError

from factories import make_library
from ytop import fsinfo
from ytop.config import Config
from ytop.yt import download as download_mod
from ytop.yt.formats import Selection
from ytop.yt.probe import ProbedItem, ProbeResult


def item(index: int, video_id: str) -> ProbedItem:
    info = {
        "id": video_id,
        "title": f"Video {video_id}",
        "description": f"Description for {video_id}",
        "channel": "Test Channel",
        "duration": 100,
        "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
        "thumbnail": f"https://example.invalid/{video_id}.jpg",
        "chapters": [{"start_time": 0, "end_time": 100, "title": "Chapter"}],
        "formats": [
            {"format_id": "395", "vcodec": "av01.0.00M.08", "acodec": "none",
             "height": 240, "width": 320, "fps": 15, "ext": "webm"},
            {"format_id": "249-9", "vcodec": "none", "acodec": "opus",
             "abr": 50, "language": "en", "ext": "webm"},
        ],
    }
    selection = Selection(
        video={
            "format_id": "395",
            "vcodec": "av01.0.00M.08",
            "height": 240,
            "width": 320,
            "fps": 15,
        },
        audio={"format_id": "249-9", "acodec": "opus", "language": "en-US"},
    )
    return ProbedItem(
        index=index,
        id=video_id,
        url=info["webpage_url"],
        title=info["title"],
        duration=100,
        info=info,
        selection=selection,
    )


@pytest.fixture
def fake_network(monkeypatch):
    calls: list[str] = []
    state = {"items": [item(1, "vid1")], "transcripts": []}

    def fake_probe(value, config, limit=None, on_item=None, **kwargs):
        items = list(state["items"])
        if limit is not None:
            items = items[:limit]
        for entry in items:
            if on_item:
                on_item(entry)
        return ProbeResult(
            url="https://www.youtube.com/playlist?list=PL1",
            playlist={"id": "PL1", "title": "Test List", "channel": "Channel"},
            items=items,
        )

    class FakeYoutubeDL:
        """Writes the media where yt-dlp would: the partial dir's outtmpl."""

        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def download(self, urls):
            video_id = urls[0].rsplit("=", 1)[-1]
            template = self.opts["outtmpl"]
            if isinstance(template, dict):
                template = template["default"]
            hook = state.get("on_download")
            if hook is not None:
                hook(video_id, template)
            calls.append(video_id)
            path = Path(template.replace("%(ext)s", "webm"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"media")

    def fake_thumb(info, dest, timeout=40):
        Path(dest).write_bytes(b"jpeg")
        return True

    def fake_transcript(video_dir, **kwargs):
        state["transcripts"].append(Path(video_dir).name)
        return download_mod.TranscriptResult(
            id=Path(video_dir).name, title="", status="skipped"
        )

    monkeypatch.setattr(download_mod, "probe_playlist", fake_probe)
    monkeypatch.setattr(download_mod, "YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr(download_mod, "ffmpeg_path", lambda: Path("/usr/bin/ffmpeg"))
    monkeypatch.setattr(download_mod, "fetch_thumbnail", fake_thumb)
    monkeypatch.setattr(download_mod, "fetch_video_transcript", fake_transcript)
    return calls, state


def test_fetch_writes_library_and_is_resumable(tmp_path: Path, fake_network):
    calls, state = fake_network
    root = make_library(tmp_path).root
    cfg = Config()

    first = download_mod.fetch_playlist("PL1", cfg, library_root=root)
    assert [i.status for i in first.items] == ["downloaded"]
    assert calls == ["vid1"]

    pdir = first.playlist_dir
    playlist_doc = json.loads((pdir / "playlist.json").read_text())
    assert playlist_doc["id"] == "PL1"
    assert playlist_doc["entries"][0]["id"] == "vid1"
    assert (pdir / "playlist-thumb.jpg").exists()

    vdir = pdir / "videos" / "vid1"
    meta = json.loads((vdir / "meta.json").read_text())
    assert meta["description"] == "Description for vid1"
    assert meta["chapters"][0]["title"] == "Chapter"
    assert meta["files"]["media"] == "Test List/01 - Video vid1.webm"
    first_seen = meta["first_seen_at"]
    meta["format"]["video_id"] = "on-disk-format"
    (vdir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    calls.clear()
    second = download_mod.fetch_playlist("PL1", cfg, library_root=root)
    assert [i.status for i in second.items] == ["skipped"]
    assert calls == []
    meta_again = json.loads((vdir / "meta.json").read_text())
    assert meta_again["first_seen_at"] == first_seen
    assert meta_again["format"]["video_id"] == "on-disk-format"


def test_sync_adds_new_items_without_refetching_old_ones(tmp_path: Path, fake_network):
    calls, state = fake_network
    root = make_library(tmp_path).root
    cfg = Config()
    download_mod.fetch_playlist("PL1", cfg, library_root=root)

    calls.clear()
    state["items"] = [item(1, "vid1"), item(2, "vid2")]
    result = download_mod.fetch_playlist("PL1", cfg, library_root=root)

    assert [i.status for i in result.items] == ["skipped", "downloaded"]
    assert calls == ["vid2"]
    doc = json.loads((result.playlist_dir / "playlist.json").read_text())
    assert [e["id"] for e in doc["entries"]] == ["vid1", "vid2"]
    assert doc["video_count"] == 2
    assert doc["kind"] == "youtube"


def test_limited_run_does_not_truncate_existing_playlist(tmp_path: Path, fake_network):
    calls, state = fake_network
    state["items"] = [item(1, "vid1"), item(2, "vid2")]
    root = make_library(tmp_path).root
    cfg = Config()
    download_mod.fetch_playlist("PL1", cfg, library_root=root)

    calls.clear()
    result = download_mod.fetch_playlist("PL1", cfg, library_root=root, limit=1)
    doc = json.loads((result.playlist_dir / "playlist.json").read_text())
    assert [e["id"] for e in doc["entries"]] == ["vid1", "vid2"]
    assert doc["partial"] is True
    assert doc["limit"] == 1


def test_dry_run_probes_without_creating_the_library(tmp_path: Path, fake_network):
    root = make_library(tmp_path).root
    cfg = Config()
    result = download_mod.fetch_playlist("PL1", cfg, library_root=root, dry_run=True)
    assert result.playlist_dir.exists() is False
    assert [i.status for i in result.items] == ["skipped"]


def test_failed_item_keeps_the_rest(tmp_path: Path, fake_network):
    calls, state = fake_network
    broken = item(2, "broken")
    broken.info = None
    broken.selection = None
    broken.error = "private video"
    state["items"] = [item(1, "vid1"), broken]

    root = make_library(tmp_path).root
    cfg = Config()
    result = download_mod.fetch_playlist("PL1", cfg, library_root=root)

    assert [i.status for i in result.items] == ["downloaded", "failed"]
    doc = json.loads((result.playlist_dir / "playlist.json").read_text())
    assert [e["available"] for e in doc["entries"]] == [True, False]
    assert (result.playlist_dir / "videos" / "vid1" / "meta.json").exists()


def test_playlist_is_visible_and_updated_while_downloading(
    tmp_path: Path, fake_network, monkeypatch,
):
    calls, state = fake_network
    state["items"] = [item(1, "vid1"), item(2, "vid2")]
    seen: dict[str, list[tuple[str, bool]]] = {}

    layout = make_library(tmp_path)

    def inspecting_download(video_id, template):
        playlist_dir = layout.playlist_meta_dir("PL1")
        doc = json.loads((playlist_dir / "playlist.json").read_text())
        seen[video_id] = [
            (row["id"], row["available"]) for row in doc["entries"]
        ]

    state["on_download"] = inspecting_download
    root = layout.root
    cfg = Config()
    result = download_mod.fetch_playlist("PL1", cfg, library_root=root)

    # Before the first item, the whole playlist already exists and every
    # entry is marked unavailable.
    assert seen["vid1"] == [("vid1", False), ("vid2", False)]
    # While the second item is downloading, the first is already playable.
    assert seen["vid2"] == [("vid1", True), ("vid2", False)]
    doc = json.loads((result.playlist_dir / "playlist.json").read_text())
    assert [row["available"] for row in doc["entries"]] == [True, True]
    assert doc["video_count"] == 2


def test_alternate_audio_is_stored_and_described_in_metadata(
    tmp_path: Path, fake_network, monkeypatch,
):
    calls, state = fake_network
    entry = item(1, "vid1")
    alternate = {
        "format_id": "249-0", "vcodec": "none", "acodec": "opus",
        "abr": 50, "language": "en-US",
        "format_note": "English (US), low", "ext": "webm",
        "filesize": 1000,
    }
    entry.selection.audio["format_note"] = "Spanish (US) original (default), low"
    entry.selection.audio["language"] = "es-US"
    entry.selection.alternates = [alternate]
    state["items"] = [entry]

    def fake_alternate(item, video_dir, config, force, lock, **kwargs):
        path = Path(video_dir) / "audio" / "249-0.webm"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"alternate-audio")
        return [(alternate, path)]

    monkeypatch.setattr(
        download_mod, "_download_alternate_audio", fake_alternate
    )
    result = download_mod.fetch_playlist("PL1", Config(), library_root=make_library(tmp_path).root)
    meta = json.loads(
        (result.playlist_dir / "videos" / "vid1" / "meta.json").read_text()
    )
    assert [track["id"] for track in meta["audio_tracks"]] == ["249-9", "249-0"]
    assert meta["audio_tracks"][0]["embedded"] is True
    assert meta["audio_tracks"][0]["original"] is True
    assert meta["audio_tracks"][1]["embedded"] is False
    assert meta["audio_tracks"][1]["file"] == "audio/249-0.webm"
    assert meta["audio_tracks"][1]["size"] == len(b"alternate-audio")


def test_download_fetches_transcripts_and_writes_the_summary(
    tmp_path: Path, fake_network,
):
    calls, state = fake_network
    result = download_mod.fetch_playlist("PL1", Config(), library_root=make_library(tmp_path).root)

    assert state["transcripts"] == ["vid1"]
    assert result.playlist["title"] == "Test List"
    summary = json.loads((result.playlist_dir / "transcripts.json").read_text())
    assert summary["totals"]["videos"] == 1
    assert summary["totals"]["available"] == 1
    playlist_doc = json.loads((result.playlist_dir / "playlist.json").read_text())
    assert playlist_doc["transcripts"]["videos"] == 1


def test_transcript_fetching_can_be_disabled(tmp_path: Path, fake_network):
    calls, state = fake_network
    result = download_mod.fetch_playlist(
        "PL1", Config(), library_root=make_library(tmp_path).root, fetch_transcripts=False
    )
    assert state["transcripts"] == []
    assert not (result.playlist_dir / "transcripts.json").exists()


def test_sync_preserves_a_manual_order_and_appends_new_videos(
    tmp_path: Path, fake_network,
):
    calls, state = fake_network
    state["items"] = [item(1, "vid1"), item(2, "vid2")]
    root = make_library(tmp_path).root
    cfg = Config()
    first = download_mod.fetch_playlist("PL1", cfg, library_root=root)

    # The user moves vid2 above vid1.
    doc = json.loads((first.playlist_dir / "playlist.json").read_text())
    doc["entries"] = [doc["entries"][1], doc["entries"][0]]
    doc["order"] = "manual"
    (first.playlist_dir / "playlist.json").write_text(
        json.dumps(doc), encoding="utf-8"
    )

    calls.clear()
    state["items"] = [item(1, "vid1"), item(2, "vid2"), item(3, "vid3")]
    result = download_mod.fetch_playlist("PL1", cfg, library_root=root)
    assert calls == ["vid3"]
    doc = json.loads((result.playlist_dir / "playlist.json").read_text())
    assert [e["id"] for e in doc["entries"]] == ["vid2", "vid1", "vid3"]
    assert [e["index"] for e in doc["entries"]] == [1, 2, 3]


def test_one_video_is_appended_to_an_existing_playlist(
    tmp_path: Path, fake_network,
):
    calls, state = fake_network
    root = make_library(tmp_path).root
    cfg = Config()
    download_mod.fetch_playlist("PL1", cfg, library_root=root)

    calls.clear()
    probe = ProbeResult(
        url="https://www.youtube.com/watch?v=vid2",
        playlist={"id": "PL1", "title": "Test List", "channel": "Channel"},
        items=[item(1, "vid2")],
    )
    result = download_mod.download_probe(probe, cfg, jobs=1, library_root=root)

    assert calls == ["vid2"]
    doc = json.loads((result.playlist_dir / "playlist.json").read_text())
    assert [e["id"] for e in doc["entries"]] == ["vid1", "vid2"]
    assert doc["video_count"] == 2
    assert doc["kind"] == "youtube"
    # The existing media was not touched.
    assert result.items[0].status == "downloaded"


def test_preserve_audio_is_stored_on_the_playlist(tmp_path: Path, fake_network):
    root = make_library(tmp_path).root
    cfg = Config()
    result = download_mod.fetch_playlist("PL1", cfg, library_root=root, preserve_audio=True
    )
    doc = json.loads((result.playlist_dir / "playlist.json").read_text())
    assert doc["options"]["preserve_audio"] is True

    # A later sync without the flag keeps the playlist's remembered policy.
    result = download_mod.fetch_playlist("PL1", cfg, library_root=root)
    doc = json.loads((result.playlist_dir / "playlist.json").read_text())
    assert doc["options"]["preserve_audio"] is True


def test_quality_is_stored_on_the_playlist(tmp_path: Path, fake_network):
    root = make_library(tmp_path).root
    cfg = Config()
    result = download_mod.fetch_playlist("PL1", cfg, library_root=root)
    probe = download_mod.probe_playlist("PL1", cfg)
    download_mod.download_probe(
        probe, cfg, quiet=True, max_height=720, target_mb_per_hour=0,
        library_root=root, quality="720p",
    )
    doc = json.loads((result.playlist_dir / "playlist.json").read_text())
    assert doc["options"]["quality"] == "720p"
    assert doc["options"]["preserve_audio"] is False

    # A download that does not name a quality keeps the remembered one.
    download_mod.download_probe(probe, cfg, quiet=True, library_root=root)
    doc = json.loads((result.playlist_dir / "playlist.json").read_text())
    assert doc["options"]["quality"] == "720p"


def test_kind_survives_a_download_into_a_local_playlist(
    tmp_path: Path, fake_network,
):
    calls, state = fake_network
    layout = make_library(tmp_path)
    root = layout.root
    cfg = Config()
    pdir = layout.playlist_meta_dir("local-abc")
    pdir.mkdir(parents=True)
    (pdir / "playlist.json").write_text(json.dumps({
        "schema": 1, "id": "local-abc", "title": "Mix",
        "kind": "local", "entries": [], "options": {},
    }), encoding="utf-8")

    probe = ProbeResult(
        url="https://www.youtube.com/watch?v=vid1",
        playlist={"id": "local-abc", "title": "Mix", "kind": "local"},
        items=[item(1, "vid1")],
    )
    result = download_mod.download_probe(probe, cfg, jobs=1, library_root=root)
    doc = json.loads((result.playlist_dir / "playlist.json").read_text())
    assert doc["kind"] == "local"
    assert [e["id"] for e in doc["entries"]] == ["vid1"]


def test_download_publishes_into_media_folder(tmp_path: Path, fake_network):
    calls, state = fake_network
    layout = make_library(tmp_path)
    seen_templates: list[str] = []
    state["on_download"] = lambda video_id, template: seen_templates.append(
        template
    )

    result = download_mod.fetch_playlist("PL1", Config(), library_root=layout.root)

    assert [i.status for i in result.items] == ["downloaded"]
    assert seen_templates == [str(layout.partial_dir("vid1") / "video.%(ext)s")]
    final = layout.media_root / "Test List" / "01 - Video vid1.webm"
    assert final.read_bytes() == b"media"
    assert not layout.partial_dir("vid1").exists()
    meta = json.loads(
        (layout.video_meta_dir("PL1", "vid1") / "meta.json").read_text()
    )
    assert meta["files"]["media"] == "Test List/01 - Video vid1.webm"
    doc = json.loads(
        (layout.playlist_meta_dir("PL1") / "playlist.json").read_text()
    )
    assert doc["folder"] == "Test List"
    assert "path" not in doc["entries"][0]
    assert "video" not in doc["entries"][0]
    assert doc["entries"][0]["available"] is True


def test_fat32_guard_blocks_large(tmp_path: Path, fake_network, monkeypatch):
    calls, state = fake_network
    layout = make_library(tmp_path)
    big = item(1, "vid1")
    big.selection.video["filesize"] = 3_900_000_000
    state["items"] = [big]
    monkeypatch.setattr(
        fsinfo, "fs_info",
        lambda path: fsinfo.FsInfo("vfat", fsinfo.FAT32_MAX_FILE),
    )

    result = download_mod.fetch_playlist("PL1", Config(), library_root=layout.root)

    assert calls == []
    [failed] = result.items
    assert failed.status == "failed"
    assert failed.error_kind == "fat32_limit"
    assert "larger than FAT32's 4 GB file limit on Test" in failed.error
    assert "Choose a smaller quality or an exFAT/other drive." in failed.error
    assert not (layout.media_root / "Test List" / "01 - Video vid1.webm").exists()


def test_fat32_guard_allows_small_on_vfat(
    tmp_path: Path, fake_network, monkeypatch,
):
    calls, state = fake_network
    layout = make_library(tmp_path)
    monkeypatch.setattr(
        fsinfo, "fs_info",
        lambda path: fsinfo.FsInfo("vfat", fsinfo.FAT32_MAX_FILE),
    )
    result = download_mod.fetch_playlist("PL1", Config(), library_root=layout.root)
    assert calls == ["vid1"]
    assert result.items[0].error_kind is None


def test_force_redownload_replaces_the_published_file(
    tmp_path: Path, fake_network,
):
    calls, state = fake_network
    layout = make_library(tmp_path)
    root = layout.root
    cfg = Config()
    download_mod.fetch_playlist("PL1", cfg, library_root=root)
    calls.clear()
    result = download_mod.fetch_playlist("PL1", cfg, library_root=root, force=True)
    assert calls == ["vid1"]
    assert [i.status for i in result.items] == ["downloaded"]
    folder = layout.media_root / "Test List"
    assert sorted(p.name for p in folder.iterdir() if p.suffix == ".webm") == [
        "01 - Video vid1.webm"
    ]


def test_transcript_fetch_writes_srt(tmp_path: Path, monkeypatch):
    from factories import add_playlist
    from ytop.transcripts import fetch as fetch_mod
    from ytop.transcripts.captions import CaptionData, Segment

    layout = make_library(tmp_path)
    add_playlist(layout, "PL1", "My List", [("vid1", "First")])
    monkeypatch.setattr(
        fetch_mod, "fetch_transcript_api",
        lambda video_id, languages: CaptionData(
            language="en", kind="auto", ext="json",
            segments=[Segment(0.0, 2.5, "Hello there.")],
            raw=b"[]",
        ),
    )
    result = fetch_mod.fetch_video_transcript(
        layout.video_meta_dir("PL1", "vid1"),
        playlist_id="PL1",
        config=Config(),
        layout=layout,
    )
    assert result.status == "fetched"
    srt = layout.media_root / "My List" / "01 - First.en.srt"
    assert srt.read_text(encoding="utf-8") == (
        "1\n00:00:00,000 --> 00:00:02,500\nHello there.\n\n"
    )


def test_parallel_workers_publish_every_file(tmp_path: Path, fake_network):
    calls, state = fake_network
    state["items"] = [item(i, f"vid{i}") for i in range(1, 5)]
    layout = make_library(tmp_path)
    result = download_mod.fetch_playlist("PL1", Config(), library_root=layout.root, jobs=4)
    assert sorted(i.status for i in result.items) == ["downloaded"] * 4
    folder = layout.media_root / "Test List"
    assert sorted(p.name for p in folder.glob("*.webm")) == [
        f"0{i} - Video vid{i}.webm" for i in range(1, 5)
    ]
    for i in range(1, 5):
        meta = json.loads(
            (layout.video_meta_dir("PL1", f"vid{i}") / "meta.json").read_text()
        )
        assert meta["files"]["media"] == f"Test List/0{i} - Video vid{i}.webm"


def test_download_publish_refreshes_index(tmp_path: Path, fake_network):
    from ytop.index import index_for

    layout = make_library(tmp_path)
    download_mod.fetch_playlist("PL1", Config(), library_root=layout.root)
    rows = index_for(layout).playlists()
    videos = [v for p in rows for v in p.videos]
    assert [(v.video_id, v.available) for v in videos] == [("vid1", True)]


def test_folder_rename_during_download_is_kept(tmp_path: Path, fake_network):
    from ytop.publish import rename_folder

    calls, state = fake_network
    state["items"] = [item(1, "vid1"), item(2, "vid2")]
    layout = make_library(tmp_path)

    def rename_once(video_id, template):
        if video_id == "vid2":
            rename_folder(layout, "PL1", "Renamed")

    state["on_download"] = rename_once
    download_mod.fetch_playlist("PL1", Config(), library_root=layout.root)

    doc = json.loads(
        (layout.playlist_meta_dir("PL1") / "playlist.json").read_text()
    )
    assert doc["folder"] == "Renamed"
    assert (layout.media_root / "Renamed" / "02 - Video vid2.webm").exists()
    assert not (layout.media_root / "Test List").exists()


def test_force_redownload_clears_stale_alternate_audio(
    tmp_path: Path, fake_network,
):
    calls, state = fake_network
    layout = make_library(tmp_path)
    download_mod.fetch_playlist("PL1", Config(), library_root=layout.root)
    stale = layout.video_meta_dir("PL1", "vid1") / "audio" / "140.m4a"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"old track")

    download_mod.fetch_playlist("PL1", Config(), library_root=layout.root, force=True)

    assert not stale.exists()


def test_skipped_item_drops_a_stale_partial_dir(tmp_path: Path, fake_network):
    calls, state = fake_network
    layout = make_library(tmp_path)
    download_mod.fetch_playlist("PL1", Config(), library_root=layout.root)
    partial = layout.partial_dir("vid1")
    partial.mkdir(parents=True)
    (partial / "video.webm.part").write_bytes(b"stale")

    result = download_mod.fetch_playlist("PL1", Config(), library_root=layout.root)

    assert [i.status for i in result.items] == ["skipped"]
    assert not partial.exists()


def test_raw_json_is_trimmed_and_resync_trims_an_old_one(
    tmp_path: Path, fake_network
):
    calls, state = fake_network
    big = item(1, "vid1")
    big.info["formats"][0]["url"] = "https://googlevideo.invalid/videoplayback?sig=x"
    big.info["formats"][0]["fragments"] = [{"url": "frag"}] * 3
    big.info["thumbnails"] = [{"url": "https://example.invalid/t.jpg"}]
    big.info["tags"] = ["kept"]
    state["items"] = [big]
    root = make_library(tmp_path).root
    cfg = Config()
    result = download_mod.fetch_playlist("PL1", cfg, library_root=root)
    raw_path = result.playlist_dir / "videos" / "vid1" / "raw.json"

    def check() -> None:
        raw = json.loads(raw_path.read_text())
        assert raw["tags"] == ["kept"]
        assert "thumbnails" not in raw
        assert raw["formats"][0]["format_id"] == "395"
        assert "url" not in raw["formats"][0]
        assert "fragments" not in raw["formats"][0]

    check()
    # The download itself still got the untrimmed info.
    assert "thumbnails" in big.info and "url" in big.info["formats"][0]

    # An old, untrimmed raw.json is rewritten trimmed by the next sync, even
    # though the video itself is skipped.
    raw_path.write_text(json.dumps(big.info), encoding="utf-8")
    calls.clear()
    again = download_mod.fetch_playlist("PL1", cfg, library_root=root)
    assert [i.status for i in again.items] == ["skipped"]
    check()



def test_probe_reports_time_per_step(monkeypatch):
    from ytop.yt import probe as probe_mod

    entries = [{"id": "vid1", "title": "One"}, {"id": "vid2", "title": "Two"},
               {"id": "bad", "title": "Bad"}]

    def fake_listing(value, config, limit=None, cache_root=None):
        return value, {"id": "PL1", "title": "List", "entries": entries}

    def fake_video(video_id, config, cache_root=None, **kwargs):
        if video_id == "bad":
            raise probe_mod.ProbeError("gone")
        return {
            "id": video_id, "title": video_id, "duration": 60,
            "formats": [
                {"format_id": "18", "vcodec": "avc1", "acodec": "none",
                 "height": 360, "width": 640, "tbr": 500, "ext": "mp4"},
                {"format_id": "140", "vcodec": "none", "acodec": "mp4a.40.2",
                 "abr": 128, "ext": "m4a"},
            ],
        }

    monkeypatch.setattr(probe_mod, "extract_playlist_info", fake_listing)
    monkeypatch.setattr(probe_mod, "extract_video_info", fake_video)
    probe = probe_mod.probe_playlist("PL1", Config(), probe_jobs=2)

    names = [phase.name for phase in probe.timings]
    assert names[:2] == ["listing", "videos"]
    listing, videos = probe.timings[:2]
    assert listing.count == 3
    assert videos.count == 3
    assert videos.unit == "videos"
    assert "mean" in videos.note and "2 at a time" in videos.note
    assert "1 failed" in videos.note
    assert all(item.seconds >= 0 for item in probe.items)


# -- re-sync: videos already downloaded are not re-read (#31) ----------------

@pytest.fixture
def real_probe(monkeypatch, fake_network):
    """fake_network, but through the real probe_playlist: only the listing
    and per-video extraction are faked, and every extraction is recorded."""
    from ytop.yt import probe as probe_mod

    calls, state = fake_network
    extracted: list[str] = []

    def listing(value, config, limit=None, cache_root=None):
        entries = [{"id": i.id, "title": state.get("titles", {}).get(i.id, i.title),
                    "duration": i.duration} for i in state["items"]]
        return value, {"id": "PL1", "title": "Test List", "channel": "Channel",
                       "entries": entries}

    def video(video_id, config, cache_root=None, **kwargs):
        extracted.append(video_id)
        info = next(i.info for i in state["items"] if i.id == video_id)
        return dict(info)

    monkeypatch.setattr(download_mod, "probe_playlist", probe_mod.probe_playlist)
    monkeypatch.setattr(probe_mod, "extract_playlist_info", listing)
    monkeypatch.setattr(probe_mod, "extract_video_info", video)
    monkeypatch.setattr(download_mod, "extract_video_info", video)
    return calls, state, extracted


def test_resync_reads_only_new_videos(tmp_path: Path, real_probe):
    calls, state, extracted = real_probe
    root = make_library(tmp_path).root
    cfg = Config()
    download_mod.fetch_playlist("PL1", cfg, library_root=root)
    assert extracted == ["vid1"]

    extracted.clear()
    calls.clear()
    state["items"] = [item(1, "vid1"), item(2, "vid2")]
    result = download_mod.fetch_playlist("PL1", cfg, library_root=root)

    assert extracted == ["vid2"]
    assert calls == ["vid2"]
    assert [i.status for i in result.items] == ["skipped", "downloaded"]
    doc = json.loads((result.playlist_dir / "playlist.json").read_text())
    assert [e["id"] for e in doc["entries"]] == ["vid1", "vid2"]
    assert all(e["available"] and e["metadata_ok"] for e in doc["entries"])
    meta = json.loads((result.playlist_dir / "videos/vid1/meta.json").read_text())
    assert meta["description"] == "Description for vid1"  # stored details kept


def test_resync_takes_a_new_title_from_the_listing(tmp_path: Path, real_probe):
    calls, state, extracted = real_probe
    root = make_library(tmp_path).root
    cfg = Config()
    download_mod.fetch_playlist("PL1", cfg, library_root=root)

    state["titles"] = {"vid1": "Renamed upstream"}
    result = download_mod.fetch_playlist("PL1", cfg, library_root=root)
    meta = json.loads((result.playlist_dir / "videos/vid1/meta.json").read_text())
    assert meta["title"] == "Renamed upstream"
    doc = json.loads((result.playlist_dir / "playlist.json").read_text())
    assert doc["entries"][0]["title"] == "Renamed upstream"

    # A video that went private keeps its real title.
    state["titles"] = {"vid1": "[Private video]"}
    result = download_mod.fetch_playlist("PL1", cfg, library_root=root)
    meta = json.loads((result.playlist_dir / "videos/vid1/meta.json").read_text())
    assert meta["title"] == "Renamed upstream"
    assert result.items[0].status == "skipped"


def test_refresh_rereads_existing_videos(tmp_path: Path, real_probe):
    calls, state, extracted = real_probe
    root = make_library(tmp_path).root
    cfg = Config()
    download_mod.fetch_playlist("PL1", cfg, library_root=root)

    extracted.clear()
    calls.clear()
    state["items"][0].info["description"] = "Updated upstream"
    result = download_mod.fetch_playlist(
        "PL1", cfg, library_root=root, refresh_existing=True,
    )
    assert extracted == ["vid1"]
    assert calls == []  # media kept
    assert [i.status for i in result.items] == ["skipped"]
    meta = json.loads((result.playlist_dir / "videos/vid1/meta.json").read_text())
    assert meta["description"] == "Updated upstream"


def test_existing_video_whose_media_vanished_is_read_and_downloaded(
    tmp_path: Path, real_probe,
):
    from ytop.layout import LibraryLayout
    from ytop.yt import probe as probe_mod

    calls, state, extracted = real_probe
    root = make_library(tmp_path).root
    cfg = Config()
    first = download_mod.fetch_playlist("PL1", cfg, library_root=root)

    layout = LibraryLayout(root)
    probe = probe_mod.probe_playlist(
        "PL1", cfg, have=download_mod.media_lookup(layout),
    )
    assert [i.existing for i in probe.items] == [True]
    assert probe.failures == []
    # Deleted between the review and the download.
    media = download_mod.find_media(layout, first.playlist_dir / "videos/vid1")
    assert media is not None
    media.unlink()

    extracted.clear()
    calls.clear()
    result = download_mod.download_probe(probe, cfg, quiet=True, library_root=root)
    assert extracted == ["vid1"]
    assert calls == ["vid1"]
    assert [i.status for i in result.items] == ["downloaded"]


def test_dry_run_counts_existing_as_skipped(tmp_path: Path, real_probe):
    root = make_library(tmp_path).root
    cfg = Config()
    download_mod.fetch_playlist("PL1", cfg, library_root=root)
    result = download_mod.fetch_playlist("PL1", cfg, library_root=root, dry_run=True)
    assert [i.status for i in result.items] == ["skipped"]


def test_probe_skips_videos_the_library_has(monkeypatch):
    from ytop.yt import probe as probe_mod

    entries = [{"id": "old", "title": "Old", "duration": 5},
               {"id": "new", "title": "New", "duration": 6}]
    extracted: list[str] = []
    started: list[str] = []

    monkeypatch.setattr(
        probe_mod, "extract_playlist_info",
        lambda value, config, limit=None, cache_root=None: (
            value, {"id": "PL1", "title": "List", "entries": entries}
        ),
    )

    def video(video_id, config, cache_root=None, **kwargs):
        extracted.append(video_id)
        return item(1, video_id).info

    monkeypatch.setattr(probe_mod, "extract_video_info", video)
    probe = probe_mod.probe_playlist(
        "PL1", Config(), probe_jobs=1,
        have=lambda playlist_id, video_id: (playlist_id, video_id) == ("PL1", "old"),
        on_item_start=lambda i: started.append(i.id),
    )
    assert extracted == ["new"]
    assert started == ["old", "new"]
    old, new = probe.items
    assert old.existing and old.info is None and old.title == "Old"
    assert old.duration == 5
    assert not new.existing and new.selection is not None
    assert probe.failures == []
    assert [i.id for i in probe.existing] == ["old"]
    assert probe.estimated_bytes == new.selection.estimated_bytes
    assert "1 already in the library" in probe.timings[1].note


def test_analysis_reads_formats_only_and_falls_back(monkeypatch):
    from ytop.yt import probe as probe_mod

    calls: list[tuple[str, bool]] = []
    good = item(1, "x").info

    def video(video_id, config, cache_root=None, *, quick=False):
        calls.append((video_id, quick))
        if quick and video_id == "muxed-only":
            return {"id": video_id, "formats": []}  # only format 18 had video
        return dict(good, id=video_id)

    monkeypatch.setattr(probe_mod, "extract_video_info", video)
    info, selection = probe_mod.analyse_video("plain", Config())
    assert calls == [("plain", True)]
    assert selection.video["format_id"] == "395"

    calls.clear()
    info, selection = probe_mod.analyse_video("muxed-only", Config())
    assert calls == [("muxed-only", True), ("muxed-only", False)]
    assert info["id"] == "muxed-only"

    calls.clear()
    probe_mod.analyse_video("plain", Config(quick_analysis=False))
    assert calls == [("plain", False)]


def test_quick_extraction_skips_the_player_js(monkeypatch):
    from ytop.yt import probe as probe_mod

    seen: list[dict] = []

    def extract(url, opts):
        seen.append(opts)
        return {"id": "v", "formats": []}

    monkeypatch.setattr(probe_mod, "_extract", extract)
    monkeypatch.setattr(
        probe_mod, "base_options",
        lambda config, cache_root=None: {
            "extractor_args": {"youtubepot-bgutilscript": {"server_home": ["/s"]}},
        },
    )
    probe_mod.extract_video_info("v", Config(), quick=True)
    probe_mod.extract_video_info("v", Config())
    quick, full = seen
    assert quick["extractor_args"]["youtube"] == {"player_skip": ["js"]}
    assert quick["extractor_args"]["youtubepot-bgutilscript"] == {"server_home": ["/s"]}
    assert "youtube" not in full["extractor_args"]


def test_probe_stops_reading_after_a_bot_check(monkeypatch):
    from ytop.yt import probe as probe_mod

    entries = [{"id": f"v{i}", "title": f"V{i}"} for i in range(6)]
    extracted: list[str] = []
    monkeypatch.setattr(
        probe_mod, "extract_playlist_info",
        lambda value, config, limit=None, cache_root=None: (
            value, {"id": "PL1", "title": "List", "entries": entries}
        ),
    )

    def video(video_id, config, cache_root=None, **kwargs):
        extracted.append(video_id)
        if video_id == "v2":
            raise probe_mod.ProbeError(
                "ERROR: [youtube] v2: Sign in to confirm you’re not a bot."
            )
        if video_id == "v1":
            raise probe_mod.ProbeError("Sign in to confirm your age.")
        return item(1, video_id).info

    monkeypatch.setattr(probe_mod, "extract_video_info", video)
    probe = probe_mod.probe_playlist("PL1", Config(), probe_jobs=1)
    assert extracted == ["v0", "v1", "v2"]  # an age gate does not stop it
    assert probe.bot_blocked
    assert [i.error == probe_mod.BOT_CHECK_STOPPED for i in probe.items] == [
        False, False, False, True, True, True,
    ]


def test_bot_check_is_not_the_age_gate():
    from ytop.yt.probe import is_bot_check

    assert is_bot_check("Sign in to confirm you're not a bot")
    assert not is_bot_check("Sign in to confirm your age")
    assert not is_bot_check(None)


# --- region-blocked videos read through proxies ---------------------------

def _proxy_cfg(*servers):
    from dataclasses import replace

    from ytop.config import ProxyServer
    return replace(Config(), proxies=tuple(ProxyServer(*s) for s in servers))


def _geo(countries=("US",)):
    from ytop.yt.probe import ProbeError
    return ProbeError(
        "The uploader has not made this video available in your country",
        list(countries),
    )


def test_region_blocked_video_is_read_through_a_matching_proxy(monkeypatch):
    from ytop.yt import probe as probe_mod
    from ytop.yt.proxies import active_proxy

    seen = []

    def video(vid, config, cache_root=None, *, quick=False):
        seen.append(active_proxy())
        if active_proxy() is None:
            raise _geo()
        return item(1, vid).info

    monkeypatch.setattr(probe_mod, "extract_video_info", video)
    cfg = _proxy_cfg(("https://de:89", "DE"), ("https://us:89", "US"))
    got = probe_mod.probe_video("abc", cfg).items[0]
    assert got.error is None and got.proxy == "https://us:89"
    assert "https://de:89" not in seen


def test_bot_check_through_a_proxy_tries_the_next_and_does_not_stop_the_playlist(monkeypatch):
    from ytop.yt import probe as probe_mod
    from ytop.yt.proxies import active_proxy

    entries = [{"id": f"v{i}", "title": f"V{i}"} for i in range(3)]
    monkeypatch.setattr(
        probe_mod, "extract_playlist_info",
        lambda value, config, limit=None, cache_root=None: (
            value, {"id": "PL1", "title": "List", "entries": entries}
        ),
    )

    def video(vid, config, cache_root=None, *, quick=False):
        proxy = active_proxy()
        if proxy is None:
            raise _geo()
        if proxy == "https://a:1":
            raise probe_mod.ProbeError("Sign in to confirm you're not a bot")
        return item(1, vid).info

    monkeypatch.setattr(probe_mod, "extract_video_info", video)
    cfg = _proxy_cfg(("https://a:1", "US"), ("https://b:1", "US"))
    result = probe_mod.probe_playlist("PL1", cfg, probe_jobs=2)
    assert not result.bot_blocked
    assert [i.proxy for i in result.items] == ["https://b:1"] * 3
    assert all(i.error is None for i in result.items)


def test_all_proxies_fail(monkeypatch):
    from ytop.yt import probe as probe_mod
    from ytop.yt.proxies import active_proxy

    def video(vid, config, cache_root=None, *, quick=False):
        if active_proxy() is None:
            raise _geo()
        raise probe_mod.ProbeError(
            "407 Proxy Authentication Required https://u:secret@us:89"
        )

    monkeypatch.setattr(probe_mod, "extract_video_info", video)
    got = probe_mod.probe_video("abc", _proxy_cfg(("https://us:89", "US"))).items[0]
    assert got.error == (
        "Region-blocked: available in US (tried 1 proxy; last: proxy login refused)"
    )
    assert got.error_kind == "region_blocked"
    assert "secret" not in got.error
    assert got.countries == ["US"]
    assert got.proxy is None


def test_failed_proxy_is_logged_redacted(monkeypatch, caplog):
    from ytop.yt import probe as probe_mod
    from ytop.yt.proxies import active_proxy

    def video(vid, config, cache_root=None, *, quick=False):
        if active_proxy() is None:
            raise _geo()
        raise probe_mod.ProbeError("tunnel failed via https://u:secret@us:89")

    monkeypatch.setattr(probe_mod, "extract_video_info", video)
    with caplog.at_level("WARNING", logger="ytop.yt.probe"):
        got = probe_mod.probe_video("abc", _proxy_cfg(("https://us:89", "US"))).items[0]
    assert "secret" not in caplog.text and "https://***@us:89" in caplog.text
    assert got.error.endswith("(tried 1 proxy; last: tunnel failed via https://***@us:89)")


def test_proxy_failure_reasons():
    from ytop.yt.probe import _proxy_failure_reason as why

    assert why("Connection refused") == "proxy unreachable"
    assert why("read timed out") == "proxy unreachable"
    assert why("Sign in to confirm you're not a bot") == (
        "proxy was asked to confirm it is not a bot"
    )
    assert why("x" * 200) == "x" * 80


def test_proxied_attempts_use_one_retry(monkeypatch):
    from ytop.yt import probe as probe_mod
    from ytop.yt.proxies import active_proxy

    seen = []

    def video(vid, config, cache_root=None, *, quick=False):
        seen.append((active_proxy(), config.retries))
        if active_proxy() is None:
            raise _geo()
        return item(1, vid).info

    monkeypatch.setattr(probe_mod, "extract_video_info", video)
    cfg = _proxy_cfg(("https://us:89", "US"))
    probe_mod.probe_video("abc", cfg)
    assert (None, cfg.retries) in seen and cfg.retries != 1
    assert ("https://us:89", 1) in seen


def test_no_proxies_set_up(monkeypatch):
    from ytop.yt import probe as probe_mod

    def video(vid, config, cache_root=None, *, quick=False):
        raise _geo()

    monkeypatch.setattr(probe_mod, "extract_video_info", video)
    got = probe_mod.probe_video("abc", Config()).items[0]
    assert got.error == "Region-blocked: available in US (no proxies set up)"
    assert got.error_kind == "region_blocked"


def test_only_wrong_country_proxies(monkeypatch):
    from ytop.yt import probe as probe_mod
    from ytop.yt.proxies import active_proxy

    seen = []

    def video(vid, config, cache_root=None, *, quick=False):
        seen.append(active_proxy())
        raise _geo()

    monkeypatch.setattr(probe_mod, "extract_video_info", video)
    got = probe_mod.probe_video("abc", _proxy_cfg(("https://de:89", "DE"))).items[0]
    assert got.error == "Region-blocked: available in US (no proxy set up for those countries)"
    assert set(seen) == {None}


def test_resync_does_not_reprobe_existing_region_blocked_videos(monkeypatch):
    from ytop.yt import probe as probe_mod

    monkeypatch.setattr(
        probe_mod, "extract_playlist_info",
        lambda value, config, limit=None, cache_root=None: (
            value, {"id": "PL1", "title": "List", "entries": [{"id": "old"}]}
        ),
    )
    calls = []
    monkeypatch.setattr(
        probe_mod, "extract_video_info", lambda *a, **k: calls.append(a),
    )
    result = probe_mod.probe_playlist(
        "PL1", _proxy_cfg(("https://us:89", "US")), probe_jobs=1,
        have=lambda playlist_id, video_id: True,
    )
    assert calls == []
    assert result.items[0].existing and result.items[0].proxy is None


def test_non_region_errors_are_not_retried(monkeypatch):
    from ytop.yt import probe as probe_mod

    calls = []

    def video(vid, config, cache_root=None, *, quick=False):
        calls.append(quick)
        raise probe_mod.ProbeError("Private video")

    monkeypatch.setattr(probe_mod, "extract_video_info", video)
    got = probe_mod.probe_video(
        "abc", _proxy_cfg(("https://us:89", "US")),
    ).items[0]
    assert len(calls) == 1
    assert got.error == "Private video" and got.error_kind is None


# --- downloads through the proxy that read the video ----------------------

def _record_proxies(monkeypatch):
    from ytop.yt.proxies import active_proxy

    recorded = []

    def fake_download(url, opts):
        recorded.append((opts.get("proxy"), active_proxy()))
        template = opts["outtmpl"]
        if isinstance(template, dict):
            template = template["default"]
        path = Path(template.replace("%(ext)s", "webm"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"media")

    monkeypatch.setattr(download_mod, "_ytdlp_download", fake_download)
    return recorded


def _two_track_item(proxy):
    entry = item(1, "vid1")
    entry.proxy = proxy
    entry.selection.alternates = [{
        "format_id": "249-0", "vcodec": "none", "acodec": "opus",
        "abr": 50, "language": "en-US", "ext": "webm", "filesize": 1000,
    }]
    return entry


def test_proxied_item_downloads_through_its_proxy(tmp_path: Path, fake_network, monkeypatch):
    _, state = fake_network
    state["items"] = [_two_track_item("https://us:89")]
    recorded = _record_proxies(monkeypatch)
    download_mod.fetch_playlist("PL1", Config(), library_root=make_library(tmp_path).root,
                                quiet=True)
    assert recorded and {r[0] for r in recorded} == {"https://us:89"}
    assert len(recorded) >= 2  # media and the alternate audio


def test_direct_item_downloads_direct(tmp_path: Path, fake_network, monkeypatch):
    _, state = fake_network
    state["items"] = [item(1, "vid1")]
    recorded = _record_proxies(monkeypatch)
    download_mod.fetch_playlist("PL1", Config(), library_root=make_library(tmp_path).root,
                                quiet=True)
    assert recorded == [(None, None)]


def test_region_blocked_result_kind_and_cli_summary(tmp_path: Path, fake_network, capsys):
    _, state = fake_network
    blocked = ProbedItem(index=1, id="vid1", url="u", title="T",
                         error="Region-blocked: available in US (no proxies set up)",
                         error_kind="region_blocked", countries=["US"])
    state["items"] = [blocked]
    result = download_mod.fetch_playlist("PL1", Config(),
                                         library_root=make_library(tmp_path).root)
    assert result.items[0].error_kind == "region_blocked"
    assert result.items[0].countries == ["US"]
    assert "1 region-blocked; countries that would cover them: US" in capsys.readouterr().out


def _failing_download(state, message):
    def hook(video_id, template):
        raise RuntimeError(message)
    state["on_download"] = hook


def test_direct_region_blocked_download_failure_is_classified(tmp_path, fake_network):
    calls, state = fake_network
    layout = make_library(tmp_path)
    _failing_download(
        state, "The uploader has not made this video available in your country"
    )
    result = download_mod.fetch_playlist("PL1", Config(), library_root=layout.root)
    assert result.items[0].error_kind == "region_blocked"


def test_proxied_download_failure_is_never_youtube_changed(tmp_path, fake_network):
    calls, state = fake_network
    layout = make_library(tmp_path)
    state["items"][0].proxy = "https://us:89"
    _failing_download(state, "Unable to extract player; Signature extraction failed")
    result = download_mod.fetch_playlist("PL1", Config(), library_root=layout.root)
    assert result.items[0].status == "failed" and result.items[0].error_kind is None


def test_direct_download_failure_can_be_youtube_changed(tmp_path, fake_network):
    calls, state = fake_network
    layout = make_library(tmp_path)
    _failing_download(state, "Unable to extract player; Signature extraction failed")
    result = download_mod.fetch_playlist("PL1", Config(), library_root=layout.root)
    assert result.items[0].error_kind == "youtube_changed"


def _run_process_item(monkeypatch, tmp_path, item, config):
    import threading

    from ytop.yt.proxies import active_proxy
    from ytop.yt.signin import active_signin

    seen = []
    monkeypatch.setattr(
        download_mod, "_process_item_body",
        lambda it, *a, **k: seen.append((active_proxy(), active_signin())) or (None, {}),
    )
    download_mod._process_item(item, {}, make_library(tmp_path), config, False,
                               threading.Lock())
    return seen


def test_signed_in_item_downloads_inside_signin_and_proxy(monkeypatch, tmp_path: Path):
    from dataclasses import replace

    it = ProbedItem(1, "vid", "u", proxy="https://us1.example:89", signed_in=True)
    seen = _run_process_item(monkeypatch, tmp_path, it,
                             replace(Config(), age_gate_browser="firefox"))
    assert seen == [("https://us1.example:89", "firefox")]


def test_unsigned_item_downloads_without_signin(monkeypatch, tmp_path: Path):
    from dataclasses import replace

    it = ProbedItem(1, "vid", "u", signed_in=False)
    seen = _run_process_item(monkeypatch, tmp_path, it,
                             replace(Config(), age_gate_browser="firefox"))
    assert seen == [(None, None)]


def test_signed_in_item_without_setting_downloads_without_signin(monkeypatch, tmp_path: Path):
    it = ProbedItem(1, "vid", "u", signed_in=True)
    assert _run_process_item(monkeypatch, tmp_path, it, Config()) == [(None, None)]


def test_signed_in_failure_is_not_youtube_changed():
    it = ProbedItem(1, "vid", "u", signed_in=True)
    assert download_mod._failure_kind(DownloadError("nsig extraction failed"), it) is None


def test_age_restricted_failure_kind_passes_through():
    it = ProbedItem(1, "vid", "u", signed_in=True)
    exc = DownloadError("Sign in to confirm your age")
    assert download_mod._failure_kind(exc, it) == "age_restricted"
