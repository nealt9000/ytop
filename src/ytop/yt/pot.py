"""Authentication and PO-token support.

YouTube increasingly requires PO tokens for some clients.  The project does not
reimplement them: ``bgutil-ytdlp-pot-provider`` is a yt-dlp plugin, so when it is
installed yt-dlp uses it automatically.  This module only reports whether that
plugin is available and applies the session's browser login (never stored).
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
from pathlib import Path

from ..components import server_home
from ..config import SESSION_AUTH, Config
from ..runtime import active_runtime
from ..toolchain import deno_path

PLUGIN_DIST = "bgutil-ytdlp-pot-provider"


def cookies_from_browser(value: str) -> tuple:
    """Convert ``chrome`` or ``chrome:profile`` into yt-dlp's tuple form."""
    parts = [p or None for p in value.split(":", 3)]
    return tuple((parts + [None, None, None, None])[:4])


def plugin_available() -> bool:
    try:
        importlib.metadata.distribution(PLUGIN_DIST)
        return True
    except importlib.metadata.PackageNotFoundError:
        pass
    # Some installations expose the module without the distribution name.
    for name in (
        "yt_dlp_plugins.extractor.getpot_bgutil",
        "yt_dlp_plugins.extractor.getpot_bgutil_http",
        "yt_dlp_plugins.extractor.getpot_bgutil_script",
    ):
        try:
            if importlib.util.find_spec(name) is not None:
                return True
        except (ImportError, ModuleNotFoundError):
            continue
    return False


def current_server_home() -> Path | None:
    """The stick's bgutil server folder (``.ytop/components/bgutil-server``),
    or None: a checkout, or a stick whose store has none."""
    home = active_runtime().home
    return None if home is None else server_home(home / ".ytop")


def pot_status(config: Config | None = None) -> tuple[str, str]:
    """Return ``(status, evidence)`` for ``ytop doctor``."""
    server = current_server_home()
    deno = deno_path()
    if plugin_available():
        if server is not None and deno is not None:
            return "ok", (
                f"bgutil PO-token plugin installed; script provider runs "
                f"server {server.parent.name} under Deno ({deno})"
            )
        if server is None:
            why = "no bgutil server in the component store"
        else:
            why = "the bgutil server needs Deno, which is missing"
        return "warn", f"bgutil PO-token plugin installed, but {why}"
    if SESSION_AUTH.browser:
        return "warn", "no bgutil PO-token plugin; using this session's browser login"
    return (
        "warn",
        "no bgutil PO-token plugin (install bgutil-ytdlp-pot-provider if YouTube "
        "starts demanding PO tokens); a per-session browser login is the fallback",
    )


def apply_auth_options(opts: dict, config: Config) -> None:
    """Add the session browser login and honour the PO-token feature flag."""
    if config.pot_provider == "off":
        # The bgutil plugin lives in yt-dlp's default plugin directory.  Clearing
        # plugin dirs is the supported way to disable it for a run.
        opts["plugin_dirs"] = []
    else:
        server = current_server_home()
        if server is not None:
            # The plugin's Deno provider runs <server_home>/src/generate_once.ts
            # with the Deno yt-dlp's js_runtimes names (the bundled one).
            args = opts.setdefault("extractor_args", {})
            args["youtubepot-bgutilscript"] = {"server_home": [str(server)]}
    browser = SESSION_AUTH.apply(config).cookies_from_browser
    if browser:
        opts["cookiesfrombrowser"] = cookies_from_browser(browser)
