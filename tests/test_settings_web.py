"""The Settings page: this stick (its platforms), updates, downloads; and
the version in every page's footer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from factories import add_playlist, library_set, make_library
from ytop import __version__
from ytop.config import ProxyServer, load_config
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


def test_proxy_settings_saved_and_password_never_rendered(home):
    client = _client(home, FakeAdds(None))
    r = client.post("/settings/proxies", data={
        "servers": "us https://h:89\nsocks5h://a:sekret@s:1080",
        "username": "u", "password": "pw-secret-123"}, follow_redirects=False)
    assert r.status_code == 303
    cfg = load_config(home.root)
    assert cfg.proxies[0] == ProxyServer("https://h:89", "US")
    assert (cfg.proxy_username, cfg.proxy_password) == ("u", "pw-secret-123")
    page = client.get("/settings").text
    assert "pw-secret-123" not in page and "sekret" not in page
    assert "Login saved" in page and "socks5h://a:\u2022\u2022\u2022@s:1080" in page
    client.app.state.jobs.close()


def test_blank_password_keeps_the_stored_one_and_masked_url_round_trips(home):
    client = _client(home, FakeAdds(None))
    client.post("/settings/proxies", data={
        "servers": "socks5h://a:sekret@s:1080", "username": "u", "password": "pw-secret-123"})
    client.post("/settings/proxies", data={
        "servers": "socks5h://a:\u2022\u2022\u2022@s:1080", "username": "u2", "password": ""})
    cfg = load_config(home.root)
    assert cfg.proxies == (ProxyServer("socks5h://a:sekret@s:1080"),)
    assert (cfg.proxy_username, cfg.proxy_password) == ("u2", "pw-secret-123")
    client.post("/settings/proxies", data={
        "servers": "https://h:1", "username": "u2", "clear_password": "1"})
    assert load_config(home.root).proxy_password == ""
    assert "Login saved" not in client.get("/settings").text
    client.app.state.jobs.close()


def test_bad_proxy_line_is_rejected_with_its_line_number(home):
    client = _client(home, FakeAdds(None))
    client.post("/settings/proxies", data={"servers": "https://ok:1"})
    bad = client.post("/settings/proxies", data={"servers": "https://ok:2\nftp://x"})
    assert bad.status_code == 400 and "line 2" in bad.text
    assert load_config(home.root).proxies == (ProxyServer("https://ok:1"),)
    client.app.state.jobs.close()


def test_control_characters_in_proxy_settings_are_refused_without_echo(home):
    client = _client(home, FakeAdds(None))
    client.post("/settings/proxies", data={"servers": "https://ok:1", "username": "u"})
    for field, value in (("username", "u\x00x"), ("password", "pw-secret-1\x1b[2J"),
                         ("servers", "https://ok:2\x07")):
        data = {"servers": "https://h:9", "username": "u2", "password": "x"}
        data[field] = value
        bad = client.post("/settings/proxies", data=data)
        assert bad.status_code == 400 and "control character" in bad.text
        assert "pw-secret-1" not in bad.text
    # Tabs and line breaks in the server list stay fine.
    ok = client.post("/settings/proxies", data={
        "servers": "us\thttps://h:1\r\nhttps://h:2", "username": "u"}, follow_redirects=False)
    assert ok.status_code == 303
    assert load_config(home.root).proxies[0] == ProxyServer("https://h:1", "US")
    client.app.state.jobs.close()


def test_proxy_settings_need_a_library():
    off = TestClient(create_app(None, libraries=library_set(None), platform_adds=FakeAdds(None)))
    assert off.post("/settings/proxies", data={"servers": ""}).status_code == 409
    assert "Region-blocked videos" in off.get("/settings").text
    off.app.state.jobs.close()


def _review_job(client, kind):
    from ytop.web.jobs import Job
    job = Job(id="j1", status="ready", kind="video", video_count=1)
    job.items = [{"index": 1, "id": "v", "title": "T", "duration": 1, "duration_label": "0:01",
                  "error": "Region-blocked: available in US (no proxies set up)",
                  "error_kind": kind, "status": "failed", "size": 0, "native_size": 0}]
    with client.app.state.jobs._lock:
        client.app.state.jobs._jobs[job.id] = job
    return client.get("/add/j1").text


def test_region_blocked_row_links_to_settings_when_no_proxies(home):
    client = _client(home, FakeAdds(None))
    page = _review_job(client, "region_blocked")
    assert "Region-blocked: available in US" in page
    assert 'href="/settings#proxies"' in page and "Set up proxies" in page
    client.post("/settings/proxies", data={"servers": "https://h:1"})
    assert "Set up proxies" not in client.get("/add/j1").text
    client.app.state.jobs.close()


def test_other_failures_get_no_proxy_link(home):
    client = _client(home, FakeAdds(None))
    assert "Set up proxies" not in _review_job(client, None)
    client.app.state.jobs.close()


def _blocked_playlist(home):
    import json as _json
    meta = add_playlist(home, "PLb", "Blocked list", [("v1", "One"), ("v2", "Two")], media=False)
    doc_path = meta / "playlist.json"
    doc = _json.loads(doc_path.read_text())
    doc["entries"][0]["error"] = "Region-blocked: available in US (no proxies set up)"
    doc["entries"][0]["available"] = False
    doc["entries"][1]["error"] = "Private video"
    doc_path.write_text(_json.dumps(doc))


def test_playlist_page_shows_region_block_and_links_to_proxy_setup(home):
    _blocked_playlist(home)
    client = _client(home, FakeAdds(None))
    page = client.get("/playlist/PLb").text
    assert "Region-blocked: available in US" in page
    assert page.count("Set up proxies") == 1 and 'href="/settings#proxies"' in page
    assert "Private video" not in page
    client.post("/settings/proxies", data={"servers": "https://h:1"})
    page = client.get("/playlist/PLb").text
    assert "Region-blocked: available in US" in page and "Set up proxies" not in page
    client.app.state.jobs.close()


def test_single_video_failure_links_to_proxy_setup(home):
    from ytop.web.jobs import Job
    client = _client(home, FakeAdds(None))
    job = Job(id="j2", status="failed", kind="video", video_count=1,
              error="Region-blocked: available in US (no proxies set up)")
    with client.app.state.jobs._lock:
        client.app.state.jobs._jobs[job.id] = job
    page = client.get("/add/j2").text
    assert "Region-blocked: available in US" in page and "Set up proxies" in page
    job.error = "Private video"
    assert "Set up proxies" not in client.get("/add/j2").text
    client.app.state.jobs.close()


def test_settings_age_gate_saves_and_shows(home):
    client = _client(home, FakeAdds(None))
    r = client.post("/settings/age-gate", data={"browser": " Firefox:My Prof "},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings#age-gate"
    assert load_config(home.root).age_gate_browser == "firefox:My Prof"
    page = client.get("/settings").text
    assert 'id="age-gate"' in page and "Age-restricted videos" in page
    assert 'value="firefox:My Prof"' in page
    client.app.state.jobs.close()


def test_settings_age_gate_rejects_bad_input(home):
    client = _client(home, FakeAdds(None))
    r = client.post("/settings/age-gate", data={"browser": "netscape"})
    assert r.status_code == 400 and "unknown browser" in r.text
    r = client.post("/settings/age-gate", data={"browser": "firefox:a\x00b"})
    assert r.status_code == 400 and "control character" in r.text
    assert load_config(home.root).age_gate_browser == ""
    off = TestClient(create_app(None, libraries=library_set(None), platform_adds=FakeAdds(None)))
    assert off.post("/settings/age-gate", data={"browser": "firefox"}).status_code == 409
    off.app.state.jobs.close()
    client.app.state.jobs.close()


def test_settings_age_gate_clear(home):
    client = _client(home, FakeAdds(None))
    client.post("/settings/age-gate", data={"browser": "firefox:p"})
    r = client.post("/settings/age-gate", data={"browser": "firefox:p", "clear": "1"},
                    follow_redirects=False)
    assert r.status_code == 303 and load_config(home.root).age_gate_browser == ""
    client.app.state.jobs.close()


def test_settings_age_gate_offers_firefox_profiles(home, monkeypatch):
    monkeypatch.setattr("ytop.web.app.firefox_profiles",
                        lambda: ["firefox:/p/Q6uSzFvx.Profile 1"])
    client = _client(home, FakeAdds(None))
    page = client.get("/settings").text
    assert '<option value="firefox:/p/Q6uSzFvx.Profile 1">' in page
    assert '<option value="brave">' in page and '<option value="firefox">' not in page
    client.app.state.jobs.close()


def test_age_restricted_row_links_to_settings_until_set(home):
    client = _client(home, FakeAdds(None))
    from ytop.web.jobs import Job
    job = Job(id="j3", status="ready", kind="video", video_count=1)
    job.items = [{"index": 1, "id": "v", "title": "T", "duration": 1, "duration_label": "0:01",
                  "error": "Age-restricted — needs the YTOP sign-in",
                  "error_kind": "age_restricted", "status": "failed", "size": 0,
                  "native_size": 0}]
    with client.app.state.jobs._lock:
        client.app.state.jobs._jobs[job.id] = job
    page = client.get("/add/j3").text
    assert "Age-restricted — needs the YTOP sign-in" in page
    assert 'href="/settings#age-gate"' in page and "Set up the age sign-in" in page
    client.post("/settings/age-gate", data={"browser": "brave"})
    page = client.get("/add/j3").text
    assert "Age-restricted — needs" in page and "Set up the age sign-in" not in page
    client.app.state.jobs.close()


def test_age_restricted_job_failure_and_playlist_link(home):
    from ytop.web.jobs import Job
    client = _client(home, FakeAdds(None))
    job = Job(id="j4", status="failed", kind="video", video_count=1,
              error="Age-restricted — needs the YTOP sign-in")
    with client.app.state.jobs._lock:
        client.app.state.jobs._jobs[job.id] = job
    assert "Set up the age sign-in" in client.get("/add/j4").text
    import json as _json
    meta = add_playlist(home, "PLa", "Aged", [("v1", "One")], media=False)
    doc_path = meta / "playlist.json"
    doc = _json.loads(doc_path.read_text())
    doc["entries"][0]["error"] = "Age-restricted — needs the YTOP sign-in"
    doc["entries"][0]["available"] = False
    doc_path.write_text(_json.dumps(doc))
    page = client.get("/playlist/PLa").text
    assert "Age-restricted — needs" in page and page.count("Set up the age sign-in") == 1
    client.post("/settings/age-gate", data={"browser": "brave"})
    assert "Set up the age sign-in" not in client.get("/playlist/PLa").text
    client.app.state.jobs.close()
