from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from factories import library_set
from factories import make_library as factory_library
from ytop.fsnames import numbered_stem
from ytop.layout import LibraryLayout
from ytop.transcripts.captions import Segment
from ytop.transcripts.store import index_video_dir, write_transcript
from ytop.web import jobs as jobs_module
from ytop.web.app import create_app
from ytop.web.render import description_html
from ytop.yt.formats import Selection
from ytop.yt.metadata import write_json
from ytop.yt.probe import ProbedItem, ProbeResult

PLAYLIST_ID = "PLTEST00000000000000000000000001"
SECOND_ID = "PLTEST00000000000000000000000002"
MEDIA = b"0123456789" * 400


def lib_layout(tmp_path: Path) -> LibraryLayout:
    return LibraryLayout(tmp_path / "lib")


def give_media(layout: LibraryLayout, playlist_id: str, video_id: str,
               name: str) -> Path:
    """Store media for a video under its playlist folder and point at it."""
    doc = json.loads(
        (layout.playlist_meta_dir(playlist_id) / "playlist.json").read_text()
    )
    media = layout.media_root / doc["folder"] / name
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(MEDIA)
    meta_path = layout.video_meta_dir(playlist_id, video_id) / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["files"]["media"] = layout.relative_media(media)
    write_json(meta_path, meta)
    return media


def make_video(
    layout: LibraryLayout,
    playlist_id: str,
    video_id: str,
    *,
    title: str,
    index: int,
    total: int,
    folder: str,
    chapters: bool = True,
    transcript: bool = True,
    media: bool = True,
    description: str = "A lecture description.",
    duration: int = 100,
) -> Path:
    video_dir = layout.video_meta_dir(playlist_id, video_id)
    video_dir.mkdir(parents=True, exist_ok=True)

    media_ref = None
    if media:
        path = layout.media_root / folder / f"{numbered_stem(index, total, title)}.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(MEDIA)
        media_ref = layout.relative_media(path)
    Image.new("RGB", (160, 90), (30, 90, 140)).save(
        video_dir / "thumb.jpg", "JPEG"
    )

    chapter_rows = []
    if chapters:
        chapter_rows = [
            {"start_time": 0.0, "end_time": 30.0, "title": "Opening"},
            {"start_time": 30.0, "end_time": 100.0, "title": "Main argument"},
        ]
    write_json(video_dir / "meta.json", {
        "schema": 1,
        "id": video_id,
        "title": title,
        "description": description,
        "channel": "Test Channel",
        "channel_id": "UCtest0000000000000000000",
        "upload_date": "20240102",
        "duration": duration,
        "chapters": chapter_rows,
        "format": {
            "video_codec": "av01", "audio_codec": "opus",
            "audio_bitrate": 160,
            "audio_label": "English (US) original (default)",
            "width": 320, "height": 240, "fps": 15, "container": "mp4",
        },
        "files": {"media": media_ref, "thumbnail": "thumb.jpg"},
    })

    if transcript:
        write_transcript(
            video_dir,
            video_id=video_id,
            title=title,
            language="en",
            kind="auto",
            segments=[
                Segment(0.0, 8.0, "This lecture discusses isomorphism."),
                Segment(8.0, 16.0, "Gödel and Escher appear here."),
            ],
            raw_file="subtitles/en.auto.json",
        )
        index_video_dir(layout, video_dir, playlist_id=playlist_id, title=title)

    return video_dir


def make_library(tmp_path: Path) -> LibraryLayout:
    layout = factory_library(tmp_path, "Test")
    first = layout.playlist_meta_dir(PLAYLIST_ID)
    first.mkdir(parents=True)
    Image.new("RGB", (320, 180), (90, 40, 40)).save(
        first / "playlist-thumb.jpg", "JPEG"
    )
    write_json(first / "playlist.json", {
        "schema": 1, "id": PLAYLIST_ID, "title": "Playlist One",
        "folder": "Playlist One",
        "channel": "Channel One", "description": "Two test videos.",
        "video_count": 2,
        "entries": [
            {"id": "vid1", "index": 1, "title": "Video One",
             "available": True, "duration": 100},
            {"id": "vid2", "index": 2, "title": "Video Two",
             "available": True, "duration": 50},
        ],
        "files": {"thumbnail": "playlist-thumb.jpg"},
        "updated_at": "2026-01-01T00:00:00+00:00",
    })
    make_video(
        layout, PLAYLIST_ID, "vid1", title="Video One", index=1, total=2,
        folder="Playlist One",
        description="Read more at https://example.com/paper?x=1&y=2 and 1:23.",
        duration=100,
    )
    make_video(
        layout, PLAYLIST_ID, "vid2", title="Video Two", index=2, total=2,
        folder="Playlist One",
        chapters=False, transcript=False, media=False,
        description="", duration=50,
    )

    second = layout.playlist_meta_dir(SECOND_ID)
    second.mkdir(parents=True)
    write_json(second / "playlist.json", {
        "schema": 1, "id": SECOND_ID, "title": "Playlist Two",
        "folder": "Playlist Two",
        "channel": "Channel Two", "description": "",
        "video_count": 1,
        "entries": [
            {"id": "vid3", "index": 1, "title": "Video Three",
             "available": True, "duration": 20},
        ],
        "files": {},
        "updated_at": "2026-01-01T00:00:00+00:00",
    })
    make_video(
        layout, SECOND_ID, "vid3", title="Video Three", index=1, total=1,
        folder="Playlist Two",
        chapters=False, transcript=False, duration=20,
        description="A second playlist item.",
    )
    return layout


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    layout = make_library(tmp_path)
    return TestClient(create_app(layout.root, libraries=library_set(layout)))


