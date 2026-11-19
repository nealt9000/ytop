"""Top-level CLI: ``ytop init|migrate|doctor|probe|fetch|transcripts|index|serve|make-stick|components|app-update``."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .config import SESSION_AUTH, load_config

# How long a takeover waits for the older YTOP to flush and let go.
TAKEOVER_WAIT_S = 60.0


def _common(parser: argparse.ArgumentParser) -> None:
    # The root parser owns the real default; repeating the flag after the
    # subcommand is accepted too and must not reset it when absent.
    parser.add_argument("--library", default=argparse.SUPPRESS,
                        help="launch library (default: $YTOP_LIBRARY or ~/Videos/YTOP)")
    parser.add_argument("--cookies-from-browser", default=None,
                        help="browser name, optionally name:profile (this run only; never saved)")
    parser.add_argument("--audio-lang", default=None,
                        help="preferred audio language (default: en)")


def _config_overrides(args) -> dict:
    """Config values set by flags actually given on the command line."""
    overrides: dict = {}
    if getattr(args, "audio_lang", None) is not None:
        overrides["audio_language"] = args.audio_lang
    if getattr(args, "max_height", None) is not None:
        overrides["max_height"] = int(args.max_height)
    if getattr(args, "probe_jobs", None) is not None:
        overrides["probe_jobs"] = int(args.probe_jobs)
    if getattr(args, "target_mb_per_hour", None) is not None:
        overrides["target_mb_per_hour"] = float(args.target_mb_per_hour)
    if getattr(args, "native", False):
        overrides["target_mb_per_hour"] = 0.0
        overrides["max_height"] = 0
    return overrides


def _launch_root(args) -> Path:
    """The launch library: --library, else $YTOP_LIBRARY, else $YTOP_HOME
    (the stick a launcher started from), else ~/Videos/YTOP."""
    value = (
        getattr(args, "library", None)
        or os.environ.get("YTOP_LIBRARY")
        or os.environ.get("YTOP_HOME")
    )
    return Path(value or "~/Videos/YTOP").expanduser()


def _missing_library(root: Path) -> None:
    print(
        f"No YTOP library at {root}. Run 'ytop init {root}' "
        f"or 'ytop migrate <old> {root}'.",
        file=sys.stderr,
    )


def _read_only(root: Path) -> bool:
    """True (and the reason printed) when the library at ``root`` was made by
    a newer YTOP, so this one must not write to it."""
    from .layout import SCHEMA_VERSION, read_library
    from .libraries import READ_ONLY_MESSAGE

    info = read_library(root)
    if info is not None and info.schema > SCHEMA_VERSION:
        print(f"{root}: {READ_ONLY_MESSAGE}", file=sys.stderr)
        return True
    return False


def _require_library(root: Path, *, writable: bool = False) -> bool:
    """True when ``root`` holds a library (one this YTOP may write to, when
    ``writable``); otherwise print why."""
    from .layout import read_library

    if read_library(root) is None:
        _missing_library(root)
        return False
    return not (writable and _read_only(root))


def _config_from_args(args, root: Path | None = None):
    cfg = load_config(root if root is not None else _launch_root(args))
    if args.cookies_from_browser:
        SESSION_AUTH.enable(args.cookies_from_browser)
    for name, value in _config_overrides(args).items():
        setattr(cfg, name, value)
    return cfg


def _duration(value) -> str:
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return "?"
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def _probe_payload(result, cfg) -> dict:
    from .yt.formats import select_format

    pl = result.playlist
    items = []
    for item in result.items:
        selection = item.selection
        items.append({
            "index": item.index,
            "id": item.id,
            "title": item.title,
            "duration": item.duration,
            "error": item.error,
            "selected": None if selection is None else {
                "video_id": selection.video.get("format_id"),
                "audio_id": selection.audio.get("format_id") if selection.audio else None,
                "video_codec": selection.video_family,
                "audio_codec": selection.audio_family,
                "height": selection.video.get("height"),
                "fps": selection.video.get("fps"),
                "container": selection.container,
                "estimated_bytes": selection.estimated_bytes,
            },
        })
    native_bytes = 0
    for item in result.items:
        if item.info is None:
            continue
        try:
            native_bytes += select_format(
                item.info, cfg.audio_language, None
            ).estimated_bytes
        except Exception:  # noqa: BLE001 - the item may already be recorded
            continue
    return {
        "url": result.url,
        "id": pl.get("id"),
        "title": pl.get("title"),
        "channel": pl.get("channel") or pl.get("uploader"),
        "description": pl.get("description"),
        "video_count": len(result.items),
        "policy": _size_policy(cfg),
        "target_mb_per_hour": cfg.target_mb_per_hour,
        "min_height": cfg.min_height,
        "max_height": cfg.max_height,
        "estimated_bytes": result.estimated_bytes,
        "estimate_native_bytes": native_bytes,
        "items": items,
    }


def cmd_probe(args) -> int:
    from .yt.probe import ProbeError, probe_playlist

    if args.limit is not None and args.limit < 1:
        print("--limit must be at least 1", file=sys.stderr)
        return 2
    try:
        cfg = _config_from_args(args)
    except (OSError, ValueError) as exc:
        print(f"config failed: {exc}", file=sys.stderr)
        return 1
    try:
        result = probe_playlist(
            args.playlist, cfg, limit=args.limit,
            max_height=cfg.max_height,
            target_mb_per_hour=cfg.target_mb_per_hour,
            min_height=cfg.min_height,
            probe_jobs=cfg.probe_jobs,
            cache_root=_launch_root(args),
        )
    except (ProbeError, ValueError, RuntimeError) as exc:
        print(f"probe failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(
            _probe_payload(result, cfg), ensure_ascii=False, indent=2
        ))
        return 1 if result.failures else 0

    pl = result.playlist
    print(f"playlist: {pl.get('title') or pl.get('id')}")
    print(f"channel:  {pl.get('channel') or pl.get('uploader') or '?'}")
    print(f"videos:   {len(result.items)}")
    print(f"policy:   {_size_policy(cfg)}")
    print(f"estimate: {_format_bytes(result.estimated_bytes)}")
    if cfg.target_mb_per_hour:
        native = _probe_payload(result, cfg)["estimate_native_bytes"]
        print(f"native:   {_format_bytes(native)}")
    print()
    print(f"{'#':>3}  {'id':<12} {'duration':>8}  {'selection':<38} {'size':>9}  title")
    for item in result.items:
        if item.selection:
            selection = item.selection.summary()
            size = _format_bytes(item.selection.estimated_bytes)
        else:
            selection = f"ERROR: {item.error or 'no format'}"
            size = "-"
        print(f"{item.index:>3}  {item.id:<12} {_duration(item.duration):>8}  "
              f"{selection:<38} {size:>9}  {item.title}")
    if result.failures:
        print(f"\n{len(result.failures)} item(s) failed to probe", file=sys.stderr)
        return 1
    return 0


def cmd_fetch(args) -> int:
    from .yt.download import fetch_playlist
    from .yt.probe import ProbeError

    if args.limit is not None and args.limit < 1:
        print("--limit must be at least 1", file=sys.stderr)
        return 2
    if args.jobs is not None and args.jobs < 1:
        print("--jobs must be at least 1", file=sys.stderr)
        return 2
    launch = _launch_root(args)
    if args.to:
        root = _resolve_destination(args.to, launch)
        if root is None:
            print(
                f"--to {args.to}: not a known library id or a library path "
                "(a folder with a YTOP library marker)",
                file=sys.stderr,
            )
            return 2
        if _read_only(root):
            return 2
    else:
        root = launch
        if not _require_library(root, writable=True):
            return 2
    try:
        cfg = _config_from_args(args, root)
    except (OSError, ValueError) as exc:
        print(f"config failed: {exc}", file=sys.stderr)
        return 1
    try:
        result = fetch_playlist(
            args.playlist,
            cfg,
            limit=args.limit,
            jobs=args.jobs,
            force=args.force,
            dry_run=args.dry_run,
            max_height=cfg.max_height,
            target_mb_per_hour=cfg.target_mb_per_hour,
            min_height=cfg.min_height,
            preserve_audio=True if args.preserve_audio else None,
            library_root=root,
        )
    except (ProbeError, ValueError, RuntimeError) as exc:
        print(f"fetch failed: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        return 1 if any(i.status == "failed" for i in result.items) else 0
    return 1 if result.failed else 0


def _resolve_destination(value: str, launch: Path) -> Path | None:
    """A library id (via the library set) or a path with a marker, else None."""
    from .layout import read_library
    from .libraries import LibrarySet

    path = Path(value).expanduser()
    if read_library(path) is not None:
        return path
    try:
        ref = LibrarySet(launch).get(value)
    except OSError:
        ref = None
    return ref.root if ref is not None else None


def _size_policy(cfg) -> str:
    if not cfg.target_mb_per_hour:
        return "native maximum (no re-encode)"
    cap = f"; {cfg.max_height}p cap" if cfg.max_height else ""
    return f"efficient, {cfg.target_mb_per_hour:g} MB/h budget{cap}"


def _split_ids(value: str | None) -> list[str] | None:
    if value is None:
        return None
    ids = [part.strip() for part in value.split(",") if part.strip()]
    if not ids:
        raise ValueError("--videos needs at least one id")
    return ids


def cmd_transcripts(args) -> int:
    from dataclasses import asdict

    from .layout import LibraryLayout
    from .targets import (
        TargetError,
        parse_target,
        resolve_playlist,
        resolve_video,
    )
    from .transcripts.fetch import (
        fetch_playlist_transcripts,
        fetch_video_transcript,
        write_playlist_summary,
    )
    from .transcripts.store import index_library, index_playlist_dir

    if args.limit is not None and args.limit < 1:
        print("--limit must be at least 1", file=sys.stderr)
        return 2
    root = _launch_root(args)
    if not _require_library(root, writable=True):
        return 2
    try:
        cfg = _config_from_args(args, root)
        layout = LibraryLayout(root)
        requested = _split_ids(args.videos)
        languages = (
            [part.strip() for part in args.languages.split(",") if part.strip()]
            if args.languages else None
        )
    except (OSError, ValueError) as exc:
        print(f"transcripts failed: {exc}", file=sys.stderr)
        return 1

    if args.index_only:
        try:
            if args.target:
                target = parse_target(args.target)
                if target.kind == "playlist":
                    resolved = resolve_playlist(layout, target.id)
                    if resolved is None:
                        raise TargetError(f"playlist {target.id} is not in {root}")
                    playlist_dir, doc = resolved
                    count = index_playlist_dir(layout, playlist_dir, doc)
                else:
                    resolved = resolve_video(layout, target.id)
                    if resolved is None:
                        raise TargetError(f"video {target.id} is not in {root}")
                    playlist_dir, doc, _entry, _video_dir, _meta = resolved
                    count = index_playlist_dir(layout, playlist_dir, doc)
            else:
                count = index_library(layout)
        except (TargetError, OSError) as exc:
            print(f"index failed: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps({"indexed": count}, indent=2))
        else:
            print(f"indexed {count} transcript(s) from {root}")
        return 0

    if not args.target:
        print("transcripts needs a target (or use --index-only)", file=sys.stderr)
        return 2
    if args.videos and args.target:
        try:
            target = parse_target(args.target)
        except TargetError as exc:
            print(f"transcripts failed: {exc}", file=sys.stderr)
            return 1
        if target.kind != "playlist":
            print("--videos applies to playlist targets only", file=sys.stderr)
            return 2

    try:
        target = parse_target(args.target)
        if target.kind == "playlist":
            resolved = resolve_playlist(layout, target.id)
            if resolved is None:
                raise TargetError(
                    f"playlist {target.id} is not in {root}; "
                    "run `ytop fetch` first"
                )
            playlist_dir, doc = resolved
            video_ids = None
            if requested is not None:
                known = {str(e.get("id")) for e in doc.get("entries") or []}
                missing = [v for v in requested if v not in known]
                if missing:
                    raise TargetError("not in this playlist: " + ", ".join(missing))
                video_ids = requested
            elif args.limit is not None:
                video_ids = [
                    str(e.get("id")) for e in (doc.get("entries") or [])[: args.limit]
                    if e.get("id")
                ]
            if not args.json:
                print(f"playlist: {doc.get('title') or doc.get('id')}")
                print(f"target:   {playlist_dir}")
            results = fetch_playlist_transcripts(
                playlist_dir,
                doc,
                config=cfg,
                video_ids=video_ids,
                languages=languages,
                force=args.force,
                delay_s=args.delay,
                layout=layout,
                index=not args.no_index,
                show_progress=not args.json,
            )
        else:
            resolved = resolve_video(layout, target.id)
            if resolved is None:
                raise TargetError(
                    f"video {target.id} is not in {root}; "
                    "run `ytop fetch` first"
                )
            playlist_dir, doc, entry, video_dir, _meta = resolved
            if not args.json:
                print(f"video:  {entry.get('title') or target.id}")
                print(f"target: {video_dir}")
            result = fetch_video_transcript(
                video_dir,
                playlist_id=str(doc.get("id") or ""),
                config=cfg,
                languages=languages,
                force=args.force,
                layout=layout,
                index=not args.no_index,
            )
            results = [result]
            if not args.json:
                detail = (
                    f" ({result.language}, {result.segments} segments)"
                    if result.status == "fetched" else
                    (f" ({result.error})" if result.error else "")
                )
                print(f"  {result.id}: {result.status}{detail}")
            known = {
                str(e.get("id")) for e in doc.get("entries") or []
            }
            if target.id in known:
                write_playlist_summary(playlist_dir, doc, results)
    except (TargetError, OSError, RuntimeError, ValueError) as exc:
        print(f"transcripts failed: {exc}", file=sys.stderr)
        return 1

    totals = {
        "videos": len(results),
        "fetched": sum(1 for r in results if r.status == "fetched"),
        "skipped": sum(1 for r in results if r.status == "skipped"),
        "missing": sum(1 for r in results if r.status == "missing"),
        "failed": sum(1 for r in results if r.status == "failed"),
        "segments": sum(r.segments for r in results),
    }
    if args.json:
        print(json.dumps({
            "target": {"kind": target.kind, "id": target.id},
            "videos": [asdict(r) for r in results],
            "totals": totals,
        }, ensure_ascii=False, indent=2))
    else:
        print()
        print(
            f"done: {totals['fetched']} fetched, {totals['skipped']} current, "
            f"{totals['missing']} without captions, {totals['failed']} failed; "
            f"{totals['segments']} segments"
        )
        if totals["missing"] and not totals["fetched"]:
            print("note: no captions were available (YouTube rate-limits caption "
                  "downloads); the library is unaffected.", file=sys.stderr)
    return 1 if totals["failed"] else 0


def cmd_index(args) -> int:
    import sqlite3

    from .index import index_for
    from .layout import LibraryLayout
    from .transcripts.store import db_path, index_library

    root = _launch_root(args)
    if not _require_library(root, writable=True):
        return 2
    try:
        layout = LibraryLayout(root)
        count = index_library(layout)
        index = index_for(layout)
        index.rebuild()
        rows = index.playlists()
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"index failed: {exc}", file=sys.stderr)
        return 1
    playlists = len(rows)
    videos = sum(len(p.videos) for p in rows)
    if args.json:
        print(json.dumps({
            "indexed": count,
            "playlists": playlists,
            "videos": videos,
            "database": str(db_path(layout)),
        }, indent=2))
    else:
        print(
            f"indexed {count} transcript(s), {playlists} playlist(s) and "
            f"{videos} video(s) into {db_path(layout)}"
        )
    return 0


def _open_existing(args, live, *, offer_from: str | None = None) -> int:
    """Hand this launch to the running instance: rescan, show it, exit."""
    import webbrowser
    from urllib.parse import urlencode

    from . import instance

    rescan = "/api/instance/rescan"
    if offer_from is not None:
        # Token-checked, so the server keeps the offer on every page.
        rescan += "?" + urlencode({"offer_from": offer_from})
    instance.post(live, rescan)
    url = live.url
    if offer_from is not None:
        url += "?" + urlencode({"offer_switch": "1", "offer_from": offer_from})
    print(f"YTOP is already running at {live.url}")
    if args.open_browser is not False:
        webbrowser.open(url)
    return 0


def _stick_name(root: Path) -> str:
    return root.name or root.anchor or str(root)


def _portable_instance(args, root: Path, serve_kwargs: dict) -> int | None:
    """Single-instance handling for ``serve --portable``.

    Returns an exit code when this launch is done (handed to a running
    instance), or ``None`` after claiming the lock, with ``serve_kwargs``
    filled so the server records itself and cleans up on exit.
    """
    import atexit
    import secrets
    import time

    from . import __build__, __version__, instance

    mine = (__version__, __build__)
    info = instance.InstanceInfo(
        pid=os.getpid(), port=0, token=secrets.token_urlsafe(32),
        version=__version__, build_id=__build__, launch_root=str(root),
        started_at=time.time(),
    )
    claim_timeout = 0.0
    live = instance.read_live()
    # A launch that loses the lock to a concurrent one waits for it to come
    # up and then decides again (it may be older and idle: take it over).
    claimed = False
    for _round in range(3):
        if live is not None:
            decision = instance.decide(mine, live)
            if decision == "takeover":
                status = instance.post(live, "/api/instance/shutdown")
                if status == 409:  # a download started meanwhile
                    decision = "open_and_offer"
                else:
                    print(f"Replacing the older YTOP {live.version} that was running "
                          "(it finishes writing to its drive first)…", flush=True)
                    # It flushes its drives before letting go, and a slow stick's
                    # final sync can take many seconds; both waits end early.
                    instance.wait_port_free(live.port, TAKEOVER_WAIT_S)
                    claim_timeout = TAKEOVER_WAIT_S
            if decision == "open":
                return _open_existing(args, live)
            if decision == "open_and_offer":
                return _open_existing(args, live, offer_from=_stick_name(root))
        if instance.claim_wait(info, claim_timeout):
            claimed = True
            break
        # Another launch holds the lock; give it a moment to come up.
        live = None
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            live = instance.read_live()
            if live is not None:
                break
            time.sleep(0.2)
        if live is None:
            break
    if not claimed and live is not None:  # rounds used up: just show it
        return _open_existing(args, live)
    if not claimed:
        print("serve failed: another YTOP is starting; try again",
              file=sys.stderr)
        return 1

    def on_bound(port: int) -> None:
        info.port = port
        instance.write_info(info)

    def cleanup() -> None:
        instance.release(info)

    atexit.register(cleanup)
    serve_kwargs.update(
        instance_token=info.token, on_bound=on_bound, on_shutdown=cleanup
    )
    return None


def cmd_serve(args) -> int:
    from .web.app import serve

    portable = bool(getattr(args, "portable", False))
    if portable:
        if args.host:
            print("serve failed: --portable always binds 127.0.0.1; "
                  "--host is not allowed", file=sys.stderr)
            return 2
        home = os.environ.get("YTOP_HOME")
        if not home:
            print("serve failed: --portable needs YTOP_HOME (start YTOP "
                  "from the stick launcher)", file=sys.stderr)
            return 2
        root = Path(home).expanduser()
    else:
        root = _launch_root(args)
    if not _require_library(root):
        return 2
    try:
        cfg = _config_from_args(args, root)
    except (OSError, ValueError) as exc:
        print(f"serve failed: {exc}", file=sys.stderr)
        return 1
    instance_kwargs: dict = {}
    if portable:
        try:
            handled = _portable_instance(args, root, instance_kwargs)
        except OSError as exc:
            print(f"serve failed: {exc}", file=sys.stderr)
            return 1
        if handled is not None:
            return handled
    try:
        serve(
            root,
            cfg,
            host=args.host,
            port=args.port,
            open_browser=args.open_browser,
            config_overrides=_config_overrides(args),
            portable=portable,
            **instance_kwargs,
        )
    except (OSError, ValueError) as exc:
        # e.g. no free port, or the instance file could not be written.
        print(f"serve failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        # Also covers a server that never started; release is idempotent.
        release = instance_kwargs.get("on_shutdown")
        if release is not None:
            release()
    return 0


def cmd_init(args) -> int:
    from .layout import init_library

    root = Path(args.path).expanduser()
    label = (args.label or "").strip() or root.name or "Library"
    try:
        info = init_library(root, label, args.description or "")
    except OSError as exc:
        print(f"init failed: {exc}", file=sys.stderr)
        return 1
    print(info.id)
    return 0


def cmd_migrate(args) -> int:
    from .migrate import plan_migration, run_migration

    old = Path(args.old).expanduser()
    target = Path(args.target).expanduser()
    if _read_only(target):
        return 2
    try:
        plan = plan_migration(old, target)
    except (OSError, ValueError) as exc:
        print(f"migrate failed: {exc}", file=sys.stderr)
        return 2
    print(f"Migrating {old} -> {target}")
    for playlist in plan.playlists:
        media = sum(1 for video in playlist.videos if video.media is not None)
        state = "  (already migrated)" if playlist.migrated else ""
        if playlist.skip_reason:
            state = f"  (SKIPPED: {playlist.skip_reason})"
        print(f"  {playlist.title} [{playlist.id}]: {len(playlist.videos)} video(s), "
              f"{media} with media{state}")
    print(f"media to move (same drive): {_format_bytes(plan.bytes_to_move)}")
    print(f"media to copy (other drive): {_format_bytes(plan.bytes_to_copy)}")
    print(f"partial downloads dropped (left in the old folder): "
          f"{len(plan.dropped_partials)}")
    for path in plan.dropped_partials:
        print(f"  {path}")
    if plan.unscanned:
        print(f"will not be migrated (left behind): {len(plan.unscanned)}")
        for path, reason in plan.unscanned:
            print(f"  {path}  ({reason})")
    for warning in plan.warnings:
        print(f"warning: {warning}")
    if args.dry_run:
        print("dry run: nothing was changed")
        return 0

    def progress(number: int, total: int, playlist) -> None:
        print(f"[{number}/{total}] {playlist.title}", flush=True)

    try:
        report = run_migration(plan, target, progress_cb=progress)
    except Exception as exc:  # noqa: BLE001 - any failure is resumable
        print(f"migration stopped: {exc}", file=sys.stderr)
        print("Nothing was deleted. Fix the problem and rerun the same command "
              "to resume.", file=sys.stderr)
        return 1
    print(f"migrated {len(report.migrated)} playlist(s) "
          f"({len(report.skipped)} already done) into {target}: "
          f"{report.moved} file(s) moved, {report.copied} copied, "
          f"{report.references} shared reference(s), "
          f"{len(report.without_media)} video(s) without media, "
          f"{report.jobs_carried} interrupted job(s), "
          f"{report.indexed} transcript(s) indexed")
    for warning in report.warnings[len(plan.warnings):]:
        print(f"warning: {warning}")
    if report.left_behind:
        print(f"{len(report.left_behind)} item(s) were not migrated and stay in "
              f"the old folder (listed in {old / 'MIGRATED.txt'}):")
        for item in report.left_behind:
            print(f"  {item}")
    return 0


def make_stick_progress_printer(clock=None, every: float = 15.0):
    """A make-stick progress callback for the terminal: the phase name when
    it changes, then how far the copy has got at most every ``every``
    seconds, so a slow stick never looks hung."""
    import time

    from .yt.metadata import human_bytes

    now = clock or time.monotonic
    state = {"phase": "", "last": 0.0}

    def show(update) -> None:
        if update.phase != state["phase"]:
            state["phase"] = update.phase
            state["last"] = now()
            if update.label:
                print(f"  {update.label}", flush=True)
            return
        if now() - state["last"] < every:
            return
        if update.phase in {"checking", "verifying", "switching"}:
            # Hashing every file of an old stick, or the copy, takes a while.
            if update.total_files:
                state["last"] = now()
                print(f"    {update.files} of {update.total_files} files", flush=True)
            return
        if update.phase not in {"copying", "playlists", "components"} or not update.total_bytes:
            return
        state["last"] = now()
        percent = int(100 * update.bytes / update.total_bytes)
        print(
            f"    {percent}% — {human_bytes(update.bytes)} of "
            f"{human_bytes(update.total_bytes)}, "
            f"{update.files} of {update.total_files} files",
            flush=True,
        )

    return show


def cmd_make_stick(args) -> int:
    from .libraries import LibrarySet
    from .makestick import (
        MakeStickError,
        drive_id,
        drive_problem,
        inspect_target,
        plan_make,
        run_make,
        upgrade_summary,
    )
    from .makestick import source_image as find_image
    from .web.library import Library
    from .yt.metadata import human_bytes
    from .yt.probe import JobCancelled

    target = Path(args.target).expanduser().absolute()
    # The launch library: --library, else the stick YTOP runs from.
    root = (
        Path(args.library).expanduser() if getattr(args, "library", None)
        else Path(os.environ["YTOP_HOME"]) if os.environ.get("YTOP_HOME")
        else _launch_root(args)
    )
    library = Library(LibrarySet(root, detect=False))
    platforms = None if args.platforms == "all" else [
        key.strip() for key in args.platforms.split(",") if key.strip()
    ]
    playlists = [
        key.strip() for key in (args.playlists or "").split(",") if key.strip()
    ]
    before = drive_id(target)
    try:
        image = find_image(args.image)
        info = inspect_target(target, source=image)
        for note in info.warnings:
            print(f"note: {note}")
        plan = plan_make(
            target, image, library, platforms=platforms, playlists=playlists,
            mode="upgrade" if args.upgrade else "new", label=args.label or "",
        )
    except MakeStickError as exc:
        print(f"make-stick: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"make-stick: {drive_problem(target, exc, before)}", file=sys.stderr)
        return 1
    verb = "Upgrading" if plan.mode == "upgrade" else "Making"
    print(f"{verb} the stick “{plan.label}” at {target}")
    print(f"  platforms: {', '.join(plan.platforms)}")
    if plan.playlists:
        print(f"  playlists: {', '.join(plan.playlists)}")
    print(f"  needs {human_bytes(plan.bytes_needed)}")
    if plan.diff is not None:
        print(upgrade_summary(plan))
    try:
        report = run_make(plan, image, library, make_stick_progress_printer())
    except OSError as exc:
        print(f"make-stick failed: {drive_problem(target, exc, before)}", file=sys.stderr)
        return 1
    except (MakeStickError, JobCancelled) as exc:
        print(f"make-stick failed: {exc}", file=sys.stderr)
        return 1
    for note in report.repaired:
        print(f"note: {note}")
    for note in report.warnings:
        print(f"note: {note}")
    done = "Upgraded" if report.mode == "upgrade" else "Made"
    print(f"{done} the stick “{report.label}” (build {report.build_id}): "
          f"{report.files_copied} files, {human_bytes(report.bytes_copied)}"
          + (f", {len(report.playlists)} playlist(s)" if report.playlists else ""))
    return 0


def _appupdate():
    """The ``ytop.appupdate`` module.  makestick must start the import chain
    (appupdate -> makestick -> web -> web.app -> appupdate); a stick's boot.py
    has imported neither."""
    import importlib

    importlib.import_module(".makestick", __package__)
    return importlib.import_module(".appupdate", __package__)


def _update_target(args):
    """``(<stick>/.ytop, config)`` of the stick YTOP runs from, or None."""
    stick_image = _appupdate().stick_image

    ytop = stick_image()
    if ytop is None:
        print("This is not a YTOP stick (run it through the stick's boot.py, "
              "or set YTOP_HOME).", file=sys.stderr)
        return None
    return ytop, load_config(ytop.parent)


def cmd_components(args) -> int:
    """``ytop components update [NAME ...]``: update the stick's components
    now (no server needed), as "Update now" does."""
    from . import updater

    found = _update_target(args)
    if found is None:
        return 2
    ytop, cfg = found
    result = updater.update(ytop, cfg, args.names or None,
                            progress=lambda label: print(label, flush=True))
    for name, (old, new) in sorted(result.updated.items()):
        print(f"updated {name}: {old or 'none'} -> {new}")
    for name, why in sorted(result.failed.items()):
        print(f"failed {name}: {why}", file=sys.stderr)
    print(f"components: {result.status}" + (f" ({result.message})" if result.message else ""))
    return 0 if result.status in {"updated", "up_to_date"} and not result.failed else 1


def cmd_app_update(args) -> int:
    """``ytop app-update check|install --yes``: the Storage page's app
    update without the page.  Installing needs ``--yes`` (the consent the
    page asks for); it runs the same planned, checksummed, journalled
    install, with nothing running to stop."""
    appupdate = _appupdate()

    if args.action == "install" and not args.yes:
        print("app-update install changes the app on this stick; pass --yes to "
              "confirm (nothing was changed).", file=sys.stderr)
        return 2
    found = _update_target(args)
    if found is None:
        return 2
    ytop, cfg = found
    check = appupdate.check_app_result(ytop, cfg)
    release = check.release
    if release is None:
        print(f"app update: {check.status}" + (f" ({check.message})" if check.message else ""))
        return 0 if check.status == "up_to_date" else 1
    print(f"available: {release.version} (build {release.build_id})")
    if release.notes:
        print(release.notes)
    if args.action == "check":
        return 0
    result = appupdate.install_app(
        ytop, cfg, release, before_switch=lambda: None,
        progress=lambda label: print(label, flush=True),
    )
    print(f"app update: {result.status}: {result.message}")
    return 0 if result.status == "updated" else 1


def _format_bytes(size: float) -> str:
    size = float(size or 0)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{size:.0f} B"
        size /= 1024
    return f"{size:.1f} GiB"


def main(argv: list[str] | None = None) -> int:
    from ytop.stick.boot import SUDO_REFUSED, under_sudo

    if under_sudo():
        print(f"ytop: {SUDO_REFUSED}", file=sys.stderr)
        return 2
    ap = argparse.ArgumentParser(prog="ytop", description="Offline YouTube playlist preserver")
    ap.add_argument("--library", default=None,
                    help="launch library (default: $YTOP_LIBRARY or ~/Videos/YTOP)")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create a new library at PATH")
    p.add_argument("path", help="folder to turn into a library")
    p.add_argument("--label", default=None, help="library name (default: folder name)")
    p.add_argument("--description", default="", help="library description")

    p = sub.add_parser("migrate", help="convert an old ytop library into a portable one")
    p.add_argument("old", help="the old library folder (with playlists/)")
    p.add_argument("target", help="the new library folder (created if needed)")
    p.add_argument("--dry-run", action="store_true",
                   help="print the plan and change nothing")

    p = sub.add_parser("doctor", help="check the download and web toolchain")
    _common(p)
    p.add_argument("--json", action="store_true", help="machine-readable output")

    p = sub.add_parser("probe", help="enumerate a playlist and show the selected formats")
    _common(p)
    p.add_argument("playlist", help="playlist URL, list id, or watch URL with list=")
    p.add_argument("--limit", type=int, default=None, help="probe only the first N videos")
    p.add_argument("--max-height", type=int, default=None,
                   help="optional hard height cap (0 = no cap; default: config)")
    p.add_argument("--target-mb-per-hour", type=float, default=None,
                   help="efficient size budget (0 = native maximum)")
    p.add_argument("--native", action="store_true",
                   help="download the native maximum instead of the efficient budget")
    p.add_argument("--probe-jobs", type=int, default=None,
                   help="parallel metadata probes (default: config)")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    p = sub.add_parser("fetch", help="download/sync a playlist into the library")
    _common(p)
    p.add_argument("playlist", help="playlist URL, list id, or watch URL with list=")
    p.add_argument("--limit", type=int, default=None, help="fetch only the first N videos")
    p.add_argument("--jobs", type=int, default=None, help="parallel downloads (default: config)")
    p.add_argument("--force", action="store_true", help="re-download media that already exists")
    p.add_argument("--dry-run", action="store_true", help="probe and estimate only")
    p.add_argument("--max-height", type=int, default=None,
                   help="optional hard height cap (0 = no cap; default: config)")
    p.add_argument("--target-mb-per-hour", type=float, default=None,
                   help="efficient size budget (0 = native maximum)")
    p.add_argument("--native", action="store_true",
                   help="download the native maximum instead of the efficient budget")
    p.add_argument("--preserve-audio", action="store_true",
                   help="keep the best audio rendition (music playlists)")
    p.add_argument("--to", default=None, metavar="LIBRARY_ID_OR_PATH",
                   help="destination library: an id or the path of an existing library "
                        "(default: the launch library)")

    p = sub.add_parser(
        "transcripts",
        aliases=["subtitles"],
        help="fetch captions and build the transcript search index",
    )
    _common(p)
    p.add_argument("target", nargs="?", default=None,
                   help="playlist URL/id, watch URL, or video id")
    p.add_argument("--force", action="store_true",
                   help="re-fetch transcripts that already exist")
    p.add_argument("--videos", default=None,
                   help="comma-separated video ids inside the playlist")
    p.add_argument("--limit", type=int, default=None,
                   help="fetch only the first N selected videos")
    p.add_argument("--languages", default=None,
                   help="comma-separated caption languages (default: config)")
    p.add_argument("--delay", type=float, default=None,
                   help="seconds between videos (default: config)")
    p.add_argument("--index-only", action="store_true",
                   help="rebuild the FTS index from existing transcript.json files")
    p.add_argument("--no-index", action="store_true",
                   help="fetch transcripts without updating the search index")
    p.add_argument("--json", action="store_true", help="machine-readable summary")

    p = sub.add_parser("index", help="rebuild the transcript search and library indexes")
    _common(p)
    p.add_argument("--json", action="store_true", help="machine-readable summary")

    p = sub.add_parser("serve", help="run the local offline web UI")
    _common(p)
    p.add_argument("--host", default=None, help="bind address (default: config)")
    p.add_argument("--port", type=int, default=None, help="port (default: config)")
    p.add_argument("--open", dest="open_browser", action="store_true",
                   default=None, help="open the browser after starting")
    p.add_argument("--no-open", dest="open_browser", action="store_false",
                   default=None, help="do not open a browser")
    p.add_argument("--portable", action="store_true",
                   help="stick mode: serve the library at $YTOP_HOME on "
                        "127.0.0.1, picking a free port")

    p = sub.add_parser("make-stick", help="make (or upgrade) a YTOP stick at TARGET")
    p.add_argument("target", help="the drive or folder to put YTOP on (never formatted)")
    p.add_argument("--platforms", default="all",
                   help="all, or comma-separated platform keys (default: all "
                        "that the source has)")
    p.add_argument("--playlists", default=None,
                   help="comma-separated playlist keys to copy onto a new stick")
    p.add_argument("--upgrade", action="store_true",
                   help="replace the app on an existing stick; its library is untouched")
    p.add_argument("--label", default=None, help="name of the new stick's library")
    p.add_argument("--image", default=None,
                   help="source image (default: the stick YTOP runs from; for "
                        "development: dist/stick/.ytop)")

    p = sub.add_parser("components", help="update the stick's YouTube-facing components")
    csub = p.add_subparsers(dest="components_command", required=True)
    cu = csub.add_parser("update", help="download, check and switch in newer components")
    cu.add_argument("names", nargs="*",
                    help="components to update (default: all of them)")

    p = sub.add_parser("app-update", help="check for, or install, a newer YTOP release")
    p.add_argument("action", choices=["check", "install"])
    p.add_argument("--yes", action="store_true",
                   help="confirm the install (required for 'install')")

    a = ap.parse_args(argv)
    if a.command == "init":
        return cmd_init(a)
    if a.command == "migrate":
        return cmd_migrate(a)
    if a.command == "doctor":
        from .doctor import print_report, run_checks
        return print_report(run_checks(_launch_root(a)), a.json)
    if a.command == "probe":
        return cmd_probe(a)
    if a.command == "fetch":
        return cmd_fetch(a)
    if a.command in {"transcripts", "subtitles"}:
        return cmd_transcripts(a)
    if a.command == "index":
        return cmd_index(a)
    if a.command == "serve":
        return cmd_serve(a)
    if a.command == "make-stick":
        return cmd_make_stick(a)
    if a.command == "components":
        return cmd_components(a)
    if a.command == "app-update":
        return cmd_app_update(a)
    ap.error(f"unknown command {a.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
