"""Age-gated videos are retried once, signed in with the age-gate profile."""

from __future__ import annotations

import pytest

from ytop.config import Config, ProxyServer
from ytop.yt import probe as probe_mod
from ytop.yt.probe import (
    ProbedItem,
    ProbeError,
    _record_failure,
    analyse_with_fallback,
    is_bot_check,
    probe_playlist,
    probe_video,
)
from ytop.yt.proxies import active_proxy
from ytop.yt.signin import active_signin

AGE = (
    "ERROR: [youtube] vid: Sign in to confirm your age. "
    "This video may be inappropriate for some users."
)
REGION = "ERROR: [youtube] vid: The uploader has not made this video available in your country"
US = "https://us1.example:89"


def _info(vid):
    return {"id": vid, "title": f"Title {vid}", "duration": 60}


class FakeAnalyse:
    """Records ``(active_proxy(), active_signin())`` per call; ``script(vid,
    proxy, signin)`` returns info or an exception to raise."""

    def __init__(self):
        self.calls: list[tuple[str | None, str | None]] = []
        self.script = lambda vid, proxy, signin: _info(vid)

    def __call__(self, video_id, config, cache_root=None, *, max_height=None,
                 preserve_audio=False):
        proxy, signin = active_proxy(), active_signin()
        self.calls.append((proxy, signin))
        got = self.script(video_id, proxy, signin)
        if isinstance(got, BaseException):
            raise got
        return got, "selection"


@pytest.fixture
def fake_analyse(monkeypatch):
    fake = FakeAnalyse()
    monkeypatch.setattr(probe_mod, "analyse_video", fake)
    return fake


def _age_gated_unless_signed_in(vid, proxy, signin):
    return _info(vid) if signin else ProbeError(AGE)


def test_age_gated_retries_signed_in(fake_analyse):
    fake_analyse.script = _age_gated_unless_signed_in
    info, sel, proxy, signed_in = analyse_with_fallback("vid", Config(age_gate_browser="firefox"))
    assert (proxy, signed_in) == (None, True)
    assert info["id"] == "vid" and sel == "selection"
    assert fake_analyse.calls == [(None, None), (None, "firefox")]


def test_age_gated_without_profile_is_age_restricted(fake_analyse):
    fake_analyse.script = _age_gated_unless_signed_in
    with pytest.raises(ProbeError, match="^Age-restricted — needs the YTOP sign-in$"):
        analyse_with_fallback("vid", Config())
    assert len(fake_analyse.calls) == 1


def test_region_and_age_gated_retries_signed_in_through_the_proxy(fake_analyse):
    def script(vid, proxy, signin):
        if proxy is None:
            return ProbeError(REGION, ["US"])
        return _info(vid) if signin else ProbeError(AGE)

    fake_analyse.script = script
    cfg = Config(age_gate_browser="firefox", proxies=(ProxyServer(US, "US"),))
    _, _, proxy, signed_in = analyse_with_fallback("vid", cfg)
    assert proxy == US and signed_in
    assert fake_analyse.calls == [(None, None), (US, None), (US, "firefox")]


def test_region_and_age_gated_without_profile_stops_and_says_so(fake_analyse):
    def script(vid, proxy, signin):
        return ProbeError(REGION, ["US"]) if proxy is None else ProbeError(AGE)

    fake_analyse.script = script
    cfg = Config(proxies=(ProxyServer(US, "US"), ProxyServer("https://us2.example:89", "US")))
    item = probe_video("vid", cfg).items[0]
    assert item.error == (
        "Age-restricted — needs the YTOP sign-in (also region-blocked; a proxy cleared that)"
    )
    assert item.error_kind == "age_restricted"
    assert item.countries == ["US"]
    assert fake_analyse.calls == [(None, None), (US, None)]  # the loop stopped


def test_unreadable_profile_is_reported_not_raised(fake_analyse):
    def script(vid, proxy, signin):
        if signin:
            return ProbeError(
                "could not find firefox cookies database in "
                "/home/x/.mozilla/firefox/Secret Profile"
            )
        return ProbeError(AGE)

    fake_analyse.script = script
    item = probe_video("vid", Config(age_gate_browser="firefox:Secret Profile")).items[0]
    assert item.error_kind == "age_restricted"
    assert item.error == (
        "Age-restricted — signed-in retry failed: the YTOP sign-in (firefox) "
        "could not be read on this computer"
    )
    assert "Secret Profile" not in item.error and ".mozilla" not in item.error


def test_signed_in_bot_check_does_not_stop_the_run(fake_analyse):
    def script(vid, proxy, signin):
        return ProbeError("Sign in to confirm you're not a bot") if signin else ProbeError(AGE)

    fake_analyse.script = script
    item = probe_video("vid", Config(age_gate_browser="firefox")).items[0]
    assert item.error_kind == "age_restricted" and not is_bot_check(item.error)
    assert item.error.endswith("YouTube asked to confirm it is not a bot")


@pytest.mark.parametrize("text", [
    "Only images are available for download",
    "n challenge solving failed: Some formats may be missing",
    "Requested format is not available",
])
def test_signed_in_without_deno_says_so(fake_analyse, text):
    fake_analyse.script = lambda vid, proxy, signin: ProbeError(text) if signin else ProbeError(AGE)
    item = probe_video("vid", Config(age_gate_browser="firefox")).items[0]
    assert item.error == (
        "Age-restricted — signed-in retry failed: signed-in reads may need a "
        "JavaScript runtime (Deno)"
    )


