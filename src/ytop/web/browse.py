"""View models for the library home page, video lists and search.

Pure data: no HTML.  Playlists come from each library's index, then the
per-library overlays (title overrides, watch progress, tag edits) are laid on.
"""

from __future__ import annotations

import html
import re
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import quote

from ..index import PlaylistRow, VideoRow, index_for
from ..libraries import LibraryRef
from ..tags import Tag, TagEdits, TagStore, apply_edits, effective_category, video_tags
from ..transcripts.store import query_tokens
from ..transcripts.store import search as search_transcripts
from ..yt.metadata import human_bytes
from .library import Library, playlist_key
from .render import format_duration, format_timestamp, thumb_url

CARD_LIMIT = 12
TOP_LIMIT = 5


@dataclass
class PlaylistCard:
    key: str
    library_id: str
    library_label: str
    playlist_id: str
    kind: str
    title: str
    channel: str
    category: str
    tags: list[Tag]
    thumb_url: str
    video_count: int
    available_count: int
    watched_count: int
    progress: float
    total_duration: float
    remaining_duration: float
    total_bytes: int
    updated_at: str
    last_watched_at: str
    next_url: str | None
    next_label: str
    next_title: str
    next_position: float

    @property
    def is_ungrouped(self) -> bool:
        return self.kind == "ungrouped"


@dataclass
class VideoCard:
    key: str
    playlist_title: str
    video_id: str
    title: str
    channel: str
    duration_label: str
    thumb_url: str
    url: str
    watched: bool
    fraction: float
    category: str
    tags: list[Tag]
    library_label: str
    available: bool = True


@dataclass
class LibrarySummary:
    id: str
    label: str
    description: str
    read_only: bool
    playlist_count: int
    video_count: int
    total_hours: float
    watched_hours: float
    unwatched_hours: float
    used_bytes: int
    free_bytes: int
    top_categories: list[tuple[str, int]]
    top_channels: list[tuple[str, int]]


@dataclass
class HomeView:
    summaries: list[LibrarySummary]
    continue_watching: list[PlaylistCard]
    start_new: list[PlaylistCard]
    all_playlists: list[PlaylistCard]
    categories: list[str]
    tags: list[Tag]
    channels: list[str]


@dataclass
class _Loaded:
    """One library's index rows with its overlays, read once per call."""

    ref: LibraryRef
    rows: list[PlaylistRow]
    progress: dict
    video_titles: dict
    playlist_titles: dict
    edits: TagEdits
    derived: dict
    vocabulary: dict[str, Tag]
    cards: list[PlaylistCard] = field(default_factory=list)


def _load(lib: Library, ref: LibraryRef) -> _Loaded:
    index = index_for(ref.layout, read_only=ref.read_only)
    index.ensure_fresh()
    titles = lib.titles_for(ref).read()
    return _Loaded(
        ref=ref,
        rows=index.playlists(),
        progress=lib.progress_for(ref).read(),
        video_titles=titles.get("videos") or {},
        playlist_titles=titles.get("playlists") or {},
        edits=TagStore(ref.layout).read(),
        derived=index.derived(),
        vocabulary=index.vocabulary(),
    )


def _load_all(lib: Library, library_id: str | None) -> tuple[list[_Loaded], Counter]:
    """Every online library, loaded once.  Keys count playlist ids across all
    of them (as Library._keyed does), even when only one is shown."""
    everything = [_load(lib, ref) for ref in lib.libraries.libraries()]
    counts: Counter = Counter()
    for item in everything:
        counts.update({row.playlist_id for row in item.rows})
    chosen = [item for item in everything if item.ref.id == library_id]
    return (chosen or everything), counts


