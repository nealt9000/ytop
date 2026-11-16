import pytest

from factories import add_playlist, library_set, make_library
from ytop.libraries import _probe
from ytop.tags import (
    UNCATEGORIZED,
    Tag,
    TagInput,
    TagStore,
    apply_edits,
    derive_tags,
    effective_category,
    is_junk,
    normalize_tag,
    playlist_category,
    video_tags,
)
from ytop.web.library import Library
from ytop.yt.metadata import CorruptDocumentError


def test_normalize_tag():
    assert normalize_tag("  #Counter_Point!! ") == "counter point"
    assert normalize_tag("ＭＩＴ") == "mit"
    assert normalize_tag("!!!") == ""


def test_normalize_drops_symbols_but_keeps_plus():
    assert normalize_tag("c++") == "c++"
    assert normalize_tag("😀 music") == "music"
    assert normalize_tag("a<b>c") == "abc"
    assert normalize_tag("＜b＞") == "b"


def test_junk_tags_dropped():
    assert is_junk("1234", "") and is_junk("x", "") and is_junk("youtube", "")
    assert is_junk("a" * 33, "") and is_junk("one two three four five", "")
    assert is_junk("mit opencourseware", "mit opencourseware")
    assert is_junk("mit", "mit opencourseware")
    assert not is_junk("counterpoint", "mit opencourseware")
    inputs = [TagInput("P1", "t", "", "", (("<script>x</script>", "😀", "a" * 500),))]
    assert all("<" not in t.label and len(t.key) <= 32 for t in derive_tags(inputs)["P1"])


def test_derive_prefers_distinctive_tags():
    music = TagInput("P1", "MIT 21M.383 Counterpoint", "", "MIT OpenCourseWare",
                     (("Music", "Counterpoint", "lecture"),) * 4)
    physics = TagInput("P2", "Physics I", "", "MIT OpenCourseWare",
                       (("Physics", "lecture"),) * 4)
    tags = derive_tags([music, physics])
    keys = [t.key for t in tags["P1"]]
    assert keys.index("counterpoint") < keys.index("lecture")
    assert "mit opencourseware" not in keys


def test_derive_merges_plurals_and_picks_common_label():
    a = TagInput("P1", "", "", "", (("Fugues",), ("fugue",), ("Fugue",)))
    assert derive_tags([a])["P1"] == [Tag("fugue", "Fugue")]


def test_title_phrase_counts_as_full_share():
    a = TagInput("P1", "", "", "", (("baroque",), ("baroque",)))
    b = TagInput("P2", "Baroque keyboard works", "", "", ((), (), (), ()))
    assert Tag("baroque", "baroque") in derive_tags([a, b])["P2"]


def test_min_share_and_min_videos_for_large_playlists():
    videos = (("rare",), ("common",), ("common",), ("common",), ("common",))
    a = TagInput("P1", "", "", "", videos)
    assert [t.key for t in derive_tags([a])["P1"]] == ["common"]
    one_of_five = TagInput("P2", "", "", "", (("lonely",), (), (), (), ()))
    assert derive_tags([one_of_five])["P2"] == []


def test_max_tags():
    a = TagInput("P1", "", "", "", (("aa", "bb", "cc", "dd", "ee", "ff", "gg"),))
    assert [t.key for t in derive_tags([a])["P1"]] == ["aa", "bb", "cc", "dd", "ee"]


def test_playlist_category():
    assert playlist_category([["Music"], ["Education"], ["Music"]]) == "Music"
    assert playlist_category([["B"], ["A"]]) == "A"
    assert playlist_category([[], []]) == UNCATEGORIZED


def test_add_remove_round_trip(tmp_path):
    store = TagStore(make_library(tmp_path))
    store.add("P1", "Fugue")
    store.remove("P1", "fugue")
    row = store.read().playlists["P1"]
    assert row.add == [] and row.remove == ["fugue"]


def test_rename_merges_into_existing_tag(tmp_path):
    store = TagStore(make_library(tmp_path))
    store.rename("fugues-x", "fugue")
    derived = [Tag("fugues-x", "Fugues X"), Tag("fugue", "Fugue")]
    assert apply_edits("P1", derived, store.read()) == [Tag("fugue", "fugue")]


