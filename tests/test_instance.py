"""Single instance per user: discovery, hand-off and takeover."""

from __future__ import annotations

import http.server
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from factories import lib_root, library_set
from ytop import instance
from ytop.cli import main
from ytop.instance import InstanceInfo
from ytop.web.app import create_app


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _info(port: int = 0, version: str = "0.1.0", build_id: str = "", **kw) -> InstanceInfo:
    base = dict(
        pid=os.getpid(), port=port, token=secrets.token_urlsafe(16),
        version=version, build_id=build_id, launch_root="/x", started_at=time.time(),
    )
    base.update(kw)
    return InstanceInfo(**base)


class RunningInstance:
    """A real uvicorn server on an OS-chosen port, recorded like ``serve`` does."""

    def __init__(self, root: Path, version: str, build_id: str = "",
                 active_jobs: int = 0):
        import uvicorn

        self.info = _info(version=version, build_id=build_id, launch_root=str(root))
        self.app = create_app(root, libraries=library_set(root),
                              instance_token=self.info.token)
        self.app.state.instance_version = (version, build_id)
        if active_jobs:
            fake = [SimpleNamespace(active=True)] * active_jobs
            self.app.state.jobs.list = lambda: fake
        self.rescans = 0
        libs = self.app.state.library.libraries
        original = libs.refresh

        def counting_refresh() -> None:
            self.rescans += 1
            original()

        libs.refresh = counting_refresh
        assert instance.claim(self.info)
        self.app.state.shutdown_hooks.append(lambda: instance.release(self.info))
        sock = socket.socket()
        if os.name != "nt":
            # As serve's socket: lingering accepted connections must not
            # keep the port from being rebound after takeover.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        self.info.port = int(sock.getsockname()[1])
        self.server = uvicorn.Server(uvicorn.Config(self.app, log_level="warning"))
        self.app.state.server = self.server
        self.thread = threading.Thread(
            target=self.server.run, kwargs={"sockets": [sock]}, daemon=True
        )
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            assert time.monotonic() < deadline, "server did not start"
            time.sleep(0.02)
        instance.write_info(self.info)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(10)


@pytest.fixture
def portable_root(tmp_path: Path, monkeypatch) -> Path:
    root = lib_root(tmp_path)
    monkeypatch.setenv("YTOP_HOME", str(root))
    return root


@pytest.fixture
def opened(monkeypatch) -> list[str]:
    urls: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url, *a, **k: urls.append(url))
    return urls


def _serve_in_thread(argv: list[str]) -> tuple[threading.Thread, dict]:
    result: dict = {}
    thread = threading.Thread(
        target=lambda: result.update(code=main(argv)), daemon=True
    )
    thread.start()
    return thread, result


def _wait_for_new_instance(old_token: str | None = None) -> InstanceInfo:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        live = instance.read_live()
        if live is not None and live.token != old_token:
            return live
        time.sleep(0.05)
    raise AssertionError("new instance never came up")


def _stop(live: InstanceInfo, thread: threading.Thread, result: dict) -> None:
    assert instance.post(live, "/api/instance/shutdown") == 202
    thread.join(15)
    assert not thread.is_alive()
    assert result["code"] == 0
    assert not instance.instance_path().exists()
    # The lock was released (the lock file itself stays, by design).
    probe = _info()
    assert instance.claim(probe)
    instance.release(probe)


def test_second_launch_opens_existing(portable_root, opened):
    running = RunningInstance(portable_root, version="0.1.0")
    try:
        assert main(["serve", "--portable", "--open"]) == 0
        assert opened == [running.info.url]
        assert running.rescans == 1
        assert running.thread.is_alive()
    finally:
        running.stop()


def test_takeover_when_newer_and_idle(portable_root, opened):
    running = RunningInstance(portable_root, version="0.0.9")
    thread, result = _serve_in_thread(
        ["serve", "--portable", "--no-open", "--port", str(_free_port())]
    )
    running.thread.join(15)
    assert not running.thread.is_alive(), "old instance did not stop"
    live = _wait_for_new_instance(running.info.token)
    assert live.version == "0.1.0"
    assert opened == []
    _stop(live, thread, result)


def test_no_takeover_when_jobs_active(portable_root, opened):
    running = RunningInstance(portable_root, version="0.0.9", active_jobs=2)
    try:
        assert main(["serve", "--portable", "--open"]) == 0
        assert len(opened) == 1
        assert opened[0].startswith(running.info.url + "?offer_switch=1")
        assert running.rescans == 1
        # The launcher's token-checked rescan carried the offer.
        assert running.app.state.switch_offer == portable_root.name
        assert running.thread.is_alive()
    finally:
        running.stop()


