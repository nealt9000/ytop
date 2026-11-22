"""Read and manage the libraries' playlists for the web UI.

Each playlist lives in one portable library (see ``ytop.libraries``): its
metadata under ``.ytop/data/playlists/<id>/`` and its media under
``YTOP Library/<folder>/``.  Watch progress and title overrides are per
library, stored beside the playlists they describe, so a stick carries its own
state.  Reordering, removing an entry and creating a local playlist only edit
those documents (and rename media through the journalled publish engine);
they never re-encode media.
"""

from __future__ import annotations

import os
import shutil
import threading
import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from .. import fsinfo
from ..layout import UNGROUPED_FOLDER, UNGROUPED_ID, LibraryLayout, safe_id
from ..libraries import LibraryRef, LibrarySet
from ..publish import (
    PUBLISH_LOCK,
    iter_video_metas,
    publish_download,
    rename_folder,
    sync_playlist_files,
    transfer_media,
)
from ..transcripts.store import (
    read_transcript,
    segments_from_doc,
)
from ..transcripts.store import (
    remove_playlist as remove_playlist_index,
)
from ..transcripts.store import (
    remove_video as remove_video_index,
)
from ..yt.metadata import (
    PLAYLIST_THUMB_NAME,
    CorruptDocumentError,
    chapters_from_description,
    document_lock,
    find_media,
    human_bytes,
    now_iso,
    read_json,
    read_json_document,
    remove_media,
    write_json,
)
from .render import (
    description_html,
    format_date,
    format_duration,
    snippet,
    stream_url,
    thumb_url,
)

UNGROUPED_TITLE = "Un-grouped"

__all__ = [
    "UNGROUPED_ID",
    "UNGROUPED_TITLE",
    "Library",
    "PlaylistView",
    "ProgressStore",
    "TitleStore",
    "VideoView",
]

Record = tuple[LibraryRef, Path, dict]
Keyed = tuple[str, LibraryRef, Path, dict]


def playlist_key(playlist_id: str, library_id: str, ambiguous: bool) -> str:
    """The id routes use for a playlist: its own id, unless another online
    library holds the same id (every library has an Un-grouped)."""
    return f"{playlist_id}@{library_id[:8]}" if ambiguous else playlist_id


def _safe_relative(base: Path, relative: str | None) -> Path | None:
    if not relative:
        return None
    candidate = (Path(base) / relative).resolve()
    try:
        candidate.relative_to(Path(base).resolve())
    except ValueError:
        return None
    return candidate



def directory_bytes(path: Path) -> int:
    """Total size of regular files below ``path`` (no symlink traversal)."""
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            try:
                total += os.stat(os.path.join(root, name), follow_symlinks=False).st_size
            except OSError:
                continue
    return total


def codec_label(value) -> str:
    """Short, readable codec names for the library UI."""
    if not value:
        return ""
    codec = str(value).strip().lower()
    if codec.startswith(("av01", "av1")):
        return "AV1"
    if codec.startswith(("vp09", "vp9")):
        return "VP9"
    if codec.startswith(("avc1", "h264", "h.264")):
        return "H.264"
    if codec.startswith(("hev1", "hvc1", "hevc")):
        return "HEVC"
    if codec.startswith("vp8"):
        return "VP8"
    return codec.upper()


def audio_codec_label(value) -> str:
    """Short, readable audio codec names for the library UI."""
    if not value:
        return ""
    codec = str(value).strip().lower()
    if codec.startswith("opus"):
        return "Opus"
    if codec.startswith(("mp4a", "aac")):
        return "AAC"
    if codec.startswith("vorbis"):
        return "Vorbis"
    if codec.startswith("flac"):
        return "FLAC"
    return codec.upper()


def audio_quality(meta: dict) -> dict:
    """Describe the primary audio track stored with one video."""
    fmt = meta.get("format") if isinstance(meta.get("format"), dict) else {}
    tracks = [
        track for track in meta.get("audio_tracks") or []
        if isinstance(track, dict)
    ]
    primary = next(
        (track for track in tracks if track.get("embedded")),
        tracks[0] if tracks else None,
    )
    codec = audio_codec_label(
        fmt.get("audio_codec") or (primary or {}).get("codec")
    )
    bitrate = _number(fmt.get("audio_bitrate"))
    if bitrate is None and primary is not None:
        bitrate = _number(primary.get("bitrate"))
    label = str(
        fmt.get("audio_label") or (primary or {}).get("label") or ""
    )
    original = fmt.get("audio_original")
    if original is None and primary is not None:
        original = bool(primary.get("original"))
    detail = " ".join(
        part for part in (
            codec,
            f"{int(round(bitrate))}k" if bitrate else "",
        ) if part
    )
    return {
        "audio_codec": codec,
        "audio_bitrate": int(round(bitrate)) if bitrate else None,
        "audio_detail": detail,
        "audio_label": label,
        "audio_original": bool(original),
    }


def container_label(value) -> str:
    container = str(value or "").strip().lower().lstrip(".")
    return {
        "webm": "WebM", "mp4": "MP4", "m4v": "M4V",
        "mkv": "MKV", "mov": "MOV",
    }.get(container, container.upper())


def channel_url(channel_id, channel: str = "") -> str:
    if channel_id:
        return f"https://www.youtube.com/channel/{channel_id}"
    if channel:
        return (
            "https://www.youtube.com/results?search_query="
            + quote(str(channel))
        )
    return ""


def audio_tracks_view(
    video_dir: Path, meta: dict, playlist_id: str, video_id: str
) -> list[dict]:
    """Audio tracks the browser can switch between for one video."""
    rows: list[dict] = []
    for track in meta.get("audio_tracks") or []:
        if not isinstance(track, dict):
            continue
        codec_label = {
            "opus": "Opus", "mp4a": "AAC", "aac": "AAC",
            "vorbis": "Vorbis", "flac": "FLAC",
        }.get(
            str(track.get("codec") or "").lower(),
            str(track.get("codec") or "").upper(),
        )
        bitrate = int(track.get("bitrate") or 0) or None
        row = {
            "id": str(track.get("id") or ""),
            "label": str(track.get("label") or "Audio"),
            "language": str(track.get("language") or ""),
            "codec": str(track.get("codec") or ""),
            "codec_label": codec_label,
            "detail_label": " ".join(
                part for part in (
                    codec_label, f"{bitrate}k" if bitrate else "",
                ) if part
            ),
            "bitrate": bitrate,
            "embedded": bool(track.get("embedded")),
            "original": bool(track.get("original")),
            "auto_dubbed": bool(track.get("auto_dubbed")),
            "default": bool(track.get("default")),
            "role_label": (
                "Original audio" if track.get("original")
                else ("AI dub" if track.get("auto_dubbed") else "Alternate audio")
            ),
            "display_label": (
                "Original audio" if track.get("original")
                else str(track.get("label") or "Audio")
            ),
            "size": int(track.get("size") or 0),
            "url": "",
        }
        if not row["id"]:
            continue
        if not row["embedded"]:
            path = _safe_relative(video_dir, track.get("file"))
            if path is None or not path.is_file():
                continue
            row["url"] = (
                f"/media/{quote(str(playlist_id), safe='')}/"
                f"{quote(str(video_id), safe='')}/audio/"
                f"{quote(row['id'], safe='')}"
            )
        rows.append(row)
    if not rows:
        fmt = meta.get("format") or {}
        if fmt.get("audio_id"):
            codec_label = {
                "mp4a": "AAC", "opus": "Opus",
            }.get(
                str(fmt.get("audio_codec") or "").lower(),
                str(fmt.get("audio_codec") or "").upper(),
            )
            bitrate = int(fmt.get("audio_bitrate") or 0) or None
            rows.append({
                "id": "embedded",
                "label": "Original audio",
                "language": "",
                "codec": fmt.get("audio_codec") or "",
                "codec_label": codec_label,
                "detail_label": " ".join(
                    part for part in (
                        codec_label, f"{bitrate}k" if bitrate else "",
                    ) if part
                ),
                "bitrate": bitrate,
                "embedded": True,
                "original": True,
                "auto_dubbed": False,
                "default": True,
                "role_label": "Original audio",
                "display_label": "Original audio",
                "size": 0,
                "url": "",
            })
    return rows


