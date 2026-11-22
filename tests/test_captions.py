from __future__ import annotations

import io
import json
import urllib.error

import pytest

from ytop.transcripts import captions
from ytop.transcripts.captions import (
    Caption,
    CaptionError,
    choose_caption,
    download_caption,
    fetch_transcript_api,
    parse_json3,
    parse_vtt,
)

JSON3 = json.dumps({
    "events": [
        {"tStartMs": 0, "dDurationMs": 2000,
         "segs": [{"utf8": "Hello "}, {"utf8": "world"}]},
        {"tStartMs": 2000, "segs": [{"utf8": "Second line"}]},
        {"tStartMs": 3000, "dDurationMs": 1000, "segs": []},
    ]
}).encode()

VTT = b"""WEBVTT

00:00:00.400 --> 00:00:03.900 align:start position:0%
Hello <c.colorE5E5E5>world</c> &amp; friends

00:00:04.000 --> 00:00:06.500
Second <i>line</i>
"""


def test_parse_json3_collects_text_and_infers_a_duration():
    segments = parse_json3(JSON3)
    assert [segment.text for segment in segments] == ["Hello world", "Second line"]
    assert segments[0].start == 0.0
    assert segments[0].end == 2.0
    assert segments[1].end == 4.0


def test_parse_vtt_strips_markup_and_entities():
    segments = parse_vtt(VTT)
    assert [segment.text for segment in segments] == [
        "Hello world & friends",
        "Second line",
    ]
    assert segments[0].start == pytest.approx(0.4)
    assert segments[1].end == pytest.approx(6.5)


def test_choose_caption_prefers_manual_then_json3():
    info = {
        "subtitles": {"en": [{"ext": "vtt", "url": "manual-vtt"}]},
        "automatic_captions": {
            "en": [{"ext": "json3", "url": "auto-json3"}],
        },
    }
    caption = choose_caption(info, ["en"])
    assert caption is not None
    assert (caption.kind, caption.ext, caption.url) == ("manual", "vtt", "manual-vtt")

    info["subtitles"] = {}
    caption = choose_caption(info, ["en"])
    assert (caption.kind, caption.ext) == ("auto", "json3")


def test_choose_caption_respects_language_preference_and_variants():
    info = {
        "automatic_captions": {
            "de": [{"ext": "json3", "url": "de"}],
            "en-GB": [{"ext": "vtt", "url": "en-gb"}],
            "en": [{"ext": "json3", "url": "en"}],
        }
    }
    assert choose_caption(info, ["en"]).language == "en"
    assert choose_caption(info, ["fr", "de"]).language == "de"
    assert choose_caption(info, ["en-AU"]).language == "en"


def test_choose_caption_returns_none_without_tracks():
    assert choose_caption({}, ["en"]) is None


def _ok_response(data: bytes):
    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()
            return False

    return Response(data)


