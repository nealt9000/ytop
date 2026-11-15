"""Keep every test away from the host's real libraries and registry."""

from __future__ import annotations

import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolated_for_wide_fixtures(tmp_path_factory):
    """The same isolation for the whole session, set up before any module- or
    session-scoped fixture runs (the per-test fixtures below start too late
    for those: a module fixture that made sticks once filled the real
    registry with test folders)."""
    import ytop.libraries as libraries

    with pytest.MonkeyPatch.context() as patch:
        registry = tmp_path_factory.mktemp("session-registry") / "libraries.json"
        patch.setattr(libraries, "registry_path_default", lambda: registry)
        patch.setattr(libraries, "_mount_parents", lambda: [])
        patch.setattr(libraries, "_well_known_roots", lambda: [])
        patch.setenv("YTOP_INSTANCE_DIR", str(tmp_path_factory.mktemp("session-instance")))
        yield


@pytest.fixture(autouse=True)
def _isolated_library_discovery(tmp_path_factory, monkeypatch):
    import ytop.libraries as libraries

    registry = tmp_path_factory.mktemp("registry") / "libraries.json"
    monkeypatch.setattr(libraries, "registry_path_default", lambda: registry)
    monkeypatch.setattr(libraries, "_mount_parents", lambda: [])
    monkeypatch.setattr(libraries, "_well_known_roots", lambda: [])


@pytest.fixture(autouse=True)
def _isolated_instance_dir(tmp_path_factory, monkeypatch):
    """Never read or write the real per-user instance file of a running YTOP."""
    monkeypatch.setenv(
        "YTOP_INSTANCE_DIR", str(tmp_path_factory.mktemp("instance"))
    )


@pytest.fixture(autouse=True)
def _isolated_deno_dir(monkeypatch):
    """base_options / portable serve set DENO_DIR process-wide: undo it."""
    monkeypatch.setenv("DENO_DIR", "unset-by-test")
    monkeypatch.delenv("DENO_DIR")
