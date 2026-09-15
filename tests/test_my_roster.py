"""`server._my_roster`: the draft record is a roster only before the draft ends.

It fell back to the draft record whenever mRoster failed, so in week 9 a
network error answered with the week-1 roster under a basis nobody reads.
"""
from types import SimpleNamespace

import pandas as pd
import pytest

from ffdraft import board, rosters, server

LIVE = pd.DataFrame([{"name": "Live Man", "position": "RB"}])
RECORD = pd.DataFrame([{"name": "Drafted Man", "position": "RB"}])
STATE = SimpleNamespace(my_rows=lambda _board: RECORD)


@pytest.fixture
def espn(monkeypatch):
    fake = SimpleNamespace(teams=[{"id": 3}], mine=LIVE, drafted=True, fail_roster=False,
                           fail_context=False)

    def teams(_league_id, _season, _week):
        if fake.fail_roster:
            raise RuntimeError("mRoster 503")
        return fake.teams

    def context(_league_id, _season):
        if fake.fail_context:
            raise RuntimeError("mSettings 503")
        return {"drafted": fake.drafted}

    monkeypatch.setattr(rosters, "fetch_roster_teams", teams)
    monkeypatch.setattr(rosters, "my_team_id", lambda _teams: 3)
    monkeypatch.setattr(rosters, "rosters_by_team", lambda _t, _b, _p: {3: fake.mine})
    monkeypatch.setattr(board, "espn_league_context", context)
    return fake


def call():
    return server._my_roster("1", 2026, 2, pd.DataFrame(), STATE)


@pytest.mark.usefixtures("espn")
def test_a_read_roster_is_the_live_roster():
    mine, basis = call()
    assert basis == server.ROSTER_LIVE and mine is LIVE


def test_a_failed_read_after_the_draft_raises(espn):
    espn.fail_roster = True
    with pytest.raises(RuntimeError, match="mRoster 503.*the draft is complete"):
        call()


def test_an_empty_roster_after_the_draft_raises(espn):
    espn.mine = LIVE.iloc[0:0]
    with pytest.raises(RuntimeError, match="no roster entries.*the draft is complete"):
        call()


def test_before_the_draft_completes_the_record_is_the_roster(espn):
    espn.fail_roster, espn.drafted = True, False
    mine, basis = call()
    assert basis == server.ROSTER_DRAFT and mine is RECORD


def test_an_unreadable_draft_state_raises(espn):
    espn.fail_roster, espn.fail_context = True, True
    with pytest.raises(RuntimeError, match="mRoster 503.*mSettings 503"):
        call()
