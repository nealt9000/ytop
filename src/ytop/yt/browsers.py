"""Find browser profiles on this machine (read-only, profile names only).

Only ``profiles.ini`` is read; no profile content (cookies, logins) is touched.
"""

from __future__ import annotations

import configparser
import os
from pathlib import Path


def _firefox_dirs(home: Path | None) -> list[Path]:
    base = home if home is not None else Path.home()
    dirs = [
        base / ".config/mozilla/firefox",
        base / ".mozilla/firefox",
        base / "Library/Application Support/Firefox",
    ]
    appdata = os.environ.get("APPDATA") if home is None else None
    if appdata:
        dirs.append(Path(appdata) / "Mozilla/Firefox")
    return dirs


def _profile_dir(folder: Path, path: str, relative: bool) -> Path:
    return folder / path if relative else Path(path)


def firefox_profiles(home: Path | None = None) -> list[str]:
    """``firefox:<profile dir>`` specs from the first ``profiles.ini`` found.

    The profile an ``[Install...]`` section marks as ``Default`` comes first.
    Explicit profiles are offered because a bare ``firefox`` lets yt-dlp pick
    whichever profile folder it finds first, which may be a stale one.
    """
    for folder in _firefox_dirs(home):
        ini = folder / "profiles.ini"
        if not ini.is_file():
            continue
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            parser.read_string(ini.read_text(encoding="utf-8", errors="replace"))
        except (OSError, configparser.Error):
            continue
        defaults = [
            parser[s].get("Default", "").strip()
            for s in parser.sections() if s.startswith("Install")
        ]
        found: list[Path] = []
        for section in parser.sections():
            if not section.startswith("Profile"):
                continue
            path = parser[section].get("Path", "").strip()
            if not path:
                continue
            relative = parser[section].get("IsRelative", "1").strip() != "0"
            found.append(_profile_dir(folder, path, relative))
        default_dirs = [_profile_dir(folder, d, True) for d in defaults if d]
        found.sort(key=lambda p: 0 if (p in default_dirs or str(p) in defaults) else 1)
        if found:
            return [f"firefox:{p}" for p in found]
    return []
