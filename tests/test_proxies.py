import sys
import textwrap

import pytest
from yt_dlp.utils import DownloadError, GeoRestrictedError

from ytop.yt import probe as probe_mod
from ytop.yt.probe import ProbeError, classify_failure
from ytop.yt.proxies import RegionBlockedError, is_region_blocked
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