def test_other_signed_in_failure_is_short_and_redacted(fake_analyse):
    def script(vid, proxy, signin):
        if signin:
            return ProbeError("ERROR: Sign in to confirm your age via https://u:secret@h:1")
        return ProbeError(AGE)

    fake_analyse.script = script
    item = probe_video("vid", Config(age_gate_browser="firefox")).items[0]
    assert item.error.startswith("Age-restricted — signed-in retry failed: ")
    assert "secret" not in item.error
    assert "sign in to confirm" not in item.error.lower()
    assert not is_bot_check(item.error)


def test_signed_in_retry_failure_through_a_proxy_keeps_countries(fake_analyse):
    def script(vid, proxy, signin):
        if proxy is None:
            return ProbeError(REGION, ["US"])
        return ProbeError("Sign in to confirm you're not a bot") if signin else ProbeError(AGE)

    fake_analyse.script = script
    cfg = Config(age_gate_browser="firefox", proxies=(ProxyServer(US, "US"),))
    item = probe_video("vid", cfg).items[0]
    assert item.error_kind == "age_restricted"
    assert item.countries == ["US"]
    assert item.proxy is None and item.signed_in is False


def test_record_failure_prefers_age_restricted_over_its_region_cause():
    region = ProbeError(REGION, ["US"])
    try:
        try:
            raise region
        except ProbeError as direct:
            raise ProbeError("Age-restricted — needs the YTOP sign-in", ["US"]) from direct
    except ProbeError as exc:
        item = ProbedItem(index=1, id="vid", url="u")
        _record_failure(item, exc)
    assert item.error_kind == "age_restricted"
    assert item.countries == ["US"]


def test_plain_videos_never_signed_in(fake_analyse):
    assert analyse_with_fallback("vid", Config(age_gate_browser="firefox"))[3] is False
    assert fake_analyse.calls == [(None, None)]


def test_probe_video_marks_signed_in(fake_analyse):
    fake_analyse.script = _age_gated_unless_signed_in
    item = probe_video("vid", Config(age_gate_browser="firefox")).items[0]
    assert item.error is None and item.signed_in is True and item.proxy is None


def test_playlist_age_gated_item_fails_others_probe(fake_analyse, monkeypatch):
    entries = [{"id": "aged", "title": "A"}, {"id": "plain", "title": "P"}]
    monkeypatch.setattr(
        probe_mod, "extract_playlist_info",
        lambda value, config, limit=None, cache_root=None: (
            value, {"id": "PL1", "title": "List", "entries": entries}
        ),
    )
    fake_analyse.script = lambda vid, proxy, signin: (
        ProbeError(AGE) if vid == "aged" else _info(vid)
    )
    result = probe_playlist("PL1", Config(), probe_jobs=1)
    aged, plain = result.items
    assert aged.error == "Age-restricted — needs the YTOP sign-in"
    assert aged.error_kind == "age_restricted"
    assert plain.error is None and plain.info["id"] == "plain"
    assert plain.signed_in is False
    assert result.bot_blocked is False


def test_permission_denied_cookie_db_with_bare_spec_hides_the_path(fake_analyse):
    def script(vid, proxy, signin):
        if signin:
            return PermissionError(
                "[Errno 13] Permission denied: "
                "'/home/x/.mozilla/firefox/abcd.default-release/cookies.sqlite'"
            )
        return ProbeError(AGE)

    fake_analyse.script = script
    item = probe_video("vid", Config(age_gate_browser="firefox")).items[0]
    assert item.error_kind == "age_restricted"
    assert "could not be read on this computer" in item.error
    assert "/home" not in item.error and ".mozilla" not in item.error


@pytest.mark.parametrize("path", [
    "/home/x/.mozilla/firefox/abcd.default-release/places.db",
    "'/home/x/.mozilla/firefox/Secret Profile/x.db'",
    r"C:\Users\Me\AppData\Roaming\Mozilla\Firefox\Profiles\abcd.default\x.db",
    r"'C:\Users\Me\AppData\Roaming\Mozilla\Firefox\Profiles\ab cd\x.db'",
])
def test_fallback_reason_masks_paths(fake_analyse, path):
    def script(vid, proxy, signin):
        return OSError(f"unable to open {path} now") if signin else ProbeError(AGE)

    fake_analyse.script = script
    item = probe_video("vid", Config(age_gate_browser="firefox")).items[0]
    assert item.error == "Age-restricted — signed-in retry failed: unable to open <path> now"


def test_short_profile_does_not_mangle_unrelated_text(fake_analyse):
    def script(vid, proxy, signin):
        return ProbeError("HTTP Error 410: retry 1 of 10") if signin else ProbeError(AGE)

    fake_analyse.script = script
    item = probe_video("vid", Config(age_gate_browser="firefox:1")).items[0]
    assert item.error == "Age-restricted — signed-in retry failed: HTTP Error 410: retry 1 of 10"


def test_signed_in_read_skips_quick_analysis(fake_analyse, monkeypatch):
    """Signed-in reads need the JS challenge solved: quick analysis skips it."""
    seen: list[tuple[str | None, bool]] = []
    inner = fake_analyse.__call__

    def recording(video_id, config, cache_root=None, **kw):
        seen.append((active_signin(), config.quick_analysis))
        return inner(video_id, config, cache_root, **kw)

    monkeypatch.setattr(probe_mod, "analyse_video", recording)
    fake_analyse.script = _age_gated_unless_signed_in
    analyse_with_fallback("vid", Config(age_gate_browser="firefox", quick_analysis=True))
    assert seen == [(None, True), ("firefox", False)]
