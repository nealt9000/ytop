"""Portable library layout: paths under one library root and its marker."""

from __future__ import annotations

import re
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from ytop.yt.metadata import read_json, write_json

SCHEMA_VERSION = 1
MEDIA_DIR_NAME = "YTOP Library"
UNGROUPED_ID = "un-grouped"
UNGROUPED_FOLDER = "Un-grouped"

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


def safe_id(value: str) -> str:
    """Reduce an identifier to ``[A-Za-z0-9_-]``, at most 64 characters.

    Raises ``ValueError`` if nothing usable remains, so an id can never
    resolve to its parent directory.
    """
    result = _UNSAFE.sub("_", value).strip("_")[:64].strip("_")
    if not result:
        raise ValueError(f"identifier {value!r} has no usable characters")
    return result


class CorruptMarkerError(OSError):
    """A library marker exists but cannot be read; never silently replaced."""


@dataclass(frozen=True)
class LibraryInfo:
    id: str
    schema: int
    label: str
    description: str
    created_at: str


@dataclass(frozen=True)
class LibraryLayout:
    root: Path

    @property
    def ytop_dir(self) -> Path:
        return self.root / ".ytop"

    @property
    def marker(self) -> Path:
        return self.ytop_dir / "library.json"

    @property
    def data_dir(self) -> Path:
        return self.ytop_dir / "data"

    @property
    def media_root(self) -> Path:
        return self.root / MEDIA_DIR_NAME

    @property
    def meta_root(self) -> Path:
        return self.data_dir / "playlists"

    @property
    def cache_dir(self) -> Path:
        return self.ytop_dir / "cache"

    @property
    def config_file(self) -> Path:
        return self.ytop_dir / "config.toml"

    @property
    def progress_file(self) -> Path:
        return self.data_dir / "progress.json"

    @property
    def titles_file(self) -> Path:
        return self.data_dir / "titles.json"

    @property
    def tags_file(self) -> Path:
        return self.data_dir / "tags.json"

    @property
    def jobs_file(self) -> Path:
        return self.data_dir / "jobs.json"

    @property
    def index_db(self) -> Path:
        return self.data_dir / "index.sqlite3"

    @property
    def rename_journal(self) -> Path:
        return self.data_dir / "rename-journal.json"

    def playlist_meta_dir(self, playlist_id: str) -> Path:
        return self.meta_root / safe_id(playlist_id)

    def video_meta_dir(self, playlist_id: str, video_id: str) -> Path:
        return self.playlist_meta_dir(playlist_id) / "videos" / safe_id(video_id)

    def partial_dir(self, video_id: str) -> Path:
        return self.cache_dir / "partial" / safe_id(video_id)

    def resolve_media(self, relative: str | None) -> Path | None:
        if not relative:
            return None
        base = self.media_root.resolve()
        candidate = (self.media_root / relative).resolve()
        if not candidate.is_relative_to(base) or not candidate.is_file():
            return None
        return self.media_root / relative

    def relative_media(self, path: Path) -> str:
        return Path(path).relative_to(self.media_root).as_posix()


def read_library(root: Path) -> LibraryInfo | None:
    data = read_json(LibraryLayout(Path(root)).marker)
    if not isinstance(data, dict):
        return None
    try:
        return LibraryInfo(
            id=str(data["id"]),
            schema=int(data["schema"]),
            label=str(data.get("label", "")),
            description=str(data.get("description", "")),
            created_at=str(data.get("created_at", "")),
        )
    except (KeyError, TypeError, ValueError):
        return None


def write_library_info(layout: LibraryLayout, info: LibraryInfo) -> None:
    write_json(layout.marker, asdict(info))


def _hide(path: Path) -> None:
    if sys.platform != "win32":
        return
    import ctypes

    ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x2)


def init_library(root: Path, label: str, description: str = "") -> LibraryInfo:
    """Create a library at ``root``; the marker is written last."""
    layout = LibraryLayout(Path(root))
    existing = read_library(layout.root)
    if existing is not None:
        return existing
    if layout.marker.exists() or layout.marker.is_symlink():
        # Re-iding would orphan every reference to this library (registry,
        # jobs, browser bookmarks); the owner has to repair the file.
        raise CorruptMarkerError(
            f"{layout.marker} exists but is not a valid library marker; "
            "repair or remove it first"
        )
    for directory in (
        layout.media_root,
        layout.data_dir,
        layout.meta_root,
        layout.cache_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    _hide(layout.ytop_dir)
    info = LibraryInfo(
        id=uuid.uuid4().hex,
        schema=SCHEMA_VERSION,
        label=label,
        description=description,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    write_library_info(layout, info)
    return info
