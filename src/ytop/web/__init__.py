"""Local FastAPI web UI (P4).

``create_app`` and ``serve`` are loaded on first use: importing a helper
module of this package (``ytop.makestick`` uses ``ytop.web.fsops``) must not
import the whole app, which imports ``ytop.appupdate``, which imports
``ytop.makestick`` again."""

from __future__ import annotations

from typing import Any

__all__ = ["create_app", "serve"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from . import app

        return getattr(app, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
