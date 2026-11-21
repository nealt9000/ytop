from ytop.config import SESSION_AUTH, Config
from ytop.yt.pot import apply_auth_options
from ytop.yt.probe import ProbeError, classify_failure
from ytop.yt.signin import active_signin, is_age_gated, using_signin


def test_is_age_gated_text_and_chain():
    assert is_age_gated("ERROR: [youtube] x: Sign in to confirm your age. This video may be inappropriate")
    assert not is_age_gated("Sign in to confirm you're not a bot")
    try:
        try:
            raise ValueError("Sign in to confirm your age")
        except ValueError as e:
            raise ProbeError("wrapped") from e
    except ProbeError as outer:
        assert is_age_gated(outer)


def test_signin_context_adds_cookies_only_inside():
    opts = {}
    apply_auth_options(opts, Config(pot_provider="off"))
    assert "cookiesfrombrowser" not in opts
    with using_signin("firefox:Q6uSzFvx.Profile 1"):
        assert active_signin() == "firefox:Q6uSzFvx.Profile 1"
        opts = {}
        apply_auth_options(opts, Config(pot_provider="off"))
    assert active_signin() is None
    assert opts["cookiesfrombrowser"] == ("firefox", "Q6uSzFvx.Profile 1", None, None)


def test_signin_context_overrides_session_browser(monkeypatch):
    monkeypatch.setattr(SESSION_AUTH, "browser", "brave")
    with using_signin("firefox"):
        opts = {}
        apply_auth_options(opts, Config(pot_provider="off"))
    assert opts["cookiesfrombrowser"][0] == "firefox"


def test_classify_failure_age_restricted():
    assert classify_failure("Sign in to confirm your age") == "age_restricted"
    assert classify_failure(
        "not made this video available in your country\nSign in to confirm your age"
    ) == "region_blocked"
