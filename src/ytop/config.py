"""Configuration loading for ytop.

Each library carries its own config at ``<root>/.ytop/config.toml``.
Every option has a safe default, so a missing file is fine.  Command-line
flags override whatever is loaded here.  YTOP never stores cookies: browser
login is enabled per process via ``SESSION_AUTH``.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

log = logging.getLogger(__name__)


def is_loopback_host(host: str) -> bool:
    """Whether ``host`` names only this machine (the web UI is local-only)."""
    value = (host or "").strip().strip("[]").lower()
    if value in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class TranscriptConfig:
    """Caption acquisition and transcript indexing (P4)."""

    languages: tuple[str, ...] = ("en",)
    retries: int = 4
    backoff_s: float = 2.0
    timeout_s: float = 30.0
    delay_s: float = 1.0
    index: bool = True

    def __post_init__(self) -> None:
        if not self.languages or any(not lang.strip() for lang in self.languages):
            raise ValueError("transcripts.languages needs at least one language")
        if self.retries < 0:
            raise ValueError("transcripts.retries must be non-negative")
        if self.backoff_s < 0:
            raise ValueError("transcripts.backoff_s must be non-negative")
        if self.timeout_s <= 0:
            raise ValueError("transcripts.timeout_s must be positive")
        if self.delay_s < 0:
            raise ValueError("transcripts.delay_s must be non-negative")


@dataclass(frozen=True)
class WebConfig:
    """Local-only web UI settings."""

    host: str = "127.0.0.1"
    port: int = 8765
    open_browser: bool = False

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("web.host must not be empty")
        if not is_loopback_host(self.host):
            raise ValueError(
                "web.host must be a loopback address (127.0.0.1, ::1 or "
                "localhost); this server is local-only and refuses LAN binds"
            )
        if not 1 <= self.port <= 65535:
            raise ValueError("web.port must be between 1 and 65535")


DEFAULT_SMOKE_VIDEO = "jNQXAC9IVRw"


@dataclass(frozen=True)
class UpdatesConfig:
    """Self-update settings (``[updates]``).  Empty ``repo``/``api_base`` mean
    the product defaults (``ytop.product``)."""

    repo: str = ""
    api_base: str = ""
    # None: the stick has not been asked yet.
    check_app: bool | None = None
    # The daily check and the automatic update after a YouTube failure.
    check_components: bool = True
    # ISO time of the last check; managed by the app.
    last_check: str = ""
    # The public video the updater's smoke test probes (metadata only) when
    # online, to tell a new yt-dlp works before switching to it.  Empty: no
    # online probe at all (hermetic CI).
    smoke_video: str = DEFAULT_SMOKE_VIDEO


@dataclass
class Config:
    # Downloads.
    jobs: int = 1
    probe_jobs: int = 4
    # Efficient downloads choose the best native rendition that fits this
    # per-video budget.  The default lands a high-resolution lecture playlist
    # near the bytes/hour of the 240p validation corpus, without re-encoding.
    target_mb_per_hour: float = 100.0
    # Keep at least this height when the source offers it.
    min_height: int = 240
    # Optional hard cap (0 = no cap); the budget usually decides.
    max_height: int = 0
    retries: int = 3
    request_interval: float = 0.5
    # A stalled socket must not leave a probe job "running" forever.
    socket_timeout: float = 30.0
    audio_language: str = "en"
    # Treat mounted volumes with a ytop layout as library locations.
    detect_removable: bool = True

    # Authentication / PO tokens.
    # Runtime-only: set by SessionAuth, never loaded from or saved to a file.
    cookies_from_browser: str | None = None
    pot_provider: str = "auto"  # auto | off

    # Captions and the transcript index.
    transcripts: TranscriptConfig = field(default_factory=TranscriptConfig)

    # Local web UI.
    web: WebConfig = field(default_factory=WebConfig)

    # Release checks and self-update.
    updates: UpdatesConfig = field(default_factory=UpdatesConfig)

    def __post_init__(self) -> None:
        if self.jobs < 1:
            raise ValueError("download.jobs must be at least 1")
        if self.probe_jobs < 1:
            raise ValueError("download.probe_jobs must be at least 1")
        if self.max_height < 0:
            raise ValueError("download.max_height must be non-negative")
        if self.target_mb_per_hour < 0:
            raise ValueError(
                "download.target_mb_per_hour must be non-negative "
                "(0 = native maximum)"
            )
        if self.min_height < 0:
            raise ValueError("download.min_height must be non-negative")
        if self.retries < 0:
            raise ValueError("download.retries must be non-negative")
        if self.request_interval < 0:
            raise ValueError("download.request_interval must be non-negative")
        if self.socket_timeout <= 0:
            raise ValueError("download.socket_timeout must be positive")
        if self.pot_provider not in {"auto", "off"}:
            raise ValueError("pot.provider must be 'auto' or 'off'")


class SessionAuth:
    """Process-only browser login; never persisted anywhere."""

    def __init__(self) -> None:
        self.browser: str | None = None

    def enable(self, browser: str) -> None:
        self.browser = browser

    def disable(self) -> None:
        self.browser = None

    def apply(self, cfg: Config) -> Config:
        """Return a copy of ``cfg`` carrying the session's browser login."""
        return replace(cfg, cookies_from_browser=self.browser)