def test_hidden_tag_removed_everywhere(tmp_path):
    store = TagStore(make_library(tmp_path))
    store.hide("lecture")
    edits = store.read()
    lecture = Tag("lecture", "Lecture")
    assert apply_edits("P1", [lecture, Tag("math", "Math")], edits) == [Tag("math", "Math")]
    assert video_tags([Tag("math", "Math")], ["Lecture"], {"lecture": lecture}, edits) == [
        Tag("math", "Math")
    ]
    store.unhide("lecture")
    assert store.read().hidden == set()


def test_edits_survive_rederivation(tmp_path):
    store = TagStore(make_library(tmp_path))
    store.add("P1", "Fugue")
    store.remove("P1", "baroque")
    edits = store.read()
    first = apply_edits("P1", [Tag("baroque", "Baroque"), Tag("bach", "Bach")], edits)
    second = apply_edits("P1", [Tag("baroque", "Baroque"), Tag("organ", "Organ")], edits)
    assert [t.key for t in first] == ["bach", "fugue"]
    assert [t.key for t in second] == ["organ", "fugue"]


def test_category_override(tmp_path):
    store = TagStore(make_library(tmp_path))
    store.set_category("P1", "Music")
    assert effective_category("P1", "Education", store.read()) == "Music"
    store.set_category("P1", "")
    assert effective_category("P1", "Education", store.read()) == "Education"


def test_label_is_cleaned_and_capped(tmp_path):
    store = TagStore(make_library(tmp_path))
    store.add("P1", "  Very   " + "long " * 12)
    (label,) = store.read().playlists["P1"].add
    assert len(label) <= 32 and "  " not in label


def test_damaged_tags_file_reads_empty_and_refuses_writes(tmp_path):
    layout = make_library(tmp_path)
    layout.tags_file.write_bytes(b"{oops")
    store = TagStore(layout)
    assert store.read().playlists == {}
    with pytest.raises(CorruptDocumentError):
        store.add("P1", "Fugue")
    assert layout.tags_file.read_bytes() == b"{oops"


def test_moved_playlist_carries_tag_edits(tmp_path):
    src = make_library(tmp_path / "a")
    dst = make_library(tmp_path / "b")
    add_playlist(src, "P1", "One", [("v1", "A")])
    TagStore(src).add("P1", "Fugue")
    refs = [_probe(layout.root, "launch") for layout in (src, dst)]
    assert refs[0] is not None and refs[1] is not None
    Library(library_set(src)).merge_state_into(refs[0], refs[1], "P1", ["v1"])
    assert TagStore(dst).read().playlists["P1"].add == ["Fugue"]


def test_remove_by_renamed_key(tmp_path):
    store = TagStore(make_library(tmp_path))
    store.rename("fugues-x", "fugue")
    store.remove("P1", "fugue")
    assert apply_edits("P1", [Tag("fugues-x", "Fugues X")], store.read()) == []


def test_case_only_rename(tmp_path):
    store = TagStore(make_library(tmp_path))
    store.rename("mit", "MIT")
    assert apply_edits("P1", [Tag("mit", "mit")], store.read()) == [Tag("mit", "MIT")]
    store.rename("mit", "mit")
    assert store.read().renames == {}


def test_unusable_rename_clears_and_keys_are_normalized(tmp_path):
    store = TagStore(make_library(tmp_path))
    store.rename("a b", "C D")
    store.rename("a b", "!!!")
    assert store.read().renames == {}
    store.hide("Lecture")
    assert store.read().hidden == {"lecture"}
    store.unhide("LECTURE")
    store.hide("!!!")
    assert store.read().hidden == set()
    store.remove("P1", "!!!")
    assert "P1" not in store.read().playlists


def test_pipe_is_dropped_from_keys_and_labels(tmp_path):
    # The browse page joins tag keys with "|" (data-tags) and splits on it.
    assert normalize_tag("rock|pop") == "rockpop"
    assert "|" not in normalize_tag("|a|b|")
    store = TagStore(make_library(tmp_path))
    store.add("P1", "Rock|Pop")
    assert store.read().playlists["P1"].add == ["RockPop"]


