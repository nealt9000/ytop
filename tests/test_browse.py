from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

from factories import add_big_library, add_playlist, library_set, make_library
from ytop.layout import read_library
from ytop.tags import TagStore
from ytop.web.browse import (
    CARD_LIMIT,
    build_home,
    build_videos,
    cards_for,
    search_library,
)
from ytop.web.library import Library, ProgressStore, TitleStore
from ytop.yt.metadata import read_json, write_json


def _lib(tmp_path: Path) -> tuple[Library, object]:
    layout = make_library(tmp_path)
    add_playlist(layout, "P1", "One", [("v1", "A"), ("v2", "B"), ("v3", "C")])
    add_playlist(layout, "P2", "Two", [("w1", "X"), ("w2", "Y")])
    return Library(library_set(layout)), layout


def test_continue_and_start_new_split(tmp_path: Path) -> None:
    lib, layout = _lib(tmp_path)
    ProgressStore(layout).update("v1", 60, duration=60, watched=True)
    ProgressStore(layout).update("v2", 30, duration=60)
    home = build_home(lib)
    assert [c.playlist_id for c in home.continue_watching] == ["P1"]
    assert home.new_keys == {"P2"}
    card = home.continue_watching[0]
    assert card.next_label == "Video 2 of 3"
    assert card.next_url and card.next_url.endswith("/v2")
    assert card.next_position == 30
    assert card.watched_count == 1
    assert len(home.all_playlists) == 2


def test_fully_watched_playlist_is_in_neither_list(tmp_path: Path) -> None:
    lib, layout = _lib(tmp_path)
    for vid in ("w1", "w2"):
        ProgressStore(layout).update(vid, 60, duration=60, watched=True)
    home = build_home(lib)
    assert [c.playlist_id for c in home.continue_watching] == []
    assert home.new_keys == {"P1"}


def test_continue_sorted_by_last_watched(tmp_path: Path) -> None:
    lib, layout = _lib(tmp_path)
    ProgressStore(layout).update("v1", 10, duration=60)
    ProgressStore(layout).update("w1", 10, duration=60)
    path = layout.progress_file
    doc = read_json(path)
    doc["videos"]["v1"]["updated_at"] = "2026-01-01T00:00:00+00:00"
    doc["videos"]["w1"]["updated_at"] = "2026-02-01T00:00:00+00:00"
    write_json(path, doc)
    home = build_home(lib)
    assert [c.playlist_id for c in home.continue_watching] == ["P2", "P1"]
    assert home.continue_watching[0].last_watched_at.startswith("2026-02")


def test_title_override_and_tag_edits_applied(tmp_path: Path) -> None:
    lib, layout = _lib(tmp_path)
    TitleStore(layout).set_playlist_title("P1", "Renamed")
    TagStore(layout).add("P1", "Baroque")
    TagStore(layout).set_category("P1", "Music")
    card = next(c for c in cards_for(lib) if c.playlist_id == "P1")
    assert card.title == "Renamed"
    assert "baroque" in [t.key for t in card.tags]
    assert card.category == "Music"
    assert card.thumb_url.startswith("/media/P1/thumb")


def test_summary_counts_shared_media_once(tmp_path: Path) -> None:
    from ytop.index import index_for

    lib, layout = _lib(tmp_path)
    # A local playlist whose video points at P1's media file.
    index_for(layout).ensure_fresh()
    media = layout.media_root / read_json(
        layout.video_meta_dir("P1", "v1") / "meta.json"
    )["files"]["media"]
    add_playlist(layout, "L1", "Local", [("v1", "A")], media=False, folder="Local")
    write_json(
        layout.video_meta_dir("L1", "v1") / "meta.json",
        {"id": "v1", "title": "A",
         "files": {"media": layout.relative_media(media)}},
    )
    summary = build_home(lib).libraries[0]
    rows = index_for(layout).playlists()
    media_total = sum(
        {v.media_ref: v.media_bytes for p in rows for v in p.videos if v.media_ref}.values()
    )
    meta_total = sum(p.meta_bytes for p in rows)
    assert media_total > 0
    assert summary.used_bytes == media_total + meta_total
    assert summary.playlist_count == 3
    assert summary.video_count == 5  # v1 is shared