def read_watch_data(html: str) -> dict:
    match = re.search(
        r'<script id="watch-data" type="application/json">(.*?)</script>',
        html, re.S,
    )
    assert match, "watch-data block missing"
    return json.loads(match.group(1))


def test_library_page_lists_playlists(client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    assert "Playlist One" in response.text
    assert "Playlist Two" in response.text
    # The stick summary counts the library's videos.
    assert re.search(r"<dt>Videos</dt><dd>3</dd>", response.text)
    assert "/playlist/PLTEST00000000000000000000000001" in response.text
    # The home cards no longer carry the quality line; the playlist page does.
    playlist = client.get("/playlist/PLTEST00000000000000000000000001").text
    assert "240p AV1" in playlist
    assert "Opus audio" in playlist


def test_playlist_card_total_time_counts_every_video(client: TestClient):
    """Like the library summary hours and the playlist page: vid2 (50 s) is
    not downloaded but still counts toward the card's total time."""
    home = client.get("/").text
    start = home.index(f'data-key="{PLAYLIST_ID}"')
    card = home[start:home.index("</article>", start)]
    assert 'data-length="150.0"' in card
    assert 'data-remaining="100.0"' in card


def test_playlist_page_keeps_youtube_order(client: TestClient):
    response = client.get(f"/playlist/{PLAYLIST_ID}")
    assert response.status_code == 200
    text = response.text
    assert text.index("Video One") < text.index("Video Two")
    assert "Two test videos." in text
    assert "2 chapters" in text
    assert "2:30" in text  # playlist total duration
    assert "transcript" in text
    assert "on disk" in text
    assert "240p AV1" in text
    assert "Opus 160k" in text
    assert "3.9 KiB" in text


def test_watch_page_has_player_chapters_and_transcript(
    client: TestClient, tmp_path: Path,
):
    give_media(lib_layout(tmp_path), PLAYLIST_ID, "vid2", "02 - Video Two.mp4")
    response = client.get(f"/watch/{PLAYLIST_ID}/vid1")
    assert response.status_code == 200
    text = response.text
    assert 'id="player"' in text
    assert 'src="/media/PLTEST00000000000000000000000001/vid1"' in text
    assert "Opening" in text
    assert "Main argument" in text
    assert "isomorphism" in text
    assert "https://example.com/paper?x=1" in text
    assert 'data-seek="83"' in text
    assert "isomorphism" in text
    assert "240p AV1 · 15 fps · MP4" in text
    assert "Opus 160k" in text
    assert "3.9 KiB" in text
    assert (
        'href="https://www.youtube.com/channel/UCtest0000000000000000000"'
        in text
    )
    assert 'target="_blank"' in text
    data = read_watch_data(text)
    assert data["videoId"] == "vid1"
    assert data["previewBase"].endswith("/vid1/preview?t=")
    assert data["duration"] == 100
    assert [chapter["title"] for chapter in data["chapters"]] == [
        "Opening", "Main argument"
    ]
    # The transcript is rendered once as HTML; caption timings live there too.
    assert "transcript" not in data
    assert text.count('class="segment-text"') == 2
    assert 'data-start="8' in text and 'data-end="16' in text
    assert data["nextUrl"].endswith("/vid2")
    assert data["prevUrl"] is None


def test_watch_page_honours_t_and_autoplay(client: TestClient):
    response = client.get(
        f"/watch/{PLAYLIST_ID}/vid1", params={"t": 15, "autoplay": 1}
    )
    data = read_watch_data(response.text)
    assert data["startAt"] == 15
    assert data["autoplay"] is True
    assert "autoplay=1" in response.text


def test_second_video_is_reported_missing_media(client: TestClient):
    response = client.get(f"/watch/{PLAYLIST_ID}/vid2")
    assert response.status_code == 200
    assert "Media file missing" in response.text
    assert "No description was provided" in response.text
    assert "No transcript is stored" in response.text


def test_media_range_and_images(client: TestClient):
    response = client.get(
        f"/media/{PLAYLIST_ID}/vid1",
        headers={"Range": "bytes=10-19"},
    )
    assert response.status_code == 206
    assert response.content == MEDIA[10:20]
    assert response.headers["content-range"] == f"bytes 10-19/{len(MEDIA)}"
    assert response.headers["content-type"] == "video/mp4"

    thumb = client.get(f"/media/{PLAYLIST_ID}/vid1/thumb")
    assert thumb.status_code == 200
    assert thumb.headers["content-type"] == "image/jpeg"

    playlist_thumb = client.get(f"/media/{PLAYLIST_ID}/thumb")
    assert playlist_thumb.status_code == 200



def test_progress_round_trip_and_watched_state(
    client: TestClient, tmp_path: Path,
):
    response = client.post("/api/progress", json={
        "video_id": "vid1", "position": 50, "duration": 100,
    })
    assert response.status_code == 200
    assert response.json()["watched"] is False

    response = client.post("/api/progress", json={
        "video_id": "vid1", "position": 99, "duration": 100,
    })
    assert response.status_code == 200
    assert response.json()["watched"] is True
    assert response.json()["fraction"] == pytest.approx(0.99)

    playlist = client.get(f"/playlist/{PLAYLIST_ID}").text
    assert "1/2 watched" in playlist

    # A later partial position must not clear an explicit watched mark.
    response = client.post("/api/progress", json={
        "video_id": "vid1", "position": 5, "duration": 100,
    })
    assert response.status_code == 200
    assert response.json()["watched"] is True

    assert client.post("/api/progress", json={
        "video_id": "nope", "position": 1,
    }).status_code == 404


def test_search_page_returns_highlighted_hits(client: TestClient):
    response = client.get("/search", params={"q": "isomorphism"})
    assert response.status_code == 200
    assert "<mark>isomorphism</mark>" in response.text
    assert f"/watch/{PLAYLIST_ID}/vid1?t=" in response.text

    response = client.get("/search", params={"q": "godel"})
    assert "<mark>Gödel</mark>" in response.text

    empty = client.get("/search", params={"q": "nothing-matches"})
    assert "No results" in empty.text


def test_transcript_api(client: TestClient):
    response = client.get(f"/api/transcript/{PLAYLIST_ID}/vid1")
    assert response.status_code == 200
    payload = response.json()
    assert payload["language"] == "en"
    assert len(payload["segments"]) == 2

    empty = client.get(f"/api/transcript/{PLAYLIST_ID}/vid2")
    assert empty.json()["transcript"] is None


def test_unknown_playlist_and_video_return_404(client: TestClient):
    assert client.get("/playlist/PLmissing").status_code == 404
    assert client.get(f"/watch/{PLAYLIST_ID}/missing").status_code == 404


def test_playlist_and_video_titles_can_be_edited(
    client: TestClient, tmp_path: Path,
):
    response = client.post(
        f"/playlist/{PLAYLIST_ID}/title",
        data={"title": "My renamed playlist"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "My renamed playlist" in client.get("/").text
    assert "My renamed playlist" in client.get(f"/playlist/{PLAYLIST_ID}").text

    response = client.post(
        f"/playlist/{PLAYLIST_ID}/videos/vid1/title",
        data={"title": "A better lecture title"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "A better lecture title" in client.get(f"/playlist/{PLAYLIST_ID}").text
    assert "A better lecture title" in client.get(f"/watch/{PLAYLIST_ID}/vid1").text
    assert "A better lecture title" in client.get(
        "/search", params={"q": "isomorphism"}
    ).text
    layout = lib_layout(tmp_path)
    assert layout.titles_file.is_file()
    # The media folder and file follow the new titles.
    assert (
        layout.media_root / "My renamed playlist" / "01 - A better lecture title.mp4"
    ).is_file()
    assert not (layout.media_root / "Playlist One").exists()
    assert client.get(f"/media/{PLAYLIST_ID}/vid1").content == MEDIA

    # An empty title restores the extracted title.
    client.post(
        f"/playlist/{PLAYLIST_ID}/title", data={"title": ""},
        follow_redirects=False,
    )
    client.post(
        f"/playlist/{PLAYLIST_ID}/videos/vid1/title", data={"title": ""},
        follow_redirects=False,
    )
    assert "Playlist One" in client.get(f"/playlist/{PLAYLIST_ID}").text
    assert "Video One" in client.get(f"/watch/{PLAYLIST_ID}/vid1").text
    assert (layout.media_root / "Playlist One" / "01 - Video One.mp4").is_file()


def test_retitle_and_reorder_blocked_while_downloading(
    client: TestClient, monkeypatch,
):
    from types import SimpleNamespace

    busy = SimpleNamespace(active=True, playlist_id=PLAYLIST_ID, library_id="")
    monkeypatch.setattr(client.app.state.jobs, "list", lambda: [busy])
    for url, data in (
        (f"/playlist/{PLAYLIST_ID}/title", {"title": "New"}),
        (f"/playlist/{PLAYLIST_ID}/videos/vid1/title", {"title": "New"}),
        (f"/playlist/{PLAYLIST_ID}/reorder", {"order": "vid2,vid1"}),
    ):
        response = client.post(url, data=data, follow_redirects=False)
        assert response.status_code == 409, url
        assert "Cancel the download" in response.text


def test_removing_a_playlist_deletes_its_folder(
    client: TestClient, tmp_path: Path,
):
    layout = lib_layout(tmp_path)
    path = layout.playlist_meta_dir(PLAYLIST_ID)
    folder = layout.media_root / "Playlist One"
    assert path.is_dir() and folder.is_dir()
    response = client.post(
        f"/playlist/{PLAYLIST_ID}/delete", follow_redirects=False
    )
    assert response.status_code == 303
    assert not path.exists()
    assert not folder.exists()
    assert client.get(f"/playlist/{PLAYLIST_ID}").status_code == 404
    library = client.get("/").text
    assert "Playlist One" not in library
    assert "Playlist Two" in library
    assert "No results" in client.get(
        "/search", params={"q": "isomorphism"}
    ).text


def test_preview_requires_media_and_falls_back_cleanly(client: TestClient):
    # vid2 has no media file, so there is nothing to extract a frame from.
    response = client.get(
        f"/media/{PLAYLIST_ID}/vid2/preview", params={"t": 5}
    )
    assert response.status_code == 404
    # vid1 exists but is not a real video; ffmpeg cannot produce a frame.
    response = client.get(
        f"/media/{PLAYLIST_ID}/vid1/preview", params={"t": 5}
    )
    assert response.status_code == 404


def test_alternate_audio_is_served_and_offered_in_the_player(
    client: TestClient,
):
    library = client.app.state.library
    video_dir = library.video_dir(PLAYLIST_ID, "vid1")
    audio_dir = video_dir / "audio"
    audio_dir.mkdir()
    (audio_dir / "249-0.m4a").write_bytes(b"alternate-audio")
    meta = json.loads((video_dir / "meta.json").read_text())
    meta["audio_tracks"] = [
        {
            "id": "249-9", "label": "Spanish (US) original",
            "language": "es-US", "codec": "opus", "embedded": True,
            "file": None, "original": True, "default": True, "size": 0,
        },
        {
            "id": "249-0", "label": "English (US)",
            "language": "en-US", "codec": "opus", "embedded": False,
            "bitrate": 50, "file": "audio/249-0.m4a", "original": False,
            "auto_dubbed": True, "default": False,
            "size": 15,
        },
    ]
    (video_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    text = client.get(f"/watch/{PLAYLIST_ID}/vid1").text
    assert "audio-toggle" in text
    assert "Original audio" in text
    assert "English (US)" in text
    assert "AI dub" in text
    assert "Opus 50k" in text
    data = read_watch_data(text)
    assert [track["id"] for track in data["audioTracks"]] == ["249-9", "249-0"]
    assert data["audioTracks"][0]["embedded"] is True
    assert data["audioTracks"][1]["url"].endswith("/audio/249-0")

    response = client.get(f"/media/{PLAYLIST_ID}/vid1/audio/249-0")
    assert response.status_code == 200
    assert response.content == b"alternate-audio"
    assert response.headers["content-type"] == "audio/mp4"
    assert client.head(f"/media/{PLAYLIST_ID}/vid1/audio/249-0").status_code == 200
    assert client.get(
        f"/media/{PLAYLIST_ID}/vid1/audio/missing"
    ).status_code == 404


def test_description_linkifies_urls_and_timestamps_and_escapes_html():
    html = description_html(
        '<script>alert(1)</script> see https://example.com/a?b=1&c=2. '
        "Then 1:23 and a word 12:34:56."
    )
    assert "&lt;script&gt;" in html
    assert "<script>" not in html
    assert 'href="https://example.com/a?b=1&amp;c=2"' in html
    assert 'data-seek="83"' in html
    assert 'data-seek="45296"' in html

    assert "No description" in description_html("")


def _wait_for_job(client: TestClient, job_id: str, status: str,
                  timeout: float = 5.0):
    import time
    deadline = time.time() + timeout
    payload = {}
    while time.time() < deadline:
        payload = client.get(f"/api/jobs/{job_id}").json()
        if payload.get("status") == status:
            return payload
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not reach {status}: {payload}")


def fake_probe_video(video_id, config, *, playlist=None, on_item=None,
                     on_item_start=None, **kwargs):
    target = playlist or {"id": "un-grouped", "title": "Un-grouped"}
    item = ProbedItem(
        index=1, id=video_id,
        url=f"https://www.youtube.com/watch?v={video_id}",
        title="Solo Video", duration=30.0,
    )
    item.info = {
        "id": video_id, "title": "Solo Video", "duration": 30,
        "channel": "Solo Channel",
        "formats": [
            {"format_id": "v", "vcodec": "av01.0.00M.08", "acodec": "none",
             "height": 240, "width": 320, "fps": 15, "ext": "webm"},
            {"format_id": "a", "vcodec": "none", "acodec": "opus",
             "abr": 50, "language": "en", "ext": "webm"},
        ],
    }
    item.selection = Selection(
        video=item.info["formats"][0], audio=item.info["formats"][1]
    )
    if on_item_start:
        on_item_start(item)
    if on_item:
        on_item(item)
    return ProbeResult(url=item.url, playlist=target, items=[item])


def test_reorder_video_moves_it_and_persists(client: TestClient, tmp_path: Path):
    page = client.get(f"/playlist/{PLAYLIST_ID}").text
    assert 'id="order-edit"' in page
    assert "Edit order" in page
    assert 'id="order-save"' in page
    assert 'id="order-form"' in page
    assert 'data-video-id="vid1"' in page
    assert 'data-video-id="vid2"' in page
    # Dragging replaced the old up/down row buttons.
    assert 'name="direction"' not in page

    response = client.post(
        f"/playlist/{PLAYLIST_ID}/reorder",
        data={"order": "vid2,vid1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    text = client.get(f"/playlist/{PLAYLIST_ID}").text
    assert text.index("Video Two") < text.index("Video One")

    doc = json.loads(
        (lib_layout(tmp_path).playlist_meta_dir(PLAYLIST_ID) / "playlist.json")
        .read_text()
    )
    assert [row["id"] for row in doc["entries"]] == ["vid2", "vid1"]
    assert [row["index"] for row in doc["entries"]] == [1, 2]
    assert doc["order"] == "manual"
    # The media file is renumbered to its new position.
    folder = lib_layout(tmp_path).media_root / "Playlist One"
    assert sorted(p.name for p in folder.glob("*.mp4")) == ["02 - Video One.mp4"]

    # A partial order keeps any omitted rows at the end rather than losing them.
    client.post(
        f"/playlist/{PLAYLIST_ID}/reorder",
        data={"order": "vid1"},
        follow_redirects=False,
    )
    doc = json.loads(
        (lib_layout(tmp_path).playlist_meta_dir(PLAYLIST_ID) / "playlist.json")
        .read_text()
    )
    assert [row["id"] for row in doc["entries"]] == ["vid1", "vid2"]

    assert client.post(
        f"/playlist/{PLAYLIST_ID}/reorder", data={}
    ).status_code == 400


def test_remove_video_deletes_its_media_but_keeps_watch_history(
    client: TestClient, tmp_path: Path,
):
    client.post("/api/progress", json={
        "video_id": "vid1", "position": 10, "duration": 100,
    })
    layout = lib_layout(tmp_path)
    video_dir = layout.video_meta_dir(PLAYLIST_ID, "vid1")
    media = layout.media_root / "Playlist One" / "01 - Video One.mp4"
    assert video_dir.is_dir() and media.is_file()

    response = client.post(
        f"/playlist/{PLAYLIST_ID}/videos/vid1/remove",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert not video_dir.exists()
    assert not media.exists()
    assert client.get(f"/watch/{PLAYLIST_ID}/vid1").status_code == 404

    doc = json.loads(
        (lib_layout(tmp_path).playlist_meta_dir(PLAYLIST_ID) / "playlist.json")
        .read_text()
    )
    assert [row["id"] for row in doc["entries"]] == ["vid2"]
    assert doc["video_count"] == 1
    assert "Video One" not in client.get(f"/playlist/{PLAYLIST_ID}").text

    progress = json.loads(layout.progress_file.read_text())
    assert "vid1" in progress["videos"]

    assert client.post(
        f"/playlist/{PLAYLIST_ID}/videos/missing/remove"
    ).status_code == 404


def test_preserve_audio_option_round_trips(client: TestClient, tmp_path: Path):
    response = client.post(
        f"/playlist/{PLAYLIST_ID}/options",
        data={"preserve_audio": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get(f"/playlist/{PLAYLIST_ID}").text
    assert "high-quality audio" in page
    assert "Keep high-quality audio" in page
    assert "checked" in page

    doc = json.loads(
        (lib_layout(tmp_path).playlist_meta_dir(PLAYLIST_ID) / "playlist.json")
        .read_text()
    )
    assert doc["options"]["preserve_audio"] is True

    client.post(
        f"/playlist/{PLAYLIST_ID}/options",
        data={},
        follow_redirects=False,
    )
    doc = json.loads(
        (lib_layout(tmp_path).playlist_meta_dir(PLAYLIST_ID) / "playlist.json")
        .read_text()
    )
    assert doc["options"]["preserve_audio"] is False


def test_create_a_playlist_from_library_videos(client: TestClient, tmp_path: Path):
    library = client.app.state.library
    source_dir = library.video_dir(PLAYLIST_ID, "vid1")
    source_meta_before = (source_dir / "meta.json").read_text()

    listing = client.get("/playlist/new")
    assert listing.status_code == 200
    assert "Video One" in listing.text
    assert "Video Three" in listing.text

    response = client.post(
        "/playlist/new",
        data={
            "title": "Lecture Mix",
            "video": [f"{PLAYLIST_ID}:vid1", f"{SECOND_ID}:vid3"],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/playlist/local-")
    playlist_id = location.rsplit("/", 1)[-1]

    page = client.get(location).text
    assert "Lecture Mix" in page
    assert "Video One" in page and "Video Three" in page
    assert "from your library" in page

    # The new playlist references the source's media file: no new bytes,
    # no hard links.
    layout = lib_layout(tmp_path)
    new_dir = library.video_dir(playlist_id, "vid1")
    new_meta = json.loads((new_dir / "meta.json").read_text())
    source_meta = json.loads(source_meta_before)
    assert new_meta["files"]["media"] == source_meta["files"]["media"]
    media_files = sorted(p for p in layout.media_root.rglob("*.mp4"))
    assert len(media_files) == 2  # vid1 and vid3 only
    assert all(os.stat(p).st_nlink == 1 for p in media_files)
    assert client.get(f"/media/{playlist_id}/vid1").content == MEDIA

    # Search stays keyed to the source playlist: the index holds one row per
    # video id, so a referencing copy does not duplicate hits.
    hits = client.get("/search", params={"q": "isomorphism"}).text
    assert f"/watch/{PLAYLIST_ID}/vid1" in hits
    assert f"/watch/{playlist_id}/vid1" not in hits

    # Library-wide usage counts a shared file once, while each playlist
    # still reports the media it plays.
    playlists = library.playlists()
    assert len(playlists) == 3
    assert library.library_bytes() < sum(p.total_bytes for p in playlists)

    # Rewriting the copy's metadata must not damage the source video.
    assert (source_dir / "meta.json").read_text() == source_meta_before
    assert new_meta["playlist"]["id"] == playlist_id

    # Removing the new playlist leaves the original and its media untouched.
    client.post(f"/playlist/{playlist_id}/delete", follow_redirects=False)
    assert library.video_dir(PLAYLIST_ID, "vid1").is_dir()
    assert client.get(f"/media/{PLAYLIST_ID}/vid1").content == MEDIA
    assert client.get(f"/watch/{PLAYLIST_ID}/vid1").status_code == 200
    hits = client.get("/search", params={"q": "isomorphism"}).text
    assert f"/watch/{PLAYLIST_ID}/vid1" in hits


def test_new_playlist_requires_a_title(client: TestClient):
    response = client.post(
        "/playlist/new",
        data={"title": "", "video": [f"{PLAYLIST_ID}:vid1"]},
    )
    assert response.status_code == 200
    assert "Give the new playlist a title." in response.text


def test_add_video_page_probes_into_the_chosen_playlist(
    client: TestClient, monkeypatch,
):
    monkeypatch.setattr(jobs_module, "probe_video", fake_probe_video)

    page = client.get("/add/video")
    assert page.status_code == 200
    assert "Un-grouped (single videos)" in page.text
    assert "Playlist One" in page.text

    response = client.post(
        "/add/video",
        data={
            "url": "https://youtu.be/abc12345678",
            "playlist": PLAYLIST_ID,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    job_id = response.headers["location"].rsplit("/", 1)[-1]
    job = _wait_for_job(client, job_id, "ready")
    assert job["kind"] == "video"
    assert job["target_playlist_id"] == PLAYLIST_ID
    assert job["target_playlist_title"] == "Playlist One"
    assert job["title"] == "Solo Video"

    status = client.get(f"/add/{job_id}").text
    assert "Destination" in status
    assert "Add to Playlist One" in status
    assert "Keep high-quality audio" in status
    assert "Opus 50k" in status
    assert "1 video" in status


def test_a_single_video_url_goes_to_ungrouped(
    client: TestClient, tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(jobs_module, "probe_video", fake_probe_video)
    response = client.post(
        "/add",
        data={"url": "https://www.youtube.com/watch?v=xyz98765432"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    job_id = response.headers["location"].rsplit("/", 1)[-1]
    job = _wait_for_job(client, job_id, "ready")
    assert job["kind"] == "video"
    assert job["target_playlist_id"] == "un-grouped"

    ungrouped = lib_layout(tmp_path).playlist_meta_dir("un-grouped")
    assert (ungrouped / "playlist.json").is_file()
    page = client.get("/playlist/un-grouped")
    assert page.status_code == 200
    assert "Un-grouped" in page.text

    # The destination picker hides the un-grouped option from the list of
    # named playlists but still offers it as the default.
    picker = client.get("/add/video").text
    assert picker.count("Un-grouped (single videos)") == 1


def test_add_playlist_url_does_not_create_a_video_job(
    client: TestClient, monkeypatch,
):
    monkeypatch.setattr(jobs_module, "probe_playlist", lambda *a, **k: None)
    response = client.post(
        "/add",
        data={"url": "https://www.youtube.com/playlist?list=PLabc123456"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    job_id = response.headers["location"].rsplit("/", 1)[-1]
    assert client.get(f"/api/jobs/{job_id}").json()["kind"] == "playlist"


def test_add_video_playlist_link_says_the_destination_is_not_used(
    client: TestClient, monkeypatch,
):
    """Choosing a destination and then pasting a playlist link adds the
    playlist as its own; the page says so instead of dropping the choice."""
    monkeypatch.setattr(jobs_module, "probe_playlist", lambda *a, **k: None)
    response = client.post(
        "/add/video",
        data={
            "url": "https://www.youtube.com/playlist?list=PLabc123456",
            "playlist": PLAYLIST_ID,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    job_id = response.headers["location"].rsplit("/", 1)[-1]
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["kind"] == "playlist"
    assert "not into Playlist One" in job["notice"]
    assert "not into Playlist One" in client.get(f"/add/{job_id}").text


def test_add_rejects_an_invalid_target(client: TestClient):
    response = client.post("/add", data={"url": "not a link"})
    assert response.status_code == 400
    video = client.post("/add/video", data={"url": "https://example.com/nope"})
    assert video.status_code == 200
    assert "no playlist or video id" in video.text


def test_video_retitle_blocked_while_another_holding_playlist_downloads(
    client: TestClient, monkeypatch,
):
    from types import SimpleNamespace

    library = client.app.state.library
    local_id = library.create_playlist("Mix", [(PLAYLIST_ID, "vid1")])
    busy = SimpleNamespace(active=True, playlist_id=local_id, library_id="")
    monkeypatch.setattr(client.app.state.jobs, "list", lambda: [busy])

    response = client.post(
        f"/playlist/{PLAYLIST_ID}/videos/vid1/title", data={"title": "New"},
        follow_redirects=False,
    )
    assert response.status_code == 409
    assert "downloading" in response.text
    # A video only this playlist holds can still be renamed.
    assert client.post(
        f"/playlist/{PLAYLIST_ID}/videos/vid2/title", data={"title": "Two!"},
        follow_redirects=False,
    ).status_code == 303


def test_search_across_libraries(tmp_path: Path):
    from ytop.layout import read_library
    from ytop.transcripts.store import search

    first = make_library(tmp_path / "a")
    second = make_library(tmp_path / "b")
    first_id = read_library(first.root).id
    second_id = read_library(second.root).id
    libs = library_set(first)
    libs.add_folder(second.root, "Second")

    hits = search([first, second], "isomorphism", 10)
    assert {hit["library_id"] for hit in hits} == {first_id, second_id}
    assert [hit["rank"] for hit in hits] == sorted(hit["rank"] for hit in hits)
    # The limit applies after the merge.
    assert len(search([first, second], "isomorphism", 1)) == 1

    client = TestClient(create_app(first.root, libraries=libs))
    page = client.get("/search", params={"q": "isomorphism"}).text
    # The same playlist id lives in both libraries, so links use keys,
    # quoted like the video result links ("@" -> "%40").
    assert f"/watch/{PLAYLIST_ID}%40{first_id[:8]}/vid1?t=" in page
    assert f"/watch/{PLAYLIST_ID}%40{second_id[:8]}/vid1?t=" in page
    only = client.get(
        "/search",
        params={"q": "isomorphism", "playlist": f"{PLAYLIST_ID}@{second_id[:8]}"},
    ).text
    assert f"/watch/{PLAYLIST_ID}%40{second_id[:8]}/vid1?t=" in only
    assert f"%40{first_id[:8]}/vid1?t=" not in only
    client.app.state.jobs.close()


def test_search_transcript_hit_keeps_timestamp_url(client: TestClient):
    text = client.get("/search", params={"q": "isomorphism"}).text
    assert f"/watch/{PLAYLIST_ID}/vid1?t=0.000#transcript" in text


def test_search_transcript_hit_for_a_missing_video_has_no_watch_link(
    tmp_path: Path,
):
    layout = make_library(tmp_path)
    make_video(
        layout, PLAYLIST_ID, "vid2", title="Video Two", index=2, total=2,
        folder="Playlist One", chapters=False, media=False, duration=50,
    )
    client = TestClient(create_app(layout.root, libraries=library_set(layout)))
    text = client.get("/search", params={"q": "isomorphism"}).text
    assert f"/watch/{PLAYLIST_ID}/vid1?t=0.000#transcript" in text
    assert "/vid2?t=" not in text
    assert "Not downloaded" in text
    client.app.state.jobs.close()


def test_search_box_label(client: TestClient):
    assert 'placeholder="Search titles, tags, transcripts"' in client.get("/").text


def test_search_escapes_hostile_metadata(tmp_path: Path):
    from ytop.tags import TagStore
    from ytop.web.library import TitleStore

    layout = make_library(tmp_path)
    evil = "<script>alert(1)</script>"
    TitleStore(layout).set_video_title("vid1", f"zork {evil}")
    TagStore(layout).add(PLAYLIST_ID, f"zork {evil}")
    client = TestClient(create_app(layout.root, libraries=library_set(layout)))
    text = client.get("/search", params={"q": "zork"}).text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text
    assert "<script>alert" not in text
    client.app.state.jobs.close()


def test_add_menu_in_top_bar(client: TestClient):
    html = client.get("/downloads").text
    start = html.index('<details class="add-menu"')
    menu = html[start:html.index("</details>", start)]
    positions = [
        menu.index(label)
        for label in ("Add playlist", "Add video", "New empty playlist")
    ]
    assert positions == sorted(positions)
    for href in ('href="/add"', 'href="/add/video"', 'href="/playlist/new"'):
        assert href in menu


def test_no_duplicate_add_buttons(client: TestClient):
    for path in ("/downloads", "/storage"):
        html = client.get(path).text
        head = html[html.index('<section class="page-head"'):]
        head = head[:head.index("</section>")]
        assert 'href="/add' not in head
    assert "/playlist/new" not in client.get("/add/video").text.split(
        "</header>", 1
    )[1]
    assert "Pasting a single video link adds it to <strong>Un-grouped</strong>." \
        in client.get("/add").text
    assert "Add video to this playlist" in client.get(
        f"/playlist/{PLAYLIST_ID}"
    ).text


def test_add_video_form_routes_playlist_link(client: TestClient, monkeypatch):
    calls = []
    jobs = client.app.state.jobs
    real = jobs.create_probe

    def recording_create_probe(url, **kwargs):
        calls.append((url, kwargs))
        return real(url, **{**kwargs, "library_id": None})

    monkeypatch.setattr(jobs_module, "probe_playlist", lambda *a, **k: None)
    monkeypatch.setattr(jobs, "create_probe", recording_create_probe)
    response = client.post(
        "/add/video",
        data={
            "url": "https://www.youtube.com/playlist?list=PLxxxxxxxxxx",
            "location": "somewhere",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    job_id = response.headers["location"].rsplit("/", 1)[-1]
    assert len(calls) == 1
    assert calls[0][1]["library_id"] == "somewhere"
    assert jobs.get(job_id).notice == (
        "That was a playlist link, so the whole playlist is being added."
    )