def test_rename_back_undoes_the_merge(tmp_path):
    store = TagStore(make_library(tmp_path))
    derived = {"P1": [Tag("fugue", "Fugue")], "P2": [Tag("counterpoint", "Counterpoint")]}
    store.rename("fugue", "Counterpoint")
    edits = store.read()
    assert {pid: apply_edits(pid, tags, edits) for pid, tags in derived.items()} == {
        "P1": [Tag("counterpoint", "Counterpoint")],
        "P2": [Tag("counterpoint", "Counterpoint")],
    }
    # Renaming the merged tag back: everything is Fugue, nothing swapped.
    store.rename("counterpoint", "Fugue")
    edits = store.read()
    assert edits.renames == {"counterpoint": "Fugue"}
    assert {pid: apply_edits(pid, tags, edits) for pid, tags in derived.items()} == {
        "P1": [Tag("fugue", "Fugue")],
        "P2": [Tag("fugue", "Fugue")],
    }


def test_rename_cycle_through_a_chain_is_broken(tmp_path):
    store = TagStore(make_library(tmp_path))
    store.rename("a1", "B1")
    store.rename("b1", "C1")
    store.rename("c1", "A1")
    edits = store.read()
    assert edits.renames == {"b1": "C1", "c1": "A1"}
    for key in ("a1", "b1", "c1"):
        assert apply_edits("P", [Tag(key, key)], edits)[0].key == "a1"


def test_derived_label_is_trimmed_like_its_key():
    long_label = "#" + "Word " * 5 + "!!"
    a = TagInput("P1", "", "", "", (("#Physics!!", long_label),) * 2)
    tags = {t.key: t.label for t in derive_tags([a])["P1"]}
    assert tags["physics"] == "Physics"
    assert all(len(label) <= 32 for label in tags.values())
    assert all(not label.startswith("#") for label in tags.values())


def test_control_characters_part_words():
    assert normalize_tag("linear\nalgebra") == "linear algebra"
    assert normalize_tag("linear\talgebra") == "linear algebra"
    a = TagInput("P1", "", "", "", (("Linear\nAlgebra",),) * 2)
    assert derive_tags([a])["P1"] == [Tag("linear algebra", "Linear Algebra")]


def test_title_plural_counts_for_the_merged_tag():
    a = TagInput("P1", "", "", "", (("fugue",), ("fugue",)))
    b = TagInput("P2", "Bach fugues", "", "", ((), (), (), ()))
    assert Tag("fugue", "fugue") in derive_tags([a, b])["P2"]
    # A word that only looks like a plural is not merged ("bas" is not "bass").
    c = TagInput("P3", "", "", "", (("bas",), ("bas",)))
    d = TagInput("P4", "Electric bass", "", "", ((), (), (), ()))
    assert "bas" not in {t.key for t in derive_tags([c, d])["P4"]}


def test_equal_scores_and_label_counts_break_ties_alphabetically():
    a = TagInput("P1", "", "", "", (("Zeta", "alpha", "Mid"), ("zeta", "Alpha", "mid")))
    tags = derive_tags([a])["P1"]
    assert [t.key for t in tags] == ["alpha", "mid", "zeta"]
    # Each spelling was seen once: the alphabetically first wins.
    assert [t.label for t in tags] == ["Alpha", "Mid", "Zeta"]


def test_rename_normalizes_its_key(tmp_path):
    store = TagStore(make_library(tmp_path))
    store.rename("  #Fugue! ", "Counterpoint")
    assert store.read().renames == {"fugue": "Counterpoint"}
    store.rename("!!!", "Anything")
    assert store.read().renames == {"fugue": "Counterpoint"}


def test_unknown_playlist_row_fields_survive_a_rewrite(tmp_path):
    import json

    layout = make_library(tmp_path)
    layout.tags_file.write_text(json.dumps({
        "schema": 1,
        "playlists": {
            "P1": {"add": ["Fugue"], "remove": [], "pinned": True},
            "P2": {"add": [], "remove": [], "note": "keep me"},
        },
    }))
    TagStore(layout).add("P1", "Baroque")
    rows = json.loads(layout.tags_file.read_text())["playlists"]
    assert rows["P1"]["pinned"] is True
    assert rows["P1"]["add"] == ["Fugue", "Baroque"]
    assert rows["P2"] == {"note": "keep me", "add": [], "remove": []}


def test_empty_label_undoes_a_rename(tmp_path):
    store = TagStore(make_library(tmp_path))
    store.rename("fugue", "Counterpoint")
    store.rename("fugue", "")
    edits = store.read()
    assert edits.renames == {}
    assert apply_edits("P1", [Tag("fugue", "Fugue")], edits) == [Tag("fugue", "Fugue")]
