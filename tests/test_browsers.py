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


def test_firefox_profiles_windows_appdata(tmp_path, monkeypatch):
    """On Windows the profiles live under %APPDATA%; specs carry absolute
    (drive-letter) paths."""
    home = tmp_path / "home"
    home.mkdir()
    appdata = tmp_path / "AppData" / "Roaming"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("APPDATA", str(appdata))
    base = _ini(appdata, "Mozilla/Firefox",
                "[Profile0]\nName=default-release\nPath=Profiles/abcd.default-release\n"
                "[Profile1]\nName=ytop\nIsRelative=0\n"
                "Path=C:\\Users\\me\\YTOP Profile\n")
    assert firefox_profiles() == [
        f"firefox:{base / 'Profiles/abcd.default-release'}",
        "firefox:C:\\Users\\me\\YTOP Profile",
    ]


def test_firefox_profiles_macos(tmp_path):
    base = _ini(tmp_path, "Library/Application Support/Firefox",
                "[Profile0]\nName=default-release\nPath=Profiles/x1.default-release\n")
    assert firefox_profiles(tmp_path) == [f"firefox:{base / 'Profiles/x1.default-release'}"]


def test_firefox_profiles_puts_every_installs_default_first(tmp_path):
    """Two Firefox installs (say release and ESR) each mark a Default."""
    base = _ini(tmp_path, ".mozilla/firefox",
                "[Profile0]\nPath=a.unused\n[Profile1]\nPath=b.release\n"
                "[Profile2]\nPath=c.esr\n"
                "[InstallAAAA]\nDefault=b.release\n[InstallBBBB]\nDefault=c.esr\n")
    assert firefox_profiles(tmp_path) == [
        f"firefox:{base / 'b.release'}", f"firefox:{base / 'c.esr'}",
        f"firefox:{base / 'a.unused'}",
    ]


def test_firefox_profiles_install_default_may_be_absolute(tmp_path):
    other = tmp_path / "elsewhere" / "ytop"
    base = _ini(tmp_path, ".mozilla/firefox",
                f"[Profile0]\nPath=a.default\n[Profile1]\nIsRelative=0\nPath={other}\n"
                f"[InstallAAAA]\nDefault={other}\n")
    assert firefox_profiles(tmp_path) == [f"firefox:{other}", f"firefox:{base / 'a.default'}"]