def _num(value: object) -> float:
    try:
        return max(0.0, float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _state(progress: dict, video_id: str) -> tuple[float, bool, str]:
    row = progress.get(video_id)
    if not isinstance(row, dict):
        return 0.0, False, ""
    return (
        _num(row.get("position")),
        bool(row.get("watched")),
        str(row.get("updated_at") or ""),
    )


def _card(item: _Loaded, row: PlaylistRow, key: str) -> PlaylistCard:
    available = [v for v in row.videos if v.available]
    watched = 0
    total_duration = remaining = 0.0
    last = ""
    # Resume the video most recently left part-way; with none, start the
    # first unwatched one in playlist order.
    first: tuple[int, VideoRow, float] | None = None
    resume: tuple[str, int, VideoRow, float] | None = None
    for number, video in enumerate(row.videos, start=1):
        position, done, stamp = _state(item.progress, video.video_id)
        if stamp > last:
            last = stamp
        if not video.available:
            continue
        duration = video.duration or 0.0
        total_duration += duration
        if done:
            watched += 1
            continue
        remaining += duration
        if first is None:
            first = (number, video, position)
        if position > 0 and (resume is None or stamp > resume[0]):
            resume = (stamp, number, video, position)
    next_video: VideoRow | None = None
    next_index = 0
    next_position = 0.0
    if resume is not None:
        _stamp, next_index, next_video, next_position = resume
    elif first is not None:
        next_index, next_video, next_position = first
    media = {v.media_ref: v.media_bytes for v in row.videos if v.media_ref}
    derived = item.derived.get(row.playlist_id)
    tags = apply_edits(
        row.playlist_id, list(derived.tags) if derived else [], item.edits
    )
    category = effective_category(
        row.playlist_id, derived.category if derived else "", item.edits
    )
    next_url = f"/watch/{quote(key)}/{quote(next_video.video_id)}" if next_video else None
    next_title = ""
    if next_video is not None:
        next_title = item.video_titles.get(next_video.video_id) or next_video.title
    return PlaylistCard(
        key=key,
        library_id=item.ref.id,
        library_label=item.ref.label,
        playlist_id=row.playlist_id,
        kind=row.kind,
        title=item.playlist_titles.get(row.playlist_id) or row.title or row.playlist_id,
        channel=row.channel,
        category=category,
        tags=tags,
        thumb_url=f"/media/{quote(key)}/thumb" if row.has_thumb else "",
        video_count=len(row.videos),
        available_count=len(available),
        watched_count=watched,
        progress=watched / len(available) if available else 0.0,
        total_duration=total_duration,
        remaining_duration=remaining,
        total_bytes=sum(media.values()) + row.meta_bytes,
        updated_at=row.updated_at,
        last_watched_at=last,
        next_url=next_url,
        next_label=f"Video {next_index} of {len(row.videos)}" if next_video else "",
        next_title=next_title,
        next_position=next_position,
    )


def _build_cards(lib: Library, library_id: str | None) -> list[_Loaded]:
    loaded, counts = _load_all(lib, library_id)
    for item in loaded:
        item.cards = [
            _card(item, row, playlist_key(row.playlist_id, item.ref.id, counts[row.playlist_id] > 1))
            for row in item.rows
        ]
    return loaded


def cards_for(lib: Library, library_id: str | None = None) -> list[PlaylistCard]:
    return [card for item in _build_cards(lib, library_id) for card in item.cards]


def _used_bytes(rows: list[PlaylistRow]) -> int:
    """Media (a file several playlists share counted once) plus metadata."""
    media: dict[str, int] = {}
    for row in rows:
        for video in row.videos:
            if video.media_ref:
                media[video.media_ref] = video.media_bytes
    return sum(media.values()) + sum(row.meta_bytes for row in rows)


def storage_usage(lib: Library) -> tuple[dict[str, int], int]:
    """Playlists per online library, and bytes used across them, from the
    index (no meta.json is opened)."""
    loaded, _counts = _load_all(lib, None)
    counts = {item.ref.id: len(item.rows) for item in loaded if item.rows}
    return counts, sum(_used_bytes(item.rows) for item in loaded)


def library_videos(lib: Library) -> list[dict]:
    """Every video in every online library, for building a new playlist.

    Read from the index (no meta.json is opened).  The ``key`` form value is
    ``"<playlist key>:<video id>"``, as ``create_playlist`` expects.  The
    index holds no quality or audio details, so those labels are empty.
    """
    rows: list[dict] = []
    for item in _build_cards(lib, None):
        for row, card in zip(item.rows, item.cards):
            for video in row.videos:
                _position, done, _stamp = _state(item.progress, video.video_id)
                rows.append({
                    "key": f"{card.key}:{video.video_id}",
                    "playlist_id": card.key,
                    "playlist_title": card.title,
                    "id": video.video_id,
                    "title": item.video_titles.get(video.video_id) or video.title,
                    "channel": video.channel,
                    "duration": video.duration,
                    "duration_label": format_duration(video.duration),
                    "thumb_url": thumb_url(card.key, video.video_id),
                    "available": video.available,
                    "watched": done,
                    "quality_label": "",
                    "size_label": (
                        human_bytes(video.media_bytes)
                        if video.available and video.media_bytes else ""
                    ),
                    "audio_detail": "",
                    "library_id": card.library_id,
                    "library_label": card.library_label,
                    "source": card.title,
                })
    rows.sort(key=lambda row: (
        not row["available"], row["source"].lower(), row["title"].lower()
    ))
    return rows


def _started(item: _Loaded, row: PlaylistRow) -> bool:
    for video in row.videos:
        position, done, _stamp = _state(item.progress, video.video_id)
        if position > 0 or done:
            return True
    return False


def _top(values: list[str]) -> list[tuple[str, int]]:
    counts = Counter(value for value in values if value)
    return sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:TOP_LIMIT]


