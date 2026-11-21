"""When component updates run: at start, on a YouTube-side failure (with
one retry and a rollback), and Update now.  No network: the updater is
faked; the subprocess runner runs a fake ``yt_dlp`` package."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from factories import lib_root, library_set
from ytop import autoupdate
from ytop.autoupdate import AutoUpdate, start_check
from ytop.components import Current, read_current, write_current
from ytop.config import Config, UpdatesConfig
from ytop.stick.boot import COMPLETE
from ytop.updater import CheckResult, UpdateResult
from ytop.web import jobs as jobs_module
from ytop.web.app import create_app
from ytop.web.jobs import JobManager
from ytop.yt import runner as ytdlp_runner
from ytop.yt.download import FetchResult, ItemResult
from ytop.yt.formats import Selection
from ytop.yt.probe import (
    YOUTUBE_CHANGED,
    JobCancelled,
    ProbedItem,
    ProbeResult,
    classify_failure,
)

SIGNATURE = "ERROR: [youtube] abc12345678: Signature extraction failed: Some formats may be missing"


# --- classify_failure ----------------------------------------------------------------

YOUTUBE_CHANGED_CASES = {
    "extractor": "ERROR: [youtube] abc: Unable to extract initial player response; "
                 "please report this issue on https://github.com/yt-dlp/yt-dlp/issues",
    "signature": SIGNATURE,
    "n-challenge": "WARNING: [youtube] abc: n challenge solving failed: Some formats may be "
                   "missing. Ensure you have a supported JavaScript runtime",
    "po-token": "ERROR: [youtube] abc: This video requires a GVS PO Token which was not provided",
    "bot-check": "ERROR: [youtube] abc: Sign in to confirm you're not a bot. Use --cookies",
    "formats": "ERROR: [youtube] abc: No video formats found!",
}
OTHER_CASES = [
    "ERROR: [youtube] abc: Private video. Sign in if you've been granted access",
    "ERROR: [youtube] abc: Video unavailable. This video has been removed by the uploader",
    "ERROR: Unable to download webpage: <urlopen error [Errno -3] "
    "Temporary failure in name resolution>",
    "[Errno 28] No space left on device",
    "the selected rendition is over FAT32's 4 GB file limit",
]


def test_age_gate_is_age_restricted_not_other():
    message = "ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate"
    assert classify_failure(message) == "age_restricted"
    assert classify_failure(OSError(message)) == "age_restricted"


def test_classify_failure_table():
    names = {what for what, _ in YOUTUBE_CHANGED}
    assert names == set(YOUTUBE_CHANGED_CASES), "every pattern has a case here"
    for what, message in YOUTUBE_CHANGED_CASES.items():
        assert classify_failure(message) == "youtube_changed", what
        pattern = dict(YOUTUBE_CHANGED)[what]
        assert pattern.search(message), f"{what} matches through its own pattern"
        assert classify_failure(RuntimeError(message)) == "youtube_changed"
    for message in OTHER_CASES:
        assert classify_failure(message) == "other", message
        assert classify_failure(OSError(message)) == "other"
    # A wrapped error is classified by its cause too.
    try:
        try:
            raise ValueError(SIGNATURE)
        except ValueError as inner:
            raise RuntimeError("download failed") from inner
    except RuntimeError as outer:
        assert classify_failure(outer) == "youtube_changed"
    assert classify_failure(None) == "other"


# --- a fake store and updater ----------------------------------------------------------


def make_store(tmp_path: Path, versions=("1.0", "2.0"), current="1.0") -> Path:
    ytop = tmp_path / "stick" / ".ytop"
    for version in versions:
        folder = ytop / "components" / "yt-dlp" / version
        folder.mkdir(parents=True)
        (folder / COMPLETE).write_text("")
    write_current(ytop, Current(current={"yt-dlp": current}))
    return ytop


class FakeRunner:
    def __init__(self, paths, *, python=None, cancel=None):
        self.paths = list(paths)

    @property
    def version(self) -> str:
        return self.paths[0].name


class FakeUpdater:
    def __init__(self, to: str | None = "2.0"):
        self.to = to
        self.calls = 0
        self.checks = 0
        self.rollbacks: list[str] = []
        self.block = threading.Event()
        self.block.set()

    def check(self, ytop, config, *, now=None):
        self.checks += 1
        self.block.wait(5)
        record = read_current(ytop)
        record.last_check, record.last_result = (now.isoformat(), "ok")
        write_current(ytop, record)
        return CheckResult("ok", available=[autoupdate.updater.Available("yt-dlp", "1.0", "2.0")])

    def update(self, ytop, config, *, progress=None):
        self.calls += 1
        if progress:
            progress("Testing the new versions…")
        record = read_current(ytop)
        old = record.current.get("yt-dlp")
        if self.to is None or old == self.to:
            return UpdateResult("up_to_date")
        record.previous["yt-dlp"] = old
        record.current["yt-dlp"] = self.to
        write_current(ytop, record)
        return UpdateResult("updated", updated={"yt-dlp": (old, self.to)},
                            message=f"updated yt-dlp {self.to}")

    def rollback(self, ytop, name):
        self.rollbacks.append(name)
        autoupdate.updater.rollback(ytop, name)


def make_auto(ytop: Path, fake: FakeUpdater, config: Config | None = None) -> AutoUpdate:
    return AutoUpdate(
        ytop, lambda: config or Config(),
        check=fake.check, update=fake.update, rollback=fake.rollback,
        make_runner=FakeRunner, loaded=[ytop / "components" / "yt-dlp" / "1.0"],
        base_path=[],
    )


def running_version() -> str:
    """Which yt-dlp the fake probe/download "runs": in-process is 1.0."""
    runner = ytdlp_runner.active()
    return "1.0" if runner is None else runner.version


def wait_for(manager: JobManager, job_id: str, statuses: set[str], timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = manager.get(job_id)
        if job is not None and job.status in statuses:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not reach {statuses}: {manager.get(job_id)}")


def video_item(video_id: str, index: int = 1) -> ProbedItem:
    item = ProbedItem(index=index, id=video_id,
                      url=f"https://www.youtube.com/watch?v={video_id}",
                      title="Solo Video", duration=30.0)
    item.info = {
        "id": video_id, "title": "Solo Video", "duration": 30, "channel": "C",
        "formats": [
            {"format_id": "v", "vcodec": "av01.0.00M.08", "acodec": "none",
             "height": 240, "width": 320, "fps": 15, "ext": "webm"},
            {"format_id": "a", "vcodec": "none", "acodec": "opus",
             "abr": 50, "language": "en", "ext": "webm"},
        ],
    }
    item.selection = Selection(video=item.info["formats"][0], audio=item.info["formats"][1])
    return item


def scripted_probe(monkeypatch, outcomes: list[bool]):
    """probe_video works or fails (signature error) per call, in order;
    records which yt-dlp each call ran."""
    seen: list[str] = []

    def probe_video(video_id, config, *, playlist=None, index=1, **kwargs):
        seen.append(running_version())
        item = video_item(video_id, index)
        if not outcomes.pop(0):
            item.info = item.selection = None
            item.error = SIGNATURE
        return ProbeResult(url=item.url, playlist=playlist or {"id": "un-grouped"},
                           items=[item])

    monkeypatch.setattr(jobs_module, "probe_video", probe_video)
    return seen


# --- on a YouTube-side failure -------------------------------------------------------


def test_youtube_failure_updates_then_retries_once(tmp_path: Path, monkeypatch):
    ytop = make_store(tmp_path)
    fake = FakeUpdater()
    seen = scripted_probe(monkeypatch, [False, True])
    manager = JobManager(lib_root(tmp_path), autoupdate=make_auto(ytop, fake))

    job = manager.create_video_probe("abc12345678")
    job = wait_for(manager, job.id, {"ready", "failed"})

    assert job.status == "ready"
    assert seen == ["1.0", "2.0"]  # once here, once with the new yt-dlp
    assert fake.calls == 1
    assert job.notice == "Updated yt-dlp to 2.0 and retried."
    assert fake.rollbacks == []
    manager.close()


def test_youtube_failure_that_persists_is_reported(tmp_path: Path, monkeypatch):
    ytop = make_store(tmp_path)
    fake = FakeUpdater()
    seen = scripted_probe(monkeypatch, [False, False, False])
    manager = JobManager(lib_root(tmp_path), autoupdate=make_auto(ytop, fake))

    job = wait_for(manager, manager.create_video_probe("abc12345678").id, {"ready", "failed"})

    assert job.status == "failed"
    assert seen == ["1.0", "2.0", "1.0"]  # one retry each, no more
    assert job.notice == "Couldn't fix automatically; try again later."
    assert fake.rollbacks == []
    assert read_current(ytop).current["yt-dlp"] == "2.0"
    manager.close()


def test_nothing_new_means_no_retry(tmp_path: Path, monkeypatch):
    ytop = make_store(tmp_path)
    fake = FakeUpdater(to=None)  # already up to date
    seen = scripted_probe(monkeypatch, [False])
    manager = JobManager(lib_root(tmp_path), autoupdate=make_auto(ytop, fake))

    job = wait_for(manager, manager.create_video_probe("abc12345678").id, {"ready", "failed"})

    assert job.status == "failed"
    assert seen == ["1.0"]
    assert job.notice == "Couldn't fix automatically; try again later."
    manager.close()


def test_youtube_failure_with_checks_off_never_updates(tmp_path: Path, monkeypatch):
    """"Check for updates to YouTube parts" off: no automatic download or
    switch after a failure either; the job suggests Update now."""
    ytop = make_store(tmp_path)
    fake = FakeUpdater()
    seen = scripted_probe(monkeypatch, [False, True])
    off = Config(updates=UpdatesConfig(check_components=False))
    manager = JobManager(lib_root(tmp_path), autoupdate=make_auto(ytop, fake, off))

    job = wait_for(manager, manager.create_video_probe("abc12345678").id, {"ready", "failed"})

    assert job.status == "failed"
    assert seen == ["1.0"]  # no retry
    assert fake.calls == 0 and fake.rollbacks == []
    assert job.notice == autoupdate.CHECKS_OFF
    assert "YouTube" in job.notice and "Update now" in job.notice
    assert read_current(ytop).current["yt-dlp"] == "1.0"
    # Update now is the user's own choice: it still runs.
    update = wait_for(manager, manager.start_update().id, {"done", "failed"})
    assert update.status == "done" and fake.calls == 1
    manager.close()


def test_other_failures_never_update(tmp_path: Path, monkeypatch):
    ytop = make_store(tmp_path)
    fake = FakeUpdater()

    def probe_video(video_id, config, *, playlist=None, **kwargs):
        item = ProbedItem(index=1, id=video_id, url="u")
        item.error = "ERROR: [youtube] abc: Private video"
        return ProbeResult(url="u", playlist=playlist or {}, items=[item])

    monkeypatch.setattr(jobs_module, "probe_video", probe_video)
    manager = JobManager(lib_root(tmp_path), autoupdate=make_auto(ytop, fake))
    job = wait_for(manager, manager.create_video_probe("abc12345678").id, {"ready", "failed"})
    assert job.status == "failed" and fake.calls == 0 and job.notice == ""
    manager.close()


def test_rollback_when_the_old_version_works(tmp_path: Path, monkeypatch):
    ytop = make_store(tmp_path)
    fake = FakeUpdater()
    # Fails here (1.0), fails with 2.0, works with 1.0 again.
    seen = scripted_probe(monkeypatch, [False, False, True])
    manager = JobManager(lib_root(tmp_path), autoupdate=make_auto(ytop, fake))

    job = wait_for(manager, manager.create_video_probe("abc12345678").id, {"ready", "failed"})

    assert seen == ["1.0", "2.0", "1.0"]
    assert job.status == "ready"
    assert fake.rollbacks == ["yt-dlp"]
    record = read_current(ytop)
    assert record.current["yt-dlp"] == "1.0"
    assert record.pinned_bad["yt-dlp"] == ["2.0"]
    assert "went back" in job.notice
    manager.close()


def test_youtube_failure_during_download_retries_the_item(tmp_path: Path, monkeypatch):
    ytop = make_store(tmp_path)
    fake = FakeUpdater()
    seen_probe = scripted_probe(monkeypatch, [True, True, True])
    downloads: list[tuple[str, str]] = []
    policies: list[dict] = []

    def download_probe(probe, config, *, on_item_start=None, on_item_done=None, **kwargs):
        policies.append({k: kwargs.get(k) for k in
                         ("max_height", "target_mb_per_hour", "min_height")})
        results = []
        for item in probe.items:
            version = running_version()
            downloads.append((item.id, version))
            if version == "1.0":
                result = ItemResult(item.index, item.id, item.title, "failed",
                                    error=f"DownloadError: {SIGNATURE}",
                                    error_kind="youtube_changed")
            else:
                result = ItemResult(item.index, item.id, item.title, "downloaded", size=500)
            results.append(result)
            if on_item_done:
                on_item_done(result)
        return FetchResult(tmp_path, probe.playlist, results,
                           total_bytes=sum(r.size for r in results))

    monkeypatch.setattr(jobs_module, "download_probe", download_probe)
    manager = JobManager(lib_root(tmp_path), autoupdate=make_auto(ytop, fake))
    job = wait_for(manager, manager.create_video_probe("abc12345678").id, {"ready"})
    manager.start_download(job.id)
    job = wait_for(manager, job.id, {"done", "failed"})

    assert downloads == [("abc12345678", "1.0"), ("abc12345678", "2.0")], job
    # Read for review (the download uses that review), then with 2.0 for
    # the retry.
    assert seen_probe == ["1.0", "2.0"]
    assert fake.calls == 1
    assert job.status == "done"
    assert (job.downloaded, job.failed) == (1, 0)
    assert job.items[0]["status"] == "downloaded"
    assert job.bytes_downloaded == 500
    assert job.notice == "Updated yt-dlp to 2.0 and retried."
    # The retry keeps the job's efficient policy (download_probe re-allocates
    # the budget for it), exactly as the main pass did.
    assert job.policy == "efficient"
    assert policies[0] == policies[1] == {
        "max_height": job.max_height, "target_mb_per_hour": 100.0,
        "min_height": Config().min_height,
    }
    manager.close()


def test_component_retry_of_a_probe_keeps_the_proxy_that_read_it(tmp_path: Path, monkeypatch):
    ytop = make_store(tmp_path)
    fake = FakeUpdater()
    broken = video_item("abc12345678")
    broken.info = broken.selection = None
    broken.error = SIGNATURE

    def probe_playlist(url, config, **kwargs):
        return ProbeResult(url=url, playlist={"id": "PL1", "title": "L"}, items=[broken])

    def probe_video(video_id, config, *, playlist=None, index=1, **kwargs):
        item = video_item(video_id, index)
        item.proxy = "https://us:89"
        return ProbeResult(url=item.url, playlist=playlist or {}, items=[item])

    monkeypatch.setattr(jobs_module, "probe_playlist", probe_playlist)
    monkeypatch.setattr(jobs_module, "probe_video", probe_video)
    manager = JobManager(lib_root(tmp_path), autoupdate=make_auto(ytop, fake))
    job = wait_for(
        manager,
        manager.create_probe("https://www.youtube.com/playlist?list=PL1").id,
        {"ready", "failed"},
    )
    assert job.status == "ready" and fake.calls == 1
    assert broken.error is None and broken.proxy == "https://us:89"
    manager.close()


def test_proxied_download_failure_never_triggers_an_update(tmp_path: Path, monkeypatch):
    ytop = make_store(tmp_path)
    fake = FakeUpdater()
    scripted_probe(monkeypatch, [True])

    def download_probe(probe, config, *, on_item_done=None, **kwargs):
        for item in probe.items:
            item.proxy = "https://us:89"
        results = [
            ItemResult(item.index, item.id, item.title, "failed",
                       error=f"DownloadError: {SIGNATURE}")
            for item in probe.items
        ]
        for result in results:
            if on_item_done:
                on_item_done(result)
        return FetchResult(tmp_path, probe.playlist, results, total_bytes=0)

    monkeypatch.setattr(jobs_module, "download_probe", download_probe)
    manager = JobManager(lib_root(tmp_path), autoupdate=make_auto(ytop, fake))
    job = wait_for(manager, manager.create_video_probe("abc12345678").id, {"ready"})
    manager.start_download(job.id)
    job = wait_for(manager, job.id, {"done", "failed"})
    assert fake.calls == 0
    assert job.failed == 1
    manager.close()


def test_retried_download_selection_follows_the_efficient_budget(tmp_path: Path, monkeypatch):
    """The real download_probe re-selects a retried video under the budget
    it is given, instead of the probe's full-quality pick."""
    from ytop.yt import download as download_mod

    formats = [
        {"format_id": f"v{h}", "vcodec": "av01", "acodec": "none", "height": h,
         "width": h * 16 // 9, "fps": 30, "ext": "webm", "filesize": h * 1_000_000}
        for h in (240, 480, 1080)
    ] + [{"format_id": "a", "vcodec": "none", "acodec": "opus", "abr": 50,
          "language": "en", "ext": "webm", "filesize": 1_000_000}]
    item = ProbedItem(index=1, id="abc12345678", url="u", title="T", duration=3600.0)
    item.info = {"id": "abc12345678", "duration": 3600, "formats": formats}
    item.selection = download_mod.select_format(item.info, "", 0, False)
    assert item.selection.video["height"] == 1080  # the probe's pick
    monkeypatch.setattr(download_mod, "_process_item",
                        lambda item, *a, **k: (_ for _ in ()).throw(RuntimeError("stop")))
    root = lib_root(tmp_path)
    download_mod.download_probe(
        ProbeResult(url="u", playlist={"id": "un-grouped", "title": "Un-grouped"},
                    items=[item]),
        Config(), jobs=1, quiet=True, max_height=0, target_mb_per_hour=300.0,
        min_height=240, fetch_transcripts=False, library_root=root,
    )
    assert item.selection.video["height"] < 1080


# --- the subprocess runner really imports the chosen yt-dlp ---------------------------


FAKE_YTDLP = '''
VERSION = {version!r}

class YoutubeDL:
    def __init__(self, opts):
        self.opts = opts
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False
    def extract_info(self, url, download=False):
        if "broken" in url:
            raise RuntimeError("ERROR: [youtube] x: Signature extraction failed")
        return {{"id": url.rsplit("=", 1)[-1], "yt_dlp": VERSION,
                 "opts": sorted(self.opts)}}
    def sanitize_info(self, info):
        return info
    def download(self, urls):
        for hook in self.opts.get("progress_hooks", []):
            hook({{"status": "downloading", "downloaded_bytes": 5, "total_bytes": 10}})
        open(self.opts["outtmpl"]["default"].replace("%(ext)s", "webm"), "w").write(VERSION)
'''


def fake_ytdlp(folder: Path, version: str) -> Path:
    package = folder / "yt_dlp"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(FAKE_YTDLP.format(version=version))
    return folder


def test_retry_runner_imports_the_new_components(tmp_path: Path):
    new = fake_ytdlp(tmp_path / "components" / "yt-dlp" / "2.0", "2.0")
    runner = ytdlp_runner.SubprocessRunner([new], python=sys.executable)

    info = runner.extract("https://www.youtube.com/watch?v=abc", {
        "quiet": True, "progress_hooks": [print], "cookiesfrombrowser": ("firefox",),
    })
    assert info is not None
    assert info["yt_dlp"] == "2.0" and info["id"] == "abc"
    assert "progress_hooks" not in info["opts"]

    seen: list[dict] = []
    out = tmp_path / "partial"
    out.mkdir()
    runner.download("https://www.youtube.com/watch?v=abc", {
        "outtmpl": {"default": str(out / "video.%(ext)s")},
        "progress_hooks": [seen.append],
    })
    assert (out / "video.webm").read_text() == "2.0"
    assert seen and seen[0]["downloaded_bytes"] == 5

    from yt_dlp.utils import DownloadError

    with pytest.raises(DownloadError, match="Signature extraction failed") as caught:
        runner.extract("https://www.youtube.com/watch?v=broken", {})
    assert classify_failure(caught.value) == "youtube_changed"

    def cancel_now(data):
        raise JobCancelled("cancelled by the user")

    with pytest.raises(JobCancelled):
        runner.download("https://www.youtube.com/watch?v=abc", {
            "outtmpl": {"default": str(out / "again.%(ext)s")},
            "progress_hooks": [cancel_now],
        })


def test_probe_code_runs_yt_dlp_through_the_active_runner(tmp_path: Path):
    from ytop.yt import probe as probe_mod

    new = fake_ytdlp(tmp_path / "v2", "2.0")
    runner = ytdlp_runner.SubprocessRunner([new], python=sys.executable)
    with ytdlp_runner.using(runner):
        info = probe_mod.extract_video_info("abc12345678", Config())
    assert info["yt_dlp"] == "2.0"


# --- at start ----------------------------------------------------------------------


def test_start_check_runs_once_per_day_in_background(tmp_path: Path):
    ytop = make_store(tmp_path)
    fake = FakeUpdater()
    fake.block.clear()  # the check hangs until released
    home = lib_root(tmp_path)
    auto = make_auto(ytop, fake)
    app = create_app(home, libraries=library_set(home), autoupdate=auto)
    with TestClient(app) as client:
        # The first page never waits for the check.
        assert client.get("/").status_code == 200
        assert fake.calls == 0
        fake.block.set()
        deadline = time.time() + 5
        while fake.calls == 0 and time.time() < deadline:
            time.sleep(0.02)
    assert (fake.checks, fake.calls) == (1, 1)
    assert read_current(ytop).last_check

    # A second start the same day does not check again.
    app = create_app(home, libraries=library_set(home), autoupdate=make_auto(ytop, fake))
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        time.sleep(0.2)
    assert (fake.checks, fake.calls) == (1, 1)

    # Nor when the setting is off; a day later it does.
    record = read_current(ytop)
    record.last_check = "2000-01-01T00:00:00+00:00"
    write_current(ytop, record)
    stop = threading.Event()
    off = Config(updates=UpdatesConfig(check_components=False))
    assert start_check(make_auto(ytop, fake, off), downloading=lambda: False, stop=stop) == "off"
    assert start_check(make_auto(ytop, fake), downloading=lambda: False, stop=stop) == "up_to_date"
    assert fake.checks == 2


def test_no_check_while_a_download_runs(tmp_path: Path, monkeypatch):
    ytop = make_store(tmp_path)
    fake = FakeUpdater()
    scripted_probe(monkeypatch, [True, True])
    release = threading.Event()
    started = threading.Event()

    def download_probe(probe, config, **kwargs):
        started.set()
        release.wait(10)
        return FetchResult(tmp_path, probe.playlist, [], total_bytes=0)

    monkeypatch.setattr(jobs_module, "download_probe", download_probe)
    home = lib_root(tmp_path)
    libs = library_set(home)
    manager = JobManager(home, start_workers=True)
    job = wait_for(manager, manager.create_video_probe("abc12345678").id, {"ready"})
    manager.start_download(job.id)
    assert started.wait(5)
    library_id = libs.default().id
    assert manager.downloading(library_id)
    assert not manager.downloading("some-other-library")

    stop = threading.Event()
    outcome: list[str] = []
    thread = autoupdate.start_check_thread(
        make_auto(ytop, fake), downloading=lambda: manager.downloading(library_id),
        stop=stop, poll_s=0.05, done=outcome.append,
    )
    assert thread is not None
    time.sleep(0.3)
    assert fake.checks == 0  # waits while the download runs
    release.set()
    thread.join(5)
    assert outcome == ["updated"]
    assert (fake.checks, fake.calls) == (1, 1)
    manager.close()


# --- Update now ----------------------------------------------------------------------


def test_update_now_route_needs_csrf_and_starts_a_job(tmp_path: Path):
    ytop = make_store(tmp_path)
    fake = FakeUpdater()
    home = lib_root(tmp_path)
    record = read_current(ytop)
    record.last_check = autoupdate.datetime.now(autoupdate.UTC).isoformat()
    write_current(ytop, record)  # no start check in this test
    app = create_app(home, libraries=library_set(home), autoupdate=make_auto(ytop, fake))
    client = TestClient(app)
    browser = {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}

    refused = client.post("/storage/components/update", headers=browser,
                          follow_redirects=False)
    assert refused.status_code == 403
    assert fake.calls == 0

    response = client.post("/storage/components/update", headers=browser,
                           data={"csrf_token": app.state.csrf_token},
                           follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/downloads"
    jobs = [job for job in app.state.jobs.list() if job.kind == "update"]
    assert len(jobs) == 1
    job = wait_for(app.state.jobs, jobs[0].id, {"done", "failed"})
    assert job.status == "done"
    assert fake.calls == 1
    assert "2.0" in job.notice
    page = client.get("/downloads").text
    assert "YouTube parts" in page and "2.0" in page

    # The setting checkbox, CSRF-guarded too; the rest of config.toml stays.
    from ytop.layout import LibraryLayout

    LibraryLayout(home).config_file.write_text(
        "[web]\nport = 9999\n\n[updates]\ncheck_components = true\n", encoding="utf-8")
    assert client.post("/storage/components/settings", headers=browser,
                       follow_redirects=False).status_code == 403
    response = client.post("/storage/components/settings", headers=browser,
                           data={"csrf_token": app.state.csrf_token},
                           follow_redirects=False)
    assert response.status_code == 303
    from ytop.config import load_config

    assert load_config(home).updates.check_components is False
    assert load_config(home).web.port == 9999
    response = client.post("/storage/components/settings", headers=browser,
                           data={"csrf_token": app.state.csrf_token,
                                 "check_components": "1"},
                           follow_redirects=False)
    assert load_config(home).updates.check_components is True
    app.state.jobs.close()


def test_update_now_without_a_store_is_unavailable(tmp_path: Path):
    home = lib_root(tmp_path)
    auto = AutoUpdate(None, Config)
    app = create_app(home, libraries=library_set(home), autoupdate=auto)
    client = TestClient(app)
    assert client.post("/storage/components/update").status_code == 409
    assert not [job for job in app.state.jobs.list() if job.kind == "update"]
    app.state.jobs.close()


# --- versions in use are never pruned ------------------------------------------------


def yt_dlp_folders(ytop: Path) -> set[str]:
    return {p.name for p in (ytop / "components" / "yt-dlp").iterdir() if p.is_dir()}


def test_prune_keeps_the_versions_this_process_imported(tmp_path: Path):
    from ytop import components as comp

    ytop = make_store(tmp_path, versions=("1.0", "2.0", "3.0"))
    make_auto(ytop, FakeUpdater())  # this process imported 1.0
    # Start check 1.0 -> 2.0, then a failure-triggered 2.0 -> 3.0.
    switched = Current(current={"yt-dlp": "3.0"}, previous={"yt-dlp": "2.0"})
    write_current(ytop, switched)
    comp._prune(ytop, switched, ["yt-dlp"])
    assert yt_dlp_folders(ytop) == {"1.0", "2.0", "3.0"}
    # A rollback (3.0 bad -> 2.0) keeps it too.
    autoupdate.updater.rollback(ytop, "yt-dlp")
    assert yt_dlp_folders(ytop) == {"1.0", "2.0"}

    # Without that protection the old version goes, as before.
    other = make_store(tmp_path / "other", versions=("1.0", "2.0", "3.0"))
    comp._prune(other, switched, ["yt-dlp"])
    assert yt_dlp_folders(other) == {"2.0", "3.0"}


def test_in_use_markers_protect_another_apps_versions(tmp_path: Path, monkeypatch):
    import json
    import os
    import subprocess

    from ytop import components as comp
    from ytop import startup

    ytop = make_store(tmp_path, versions=("1.0", "2.0", "3.0", "4.0", "5.0"))
    markers = ytop / "components" / comp.IN_USE_DIR
    markers.mkdir()
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    live = os.getppid()  # the test runner's parent: alive
    live_start = startup.process_start_time(live)
    for name, pid, started, folder in (
        (f"{live}-aa", live, live_start, "yt-dlp/1.0"),
        (f"{live}-bb", live, None, "yt-dlp/2.0"),  # start time unknown: live
        (f"{dead.pid}-cc", dead.pid, None, "yt-dlp/3.0"),  # dead pid
    ):
        (markers / f"{name}.json").write_text(json.dumps(
            {"pid": pid, "started": started, "folders": [folder]}))
    if live_start is not None:  # a reused pid: same pid, another start time
        (markers / f"{live}-dd.json").write_text(json.dumps(
            {"pid": live, "started": live_start - 3600, "folders": ["yt-dlp/3.0"]}))
    # Temp files a crash left: a dead writer's goes, a live writer's stays.
    (markers / f"{dead.pid}-ee.json{comp.TEMP_SUFFIX}").write_text("{")
    (markers / f"{os.getpid()}-ff.json{comp.TEMP_SUFFIX}").write_text("{")
    switched = Current(current={"yt-dlp": "5.0"}, previous={"yt-dlp": "4.0"})
    write_current(ytop, switched)
    comp._prune(ytop, switched, ["yt-dlp"])

    assert yt_dlp_folders(ytop) == {"1.0", "2.0", "4.0", "5.0"}
    assert {p.name for p in markers.iterdir()} == {
        f"{live}-aa.json", f"{live}-bb.json", f"{os.getpid()}-ff.json{comp.TEMP_SUFFIX}",
    }


def test_in_use_marker_holds_no_personal_data(tmp_path: Path):
    import json
    import os
    import socket

    from ytop import components as comp

    ytop = make_store(tmp_path)
    marker = comp.write_in_use(ytop, [ytop / "components" / "yt-dlp" / "1.0"])
    assert marker is not None
    assert marker.name.startswith(f"{os.getpid()}-") and marker.name.endswith(".json")
    doc = json.loads(marker.read_text())
    assert set(doc) == {"pid", "started", "ticks", "folders"}
    host = socket.gethostname()
    assert host not in marker.name and host not in marker.read_text()
    comp.remove_in_use(ytop)
    assert not marker.exists()


def test_the_deno_and_server_a_process_runs_are_protected(tmp_path: Path, monkeypatch):
    import json

    from ytop import components as comp

    plat = "linux-x86_64"
    monkeypatch.setattr(autoupdate, "_host_platform", lambda: plat)
    ytop = make_store(tmp_path)
    store = ytop / "components"
    for version in ("2.5.0", "2.6.0", "2.7.0"):
        exe = store / "deno" / version / plat / "deno"
        exe.parent.mkdir(parents=True)
        exe.write_text("")
    for version in ("1.0.0", "1.1.0", "1.2.0"):
        home = store / "bgutil-server" / version / "server"
        (home / "src").mkdir(parents=True)
        (home / "src" / "generate_once.ts").write_text("")
    write_current(ytop, Current(current={"yt-dlp": "1.0", "deno": "2.5.0",
                                         "bgutil-server": "1.0.0"}))
    auto = make_auto(ytop, FakeUpdater())  # starts on deno 2.5.0, server 1.0.0
    assert store / "deno" / "2.5.0" in auto.in_use
    assert store / "bgutil-server" / "1.0.0" in auto.in_use
    auto.claim()
    [marker] = list((store / comp.IN_USE_DIR).iterdir())
    assert json.loads(marker.read_text())["folders"] == [
        "bgutil-server/1.0.0", "deno/2.5.0", "yt-dlp/1.0",
    ]
    # Two switches later both are still there.
    later = Current(current={"yt-dlp": "1.0", "deno": "2.7.0", "bgutil-server": "1.2.0"})
    write_current(ytop, later)
    comp._prune(ytop, later, ["deno", "bgutil-server"])
    assert {p.name for p in (store / "deno").iterdir()} == {"2.5.0", "2.7.0"}
    assert {p.name for p in (store / "bgutil-server").iterdir()} == {"1.0.0", "1.2.0"}
    auto.release()


def test_merge_never_copies_in_use_markers(tmp_path: Path):
    from test_components import PLATS, add_component
    from ytop import components as comp

    source, target = tmp_path / "src/.ytop", tmp_path / "dst/.ytop"
    add_component(source, "yt-dlp", "2026.11.01")
    target.mkdir(parents=True)
    assert comp.write_in_use(source, [source / "components/yt-dlp/2026.11.01"])
    comp.merge_newest(source, target, PLATS, None)
    assert comp.read_current(target).current["yt-dlp"] == "2026.11.01"
    assert not (target / "components" / comp.IN_USE_DIR).exists()
    comp.remove_in_use(source)


def test_the_app_leaves_an_in_use_marker_while_it_runs(tmp_path: Path):
    import json

    from ytop import components as comp

    ytop = make_store(tmp_path)
    fake = FakeUpdater()
    record = read_current(ytop)
    record.last_check = autoupdate.datetime.now(autoupdate.UTC).isoformat()
    write_current(ytop, record)
    home = lib_root(tmp_path)
    app = create_app(home, libraries=library_set(home), autoupdate=make_auto(ytop, fake))
    markers = ytop / "components" / comp.IN_USE_DIR
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        [marker] = list(markers.iterdir())
        assert json.loads(marker.read_text())["folders"] == ["yt-dlp/1.0"]
        assert comp.in_use_folders(ytop) == {"yt-dlp/1.0"}
    assert list(markers.iterdir()) == []


def test_a_check_never_runs_alongside_an_update(tmp_path: Path):
    ytop = make_store(tmp_path)
    fake = FakeUpdater()
    fake.block.clear()
    auto = make_auto(ytop, fake)
    checker = threading.Thread(target=auto.check)
    checker.start()
    deadline = time.time() + 5
    while fake.checks == 0 and time.time() < deadline:
        time.sleep(0.01)
    updater_thread = threading.Thread(target=auto.update)
    updater_thread.start()
    time.sleep(0.2)
    assert fake.calls == 0  # waits for the check
    fake.block.set()
    checker.join(5)
    updater_thread.join(5)
    assert fake.calls == 1
    # The check's write did not undo the switch.
    assert read_current(ytop).current["yt-dlp"] == "2.0"
