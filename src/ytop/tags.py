"""Offline tag normalisation, derivation and categories for playlists."""

from __future__ import annotations

import math
import re
import threading
import unicodedata
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from .layout import LibraryLayout
from .yt.metadata import (
    CorruptDocumentError,
    document_lock,
    read_json_document,
    write_json,
)

UNCATEGORIZED = "Uncategorized"
MAX_TAGS = 5
MIN_SHARE = 0.25
STOPWORDS = frozenset(
    {"video", "videos", "youtube", "hd", "4k", "1080p", "official", "full", "new"}
)

_DROPPED_CHARS = frozenset('<>"`{}|')  # | separates keys in data-tags
_DROPPED_CATEGORIES = frozenset({"So", "Cc", "Cf"})
_ONLY_NOISE = re.compile(r"^[\d\W_]+$")


@dataclass(frozen=True)
class Tag:
    key: str
    label: str


@dataclass(frozen=True)
class TagInput:
    playlist_id: str
    title: str
    description: str
    channel: str
    video_tags: tuple[tuple[str, ...], ...]  # one inner tuple per video


def _clean(text: str) -> str:
    return "".join(
        ch
        for ch in text
        if ch not in _DROPPED_CHARS and unicodedata.category(ch) not in _DROPPED_CATEGORIES
    )


def _strip_punct(text: str) -> str:
    start, end = 0, len(text)
    while start < end and (text[start].isspace() or unicodedata.category(text[start]).startswith("P")):
        start += 1
    while end > start and (text[end - 1].isspace() or unicodedata.category(text[end - 1]).startswith("P")):
        end -= 1
    return text[start:end]


def _normalize_text(text: str) -> str:
    """Normalise free text the same way as a tag, without trimming the ends."""
    text = _clean(unicodedata.normalize("NFKC", _clean(text))).casefold()
    return " ".join(text.replace("_", " ").split())


def normalize_tag(raw: str) -> str:
    """Return the tag key, or "" if unusable."""
    text = _normalize_text(raw)
    text = text.lstrip("#")
    text = " ".join(text.replace("_", " ").split())
    return _strip_punct(text)


def is_junk(key: str, channel_key: str) -> bool:
    if len(key) < 2 or len(key) > 32:
        return True
    if len(key.split()) > 4:
        return True
    if _ONLY_NOISE.match(key):
        return True
    if key in STOPWORDS:
        return True
    return bool(channel_key) and key in channel_key


def _label(raw: str) -> str:
    return _clean(raw).strip()


def derive_tags(inputs: Sequence[TagInput]) -> dict[str, list[Tag]]:
    # Vocabulary of normalised keys across the whole call, for plural merging.
    raw_vocab = {
        normalize_tag(t) for inp in inputs for video in inp.video_tags for t in video
    }
    raw_vocab.discard("")

    def merge(key: str) -> str:
        if key.endswith("s") and not key.endswith("ss") and len(key) > 3 and key[:-1] in raw_vocab:
            return key[:-1]
        return key

    label_counts: dict[str, Counter[str]] = {}
    per_playlist: list[tuple[TagInput, str, list[set[str]]]] = []
    for inp in inputs:
        channel_key = normalize_tag(inp.channel)
        videos: list[set[str]] = []
        for video in inp.video_tags:
            keys: set[str] = set()
            for raw in video:
                key = normalize_tag(raw)
                if not key:
                    continue
                key = merge(key)
                label_counts.setdefault(key, Counter())[_label(raw)] += 1
                if not is_junk(key, channel_key):
                    keys.add(key)
            videos.append(keys)
        per_playlist.append((inp, channel_key, videos))

    vocabulary = sorted(label_counts)
    candidates: list[tuple[Counter[str], set[str]]] = []
    for inp, channel_key, videos in per_playlist:
        counts: Counter[str] = Counter()
        for keys in videos:
            counts.update(keys)
        text = _normalize_text(f"{inp.title} {inp.description}")
        from_text = {
            key
            for key in vocabulary
            if not is_junk(key, channel_key)
            and re.search(rf"(?<!\w){re.escape(key)}(?!\w)", text)
        }
        candidates.append((counts, from_text))

    df: Counter[str] = Counter()
    n_with = 0
    for counts, from_text in candidates:
        keys = set(counts) | from_text
        if keys:
            n_with += 1
        df.update(keys)

    result: dict[str, list[Tag]] = {}
    for (inp, _channel_key, videos), (counts, from_text) in zip(per_playlist, candidates):
        n_videos = len(videos)
        scored: list[tuple[float, str]] = []
        for key in set(counts) | from_text:
            in_text = key in from_text
            tf = 1.0 if in_text else counts[key] / n_videos
            if tf < MIN_SHARE:
                continue
            if n_videos >= 4 and not in_text and counts[key] < 2:
                continue
            idf = math.log((1 + n_with) / (1 + df[key])) + 1
            scored.append((tf * idf, key))
        scored.sort(key=lambda item: (-item[0], item[1]))
        result[inp.playlist_id] = [
            Tag(key, _best_label(label_counts[key])) for _, key in scored[:MAX_TAGS]
        ]
    return result


