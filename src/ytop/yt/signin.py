"""Age-gated videos: recognising them and scoping the dedicated sign-in."""

from __future__ import annotations

import contextvars
import re
from collections.abc import Iterator
from contextlib import contextmanager

AGE_GATED = re.compile(
    r"sign in to confirm your age|age-restricted|inappropriate for some users",
    re.IGNORECASE,
)
AGE_RESTRICTED_NEEDS_SIGNIN = "Age-restricted — needs the YTOP sign-in"

_signin: contextvars.ContextVar[str | None] = contextvars.ContextVar("ytop_signin", default=None)


def is_age_gated(error: BaseException | str | None) -> bool:
    """True when YouTube's age-gate wording is in any text of the error's
    cause chain (including yt-dlp's wrapped ``exc_info``)."""
    seen: set[int] = set()
    current: object = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        inner = (getattr(current, "exc_info", None) or (None, None))[1]
        if inner is not None and inner is not current and is_age_gated(inner):
            return True
        if AGE_GATED.search(str(current)):
            return True
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
    return False


@contextmanager
def using_signin(browser: str | None) -> Iterator[None]:
    """Make this context's yt-dlp calls use ``browser[:profile]`` cookies."""
    token = _signin.set(browser)
    try:
        yield
    finally:
        _signin.reset(token)


def active_signin() -> str | None:
    return _signin.get()
