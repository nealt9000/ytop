from __future__ import annotations

from ytop.yt.formats import FormatError, select_format


def video(fid, codec, height, fps=15, tbr=300, **extra):
    data = {
        "format_id": fid,
        "vcodec": codec,
        "acodec": "none",
        "height": height,
        "width": height * 4 // 3,
        "fps": fps,
        "tbr": tbr,
        "ext": "mp4" if codec.startswith("avc1") else "webm",
    }
    data.update(extra)
    return data


def audio(fid, codec="opus", abr=50, language=None, note=None, ext="webm"):
    return {
        "format_id": fid,
        "vcodec": "none",
        "acodec": codec,
        "abr": abr,
        "language": language,
        "format_note": note,
        "ext": ext,
        "filesize": int(abr * 1000 / 8 * 3754),
    }


def test_geb_selects_av1_and_original_english_opus():
    info = {
        "id": "lWZ2Bz0tS-s",
        "duration": 3754,
        "formats": [
            video("395", "av01.0.00M.08", 240, tbr=300),
            video("242", "vp9", 240, tbr=410),
            video("134", "avc1.4D400C", 240, tbr=440),
            video("394", "av01.0.00M.08", 144, tbr=170),
            audio("249-0", abr=49.3, language="pt-BR", note="Portuguese (BR), low"),
            audio("249-9", abr=50.0, language="en-US",
                  note="English (US) original (default), low"),
            audio("251-9", abr=97.6, language="en-US",
                  note="English (US) original (default), medium"),
        ],
    }
    chosen = select_format(info, audio_language="en")
    assert chosen.video["format_id"] == "395"
    assert chosen.video_family == "av01"
    assert chosen.audio["format_id"] == "249-9"
    assert chosen.container == "webm"
    assert chosen.format_spec == "395+249-9"


def test_native_maximum_beats_codec_preference():
    info = {
        "formats": [
            video("720-avc1", "avc1.64001F", 720, tbr=500),
            video("240-av01", "av01.0.00M.08", 240, tbr=300),
            audio("140", codec="mp4a.40.2", abr=129, language="en", ext="m4a"),
            audio("139", codec="mp4a.40.5", abr=49, language="en", ext="m4a"),
        ]
    }
    chosen = select_format(info, audio_language="en")
    assert chosen.video["height"] == 720
    assert chosen.video_family == "avc1"
    assert chosen.audio["format_id"] == "139"
    assert chosen.container == "mp4"


def test_codec_preference_within_max_height():
    info = {
        "formats": [
            video("v", "vp9", 720, fps=30),
            video("a", "av01.0.08M.08", 720, fps=30),
            video("h", "avc1.64001F", 720, fps=30),
        ]
    }
    assert select_format(info).video["format_id"] == "a"


def test_video_only_selection_has_no_audio():
    info = {"formats": [video("395", "av01", 240)]}
    chosen = select_format(info)
    assert chosen.audio is None
    assert chosen.container is None
    assert chosen.format_spec == "395"


def test_drm_storyboard_and_audio_only_are_ignored():
    info = {
        "formats": [
            video("drm", "av01", 720, has_drm=True),
            video("story", "none", 720, ext="mhtml", format_note="storyboard"),
            video("395", "av01", 240),
            audio("249", language="en", note="English original"),
        ]
    }
    chosen = select_format(info)
    assert chosen.video["format_id"] == "395"


def test_audio_language_falls_back_when_preferred_is_absent():
    info = {
        "formats": [
            video("395", "av01", 240),
            audio("249-pt", language="pt-BR", note="Portuguese"),
        ]
    }
    chosen = select_format(info, audio_language="en")
    assert chosen.audio["format_id"] == "249-pt"


def test_original_audio_wins_and_preferred_language_becomes_alternate():
    info = {
        "formats": [
            video("243", "vp9", 240),
            audio("249-0", language="en-US", note="English (US), low"),
            audio("251-0", language="en-US", note="English (US), medium"),
            audio("249-2", language="es-US",
                  note="Spanish (US) original (default), low"),
            audio("251-2", language="es-US",
                  note="Spanish (US) original (default), medium"),
        ]
    }
    chosen = select_format(info, audio_language="en")
    assert chosen.audio["format_id"] == "249-2"
    assert chosen.audio["language"] == "es-US"
    assert [fmt["format_id"] for fmt in chosen.alternates] == ["249-0"]
    # The alternate bytes are part of the pre-download estimate.
    assert chosen.estimated_bytes == sum(
        fmt.get("filesize", 0)
        for fmt in [chosen.video, chosen.audio, *chosen.alternates]
    )


def test_original_audio_has_no_alternate_when_it_is_already_english():
    info = {
        "formats": [
            video("395", "av01", 240),
            audio("249-9", language="en-US",
                  note="English (US) original (default), low"),
            audio("249-1", language="pt-BR", note="Portuguese (BR), low"),
        ]
    }
    chosen = select_format(info, audio_language="en")
    assert chosen.audio["format_id"] == "249-9"
    assert chosen.alternates == []


def test_hls_duplicate_without_size_does_not_beat_direct_stream():
    info = {
        "formats": [
            video("230", "avc1.4D400C", 240, tbr=307.8,
                  protocol="m3u8_native", format_note=None),
            video("134", "avc1.4D400C", 240, tbr=101.4,
                  protocol="https", format_note="240p",
                  filesize=57_852_139),
            audio("139-16", codec="mp4a.40.5", abr=48.8, language="en-US",
                  note="English (US) original (default), low", ext="m4a"),
        ]
    }
    chosen = select_format(info, audio_language="en")
    assert chosen.video["format_id"] == "134"
    assert chosen.estimated_bytes >= 57_852_139