def _summary(item: _Loaded) -> LibrarySummary:
    seen: set[str] = set()
    total = watched = 0.0
    for row in item.rows:
        for video in row.videos:
            if video.video_id in seen:
                continue
            seen.add(video.video_id)
            duration = video.duration or 0.0
            total += duration
            if _state(item.progress, video.video_id)[1]:
                watched += duration
    info = item.ref.info
    return LibrarySummary(
        id=item.ref.id,
        label=item.ref.label,
        description=getattr(info, "description", "") or "",
        read_only=item.ref.read_only,
        playlist_count=len(item.rows),
        video_count=len(seen),
        total_hours=total / 3600,
        watched_hours=watched / 3600,
        unwatched_hours=(total - watched) / 3600,
        used_bytes=_used_bytes(item.rows),
        free_bytes=item.ref.free_bytes,
        top_categories=_top([card.category for card in item.cards]),
        top_channels=_top([card.channel for card in item.cards]),
    )


def build_home(lib: Library, library_id: str | None = None) -> HomeView:
    loaded = _build_cards(lib, library_id)
    continuing: list[PlaylistCard] = []
    fresh: list[PlaylistCard] = []
    for item in loaded:
        for row, card in zip(item.rows, item.cards):
            if not _started(item, row):
                fresh.append(card)
            elif card.next_url is not None:
                continuing.append(card)
    continuing.sort(key=lambda card: card.last_watched_at, reverse=True)
    fresh.sort(key=lambda card: card.updated_at, reverse=True)
    all_cards = [card for item in loaded for card in item.cards]
    tags: dict[str, Tag] = {}
    for card in all_cards:
        for tag in card.tags:
            tags.setdefault(tag.key, tag)
    return HomeView(
        summaries=[_summary(item) for item in loaded],
        continue_watching=continuing[:CARD_LIMIT],
        start_new=fresh[:CARD_LIMIT],
        all_playlists=all_cards,
        categories=sorted({c.category for c in all_cards if c.category}),
        tags=sorted(tags.values(), key=lambda tag: tag.label.casefold()),
        channels=sorted({c.channel for c in all_cards if c.channel}, key=str.casefold),
    )


