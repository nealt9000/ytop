"""Migration from the pre-portable layout (``playlists/<Title> [id]/...``)."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

import pytest

import ytop.migrate as migrate
from factories import add_playlist, make_old_playlist
from ytop.cli import main
from ytop.layout import MEDIA_DIR_NAME, LibraryLayout, read_library
from ytop.migrate import plan_migration, run_migration
from ytop.web.library import TitleStore
from ytop.yt.metadata import read_json, write_json

FIXTURES = Path(__file__).parent / "fixtures" / "schema1"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(migrate, "_host_config_path",
                        lambda: home / ".config" / "ytop" / "config.toml")
    return home


def _tree_digest(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not root.exists():
        return result
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_file():
            stat = path.stat()
            result[rel] = (
                hashlib.sha256(path.read_bytes()).hexdigest()
                + f":{stat.st_mtime_ns}:{stat.st_ino}"
            )
        else:
            result[rel] = "dir"
    return result


def _old_library(tmp_path: Path) -> Path:
    old = tmp_path / "old"
    make_old_playlist(
        old, "PLA", "Lectures",
        videos=(("a1", "Intro"), ("a2", "Second"), ("a3", "Third")),
        ext="webm",
        options={"preserve_audio": True},
        audio_for=("a1",),
        transcript_for=("a1",),
        partial_for=("a3",),
    )
    make_old_playlist(
        old, "un-grouped", "Un-grouped",
        videos=(("u1", "Loose clip"),),
        kind="ungrouped",
    )
    write_json(old / "progress.json", {
        "schema": 1,
        "videos": {
            "a1": {"position": 120, "duration": 600, "watched": False,
                   "updated_at": "2026-01-01T00:00:00+00:00"},
            "zz": {"position": 5, "duration": 50, "watched": False,
                   "updated_at": "2026-01-01T00:00:00+00:00"},
        },
    })
    write_json(old / "titles.json", {
        "schema": 1,
        "playlists": {"PLA": "My Lectures"},
        "videos": {"a2": "My Second"},
    })
    write_json(old / "jobs.json", {
        "schema": 1,
        "jobs": [
            {"id": "jdone", "status": "done", "url": "u1", "location_id": "local"},
            {"id": "jrun", "status": "running", "url": "u2", "title": "Live",
             "location_id": "local", "location_label": "This computer",
             "current_id": "a2"},
        ],
    })
    (old / "ytop.sqlite3").write_bytes(b"old index")
    return old


def _layout(target: Path) -> LibraryLayout:
    return LibraryLayout(target)


def _media_of(layout: LibraryLayout, playlist_id: str, video_id: str) -> Path | None:
    meta = read_json(layout.video_meta_dir(playlist_id, video_id) / "meta.json")
    assert meta is not None
    return layout.resolve_media((meta.get("files") or {}).get("media"))


def test_dry_run_changes_nothing(tmp_path, capsys):
    old = _old_library(tmp_path)
    target = tmp_path / "new"
    before = _tree_digest(tmp_path)

    plan = plan_migration(old, target)
    assert [p.id for p in plan.playlists] == ["PLA", "un-grouped"]
    assert plan.bytes_to_move > 0 and plan.bytes_to_copy == 0
    assert [p.name for p in plan.dropped_partials] == ["video.f249-17.webm.part"]

    assert main(["migrate", str(old), str(target), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Lectures" in out and "partial" in out.lower()

    assert _tree_digest(tmp_path) == before
    assert not target.exists()


def test_migrate_preserves_state(tmp_path):
    old = _old_library(tmp_path)
    original_audio = (old / "playlists" / "Lectures [PLA]" / "videos" / "a1"
                      / "audio" / "251-1.webm").read_bytes()
    target = tmp_path / "new"

    report = run_migration(plan_migration(old, target), target)

    layout = _layout(target)
    info = read_library(target)
    assert info is not None and info.label == "new"
    media = target / MEDIA_DIR_NAME

    # Numbered files under the custom playlist title, custom video title used.
    folder = media / "My Lectures"
    assert sorted(p.name for p in folder.iterdir()) == [
        "01 - Intro.en.srt", "01 - Intro.webm", "02 - My Second.webm",
        "folder.jpg",
    ]
    assert _media_of(layout, "PLA", "a1") == folder / "01 - Intro.webm"
    assert (folder / "01 - Intro.webm").read_bytes() == b"media-a1" * 100

    # Meta dir: copied metadata, files.media instead of files.video.
    vdir = layout.video_meta_dir("PLA", "a1")
    for name in ("meta.json", "raw.json", "thumb.jpg", "transcript.json"):
        assert (vdir / name).is_file(), name
    assert (vdir / "subtitles" / "a1.en.vtt").is_file()
    assert (vdir / "audio" / "251-1.webm").read_bytes() == original_audio
    meta = read_json(vdir / "meta.json")
    assert meta is not None and "video" not in meta["files"]
    assert meta["files"]["thumbnail"] == "thumb.jpg"

    playlist = read_json(layout.playlist_meta_dir("PLA") / "playlist.json")
    assert playlist is not None
    assert playlist["options"] == {"preserve_audio": True}
    assert (layout.playlist_meta_dir("PLA") / "playlist-thumb.jpg").is_file()
    for entry in playlist["entries"]:
        assert "path" not in entry and "video" not in entry
    assert [e["id"] for e in playlist["entries"]] == ["a1", "a2", "a3"]

    # The .part video has no media and is shown as not downloaded.
    assert _media_of(layout, "PLA", "a3") is None
    a3 = next(e for e in playlist["entries"] if e["id"] == "a3")
    assert a3["available"] is False
    assert not list(media.rglob("*.part"))

    # Un-grouped keeps its folder and is not numbered.
    assert (media / "Un-grouped" / "Loose clip.mp4").is_file()

    # State carried: titles and progress (only videos that were migrated).
    titles = read_json(layout.titles_file)
    assert titles is not None
    assert titles["playlists"] == {"PLA": "My Lectures"}
    assert titles["videos"] == {"a2": "My Second"}
    progress = read_json(layout.progress_file)
    assert progress is not None
    assert progress["videos"]["a1"]["position"] == 120
    assert "zz" not in progress["videos"]

    # Jobs: finished dropped, active failed with the migration message.
    jobs = read_json(layout.jobs_file)
    assert jobs is not None
    rows = {row["id"]: row for row in jobs["jobs"]}
    assert set(rows) == {"jrun"}
    assert rows["jrun"]["status"] == "failed"
    assert rows["jrun"]["error"] == "Interrupted by migration — re-add to resume"
    assert rows["jrun"]["library_id"] == info.id
    assert "location_id" not in rows["jrun"]

    # Index rebuilt from transcripts; the old index is not carried.
    assert layout.index_db.is_file()
    assert report.indexed == 1

    # Source: marked, old JSON kept, moved media absent, partial kept.
    old_pl = old / "playlists" / "Lectures [PLA]"
    assert "new" in (old / "MIGRATED.txt").read_text(encoding="utf-8")
    assert (old_pl / "playlist.json").is_file()
    assert (old_pl / "videos" / "a1" / "meta.json").is_file()
    assert not (old_pl / "videos" / "a1" / "video.webm").exists()
    assert (old_pl / "videos" / "a3" / "video.f249-17.webm.part").is_file()
    assert (old / "progress.json").is_file() and (old / "ytop.sqlite3").is_file()


def test_unmerged_format_stream_is_not_media(tmp_path):
    """Real leftovers: meta points at ``video.f395.mp4`` (one stream of an
    unfinished merge) beside ``video.f249-17.webm.part``."""
    old = tmp_path / "old"
    playlist_dir = make_old_playlist(
        old, "PLA", "Lectures", videos=(("v1", "Done"), ("v2", "Unmerged")),
    )
    video_dir = playlist_dir / "videos" / "v2"
    (video_dir / "video.mp4").rename(video_dir / "video.f395.mp4")
    (video_dir / "video.f249-17.webm.part").write_bytes(b"partial" * 10)
    meta = read_json(video_dir / "meta.json")
    assert meta is not None
    meta["files"]["video"] = "video.f395.mp4"
    write_json(video_dir / "meta.json", meta)
    target = tmp_path / "new"

    plan = plan_migration(old, target)
    assert sorted(p.name for p in plan.dropped_partials) == [
        "video.f249-17.webm.part", "video.f395.mp4",
    ]
    report = run_migration(plan, target)

    layout = _layout(target)
    assert _media_of(layout, "PLA", "v2") is None
    assert report.without_media == ["PLA/v2"]
    assert (video_dir / "video.f395.mp4").is_file()
    assert not (layout.video_meta_dir("PLA", "v2") / "video.f395.mp4").exists()
    assert sorted(p.name for p in (layout.media_root / "Lectures").iterdir()) == [
        "01 - Done.mp4", "folder.jpg",
    ]


def test_hardlinked_duplicate_becomes_reference(tmp_path):
    old = tmp_path / "old"
    make_old_playlist(old, "PLA", "First", videos=(("v1", "Shared"),))
    second = make_old_playlist(old, "PLB", "Second",
                               videos=(("v1", "Shared"), ("v2", "Own")))
    first_media = old / "playlists" / "First [PLA]" / "videos" / "v1" / "video.mp4"
    dup = second / "videos" / "v1" / "video.mp4"
    dup.unlink()
    os.link(first_media, dup)
    target = tmp_path / "new"

    plan = plan_migration(old, target)
    single = len(b"media-v1" * 100) + len(b"media-v2" * 100)
    assert plan.bytes_to_move == single

    report = run_migration(plan, target)
    layout = _layout(target)
    owner = _media_of(layout, "PLA", "v1")
    assert owner is not None and owner.parent.name == "First"
    meta_b = read_json(layout.video_meta_dir("PLB", "v1") / "meta.json")
    assert meta_b is not None
    assert meta_b["files"]["media"] == layout.relative_media(owner)
    assert sorted(p.name for p in (layout.media_root / "Second").iterdir()) == [
        "02 - Own.mp4", "folder.jpg",
    ]
    assert report.references == 1
    files = [p for p in layout.media_root.rglob("*") if p.is_file()]
    assert len([p for p in files if p.suffix == ".mp4"]) == 2


def test_resume_after_interruption(tmp_path, monkeypatch):
    old = _old_library(tmp_path)
    target = tmp_path / "new"
    real = migrate._migrate_playlist
    calls: list[str] = []

    def failing(ctx, playlist):
        calls.append(playlist.id)
        if len(calls) == 2:
            raise RuntimeError("stick yanked")
        return real(ctx, playlist)

    monkeypatch.setattr(migrate, "_migrate_playlist", failing)
    with pytest.raises(RuntimeError):
        run_migration(plan_migration(old, target), target)
    journal = read_json(target / ".ytop" / "data" / "migration-journal.json")
    assert journal is not None
    assert [(row["id"], row["state"]) for row in journal["playlists"].values()] == [
        ("PLA", "done"), ("un-grouped", "started"),
    ]

    monkeypatch.setattr(migrate, "_migrate_playlist", real)
    plan = plan_migration(old, target)
    assert [p.migrated for p in plan.playlists] == [True, False]
    report = run_migration(plan, target)
    assert report.migrated == ["un-grouped"]
    assert report.skipped == ["PLA"]

    layout = _layout(target)
    files = sorted(
        p.relative_to(layout.media_root).as_posix()
        for p in layout.media_root.rglob("*") if p.is_file()
    )
    assert files == [
        "My Lectures/01 - Intro.en.srt", "My Lectures/01 - Intro.webm",
        "My Lectures/02 - My Second.webm", "My Lectures/folder.jpg",
        "Un-grouped/Loose clip.mp4", "Un-grouped/folder.jpg",
    ]
    jobs = read_json(layout.jobs_file)
    assert jobs is not None and [row["id"] for row in jobs["jobs"]] == ["jrun"]


def test_resume_mid_playlist_does_not_duplicate(tmp_path, monkeypatch):
    old = _old_library(tmp_path)
    target = tmp_path / "new"
    real = migrate._place_media
    calls: list[str] = []

    def failing(ctx, source, dest):
        calls.append(source.name)
        if len(calls) == 2:
            raise OSError("stick yanked")
        return real(ctx, source, dest)

    monkeypatch.setattr(migrate, "_place_media", failing)
    with pytest.raises(OSError):
        run_migration(plan_migration(old, target), target)
    monkeypatch.setattr(migrate, "_place_media", real)
    run_migration(plan_migration(old, target), target)

    layout = _layout(target)
    folder = layout.media_root / "My Lectures"
    assert sorted(p.name for p in folder.iterdir()) == [
        "01 - Intro.en.srt", "01 - Intro.webm", "02 - My Second.webm",
        "folder.jpg",
    ]
    assert not list(layout.root.rglob(".ytoppublish-*"))


def test_storage_json_extra_location_included(tmp_path):
    old = _old_library(tmp_path)
    extra = tmp_path / "usb"
    make_old_playlist(extra, "PLX", "On The Stick", videos=(("x1", "Extra"),))
    gone = tmp_path / "unplugged"
    write_json(old / "storage.json", {
        "schema": 1, "default": "local",
        "locations": [
            {"id": "loc-1", "path": str(extra), "label": "USB"},
            {"id": "loc-2", "path": str(gone), "label": "Gone"},
        ],
    })
    write_json(old / "progress.json", {"schema": 1, "videos": {
        "x1": {"position": 33, "duration": 100, "watched": False,
               "updated_at": "2026-01-01T00:00:00+00:00"},
    }})
    target = tmp_path / "new"

    plan = plan_migration(old, target)
    assert [p.id for p in plan.playlists] == ["PLA", "un-grouped", "PLX"]
    assert any(str(gone) in warning for warning in plan.warnings)

    run_migration(plan, target)
    layout = _layout(target)
    assert (layout.media_root / "On The Stick" / "01 - Extra.mp4").is_file()
    progress = read_json(layout.progress_file)
    assert progress is not None and progress["videos"]["x1"]["position"] == 33


def test_same_device_moves_media(tmp_path):
    old = tmp_path / "old"
    make_old_playlist(old, "PLA", "Lectures", videos=(("v1", "One"),))
    source = old / "playlists" / "Lectures [PLA]" / "videos" / "v1" / "video.mp4"
    inode = source.stat().st_ino
    target = tmp_path / "new"

    run_migration(plan_migration(old, target), target)
    moved = target / MEDIA_DIR_NAME / "Lectures" / "01 - One.mp4"
    assert moved.stat().st_ino == inode
    assert not source.exists()


def test_cross_device_copies_and_keeps_source(tmp_path, monkeypatch):
    old = tmp_path / "old"
    make_old_playlist(old, "PLA", "Lectures", videos=(("v1", "One"),),
                      audio_for=("v1",))
    source = old / "playlists" / "Lectures [PLA]" / "videos" / "v1" / "video.mp4"
    audio = source.parent / "audio" / "251-1.webm"
    monkeypatch.setattr(migrate, "_same_device", lambda a, b: False)
    target = tmp_path / "new"

    plan = plan_migration(old, target)
    assert plan.bytes_to_move == 0 and plan.bytes_to_copy > 0
    run_migration(plan, target)

    copied = target / MEDIA_DIR_NAME / "Lectures" / "01 - One.mp4"
    assert copied.read_bytes() == source.read_bytes()
    assert copied.stat().st_ino != source.stat().st_ino
    assert audio.is_file()
    layout = _layout(target)
    assert (layout.video_meta_dir("PLA", "v1") / "audio" / "251-1.webm").is_file()


def test_host_cookies_warned_not_copied(tmp_path, _isolated_home):
    config = _isolated_home / ".config" / "ytop" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('cookies = "/secret/cookies.txt"\n', encoding="utf-8")
    old = _old_library(tmp_path)
    target = tmp_path / "new"

    plan = plan_migration(old, target)
    assert any("cookies" in warning.lower() for warning in plan.warnings)
    run_migration(plan, target)
    for path in target.rglob("*"):
        if path.is_file() and path.suffix in {".json", ".toml", ".txt"}:
            assert "/secret/cookies.txt" not in path.read_text(encoding="utf-8")


def test_schema1_fixture_migrates(tmp_path):
    old = tmp_path / "old"
    playlist_dir = old / "playlists" / "Schema One [PLSCHEMA1000000000000000000001]"
    video_dir = playlist_dir / "videos" / "vid1"
    video_dir.mkdir(parents=True)
    shutil.copy(FIXTURES / "playlist.json", playlist_dir / "playlist.json")
    shutil.copy(FIXTURES / "video-meta.json", video_dir / "meta.json")
    (video_dir / "video.mp4").write_bytes(b"x" * 2048)
    for name in ("progress.json", "titles.json", "jobs.json", "storage.json"):
        shutil.copy(FIXTURES / name, old / name)
    target = tmp_path / "new"

    run_migration(plan_migration(old, target), target)
    layout = _layout(target)
    playlist_id = "PLSCHEMA1000000000000000000001"
    media = _media_of(layout, playlist_id, "vid1")
    assert media == layout.media_root / "My schema playlist" / "01 - My schema video.mp4"
    progress = read_json(layout.progress_file)
    assert progress is not None and progress["videos"]["vid1"]["position"] == 12.5
    jobs = read_json(layout.jobs_file)
    assert jobs is None or jobs.get("jobs") == []


def test_migration_leaves_a_built_library_index(tmp_path):
    import contextlib
    import sqlite3

    old = _old_library(tmp_path)
    for video_id in ("a1", "a2", "a3"):
        path = old / "playlists" / "Lectures [PLA]" / "videos" / video_id / "meta.json"
        meta = read_json(path) or {}
        meta["tags"] = ["Linear Algebra"]
        write_json(path, meta)
    target = tmp_path / "new"
    run_migration(plan_migration(old, target), target)

    # Read straight from the file: the first page must not have to build it.
    with contextlib.closing(sqlite3.connect(_layout(target).index_db)) as conn:
        playlists = {r[0] for r in conn.execute("SELECT playlist_id FROM idx_playlists")}
        dirty = conn.execute(
            "SELECT value FROM idx_meta WHERE key = 'tags_dirty'"
        ).fetchone()
        tags = {r[0] for r in conn.execute(
            "SELECT key FROM idx_tags WHERE playlist_id = 'PLA'"
        )}
    assert playlists == {"PLA", "un-grouped"}
    assert dirty == ("0",)
    assert "linear algebra" in tags


def test_cli_migrate_runs(tmp_path, capsys):
    old = _old_library(tmp_path)
    target = tmp_path / "new"
    assert main(["migrate", str(old), str(target)]) == 0
    assert read_library(target) is not None
    assert "migrated" in capsys.readouterr().out.lower()


def test_refuses_target_inside_old_tree(tmp_path):
    old = _old_library(tmp_path)
    with pytest.raises(ValueError):
        plan_migration(old, old)
    with pytest.raises(ValueError):
        plan_migration(old, old / "playlists" / "new")


def test_existing_target_playlist_is_left_untouched(tmp_path):
    old = _old_library(tmp_path)
    target = tmp_path / "new"
    from ytop.layout import init_library

    init_library(target, "new")
    layout = _layout(target)
    add_playlist(layout, "PLA", "Mine", [("a9", "Nine")])
    TitleStore(layout).set_playlist_title("PLA", "My own title")
    meta_dir = layout.playlist_meta_dir("PLA")
    before = _tree_digest(meta_dir)
    before_titles = read_json(layout.titles_file)
    before_media = _tree_digest(layout.media_root / "Mine")

    plan = plan_migration(old, target)
    pla = next(p for p in plan.playlists if p.id == "PLA")
    assert "already has playlist PLA" in pla.skip_reason
    report = run_migration(plan, target)

    assert report.unmigrated == ["PLA"]
    assert "PLA" not in report.migrated
    assert any("already has playlist PLA" in w for w in report.warnings)
    assert _tree_digest(meta_dir) == before
    assert _tree_digest(layout.media_root / "Mine") == before_media
    titles = read_json(layout.titles_file)
    assert titles is not None and before_titles is not None
    assert titles["playlists"] == before_titles["playlists"]
    assert "a1" not in (read_json(layout.progress_file) or {}).get("videos", {})
    old_pl = old / "playlists" / "Lectures [PLA]"
    assert (old_pl / "videos" / "a1" / "video.webm").is_file()
    marker = (old / "MIGRATED.txt").read_text(encoding="utf-8")
    assert str(old_pl) in marker
    assert "NOT everything was migrated" in marker
    assert "Everything was migrated" not in marker
    # Un-grouped (not in the target before) was still migrated.
    assert report.migrated == ["un-grouped"]


def test_two_old_libraries_with_ungrouped(tmp_path):
    first = tmp_path / "old1"
    second = tmp_path / "old2"
    make_old_playlist(first, "un-grouped", "Un-grouped",
                      videos=(("u1", "One"),), kind="ungrouped")
    make_old_playlist(second, "un-grouped", "Un-grouped",
                      videos=(("u2", "Two"),), kind="ungrouped")
    target = tmp_path / "new"

    run_migration(plan_migration(first, target), target)
    marker = (first / "MIGRATED.txt").read_text(encoding="utf-8")
    assert "Everything was migrated" in marker

    plan = plan_migration(second, target)
    assert plan.playlists[0].migrated is False
    assert plan.playlists[0].skip_reason
    report = run_migration(plan, target)
    assert report.skipped == [] and report.unmigrated == ["un-grouped"]
    assert (second / "playlists" / "Un-grouped [un-grouped]" / "videos" / "u2"
            / "video.mp4").is_file()
    marker = (second / "MIGRATED.txt").read_text(encoding="utf-8")
    assert "NOT everything was migrated" in marker
    assert "Un-grouped [un-grouped]" in marker
    layout = _layout(target)
    assert sorted(p.name for p in (layout.media_root / "Un-grouped").iterdir()) == [
        "One.mp4", "folder.jpg",
    ]


def test_marker_lists_partials(tmp_path):
    old = _old_library(tmp_path)
    target = tmp_path / "new"
    report = run_migration(plan_migration(old, target), target)
    marker = (old / "MIGRATED.txt").read_text(encoding="utf-8")
    assert "video.f249-17.webm.part" in marker
    assert "NOT everything was migrated" in marker
    assert len(report.left_behind) == 1


def test_hardlink_within_playlist_becomes_reference(tmp_path):
    from ytop.publish import sync_playlist_files

    old = tmp_path / "old"
    playlist_dir = make_old_playlist(
        old, "PLA", "Lectures", videos=(("v1", "One"), ("v2", "Two")),
    )
    dup = playlist_dir / "videos" / "v2" / "video.mp4"
    dup.unlink()
    os.link(playlist_dir / "videos" / "v1" / "video.mp4", dup)
    target = tmp_path / "new"

    report = run_migration(plan_migration(old, target), target)
    layout = _layout(target)
    one = _media_of(layout, "PLA", "v1")
    assert one is not None and one.name == "01 - One.mp4"
    assert _media_of(layout, "PLA", "v2") == one
    assert report.references == 1 and report.copied == 0
    sync_playlist_files(layout, "PLA")  # stable: no flip-flop, no broken refs
    assert _media_of(layout, "PLA", "v1") == one == _media_of(layout, "PLA", "v2")
    assert sorted(p.name for p in (layout.media_root / "Lectures").iterdir()) == [
        "01 - One.mp4", "folder.jpg",
    ]
    assert "Everything was migrated" in (old / "MIGRATED.txt").read_text("utf-8")


def test_resume_replaces_truncated_placement(tmp_path, monkeypatch):
    old = tmp_path / "old"
    make_old_playlist(old, "PLA", "Lectures", videos=(("v1", "One"),))
    source = old / "playlists" / "Lectures [PLA]" / "videos" / "v1" / "video.mp4"
    monkeypatch.setattr(migrate, "_same_device", lambda a, b: False)

    def boom(layout, playlist_id):
        raise OSError("stick yanked")

    monkeypatch.setattr(migrate, "sync_playlist_files", boom)
    target = tmp_path / "new"
    with pytest.raises(OSError):
        run_migration(plan_migration(old, target), target)
    layout = _layout(target)
    placed = _media_of(layout, "PLA", "v1")
    assert placed is not None
    placed.write_bytes(source.read_bytes()[:10])  # an interrupted copy

    monkeypatch.undo()
    monkeypatch.setattr(migrate, "_same_device", lambda a, b: False)
    monkeypatch.setattr(migrate, "_host_config_path", lambda: tmp_path / "none.toml")
    run_migration(plan_migration(old, target), target)
    final = _media_of(layout, "PLA", "v1")
    assert final is not None and final.read_bytes() == source.read_bytes()
    aside = list((layout.cache_dir / "migrate" / "set-aside").iterdir())
    assert len(aside) == 1 and aside[0].stat().st_size == 10


def test_unreadable_playlist_folder_is_listed(tmp_path, capsys):
    old = tmp_path / "old"
    make_old_playlist(old, "PLA", "Fine", videos=(("v1", "One"),))
    broken = make_old_playlist(old, "PLB", "Broken", videos=(("b1", "Two"),))
    (broken / "playlist.json").write_text("{not json", encoding="utf-8")
    target = tmp_path / "new"

    plan = plan_migration(old, target)
    assert [p.id for p in plan.playlists] == ["PLA"]
    assert [path for path, _ in plan.unscanned] == [broken]
    assert main(["migrate", str(old), str(target), "--dry-run"]) == 0
    assert str(broken) in capsys.readouterr().out

    report = run_migration(plan, target)
    assert any(str(broken) in item for item in report.left_behind)
    marker = (old / "MIGRATED.txt").read_text(encoding="utf-8")
    assert str(broken) in marker
    assert "NOT everything was migrated" in marker
    assert "Remove this folder" not in marker
    assert (broken / "videos" / "b1" / "video.mp4").is_file()


def test_unconnected_storage_location_is_listed(tmp_path):
    old = tmp_path / "old"
    make_old_playlist(old, "PLA", "Fine", videos=(("v1", "One"),))
    gone = tmp_path / "unplugged"
    write_json(old / "storage.json", {
        "schema": 1, "default": "local",
        "locations": [{"id": "loc-2", "path": str(gone), "label": "Gone"}],
    })
    target = tmp_path / "new"

    plan = plan_migration(old, target)
    assert [path for path, _ in plan.unscanned] == [gone]
    report = run_migration(plan, target)
    assert any(str(gone) in item for item in report.left_behind)
    marker = (old / "MIGRATED.txt").read_text(encoding="utf-8")
    assert str(gone) in marker and "Gone" in marker
    assert "Remove this folder" not in marker


def test_user_playlist_titled_un_grouped_gets_its_own_folder(tmp_path):
    old = tmp_path / "old"
    make_old_playlist(old, "PLU", "Un-grouped", videos=(("p1", "Mine"),))
    make_old_playlist(old, "un-grouped", "Un-grouped",
                      videos=(("u1", "Loose"),), kind="ungrouped")
    target = tmp_path / "new"
    run_migration(plan_migration(old, target), target)
    layout = _layout(target)
    user = _media_of(layout, "PLU", "p1")
    loose = _media_of(layout, "un-grouped", "u1")
    assert user is not None and loose is not None
    assert user.parent != loose.parent
    assert loose.parent.name == "Un-grouped"


def test_stray_files_in_old_playlists_are_left_behind(tmp_path):
    old = tmp_path / "old"
    make_old_playlist(old, "PLA", "Fine", videos=(("v1", "One"),))
    stray = old / "playlists" / "notes.txt"
    stray.write_text("keep", encoding="utf-8")
    target = tmp_path / "new"
    report = run_migration(plan_migration(old, target), target)
    assert any(str(stray) in item for item in report.left_behind)
    assert stray.read_text(encoding="utf-8") == "keep"


def test_unreadable_storage_location_is_listed_not_raised(tmp_path, monkeypatch):
    old = tmp_path / "old"
    make_old_playlist(old, "PLA", "Fine", videos=(("v1", "One"),))
    extra = tmp_path / "extra"
    (extra / "playlists").mkdir(parents=True)
    write_json(old / "storage.json", {
        "schema": 1, "default": "local",
        "locations": [{"id": "loc-2", "path": str(extra), "label": "Extra"}],
    })
    real_iterdir = Path.iterdir

    def iterdir(self):
        if self == extra / "playlists":
            raise PermissionError(13, "Permission denied", str(self))
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    plan = plan_migration(old, tmp_path / "new")
    assert [p.id for p in plan.playlists] == ["PLA"]
    assert [path for path, _ in plan.unscanned] == [extra]


def test_run_migration_refuses_a_newer_schema_target(tmp_path):
    from ytop.layout import init_library

    old = tmp_path / "old"
    make_old_playlist(old, "PLA", "Fine", videos=(("v1", "One"),))
    target = tmp_path / "new"
    init_library(target, "New")
    marker = LibraryLayout(target).marker
    doc = read_json(marker)
    doc["schema"] = 99
    write_json(marker, doc)
    plan = plan_migration(old, target)
    with pytest.raises(ValueError, match="newer"):
        run_migration(plan, target)
    assert not LibraryLayout(target).playlist_meta_dir("PLA").exists()
