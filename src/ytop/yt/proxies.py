"""Region-blocked videos: recognising them and keeping YouTube's country list."""

from __future__ import annotations

import re

from yt_dlp.utils import DownloadError, GeoRestrictedError

REGION_BLOCKED = re.compile(
    r"not made this video available in your country"
    r"|not available from your location"
    r"|not available in your country",
    re.IGNORECASE,
)


class RegionBlockedError(DownloadError):
    """YouTube does not offer the video in this country; ``countries`` are
    the ones it is offered in (upper-case codes), when YouTube said."""

    def __init__(self, message: str, countries: list[str] | None = None) -> None:
        super().__init__(message)
        self.countries = clean_countries(countries)


def clean_countries(countries: object) -> list[str] | None:
    """Upper-case two-letter-style codes from a yt-dlp ``countries`` value."""
    if not countries or isinstance(countries, (str, bytes)):
        return None
    codes = [str(c).strip().upper() for c in countries if str(c).strip()]
    return codes or None


def countries_of(exc: BaseException | None) -> list[str] | None:
    """The allowed countries carried by a yt-dlp error, in-process or not."""
    found = clean_countries(getattr(exc, "countries", None))
    if found:
        return found
    inner = (getattr(exc, "exc_info", None) or (None, None))[1]
    return clean_countries(getattr(inner, "countries", None)) if inner is not exc else None


def is_region_blocked(error: BaseException | str | None) -> bool:
    """True for a ``GeoRestrictedError`` anywhere in the error's cause chain,
    or YouTube's region-block wording in any of its texts."""
    seen: set[int] = set()
    current: object = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, GeoRestrictedError):
            return True
        inner = (getattr(current, "exc_info", None) or (None, None))[1]
        if isinstance(inner, GeoRestrictedError):
            return True
        if REGION_BLOCKED.search(str(current)):
            return True
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
    return False
