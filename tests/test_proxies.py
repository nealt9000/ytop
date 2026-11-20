import sys
import textwrap

import pytest
from yt_dlp.utils import DownloadError, GeoRestrictedError

from ytop.yt import probe as probe_mod
from ytop.yt.probe import ProbeError, classify_failure
from dataclasses import replace

from ytop.config import Config, ProxyServer
from ytop.yt.probe import base_options
from ytop.yt.proxies import (
    RegionBlockedError,
    covering_countries,
    is_region_blocked,
    order_proxies,
    proxy_url,
    redact,
    region_blocked_message,
    using_proxy,
)
from ytop.yt.runner import SubprocessRunner

BLOCKED = "ERROR: [youtube] x: The uploader has not made this video available in your country"


def test_region_blocked_from_text_and_type():
    assert is_region_blocked(BLOCKED)
    assert is_region_blocked("Video not available from your location")
    assert is_region_blocked(GeoRestrictedError("blocked", countries=["US"]))
    wrapped = DownloadError("ERROR: x")
    wrapped.__cause__ = GeoRestrictedError("m", countries=["US"])
    assert is_region_blocked(wrapped)
    assert not is_region_blocked("Private video")
    assert not is_region_blocked(None)


def test_extract_keeps_allowed_countries(monkeypatch):
    class FakeYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            geo = GeoRestrictedError(BLOCKED, countries=["us", "CA"])
            raise DownloadError(BLOCKED, (type(geo), geo, None))

    monkeypatch.setattr(probe_mod, "YoutubeDL", FakeYDL)
    with pytest.raises(ProbeError) as e:
        probe_mod._extract("https://www.youtube.com/watch?v=x", {})
    assert e.value.countries == ["US", "CA"]
    assert classify_failure(e.value) == "region_blocked"


def test_classify_failure_prefers_region_blocked_over_youtube_changed():
    assert classify_failure(BLOCKED + " (unable to extract)") == "region_blocked"
    assert classify_failure("Unable to extract player") == "youtube_changed"
    assert classify_failure("Private video") == "other"


def _fake_python(tmp_path, doc):
    script = tmp_path / "fakepy.py"
    script.write_text(textwrap.dedent(f"""\
        import json, sys
        sys.stdin.readline()
        sys.stdout.write("\\x1eYTOP-RUNNER " + json.dumps({doc!r}) + "\\n")
        """))
    wrapper = tmp_path / "fakepy"
    wrapper.write_text(f"#!/bin/sh\nshift 4\nexec {sys.executable} {script}\n")
    wrapper.chmod(0o755)
    return str(wrapper)


@pytest.mark.skipif(sys.platform == "win32", reason="shell wrapper")
def test_subprocess_runner_carries_countries(tmp_path):
    python = _fake_python(tmp_path, {
        "error": "not made this video available in your country",
        "type": "GeoRestrictedError", "countries": ["de"],
    })
    with pytest.raises(RegionBlockedError) as e:
        SubprocessRunner([], python=python).extract("u", {})
    assert e.value.countries == ["DE"]


@pytest.mark.skipif(sys.platform == "win32", reason="shell wrapper")
def test_subprocess_runner_plain_error_is_not_region_blocked(tmp_path):
    python = _fake_python(tmp_path, {"error": "Private video", "type": "ExtractorError"})
    with pytest.raises(DownloadError) as e:
        SubprocessRunner([], python=python).extract("u", {})
    assert not isinstance(e.value, RegionBlockedError)


def test_child_reports_countries_from_a_real_child(tmp_path):
    pkg = tmp_path / "yt_dlp"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(textwrap.dedent("""\
        class YoutubeDL:
            def __init__(self, opts): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def extract_info(self, url, download=False):
                class Geo(Exception):
                    countries = ["fr"]
                raise Geo("not available in your country")
        """))
    with pytest.raises(RegionBlockedError) as e:
        SubprocessRunner([tmp_path]).extract("u", {})
    assert e.value.countries == ["FR"]


def test_proxy_url_adds_login():
    cfg = replace(Config(), proxy_username="u@x", proxy_password="p:w")
    assert proxy_url(ProxyServer("https://h:89"), cfg) == "https://u%40x:p%3Aw@h:89"
    assert proxy_url(ProxyServer("https://a:b@h:89"), cfg) == "https://a:b@h:89"
    assert proxy_url(ProxyServer("https://h:89"), Config()) == "https://h:89"


def test_order_proxies():
    us, de, any_ = ProxyServer("a", "US"), ProxyServer("b", "DE"), ProxyServer("c")
    assert order_proxies([any_, de, us], ["US"]) == [us, any_]
    assert order_proxies([any_, de, us], None) == [any_, de, us]
    assert order_proxies([de], ["US"]) == []


def test_redact():
    assert redact("Unable to connect to proxy https://u:secret@h:89/") == "Unable to connect to proxy https://***@h:89/"
    assert redact("socks5h://user@h:1 and http://a:b@c") == "socks5h://***@h:1 and http://***@c"


def test_redact_password_containing_at_sign():
    assert redact("x https://u:p@ss@h:89/y") == "x https://***@h:89/y"


def test_region_blocked_message():
    assert region_blocked_message(["US", "CA"], 2) == "Region-blocked: available in US, CA (tried 2 proxies)"
    assert region_blocked_message(["US"], 1).endswith("(tried 1 proxy)")
    assert region_blocked_message(["US"], 0).endswith("(no proxy set up for those countries)")
    assert region_blocked_message(None, -1) == "Region-blocked: available only in other countries (no proxies set up)"
    assert region_blocked_message([f"C{i}" for i in range(10)], 0).startswith(
        "Region-blocked: available in C0, C1, C2, C3, C4, C5, C6, C7 and 2 more")


def test_covering_countries():
    assert covering_countries([["US", "CA"], ["CA", "GB"], ["DE"], None]) == ["CA", "DE"]
    assert covering_countries([["US", "CA"]], offered={"US"}) == ["US"]


def test_base_options_uses_active_proxy():
    assert "proxy" not in base_options(Config())
    with using_proxy("https://h:89"):
        assert base_options(Config())["proxy"] == "https://h:89"
    assert "proxy" not in base_options(Config())
