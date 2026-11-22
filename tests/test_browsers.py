from __future__ import annotations

from pathlib import Path

from ytop.yt.browsers import firefox_profiles


def _ini(root: Path, rel: str, text: str) -> Path:
    ini = root / rel / "profiles.ini"
    ini.parent.mkdir(parents=True)
    ini.write_text(text)
    return ini.parent


def test_firefox_profiles_reads_profiles_ini(tmp_path):
    base = _ini(tmp_path, ".config/mozilla/firefox",
                "[Profile1]\nName=default\nPath=2ba1l7ed.default\n"
                "[Profile0]\nName=default-release\nPath=Q6uSzFvx.Profile 1\n"
                "[Install11457493C5A56847]\nDefault=Q6uSzFvx.Profile 1\n")
    assert firefox_profiles(tmp_path) == [
        f"firefox:{base / 'Q6uSzFvx.Profile 1'}", f"firefox:{base / '2ba1l7ed.default'}"]


def test_firefox_profiles_absolute_path(tmp_path):
    other = tmp_path / "elsewhere" / "prof"
    _ini(tmp_path, ".mozilla/firefox", f"[Profile0]\nIsRelative=0\nPath={other}\n")
    assert firefox_profiles(tmp_path) == [f"firefox:{other}"]


def test_firefox_profiles_none(tmp_path):
    assert firefox_profiles(tmp_path) == []


def test_firefox_profiles_ignores_garbage(tmp_path):
    _ini(tmp_path, ".config/mozilla/firefox", "not an ini [[[")
    assert firefox_profiles(tmp_path) == []
