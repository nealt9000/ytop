from __future__ import annotations

import json
from pathlib import Path

import pytest

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
