"""Region-blocked videos: recognising them and keeping YouTube's country list."""

from __future__ import annotations

import contextvars
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from urllib.parse import quote, urlsplit, urlunsplit

from yt_dlp.utils import DownloadError, GeoRestrictedError

from ..config import Config, ProxyServer

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


_proxy: contextvars.ContextVar[str | None] = contextvars.ContextVar("ytop_proxy", default=None)
_USERINFO = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/\s]*@")
_SHOWN_COUNTRIES = 8


@contextmanager
def using_proxy(url: str | None) -> Iterator[None]:
    """Route this context's yt-dlp calls through ``url`` (None: direct)."""
    token = _proxy.set(url)
    try:
        yield
    finally:
        _proxy.reset(token)


def active_proxy() -> str | None:
    return _proxy.get()


def proxy_url(server: ProxyServer, config: Config) -> str:
    """The server's URL with the shared login added when it has none."""
    if not config.proxy_username:
        return server.url
    parts = urlsplit(server.url)
    if "@" in parts.netloc:
        return server.url
    login = f"{quote(config.proxy_username, safe='')}:{quote(config.proxy_password, safe='')}"
    return urlunsplit(parts._replace(netloc=f"{login}@{parts.netloc}"))


def order_proxies(servers: Sequence[ProxyServer],
                  countries: list[str] | None) -> list[ProxyServer]:
    """Servers worth trying for a video offered in ``countries``."""
    if not countries:
        return list(servers)
    wanted = {c.upper() for c in countries}
    matching = [s for s in servers if s.country and s.country.upper() in wanted]
    return matching + [s for s in servers if not s.country]


def redact(text: str) -> str:
    """Hide proxy logins (``scheme://user:pass@``) in any text."""
    return _USERINFO.sub(r"\1***@", text)


def region_blocked_message(countries: list[str] | None, tried: int) -> str:
    """The user-facing reason; ``tried`` is the proxies attempted, or -1
    when none are set up."""
    if countries:
        shown = ", ".join(countries[:_SHOWN_COUNTRIES])
        extra = len(countries) - _SHOWN_COUNTRIES
        text = f"Region-blocked: available in {shown}" + (f" and {extra} more" if extra > 0 else "")
    else:
        text = "Region-blocked: available only in other countries"
    if tried < 0:
        return text + " (no proxies set up)"
    if tried == 0:
        return text + " (no proxy set up for those countries)"
    return text + f" (tried {tried} {'proxy' if tried == 1 else 'proxies'})"


def covering_countries(blocked: list[list[str] | None],
                       offered: set[str] | None = None) -> list[str]:
    """Few countries that together offer every video with a known list
    (greedy; ties go to the alphabetically first code)."""
    remaining = []
    for codes in blocked:
        have = {c for c in (codes or []) if offered is None or c in offered}
        if have:
            remaining.append(have)
    chosen: list[str] = []
    while remaining:
        counts: dict[str, int] = {}
        for have in remaining:
            for code in have:
                counts[code] = counts.get(code, 0) + 1
        best = min(counts, key=lambda c: (-counts[c], c))
        chosen.append(best)
        remaining = [have for have in remaining if best not in have]
    return chosen
