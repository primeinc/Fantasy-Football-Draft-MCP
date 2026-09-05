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

    Scoped to `WATCH_DIR` deliberately. Isolating `STATE_DIR` as well is the
    right end state and is NOT done here, because it turns two passing tests red:
    `test_json_payloads` reads the developer's real saved draft, so on a fresh
    clone it has never worked. That is a real defect and a separate one; fixing
    it here would mean editing a file another task is already changing, and would
    hide the finding inside an unrelated commit.
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
