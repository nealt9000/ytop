import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
MEDIA = ROOT / "tests/fixtures/media"
VERIFY = ROOT / "tools/ffmpeg-minimal/verify.py"

FIXTURES = [
    "av1_opus.webm",
    "vp9_opus.webm",
    "h264_aac.mp4",
    "av1.webm",
    "opus.webm",
    "h264.mp4",
    "aac.m4a",
]


@pytest.mark.parametrize("name", FIXTURES)
def test_fixture_exists_and_is_small(name: str) -> None:
    path = MEDIA / name
    assert path.is_file()
    assert 0 < path.stat().st_size < 50 * 1024


def test_verify_help_runs() -> None:
    result = subprocess.run(
        [sys.executable, str(VERIFY), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "fixtures_dir" in result.stdout
