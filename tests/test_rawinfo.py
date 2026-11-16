from __future__ import annotations

import copy
import json

from ytop.transcripts.captions import choose_caption
from ytop.yt.formats import audio_is_auto_dubbed, audio_is_original, select_format
from ytop.yt.rawinfo import trim_raw_info

_SIG = "&sig=" + "A" * 180 + "&lsig=" + "B" * 120


def _stream_url(itag: str, xtags: str = "") -> str:
    tags = f"&xtags={xtags}" if xtags else ""
    return (
        "https://rr3---sn-abc.googlevideo.com/videoplayback?expire=1763300000"
        f"&ei=xyz&ip=203.0.113.9&id=o-ABC&itag={itag}{tags}&source=youtube"
        "&mime=video%2Fwebm&dur=600.0&lmt=1700000000000000" + _SIG
    )


def realistic_info() -> dict:
    """Shaped like a YouTube yt-dlp info dict (sizes as in real ones)."""
    formats: list[dict] = []
    for n in range(4):  # storyboards: big fragment lists
        formats.append({
            "format_id": f"sb{n}", "format_note": "storyboard", "ext": "mhtml",
            "protocol": "mhtml", "vcodec": "none", "acodec": "none",
            "width": 160, "height": 90, "url": _stream_url(f"sb{n}"),
            "fragments": [{"url": _stream_url(f"sb{n}-{i}"), "duration": 10.0}
                          for i in range(60)],
        })
    for itag, note, xtags, abr in (
        ("249", "English original, low", "acont%3Doriginal%3Alang%3Den", 50),
        ("251", "English original, medium", "acont%3Doriginal%3Alang%3Den", 130),
        ("251-1", "German, medium", "acont%3Ddubbed-auto%3Alang%3Dde", 128),
    ):
        formats.append({
            "format_id": itag, "format_note": note, "ext": "webm",
            "protocol": "https", "vcodec": "none", "acodec": "opus",
            "abr": abr, "tbr": abr, "asr": 48000, "audio_channels": 2,
            "filesize": abr * 75_000, "language": itag[-1:] == "1" and "de" or "en",
            "url": _stream_url(itag, xtags),
            "http_headers": {"User-Agent": "Mozilla/5.0 " + "x" * 100,
                             "Accept": "text/html", "Sec-Fetch-Mode": "navigate"},
            "downloader_options": {"http_chunk_size": 10485760},
        })
    for itag, height in (("394", 144), ("395", 240), ("396", 360),
                         ("397", 480), ("398", 720), ("399", 1080)):
        for protocol in ("https", "m3u8_native"):
            formats.append({
                "format_id": itag if protocol == "https" else f"{itag}-hls",
                "format_note": f"{height}p", "ext": "mp4",
                "protocol": protocol, "vcodec": "av01.0.05M.08", "acodec": "none",
                "width": height * 16 // 9, "height": height, "fps": 30,
                "dynamic_range": "SDR", "tbr": height * 2.5,
                "filesize": height * 300_000 if protocol == "https" else None,
                "url": _stream_url(itag),
                "manifest_url": _stream_url("manifest") if protocol != "https" else None,
                "http_headers": {"User-Agent": "Mozilla/5.0 " + "x" * 100},
            })
    langs = [f"l{n:03d}" for n in range(150)] + ["en", "en-orig", "de"]
    auto = {
        lang: [{"ext": ext, "url": _stream_url("tt") + f"&lang={lang}&fmt={ext}",
                "name": f"{lang} (auto)"}
               for ext in ("json3", "srv1", "srv2", "srv3", "ttml", "srt", "vtt")]
        for lang in langs
    }
    subs = {"es": [{"ext": ext, "url": _stream_url("tt") + f"&fmt={ext}",
                    "name": "Spanish"} for ext in ("vtt", "ttml", "srv3")]}
    return {
        "id": "vid", "title": "A lecture", "duration": 600,
        "description": "intro\n0:00 Start", "channel": "Chan",
        "categories": ["Education"], "tags": ["math", "lecture"],
        "thumbnail": "https://i.ytimg.com/vi/vid/maxresdefault.jpg",
        "thumbnails": [{"url": f"https://i.ytimg.com/vi/vid/{n}.jpg",
                        "preference": -n, "id": str(n)} for n in range(40)],
        "heatmap": [{"start_time": i * 6.0, "end_time": i * 6.0 + 6,
                     "value": 0.5} for i in range(100)],
        "formats": formats,
        "requested_formats": [copy.deepcopy(formats[-2]), copy.deepcopy(formats[5])],
        "subtitles": subs,
        "automatic_captions": auto,
        "http_headers": {"User-Agent": "Mozilla/5.0"},
        "url": _stream_url("top"),
    }


