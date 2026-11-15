"""Static CSS/JS must be re-checked on each load, so a browser never pairs
new pages with the stylesheet or scripts from before a stick upgrade."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from factories import library_set, make_library
from ytop.web.app import create_app


@pytest.mark.parametrize("path", ["/static/app.css", "/static/app.js", "/static/js/browse.js"])
def test_static_files_are_revalidated(tmp_path: Path, path: str) -> None:
    layout = make_library(tmp_path)
    client = TestClient(create_app(layout.root, libraries=library_set(layout)))
    try:
        first = client.get(path)
        assert first.status_code == 200
        assert first.headers["cache-control"] == "no-cache"
        etag = first.headers["etag"]
        again = client.get(path, headers={"If-None-Match": etag})
        assert again.status_code == 304
    finally:
        client.app.state.jobs.close()
