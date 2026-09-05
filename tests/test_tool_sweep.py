"""Three tools found lying or swallowing by running every tool once live.

model_settings() with no arguments reported "will rebuild on next query" and
deleted the board: a read invalidated the cache. draft_backtest raised on a
season the league never drafted and the SDK reduced it to "Error executing
tool". on_the_clock called sync_draft under a running watch, which the server's
own instructions forbid, and the read API is blind mid-draft.
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from ffdraft import adp as adp_mod
from ffdraft import server


def test_model_settings_without_arguments_keeps_the_board(monkeypatch):
    league, _weights = server._settings()
    sentinel = pd.DataFrame({"marker": [1]})
    server._BOARDS[league.cache_key()] = sentinel
    saved: list = []
    monkeypatch.setattr(server, "save_settings", lambda *a: saved.append(a))
    out = json.loads(server.model_settings())
    assert out["board"] == "unchanged" and out["changed"] == {}
    assert server._BOARDS[league.cache_key()] is sentinel
    assert saved == []
    server._BOARDS.pop(league.cache_key(), None)


def test_model_settings_with_a_new_value_invalidates_and_says_which(monkeypatch):
    league, weights = server._settings()
    server._BOARDS[league.cache_key()] = pd.DataFrame({"marker": [1]})
    monkeypatch.setattr(server, "save_settings", lambda *_a: None)
    before = weights.bye
    try:
        out = json.loads(server.model_settings(bye_weight=before + 0.25))
        assert out["board"] == "will rebuild on next query"
        assert out["changed"] == {"bye": before + 0.25}
        assert league.cache_key() not in server._BOARDS
    finally:
        weights.bye = before


def test_draft_backtest_names_the_failure(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("404 Client Error: Not Found")
    monkeypatch.setattr(adp_mod, "draft_backtest", boom)
    out = json.loads(server.draft_backtest("L", 2025))
    assert out["season"] == 2025 and out["league_id"] == "L"
    assert "RuntimeError" in out["error"] and "404" in out["error"]


def test_on_the_clock_skips_sync_under_a_watch(monkeypatch):
    calls: list = []
    monkeypatch.setattr(server, "sync_draft",
                        lambda *a, **_k: calls.append(a) or json.dumps({"picks_made": 0}))
    monkeypatch.setattr(server, "draft_status", lambda *_a, **_k: json.dumps({"error": "stop here"}))
    monkeypatch.setitem(server._WATCHES, "L", object())
    out = json.loads(server.on_the_clock(platform="espn", league_id="L"))
    assert calls == []
    assert "watch is running" in json.dumps(out)


def test_on_the_clock_is_the_highest_overall_plus_one():
    from ffdraft import board, config
    state = board.DraftState(config.LeagueSettings(teams=16, draft_slot=4), "sweep-probe")
    state.record("Ja'Marr Chase")
    state.record("Nobody Realperson", overall=7)
    assert state.on_the_clock == 8
    assert state.summary()["my_next_pick"] == 29  # pick 4 has passed
    state.undo()
    assert state.on_the_clock == 2


def test_mock_draft_scratch_state_is_removed_when_the_mock_raises(monkeypatch):
    import numpy as np

    from ffdraft import adp as adp_mod
    from ffdraft import board as bd
    from ffdraft import config, model

    league = config.LeagueSettings(teams=2, draft_slot=1, rounds=3)
    created: list = []
    real = bd.DraftState

    def spy(*a, **k):
        st = real(*a, **k)
        created.append(st.path)
        return st
    monkeypatch.setattr(bd, "DraftState", spy)

    def boom(*_a, **_k):
        raise RuntimeError("mid-draft")
    monkeypatch.setattr(model, "recommend", boom)
    board = pd.DataFrame([{"name": "A", "_key": "a", "position": "RB", "adp": 1.0,
                           "draft_score": 1.0, "drafted": False, "proj_points": 100.0}])
    with pytest.raises(RuntimeError):
        adp_mod._draft_trial(board, league, np.random.default_rng(0))
    assert created and not created[0].exists()
