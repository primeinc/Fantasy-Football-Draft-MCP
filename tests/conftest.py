"""Keep the suite out of the user's real `~/.ffdraft` directories.

This exists because it failed. `reload_package` now writes a resume record for
any live watch that lacks one, and `test_reload.py` puts a watch into `_WATCHES`
and calls `reload_package` -- so running the suite wrote `probe-league.json`
into the real store, beside the record of the draft actually running. Two more
arrived from a test of my own. All three were removed by hand.

Monkeypatching a module global is not enough on its own, and that is the whole
lesson: `reload_package` re-imports `config` and `watchstore`, which re-read
these paths from the environment, so any patch applied to the module object is
undone the moment a test reloads the package. The environment is what survives a
reload, so the environment is what has to be set -- and the module globals are
set too, for the code that read them at import and never looks again.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_watch_store(tmp_path, monkeypatch):
    """Point the watch store at this test's own tmp_path.

    Autouse and unconditional: a test that writes to the real store does not
    fail, it succeeds quietly and leaves something behind for a live server to
    read at its next start.

    Scoped to `WATCH_DIR`; `STATE_DIR` and `DATA_DIR` are isolated by the
    session fixture below, which became possible once the payload tests owned
    their own draft instead of reading the developer's saved one.
    """
    from ffdraft import config, watchstore

    store = tmp_path / "watch_dir"
    store.mkdir(parents=True, exist_ok=True)
    # The environment first: it is what a reload re-reads. Patching only the
    # module attribute is undone the moment a test calls `reload_package`, which
    # is exactly the path that caused the pollution.
    monkeypatch.setenv("FFDRAFT_WATCH", str(store))
    monkeypatch.setattr(config, "WATCH_DIR", store, raising=False)
    monkeypatch.setattr(watchstore, "WATCH_DIR", store, raising=False)


@pytest.fixture(autouse=True, scope="session")
def _isolated_state_and_data(tmp_path_factory):
    """Point the draft state and derived-board directories at a session tmp dir.

    `STATE_DIR` holds the live draft's picks and the league store; `DATA_DIR`
    holds the board parquet a league builds. `DraftState.save()` writes on every
    `record()`, so a suite run without this would write beside the draft that is
    actually running, and fifteen parallel runs would race it. `CACHE_DIR` is
    left alone on purpose: it is the nflverse download cache, read-only in
    practice, and isolating it would turn every board build into a network
    fetch.

    Session-scoped because the module globals are read once at import by
    `board`, `server` and `watch`; the environment is set as well so a
    `reload_package` inside a test re-reads the same paths.
    """
    import os

    from ffdraft import board, config, server, watch

    root = tmp_path_factory.mktemp("ffdraft_state")
    state = root / "state"
    data = root / "data"
    state.mkdir()
    data.mkdir()
    saved = {k: os.environ.get(k) for k in ("FFDRAFT_STATE", "FFDRAFT_DATA")}
    os.environ["FFDRAFT_STATE"] = str(state)
    os.environ["FFDRAFT_DATA"] = str(data)
    patched = [
        (config, "STATE_DIR", state), (config, "DATA_DIR", data),
        (config, "LEAGUES_PATH", state / "leagues.json"),
        (board, "STATE_DIR", state), (server, "STATE_DIR", state),
        (server, "DATA_DIR", data), (watch, "STATE_DIR", state),
    ]
    before = [(m, n, getattr(m, n)) for m, n, _ in patched]
    for m, n, v in patched:
        setattr(m, n, v)
    yield
    for m, n, v in before:
        setattr(m, n, v)
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
