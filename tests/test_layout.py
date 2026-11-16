from __future__ import annotations

from pathlib import Path

import pytest

from ytop import layout as layout_mod
from ytop.layout import LibraryLayout, init_library, read_library, safe_id
from ytop.yt.metadata import write_json as real_write_json


def test_init_library_creates_marker_last(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "lib"
    order: list[Path] = []

    def recording(path, data, **kwargs):
        order.append(Path(path))
        real_write_json(path, data, **kwargs)

    monkeypatch.setattr(layout_mod, "write_json", recording)
    init_library(root, "Stick")
    assert order[-1] == LibraryLayout(root).marker
    info = read_library(root)
    assert info is not None and info.schema == 1
    assert (root / "YTOP Library").is_dir()


def test_init_library_idempotent(tmp_path: Path) -> None:
    first = init_library(tmp_path / "lib", "A")
    second = init_library(tmp_path / "lib", "B")
    assert first.id == second.id


def test_resolve_media_rejects_escape(tmp_path: Path) -> None:
    init_library(tmp_path / "lib", "A")
    layout = LibraryLayout(tmp_path / "lib")
    assert layout.resolve_media("../.ytop/library.json") is None
    assert layout.resolve_media("A/x.webm") is None
    (layout.media_root / "A").mkdir()
    (layout.media_root / "A" / "x.webm").write_bytes(b"x")
    assert layout.resolve_media("A/x.webm") == layout.media_root / "A" / "x.webm"
    assert layout.relative_media(layout.media_root / "A" / "x.webm") == "A/x.webm"


def test_safe_id() -> None:
    assert safe_id("PL/../x y") == "PL_x_y"
    assert len(safe_id("a" * 100)) == 64


def test_read_library_missing_or_corrupt(tmp_path: Path) -> None:
    assert read_library(tmp_path) is None
    layout = LibraryLayout(tmp_path)
    layout.ytop_dir.mkdir()
    layout.marker.write_text("{not json")
    assert read_library(tmp_path) is None


@pytest.mark.parametrize("value", ["", "///", "..."])
def test_safe_id_rejects_empty(value: str) -> None:
    with pytest.raises(ValueError):
        safe_id(value)


def test_layout_dirs_reject_empty_ids(tmp_path: Path) -> None:
    layout = LibraryLayout(tmp_path)
    with pytest.raises(ValueError):
        layout.playlist_meta_dir("///")
    with pytest.raises(ValueError):
        layout.partial_dir("")


def test_init_library_refuses_corrupt_marker(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    init_library(root, "A")
    marker = LibraryLayout(root).marker
    marker.write_text("{not json", encoding="utf-8")
    with pytest.raises(layout_mod.CorruptMarkerError):
        init_library(root, "B")
    assert marker.read_text(encoding="utf-8") == "{not json"