def _best_label(counts: Counter[str]) -> str:
    return min(counts, key=lambda lab: (-counts[lab], lab))


def playlist_category(video_categories: Sequence[Sequence[str]]) -> str:
    counts = Counter(cat for cats in video_categories for cat in cats)
    if not counts:
        return UNCATEGORIZED
    return min(counts, key=lambda cat: (-counts[cat], cat))


# -- user edits ---------------------------------------------------------------

MAX_LABEL = 32
MAX_RENAME_HOPS = 8


@dataclass
class PlaylistTagEdit:
    add: list[str] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)
    category: str | None = None


@dataclass
class TagEdits:
    renames: dict[str, str] = field(default_factory=dict)
    hidden: set[str] = field(default_factory=set)
    playlists: dict[str, PlaylistTagEdit] = field(default_factory=dict)


def _clean_label(label: str) -> str:
    return " ".join(_clean(label).split())[:MAX_LABEL].strip()


def _str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _parse_edits(data: object) -> TagEdits:
    edits = TagEdits()
    if not isinstance(data, dict):
        return edits
    renames = data.get("renames")
    if isinstance(renames, dict):
        edits.renames = {
            str(k): v for k, v in renames.items() if isinstance(v, str) and v
        }
    edits.hidden = set(_str_list(data.get("hidden")))
    playlists = data.get("playlists")
    if isinstance(playlists, dict):
        for pid, row in playlists.items():
            if not isinstance(row, dict):
                continue
            category = row.get("category")
            edits.playlists[str(pid)] = PlaylistTagEdit(
                add=_str_list(row.get("add")),
                remove=_str_list(row.get("remove")),
                category=category if isinstance(category, str) and category else None,
            )
    return edits