def build_videos(
    lib: Library,
    *,
    library_id: str | None = None,
    tag: str = "",
    category: str = "",
    channel: str = "",
) -> list[tuple[PlaylistCard, list[VideoCard]]]:
    wanted_channel = channel.casefold()
    groups: list[tuple[PlaylistCard, list[VideoCard]]] = []
    for item in _build_cards(lib, library_id):
        for row, card in zip(item.rows, item.cards):
            if category and card.category != category:
                continue
            videos: list[VideoCard] = []
            for video in row.videos:
                video_channel = video.channel or card.channel
                if wanted_channel and video_channel.casefold() != wanted_channel:
                    continue
                tags = video_tags(card.tags, video.yt_tags, item.vocabulary, item.edits)
                if tag and tag not in {t.key for t in tags}:
                    continue
                position, done, _stamp = _state(item.progress, video.video_id)
                duration = video.duration or 0.0
                videos.append(
                    VideoCard(
                        key=card.key,
                        playlist_title=card.title,
                        video_id=video.video_id,
                        title=item.video_titles.get(video.video_id) or video.title,
                        channel=video_channel,
                        duration_label=format_duration(video.duration),
                        thumb_url=thumb_url(card.key, video.video_id),
                        url=f"/watch/{quote(card.key)}/{quote(video.video_id)}",
                        watched=done,
                        fraction=1.0 if done else (min(1.0, position / duration) if duration else 0.0),
                        category=card.category,
                        tags=tags,
                        library_label=card.library_label,
                        available=video.available,
                    )
                )
            if videos:
                groups.append((card, videos))
    return groups


SEARCH_QUERY_MAX = 200
GROUP_HIT_LIMIT = 10
META_FETCH = 400
TRANSCRIPT_FETCH = 200


@dataclass
class SearchGroup:
    card: PlaylistCard
    tag_matches: list[Tag]
    video_hits: list[dict]
    transcript_hits: list[dict]
    playlist_matched: bool = False
    rank: float = 0.0


def _mark(text: str, tokens: list[str]) -> str:
    """``text`` escaped, with each token occurrence in <mark>."""
    pattern = re.compile("|".join(re.escape(t) for t in tokens), re.IGNORECASE)
    out: list[str] = []
    last = 0
    for found in pattern.finditer(text):
        out.append(html.escape(text[last:found.start()], quote=False))
        out.append("<mark>" + html.escape(found.group(0), quote=False) + "</mark>")
        last = found.end()
    out.append(html.escape(text[last:], quote=False))
    return "".join(out)