def _two_libraries(tmp_path: Path):
    first = make_library(tmp_path / "a", "First")
    second = make_library(tmp_path / "b", "Second")
    add_playlist(first, "P1", "One", [("v1", "A")])
    add_playlist(second, "P1", "One", [("v1", "A")])
    libs = library_set(first)
    libs.add_folder(second.root, "Second")
    return Library(libs), first, second, libs


def test_ambiguous_playlist_keys_match_library(tmp_path: Path) -> None:
    lib, first, second, _ = _two_libraries(tmp_path)
    cards = [c for c in cards_for(lib) if c.playlist_id == "P1"]
    assert len(cards) == 2
    for card in cards:
        assert card.key == lib.key_for(card.library_id, "P1")
        resolved = lib.resolve_playlist(card.key)
        assert resolved is not None and resolved[0].id == card.library_id
    assert cards[0].key != cards[1].key
    only = cards_for(lib, read_library(second.root).id)
    assert {c.library_id for c in only} == {read_library(second.root).id}
    # Unknown id falls back to all libraries.
    assert len(cards_for(lib, "nope")) == len(cards_for(lib))


def test_unplugged_library_absent(tmp_path: Path) -> None:
    lib, _first, second, libs = _two_libraries(tmp_path)
    assert len(build_home(lib).libraries) == 2
    shutil.rmtree(second.root)
    libs.libraries(refresh=True)
    assert [s.label for s in build_home(lib).libraries] == ["First"]


def test_build_videos_by_tag_across_playlists(tmp_path: Path) -> None:
    layout = make_library(tmp_path)
    add_playlist(layout, "P1", "One", [("a1", "A"), ("a2", "B"), ("a3", "C")])
    add_playlist(layout, "P2", "Two", [("b1", "X"), ("b2", "Y")])
    for vid in ("a1", "a2", "a3"):
        path = layout.video_meta_dir("P1", vid) / "meta.json"
        meta = read_json(path) or {}
        meta["tags"] = ["Counterpoint"]
        write_json(path, meta)
    path = layout.video_meta_dir("P2", "b2") / "meta.json"
    meta = read_json(path) or {}
    meta["tags"] = ["counterpoint"]
    write_json(path, meta)
    lib = Library(library_set(layout))
    groups = build_videos(lib, tag="counterpoint")
    flat = {(card.title, v.video_id) for card, vids in groups for v in vids}
    assert ("Two", "b2") in flat
    assert [card.playlist_id for card, _ in groups] == ["P1", "P2"]
    assert build_videos(lib, category="Nope") == []


def test_home_5000_videos_under_300ms(tmp_path: Path) -> None:
    # Built inside the test (not a module fixture) so the autouse isolation
    # of library discovery applies and no real stick is scanned.
    layout = make_library(tmp_path)
    add_big_library(layout)
    big_library = Library(library_set(layout))
    build_home(big_library)
    start = time.perf_counter()
    home = build_home(big_library)
    elapsed = time.perf_counter() - start
    print(f"build_home 50x100: {elapsed:.3f}s")
    assert len(home.all_playlists) == 50
    assert elapsed < 0.300


