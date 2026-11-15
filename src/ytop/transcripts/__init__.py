"""Caption acquisition, transcript storage and FTS search (P4)."""

from .captions import Caption, CaptionError, Segment, choose_caption
from .fetch import TranscriptResult, fetch_playlist_transcripts
from .store import index_library, read_transcript, search, segments_from_doc

__all__ = [
    "Caption",
    "CaptionError",
    "Segment",
    "TranscriptResult",
    "choose_caption",
    "fetch_playlist_transcripts",
    "index_library",
    "read_transcript",
    "search",
    "segments_from_doc",
]
