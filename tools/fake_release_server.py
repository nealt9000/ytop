#!/usr/bin/env python3
"""A fake GitHub releases API on 127.0.0.1, for CI and rehearsals (test-only).

    python tools/fake_release_server.py <root> [--port 0] [--port-file F] [--log F]

``<root>/<owner>/<repo>/<tag>/<files>`` is one release; the newest folder
(by modification time) is ``releases/latest``.  Folders are read on every
request, so a release can be added while the server runs.  Serves::

    /api/repos/<owner>/<repo>/releases/latest   (also /releases)
    /dl/<owner>/<repo>/<tag>/<file>

Point a stick at it with ``[updates] api_base = "http://127.0.0.1:<port>/api"``
and ``repo = "<owner>/<repo>"``.  Every request path is appended to ``--log``."""

from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _releases(root: Path, repo: str) -> list[Path]:
    folder = root / repo
    tags = [p for p in folder.iterdir() if p.is_dir()] if folder.is_dir() else []
    return sorted(tags, key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)


def _document(base: str, repo: str, tag: Path) -> dict[str, object]:
    assets = [
        {"name": f.name, "size": f.stat().st_size,
         "browser_download_url": f"{base}/dl/{repo}/{tag.name}/{f.name}"}
        for f in sorted(tag.iterdir()) if f.is_file()
    ]
    return {"tag_name": tag.name, "name": tag.name, "body": "CI fake release",
            "draft": False, "prerelease": False, "assets": assets}


def make_server(root: Path, log: Path | None = None, port: int = 0) -> ThreadingHTTPServer:
    root = Path(root)
    lock = threading.Lock()
    holder: list[ThreadingHTTPServer] = []

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, kind: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if log is not None:
                with lock, open(log, "a", encoding="utf-8") as handle:
                    handle.write(path + "\n")
            base = f"http://127.0.0.1:{holder[0].server_port}"
            parts = path.strip("/").split("/")
            if len(parts) >= 5 and parts[0] == "api" and parts[1] == "repos":
                repo = f"{parts[2]}/{parts[3]}"
                if ".." in repo or parts[4] != "releases":
                    return self._send(404, b'{"message": "Not Found"}')
                tags = _releases(root, repo)
                if len(parts) == 6 and parts[5] == "latest" and tags:
                    return self._send(
                        200, json.dumps(_document(base, repo, tags[0])).encode())
                if len(parts) == 5:
                    return self._send(200, json.dumps(
                        [_document(base, repo, t) for t in tags]).encode())
            if len(parts) == 5 and parts[0] == "dl":
                repo = f"{parts[1]}/{parts[2]}"
                for tag in _releases(root, repo):
                    target = tag / parts[4]
                    if tag.name == parts[3] and target.is_file():
                        return self._send(200, target.read_bytes(),
                                          "application/octet-stream")
            self._send(404, b'{"message": "Not Found"}')

        def log_message(self, *args: object) -> None:
            pass

    holder.append(ThreadingHTTPServer(("127.0.0.1", port), Handler))
    return holder[0]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root", type=Path)
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--port-file", type=Path, default=None)
    ap.add_argument("--log", type=Path, default=None)
    opts = ap.parse_args(argv)
    server = make_server(opts.root, opts.log, opts.port)
    port = server.server_port
    if opts.port_file is not None:
        opts.port_file.write_text(f"{port}\n", encoding="utf-8")
    print(f"serving {opts.root} on http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
