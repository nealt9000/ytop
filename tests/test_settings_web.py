"""The Settings page: this stick (its platforms), updates, downloads; and
the version in every page's footer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from factories import library_set, make_library
from ytop import __version__
from ytop.config import load_config
from ytop.stickplatforms import PlatformAdds
from ytop.web.app import create_app


class FakeAdds(PlatformAdds):
    """A stick whose build.json lists ``platforms``; ``start`` is recorded."""

    def __init__(self, ytop: Path | None) -> None:
        super().__init__(lambda: None, ytop)  # type: ignore[arg-type,return-value]
        self.started: list[list[str]] = []

    def start(self, platforms: list[str]) -> None:
        if not platforms:
            raise ValueError("choose a platform to add")
        self.started.append(list(platforms))


def _stick(tmp_path: Path, platforms: list[str]) -> Path:
    ytop = tmp_path / "stickroot/.ytop"
    ytop.mkdir(parents=True)
    (ytop / "build.json").write_text(json.dumps({
        "build_id": "b1", "version": "0.1.0", "platforms": platforms,
        "sizes": {"linux-x86_64": 200_000_000},
    }))
    return ytop


@pytest.fixture
def home(tmp_path: Path):
    return make_library(tmp_path / "home", "Laptop")


def _client(home, adds) -> TestClient:
    return TestClient(create_app(home.root, libraries=library_set(home), platform_adds=adds))


def test_footer_shows_the_version_on_every_page(home, tmp_path: Path):
    client = _client(home, FakeAdds(None))
    for path in ("/", "/storage", "/settings", "/downloads", "/no-such-page"):
        page = client.get(path).text
        assert f"YTOP {__version__}" in page, path
    client.app.state.jobs.close()


def test_settings_lists_the_sticks_platforms_and_offers_the_rest(home, tmp_path: Path):
    adds = FakeAdds(_stick(tmp_path, ["linux-x86_64"]))
    client = _client(home, adds)
    page = client.get("/settings").text
    assert 'href="/settings"' in page  # in the top bar
    assert "This stick" in page and "Linux (Intel/AMD)" in page
    assert 'name="platforms" value="windows-x86_64"' in page
    assert 'name="platforms" value="linux-x86_64"' not in page
    assert "Mac (Apple silicon)" in page
    # Updates moved here from Storage.
    assert "YouTube parts" in page and "YTOP updates" in page
    assert "YouTube parts" not in client.get("/storage").text

    response = client.post("/settings/platforms",
                           data={"platforms": ["windows-x86_64", "macos-arm64"]},
                           follow_redirects=False)
    assert response.status_code == 303
    assert adds.started == [["windows-x86_64", "macos-arm64"]]
    assert client.post("/settings/platforms", data={}).status_code == 409
    client.app.state.jobs.close()


def test_settings_shows_an_add_in_progress_and_its_result(home, tmp_path: Path):
    adds = FakeAdds(_stick(tmp_path, ["linux-x86_64"]))
    client = _client(home, adds)
    adds._state = {"running": True, "platforms": ["windows-x86_64"],
                   "label": "Downloading lib-windows…", "result": None}
    page = client.get("/settings").text
    assert "Adding Windows: Downloading lib-windows…" in page
    assert 'http-equiv="refresh"' in page
    assert "Download and add" not in page
    adds._state = {"running": False, "platforms": ["windows-x86_64"], "label": "",
                   "result": {"status": "failed", "message": "No network: try again."}}
    page = client.get("/settings").text
    assert "No network: try again." in page and 'http-equiv="refresh"' not in page
    client.app.state.jobs.close()


def test_settings_off_a_stick(home):
    client = _client(home, FakeAdds(None))
    page = client.get("/settings").text
    assert "not running from a stick" in page
    assert "Download and add" not in page
    client.app.state.jobs.close()


def test_download_settings_are_saved_and_checked(home):
    client = _client(home, FakeAdds(None))
    response = client.post("/settings/downloads", data={
        "target_mb_per_hour": "250", "max_height": "1080", "min_height": "360",
        "audio_language": "de", "jobs": "2",
    }, follow_redirects=False)
    assert response.status_code == 303
    cfg = load_config(home.root)
    assert (cfg.target_mb_per_hour, cfg.max_height, cfg.min_height) == (250.0, 1080, 360)
    assert (cfg.audio_language, cfg.jobs, cfg.quick_analysis) == ("de", 2, False)
    page = client.get("/settings").text
    assert 'value="1080" selected' in page and 'value="de"' in page

    bad = client.post("/settings/downloads", data={"target_mb_per_hour": "lots"})
    assert bad.status_code == 400 and "is not a number" in bad.text
    assert client.post("/settings/downloads", data={"jobs": "9"}).status_code == 400
    assert client.post("/settings/downloads",
                       data={"target_mb_per_hour": "-5"}).status_code == 400
    assert load_config(home.root).target_mb_per_hour == 250.0  # unchanged
    client.app.state.jobs.close()
