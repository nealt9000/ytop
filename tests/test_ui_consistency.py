"""Cross-page UI consistency: buttons, confirms, empty states, wording."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from factories import add_playlist, library_set, make_library
from ytop.layout import LibraryLayout
from ytop.web.app import create_app

TEMPLATES = Path(__file__).resolve().parents[1] / "src/ytop/web/templates"
ALL = sorted(TEMPLATES.glob("*.html"))


def _plain(source: str) -> str:
    source = re.sub(r"\{#.*?#\}", "", source, flags=re.S)
    source = re.sub(r"\{%.*?%\}", "", source, flags=re.S)
    return re.sub(r"\{\{.*?\}\}", "x", source, flags=re.S)


class _Scan(HTMLParser):
    """Collects forms and, per head-like container, its visible primary buttons."""

    VOID = {"input", "br", "img", "hr", "meta", "link"}

    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, str | None]] = []
        self.stack: list[tuple[str, bool]] = []  # (tag, is_container)
        self.primaries: list[int] = []  # one counter per open container
        self.counts: list[int] = []  # finished containers' primary counts

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if tag == "form":
            self.forms.append(a)
        if tag in self.VOID:
            return
        container = "page-head" in classes or "hero-actions" in classes or "empty-state" in classes
        self.stack.append((tag, container))
        if container:
            self.primaries.append(0)
        if (
            self.primaries
            and "btn" in classes
            and "primary" in classes
            and "hidden" not in a
        ):
            self.primaries[-1] += 1

    def handle_endtag(self, tag):
        if tag in self.VOID or not self.stack:
            return
        name, container = self.stack.pop()
        if container:
            self.counts.append(self.primaries.pop())


def _scan(path: Path) -> _Scan:
    scan = _Scan()
    scan.feed(_plain(path.read_text(encoding="utf-8")))
    scan.close()
    return scan


@pytest.mark.parametrize("path", ALL, ids=lambda p: p.name)
def test_one_primary_button_per_page_head(path: Path) -> None:
    assert all(count <= 1 for count in _scan(path).counts), path.name


@pytest.mark.parametrize("path", ALL, ids=lambda p: p.name)
def test_destructive_forms_confirm(path: Path) -> None:
    for form in _scan(path).forms:
        action = form.get("action") or ""
        if action.endswith(("/remove", "/delete")):
            assert form.get("data-confirm"), f"{path.name}: {action}"


@pytest.mark.parametrize("path", ALL, ids=lambda p: p.name)
def test_consistent_labels_and_wording(path: Path) -> None:
    text = _plain(path.read_text(encoding="utf-8"))
    assert not re.search(r">\s*Add a playlist\s*<", text), "use Add playlist"
    assert not re.search(r">\s*Delete\b", text), "use Remove"
    assert not re.search(r"\bseries\b", text, re.I)
    assert "What is on this stick" not in text  # a library is not a stick


def _client(tmp_path: Path, playlists: int = 0) -> TestClient:
    layout: LibraryLayout = make_library(tmp_path)
    for n in range(playlists):
        add_playlist(layout, f"P{n}", f"List {n}", [(f"v{n}", "A")])
    return TestClient(create_app(layout.root, libraries=library_set(layout)))


def test_no_series_wording(tmp_path: Path) -> None:
    client = _client(tmp_path, 2)
    for url in ("/", "/videos", "/search?q=x"):
        assert "series" not in client.get(url).text.lower(), url


@pytest.mark.parametrize(
    "url", ["/", "/downloads", "/tags", "/search?q=zzzz", "/videos?tag=nothing"]
)
def test_empty_list_pages_have_an_empty_state(tmp_path: Path, url: str) -> None:
    html = _client(tmp_path).get(url).text
    assert 'class="empty-state"' in html, url
    block = html.split('class="empty-state"', 1)[1].split("</section>", 1)[0]
    assert block.count("btn primary") <= 1


def test_storage_page_has_empty_state_block() -> None:
    source = (TEMPLATES / "storage.html").read_text(encoding="utf-8")
    assert re.search(
        r'\{% if libraries %\}.*?\{% else %\}\s*<section class="empty-state">.*?\{% endif %\}',
        source,
        re.S,
    )


def test_chip_strip_has_no_more_toggle(tmp_path: Path) -> None:
    from ytop.tags import TagStore

    layout = make_library(tmp_path)
    store = TagStore(layout)
    for n in range(15):
        add_playlist(layout, f"P{n}", f"List {n}", [(f"v{n}", "A")])
        store.set_category(f"P{n}", f"Topic {n:02d}")
    client = TestClient(create_app(layout.root, libraries=library_set(layout)))
    html = client.get("/").text
    found = re.search(r'<div class="chip-strip"[^>]*>(.*?)</div>', html, re.S)
    assert found, "chip strip not rendered"
    strip = found.group(1)
    assert strip.count('data-chip="category"') == 15
    assert "chip-more" not in html
    client.app.state.jobs.close()