def test_offer_switch_banner(tmp_path: Path):
    root = lib_root(tmp_path)
    client = TestClient(create_app(root, libraries=library_set(root)))
    assert "A newer YTOP" not in client.get("/").text
    page = client.get("/", params={"offer_switch": "1", "offer_from": "BLUE"}).text
    assert ("A newer YTOP is available on BLUE; switch when downloads finish"
            in page)
    # A plain link (anyone can make one) is not remembered.
    assert "A newer YTOP" not in client.get("/videos").text


def test_offer_switch_from_the_launcher_stays_on_every_page(tmp_path: Path):
    root = lib_root(tmp_path)
    app = create_app(root, libraries=library_set(root), instance_token="secret")
    client = TestClient(app)
    # Without the instance token the offer is refused.
    assert client.post("/api/instance/rescan",
                       params={"offer_from": "BLUE"}).status_code == 403
    assert "A newer YTOP" not in client.get("/").text
    assert client.post(
        "/api/instance/rescan", params={"offer_from": "BLUE"},
        headers={"X-YTOP-Token": "secret"},
    ).status_code == 204
    for path in ("/", "/videos", "/downloads"):
        assert "A newer YTOP is available on BLUE" in client.get(path).text, path
    # A later spoofed link can't replace the launcher's stick name.
    page = client.get("/", params={"offer_switch": "1", "offer_from": "EVIL"}).text
    assert "EVIL" not in page and "available on BLUE" in page


@pytest.mark.parametrize(("value", "shown"), [
    ("BLUE", "BLUE"),
    ("NO NAME", "NO NAME"),
    ("Neal's stick (2)", "Neal's stick (2)"),
    ("Größe_1-a.b", "Größe_1-a.b"),
    ("x" * 40, "x" * 40),
    ("x" * 41, "another stick"),
    ("", "another stick"),
    ("Call 555-0100 now! Visit evil.example/fix", "another stick"),
    ("BLUE; your library is corrupt", "another stick"),
    ("a\nb", "another stick"),
    ("<b>x</b>", "another stick"),
])
def test_offer_label_accepts_only_stick_like_names(value: str, shown: str):
    from ytop.web.app import offer_label

    assert offer_label(value) == shown


def test_offer_switch_link_with_odd_text_says_another_stick(tmp_path: Path):
    root = lib_root(tmp_path)
    client = TestClient(create_app(root, libraries=library_set(root)))
    page = client.get("/", params={
        "offer_switch": "1", "offer_from": "BLUE; call 555-0100 to fix it",
    }).text
    assert "A newer YTOP is available on another stick;" in page
    assert "555-0100" not in page


def test_stale_instance_file_ignored(portable_root, opened):
    instance.write_info(_info(port=_free_port(), pid=_dead_pid()))
    lock = instance.lock_path()
    lock.write_text(str(_dead_pid()))
    assert instance.read_live() is None
    thread, result = _serve_in_thread(
        ["serve", "--portable", "--no-open", "--port", str(_free_port())]
    )
    live = _wait_for_new_instance()
    assert live.pid == os.getpid()
    _stop(live, thread, result)


_HOLD_LOCK = """
import sys, time
from ytop import instance
info = instance.InstanceInfo(pid=0, port=0, token="child", version="0",
                             build_id="", launch_root="", started_at=0.0)
assert instance.claim(info)
print("locked", flush=True)
time.sleep(60)
"""


