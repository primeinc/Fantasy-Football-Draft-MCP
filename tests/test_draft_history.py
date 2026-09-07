"""draft_history lists every pick whole; draft_replay scores and windows."""
from __future__ import annotations

import json

from ffdraft import board, config, server


def _full_state(monkeypatch, n=224):
    league = config.LeagueSettings(teams=16, draft_slot=4, rounds=14)
    state = board.DraftState(league, "history-probe")
    state.reset()
    for i in range(1, n + 1):
        state.record(f"Player Number {i}", i, position="WR" if i % 2 else "RB")
    monkeypatch.setattr(server, "_state", lambda: state)
    return state


def test_a_full_draft_fits_the_cap_in_order(monkeypatch):
    _full_state(monkeypatch)
    raw = server.draft_history()
    assert len(raw) < server.PAYLOAD_LIMIT
    out = json.loads(raw)
    assert out["count"] == 224 and "truncated" not in out
    assert out["columns"] == ["pick", "round", "slot", "position", "player"]
    assert out["rows"][0] == [1, 1, 1, "WR", "Player Number 1"]
    assert out["rows"][16] == [17, 2, 16, "WR", "Player Number 17"]
    assert out["rows"][-1][0] == 224 and out["rows"][-1][1] == 14


def test_slot_and_last_filters(monkeypatch):
    _full_state(monkeypatch, 40)
    mine = json.loads(server.draft_history(slot=4))
    assert [r[0] for r in mine["rows"]] == [4, 29, 36]
    recent = json.loads(server.draft_history(last=3))
    assert [r[0] for r in recent["rows"]] == [38, 39, 40]


def test_a_watch_names_the_teams(monkeypatch):
    state = _full_state(monkeypatch, 3)

    class W:
        slot_of = {7: 1, 3: 2, 9: 3}

        def __init__(self, state):
            self.state = state

        def team_label(self, team_id):
            return f"team {team_id} label"
    w = W(state)
    monkeypatch.setitem(server._WATCHES, "L", (w, None))
    out = json.loads(server.draft_history(league_id="L"))
    assert out["teams"] == {"1": "team 7 label", "2": "team 3 label", "3": "team 9 label"}
    assert out["rows"][1][2] == 2
