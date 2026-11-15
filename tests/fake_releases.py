"""A fake GitHub API, PyPI JSON API and asset host on 127.0.0.1 for the
updater tests (no test ever touches the network).

``FakeReleases`` serves whatever routes a test adds and records every
request (path and headers).  The builders make tiny wheels, Deno zips with
their ``.sha256sum`` files and bgutil server tarballs.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import threading
import zipfile
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ytop.updater import DENO_TRIPLES, Endpoints


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class Route:
    body: bytes
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Request:
    path: str
    headers: dict[str, str]


class FakeReleases:
    def __init__(self) -> None:
        self.routes: dict[str, Route] = {}
        self.requests: list[Request] = []
        self._github: dict[str, list[dict[str, Any]]] = {}
        self._pypi: dict[str, dict[str, Any]] = {}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                fake.requests.append(Request(self.path, dict(self.headers.items())))
                route = fake.routes.get(self.path.split("?", 1)[0])
                if route is None:
                    route = Route(b'{"message": "Not Found"}', 404)
                self.send_response(route.status)
                for key, value in route.headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(route.body)))
                self.end_headers()
                self.wfile.write(route.body)

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def endpoints(self) -> Endpoints:
        return Endpoints(
            github_api=self.base + "/api", github_web=self.base + "/web",
            pypi=self.base + "/pypi-host",
        )

    def add(self, path: str, body: bytes | Any, status: int = 200,
            headers: dict[str, str] | None = None) -> str:
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.routes[path] = Route(body, status, dict(headers or {}))
        return self.base + path

    # --- GitHub ---------------------------------------------------------------

    def github_release(
        self, repo: str, tag: str, assets: dict[str, bytes], *,
        prerelease: bool = False, digests: bool = False,
    ) -> None:
        listed = []
        for name, data in assets.items():
            url = self.add(f"/dl/{repo}/{tag}/{name}", data)
            entry: dict[str, Any] = {"name": name, "browser_download_url": url,
                                     "size": len(data)}
            if digests:
                entry["digest"] = f"sha256:{sha256(data)}"
            listed.append(entry)
        release = {"tag_name": tag, "draft": False, "prerelease": prerelease,
                   "assets": listed}
        releases = self._github.setdefault(repo, [])
        releases.insert(0, release)
        self.add(f"/api/repos/{repo}/releases", releases)
        self.add(f"/api/repos/{repo}/releases/tags/{tag}", release)

    def app_release(self, repo: str, tag: str, assets: dict[str, bytes], *,
                    notes: str = "") -> None:
        """A YTOP release, served as ``releases/latest`` (with its notes)."""
        listed = []
        for name, data in assets.items():
            url = self.add(f"/dl/{repo}/{tag}/{name}", data)
            listed.append({"name": name, "browser_download_url": url, "size": len(data)})
        self.add(f"/api/repos/{repo}/releases/latest", {
            "tag_name": tag, "name": tag, "body": notes, "draft": False,
            "prerelease": False, "assets": listed,
        })

    def ytdlp_release(self, version: str, *, tamper: bool = False,
                      prerelease: bool = False) -> bytes:
        wheel = make_wheel("yt-dlp", version, {
            "yt_dlp/__init__.py": f"__version__ = {version!r}\n",
            "yt_dlp/version.py": f"__version__ = {version!r}\n",
        })
        name = f"yt_dlp-{version}-py3-none-any.whl"
        sums = f"{sha256(wheel)}  {name}\n{'0' * 64}  yt-dlp.exe\n".encode()
        served = wheel + b"tampered" if tamper else wheel
        self.github_release("yt-dlp/yt-dlp", version, {
            name: served, "SHA2-256SUMS": sums, "yt-dlp.exe": b"MZ",
        }, prerelease=prerelease)
        return wheel

    def deno_release(self, version: str, platforms: list[str] | None = None, *,
                     tamper: str | None = None) -> None:
        assets: dict[str, bytes] = {}
        for plat in platforms or list(DENO_TRIPLES):
            data = make_deno_zip(plat, version)
            name = f"deno-{DENO_TRIPLES[plat]}.zip"
            assets[name] = data + (b"x" if tamper == plat else b"")
            assets[name + ".sha256sum"] = f"{sha256(data)}  {name}\n".encode()
        self.github_release("denoland/deno", f"v{version}", assets)

    def server_archive(self, version: str, dependencies: dict[str, str] | None = None) -> None:
        self.add(
            f"/web/Brainicism/bgutil-ytdlp-pot-provider/archive/refs/tags/{version}.tar.gz",
            make_server_tarball(version, dependencies),
        )

    # --- PyPI -----------------------------------------------------------------

    def pypi_release(self, dist: str, version: str, files: dict[str, str] | None = None,
                     *, tamper: bool = False, metadata: str = "",
                     yanked: bool = False) -> bytes:
        package = dist.replace("-", "_")
        wheel = make_wheel(dist, version, files or {
            f"{package}/__init__.py": f"__version__ = {version!r}\n",
        }, metadata=metadata)
        name = f"{package}-{version}-py3-none-any.whl"
        url = self.add(f"/files/{dist}/{name}", wheel + (b"x" if tamper else b""))
        doc = self._pypi.setdefault(dist, {"info": {"name": dist}, "releases": {}})
        doc["releases"][version] = [
            {"filename": f"{package}-{version}.tar.gz", "packagetype": "sdist",
             "url": url + ".tar.gz", "digests": {"sha256": "0" * 64}, "yanked": False},
            {"filename": name, "packagetype": "bdist_wheel", "url": url,
             "digests": {"sha256": sha256(wheel)}, "yanked": yanked},
        ]
        self.add(f"/pypi-host/pypi/{dist}/json", doc)
        return wheel


def make_wheel(dist: str, version: str, files: dict[str, str], *,
               metadata: str = "", extra: dict[str, bytes] | None = None) -> bytes:
    """A tiny wheel: the files, plus a dist-info (METADATA, WHEEL, RECORD)
    and, like real ones, a ``.data`` folder that must not be installed."""
    info = f"{dist.replace('-', '_')}-{version}.dist-info"
    entries: dict[str, bytes] = {rel: text.encode() for rel, text in files.items()}
    entries[f"{dist.replace('-', '_')}-{version}.data/scripts/{dist}"] = b"#!python\n"
    entries.update(extra or {})
    entries[f"{info}/METADATA"] = (
        f"Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\n{metadata}"
    ).encode()
    entries[f"{info}/WHEEL"] = b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    entries[f"{info}/RECORD"] = "".join(f"{rel},,\n" for rel in [*entries, f"{info}/RECORD"]).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for rel, data in entries.items():
            zf.writestr(rel, data)
    return buf.getvalue()


def deno_script(version: str) -> bytes:
    return f'#!/bin/sh\necho "deno {version} (stable, release)"\n'.encode()


def make_deno_zip(plat: str, version: str) -> bytes:
    name = "deno.exe" if plat.startswith("windows") else "deno"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info = zipfile.ZipInfo(name)
        info.external_attr = 0o755 << 16
        data = deno_script(version) if plat.startswith("linux") else f"deno {version} {plat}".encode()
        zf.writestr(info, data)
    return buf.getvalue()


def make_server_tarball(version: str, dependencies: dict[str, str] | None = None) -> bytes:
    root = f"bgutil-ytdlp-pot-provider-{version}"
    files = {
        f"{root}/LICENSE": b"GPL text\n",
        f"{root}/server/src/generate_once.ts": f"// script {version}\n".encode(),
        f"{root}/server/package.json": json.dumps({
            "name": "bgutil", "version": version,
            "dependencies": dependencies or {"axios": "^1", "jsdom": "^29"},
            "devDependencies": {"eslint": "^10"},
        }).encode(),
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()
