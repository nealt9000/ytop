"""The library home page, video lists and stick summaries (rendered HTML)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from factories import add_playlist, library_set, make_library
from ytop.layout import LibraryLayout, init_library, read_library
from ytop.tags import TagStore
from ytop.web.app import create_app
from ytop.web.library import ProgressStore

STATIC_JS = Path(__file__).resolve().parents[1] / "src/ytop/web/static/js"


def _layout(tmp_path: Path) -> LibraryLayout:
    layout = make_library(tmp_path)
    add_playlist(layout, "P1", "One", [("v1", "A"), ("v2", "B"), ("v3", "C")])
    add_playlist(layout, "P2", "Two", [("w1", "X"), ("w2", "Y")])
    return layout


def _client(layout: LibraryLayout, libs=None) -> TestClient:
    return TestClient(create_app(layout.root, libraries=libs or library_set(layout)))


def _main(html: str) -> str:
    match = re.search(r"<main\b.*?</main>", html, re.S)
    assert match, "no <main> element"
    return match.group(0)


def test_home_sections_present(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    ProgressStore(layout).update("v1", 60, duration=60, watched=True)
    ProgressStore(layout).update("v2", 30, duration=60)
    client = _client(layout)
    text = client.get("/").text
    for heading in ("Continue watching", "Start something new", "Browse all"):
        assert heading in text
    assert "Video 2 of 3" in text
    assert 'href="/watch/P1/v2"' in text
    client.app.state.jobs.close()


def test_home_has_no_page_level_add_buttons(tmp_path: Path) -> None:
    client = _client(_layout(tmp_path))
    main = _main(client.get("/").text)
    assert 'href="/add/video"' not in main
    assert 'href="/playlist/new"' not in main
    client.app.state.jobs.close()


def test_home_empty_state_has_one_add_button(tmp_path: Path) -> None:
    layout = make_library(tmp_path)
    client = _client(layout)
    main = _main(client.get("/").text)
    assert main.count('href="/add"') == 1
    assert "ytop fetch" not in main
    client.app.state.jobs.close()


def test_home_escapes_tag_labels(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    evil = "<script>alert(1)</script>"
    # A hand-edited tags.json skips TagStore's label cleaning.
    layout.tags_file.parent.mkdir(parents=True, exist_ok=True)
    layout.tags_file.write_text(json.dumps({
        "schema": 1,
        "playlists": {"P1": {"add": [evil], "category": evil}},
    }))
    client = _client(layout)
    text = client.get("/").text
    assert "&lt;script&gt;" in text
    assert "<script>alert" not in text
    client.app.state.jobs.close()


def test_library_switcher_only_with_two_libraries(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    libs = library_set(layout)
    client = _client(layout, libs)
    assert 'class="library-switcher"' not in client.get("/").text

    usb_root = tmp_path / "usb" / "YTOP"
    init_library(usb_root, "Blue stick")
    add_playlist(LibraryLayout(usb_root), "PU", "Stick list", [("u1", "U")])
    libs.add_folder(usb_root, "Blue stick")
    usb_id = read_library(usb_root).id

    text = client.get("/").text
    assert 'class="library-switcher"' in text
    assert "All libraries" in text
    assert "Blue stick" in text
    # Several libraries shown: each summary heading is an h2.
    assert "<h1" not in _main(text).split("Continue watching")[0].split("library-switcher")[1]

    only = client.get("/", params={"library": usb_id}).text
    assert re.search(
        rf'<a[^>]*href="/\?library={usb_id}"[^>]*aria-current="page"', only
    )
    assert "Stick list" in only
    assert ">One<" not in only
    client.app.state.jobs.close()


def test_summary_edit_redirects_back(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    library_id = read_library(layout.root).id
    client = _client(layout)
    home = client.get("/").text
    assert f'action="/storage/{library_id}/label"' in home
    assert f'name="next" value="/?library={library_id}"' in home
    assert 'name="csrf_token"' in home

    response = client.post(
        f"/storage/{library_id}/label",
        data={"label": "Renamed stick", "description": "Lectures",
              "next": f"/?library={library_id}"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/?library={library_id}"
    assert read_library(layout.root).label == "Renamed stick"
    assert "Renamed stick" in client.get("/").text

    evil = client.post(
        f"/storage/{library_id}/label",
        data={"label": "Again", "next": "https://evil.example"},
        follow_redirects=False,
    )
    assert evil.status_code == 303
    assert evil.headers["location"] == "/storage"
    client.app.state.jobs.close()


def test_read_only_library_has_no_edit_button(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    marker = json.loads(layout.marker.read_text())
    marker["schema"] = 99
    layout.marker.write_text(json.dumps(marker))
    client = _client(layout)
    text = client.get("/").text
    assert "One" in text
    assert '/label"' not in text
    assert "summary-edit" not in text
    client.app.state.jobs.close()


def test_videos_page_filters_by_tag(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    TagStore(layout).add("P1", "Counterpoint")
    ProgressStore(layout).update("v1", 60, duration=60, watched=True)
    client = _client(layout)
    response = client.get("/videos", params={"tag": "counterpoint"})
    assert response.status_code == 200
    text = response.text
    assert "Videos tagged “Counterpoint”" in text
    assert 'href="/playlist/P1"' in text
    assert 'href="/watch/P1/v2"' in text
    assert "/watch/P2/" not in text
    assert 'href="/videos?tag=counterpoint"' in text
    assert "Watched" in text
    client.app.state.jobs.close()


def test_videos_page_marks_unavailable_videos(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    add_playlist(layout, "P3", "Three", [("z1", "Missing")], media=False)
    client = _client(layout)
    text = client.get("/videos").text
    assert "Not downloaded" in text
    assert 'href="/watch/P3/z1"' not in text
    assert 'href="/watch/P1/v1"' in text
    client.app.state.jobs.close()


def test_browse_js_wraps_storage() -> None:
    offenders = [
        path.name for path in STATIC_JS.glob("*.js")
        if path.name != "core.js" and "localStorage" in path.read_text()
    ]
    assert offenders == []
    core = (STATIC_JS / "core.js").read_text()
    assert "export function storageGet" in core
    assert "export function storageSet" in core
    assert (STATIC_JS / "browse.js").is_file()


def test_search_page_marks_missing_video_without_link(tmp_path: Path) -> None:
    layout = make_library(tmp_path)
    add_playlist(layout, "P1", "One", [("v1", "Zebra stripes")], media=False)
    client = _client(layout)
    page = client.get("/search", params={"q": "zebra"}).text
    assert "Not downloaded" in page
    assert 'href="/watch/P1/v1"' not in page
    client.app.state.jobs.close()


def test_pages_with_library_lists_read_no_meta_json(
    tmp_path: Path, monkeypatch
) -> None:
    import ytop.web.library as library_mod

    layout = _layout(tmp_path)
    ProgressStore(layout).update("v2", 60, duration=60, watched=True)
    client = _client(layout)
    client.get("/")  # builds the index
    real_read = library_mod.read_json

    def no_meta(path: Path):
        assert path.name != "meta.json", path
        return real_read(path)

    def no_media(*args: object, **kwargs: object):
        raise AssertionError("looked for a media file")

    monkeypatch.setattr(library_mod, "read_json", no_meta)
    monkeypatch.setattr(library_mod, "find_media", no_media)

    add = client.get("/add/video")
    assert add.status_code == 200
    assert '<option value="P1"' in add.text and ">One</option>" in add.text
    assert '<option value="P2"' in add.text

    new = client.get("/playlist/new")
    assert new.status_code == 200
    assert 'value="P1:v1"' in new.text and 'value="P2:w2"' in new.text
    assert 'src="/media/P1/v1/thumb"' in new.text
    assert "watched-tag" in new.text  # v2
    assert ">1.0 KiB<" in new.text  # media size from the index

    storage = client.get("/storage")
    assert storage.status_code == 200
    assert "2 playlists" in storage.text
    client.app.state.jobs.close()


def test_playlist_page_tags_follow_disk_changes(tmp_path: Path) -> None:
    from ytop.yt.metadata import read_json, write_json

    layout = _layout(tmp_path)
    client = _client(layout)
    client.get("/playlist/P1")
    for vid in ("v1", "v2", "v3"):
        path = layout.video_meta_dir("P1", vid) / "meta.json"
        meta = read_json(path) or {}
        meta["tags"] = ["Counterpoint"]
        write_json(path, meta)
    playlist = layout.playlist_meta_dir("P1") / "playlist.json"
    doc = read_json(playlist) or {}
    doc["title"] = "One!"
    write_json(playlist, doc)
    assert 'aria-label="Remove tag Counterpoint"' in client.get("/playlist/P1").text
    client.app.state.jobs.close()


def test_browse_js_spells_uncategorized_like_python() -> None:
    from ytop.tags import UNCATEGORIZED

    text = (STATIC_JS / "browse.js").read_text()
    assert f'"{UNCATEGORIZED}"' in text
    assert "Uncategorised" not in text


def test_add_menu_closes_when_focus_leaves() -> None:
    text = (STATIC_JS / "ui.js").read_text()
    body = text[text.index("export function initAddMenu"):text.index("// -- dialogs")]
    assert '"focusout"' in body
    assert "menu.contains(next)" in body
