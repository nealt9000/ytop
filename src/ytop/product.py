"""The product's name and where its releases live: one place.

``NAME`` is what the app shows people (pages, titles, the stick README).  The
on-disk names (``.ytop``, the ``YTOP Library`` folder, launcher file names,
the package) are compatibility identifiers and stay as they are.  A stick's
``[updates]`` config can point at another repo or API (a fork, or a test
server)."""

from __future__ import annotations

from .config import Config

NAME = "YTOP"
REPO = "nealt9000/ytop"
API_BASE = "https://api.github.com"


def repo(config: Config) -> str:
    """``owner/name`` to check for releases: ``[updates] repo`` or ``REPO``."""
    return config.updates.repo or REPO


def api_base(config: Config) -> str:
    """The releases API root: ``[updates] api_base`` or ``API_BASE``."""
    return config.updates.api_base or API_BASE