def test_search_groups_by_playlist_with_tag_match_first(tmp_path: Path) -> None:
    layout = make_library(tmp_path)
    add_playlist(layout, "P1", "Alpha Course", [("a1", "Fugue intro"), ("a2", "Fugue two")])
    add_playlist(layout, "P2", "Beta Course", [("b1", "Other")])
    add_playlist(layout, "P3", "Gamma", [("c1", "Fugue notes")])
    TagStore(layout).add("P2", "Fugue forms")
    lib = Library(library_set(layout))
    groups = search_library(lib, "fugue")
    assert [g.card.title for g in groups][0] == "Beta Course"
    assert [t.key for t in groups[0].tag_matches] == ["fugue forms"]
    assert groups[0].video_hits == []
    assert {g.card.title for g in groups} == {"Alpha Course", "Beta Course", "Gamma"}
    alpha = next(g for g in groups if g.card.title == "Alpha Course")
    assert {h["video_id"] for h in alpha.video_hits} == {"a1", "a2"}
    assert alpha.video_hits[0]["url"] == f"/watch/{alpha.card.key}/{alpha.video_hits[0]['video_id']}"
    assert "<mark>Fugue</mark>" in alpha.video_hits[0]["html"]
    # A playlist-title match leads too.
    assert search_library(lib, "gamma")[0].card.title == "Gamma"
    scoped = search_library(lib, "fugue", playlist=alpha.card.key)
    assert [g.card.title for g in scoped] == ["Alpha Course"]


def test_search_finds_custom_title(tmp_path: Path) -> None:
    layout = make_library(tmp_path)
    add_playlist(layout, "P1", "One", [("v1", "Untitled clip"), ("v2", "Other")])
    lib = Library(library_set(layout))
    store = TitleStore(layout)
    store.set_video_title("v1", "Zebra migration")
    store.set_playlist_title("P1", "Savannah")
    groups = search_library(lib, "zebra")
    assert [h["video_id"] for h in groups[0].video_hits] == ["v1"]
    assert "<mark>Zebra</mark>" in groups[0].video_hits[0]["html"]
    assert [g.card.title for g in search_library(lib, "savannah")] == ["Savannah"]
    assert search_library(lib, "") == []


def test_search_5000_videos_under_500ms(tmp_path: Path) -> None:
    layout = make_library(tmp_path)
    add_big_library(layout)
    lib = Library(library_set(layout))
    search_library(lib, "lecture")
    start = time.perf_counter()
    groups = search_library(lib, "lecture")
    elapsed = time.perf_counter() - start
    print(f"search_library 50x100: {elapsed:.3f}s")
    assert groups
    assert elapsed < 0.500


def test_search_playlist_title_match_sorts_first_even_when_video_title_matches(
    tmp_path: Path,
) -> None:
    layout = make_library(tmp_path)
    add_playlist(layout, "P1", "Aaa", [("a1", "Lecture one"), ("a2", "Lecture two")])
    add_playlist(layout, "P2", "Lecture series", [("b1", "Lecture 1")])
    lib = Library(library_set(layout))
    groups = search_library(lib, "lecture")
    assert groups[0].card.title == "Lecture series"
    assert groups[0].playlist_matched
    assert [h["video_id"] for h in groups[0].video_hits] == ["b1"]


def test_search_many_hits_in_one_playlist_do_not_crowd_out_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ytop.web.browse as browse
    from ytop.transcripts.captions import Segment
    from ytop.transcripts.store import index_video_dir, write_transcript

    layout = make_library(tmp_path)
    # Stronger matches (the word twice, short titles) all in one playlist.
    add_playlist(layout, "P1", "Big", [(f"a{n:02d}", "Fugue fugue") for n in range(30)])
    add_playlist(layout, "P2", "Small", [("b1", "A long talk that mentions a fugue once")])
    for pid, vid, text in (
        *(("P1", f"a{n:02d}", "fugue fugue fugue") for n in range(30)),
        ("P2", "b1", "and then, much later, a fugue appears in this talk"),
    ):
        vdir = layout.video_meta_dir(pid, vid)
        write_transcript(vdir, video_id=vid, title=vid, language="en", kind="auto",
                         segments=[Segment(0.0, 4.0, text)], raw_file=None)
        index_video_dir(layout, vdir, playlist_id=pid, title=vid)
    monkeypatch.setattr(browse, "META_FETCH", 5)
    monkeypatch.setattr(browse, "TRANSCRIPT_FETCH", 5)
    groups = search_library(Library(library_set(layout)), "fugue")
    small = next(g for g in groups if g.card.playlist_id == "P2")
    assert [h["video_id"] for h in small.video_hits] == ["b1"]
    assert [h["video_id"] for h in small.transcript_hits] == ["b1"]


