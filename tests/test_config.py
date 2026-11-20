from __future__ import annotations

import logging
from pathlib import Path

import pytest

from ytop.config import SESSION_AUTH, Config, load_config, save_config
from ytop.layout import LibraryLayout


@pytest.fixture(autouse=True)
def _reset_session_auth():
    SESSION_AUTH.disable()
    yield
    SESSION_AUTH.disable()


def _write_cfg(root: Path, text: str) -> None:
    path = LibraryLayout(root).config_file
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")


def test_missing_config_returns_defaults(tmp_path: Path):
    cfg = load_config(tmp_path)
    assert cfg.jobs == 1
    assert cfg.audio_language == "en"
    assert cfg.pot_provider == "auto"
    assert not hasattr(cfg, "library")


def test_config_reads_sections_and_paths(tmp_path: Path):
    _write_cfg(
        tmp_path,
        """
[paths]
library = "~/Media/ytop"

[download]
jobs = 3
retries = 5
request_interval = 1.5
audio_language = "en-GB"

[pot]
provider = "off"

[transcripts]
languages = ["en", "en-orig"]
retries = 2
backoff_s = 0.5
timeout_s = 20
delay_s = 0.25
index = false

[web]
host = "127.0.0.1"
port = 9000
open_browser = true
""",
    )
    cfg = load_config(tmp_path)
    assert not hasattr(cfg, "library")  # [paths].library is ignored
    assert cfg.jobs == 3
    assert cfg.retries == 5
    assert cfg.request_interval == 1.5
    assert cfg.audio_language == "en-GB"
    assert cfg.pot_provider == "off"
    assert cfg.transcripts.languages == ("en", "en-orig")
    assert cfg.transcripts.retries == 2
    assert cfg.transcripts.backoff_s == 0.5
    assert cfg.transcripts.timeout_s == 20
    assert cfg.transcripts.delay_s == 0.25
    assert cfg.transcripts.index is False
    assert cfg.web.host == "127.0.0.1"
    assert cfg.web.port == 9000
    assert cfg.web.open_browser is True


def test_defaults_for_transcripts_and_web():
    cfg = Config()
    assert cfg.transcripts.languages == ("en",)
    assert cfg.transcripts.index is True
    assert cfg.web.host == "127.0.0.1"
    assert cfg.web.port == 8765
    assert cfg.web.open_browser is False


def test_unknown_sections_are_ignored(tmp_path: Path):
    _write_cfg(
        tmp_path,
        """
[analysis]
playlists = ["PL1"]

[snapshots]
upscale = "off"
""",
    )
    cfg = load_config(tmp_path)
    assert cfg.transcripts.languages == ("en",)


def test_invalid_worker_count_is_rejected():
    with pytest.raises(ValueError):
        Config(jobs=0)


def test_invalid_pot_provider_is_rejected():
    with pytest.raises(ValueError):
        Config(pot_provider="magic")


def test_invalid_transcript_and_web_settings_are_rejected():
    from ytop.config import TranscriptConfig, WebConfig

    with pytest.raises(ValueError):
        TranscriptConfig(languages=())
    with pytest.raises(ValueError):
        TranscriptConfig(timeout_s=0)
    with pytest.raises(ValueError):
        WebConfig(port=0)
    with pytest.raises(ValueError):
        WebConfig(host="")


def test_download_policy_defaults_to_an_efficient_budget():
    cfg = Config()
    assert cfg.target_mb_per_hour == 100.0
    assert cfg.min_height == 240
    assert cfg.max_height == 0
    assert cfg.probe_jobs == 4


def test_native_policy_is_allowed_and_negative_budgets_are_rejected():
    assert Config(target_mb_per_hour=0).target_mb_per_hour == 0
    with pytest.raises(ValueError):
        Config(target_mb_per_hour=-1)
    with pytest.raises(ValueError):
        Config(min_height=-1)


def test_load_config_from_library(tmp_path: Path):
    _write_cfg(tmp_path, "[download]\ntarget_mb_per_hour = 50\n")
    assert load_config(tmp_path).target_mb_per_hour == 50


def test_cookies_key_ignored(tmp_path: Path, caplog):
    _write_cfg(
        tmp_path,
        '[download]\ncookies = "/x"\ncookies_from_browser = "firefox"\n',
    )
    with caplog.at_level(logging.WARNING):
        cfg = load_config(tmp_path)
    assert cfg.cookies_from_browser is None
    assert not hasattr(cfg, "cookies")
    assert "'cookies' is no longer supported" in caplog.text
    assert "'cookies_from_browser' is ignored" in caplog.text


def test_session_auth_apply():
    cfg = Config()
    SESSION_AUTH.enable("firefox")
    applied = SESSION_AUTH.apply(cfg)
    assert applied.cookies_from_browser == "firefox"
    assert cfg.cookies_from_browser is None
    SESSION_AUTH.disable()
    assert SESSION_AUTH.apply(cfg).cookies_from_browser is None


def test_save_config_never_writes_cookies(tmp_path: Path):
    SESSION_AUTH.enable("firefox")
    cfg = SESSION_AUTH.apply(Config(jobs=2, audio_language="en-GB"))
    save_config(tmp_path, cfg)
    text = LibraryLayout(tmp_path).config_file.read_text(encoding="utf-8")
    assert "cookie" not in text and "firefox" not in text
    loaded = load_config(tmp_path)
    assert loaded.jobs == 2
    assert loaded.audio_language == "en-GB"
    assert loaded.transcripts == cfg.transcripts


def test_save_config_keeps_paths_pot_and_web(tmp_path: Path):
    _write_cfg(
        tmp_path,
        "[paths]\ndetect_removable = false\n[pot]\nprovider = \"off\"\n"
        "[web]\nport = 9999\nopen_browser = true\n",
    )
    cfg = load_config(tmp_path)
    save_config(tmp_path, cfg)
    again = load_config(tmp_path)
    assert again.detect_removable is False
    assert again.pot_provider == "off"
    assert again.web.port == 9999 and again.web.open_browser is True


def test_quick_analysis_defaults_on_and_round_trips(tmp_path: Path):
    assert load_config(tmp_path).quick_analysis is True
    save_config(tmp_path, Config(quick_analysis=False))
    assert load_config(tmp_path).quick_analysis is False