def search_library(
    lib: Library, q: str, *, playlist: str | None = None
) -> list[SearchGroup]:
    """Titles, descriptions, channels, tags and transcripts, grouped by
    playlist.  Playlists whose title or tags matched come first, then the
    rest by best rank (bm25, lower is better, for both sources)."""
    q = (q or "")[:SEARCH_QUERY_MAX]
    tokens = [t.casefold() for t in query_tokens(q)]
    if not tokens:
        return []
    only = lib.resolve_playlist(playlist) if playlist else None
    only_id = str(only[2].get("id")) if only else None
    groups: dict[tuple[str, str], SearchGroup] = {}
    cards: dict[tuple[str, str], PlaylistCard] = {}
    loaded = _build_cards(lib, None)
    for item in loaded:
        if playlist and (only is None or only[0].id != item.ref.id):
            continue
        for card in item.cards:
            if only_id and card.playlist_id != only_id:
                continue
            cards[(card.library_id, card.playlist_id)] = card

    def group(library_id: str, playlist_id: str) -> SearchGroup | None:
        card = cards.get((library_id, playlist_id))
        if card is None:
            return None  # not online (or outside the playlist filter)
        found = groups.get((library_id, playlist_id))
        if found is None:
            found = groups[(library_id, playlist_id)] = SearchGroup(
                card=card, tag_matches=[], video_hits=[], transcript_hits=[],
                rank=float("inf"),
            )
        return found

    def url(card: PlaylistCard, video_id: str, available: bool) -> str | None:
        """No watch link for a video that is not downloaded (as on /videos)."""
        if not available:
            return None
        return f"/watch/{quote(card.key)}/{quote(video_id)}"

    downloaded: dict[str, set[tuple[str, str]]] = {}
    for item in loaded:
        if not any(key[0] == item.ref.id for key in cards):
            continue
        index = index_for(item.ref.layout, read_only=item.ref.read_only)
        titles = {
            (row.playlist_id, v.video_id): v.title
            for row in item.rows for v in row.videos
        }
        available = downloaded[item.ref.id] = {
            (row.playlist_id, v.video_id)
            for row in item.rows for v in row.videos if v.available
        }
        overridden: set[tuple[str, str]] = set()
        # Custom titles match even where the stored title does not.
        for row in item.rows:
            custom = item.playlist_titles.get(row.playlist_id)
            if custom and all(t in str(custom).casefold() for t in tokens):
                found = group(item.ref.id, row.playlist_id)
                if found is not None:
                    found.playlist_matched = True
                    found.rank = min(found.rank, 0.0)
            for video in row.videos:
                custom = item.video_titles.get(video.video_id)
                if custom and all(t in str(custom).casefold() for t in tokens):
                    found = group(item.ref.id, row.playlist_id)
                    if found is not None:
                        overridden.add((row.playlist_id, video.video_id))
                        found.video_hits.append({
                            "video_id": video.video_id, "title": str(custom),
                            "field": "title", "html": _mark(str(custom), tokens),
                            "url": url(found.card, video.video_id, video.available),
                            "available": video.available, "rank": 0.0,
                        })
                        found.rank = min(found.rank, 0.0)
        for hit in index.search_meta(q, META_FETCH):
            found = group(item.ref.id, hit.playlist_id)
            if found is None:
                continue
            found.rank = min(found.rank, hit.rank)
            if hit.playlist_match:
                found.playlist_matched = True
            if hit.field == "playlist":
                continue
            if (hit.playlist_id, hit.video_id) in overridden:
                continue
            title = item.video_titles.get(hit.video_id) or titles.get(
                (hit.playlist_id, hit.video_id), hit.video_id
            )
            is_available = (hit.playlist_id, hit.video_id) in available
            found.video_hits.append({
                "video_id": hit.video_id, "title": title, "field": hit.field,
                "html": hit.html, "url": url(found.card, hit.video_id, is_available),
                "available": is_available, "rank": hit.rank,
            })

    for card in cards.values():
        matched = [
            tag for tag in card.tags
            if all(t in tag.label.casefold() for t in tokens)
        ]
        if matched:
            found = group(card.library_id, card.playlist_id)
            if found is not None:
                found.tag_matches = matched

    refs = {item.ref.id: item for item in loaded}
    layouts = [refs[lid].ref.layout for lid in {key[0] for key in cards}]
    for trow in search_transcripts(
        layouts, q, TRANSCRIPT_FETCH, playlist_id=only_id
    ):
        found = group(trow["library_id"], trow["playlist_id"])
        if found is None:
            continue
        item = refs[trow["library_id"]]
        trow["title"] = item.video_titles.get(trow["video_id"]) or trow["title"]
        trow["playlist_id"] = found.card.key
        trow["playlist_title"] = found.card.title
        watch = url(
            found.card, trow["video_id"],
            (found.card.playlist_id, trow["video_id"])
            in downloaded.get(trow["library_id"], set()),
        )
        trow["url"] = f"{watch}?t={trow['start']:.3f}#transcript" if watch else None
        trow["time_label"] = format_timestamp(trow["start"])
        found.transcript_hits.append(trow)
        found.rank = min(found.rank, float(trow["rank"]))

    out = []
    for found in groups.values():
        found.video_hits = found.video_hits[:GROUP_HIT_LIMIT]
        found.transcript_hits = found.transcript_hits[:GROUP_HIT_LIMIT]
        if found.video_hits or found.transcript_hits or found.tag_matches or found.playlist_matched:
            out.append(found)
    out.sort(key=lambda g: (not (g.playlist_matched or g.tag_matches), g.rank))
    return out
