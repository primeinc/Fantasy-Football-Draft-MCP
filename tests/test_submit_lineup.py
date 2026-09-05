"""The submit_lineup tool: a dry run sends nothing, a refusal sends nothing,
and a send is followed by a read-back that reports what ESPN holds."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from ffdraft import config, lineup, lineup_write, rosters, server


def _priced():
    return pd.DataFrame([
        {"name": "QB One", "espn_id": "1", "position": "QB", "lineup_slot": 0,
         "eligible_slots": [0, 20], "lineup_locked": False, "week_points": 20.0},
        {"name": "RB One", "espn_id": "2", "position": "RB", "lineup_slot": 20,
         "eligible_slots": [2, 23, 20], "lineup_locked": False, "week_points": 15.0},
        {"name": "RB Two", "espn_id": "3", "position": "RB", "lineup_slot": 2,
         "eligible_slots": [2, 23, 20], "lineup_locked": False, "week_points": 9.0},
    ])


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setenv("ESPN_SWID", "SWID-TEST")
    monkeypatch.setenv("ESPN_S2", "S2-TEST")
    priced = _priced()
    monkeypatch.setattr(server, "_lineup_inputs",
                        lambda league_id, week, season: (config.LeagueSettings(), priced, 3, 7))
    starters = pd.DataFrame([{"name": "QB One", lineup.SLOT_COLUMN: "QB"},
                             {"name": "RB One", lineup.SLOT_COLUMN: "RB"}])
    monkeypatch.setattr(lineup, "starting_lineup",
                        lambda rows, league, value: (starters, rows.iloc[0:0]))
    sent: list = []

    def fake_send(league_id, season, payload, swid=None, espn_s2=None, post=None):
        sent.append(payload)
        return {"status": 200, "body": {"ok": True}}
    monkeypatch.setattr(lineup_write, "send", fake_send)
    after = priced.copy()
    after.loc[after["name"] == "RB One", "lineup_slot"] = 2
    after.loc[after["name"] == "RB Two", "lineup_slot"] = 20
    monkeypatch.setattr(rosters, "fetch_roster_teams", lambda *a, **k: [{"id": 7}])
    monkeypatch.setattr(rosters, "rosters_by_team", lambda teams, board, positions: {7: after})
    monkeypatch.setattr(server, "_build_board", lambda: pd.DataFrame())
    return sent


def test_dry_run_returns_the_moves_and_sends_nothing(wired):
    out = json.loads(server.submit_lineup("L", 3))
    assert out["sent"] is False and "dry run" in out["why_not_sent"]
    assert out["moves"] == [{"player": "RB One", "from": "BENCH", "to": "RB"},
                            {"player": "RB Two", "from": "RB", "to": "BENCH"}]
    assert out["transaction"]["memberId"] == "<SWID>"
    assert out["transaction"]["type"] == "ROSTER"
    assert wired == []


def test_a_refusal_sends_nothing(wired, monkeypatch):
    locked = _priced()
    locked.loc[locked["name"] == "RB One", "lineup_locked"] = True
    monkeypatch.setattr(server, "_lineup_inputs",
                        lambda league_id, week, season: (config.LeagueSettings(), locked, 3, 7))
    out = json.loads(server.submit_lineup("L", 3, dry_run=False))
    assert out["sent"] is False and out["refusals"]
    assert wired == []


def test_a_send_is_followed_by_a_read_back(wired):
    out = json.loads(server.submit_lineup("L", 3, dry_run=False))
    assert out["sent"] is True
    assert len(wired) == 1
    assert wired[0]["memberId"] == "{SWID-TEST}"
    assert [i["playerId"] for i in wired[0]["items"]] == [2, 3]
    assert out["espn_holds"] == {"QB One": "QB", "RB One": "RB", "RB Two": "BENCH"}
    assert out["mismatches"] == []
    assert "SWID-TEST" not in json.dumps(out)