class TagStore:
    """User tag edits that always beat derived tags.

    ``tags.json``::

        {"schema": 1,
         "renames": {"<key>": "<new label>"},
         "hidden": ["<key>"],
         "playlists": {"<id>": {"add": ["Label"], "remove": ["<key>"],
                                "category": "Music"}},
         "videos": {}}
    """

    def __init__(self, layout: LibraryLayout):
        self.layout = layout
        self.path = layout.tags_file
        self._lock = threading.Lock()

    def read(self) -> TagEdits:
        document = read_json_document(self.path)
        if document.corrupt or document.status == "unreadable":
            return TagEdits()
        return _parse_edits(document.data)

    def _update(self, change: Callable[[TagEdits], None]) -> None:
        with self._lock, document_lock(self.path):
            document = read_json_document(self.path)
            if document.corrupt or document.status == "unreadable":
                raise CorruptDocumentError(self.path, document.status, document.detail)
            raw = document.data if isinstance(document.data, dict) else {}
            edits = _parse_edits(raw)
            change(edits)
            out = {k: v for k, v in raw.items() if k not in ("renames", "hidden", "playlists")}
            videos = out.get("videos")
            out["schema"] = 1
            out["videos"] = videos if isinstance(videos, dict) else {}
            out["renames"] = dict(sorted(edits.renames.items()))
            out["hidden"] = sorted(edits.hidden)
            out["playlists"] = {
                pid: {
                    "add": row.add,
                    "remove": row.remove,
                    **({"category": row.category} if row.category else {}),
                }
                for pid, row in edits.playlists.items()
                if row.add or row.remove or row.category
            }
            write_json(self.path, out)

    def rename(self, key: str, label: str) -> None:
        """Rename (or merge) ``key``; an empty label undoes its rename.

        Renaming a tag back into one that was merged into it (fugue →
        Counterpoint, then counterpoint → Fugue) undoes the old merge instead
        of leaving a loop, so keys and labels never get swapped.
        """
        clean = _clean_label(label)
        target = normalize_tag(clean)

        def change(edits: TagEdits) -> None:
            if clean and target and clean != key:
                edits.renames[key] = clean
                if target != key and _reaches(target, key, edits.renames):
                    edits.renames.pop(target, None)
            else:
                edits.renames.pop(key, None)

        self._update(change)

    def hide(self, key: str) -> None:
        key = normalize_tag(key)
        if key:
            self._update(lambda edits: edits.hidden.add(key))

    def unhide(self, key: str) -> None:
        key = normalize_tag(key)
        if key:
            self._update(lambda edits: edits.hidden.discard(key))

    def add(self, playlist_id: str, label: str) -> None:
        clean = _clean_label(label)
        key = normalize_tag(clean)

        def change(edits: TagEdits) -> None:
            if not key:
                return
            row = edits.playlists.setdefault(playlist_id, PlaylistTagEdit())
            row.remove = [k for k in row.remove if k != key]
            row.add = [x for x in row.add if normalize_tag(x) != key] + [clean]

        self._update(change)

    def remove(self, playlist_id: str, key: str) -> None:
        key = normalize_tag(key)
        if not key:
            return

        def change(edits: TagEdits) -> None:
            row = edits.playlists.setdefault(playlist_id, PlaylistTagEdit())
            row.add = [x for x in row.add if normalize_tag(x) != key]
            if key not in row.remove:
                row.remove.append(key)

        self._update(change)

    def set_category(self, playlist_id: str, category: str | None) -> None:
        clean = _clean_label(category or "")

        def change(edits: TagEdits) -> None:
            row = edits.playlists.setdefault(playlist_id, PlaylistTagEdit())
            row.category = clean or None

        self._update(change)


def _reaches(start: str, goal: str, renames: dict[str, str]) -> bool:
    """Following renames from ``start`` arrives at ``goal``."""
    key = start
    for _ in range(MAX_RENAME_HOPS):
        target = renames.get(key)
        if target is None:
            return False
        next_key = normalize_tag(target)
        if next_key == goal:
            return True
        if not next_key or next_key == key:
            return False
        key = next_key
    return False


def _follow_rename(tag: Tag, renames: dict[str, str]) -> Tag:
    key, label = tag.key, tag.label
    seen = {key}
    for _ in range(MAX_RENAME_HOPS):
        target = renames.get(key)
        if target is None:
            break
        label = target
        new_key = normalize_tag(target)
        if not new_key or new_key == key or new_key in seen:
            break
        key = new_key
        seen.add(key)
    return Tag(key or tag.key, label)


def _finish(
    tags: Sequence[Tag], edits: TagEdits, removed: frozenset[str] = frozenset()
) -> list[Tag]:
    out: list[Tag] = []
    seen: set[str] = set()
    for tag in tags:
        tag = _follow_rename(tag, edits.renames)
        if tag.key in edits.hidden or tag.key in removed or tag.key in seen:
            continue
        seen.add(tag.key)
        out.append(tag)
    return out


def apply_edits(playlist_id: str, derived: list[Tag], edits: TagEdits) -> list[Tag]:
    row = edits.playlists.get(playlist_id) or PlaylistTagEdit()
    removed = frozenset(row.remove)
    tags = list(derived)
    tags += [Tag(normalize_tag(x), x) for x in row.add if normalize_tag(x)]
    # Drop removed keys both before and after renames, so either name works.
    tags = [t for t in tags if t.key not in removed]
    return _finish(tags, edits, removed)


def effective_category(playlist_id: str, derived: str, edits: TagEdits) -> str:
    row = edits.playlists.get(playlist_id)
    return row.category if row and row.category else derived


def video_tags(
    playlist_tags: list[Tag],
    yt_tags: Sequence[str],
    vocabulary: dict[str, Tag],
    edits: TagEdits,
) -> list[Tag]:
    tags = list(playlist_tags)
    for raw in yt_tags:
        known = vocabulary.get(normalize_tag(raw))
        if known is not None:
            tags.append(known)
    return _finish(tags, edits)