def test_no_video_raises():
    try:
        select_format({"formats": []})
    except FormatError:
        return
    raise AssertionError("expected FormatError")


def test_max_height_cap_keeps_the_highest_rendition_below_it():
    info = {
        "formats": [
            video("720", "av01", 720),
            video("480", "av01", 480),
            video("360", "vp9", 360),
            video("240", "av01", 240),
        ]
    }
    assert select_format(info, max_height=360).video["height"] == 360
    assert select_format(info, max_height=240).video["height"] == 240
    assert select_format(info, max_height=0).video["height"] == 720


def test_efficient_allocation_spends_the_playlist_budget_on_upgrades():
    from types import SimpleNamespace

    from ytop.yt.formats import allocate_efficient

    def item(hours, formats):
        info = {"formats": formats}
        return SimpleNamespace(
            info=info, duration=hours * 3600, selection=None
        )

    a = item(1, [
        video("a240", "av01", 240, filesize=50_000_000),
        video("a360", "av01", 360, filesize=100_000_000),
    ])
    b = item(1, [
        video("b144", "avc1", 144, filesize=20_000_000),
        video("b240", "avc1", 240, filesize=60_000_000),
        video("b360", "avc1", 360, filesize=120_000_000),
    ])
    # 80 MB/h over two hours = 160 MB.  Base 240p is 110 MB; upgrading A to
    # 360p costs 50 MB, so it fits and B stays at the 240p legibility floor.
    total = allocate_efficient(
        [a, b], target_mb_per_hour=80, min_height=240
    )
    assert total == 160_000_000
    assert a.selection.video["format_id"] == "a360"
    assert b.selection.video["format_id"] == "b240"

    c = item(1, [
        video("c144", "avc1", 144, filesize=20_000_000),
        video("c240", "avc1", 240, filesize=60_000_000),
    ])
    d = item(1, [
        video("d144", "avc1", 144, filesize=20_000_000),
        video("d240", "avc1", 240, filesize=60_000_000),
    ])
    # The minimum legibility floor wins over an impossible budget.
    total = allocate_efficient(
        [c, d], target_mb_per_hour=10, min_height=240
    )
    assert total == 120_000_000
    assert c.selection.video["height"] == 240
    assert d.selection.video["height"] == 240


def test_audio_acont_marks_original_and_auto_dubs():
    from ytop.yt.formats import audio_is_auto_dubbed, audio_is_original

    original = audio("251-2", language="es-US", note="Spanish (US), medium")
    original["url"] = (
        "https://example.test/media?itag=251&xtags=acont%3Doriginal%3Alang%3Des-US"
    )
    dub = audio("249-0", language="en-US", note="English (US), low")
    dub["url"] = (
        "https://example.test/media?itag=249&xtags=acont%3Ddubbed-auto%3Alang%3Den-US"
    )
    assert audio_is_original(original) is True
    assert audio_is_auto_dubbed(original) is False
    assert audio_is_original(dub) is False
    assert audio_is_auto_dubbed(dub) is True


def test_original_acont_track_beats_a_preferred_language_dub():
    from ytop.yt.formats import audio_is_original

    original = audio("251-2", language="es-US", note="Spanish (US), medium")
    original["url"] = (
        "https://example.test/media?itag=251&xtags=acont%3Doriginal%3Alang%3Des-US"
    )
    dub = audio("249-0", language="en-US", note="English (US), low")
    dub["url"] = (
        "https://example.test/media?itag=249&xtags=acont%3Ddubbed-auto%3Alang%3Den-US"
    )
    info = {"formats": [video("243", "vp9", 240), dub, original]}
    chosen = select_format(info, audio_language="en")
    assert audio_is_original(chosen.audio)
    assert chosen.audio["format_id"] == "251-2"
    assert [fmt["format_id"] for fmt in chosen.alternates] == ["249-0"]


def test_preserve_audio_selects_the_best_original_track():
    info = {
        "duration": 100,
        "formats": [
            video("395", "av01.0.00M.08", 240, tbr=300),
            audio("249-9", abr=50.0, language="en-US",
                  note="English (US) original (default), low"),
            audio("251-9", abr=160.0, language="en-US",
                  note="English (US) original (default), medium"),
            audio("140-9", codec="mp4a.40.2", abr=128.0, language="en-US",
                  note="English (US) original (default), medium", ext="m4a"),
        ],
    }
    efficient = select_format(info, audio_language="en")
    assert efficient.audio["format_id"] == "249-9"

    music = select_format(info, audio_language="en", preserve_audio=True)
    assert music.audio["format_id"] == "251-9"
    assert music.container == "webm"
    assert music.estimated_bytes > efficient.estimated_bytes


def test_preserve_audio_keeps_an_h264_video_in_mp4():
    info = {
        "duration": 100,
        "formats": [
            video("134", "avc1.4D400C", 240, tbr=440),
            audio("140-9", codec="mp4a.40.2", abr=128.0, language="en-US",
                  note="English (US) original (default), medium", ext="m4a"),
            audio("251-9", abr=160.0, language="en-US",
                  note="English (US) original (default), medium"),
        ],
    }
    music = select_format(info, audio_language="en", preserve_audio=True)
    assert music.audio["format_id"] == "140-9"
    assert music.container == "mp4"