def _number(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def media_quality(meta: dict, media: Path | None) -> dict:
    """Describe the rendition that is actually on disk for one video."""
    if media is None:
        return {
            "height": None, "video_codec": "", "fps": None,
            "container": "", "quality_label": "", "quality_detail": "",
            "size_bytes": 0, "size_label": "",
            "audio_codec": "", "audio_bitrate": None,
            "audio_detail": "", "audio_label": "", "audio_original": False,
        }
    raw_format = meta.get("format")
    fmt = raw_format if isinstance(raw_format, dict) else {}
    height = _number(fmt.get("height"))
    height = int(height) if height else None
    codec = codec_label(fmt.get("video_codec"))
    fps = _number(fmt.get("fps"))
    container = container_label(
        fmt.get("container") or (media.suffix[1:] if media else "")
    )
    quality_label = " ".join(
        part for part in (f"{height}p" if height else "", codec) if part
    )
    bits = [quality_label] if quality_label else []
    if fps and abs(fps - 30) > 0.05:
        bits.append(f"{fps:g} fps")
    if container:
        bits.append(container)
    try:
        size = media.stat().st_size
    except OSError:
        size = 0
    audio = audio_quality(meta)
    return {
        "height": height,
        "video_codec": codec,
        "fps": fps,
        "container": container,
        "quality_label": quality_label,
        "quality_detail": " · ".join(bits),
        "size_bytes": size,
        "size_label": human_bytes(size),
        **audio,
    }


def quality_summary(entries: list["Entry"]) -> str:
    available = [entry for entry in entries if entry.available and entry.height]
    if not available:
        return ""
    heights = sorted({entry.height for entry in available if entry.height})
    if len(heights) == 1:
        text = f"{heights[0]}p"
    elif len(heights) == 2:
        text = f"{heights[0]}p/{heights[1]}p"
    else:
        text = f"{heights[0]}p–{heights[-1]}p"
    codecs = sorted({entry.video_codec for entry in available if entry.video_codec})
    if len(codecs) == 1:
        text += f" {codecs[0]}"
    elif codecs:
        text += " mixed codecs"
    audio = sorted({
        entry.audio_codec for entry in available if entry.audio_codec
    })
    if len(audio) == 1:
        text += f" · {audio[0]} audio"
    elif audio:
        text += " · mixed audio codecs"
    return text


@dataclass
class Progress:
    position: float = 0.0
    duration: float | None = None
    watched: bool = False
    updated_at: str | None = None

    @property
    def fraction(self) -> float:
        if not self.duration or self.duration <= 0:
            return 0.0
        return max(0.0, min(1.0, self.position / self.duration))




class ProgressStore:
    """Atomic, thread-safe watched-position store.

    ``progress.json``::

        {"schema": 1,
         "videos": {"<video-id>": {"position": 123.4, "duration": 3754,
                                   "watched": false, "updated_at": "..."}}}
    """

    def __init__(self, layout: LibraryLayout):
        self.layout = layout
        self.path = layout.progress_file
        self._lock = threading.Lock()

    def read(self) -> dict:
        doc = read_json_document(self.path)
        if doc.corrupt or doc.status == "unreadable":
            # Reading is best-effort so the library stays browsable; any write
            # path refuses to reset the document (see update()).
            return {}
        videos = (doc.data or {}).get("videos")
        return videos if isinstance(videos, dict) else {}

    def get(self, video_id: str) -> Progress:
        row = self.read().get(video_id) or {}
        try:
            position = max(0.0, float(row.get("position") or 0.0))
        except (TypeError, ValueError):
            position = 0.0
        duration = row.get("duration")
        try:
            duration = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration = None
        return Progress(
            position=position,
            duration=duration,
            watched=bool(row.get("watched")),
            updated_at=row.get("updated_at"),
        )

    def update(
        self,
        video_id: str,
        position: float,
        *,
        duration: float | None = None,
        watched: bool | None = None,
    ) -> Progress:
        with self._lock, document_lock(self.path):
            document = read_json_document(self.path)
            if document.corrupt or document.status == "unreadable":
                raise CorruptDocumentError(
                    self.path, document.status, document.detail
                )
            videos = (document.data or {}).get("videos")
            if not isinstance(videos, dict):
                videos = {}
            current = dict(videos.get(video_id) or {})
            try:
                duration_value = float(duration) if duration is not None else (
                    float(current.get("duration"))
                    if current.get("duration") is not None else None
                )
            except (TypeError, ValueError):
                duration_value = None
            try:
                position_value = max(0.0, float(position))
            except (TypeError, ValueError):
                position_value = 0.0
            if duration_value and duration_value > 0:
                position_value = min(position_value, duration_value)

            if watched is None:
                if duration_value and duration_value > 0:
                    # Watching to the end marks a video; a later partial
                    # rewatch never clears that mark.
                    watched_value = bool(current.get("watched")) or (
                        position_value >= duration_value * 0.95
                        or duration_value - position_value <= 10
                    )
                else:
                    watched_value = bool(current.get("watched"))
            else:
                watched_value = bool(watched)

            current.update({
                "position": round(position_value, 3),
                "duration": round(duration_value, 3)
                if duration_value is not None else None,
                "watched": watched_value,
                "updated_at": now_iso(),
            })
            videos[video_id] = current
            write_json(self.path, {"schema": 1, "videos": videos})
            return Progress(
                position=current["position"],
                duration=current["duration"],
                watched=current["watched"],
                updated_at=current["updated_at"],
            )


class TitleStore:
    """Small, user-owned title overrides that survive remote metadata syncs.

    ``titles.json``::

        {"schema": 1,
         "playlists": {"<playlist-id>": "My title"},
         "videos": {"<video-id>": "My lecture title"}}
    """

    def __init__(self, layout: LibraryLayout):
        self.layout = layout
        self.path = layout.titles_file
        self._lock = threading.Lock()

    def read(self) -> dict[str, dict[str, str]]:
        doc = read_json(self.path) or {}
        result: dict[str, dict[str, str]] = {}
        for section in ("playlists", "videos"):
            rows = doc.get(section)
            if not isinstance(rows, dict):
                rows = {}
            result[section] = {
                str(key): str(value).strip()
                for key, value in rows.items()
                if isinstance(value, str) and value.strip()
            }
        return result

    def _set(self, section: str, key: str, title: str | None) -> str:
        clean = " ".join((title or "").split()).strip()
        with self._lock, document_lock(self.path):
            document = read_json_document(self.path)
            if document.corrupt or document.status == "unreadable":
                raise CorruptDocumentError(
                    self.path, document.status, document.detail
                )
            rows: dict[str, dict[str, str]] = {}
            for name in ("playlists", "videos"):
                part = (document.data or {}).get(name)
                if not isinstance(part, dict):
                    part = {}
                rows[name] = {
                    str(item_key): str(value).strip()
                    for item_key, value in part.items()
                    if isinstance(value, str) and value.strip()
                }
            if clean:
                rows[section][str(key)] = clean[:300]
            else:
                rows[section].pop(str(key), None)
            write_json(self.path, {"schema": 1, **rows})
        return clean[:300]

    def set_playlist_title(self, playlist_id: str, title: str | None) -> str:
        return self._set("playlists", playlist_id, title)

    def set_video_title(self, video_id: str, title: str | None) -> str:
        return self._set("videos", video_id, title)


@dataclass
class Chapter:
    start: float
    end: float | None
    title: str
    time_label: str
    duration_label: str | None = None


@dataclass
class Entry:
    id: str
    index: int
    title: str
    channel: str
    duration: float | None
    duration_label: str
    description: str
    description_snippet: str
    available: bool
    watched: bool
    position: float
    fraction: float
    thumb_url: str
    url: str
    chapter_count: int = 0
    has_transcript: bool = False
    height: int | None = None
    video_codec: str = ""
    fps: float | None = None
    container: str = ""
    quality_label: str = ""
    quality_detail: str = ""
    size_bytes: int = 0
    size_label: str = ""
    audio_codec: str = ""
    audio_detail: str = ""
    audio_label: str = ""
    error: str = ""  # why the last sync could not fetch it, when it said


@dataclass
class PlaylistView:
    id: str
    title: str
    channel: str
    description: str
    description_html: str
    thumb_url: str
    entries: list[Entry] = field(default_factory=list)
    total_duration: float = 0.0
    total_duration_label: str = ""
    watched_count: int = 0
    progress: float = 0.0
    next_url: str | None = None
    transcript_count: int = 0
    transcript_summary: str = ""
    updated_at: str | None = None
    total_bytes: int = 0
    total_bytes_label: str = ""
    downloaded_count: int = 0
    quality_summary: str = ""
    channel_url: str = ""
    kind: str = "youtube"
    preserve_audio: bool = False
    library_id: str = ""
    library_label: str = ""

    key: str = ""

    @property
    def video_count(self) -> int:
        return len(self.entries)

    @property
    def is_ungrouped(self) -> bool:
        return self.kind == "ungrouped"

    @property
    def is_local(self) -> bool:
        return self.kind == "local"

    def upgrade_reads(self, heights: Iterable[int]) -> list[tuple[int, int]]:
        """``(height, videos an upgrade to it reads from YouTube)``: those not
        stored at or above it (missing ones included), as ``upgrade_lookup``
        decides; read from ``meta.json``, no network."""
        return [
            (height, sum(
                1 for entry in self.entries
                if not (entry.available and entry.height and entry.height >= height)
            ))
            for height in heights
        ]


@dataclass
class VideoView:
    playlist: PlaylistView
    entry: Entry
    meta: dict
    title: str
    channel: str
    description: str
    description_html: str
    upload_date: str
    duration: float | None
    duration_label: str
    chapters: list[Chapter]
    transcript: dict | None
    transcript_segments: list[dict]
    progress: Progress
    media_url: str
    thumb_url: str
    media_type: str
    has_media: bool
    prev_entry: Entry | None
    next_entry: Entry | None
    queue: list[Entry]
    quality_label: str = ""
    quality_detail: str = ""
    media_size: int = 0
    media_size_label: str = ""
    audio_detail: str = ""
    audio_label: str = ""
    channel_url: str = ""
    audio_tracks: list[dict] = field(default_factory=list)



def _media_ref(meta: dict | None) -> str | None:
    files = (meta or {}).get("files")
    ref = files.get("media") if isinstance(files, dict) else None
    return ref if isinstance(ref, str) and ref else None


def media_references(
    layout: LibraryLayout, relative_media: str
) -> list[tuple[str, str]]:
    """Every ``(playlist_id, video_id)`` in a library whose ``files.media``
    is ``relative_media`` (a scan; the index will replace it)."""
    return _reference_map(layout).get(relative_media, [])


def _reference_map(layout: LibraryLayout) -> dict[str, list[tuple[str, str]]]:
    """``files.media`` ref -> the ``(playlist_id, video_id)`` using it."""
    refs: dict[str, list[tuple[str, str]]] = {}
    for playlist_id, video_id, _path, meta in iter_video_metas(layout):
        ref = _media_ref(meta)
        if ref:
            refs.setdefault(ref, []).append((playlist_id, video_id))
    return refs


def _audio_home(layout: LibraryLayout, video_dir: Path, meta: dict) -> Path:
    """The meta dir holding a video's alternate audio files.

    Alternate audio belongs to the media file, so it lives beside the owner's
    meta.json; a referencing copy finds it through the shared media ref.
    """
    if not meta.get("audio_tracks") or (video_dir / "audio").is_dir():
        return video_dir  # most videos: no library scan
    ref = _media_ref(meta)
    if ref:
        for playlist_id, video_id in media_references(layout, ref):
            try:
                other = layout.video_meta_dir(playlist_id, video_id)
            except ValueError:
                continue
            if (other / "audio").is_dir():
                return other
    return video_dir


def _read_section_doc(path: Path, sections: tuple[str, ...]) -> dict[str, dict]:
    """The dict sections of a state document; a damaged one raises."""
    document = read_json_document(path)
    if document.corrupt or document.status == "unreadable":
        raise CorruptDocumentError(path, document.status, document.detail)
    data = document.data if isinstance(document.data, dict) else {}
    result: dict[str, dict] = {}
    for name in sections:
        part = data.get(name)
        result[name] = dict(part) if isinstance(part, dict) else {}
    return result


def _updated_at(row) -> str:
    return str(row.get("updated_at") or "") if isinstance(row, dict) else ""


def _merge_sections(
    path: Path,
    sections: tuple[str, ...],
    incoming: dict[str, dict],
    *,
    prefer_newer: bool = False,
) -> None:
    """Merge ``incoming`` rows into a state document's sections.

    ``prefer_newer`` (progress): the row with the later ``updated_at`` wins.
    Otherwise the ``playlists`` section takes the incoming row and any other
    section keeps a row it already has."""
    with document_lock(path):
        document = read_json_document(path)
        if document.corrupt or document.status == "unreadable":
            raise CorruptDocumentError(path, document.status, document.detail)
        data = dict(document.data) if isinstance(document.data, dict) else {}
        changed = False
        for name in sections:
            current = data.get(name)
            rows = dict(current) if isinstance(current, dict) else {}
            for key, value in (incoming.get(name) or {}).items():
                existing = rows.get(key)
                if existing is None:
                    take = True
                elif prefer_newer:
                    take = _updated_at(value) > _updated_at(existing)
                else:
                    take = name == "playlists"
                if take and existing != value:
                    rows[key] = value
                    changed = True
            data[name] = rows
        if changed:
            data.setdefault("schema", 1)
            write_json(path, data)


def _walk_files(path: Path):
    for root, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [name for name in dirs if not name.startswith(".")]
        for name in files:
            yield os.path.join(root, name)


def _lives_in(playlist_id: str, meta_dir: Path) -> bool:
    """``meta_dir`` is the folder every path for ``playlist_id`` resolves to.

    A playlist.json whose id points at another folder cannot reach its own
    videos; the library index skips it, and so does every listing.
    """
    try:
        return safe_id(playlist_id) == meta_dir.name
    except ValueError:
        return False


class Library:
    """Playlist views and edits across every online library."""

    def __init__(self, libraries: LibrarySet):
        self.libraries = libraries
        self._stores_lock = threading.Lock()
        self._progress: dict[str, ProgressStore] = {}
        self._titles: dict[str, TitleStore] = {}

    # -- per-library state -------------------------------------------------
    def progress_for(self, ref: LibraryRef) -> ProgressStore:
        key = str(ref.layout.root)
        with self._stores_lock:
            store = self._progress.get(key)
            if store is None:
                store = self._progress[key] = ProgressStore(ref.layout)
            return store

    def titles_for(self, ref: LibraryRef) -> TitleStore:
        key = str(ref.layout.root)
        with self._stores_lock:
            store = self._titles.get(key)
            if store is None:
                store = self._titles[key] = TitleStore(ref.layout)
            return store

    def _state(self, ref: LibraryRef, cache: dict) -> tuple[dict, dict]:
        """One read of a library's progress and titles per request."""
        if ref.id not in cache:
            cache[ref.id] = (
                self.progress_for(ref).read(),
                self.titles_for(ref).read(),
            )
        return cache[ref.id]

    # -- discovery ---------------------------------------------------------
    def _records(self) -> list[Record]:
        """Every playlist on every online library (launch library first)."""
        records: list[Record] = []
        for ref in self.libraries.libraries():
            seen: set[str] = set()
            try:
                candidates = sorted(ref.layout.meta_root.iterdir())
            except OSError:
                continue
            for path in candidates:
                try:
                    if path.name.startswith(".") or not path.is_dir():
                        continue
                except OSError:
                    continue
                doc = read_json(path / "playlist.json")
                if not doc:
                    continue
                playlist_id = str(doc.get("id") or path.name)
                if playlist_id in seen or not _lives_in(playlist_id, path):
                    continue
                seen.add(playlist_id)
                records.append((ref, path, doc))
        return records

    def _keyed(self) -> list[Keyed]:
        records = self._records()
        counts = Counter(str(doc.get("id")) for _ref, _path, doc in records)
        return [
            (
                playlist_key(str(doc.get("id")), ref.id, counts[str(doc.get("id"))] > 1),
                ref, path, doc,
            )
            for ref, path, doc in records
        ]

    def playlist_dirs(self) -> list[Path]:
        """Every playlist's metadata directory on an online library."""
        return [path for _ref, path, _doc in self._records()]

    def _resolve_keyed(self, key: str) -> Keyed | None:
        """Resolve a playlist key; a bare id that several libraries hold
        resolves to the first (launch library first)."""
        keyed = self._keyed()
        for row in keyed:
            if row[0] == key:
                return row
        for row in keyed:
            if str(row[3].get("id")) == key:
                return row
        return None

    def is_ambiguous(self, key: str) -> bool:
        """``key`` is a bare playlist id that more than one online library
        holds (so it is no longer anyone's route key)."""
        keyed = self._keyed()
        if any(row[0] == key for row in keyed):
            return False
        return sum(1 for row in keyed if str(row[3].get("id")) == key) > 1

    def resolve_playlist(self, key: str) -> Record | None:
        row = self._resolve_keyed(key)
        return (row[1], row[2], row[3]) if row is not None else None

    def resolve_in(self, library_id: str, playlist_id: str) -> Record | None:
        """A playlist by its real id within one library."""
        for ref, meta_dir, doc in self._records():
            if ref.id == library_id and str(doc.get("id")) == playlist_id:
                return ref, meta_dir, doc
        return None

    def key_for(self, library_id: str, playlist_id: str) -> str:
        for key, ref, _path, doc in self._keyed():
            if ref.id == library_id and str(doc.get("id")) == playlist_id:
                return key
        return playlist_id

    def library_for_playlist(self, key: str) -> LibraryRef | None:
        resolved = self.resolve_playlist(key)
        return resolved[0] if resolved is not None else None

    def playlists_holding(self, key: str, video_id: str) -> list[tuple[LibraryRef, str]]:
        """``(library, playlist id)`` of every playlist in ``key``'s library
        that holds ``video_id`` (a video title applies to all of them)."""
        ref = self.library_for_playlist(key)
        if ref is None:
            return []
        return [
            (ref, str(doc.get("id")))
            for other, _path, doc in self._records()
            if other.id == ref.id and any(
                str(raw.get("id")) == video_id for raw in doc.get("entries") or []
            )
        ]

    def _writable(self, key: str) -> Record | None:
        resolved = self.resolve_playlist(key)
        if resolved is not None:
            resolved[0].require_writable()
        return resolved

    @staticmethod
    def _video_dir(layout: LibraryLayout, playlist_id: str, video_id: str) -> Path | None:
        try:
            return layout.video_meta_dir(playlist_id, video_id)
        except ValueError:
            return None

    def playlist_titles(self) -> dict[str, str]:
        """Display titles by playlist key."""
        cache: dict = {}
        titles = {}
        for key, ref, _meta_dir, doc in self._keyed():
            if doc.get("id"):
                playlist_id = str(doc["id"])
                overrides = self._state(ref, cache)[1]["playlists"]
                titles[key] = overrides.get(playlist_id) or str(
                    doc.get("title") or playlist_id
                )
        return titles

    # -- usage -------------------------------------------------------------
    def _usage(
        self, records: list[Record]
    ) -> tuple[dict[tuple[str, str], int], int]:
        """Per-playlist bytes and a library-wide total that counts a media
        file referenced by several playlists only once."""
        per: dict[tuple[str, str], int] = {}
        seen: set[tuple[int, int]] = set()
        total = 0
        for ref, meta_dir, doc in records:
            playlist_id = str(doc.get("id") or "")
            paths = list(_walk_files(meta_dir))
            for raw in doc.get("entries") or []:
                video_dir = self._video_dir(ref.layout, playlist_id, str(raw.get("id") or ""))
                if video_dir is None:
                    continue
                media = find_media(ref.layout, video_dir)
                if media is not None:
                    paths.append(str(media))
            subtotal = 0
            for path in paths:
                try:
                    stat = os.stat(path, follow_symlinks=False)
                except OSError:
                    continue
                subtotal += stat.st_size
                key = (stat.st_dev, stat.st_ino)
                if key not in seen:
                    seen.add(key)
                    total += stat.st_size
            per[(ref.id, playlist_id)] = subtotal
        return per, total

    # -- views -------------------------------------------------------------
    def playlists(self) -> list[PlaylistView]:
        cache: dict = {}
        return [
            self._playlist_view(key, ref, meta_dir, doc, *self._state(ref, cache))
            for key, ref, meta_dir, doc in self._keyed()
            if doc.get("id")
        ]

    def playlists_and_bytes(self) -> tuple[list[PlaylistView], int]:
        """Build the library page's views and its total with one disk walk."""
        keyed = self._keyed()
        per, total = self._usage([(ref, path, doc) for _k, ref, path, doc in keyed])
        cache: dict = {}
        views = [
            self._playlist_view(
                key, ref, meta_dir, doc, *self._state(ref, cache),
                used_bytes=per.get((ref.id, str(doc.get("id") or "")), 0),
            )
            for key, ref, meta_dir, doc in keyed
            if doc.get("id")
        ]
        return views, total

    def playlist_view(self, key: str) -> PlaylistView | None:
        row = self._resolve_keyed(key)
        if row is None:
            return None
        canonical, ref, meta_dir, doc = row
        return self._playlist_view(
            canonical, ref, meta_dir, doc, *self._state(ref, {})
        )

    def _playlist_view(
        self,
        key: str,
        ref: LibraryRef,
        meta_dir: Path,
        doc: dict,
        progress_map: dict,
        title_overrides: dict,
        *,
        used_bytes: int | None = None,
    ) -> PlaylistView:
        layout = ref.layout
        playlist_id = str(doc.get("id") or "")
        playlist_title = title_overrides["playlists"].get(playlist_id)
        video_titles = title_overrides["videos"]
        entries: list[Entry] = []
        total = 0.0
        transcript_total = 0
        watched = 0
        available_count = 0

        for position, raw in enumerate(doc.get("entries") or [], start=1):
            video_id = str(raw.get("id") or "")
            if not video_id:
                continue
            video_dir = self._video_dir(layout, playlist_id, video_id)
            if video_dir is None:
                continue
            meta = read_json(video_dir / "meta.json") or {}
            media = find_media(layout, video_dir)
            duration = meta.get("duration") or raw.get("duration")
            try:
                duration_value = float(duration) if duration is not None else None
            except (TypeError, ValueError):
                duration_value = None
            if duration_value:
                total += duration_value

            row = progress_map.get(video_id) or {}
            try:
                position_s = max(0.0, float(row.get("position") or 0.0))
            except (TypeError, ValueError):
                position_s = 0.0
            is_watched = bool(row.get("watched"))
            if media is not None:
                available_count += 1
                if is_watched:
                    watched += 1
            fraction = (
                min(1.0, position_s / duration_value)
                if duration_value and duration_value > 0 else 0.0
            )
            chapters = meta.get("chapters") or []
            if not chapters:
                chapters = chapters_from_description(meta.get("description") or "")
            transcript_block = meta.get("transcript") or {}
            # Presence is enough for a list badge; never parse the full
            # transcript while rendering the library.
            has_transcript = bool(transcript_block) or (
                (video_dir / "transcript.json").is_file()
            )
            if has_transcript:
                transcript_total += 1

            quality = media_quality(meta, media)
            entries.append(Entry(
                id=video_id,
                index=position,
                title=str(
                    video_titles.get(video_id)
                    or meta.get("title")
                    or raw.get("title")
                    or video_id
                ),
                channel=str(meta.get("channel") or doc.get("channel") or ""),
                duration=duration_value,
                duration_label=format_duration(duration_value),
                description=str(meta.get("description") or ""),
                description_snippet=snippet(
                    str(meta.get("description") or ""), 180
                ),
                available=media is not None,
                watched=is_watched,
                position=position_s,
                fraction=fraction,
                thumb_url=thumb_url(key, video_id),
                url=f"/watch/{quote(key, safe='@')}/{quote(video_id)}",
                chapter_count=len(chapters),
                has_transcript=has_transcript,
                height=quality["height"],
                video_codec=quality["video_codec"],
                fps=quality["fps"],
                container=quality["container"],
                quality_label=quality["quality_label"],
                quality_detail=quality["quality_detail"],
                size_bytes=quality["size_bytes"],
                size_label=quality["size_label"],
                audio_codec=quality["audio_codec"],
                audio_detail=quality["audio_detail"],
                audio_label=quality["audio_label"],
                error=str(raw.get("error") or ""),
            ))

        next_entry = next(
            (entry for entry in entries if entry.available and not entry.watched),
            next((entry for entry in entries if entry.available), None),
        )
        playlist_thumb = (doc.get("files") or {}).get("thumbnail")
        if used_bytes is None:
            used_bytes = self._usage([(ref, meta_dir, doc)])[0].get(
                (ref.id, playlist_id), 0
            )
        summary = doc.get("transcripts") or {}
        transcript_summary = f"{transcript_total}/{len(entries)} transcripts"
        if summary:
            failed = int(summary.get("failed") or 0)
            missing = int(summary.get("missing") or 0)
            if failed:
                transcript_summary += f" ({failed} failed)"
            elif missing:
                transcript_summary += f" ({missing} unavailable)"
        return PlaylistView(
            id=str(doc.get("id")),
            title=str(playlist_title or doc.get("title") or doc.get("id")),
            channel=str(doc.get("channel") or ""),
            description=str(doc.get("description") or ""),
            description_html=description_html(str(doc.get("description") or "")),
            thumb_url=f"/media/{quote(key)}/thumb"
            if playlist_thumb and (meta_dir / playlist_thumb).is_file()
            else "",
            entries=entries,
            total_duration=total,
            total_duration_label=format_duration(total),
            watched_count=watched,
            progress=(watched / available_count if available_count else 0.0),
            next_url=next_entry.url if next_entry else None,
            transcript_count=transcript_total,
            transcript_summary=transcript_summary,
            updated_at=doc.get("updated_at"),
            total_bytes=used_bytes,
            total_bytes_label=human_bytes(used_bytes),
            downloaded_count=available_count,
            quality_summary=quality_summary(entries),
            channel_url=channel_url(doc.get("channel_id"), str(doc.get("channel") or "")),
            kind=str(doc.get("kind") or "youtube"),
            library_id=ref.id,
            library_label=ref.label,
            key=key,
            preserve_audio=bool(
                (doc.get("options") or {}).get("preserve_audio")
                if isinstance(doc.get("options"), dict) else False
            ),
        )

    def video_view(self, key: str, video_id: str) -> VideoView | None:
        row = self._resolve_keyed(key)
        if row is None:
            return None
        key, ref, meta_dir, doc = row
        playlist_id = str(doc.get("id") or "")
        progress_map, title_overrides = self._state(ref, {})
        playlist = self._playlist_view(
            key, ref, meta_dir, doc, progress_map, title_overrides
        )
        entry = next((e for e in playlist.entries if e.id == video_id), None)
        if entry is None:
            return None
        video_dir = self._video_dir(ref.layout, playlist_id, video_id)
        if video_dir is None:
            return None
        meta = read_json(video_dir / "meta.json") or {}
        media = find_media(ref.layout, video_dir)
        chapters = self._chapters(meta.get("chapters") or [])
        if not chapters:
            chapters = self._chapters(
                chapters_from_description(meta.get("description") or "")
            )
        transcript = read_transcript(video_dir)
        segments = [
            segment.to_dict() for segment in segments_from_doc(transcript)
        ]

        available = [e for e in playlist.entries if e.available]
        index = next(
            (i for i, e in enumerate(available) if e.id == video_id), None
        )
        prev_entry = available[index - 1] if index is not None and index > 0 else None
        next_entry = (
            available[index + 1]
            if index is not None and index + 1 < len(available) else None
        )
        try:
            duration_value = float(meta.get("duration") or entry.duration)
        except (TypeError, ValueError):
            duration_value = entry.duration

        quality = media_quality(meta, media)
        video_channel = str(meta.get("channel") or playlist.channel)
        return VideoView(
            playlist=playlist,
            entry=entry,
            meta=meta,
            title=entry.title,
            channel=video_channel,
            description=entry.description,
            description_html=description_html(entry.description),
            upload_date=format_date(meta.get("upload_date")),
            duration=duration_value,
            duration_label=format_duration(duration_value),
            chapters=chapters,
            transcript=transcript,
            transcript_segments=segments,
            progress=self.progress_for(ref).get(video_id),
            media_url=stream_url(key, video_id),
            thumb_url=thumb_url(key, video_id),
            media_type=self._media_type(media),
            has_media=media is not None,
            prev_entry=prev_entry,
            next_entry=next_entry,
            queue=playlist.entries,
            quality_label=quality["quality_label"],
            quality_detail=quality["quality_detail"],
            media_size=quality["size_bytes"],
            media_size_label=quality["size_label"],
            audio_detail=quality["audio_detail"],
            audio_label=quality["audio_label"],
            channel_url=channel_url(meta.get("channel_id"), video_channel),
            audio_tracks=audio_tracks_view(
                _audio_home(ref.layout, video_dir, meta), meta, key, video_id
            ),
        )

    # -- titles ------------------------------------------------------------
    def set_playlist_title(self, key: str, title: str | None) -> bool:
        """Save a title override and rename the playlist's media folder."""
        resolved = self._writable(key)
        if resolved is None:
            return False
        ref, _meta_dir, doc = resolved
        playlist_id = str(doc.get("id"))
        clean = self.titles_for(ref).set_playlist_title(playlist_id, title)
        if playlist_id != UNGROUPED_ID:
            rename_folder(
                ref.layout, playlist_id,
                clean or str(doc.get("title") or playlist_id),
            )
        return True

    def set_video_title(self, key: str, video_id: str, title: str | None) -> bool:
        """Save a video title override and rename its media to match."""
        resolved = self._writable(key)
        if resolved is None:
            return False
        ref = resolved[0]
        self.titles_for(ref).set_video_title(video_id, title)
        # Titles are per video id, so every playlist holding it is renamed.
        for _ref, playlist_id in self.playlists_holding(key, video_id):
            sync_playlist_files(ref.layout, playlist_id)
        return True

    # -- playlist editing --------------------------------------------------
    def remove_playlist(self, key: str) -> bool:
        """Delete one playlist's metadata and the media only it uses.

        Media this playlist owns that other playlists still reference moves
        to the first of them instead of being deleted.
        """
        resolved = self._writable(key)
        if resolved is None:
            return False
        self._remove_record(*resolved)
        return True

    def remove_from(self, library_id: str, playlist_id: str) -> bool:
        """:meth:`remove_playlist` by real id within one library (a moved
        playlist's id is held by two libraries until its source goes)."""
        resolved = self.resolve_in(library_id, playlist_id)
        if resolved is None:
            return False
        resolved[0].require_writable()
        self._remove_record(*resolved)
        return True

    def release_shared_media(self, library_id: str, playlist_id: str) -> None:
        """Hand media this playlist owns that other playlists of its library
        reference to the first of them (Task 8 rules), keeping the rest.

        Afterwards the playlist only *references* that media, so its own
        folder can leave the library without breaking anyone."""
        resolved = self.resolve_in(library_id, playlist_id)
        if resolved is None:
            return
        ref, _meta_dir, doc = resolved
        ref.require_writable()
        with PUBLISH_LOCK:
            self._release_media(
                ref.layout, doc, self._owned_video_ids(ref.layout, playlist_id),
                delete=False,
            )

    @staticmethod
    def _owned_video_ids(layout: LibraryLayout, playlist_id: str) -> list[str]:
        return [
            video_id
            for owner, video_id, _path, _meta in iter_video_metas(layout)
            if owner == playlist_id
        ]

    def _remove_record(self, ref: LibraryRef, meta_dir: Path, doc: dict) -> None:
        playlist_id = str(doc.get("id"))
        layout = ref.layout
        with PUBLISH_LOCK:
            video_ids = self._owned_video_ids(layout, playlist_id)
            self._release_media(layout, doc, video_ids, delete=True)
            shutil.rmtree(meta_dir)
            folder = doc.get("folder")
            if folder:
                media_folder = layout.media_root / str(folder)
                (media_folder / "folder.jpg").unlink(missing_ok=True)
                try:
                    media_folder.rmdir()
                except OSError:
                    pass  # still holds media another playlist uses
        remove_playlist_index(layout, playlist_id)
        self._drop_previews(layout, playlist_id)

    def forget_moved(self, ref: LibraryRef, playlist_id: str) -> None:
        """Drop the derived state of a playlist renamed out of ``ref``."""
        remove_playlist_index(ref.layout, playlist_id)
        self._drop_previews(ref.layout, playlist_id)

    def merge_state_into(
        self,
        source: LibraryRef,
        dest: LibraryRef,
        playlist_id: str,
        video_ids: list[str],
    ) -> None:
        """Carry a moving playlist's watch progress, title overrides and tags
        from ``source`` into ``dest``.

        Progress keeps the newer of two entries.  The playlist's own title and
        tags come from the source; a video title or tag ``dest`` already has
        wins, since it applies to every playlist there holding that video.
        Damaged documents raise :class:`CorruptDocumentError` before anything
        is written."""
        wanted = set(video_ids)
        progress = _read_section_doc(source.layout.progress_file, ("videos",))
        rows = {
            key: value for key, value in progress["videos"].items()
            if key in wanted and isinstance(value, dict)
        }
        if rows:
            store = self.progress_for(dest)
            with store._lock:
                _merge_sections(store.path, ("videos",), {"videos": rows},
                                prefer_newer=True)
        sections = ("playlists", "videos")
        documents: list[tuple[Path, Path, threading.Lock | None]] = [(
            source.layout.titles_file, dest.layout.titles_file,
            self.titles_for(dest)._lock,
        )]
        if source.layout.tags_file.is_file():
            documents.append((source.layout.tags_file, dest.layout.tags_file, None))
        for src_path, dest_path, lock in documents:
            doc = _read_section_doc(src_path, sections)
            picked = {
                "playlists": {
                    key: value for key, value in doc["playlists"].items()
                    if key == playlist_id
                },
                "videos": {
                    key: value for key, value in doc["videos"].items()
                    if key in wanted
                },
            }
            if not any(picked.values()):
                continue
            if lock is None:
                _merge_sections(dest_path, sections, picked)
            else:
                with lock:
                    _merge_sections(dest_path, sections, picked)

    def remove_video(self, key: str, video_id: str) -> bool:
        """Drop one entry from a playlist, with everything that belongs to it.

        Its media follows the ownership rules of :meth:`remove_playlist`:
        media another playlist uses moves there, the rest is deleted with its
        subtitles and alternate audio, so nothing is left behind.
        """
        resolved = self._writable(key)
        if resolved is None:
            return False
        ref, meta_dir, _doc = resolved
        playlist_id = str(_doc.get("id"))
        layout = ref.layout
        with PUBLISH_LOCK:
            doc = read_json(meta_dir / "playlist.json") or {}
            rows = [
                row for row in doc.get("entries") or []
                if isinstance(row, dict) and row.get("id")
            ]
            if not any(str(row["id"]) == video_id for row in rows):
                return False
            remaining = [row for row in rows if str(row["id"]) != video_id]
            updated = dict(doc)
            updated["entries"] = self._renumber(remaining)
            updated["video_count"] = len(remaining)
            updated["updated_at"] = now_iso()
            write_json(meta_dir / "playlist.json", updated)

            video_dir = self._video_dir(layout, playlist_id, video_id)
            if video_dir is not None and video_dir.is_dir():
                self._release_media(layout, doc, [video_id], delete=True)
                shutil.rmtree(video_dir)
            sync_playlist_files(layout, playlist_id)
        self._drop_previews(layout, playlist_id, video_id)
        remove_video_index(layout, playlist_id, video_id)
        return True

    @staticmethod
    def _release_media(
        layout: LibraryLayout, doc: dict, video_ids: list[str], *, delete: bool
    ) -> None:
        """Let ``video_ids`` leave playlist ``doc``: media it owns (the file
        is in its folder) that other playlists reference moves into the first
        referencing playlist's folder; media nothing else references is
        deleted when ``delete``.  The caller holds ``PUBLISH_LOCK``."""
        playlist_id = str(doc.get("id"))
        folder = doc.get("folder")
        leaving = set(video_ids)
        references = _reference_map(layout)
        for video_id in video_ids:
            video_dir = Library._video_dir(layout, playlist_id, video_id)
            if video_dir is None:
                continue
            ref = _media_ref(read_json(video_dir / "meta.json"))
            if not ref:
                continue
            others = [
                row for row in references.get(ref, [])
                if not (row[0] == playlist_id and row[1] in leaving)
            ]
            if not others:
                if delete:
                    remove_media(layout, video_dir)
                continue
            if not folder or not ref.startswith(f"{folder}/"):
                continue  # only a reference; the owner keeps the file
            heir_playlist, heir_video = others[0]
            heir_dir = Library._video_dir(layout, heir_playlist, heir_video)
            audio = video_dir / "audio"
            if heir_dir is not None and audio.is_dir() and not (
                heir_dir / "audio"
            ).exists():
                shutil.move(audio, heir_dir / "audio")
            media = layout.resolve_media(ref)
            if media is not None and media.exists():
                transfer_media(layout, ref, heir_playlist)

    def reorder_video(
        self, key: str, video_id: str, direction: str
    ) -> bool:
        """Move one entry one position up or down."""
        resolved = self.resolve_playlist(key)
        if resolved is None:
            return False
        _ref, _meta_dir, doc = resolved
        ids = [
            str(row["id"]) for row in doc.get("entries") or []
            if isinstance(row, dict) and row.get("id")
        ]
        if video_id not in ids:
            return False
        index = ids.index(video_id)
        if direction == "up" and index > 0:
            ids[index - 1], ids[index] = ids[index], ids[index - 1]
        elif direction == "down" and index + 1 < len(ids):
            ids[index + 1], ids[index] = ids[index], ids[index + 1]
        else:
            return False
        return self.set_order(key, ids)

    def set_order(self, key: str, order: list[str]) -> bool:
        """Persist a full manual order (unknown ids are ignored) and renumber
        the media files to match."""
        resolved = self._writable(key)
        if resolved is None:
            return False
        ref, meta_dir, _doc = resolved
        playlist_id = str(_doc.get("id"))
        with PUBLISH_LOCK:
            doc = read_json(meta_dir / "playlist.json") or {}
            rows = {
                str(row["id"]): row for row in doc.get("entries") or []
                if isinstance(row, dict) and row.get("id")
            }
            if not rows:
                return False
            ordered: list[dict] = []
            seen: set[str] = set()
            for raw in order:
                video_id = str(raw or "")
                if video_id in rows and video_id not in seen:
                    ordered.append(rows[video_id])
                    seen.add(video_id)
            for video_id, row in rows.items():
                if video_id not in seen:
                    ordered.append(row)
            updated = dict(doc)
            updated["entries"] = self._renumber(ordered)
            updated["video_count"] = len(ordered)
            updated["order"] = "manual"
            updated["updated_at"] = now_iso()
            write_json(meta_dir / "playlist.json", updated)
            sync_playlist_files(ref.layout, playlist_id)
        return True

    def set_preserve_audio(self, key: str, enabled: bool) -> bool:
        """Remember the per-playlist audio policy for future syncs."""
        resolved = self._writable(key)
        if resolved is None:
            return False
        _ref, meta_dir, _doc = resolved
        with PUBLISH_LOCK:
            doc = read_json(meta_dir / "playlist.json") or {}
            current = doc.get("options")
            options = dict(current) if isinstance(current, dict) else {}
            options["preserve_audio"] = bool(enabled)
            updated = dict(doc)
            updated["options"] = options
            updated["updated_at"] = now_iso()
            write_json(meta_dir / "playlist.json", updated)
        return True

    def ensure_ungrouped(self, library_id: str | None = None) -> str:
        """Create the catch-all playlist used for single videos."""
        ref = (
            self.libraries.get(library_id) if library_id
            else self.libraries.default()
        )
        if ref is None:
            raise LookupError("that library is not connected")
        meta_dir = ref.layout.playlist_meta_dir(UNGROUPED_ID)
        if (meta_dir / "playlist.json").is_file():
            return UNGROUPED_ID
        ref.require_writable()
        meta_dir.mkdir(parents=True, exist_ok=True)
        now = now_iso()
        write_json(meta_dir / "playlist.json", {
            "schema": 1,
            "id": UNGROUPED_ID,
            "title": UNGROUPED_TITLE,
            "description": "Single videos added to this library.",
            "channel": "",
            "channel_id": "",
            "kind": "ungrouped",
            "folder": UNGROUPED_FOLDER,
            "video_count": 0,
            "entries": [],
            "options": {},
            "files": {},
            "first_seen_at": now,
            "updated_at": now,
        })
        return UNGROUPED_ID

    def create_playlist(
        self,
        title: str,
        selections: list[tuple[str, str]],
        library_id: str | None = None,
    ) -> str | None:
        """Create a local playlist from videos already in a library.

        Each video's metadata folder is copied; ``files.media`` keeps pointing
        at the file the source playlist owns, so no media bytes are added.
        References never cross libraries: a video from another library is
        copied (media and alternate audio) into the new playlist's folder.
        """
        clean_title = " ".join((title or "").split()).strip()
        if not clean_title:
            return None
        if library_id:
            dest = self.libraries.get(library_id)
            if dest is None:
                raise ValueError(
                    "that library is not connected; plug it in or choose another"
                )
        else:
            try:
                dest = self.libraries.default()
            except LookupError as exc:
                raise ValueError("no library is connected; add one first") from exc
        dest.require_writable()
        layout = dest.layout
        playlist_id = f"local-{uuid.uuid4().hex[:10]}"
        final = layout.playlist_meta_dir(playlist_id)
        # Build in a hidden sibling and publish with one rename, so a crash
        # never leaves a half-built playlist in the library index.
        staging = layout.meta_root / f".{playlist_id}.{uuid.uuid4().hex[:8]}.tmp"
        # Media copied from other libraries, staged on the destination volume
        # before the playlist appears, then published into its folder.
        imports_dir = layout.cache_dir / f"import-{uuid.uuid4().hex[:8]}"
        imports: list[tuple[str, Path]] = []
        try:
            # Slow copies from other libraries first, outside PUBLISH_LOCK.
            copied = self._stage_imports(dest, selections, imports_dir)
            # Staged references are invisible to the reference scan, so a
            # concurrent remove of an owner must wait until they are live.
            with PUBLISH_LOCK:
                self._build_playlist(
                    layout, staging, final, playlist_id, clean_title,
                    selections, dest, copied, imports,
                )
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(imports_dir, ignore_errors=True)
            raise
        try:
            for video_id, staged in imports:
                publish_download(layout, playlist_id, video_id, staged)
        finally:
            shutil.rmtree(imports_dir, ignore_errors=True)
        return playlist_id

    def _stage_imports(
        self,
        dest: LibraryRef,
        selections: list[tuple[str, str]],
        imports_dir: Path,
    ) -> dict[tuple[str, str], Path]:
        """Copy media of selections from other libraries onto ``dest``.

        Refuses (``ValueError``) before copying anything when a file is
        larger than the destination's per-file limit (FAT32)."""
        copied: dict[tuple[str, str], Path] = {}
        sources: list[tuple[tuple[str, str], Path]] = []
        for source_playlist, video_id in selections:
            resolved = self.resolve_playlist(source_playlist)
            if resolved is None or resolved[0].id == dest.id:
                continue
            source_ref, _meta, source_doc = resolved
            source_dir = self._video_dir(
                source_ref.layout, str(source_doc.get("id")), video_id
            )
            key = (source_playlist, video_id)
            if source_dir is None or any(key == seen for seen, _ in sources):
                continue
            source_media = find_media(source_ref.layout, source_dir)
            if source_media is None:
                continue
            sources.append((key, source_media))
        limit = fsinfo.fs_info(dest.layout.root).max_file_bytes
        if limit is not None:
            for _key, source_media in sources:
                size = source_media.stat().st_size
                if size > limit:
                    raise ValueError(
                        f"“{source_media.name}” is {human_bytes(size)}, larger "
                        f"than the FAT32 limit of {human_bytes(limit)} per file "
                        f"on {dest.label}"
                    )
        for key, source_media in sources:
            imports_dir.mkdir(parents=True, exist_ok=True)
            staged = imports_dir / f"{len(copied)}-{safe_id(key[1])}{source_media.suffix}"
            shutil.copyfile(source_media, staged)
            copied[key] = staged
        return copied

    def _build_playlist(
        self,
        layout: LibraryLayout,
        staging: Path,
        final: Path,
        playlist_id: str,
        clean_title: str,
        selections: list[tuple[str, str]],
        dest: LibraryRef,
        copied: dict[tuple[str, str], Path],
        imports: list[tuple[str, Path]],
    ) -> None:
        """Stage the playlist's metadata and publish it with one rename."""
        video_titles = self.titles_for(dest).read()["videos"]
        entries: list[dict] = []
        thumb_source: Path | None = None
        channel = ""
        seen: set[str] = set()
        staging.mkdir(parents=True, exist_ok=False)
        for source_playlist, video_id in selections:
            if video_id in seen:
                continue
            resolved = self.resolve_playlist(source_playlist)
            if resolved is None:
                continue
            source_ref, _source_meta, source_doc = resolved
            source_id = str(source_doc.get("id"))
            if not any(
                str(raw.get("id")) == video_id
                for raw in source_doc.get("entries") or []
            ):
                continue
            copying = source_ref.id != dest.id
            source_media: Path | None = None
            source_dir = self._video_dir(source_ref.layout, source_id, video_id)
            if source_dir is None or not (source_dir / "meta.json").is_file():
                continue
            seen.add(video_id)
            dest_dir = staging / "videos" / safe_id(video_id)
            # A reference shares the owner's alternate audio (see
            # _audio_home); a copy takes its own.
            skip = ["*.part", "*.tmp"] if copying else ["*.part", "*.tmp", "audio"]
            shutil.copytree(
                source_dir, dest_dir, ignore=shutil.ignore_patterns(*skip),
            )
            if copying and not (dest_dir / "audio").exists():
                home = _audio_home(
                    source_ref.layout, source_dir,
                    read_json(source_dir / "meta.json") or {},
                )
                if (home / "audio").is_dir():
                    shutil.copytree(home / "audio", dest_dir / "audio")
            meta = read_json(dest_dir / "meta.json") or {}
            if copying:
                source_media = copied.get((source_playlist, video_id))
                files = meta.get("files")
                if not isinstance(files, dict):
                    files = meta["files"] = {}
                files["media"] = None
                if source_media is not None:
                    imports.append((video_id, source_media))
            index = len(entries) + 1
            meta["playlist"] = {
                "id": playlist_id,
                "title": clean_title,
                "index": index,
            }
            write_json(dest_dir / "meta.json", meta)
            if not channel:
                channel = str(meta.get("channel") or "")
            media = (
                source_media if copying else find_media(layout, dest_dir)
            )
            thumbnail = (meta.get("files") or {}).get("thumbnail")
            if thumb_source is None and thumbnail:
                candidate = dest_dir / str(thumbnail)
                if candidate.is_file():
                    thumb_source = candidate
            entries.append({
                "id": video_id,
                "title": video_titles.get(video_id)
                or str(meta.get("title") or video_id),
                "duration": meta.get("duration"),
                "index": index,
                "available": media is not None,
                "metadata_ok": True,
                "error": None,
            })
        thumb_name = None
        if thumb_source is not None:
            try:
                shutil.copyfile(thumb_source, staging / PLAYLIST_THUMB_NAME)
                thumb_name = PLAYLIST_THUMB_NAME
            except OSError:
                thumb_name = None
        now = now_iso()
        write_json(staging / "playlist.json", {
            "schema": 1,
            "id": playlist_id,
            "title": clean_title,
            "description": "",
            "channel": channel,
            "channel_id": "",
            "kind": "local",
            "video_count": len(entries),
            "entries": entries,
            "options": {},
            "files": {"thumbnail": thumb_name},
            "first_seen_at": now,
            "updated_at": now,
        })
        os.replace(staging, final)

    def library_bytes(self) -> int:
        """Bytes on disk, counting a media file shared by playlists once."""
        return self._usage(self._records())[1]

    @staticmethod
    def _renumber(rows: list[dict]) -> list[dict]:
        for position, row in enumerate(rows, start=1):
            row["index"] = position
        return rows

    @staticmethod
    def _drop_previews(
        layout: LibraryLayout, playlist_id: str, video_id: str | None = None
    ) -> None:
        try:
            cache = layout.cache_dir / "previews" / safe_id(playlist_id)
            if video_id is not None:
                cache = cache / safe_id(video_id)
        except ValueError:
            return
        if cache.is_dir():
            shutil.rmtree(cache, ignore_errors=True)

    # -- files -------------------------------------------------------------
    def locate_video(
        self, key: str | None, video_id: str
    ) -> tuple[LibraryRef, Path] | None:
        """A video's library and metadata dir; without a playlist key, the
        first playlist holding the video wins."""
        if key:
            resolved = self.resolve_playlist(key)
            candidates = [resolved] if resolved is not None else []
        else:
            candidates = self._records()
        for ref, _meta_dir, doc in candidates:
            if any(str(raw.get("id")) == video_id for raw in doc.get("entries") or []):
                video_dir = self._video_dir(ref.layout, str(doc.get("id") or ""), video_id)
                if video_dir is not None:
                    return ref, video_dir
        return None

    def video_dir(self, key: str, video_id: str) -> Path | None:
        located = self.locate_video(key, video_id)
        return located[1] if located is not None else None

    def media_path(self, key: str, video_id: str) -> Path | None:
        located = self.locate_video(key, video_id)
        if located is None:
            return None
        ref, video_dir = located
        return find_media(ref.layout, video_dir)

    def audio_path(
        self, key: str, video_id: str, track_id: str
    ) -> Path | None:
        located = self.locate_video(key, video_id)
        if located is None:
            return None
        ref, video_dir = located
        meta = read_json(video_dir / "meta.json") or {}
        video_dir = _audio_home(ref.layout, video_dir, meta)
        for track in meta.get("audio_tracks") or []:
            if not isinstance(track, dict):
                continue
            if str(track.get("id") or "") != track_id:
                continue
            path = _safe_relative(video_dir, track.get("file"))
            if path is None or not path.is_file():
                return None
            return path
        return None

    def preview_cache_path(
        self, key: str, video_id: str, seconds: float
    ) -> Path | None:
        """Where a scrubber frame is cached; None for a read-only library."""
        resolved = self.resolve_playlist(key)
        if resolved is None or resolved[0].read_only:
            return None
        ref = resolved[0]
        playlist_id = str(resolved[2].get("id") or "")
        try:
            root = (
                ref.layout.cache_dir / "previews"
                / safe_id(playlist_id) / safe_id(video_id)
            )
        except ValueError:
            return None
        return root / f"{max(0, int(seconds)):07d}.jpg"

    def thumbnail_path(self, key: str, video_id: str) -> Path | None:
        video_dir = self.video_dir(key, video_id)
        if video_dir is None:
            return None
        meta = read_json(video_dir / "meta.json") or {}
        name = (meta.get("files") or {}).get("thumbnail")
        path = _safe_relative(video_dir, name)
        return path if path is not None and path.is_file() else None

    def playlist_thumbnail_path(self, key: str) -> Path | None:
        resolved = self.resolve_playlist(key)
        if resolved is None:
            return None
        _ref, meta_dir, doc = resolved
        name = (doc.get("files") or {}).get("thumbnail")
        path = _safe_relative(meta_dir, name)
        return path if path is not None and path.is_file() else None

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _media_type(media: Path | None) -> str:
        if media is None:
            return "application/octet-stream"
        return {
            ".webm": "video/webm",
            ".mp4": "video/mp4",
            ".m4v": "video/mp4",
            ".mkv": "video/x-matroska",
            ".mov": "video/quicktime",
        }.get(media.suffix.lower(), "application/octet-stream")

    @staticmethod
    def _chapters(raw: list) -> list[Chapter]:
        chapters: list[Chapter] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                start = float(item.get("start_time"))
            except (TypeError, ValueError):
                continue
            end = item.get("end_time")
            try:
                end = float(end) if end is not None else None
            except (TypeError, ValueError):
                end = None
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            chapters.append(Chapter(
                start=start,
                end=end,
                title=title,
                time_label=format_duration(start),
                duration_label=format_duration(end - start)
                if end and end > start else None,
            ))
        chapters.sort(key=lambda c: c.start)
        return chapters
