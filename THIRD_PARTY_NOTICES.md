# Third-party notices

YTOP itself is MIT licensed (see `LICENSE`). A YTOP stick also carries the
programs and libraries below, each under its own licence. The licences were
checked against the installed package metadata (Python packages) or the
upstream project (the standalone programs); the versions are those pinned in
`stick/versions.toml`, `pyproject.toml` and `uv.lock`. A stick's `.ytop/`
folder holds this file next to the full YTOP licence.

## Programs shipped in the runtime

| Component | Licence | Source |
| --- | --- | --- |
| CPython 3.12 (python-build-standalone) | Python Software Foundation License | https://www.python.org/ and https://github.com/astral-sh/python-build-standalone |
| FFmpeg (`ffmpeg`, `ffprobe`) | LGPL-2.1-or-later | https://ffmpeg.org/ (source: https://git.ffmpeg.org/ffmpeg.git, tag `n7.1.5`) |
| dav1d (linked into FFmpeg) | BSD-2-Clause | https://code.videolan.org/videolan/dav1d (tag `1.5.4`) |
| Deno | MIT | https://github.com/denoland/deno |

**FFmpeg.** YTOP's FFmpeg is a minimal static build made by
`tools/ffmpeg-minimal/` in the YTOP repository. `build.sh` pins the exact
FFmpeg and dav1d sources and configures FFmpeg without `--enable-gpl` and
without `--enable-nonfree`, so the binaries are covered by the LGPL, version
2.1 or later. The build recipe (`tools/ffmpeg-minimal/build.sh` and the
Containerfiles beside it) is the corresponding build information: anyone can
rebuild the binaries from the upstream source named above. FFmpeg is a
trademark of Fabrice Bellard; the FFmpeg developers own the code. See
https://ffmpeg.org/legal.html.

**CPython.** The interpreter is the python-build-standalone distribution of
CPython, under the PSF licence (https://docs.python.org/3/license.html).
Third-party code bundled in that distribution keeps its own licences, which are
included in the distribution's own licence files.

## Python packages (in `lib/`)

| Package | Licence |
| --- | --- |
| yt-dlp | Unlicense (public domain) |
| yt-dlp-ejs | Unlicense AND MIT AND ISC (its bundled meriyah is ISC, astring is MIT) |
| bgutil-ytdlp-pot-provider | GPL-3.0 |
| youtube-transcript-api | MIT |
| FastAPI | MIT |
| Starlette | BSD-3-Clause |
| Uvicorn | BSD-3-Clause |
| Jinja2 | BSD-3-Clause |
| Pillow | MIT-CMU (HPND-style) |
| python-multipart | Apache-2.0 |
| SecretStorage (Linux only; reads Chromium-browser sign-in cookies) | BSD-3-Clause |
| jeepney (Linux only) | MIT |
| cryptography (Linux only) | Apache-2.0 OR BSD-3-Clause |

**bgutil-ytdlp-pot-provider** is distributed under the GNU General Public
License v3 (https://github.com/Brainicism/bgutil-ytdlp-pot-provider). YTOP
loads it as a yt-dlp plugin and ships it unmodified; its source is at that
address and in its PyPI source distribution.

The same project's PO-token **server** (GPL-3.0-only) is also shipped, in the
component store at `.ytop/components/bgutil-server/<version>/server/`, with
its LICENSE beside it. The build keeps only its `src/` and `package.json`
(without the dev tools or the optional native `canvas`), plus the npm
packages `deno install` fetches into `node_modules/`, which carry their own
licence files. The source is the upstream tag tarball pinned in
`stick/versions.toml`.

Other packages that these depend on (for example httpx, pydantic, anyio,
certifi, h11, idna, MarkupSafe, defusedxml and requests) are installed with
their own licence files in each package's `*.dist-info/` folder under `lib/`.
Those files travel with the stick and are the authoritative text.