SESSION_AUTH = SessionAuth()


def _section(data: dict, name: str) -> dict:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"config section [{name}] must be a table")
    return value


def load_config(library_root: Path | None = None) -> Config:
    """Load a library's TOML config, or defaults when there is none."""
    if library_root is None:
        return Config()
    from .layout import LibraryLayout  # lazy: layout -> yt -> config

    p = LibraryLayout(Path(library_root)).config_file
    if not p.exists():
        return Config()

    with p.open("rb") as fh:
        data = tomllib.load(fh)

    paths = _section(data, "paths")
    download = _section(data, "download")
    pot = _section(data, "pot")
    transcripts = _section(data, "transcripts")
    web = _section(data, "web")
    updates = _section(data, "updates")
    check_app = updates.get("check_app")

    if "library" in paths:
        log.debug("config.toml [paths].library is ignored; a library's config "
                  "cannot point at another library")
    if "cookies" in download:
        log.warning(
            "config.toml 'cookies' is no longer supported; "
            "YTOP never stores cookies"
        )
    if "cookies_from_browser" in download:
        log.warning(
            "config.toml 'cookies_from_browser' is ignored; "
            "enable browser login per session instead"
        )

    return Config(
        jobs=int(download.get("jobs", 1)),
        probe_jobs=int(download.get("probe_jobs", 4)),
        target_mb_per_hour=float(download.get("target_mb_per_hour", 100.0)),
        min_height=int(download.get("min_height", 240)),
        max_height=int(download.get("max_height", 0)),
        retries=int(download.get("retries", 3)),
        request_interval=float(download.get("request_interval", 0.5)),
        socket_timeout=float(download.get("socket_timeout", 30.0)),
        audio_language=str(download.get("audio_language") or ""),
        detect_removable=bool(paths.get("detect_removable", True)),
        pot_provider=str(pot.get("provider", "auto")),
        transcripts=TranscriptConfig(
            languages=tuple(
                str(v) for v in transcripts.get("languages", ["en"])
            ),
            retries=int(transcripts.get("retries", 4)),
            backoff_s=float(transcripts.get("backoff_s", 2.0)),
            timeout_s=float(transcripts.get("timeout_s", 30.0)),
            delay_s=float(transcripts.get("delay_s", 1.0)),
            index=bool(transcripts.get("index", True)),
        ),
        web=WebConfig(
            host=str(web.get("host", "127.0.0.1")),
            port=int(web.get("port", 8765)),
            open_browser=bool(web.get("open_browser", False)),
        ),
        updates=UpdatesConfig(
            repo=str(updates.get("repo", "")).strip(),
            api_base=str(updates.get("api_base", "")).strip(),
            check_app=None if check_app is None else bool(check_app),
            check_components=bool(updates.get("check_components", True)),
            last_check=str(updates.get("last_check", "")),
            smoke_video=str(updates.get("smoke_video", DEFAULT_SMOKE_VIDEO)).strip(),
        ),
    )


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def save_config(library_root: Path, cfg: Config) -> None:
    """Write every persisted setting; never any cookie keys."""
    download = {
        "jobs": cfg.jobs,
        "probe_jobs": cfg.probe_jobs,
        "target_mb_per_hour": cfg.target_mb_per_hour,
        "min_height": cfg.min_height,
        "max_height": cfg.max_height,
        "retries": cfg.retries,
        "request_interval": cfg.request_interval,
        "socket_timeout": cfg.socket_timeout,
        "audio_language": cfg.audio_language,
    }
    t = cfg.transcripts
    transcripts = {
        "languages": t.languages,
        "retries": t.retries,
        "backoff_s": t.backoff_s,
        "timeout_s": t.timeout_s,
        "delay_s": t.delay_s,
        "index": t.index,
    }
    lines = ["[paths]", f"detect_removable = {_toml_value(cfg.detect_removable)}"]
    lines += ["", "[download]"]
    lines += [f"{k} = {_toml_value(v)}" for k, v in download.items()]
    lines += ["", "[pot]", f"provider = {_toml_value(cfg.pot_provider)}"]
    lines += ["", "[transcripts]"]
    lines += [f"{k} = {_toml_value(v)}" for k, v in transcripts.items()]
    w = cfg.web
    lines += ["", "[web]"]
    lines += [
        f"{k} = {_toml_value(v)}"
        for k, v in (
            ("host", w.host), ("port", w.port), ("open_browser", w.open_browser)
        )
    ]
    u = cfg.updates
    updates: dict[str, object] = {"check_components": u.check_components}
    if u.check_app is not None:
        updates["check_app"] = u.check_app
    for key in ("repo", "api_base", "last_check"):
        if getattr(u, key):
            updates[key] = getattr(u, key)
    if u.smoke_video != DEFAULT_SMOKE_VIDEO:
        updates["smoke_video"] = u.smoke_video
    lines += ["", "[updates]"]
    lines += [f"{k} = {_toml_value(v)}" for k, v in updates.items()]
    from .layout import LibraryLayout  # lazy: layout -> yt -> config

    path = LibraryLayout(Path(library_root)).config_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def set_updates_option(library_root: Path, key: str, value: bool | str) -> None:
    """Set one ``[updates]`` key in a library's ``config.toml``, keeping every
    other line as it is (``save_config`` rewrites only some sections).  The
    file is replaced atomically."""
    from .layout import LibraryLayout  # lazy: layout -> yt -> config

    if key not in UpdatesConfig.__dataclass_fields__:
        raise ValueError(f"unknown [updates] key: {key}")
    path = LibraryLayout(Path(library_root)).config_file
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    entry = f"{key} = {_toml_value(value)}"
    header = re.compile(r"^\s*\[([^\[\]]+)\]\s*(#.*)?$")
    assigns = re.compile(rf"^\s*{re.escape(key)}\s*=")
    start = next((i for i, line in enumerate(lines)
                  if (m := header.match(line)) and m.group(1).strip() == "updates"), None)
    if start is None:
        lines += ([""] if lines else []) + ["[updates]", entry]
    else:
        end = next((i for i in range(start + 1, len(lines)) if header.match(lines[i])),
                   len(lines))
        found = next((i for i in range(start + 1, end) if assigns.match(lines[i])), None)
        if found is None:
            lines.insert(start + 1, entry)
        else:
            lines[found] = entry
    text = "\n".join(lines) + "\n"
    tomllib.loads(text)  # never write a file that no longer parses
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".ytop-tmp")
    with open(temp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