def test_search_ranks_are_normalised_per_library(tmp_path: Path) -> None:
    from ytop.transcripts.captions import Segment
    from ytop.transcripts.store import index_video_dir, search, write_transcript

    layouts = []
    # The same matching line, in an index where the word is rare and in one
    # where every line has it.
    for name, others in (("a", ["sonata", "rondo", "minuet"]), ("b", ["fugue"] * 3)):
        layout = make_library(tmp_path / name)
        add_playlist(layout, "P1", "One", [("v1", "V")])
        vdir = layout.video_meta_dir("P1", "v1")
        lines = ["a fugue in four voices", *others]
        write_transcript(vdir, video_id="v1", title="V", language="en", kind="auto",
                         segments=[Segment(n * 4.0, n * 4.0 + 4, text)
                                   for n, text in enumerate(lines)],
                         raw_file=None)
        index_video_dir(layout, vdir, playlist_id="P1", title="V")
        layouts.append(layout)
    def best(hits: list[dict]) -> dict[str, float]:
        out: dict[str, float] = {}
        for hit in hits:
            out[hit["library_id"]] = min(out.get(hit["library_id"], 0.0), hit["rank"])
        return out

    raw = best(search(layouts, "fugue", 10))
    assert len(set(raw.values())) == 2  # each index scores its own way
    assert set(best(search(layouts, "fugue", 10, normalize=True)).values()) == {-1.0}


def test_continue_resumes_the_video_left_part_way(tmp_path: Path) -> None:
    layout = make_library(tmp_path)
    add_playlist(layout, "P1", "One", [(f"v{n}", f"V{n}") for n in range(1, 7)])
    lib = Library(library_set(layout))
    ProgressStore(layout).update("v5", 300, duration=600)
    card = build_home(lib).continue_watching[0]
    assert card.next_label == "Video 5 of 6"
    assert card.next_url and card.next_url.endswith("/v5")
    assert card.next_position == 300


def test_continue_picks_the_most_recently_watched_partial(tmp_path: Path) -> None:
    layout = make_library(tmp_path)
    add_playlist(layout, "P1", "One", [(f"v{n}", f"V{n}") for n in range(1, 5)])
    write_json(layout.progress_file, {"schema": 1, "videos": {
        "v1": {"position": 0, "watched": True, "updated_at": "2026-01-01T00:00:00Z"},
        "v2": {"position": 40, "watched": False, "updated_at": "2026-01-03T00:00:00Z"},
        "v4": {"position": 90, "watched": False, "updated_at": "2026-01-02T00:00:00Z"},
    }})
    card = cards_for(Library(library_set(layout)))[0]
    assert card.next_label == "Video 2 of 4"
    assert card.next_position == 40


def test_continue_falls_back_to_first_unwatched(tmp_path: Path) -> None:
    layout = make_library(tmp_path)
    add_playlist(layout, "P1", "One", [(f"v{n}", f"V{n}") for n in range(1, 4)])
    ProgressStore(layout).update("v1", 60, duration=60, watched=True)
    card = cards_for(Library(library_set(layout)))[0]
    assert card.next_label == "Video 2 of 3"
    assert card.next_position == 0


def test_search_hit_for_missing_video_has_no_watch_link(tmp_path: Path) -> None:
    layout = make_library(tmp_path)
    add_playlist(layout, "P1", "One", [("v1", "Zebra stripes")], media=False)
    add_playlist(layout, "P2", "Two", [("w1", "Zebra crossing")])
    groups = {g.card.playlist_id: g for g in search_library(Library(library_set(layout)), "zebra")}
    (missing,) = groups["P1"].video_hits
    assert missing["url"] is None and missing["available"] is False
    (present,) = groups["P2"].video_hits
    assert present["url"] == "/watch/P2/w1" and present["available"] is True


def _set_rendition(layout, playlist_id: str, video_id: str, height: int, codec: str) -> None:
    path = layout.video_meta_dir(playlist_id, video_id) / "meta.json"
    meta = read_json(path)
    meta["format"] = {"height": height, "video_codec": codec,
                      "audio_codec": "opus", "audio_bitrate": 128}
    write_json(path, meta)


