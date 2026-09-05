"""The suite never touches the real ~/.ffdraft state, data or watch directories.

Written after the suite did: reload_package wrote three resume records beside
the live draft's, and DraftState.save() writes on every record(). A test that
reads the developer's real saved draft passes on one machine and has never
worked on a fresh clone.
"""
from __future__ import annotations

import os
from pathlib import Path

from ffdraft import board, config, server, watch, watchstore

REAL = Path.home() / ".ffdraft"


def _outside_real(p: Path) -> bool:
    try:
        p.resolve().relative_to(REAL.resolve())
    except ValueError:
        return True
    return False


def test_every_state_path_points_away_from_the_real_directories():
    for mod, name in ((config, "STATE_DIR"), (config, "DATA_DIR"), (config, "WATCH_DIR"),
                      (config, "LEAGUES_PATH"), (board, "STATE_DIR"), (server, "STATE_DIR"),
                      (server, "DATA_DIR"), (watch, "STATE_DIR"), (watchstore, "WATCH_DIR")):
        assert _outside_real(getattr(mod, name)), f"{mod.__name__}.{name} is under {REAL}"


def test_the_environment_agrees_so_a_reload_cannot_undo_it():
    for key in ("FFDRAFT_STATE", "FFDRAFT_DATA", "FFDRAFT_WATCH"):
        assert key in os.environ and _outside_real(Path(os.environ[key])), key


def test_a_draft_state_written_now_lands_in_the_isolated_directory(tmp_path):
    st = board.DraftState(config.LeagueSettings(), "isolation-probe")
    st.save()
    assert _outside_real(st.path)
    assert st.path.exists()
