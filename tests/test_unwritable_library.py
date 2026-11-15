"""A library the app cannot write to (a locked stick, a read-only mount, a
folder without write permission) still lists and searches everything, and
refuses tag edits with the read-only message instead of failing."""

from __future__ import annotations

import os
import re
import sqlite3
import stat
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from factories import add_playlist, library_set, make_library
from ytop.index import index_for
from ytop.layout import LibraryLayout, read_library
from ytop.web.app import create_app
from ytop.web.library import Library

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="POSIX permission bits do not stop root (or Windows)",
)

READ_ONLY = "This library is read-only, so its tags can&#39;t be changed."


def _lock(paths: list[Path]) -> list[tuple[Path, int]]:
    """Remove write permission from ``paths``; returns what to restore."""
    saved = []
    for path in paths:
        mode = path.stat().st_mode
        saved.append((path, mode))
        path.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    return saved


@pytest.fixture
def locked() -> Iterator[list[tuple[Path, int]]]:
    saved: list[tuple[Path, int]] = []
    yield saved
    for path, mode in reversed(saved):
        path.chmod(mode)


def _old_library(tmp_path: Path, label: str, *, with_db: bool) -> LibraryLayout:
    """A library as the previous version left it: no idx_* tables."""
    layout = make_library(tmp_path, label)
    add_playlist(layout, "P1", "Zebra lectures", [("v1", "Stripes"), ("v2", "Grass")])
    if with_db:
        conn = sqlite3.connect(layout.index_db)
        conn.execute("CREATE TABLE IF NOT EXISTS transcript_meta (k TEXT, v TEXT)")
        conn.commit()
        conn.close()
    else:
        layout.index_db.unlink(missing_ok=True)
    return layout


def _post(client: TestClient, path: str, **data: str):
    data["csrf_token"] = client.app.state.csrf_token
    return client.post(path, data=data, follow_redirects=False)


@pytest.mark.parametrize("with_db", [False, True])
def test_unwritable_library_lists_and_refuses_tag_edits(
    tmp_path: Path, locked: list, with_db: bool
) -> None:
    layout = _old_library(tmp_path, "Locked", with_db=with_db)
    targets = [layout.data_dir]
    if with_db:
        targets.append(layout.index_db)
    locked.extend(_lock(targets))
    libs = library_set(layout)
    assert not libs.libraries()[0].read_only  # not a newer-schema library
    client = TestClient(create_app(layout.root, libraries=libs))
    try:
        assert 'data-key="P1"' in client.get("/").text
        search = client.get("/search", params={"q": "Zebra"}).text
        assert "Zebra lectures" in search
        videos = client.get("/videos").text
        assert "Stripes" in videos and "Grass" in videos
        lib_id = read_library(layout.root).id
        for path, data in (
            ("/playlist/P1/tags", {"action": "add", "tag": "x"}),
            ("/playlist/P1/category", {"category": "x"}),
            (f"/tags/{lib_id}/rename", {"key": "a", "label": "b"}),
            (f"/tags/{lib_id}/hide", {"key": "a"}),
        ):
            response = _post(client, path, **data)
            assert response.status_code == 409, path
            assert READ_ONLY in response.text, path
        assert not layout.tags_file.exists()
        if with_db:
            conn = sqlite3.connect(f"file:{layout.index_db}?mode=ro", uri=True)
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
            conn.close()
            assert not any(name.startswith("idx_") for name in names)
    finally:
        client.app.state.jobs.close()


def test_unwritable_second_library_keys_match(tmp_path: Path, locked: list) -> None:
    first = make_library(tmp_path / "a", "First")
    add_playlist(first, "P1", "Zebra lectures", [("v1", "Stripes")])
    second = _old_library(tmp_path / "b", "Second", with_db=True)
    locked.extend(_lock([second.data_dir, second.index_db]))
    libs = library_set(first)
    libs.add_folder(second.root, "Second")
    lib = Library(libs)
    client = TestClient(create_app(first.root, libraries=libs))
    try:
        keys = re.findall(r'data-key="([^"]+)"', client.get("/").text)
        expected = {
            lib.key_for(ref.id, "P1") for ref in libs.libraries()
        }
        assert len(expected) == 2
        assert set(keys) == expected
    finally:
        client.app.state.jobs.close()


def test_unwritable_index_falls_back_to_memory(tmp_path: Path, locked: list) -> None:
    layout = _old_library(tmp_path, "Locked", with_db=False)
    locked.extend(_lock([layout.data_dir]))
    index = index_for(layout)
    index.ensure_fresh()
    assert [row.playlist_id for row in index.playlists()] == ["P1"]
    assert index.backfill_tags() == 0
    assert not layout.index_db.exists()
