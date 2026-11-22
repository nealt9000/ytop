"""#34 (one YoutubeDL per worker thread) and #35 (analysis reads cached)."""

from __future__ import annotations

import threading

from ytop.config import Config
from ytop.yt import probe
from ytop.yt.proxies import using_proxy


class CountingYDL:
    made: list[dict] = []
    closed = 0

    def __init__(self, opts):
        CountingYDL.made.append(opts)

    def extract_info(self, url, download=False):
        return {"id": url[-11:], "title": "T", "formats": [{"format_id": "18", "url": "https://x/y"}],
                "thumbnails": [{"url": "https://i/t.jpg", "width": 480}]}

    def close(self):
        CountingYDL.closed += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _counting(monkeypatch):
    CountingYDL.made, CountingYDL.closed = [], 0
    monkeypatch.setattr(probe, "YoutubeDL", CountingYDL)


def test_one_youtubedl_serves_a_threads_reads(monkeypatch):
    _counting(monkeypatch)
    probe._extract("https://www.youtube.com/watch?v=aaaaaaaaaaa", {"quiet": True})
    probe._extract("https://www.youtube.com/watch?v=bbbbbbbbbbb", {"quiet": True})
    assert len(CountingYDL.made) == 1
    probe._extract("https://www.youtube.com/watch?v=ccccccccccc", {"quiet": True, "proxy": "http://p:1"})
    assert len(CountingYDL.made) == 2  # other options: its own instance

    other = threading.Thread(
        target=probe._extract, args=("https://www.youtube.com/watch?v=ddddddddddd", {"quiet": True}))
    other.start()
    other.join()
    assert len(CountingYDL.made) == 3  # instances are never shared between threads


def test_old_instances_are_closed(monkeypatch):
    _counting(monkeypatch)
    for n in range(probe.YDL_PER_THREAD + 2):
        probe._extract("https://www.youtube.com/watch?v=aaaaaaaaaaa", {"socket_timeout": n})
    assert CountingYDL.closed == 2
    probe.clear_caches()
    assert CountingYDL.closed == probe.YDL_PER_THREAD + 2


def test_analysis_reads_are_cached_for_a_few_hours(monkeypatch):
    reads: list[str] = []
    clock = [1000.0]

    def fake_extract(video_id, config, cache_root=None, *, quick=False):
        reads.append(video_id)
        return {"id": "vid00000001", "formats": [], "thumbnails": [{"url": "u", "width": 480}],
                "http_headers": {"big": "x"}}

    monkeypatch.setattr(probe, "extract_video_info", fake_extract)
    monkeypatch.setattr(probe.time, "monotonic", lambda: clock[0])
    cfg = Config()
    probe._analysis_read("vid00000001", cfg, None, quick=True)
    probe._analysis_read("vid00000001", cfg, None, quick=True)
    assert len(reads) == 2  # outside a review nothing is reused
    reads.clear()
    reusing = probe.reusing_analyses()
    reusing.__enter__()
    first = probe._analysis_read("vid00000001", cfg, None, quick=True)
    again = probe._analysis_read("vid00000001", cfg, None, quick=True)
    assert len(reads) == 0  # the reads above are reused
    assert again["thumbnails"] == first["thumbnails"]  # downloads still need these
    assert "http_headers" not in again  # trimmed like raw.json
    again["title"] = "changed"
    assert "title" not in probe._analysis_read("vid00000001", cfg, None, quick=True)

    probe._analysis_read("vid00000001", cfg, None, quick=False)
    assert len(reads) == 1  # a full read is not a quick one
    with using_proxy("http://p:1"):
        probe._analysis_read("vid00000001", cfg, None, quick=True)
    assert len(reads) == 2  # nor is one through a proxy
    clock[0] += probe.ANALYSIS_TTL_S + 1
    probe._analysis_read("vid00000001", cfg, None, quick=True)
    assert len(reads) == 3  # expired
    reusing.__exit__(None, None, None)