def test_lock_of_killed_process_does_not_block_claim():
    child = subprocess.Popen([sys.executable, "-c", _HOLD_LOCK],
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked"
        info = _info()
        assert not instance.claim(info)  # a live holder keeps it
        child.kill()
        child.wait(10)
        assert instance.claim(info)
        instance.release(info)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(10)


def test_release_frees_lock_for_next_claim():
    first, second = _info(), _info()
    assert instance.claim(first)
    assert not instance.claim(second)
    instance.release(first)
    instance.release(first)  # idempotent
    assert instance.claim(second)
    instance.release(second)


def _non_http_server() -> tuple[socket.socket, int]:
    """Answers every connection like an SSH daemon, then hangs up."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()

    def loop() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                conn.sendall(b"SSH-2.0-OpenSSH_9\r\n")

    threading.Thread(target=loop, daemon=True).start()
    return srv, int(srv.getsockname()[1])


def test_non_http_service_on_port_not_trusted(portable_root, opened):
    srv, port = _non_http_server()
    try:
        info = _info(port=port)
        instance.write_info(info)
        assert instance.read_live() is None
        assert instance.post(info, "/api/instance/shutdown") == 0
        thread, result = _serve_in_thread(
            ["serve", "--portable", "--no-open", "--port", str(port)]
        )
        live = _wait_for_new_instance()
        assert live.port != port
        _stop(live, thread, result)
    finally:
        srv.close()


def test_unsafe_instance_dir_refused(tmp_path: Path, monkeypatch, capsys,
                                     portable_root):
    if os.name == "nt":
        pytest.skip("symlink creation needs privileges on Windows")
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    monkeypatch.setenv("YTOP_INSTANCE_DIR", str(link))
    with pytest.raises(instance.InstanceDirError):
        instance.ensure_dir()
    assert main(["serve", "--portable", "--no-open"]) == 1
    assert "serve failed:" in capsys.readouterr().err


def test_instance_file_write_failure_is_reported(portable_root, monkeypatch,
                                                 capsys):
    def boom(info):
        raise OSError("disk full")

    monkeypatch.setattr(instance, "write_info", boom)
    monkeypatch.setattr("ytop.web.app._run_server",
                        lambda *a, **kw: pytest.fail("ran"))
    assert main(["serve", "--portable", "--no-open",
                 "--port", str(_free_port())]) == 1
    assert "serve failed: disk full" in capsys.readouterr().err
    probe = _info()
    assert instance.claim(probe)  # the lock was released
    instance.release(probe)


def test_user_name_fallback(monkeypatch):
    def broken():
        raise KeyError("no passwd entry")

    monkeypatch.setattr(instance.getpass, "getuser", broken)
    expected = str(os.getuid()) if hasattr(os, "getuid") else "user"
    assert instance._user_name() == expected


class _Foreign(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


def test_foreign_server_on_port_not_trusted(portable_root, opened):
    foreign = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Foreign)
    port = foreign.server_address[1]
    threading.Thread(target=foreign.serve_forever, daemon=True).start()
    try:
        instance.write_info(_info(port=port))
        assert instance.read_live() is None
        thread, result = _serve_in_thread(
            ["serve", "--portable", "--no-open", "--port", str(port)]
        )
        live = _wait_for_new_instance()
        assert live.port != port
        assert opened == []
        _stop(live, thread, result)
    finally:
        foreign.shutdown()
        foreign.server_close()


def test_concurrent_launch_single_winner():
    barrier = threading.Barrier(2)
    results: list[bool] = []

    def attempt() -> None:
        barrier.wait()
        results.append(instance.claim(_info()))

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert sorted(results) == [False, True]


def test_shutdown_endpoint_requires_token(tmp_path: Path):
    root = lib_root(tmp_path)
    app = create_app(root, libraries=library_set(root), instance_token="secret")
    stops: list[int] = []
    app.state.request_stop = lambda: stops.append(1)
    client = TestClient(app)
    url = "/api/instance/shutdown"
    assert client.post(url).status_code == 403
    assert client.post(url, headers={"X-YTOP-Token": "wrong"}).status_code == 403
    assert client.get("/api/instance").status_code == 403
    assert client.post("/api/instance/rescan").status_code == 403
    assert stops == []
    # Token-authenticated: a same-origin browser request needs no CSRF token.
    resp = client.post(url, headers={"X-YTOP-Token": "secret",
                                     "Origin": "http://testserver"})
    assert resp.status_code == 202
    deadline = time.monotonic() + 5  # the flush and stop run in the background
    while stops != [1] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert stops == [1]


def test_shutdown_refused_while_jobs_run(tmp_path: Path):
    root = lib_root(tmp_path)
    app = create_app(root, libraries=library_set(root), instance_token="secret")
    app.state.jobs.list = lambda: [SimpleNamespace(active=True),
                                   SimpleNamespace(active=False)]
    stops: list[int] = []
    app.state.request_stop = lambda: stops.append(1)
    client = TestClient(app)
    headers = {"X-YTOP-Token": "secret"}
    data = client.get("/api/instance", headers=headers).json()
    assert data["token_ok"] is True and data["pid"] == os.getpid()
    assert data["active_jobs"] == 1
    assert set(data) == {"token_ok", "pid", "version", "build_id", "active_jobs",
                         "python", "ffmpeg", "ffprobe", "deno", "toolchain_source"}
    assert data["python"] == sys.executable
    assert data["deno"] is None  # not running from a stick
    assert data["toolchain_source"] in {"project", "system"}
    assert client.post("/api/instance/shutdown", headers=headers).status_code == 409
    assert client.post("/api/instance/rescan", headers=headers).status_code == 204
    assert stops == []


def test_instance_status_reports_bundled_tools(tmp_path: Path, monkeypatch):
    runtime = tmp_path / "runtime"
    (runtime / "bin").mkdir(parents=True)
    exe = ".exe" if sys.platform == "win32" else ""
    for name in ("ffmpeg", "ffprobe", "deno"):
        (runtime / "bin" / f"{name}{exe}").write_bytes(b"")
    monkeypatch.setenv("YTOP_RUNTIME", str(runtime))
    monkeypatch.delenv("YTOP_FFMPEG", raising=False)
    monkeypatch.delenv("YTOP_FFPROBE", raising=False)
    root = lib_root(tmp_path)
    app = create_app(root, libraries=library_set(root), instance_token="secret")
    data = TestClient(app).get(
        "/api/instance", headers={"X-YTOP-Token": "secret"}
    ).json()
    for name in ("ffmpeg", "ffprobe", "deno"):
        assert data[name] == str(runtime / "bin" / f"{name}{exe}")
    assert data["toolchain_source"] == "bundled"


def test_decide():
    newer = ("0.2.0", "")
    assert instance.decide(newer, _info(version="0.1.0")) == "takeover"
    assert instance.decide(newer, _info(version="0.1.0", active_jobs=1)) == "open_and_offer"
    assert instance.decide(("0.1.0", ""), _info(version="0.1.0")) == "open"
    assert instance.decide(("0.1.0", ""), _info(version="0.2.0")) == "open"
    assert instance.decide(("0.1.0", "b2"), _info(version="0.1", build_id="b1")) == "takeover"
    assert instance.decide(("0.9.0", ""), _info(version="0.10.0")) == "open"


def test_instance_file_atomic_and_private():
    info = _info(port=1234, active_jobs=3)
    instance.write_info(info)
    data = json.loads(instance.instance_path().read_text())
    assert set(data) == {"pid", "port", "token", "version", "build_id",
                         "launch_root", "started_at"}
    assert instance.read_info() == info
    if os.name != "nt":
        assert instance.instance_dir().stat().st_mode & 0o777 == 0o700
        assert instance.instance_path().stat().st_mode & 0o777 == 0o600
    assert [p.name for p in instance.instance_dir().iterdir()] == ["instance.json"]


def test_takeover_waits_out_a_slow_final_sync(portable_root, opened, monkeypatch):
    """The older YTOP flushes its drives before letting go; a slow stick's
    sync must not make the new launch give up (it used to wait 13 s)."""
    import ytop.cli as cli
    from ytop.web import app as app_module

    monkeypatch.setattr(app_module, "_sync_filesystems", lambda: time.sleep(1.5))
    waits: list[float] = []
    real_wait = instance.wait_port_free

    def spy(port: int, timeout: float = 10.0) -> bool:
        waits.append(timeout)
        return real_wait(port, timeout)

    monkeypatch.setattr(instance, "wait_port_free", spy)
    running = RunningInstance(portable_root, version="0.0.9")
    thread, result = _serve_in_thread(
        ["serve", "--portable", "--no-open", "--port", str(_free_port())]
    )
    running.thread.join(15)
    assert not running.thread.is_alive(), "old instance did not stop"
    live = _wait_for_new_instance(running.info.token)
    assert live.version == "0.1.0"
    assert waits == [cli.TAKEOVER_WAIT_S] and cli.TAKEOVER_WAIT_S >= 60
    monkeypatch.setattr(app_module, "_sync_filesystems", lambda: None)
    _stop(live, thread, result)


# --- #9: a claim that fails half way, a concurrent launch ---------------------


def test_claim_survives_a_failed_pid_write_and_never_leaks_its_lock(monkeypatch):
    first = _info()
    monkeypatch.setattr(instance.os, "ftruncate", _raise(OSError(28, "No space")))
    assert instance.claim(first)  # the pid in the file is informational only
    monkeypatch.undo()
    instance.release(first)
    second = _info()
    monkeypatch.setattr(instance.os, "ftruncate", _raise(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        instance.claim(second)
    monkeypatch.undo()
    third = _info()
    assert instance.claim(third)  # the interrupted claim let go of its lock
    instance.release(third)


def _raise(exc: BaseException):
    def fail(*args, **kwargs):
        raise exc

    return fail


def test_a_launch_beaten_to_the_lock_still_decides(monkeypatch, tmp_path: Path):
    """Two launches at once: the one that loses the lock waits for the
    winner to come up, then decides as usual (here: the winner is older
    and idle, so it is taken over rather than just opened)."""
    import ytop.cli as cli

    older = _info(port=1, version="0.0.1")
    lives = iter([None, older])
    claims = iter([False, True])
    posts: list[str] = []
    monkeypatch.setattr(instance, "read_live", lambda: next(lives))
    monkeypatch.setattr(instance, "claim_wait", lambda info, timeout: next(claims))
    monkeypatch.setattr(instance, "post",
                        lambda live, path, timeout=3.0: posts.append(path) or 202)
    monkeypatch.setattr(instance, "wait_port_free", lambda port, timeout=10.0: True)
    monkeypatch.setattr("atexit.register", lambda fn: None)
    opened: list = []
    monkeypatch.setattr(cli, "_open_existing", lambda *a, **k: opened.append(a) or 0)
    kwargs: dict = {}
    args = SimpleNamespace(open_browser=False)
    assert cli._portable_instance(args, tmp_path, kwargs) is None
    assert posts == ["/api/instance/shutdown"] and opened == []
    assert "instance_token" in kwargs
