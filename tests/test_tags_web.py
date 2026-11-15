"""Editing tags and categories from the playlist page and /tags."""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from factories import add_playlist, library_set, make_library
from ytop.layout import LibraryLayout
from ytop.tags import TagStore
from ytop.web.app import create_app

READ_ONLY = "This library is read-only, so its tags can't be changed."
DAMAGED = "The tags file on this library is damaged; nothing was changed."


def _layout(tmp_path: Path) -> LibraryLayout:
    layout = make_library(tmp_path)
    add_playlist(layout, "P1", "One", [("v1", "A")])
    add_playlist(layout, "P2", "Two", [("w1", "X")])
    return layout


def _client(layout: LibraryLayout) -> TestClient:
    return TestClient(create_app(layout.root, libraries=library_set(layout)))


def _post(client: TestClient, path: str, **data: str):
    data["csrf_token"] = client.app.state.csrf_token
    return client.post(path, data=data, follow_redirects=False)


def _lib_id(layout: LibraryLayout) -> str:
    from ytop.layout import read_library

    info = read_library(layout.root)
    assert info is not None
    return info.id


def test_add_and_remove_playlist_tag(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    client = _client(layout)
    response = _post(client, "/playlist/P1/tags", action="add", tag="Counterpoint")
    assert response.status_code == 303
    assert response.headers["location"] == "/playlist/P1"
    page = client.get("/playlist/P1").text
    assert "Counterpoint" in page
    assert 'name="tag" value="counterpoint"' in page
    assert "<datalist" in page
    response = _post(client, "/playlist/P1/tags", action="remove", tag="counterpoint")
    assert response.status_code == 303
    after = client.get("/playlist/P1").text
    assert 'aria-label="Remove tag Counterpoint"' not in after
    assert 'aria-label="Remove tag Counterpoint"' in page
    assert "&amp;library=" in page
    client.app.state.jobs.close()


def test_set_and_clear_category(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    client = _client(layout)
    response = _post(client, "/playlist/P1/category", category="Lectures")
    assert response.status_code == 303
    assert response.headers["location"] == "/playlist/P1"
    assert 'value="Lectures"' in client.get("/playlist/P1").text
    _post(client, "/playlist/P1/category", category="")
    assert 'value="Lectures"' not in client.get("/playlist/P1").text
    assert TagStore(layout).read().playlists.get("P1") is None
    client.app.state.jobs.close()


def test_rename_merges_tags_on_tags_page(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    store = TagStore(layout)
    store.add("P1", "Fugue")
    store.add("P2", "Counterpoint")
    client = _client(layout)
    page = client.get("/tags").text
    assert "Fugue" in page and "Counterpoint" in page
    assert "Rename or merge into" in page
    assert 'href="/videos?tag=fugue&amp;library=' in page
    lib = _lib_id(layout)
    response = _post(client, f"/tags/{lib}/rename", key="fugue", label="Counterpoint")
    assert response.status_code == 303
    assert response.headers["location"] == f"/tags?library={lib}"
    page = client.get("/tags", params={"library": lib}).text
    assert 'href="/videos?tag=fugue&amp;library=' not in page
    assert "Fugue → Counterpoint" in page  # listed so it can be undone
    assert re.search(r"Counterpoint.*?2 playlists", page, re.S)
    client.app.state.jobs.close()


def test_hide_and_unhide(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    TagStore(layout).add("P1", "Fugue")
    client = _client(layout)
    lib = _lib_id(layout)
    response = _post(client, f"/tags/{lib}/hide", key="Fugue")
    assert response.status_code == 303
    page = client.get("/tags").text
    assert "Hidden tags" in page
    assert 'href="/videos?tag=fugue&amp;library=' not in page
    assert "/unhide" in page
    assert 'aria-label="Remove tag Fugue"' not in client.get("/playlist/P1").text
    assert 'aria-label="Unhide fugue"' in page
    assert 'aria-label="Hide Fugue"' not in page
    response = _post(client, f"/tags/{lib}/unhide", key="fugue")
    assert response.status_code == 303
    page = client.get("/tags").text
    assert 'href="/videos?tag=fugue&amp;library=' in page
    assert "Hidden tags" not in page
    client.app.state.jobs.close()


def test_tag_edit_on_read_only_library_is_refused(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    marker = json.loads(layout.marker.read_text())
    marker["schema"] = 99
    layout.marker.write_text(json.dumps(marker))
    client = _client(layout)
    lib = _lib_id(layout)
    for path, data in (
        ("/playlist/P1/tags", {"action": "add", "tag": "x"}),
        ("/playlist/P1/category", {"category": "x"}),
        (f"/tags/{lib}/rename", {"key": "a", "label": "b"}),
        (f"/tags/{lib}/hide", {"key": "a"}),
        (f"/tags/{lib}/unhide", {"key": "a"}),
    ):
        response = _post(client, path, **data)
        assert response.status_code == 409, path
        assert READ_ONLY.replace("'", "&#39;") in response.text, path
    assert not layout.tags_file.exists()
    client.app.state.jobs.close()


def test_damaged_tags_file_refuses_edit(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    layout.tags_file.parent.mkdir(parents=True, exist_ok=True)
    layout.tags_file.write_bytes(b"{ not json")
    client = _client(layout)
    lib = _lib_id(layout)
    for path, data in (
        ("/playlist/P1/tags", {"action": "add", "tag": "x"}),
        ("/playlist/P1/category", {"category": "x"}),
        (f"/tags/{lib}/hide", {"key": "a"}),
    ):
        response = _post(client, path, **data)
        assert response.status_code == 409, path
        assert DAMAGED in response.text, path
    assert layout.tags_file.read_bytes() == b"{ not json"
    client.app.state.jobs.close()


def test_ambiguous_key_refused(tmp_path: Path) -> None:
    first = make_library(tmp_path / "a")
    add_playlist(first, "P1", "One", [("v1", "A")])
    second = make_library(tmp_path / "b")
    add_playlist(second, "P1", "One", [("v1", "A")])
    libs = library_set(first)
    libs.add_folder(second.root, "Second")
    client = TestClient(create_app(first.root, libraries=libs))
    for path, data in (
        ("/playlist/P1/tags", {"action": "add", "tag": "x"}),
        ("/playlist/P1/category", {"category": "x"}),
    ):
        response = _post(client, path, **data)
        assert response.status_code == 409, path
    assert not first.tags_file.exists() and not second.tags_file.exists()
    client.app.state.jobs.close()


def _playlist_tags(client: TestClient, playlist: str) -> set[str]:
    return set(re.findall(r'aria-label="Remove tag ([^"]+)"', client.get(f"/playlist/{playlist}").text))


def test_rename_can_be_undone_from_tags_page(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    store = TagStore(layout)
    store.add("P1", "Fugue")
    store.add("P2", "Counterpoint")
    client = _client(layout)
    lib = _lib_id(layout)
    assert "Renamed tags" not in client.get("/tags").text
    _post(client, f"/tags/{lib}/rename", key="fugue", label="Counterpoint")
    page = client.get("/tags").text
    assert "Renamed tags" in page
    assert "Fugue → Counterpoint" in page
    assert 'aria-label="Undo renaming Fugue to Counterpoint"' in page
    assert _playlist_tags(client, "P1") == {"Counterpoint"}
    response = _post(client, f"/tags/{lib}/rename", key="fugue", label="")
    assert response.status_code == 303
    page = client.get("/tags").text
    assert "Renamed tags" not in page
    assert re.search(r"Fugue</a>\s*<span[^>]*>1 playlist<", page)
    assert re.search(r"Counterpoint</a>\s*<span[^>]*>1 playlist<", page)
    assert _playlist_tags(client, "P1") == {"Fugue"}
    assert _playlist_tags(client, "P2") == {"Counterpoint"}
    assert TagStore(layout).read().renames == {}
    client.app.state.jobs.close()


def test_renaming_back_does_not_swap_tags(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    store = TagStore(layout)
    store.add("P1", "Fugue")
    store.add("P2", "Counterpoint")
    client = _client(layout)
    lib = _lib_id(layout)
    _post(client, f"/tags/{lib}/rename", key="fugue", label="Counterpoint")
    _post(client, f"/tags/{lib}/rename", key="counterpoint", label="Fugue")
    assert _playlist_tags(client, "P1") == {"Fugue"}
    assert _playlist_tags(client, "P2") == {"Fugue"}
    page = client.get("/tags").text
    assert 'href="/videos?tag=fugue&amp;library=' in page
    assert 'href="/videos?tag=counterpoint&amp;library=' not in page
    assert re.search(r"Fugue</a>\s*<span[^>]*>2 playlists<", page)
    client.app.state.jobs.close()
