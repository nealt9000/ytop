"""Resolve a CLI target to a downloaded playlist or video.

No network access happens here: commands that operate on the local library
accept a playlist URL/id, a watch URL, or a bare 11-character video id.  URLs
with ``list=`` are treated as playlists; URLs with only ``v=`` are videos.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .layout import LibraryLayout
from .yt.metadata import read_json, video_directory

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")


class TargetError(ValueError):
    """The target string or local library lookup is invalid."""


@dataclass(frozen=True)
class Target:
    kind: str  # playlist | video
    id: str


def parse_target(value: str, *, prefer_video: bool = False) -> Target:
    value = (value or "").strip()
    if not value:
        raise TargetError("empty target")

    if value.startswith(("http://", "https://")):
        parsed = urlparse(value)
        query = parse_qs(parsed.query)
        playlist_ids = query.get("list") or []
        video_ids = query.get("v") or []
        path = parsed.path.strip("/")
        path_video = ""
        if parsed.netloc.endswith("youtu.be") and VIDEO_ID_RE.fullmatch(path):
            path_video = path
        else:
            for prefix in ("shorts/", "embed/", "live/"):
                if path.startswith(prefix):
                    candidate = path[len(prefix):].split("/", 1)[0]
                    if VIDEO_ID_RE.fullmatch(candidate):
                        path_video = candidate
                        break
        video_id = (video_ids[0] if video_ids else "") or path_video
        if prefer_video and video_id:
            return Target("video", video_id)
        if playlist_ids and playlist_ids[0]:
            return Target("playlist", playlist_ids[0])
        if video_id:
            return Target("video", video_id)
        raise TargetError(f"no playlist or video id in URL: {value!r}")

    if VIDEO_ID_RE.fullmatch(value):
        return Target("video", value)
    if PLAYLIST_ID_RE.fullmatch(value):
        return Target("playlist", value)
    raise TargetError(f"not a playlist/video id or URL: {value!r}")


def library_playlists(layout: LibraryLayout):
    """Yield ``(playlist meta dir, playlist doc)`` for one library."""
    root = layout.meta_root
    if not root.is_dir():
        return
    seen: set[str] = set()
    for playlist_dir in sorted(root.iterdir()):
        if not playlist_dir.is_dir() or playlist_dir.name.startswith("."):
            continue
        doc = read_json(playlist_dir / "playlist.json")
        if doc and str(doc.get("id")) not in seen:
            seen.add(str(doc.get("id")))
            yield playlist_dir, doc


def resolve_playlist(
    layout: LibraryLayout, playlist_id: str
) -> tuple[Path, dict] | None:
    for playlist_dir, doc in library_playlists(layout):
        if str(doc.get("id")) == playlist_id:
            return playlist_dir, doc
    return None


def video_dir_for_entry(layout: LibraryLayout, playlist: dict, entry: dict) -> Path:
    return video_directory(
        layout, str(playlist.get("id") or ""), str(entry.get("id") or "")
    )


def resolve_video(
    layout: LibraryLayout, video_id: str
) -> tuple[Path, dict, dict, Path, dict] | None:
    """Return ``(playlist_dir, playlist_doc, entry, video_dir, meta)``."""
    for playlist_dir, doc in library_playlists(layout):
        for entry in doc.get("entries") or []:
            if str(entry.get("id")) != video_id:
                continue
            vdir = video_dir_for_entry(layout, doc, entry)
            meta = read_json(vdir / "meta.json") or {}
            return playlist_dir, doc, entry, vdir, meta

    # An orphaned video directory (playlist.json missing or edited) is still
    # readable by id.
    for playlist_dir, doc in library_playlists(layout):
        try:
            vdir = video_dir_for_entry(layout, doc, {"id": video_id})
        except ValueError:
            return None
        meta = read_json(vdir / "meta.json")
        if meta:
            return playlist_dir, doc, {"id": video_id}, vdir, meta
    return None