def _size(doc: dict) -> int:
    return len((json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode())


def test_trim_shrinks_a_realistic_info_dict() -> None:
    info = realistic_info()
    before, after = _size(info), _size(trim_raw_info(info))
    print(f"raw.json before {before} bytes, after {after} bytes")
    assert after * 5 < before


def test_trim_does_not_change_the_input() -> None:
    info = realistic_info()
    snapshot = copy.deepcopy(info)
    trim_raw_info(info)
    assert info == snapshot


def test_trim_keeps_what_readers_use() -> None:
    info = realistic_info()
    trimmed = trim_raw_info(info)
    # Index backfill.
    assert trimmed["categories"] == ["Education"]
    assert trimmed["tags"] == ["math", "lecture"]
    # Small top-level metadata survives untouched.
    for key in ("id", "title", "duration", "description", "channel", "thumbnail"):
        assert trimmed[key] == info[key]
    # Every caption language is still offered, and the transcript fallback
    # picks the very same track for any language preference.
    assert set(trimmed["automatic_captions"]) == set(info["automatic_captions"])
    assert set(trimmed["subtitles"]) == set(info["subtitles"])
    for wanted in (["en"], ["de", "en"], ["es"], ["l042"], ["fr"], []):
        assert choose_caption(trimmed, wanted) == choose_caption(info, wanted)


def test_trim_drops_bulk_but_keeps_a_format_summary() -> None:
    info = realistic_info()
    trimmed = trim_raw_info(info)
    for key in ("thumbnails", "heatmap", "http_headers", "url"):
        assert key not in trimmed
    assert len(trimmed["formats"]) == len(info["formats"])
    assert len(trimmed["requested_formats"]) == 2
    text = json.dumps(trimmed)
    for bulk in ("fragments", "http_headers", "manifest_url", "downloader_options"):
        assert bulk not in text
    streams = json.dumps([trimmed["formats"], trimmed["requested_formats"]])
    assert "googlevideo.com" not in streams
    by_id = {f["format_id"]: f for f in trimmed["formats"]}
    assert by_id["399"]["height"] == 1080 and by_id["399"]["vcodec"].startswith("av01")
    assert by_id["251"]["abr"] == 130 and by_id["251"]["filesize"] == 130 * 75_000
    # Quality selection and the audio-track labels still work on the summary.
    original = select_format(info, "en")
    again = select_format(trimmed, "en")
    assert again.format_spec == original.format_spec
    assert audio_is_original(by_id["251"]) and not audio_is_auto_dubbed(by_id["251"])
    assert audio_is_auto_dubbed(by_id["251-1"])


def test_trim_is_idempotent_and_tolerates_odd_shapes() -> None:
    trimmed = trim_raw_info(realistic_info())
    assert trim_raw_info(trimmed) == trimmed
    odd = {"id": "x", "formats": None, "subtitles": [], "automatic_captions": {
        "en": "nope", "de": [None, {"ext": "vtt"}], "fr": [{"ext": "srv1", "url": "u"}],
    }}
    assert trim_raw_info(odd) == {
        "id": "x", "formats": None, "subtitles": [], "automatic_captions": {},
    }
