from __future__ import annotations

import shutil
from pathlib import Path

from factories import library_set, make_library
from ytop.config import Config
from ytop.layout import LibraryLayout
from ytop.web.jobs import JobManager
from ytop.web.library import Library, ProgressStore, TitleStore

FIXTURES = Path(__file__).parent / "fixtures" / "schema1"
PLAYLIST_ID = "PLSCHEMA1000000000000000000001"


def _copy_library(tmp_path: Path) -> LibraryLayout:
    """Schema-1 documents placed where the portable layout keeps them."""
    layout = make_library(tmp_path, "Schema")
    video_dir = layout.video_meta_dir(PLAYLIST_ID, "vid1")
    video_dir.mkdir(parents=True)
    shutil.copy(FIXTURES / "playlist.json", layout.playlist_meta_dir(PLAYLIST_ID) / "playlist.json")
    shutil.copy(FIXTURES / "video-meta.json", video_dir / "meta.json")
    shutil.copy(FIXTURES / "progress.json", layout.progress_file)
    shutil.copy(FIXTURES / "titles.json", layout.titles_file)
    shutil.copy(FIXTURES / "jobs.json", layout.jobs_file)
    return layout


def test_schema_1_documents_remain_readable(tmp_path: Path):
    layout = _copy_library(tmp_path)
    library = Library(library_set(layout))

    playlists = library.playlists()
    assert [playlist.id for playlist in playlists] == [PLAYLIST_ID]
    assert playlists[0].title == "My schema playlist"
    view = library.playlist_view(PLAYLIST_ID)
    assert view is not None
    assert view.video_count == 1
    assert view.preserve_audio is True
    assert view.entries[0].title == "My schema video"

    progress = ProgressStore(layout).get("vid1")
    assert progress.position == 12.5
    assert progress.watched is False

    titles = TitleStore(layout).read()
    assert titles["videos"]["vid1"] == "My schema video"

    manager = JobManager(library)
    try:
        job = manager.get("job1")
        assert job is not None
        assert job.status == "done"
        assert job.downloaded == 1
    finally:
        manager.close()