def test_home_cards_show_quality_and_transcripts(tmp_path: Path) -> None:
    """#7: the cards carry the quality summary and transcript count again."""
    lib, layout = _lib(tmp_path)
    _set_rendition(layout, "P1", "v1", 720, "vp09")
    _set_rendition(layout, "P1", "v2", 1080, "vp09")
    _set_rendition(layout, "P1", "v3", 1080, "vp09")
    (layout.video_meta_dir("P1", "v1") / "transcript.json").write_text("{}")
    card = next(c for c in build_home(lib).all_playlists if c.playlist_id == "P1")
    assert card.quality_summary == "720p/1080p VP9 · Opus audio"
    assert card.transcript_count == 1


def test_new_playlist_list_shows_each_videos_quality(tmp_path: Path) -> None:
    from ytop.web.browse import library_videos

    lib, layout = _lib(tmp_path)
    _set_rendition(layout, "P1", "v1", 720, "avc1.4d401f")
    row = next(r for r in library_videos(lib) if r["id"] == "v1")
    assert row["quality_label"] == "720p H.264"
    assert row["audio_detail"] == "Opus 128k"


def test_home_lists_every_library_when_one_selected(tmp_path: Path) -> None:
    lib, _first, second, _ = _two_libraries(tmp_path)
    second_id = read_library(second.root).id
    home = build_home(lib, second_id)
    assert [s.label for s in home.libraries] == ["First", "Second"]
    assert home.header.id == second_id
    assert {c.library_id for c in home.all_playlists} == {second_id}


def test_home_header_combines_libraries(tmp_path: Path) -> None:
    lib, *_ = _two_libraries(tmp_path)
    home = build_home(lib)
    assert home.header.id == "" and home.header.label == "All libraries"
    assert home.header.playlist_count == sum(s.playlist_count for s in home.libraries) == 2
    assert home.header.used_bytes == sum(s.used_bytes for s in home.libraries)


def test_home_header_single_library_is_that_library(tmp_path: Path) -> None:
    lib, layout = _lib(tmp_path)
    home = build_home(lib)
    assert home.header.id == read_library(layout.root).id
    assert len(home.libraries) == 1


def test_home_loads_each_library_once(tmp_path: Path, monkeypatch) -> None:
    import ytop.web.browse as browse

    lib, _first, second, _ = _two_libraries(tmp_path)
    calls = []
    real = browse._load
    monkeypatch.setattr(
        browse, "_load", lambda l, ref: calls.append(ref.id) or real(l, ref)
    )
    build_home(lib, read_library(second.root).id)
    assert len(calls) == 2


def test_new_keys_are_recent_unstarted(tmp_path: Path) -> None:
    lib, layout = _lib(tmp_path)
    ProgressStore(layout).update("v2", 30, duration=60)
    assert build_home(lib).new_keys == {"P2"}
    for n in range(CARD_LIMIT + 2):
        add_playlist(layout, f"N{n}", f"New {n}", [(f"n{n}", "X")])
    assert len(build_home(lib).new_keys) == CARD_LIMIT


def test_category_counts_order(tmp_path: Path) -> None:
    lib, layout = _lib(tmp_path)
    add_playlist(layout, "P3", "Three", [("x1", "X")])
    store = TagStore(layout)
    store.set_category("P1", "Music")
    store.set_category("P2", "Cooking")
    store.set_category("P3", "Cooking")
    assert build_home(lib).category_counts == [("Cooking", 2), ("Music", 1)]


def test_combined_free_space_counts_each_disk_once(tmp_path: Path) -> None:
    # Both libraries sit on the same filesystem (tmp_path): one pool of free
    # space, not two.
    lib, *_ = _two_libraries(tmp_path)
    home = build_home(lib)
    frees = {s.free_bytes for s in home.libraries}
    assert len(home.libraries) == 2 and all(frees)
    assert home.header.free_bytes == max(frees)
