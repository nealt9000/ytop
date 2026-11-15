from __future__ import annotations

import re
from pathlib import Path

import pytest

from stick_fakes import make_fake_image
from ytop import product
from ytop.config import Config, load_config
from ytop.layout import LibraryLayout
from ytop.manifest import MANIFEST_NAME, read_manifest

ROOT = Path(__file__).parent.parent
TEMPLATES = ROOT / "src/ytop/web/templates"
README = ROOT / "src/ytop/stick/launchers/README.txt"
PLATS = ["linux-x86_64", "windows-x86_64"]


def test_constants():
    assert product.NAME == "YTOP"
    assert product.REPO == "nealt9000/ytop"
    assert product.API_BASE == "https://api.github.com"


def test_name_lives_in_one_place():
    # User-visible product-name text comes from the Jinja global / {product}.
    pattern = re.compile(r"ytop", re.IGNORECASE)
    allowed = re.compile(
        r"""<code>[^<]*ytop[^<]*</code>   # command / folder names in code
        |YTOP\ Library                    # on-disk folder name
        |\.ytop|/ytop|ytop/|ytop\.        # paths and modules
        |start-ytop|Start\ YTOP            # launcher file names
        """,
        re.IGNORECASE | re.VERBOSE,
    )
    offenders = []
    for path in sorted(TEMPLATES.glob("*.html")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(allowed.sub("", line)):
                offenders.append(f"{path.name}:{n}: {line.strip()}")
    assert not offenders, "\n".join(offenders)
    text = README.read_text(encoding="utf-8")
    assert "{product}" in text
    assert pattern.search(allowed.sub("", text)) is None


def test_templates_render_the_name_from_the_global(tmp_path: Path, monkeypatch):
    from fastapi.testclient import TestClient

    from factories import library_set, make_library
    from ytop.web.app import create_app

    monkeypatch.setattr(product, "NAME", "Fancy")
    layout = make_library(tmp_path)
    client = TestClient(create_app(layout.root, libraries=library_set(layout)))
    page = client.get("/").text
    assert "Fancy" in page
    assert "ytop" not in page.split("<body", 1)[0].lower().replace("/ytop", "")


def test_repo_override(tmp_path: Path):
    assert product.repo(Config()) == product.REPO
    assert product.api_base(Config()) == product.API_BASE
    path = LibraryLayout(tmp_path).config_file
    path.parent.mkdir(parents=True)
    path.write_text(
        '[updates]\nrepo = "fork/ytop"\napi_base = "http://127.0.0.1:9/"\n',
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert product.repo(cfg) == "fork/ytop"
    assert product.api_base(cfg) == "http://127.0.0.1:9/"


def test_updates_defaults_and_values(tmp_path: Path):
    u = Config().updates
    assert u.check_app is None
    assert u.check_components is True
    assert u.last_check == ""
    path = LibraryLayout(tmp_path).config_file
    path.parent.mkdir(parents=True)
    path.write_text(
        '[updates]\ncheck_app = false\ncheck_components = false\n'
        'last_check = "2026-11-14T10:00:00+00:00"\n',
        encoding="utf-8",
    )
    u = load_config(tmp_path).updates
    assert u.check_app is False and u.check_components is False
    assert u.last_check == "2026-11-14T10:00:00+00:00"


def test_licence_text():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License")
    assert "Copyright (c) 2026 the YTOP authors" in text
    assert "@" not in text


def test_notices_cover_the_components():
    text = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    for name in ("CPython", "FFmpeg", "Deno", "yt-dlp", "yt-dlp-ejs",
                 "bgutil-ytdlp-pot-provider", "youtube-transcript-api",
                 "FastAPI", "Starlette", "Uvicorn", "Jinja", "Pillow",
                 "tools/ffmpeg-minimal"):
        assert name in text, name


@pytest.fixture
def image(tmp_path: Path) -> Path:
    return make_fake_image(tmp_path / "img", PLATS)


def test_image_ships_licence_and_notices(image: Path):
    entries = read_manifest(image / MANIFEST_NAME)
    assert entries is not None
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        assert (image / name).read_bytes() == (ROOT / name).read_bytes()
        assert name in entries


def test_save_config_keeps_updates(tmp_path: Path):
    from dataclasses import replace

    from ytop.config import UpdatesConfig, save_config

    cfg = Config(updates=UpdatesConfig(repo="fork/ytop", check_app=True,
                                       last_check="2026-11-14T10:00:00+00:00"))
    save_config(tmp_path, cfg)
    assert load_config(tmp_path).updates == cfg.updates
    save_config(tmp_path, replace(cfg, updates=UpdatesConfig()))
    assert load_config(tmp_path).updates == UpdatesConfig()


def test_updates_smoke_video(tmp_path: Path):
    from ytop.config import UpdatesConfig, save_config

    assert Config().updates.smoke_video == "jNQXAC9IVRw"
    cfg = Config(updates=UpdatesConfig(smoke_video="abcdefghijk"))
    save_config(tmp_path, cfg)
    assert load_config(tmp_path).updates.smoke_video == "abcdefghijk"
    save_config(tmp_path, Config())
    text = LibraryLayout(tmp_path).config_file.read_text(encoding="utf-8")
    assert "smoke_video" not in text  # the default is not written out
    # Empty means "no online probe": it is kept, not turned back into the default.
    save_config(tmp_path, Config(updates=UpdatesConfig(smoke_video="")))
    assert load_config(tmp_path).updates.smoke_video == ""


def test_empty_smoke_video_skips_the_online_probe(monkeypatch):
    from ytop import updater

    calls: list[str] = []

    def fake(python, script, args, timeout):
        calls.append(script)
        return {"errors": [], "found": {}}, ""

    monkeypatch.setattr(updater, "_run_python", fake)
    out = updater.run_smoke(updater.SmokeRequest(
        python="py", paths=[], expect={"yt-dlp": "1"}, deno=None, deno_version=None,
        video="", online=True, baseline=[],
    ))
    assert out.ok and calls == [updater._IMPORT_SCRIPT]