def test_download_caption_retries_and_honours_backoff(monkeypatch):
    calls = []
    sleeps = []

    def urlopen(request, timeout=0):
        calls.append(request.full_url)
        if len(calls) < 3:
            raise urllib.error.HTTPError(
                request.full_url, 429, "Too Many Requests", {}, None
            )
        return _ok_response(b'{"events": []}')

    monkeypatch.setattr(captions.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(captions.time, "sleep", lambda value: sleeps.append(value))

    data = download_caption(
        Caption("en", "auto", "json3", "https://example.test/caption"),
        retries=3, backoff_s=0.5,
    )
    assert data.startswith(b"{")
    assert len(calls) == 3
    assert sleeps == [0.5, 1.0]


def test_download_caption_surfaces_a_permanent_failure(monkeypatch):
    def urlopen(request, timeout=0):
        raise urllib.error.HTTPError(
            request.full_url, 404, "Not Found", {}, None
        )

    monkeypatch.setattr(captions.urllib.request, "urlopen", urlopen)
    with pytest.raises(CaptionError):
        download_caption(
            Caption("en", "auto", "json3", "https://example.test/caption"),
            retries=2, backoff_s=0,
        )


def test_fetch_transcript_api_is_optional(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("youtube_transcript_api"):
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    # A missing optional dependency must return None, not raise.
    assert fetch_transcript_api("vid", ["en"]) is None


def test_panel_params_encode_an_eleven_character_video_id():
    params = captions._panel_params("lWZ2Bz0tS-s")
    assert params == "qgkPCgtsV1oyQnowdFMtcxgB"


def test_panel_segments_read_modern_timeline_rows():
    data = {
        "content": [
            {
                "transcriptSegmentViewModel": {
                    "timestamp": "1:02",
                    "simpleText": "first <b>line</b>",
                }
            },
            {
                "transcriptSegmentViewModel": {
                    "timestamp": "1:07",
                    "simpleText": "second line",
                }
            },
            {
                "transcriptSegmentViewModel": {
                    "timestamp": "1:12",
                    "simpleText": "last line",
                }
            },
        ]
    }
    segments = captions._panel_segments(data)
    assert [segment.text for segment in segments] == [
        "first line", "second line", "last line",
    ]
    assert [segment.start for segment in segments] == [62.0, 67.0, 72.0]
    assert segments[0].end == 67.0
    assert segments[-1].end > segments[-1].start


def test_panel_is_used_when_timedtext_is_rate_limited(tmp_path, monkeypatch):
    from factories import add_playlist, make_library
    from ytop.config import Config
    from ytop.transcripts import fetch as fetch_mod
    from ytop.transcripts.captions import CaptionData, Segment
    from ytop.yt.metadata import write_json

    layout = make_library(tmp_path)
    add_playlist(layout, "PL1", "List", [("vid", "A lecture")])
    video_dir = layout.video_meta_dir("PL1", "vid")
    write_json(video_dir / "raw.json", {
        "id": "vid",
        "automatic_captions": {
            "en": [{"ext": "json3", "url": "https://example.test/en"}],
        },
    })

    def blocked(*args, **kwargs):
        raise RuntimeError("blocked")

    def rate_limited(*args, **kwargs):
        raise CaptionError("HTTP Error 429")

    monkeypatch.setattr(fetch_mod, "fetch_transcript_api", blocked)
    monkeypatch.setattr(fetch_mod, "download_caption", rate_limited)
    monkeypatch.setattr(
        fetch_mod, "fetch_panel_api",
        lambda *args, **kwargs: CaptionData(
            "en", "auto", "panel",
            [Segment(0.0, 2.0, "panel transcript")], b"{}",
        ),
    )
    result = fetch_mod.fetch_video_transcript(
        video_dir,
        playlist_id="PL1",
        config=Config(),
        layout=layout,
        retries=0,
        backoff_s=0,
    )
    assert result.status == "fetched"
    assert result.segments == 1
    assert result.raw_file == "subtitles/en.auto.panel"
    assert (video_dir / "transcript.json").is_file()


def test_preferred_language_beats_a_manual_track_in_another_language():
    info = {
        "subtitles": {"es": [{"ext": "vtt", "url": "spanish-manual"}]},
        "automatic_captions": {
            "en": [{"ext": "json3", "url": "english-auto"}],
        },
    }
    caption = choose_caption(info, ["en"])
    assert caption is not None
    assert (caption.language, caption.kind, caption.url) == (
        "en", "auto", "english-auto",
    )
    # If English is not available at all, the manual Spanish track is still
    # the best fallback.
    caption = choose_caption({"subtitles": info["subtitles"]}, ["en"])
    assert caption is not None
    assert (caption.language, caption.kind) == ("es", "manual")


def _fake_transcript_api(monkeypatch):
    import sys
    import types

    built = []

    class FakeApi:
        def __init__(self, proxy_config=None):
            built.append(proxy_config)

        def list(self, video_id):
            return []

    module = types.ModuleType("youtube_transcript_api")
    module.YouTubeTranscriptApi = FakeApi
    monkeypatch.setitem(sys.modules, "youtube_transcript_api", module)
    return built


def test_transcript_api_uses_active_http_proxy(monkeypatch):
    from youtube_transcript_api.proxies import GenericProxyConfig

    from ytop.yt.proxies import using_proxy

    built = _fake_transcript_api(monkeypatch)
    with using_proxy("https://us:89"):
        assert fetch_transcript_api("vid", ["en"]) is None
    assert len(built) == 1
    assert built[0].http_url == built[0].https_url == "https://us:89"
    assert isinstance(built[0], GenericProxyConfig)
    built.clear()
    fetch_transcript_api("vid", ["en"])
    assert built == [None]


def test_transcript_api_is_skipped_for_a_socks_proxy(monkeypatch):
    from ytop.yt.proxies import using_proxy

    built = _fake_transcript_api(monkeypatch)
    with using_proxy("socks5h://us:1080"):
        assert fetch_transcript_api("vid", ["en"]) is None
    assert built == []
