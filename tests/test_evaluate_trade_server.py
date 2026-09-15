"""`server.evaluate_trade` at the seam where it chooses its sources.

Every ESPN read is a fake at the module attribute the server calls through, so
each test attacks one provenance branch: a read that fails, a league whose
settings are not the active league's, a window bound outside the league's
season, a counterparty that names nothing. None of them may produce a number
from a substituted fact. Payload shapes follow the fixtures that document the
live ones: mRoster entries in tests/test_rosters.py, kona_player_info entries in
tests/test_pool.py, and `espn_league_rules` for league 1734659820 on 2026-09-15.
"""
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from ffdraft import board, features, live, pool, rosters, server, trade
from ffdraft.config import CURRENT_SEASON, LeagueSettings, ModelWeights, Scoring

SWID = "{AAAA-BBBB}"
LEAGUE_ID = "1734659820"
TITLE = LeagueSettings(name="title", teams=16, rounds=14, draft_slot=4,
                       scoring=Scoring.preset("ppr"),
                       starters={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 0,
                                 "K": 1, "DST": 1})

# name, ESPN position id, position, ESPN id, pro team id, team, adj_ppg, season proj
MINE = [("Mine QB", 1, "QB", 101, 22, "ARI", 18.0, 290.0),
        ("Mine RB", 2, "RB", 102, 1, "ATL", 12.0, 190.0),
        ("Mine RB2", 2, "RB", 103, 2, "BUF", 9.0, 140.0),
        ("Mine WR", 3, "WR", 104, 3, "CHI", 14.0, 220.0),
        ("Mine WR2", 3, "WR", 105, 4, "CIN", 10.0, 160.0),
        ("Mine TE", 4, "TE", 106, 5, "CLE", 8.0, 120.0)]
THEIRS = [("Their QB", 1, "QB", 201, 6, "DAL", 17.0, 270.0),
          ("Their RB", 2, "RB", 202, 7, "DEN", 13.0, 200.0),
          ("Their RB2", 2, "RB", 203, 8, "DET", 10.0, 150.0),
          ("Their WR", 3, "WR", 204, 9, "GB", 13.0, 200.0),
          ("Their WR2", 3, "WR", 205, 10, "TEN", 11.0, 170.0),
          ("Their TE", 4, "TE", 206, 11, "IND", 7.0, 110.0)]
# Free agents: all KC (bye 6) but two, so a two-slot position needs two of them.
FREE = [("FA QB", 1, "QB", 301, 12, "KC", 0.0, 150.0),
        ("FA RB", 2, "RB", 302, 12, "KC", 0.0, 100.0),
        ("FA RB2", 2, "RB", 303, 13, "LV", 0.0, 90.0),
        ("FA WR", 3, "WR", 304, 12, "KC", 0.0, 110.0),
        ("FA WR2", 3, "WR", 305, 13, "LV", 0.0, 95.0),
        ("FA TE", 4, "TE", 306, 12, "KC", 0.0, 80.0)]
BYES = {"ARI": 8, "ATL": 5, "BUF": 7, "CHI": 5, "CIN": 10, "CLE": 9, "DAL": 10, "DEN": 12,
        "DET": 8, "GB": 5, "TEN": 10, "IND": 14, "KC": 6, "LV": 13}
MY_TEAM, THEIR_TEAM = 3, 7
# Keys that carry a quantity. A refusal may name the sources it read; it may not
# carry any of these.
NUMBERS = ("you", "counterparty", "weeks", "waiver", "stand_ins", "known_out")


def _board(extra: list[dict] | None = None) -> pd.DataFrame:
    rows = [{"name": n, "position": pos, "espn_id": str(pid), "team": team,
             "bye_week": BYES[team], "adj_ppg": ppg, "exp_games": 16.0,
             "proj_points": ppg * 16.0, "replacement_points": 100.0, "vor": 0.0,
             "draft_score": 0.0, "adp": float(pid), "off_roster": False,
             "is_rookie": False}
            for n, _, pos, pid, _, team, ppg, _ in MINE + THEIRS]
    b = pd.DataFrame(rows + (extra or []))
    b["_key"] = b["name"].map(board.norm_name)
    return b


def _roster_entry(pid: int, name: str, pos_id: int, pro_team: int) -> dict:
    return {"playerId": pid, "lineupSlotId": 20,
            "playerPoolEntry": {"player": {"id": pid, "fullName": name,
                                           "defaultPositionId": pos_id,
                                           "proTeamId": pro_team}}}


def _pool_entry(pid: int, name: str, pos_id: int, pro_team: int, proj: float,
                on_team: int) -> dict:
    return {"id": pid, "status": "ONTEAM" if on_team else "FREEAGENT", "onTeamId": on_team,
            "waiverProcessDate": 0,
            "player": {"id": pid, "fullName": name, "defaultPositionId": pos_id,
                       "proTeamId": pro_team, "injuryStatus": "ACTIVE",
                       "ownership": {"percentOwned": 50.0, "percentChange": 0.0},
                       "stats": [{"seasonId": CURRENT_SEASON, "scoringPeriodId": 0,
                                  "statSourceId": 1, "statSplitTypeId": 0,
                                  "appliedTotal": proj}]}}


class Response:
    def __init__(self, body: dict):
        self.body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.body


class Espn:
    """One league's worth of reads. `fail` names the read that raises."""

    def __init__(self):
        self.league = TITLE
        self.board = _board()
        self.rules = {
            "league": "TITLE LEAGUE ", "teams": 16,
            "roster": {"starters": {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "DST": 1, "K": 1}},
            "scoring": {"items": {"interceptions": -2.0, "fumbles_lost": -2.0,
                                  "passing_yards": 0.04, "rushing_yards": 0.1,
                                  "receiving_yards": 0.1, "receptions": 1.0,
                                  "passing_2pt": 2.0, "rushing_2pt": 2.0,
                                  "receiving_2pt": 2.0, "passing_tds": 4.0,
                                  "rushing_tds": 6.0, "receiving_tds": 6.0},
                        "slot_overrides": {}},
            "schedule": {"regular_season_weeks": 14, "playoff_weeks": [15, 16, 17]},
        }
        self.status: dict = {"scoringPeriodId": 2}
        self.byes = dict(BYES)
        self.mine = [_roster_entry(pid, n, p, pro) for n, p, _, pid, pro, _, _, _ in MINE]
        self.theirs = [_roster_entry(pid, n, p, pro) for n, p, _, pid, pro, _, _, _ in THEIRS]
        self.pool = ([_pool_entry(pid, n, p, pro, proj, MY_TEAM)
                      for n, p, _, pid, pro, _, _, proj in MINE]
                     + [_pool_entry(pid, n, p, pro, proj, THEIR_TEAM)
                        for n, p, _, pid, pro, _, _, proj in THEIRS]
                     + [_pool_entry(pid, n, p, pro, proj, 0)
                        for n, p, _, pid, pro, _, _, proj in FREE])
        self.picks = [{"teamId": MY_TEAM, "overallPickNumber": 4, "roundPickNumber": 4,
                       "playerId": 101},
                      {"teamId": THEIR_TEAM, "overallPickNumber": 7, "roundPickNumber": 7,
                       "playerId": 201},
                      {"teamId": THEIR_TEAM, "overallPickNumber": 26, "roundPickNumber": 10,
                       "playerId": 202}]
        self.fail = ""
        self.calls: list[str] = []

    def read(self, name: str, value):
        self.calls.append(name)
        if self.fail == name:
            raise RuntimeError(f"{name} unreachable")
        return value

    def payload(self) -> dict:
        return {"teams": [{"id": MY_TEAM, "name": "adverse possession", "owners": [SWID],
                           "roster": {"entries": self.mine}},
                          {"id": THEIR_TEAM, "name": "Post Closing King",
                           "owners": ["{CCCC-DDDD}"], "roster": {"entries": self.theirs}}],
                "members": []}


@pytest.fixture
def espn(monkeypatch) -> Espn:
    fake = Espn()
    monkeypatch.setenv("ESPN_SWID", SWID)
    monkeypatch.setattr(server, "_settings", lambda: (fake.league, ModelWeights()))
    monkeypatch.setattr(server, "_state",
                        lambda: SimpleNamespace(picks=[], my_slot=4, league=fake.league))
    monkeypatch.setattr(server, "_build_board", lambda: fake.board)
    monkeypatch.setattr(board, "espn_league_rules",
                        lambda _league_id, _season: fake.read("rules", fake.rules))
    monkeypatch.setattr(live, "fetch_league_status",
                        lambda _league_id, _season: fake.read("status", fake.status))
    monkeypatch.setattr(features, "team_bye_weeks",
                        lambda _season: fake.read("byes", fake.byes))
    monkeypatch.setattr(rosters, "fetch_roster_payload",
                        lambda _league_id, _season: fake.read("rosters", fake.payload()))

    def draft_detail(_league_id, _season, params, **_kwargs):
        assert params == {"view": "mDraftDetail"}
        return fake.read("picks", Response({"draftDetail": {"picks": fake.picks}}))

    monkeypatch.setattr(board, "espn_league_get", draft_detail)
    monkeypatch.setattr(pool, "fetch_pool",
                        lambda _league_id, _season: fake.read("pool", fake.pool))
    return fake


def run(give: str = "Mine WR2", get: str = "Their WR2", league_id: str = LEAGUE_ID,
        counterparty_team: str = str(THEIR_TEAM), counterparty_slot: int = 0,
        first_week: int = 0, last_week: int = 0, n_trials: int = 10, blocks: int = 2,
        out: str = "") -> dict:
    return json.loads(server.evaluate_trade(
        give, get, counterparty_slot=counterparty_slot, league_id=league_id,
        n_trials=n_trials, blocks=blocks, first_week=first_week, last_week=last_week,
        out=out, counterparty_team=counterparty_team))


def _refused(body: dict) -> None:
    """No quantity of any kind came back."""
    assert body["ok"] is False
    assert body["errors"]
    for key in NUMBERS:
        assert key not in body


@pytest.mark.usefixtures("espn")
class TestOneLeagueSuppliesEveryFact:
    def test_the_live_evaluation_reads_the_window_rosters_and_free_agents(self):
        body = run(out="Mine QB:2-3")
        assert body["ok"] is True, body.get("errors")
        assert body["weeks"] == {"from": 2, "to": 17}
        assert body["window_basis"] == {"from": "ESPN mStatus scoringPeriodId",
                                        "to": "league settings: last playoff week"}
        assert body["roster_basis"] == server.ROSTER_LIVE
        assert body["league"] == {"league_id": LEAGUE_ID, "name": "TITLE LEAGUE ",
                                  "settings_of": "title", "current_period": 2,
                                  "final_week": 17}
        assert body["you"]["team_id"] == MY_TEAM
        assert body["counterparty"]["team"] == "Post Closing King"
        assert body["known_out"] == {"Mine QB": [2, 3]}
        assert body["waiver"]["QB"]["source"] == trade.WAIVER_FROM_ESPN
        assert body["waiver"]["QB"]["candidates"][0] == {
            "player": "FA QB", "per_game": round(150.0 / 17, 2), "bye_week": 6}
        # Tendencies are this league's draft picks, named through ESPN's pool,
        # not the active league's draft record.
        tendencies = body["counterparty"]["tendencies"]
        assert tendencies["picks"] == 2
        assert tendencies["by_position"] == {"QB": 1, "RB": 1}

    def test_two_holes_at_one_position_get_two_different_free_agents(self):
        body = run()
        names = [c["player"] for c in body["waiver"]["RB"]["candidates"]]
        assert names == ["FA RB", "FA RB2"]

    def test_explicit_bounds_inside_the_season_are_used_and_named(self):
        body = run(first_week=4, last_week=15)
        assert body["weeks"] == {"from": 4, "to": 15}
        assert body["window_basis"] == {"from": "first_week argument",
                                        "to": "last_week argument"}

    def test_a_board_less_player_is_priced_from_espn_and_sits_out_his_bye(self, espn):
        # On no board row, on KC (bye 6): ESPN's 85 over 17 a game, for the 15
        # weeks of 2-17 he has a game.
        espn.mine.append(_roster_entry(107, "Rookie RB", 2, 12))
        espn.pool.append(_pool_entry(107, "Rookie RB", 2, 12, 85.0, MY_TEAM))
        espn.board = _board([{"name": "KC Somebody", "position": "WR", "espn_id": "999",
                              "team": "KC", "bye_week": 6, "adj_ppg": 1.0, "exp_games": 16.0,
                              "proj_points": 16.0, "replacement_points": 100.0, "vor": 0.0,
                              "draft_score": 0.0, "adp": 999.0, "off_roster": False,
                              "is_rookie": False}])
        body = run()
        assert body["ok"] is True, body.get("errors")
        assert body["stand_ins"]["yours"] == [{
            "player": "Rookie RB", "position": "RB", "bye_week": 6,
            "points": round(85.0 / 17 * 15, 1), "basis": trade.BASIS_ESPN_STAND_IN}]


class TestTheActiveLeagueMustBeThisLeague:
    def test_another_league_s_settings_refuse_before_any_roster_is_read(self, espn):
        # Active league A with league_id B mixed A's board and scoring with B's
        # rosters, window and free agents.
        espn.league = LeagueSettings(name="other", teams=12, scoring=Scoring.preset("half_ppr"))
        body = run()
        _refused(body)
        assert "does not have the settings of the active league 'other'" in body["errors"][0]
        assert "teams: ESPN 16, 'other' 12" in body["errors"][0]
        assert "scoring rec (receptions): ESPN 1.0, 'other' 0.5" in body["errors"][0]
        assert "FLEX starters: ESPN 0, 'other' 1" in body["errors"][0]
        assert espn.calls == ["rules"]


class TestAFailedReadIsNoNumber:
    @pytest.mark.parametrize("read", ["rules", "status", "byes", "rosters", "picks", "pool"])
    def test_each_read_failing_refuses_and_names_the_read(self, espn, read):
        # mRoster failing fell back to the draft record, mStatus and settings to
        # weeks 1-14, the pool to board replacement level -- each still scored.
        espn.fail = read
        body = run()
        _refused(body)
        assert body["errors"][0].startswith("not scored, a league read failed: ")
        assert f"{read} unreachable" in body["errors"][0]
        assert "roster_basis" not in body

    def test_a_status_with_no_scoring_period_refuses(self, espn):
        espn.status = {}
        body = run()
        _refused(body)
        assert "scoringPeriodId is None" in body["errors"][0]

    def test_a_schedule_with_no_weeks_refuses(self, espn):
        espn.rules["schedule"] = {"regular_season_weeks": 0, "playoff_weeks": []}
        body = run()
        _refused(body)
        assert "no playoff or regular-season weeks" in body["errors"][0]

    def test_no_bye_weeks_refuses(self, espn):
        espn.byes = {}
        _refused(run())

    def test_a_roster_read_with_no_entries_for_your_team_refuses(self, espn):
        espn.mine = []
        body = run()
        _refused(body)
        assert "no roster entries for your team" in body["errors"][0]

    def test_a_pool_with_no_free_agent_at_a_position_refuses(self, espn):
        espn.pool = [e for e in espn.pool if e["player"]["fullName"] != "FA TE"]
        body = run()
        _refused(body)
        assert any("no free agent at TE" in e for e in body["errors"])


@pytest.mark.usefixtures("espn")
class TestTheWindowIsTheLeague:
    def test_week_99_is_refused(self):
        body = run(last_week=99)
        _refused(body)
        assert body["errors"] == ["last_week 99 is past the league's final scoring week 17"]

    def test_a_played_week_is_refused(self):
        body = run(first_week=1)
        _refused(body)
        assert body["errors"] == ["first_week 1 is before the current scoring period 2: a "
                                  "trade cannot change a week already played"]


class TestInputsThatDefineNoTrade:
    @pytest.mark.usefixtures("espn")
    @pytest.mark.parametrize("trials, blocks", [(-1, 0), (0, -1), (6000, 0), (0, 21)])
    def test_harness_counts_outside_their_bounds_are_refused(self, trials, blocks):
        body = run(n_trials=trials, blocks=blocks)
        _refused(body)
        assert any("is outside" in e for e in body["errors"])

    def test_a_defense_in_the_trade_is_refused(self, espn):
        espn.mine.append(_roster_entry(-16033, "Ravens D/ST", 16, 33))
        espn.board = _board([{"name": "Baltimore Ravens D/ST", "position": "DST",
                              "espn_id": "-16033", "team": "BAL", "bye_week": 7,
                              "adj_ppg": np.nan, "exp_games": 17.0, "proj_points": 120.0,
                              "replacement_points": 100.0, "vor": 0.0, "draft_score": 0.0,
                              "adp": 150.0, "off_roster": False, "is_rookie": False}])
        body = run(give="Baltimore Ravens D/ST")
        _refused(body)
        assert any("plays DST, which the simulated lineup does not score" in e
                   for e in body["errors"])

    def test_a_roster_entry_with_no_position_refuses(self, espn):
        # `rosters_by_team` has no row for him, so he was invisible downstream.
        espn.mine.append(_roster_entry(108, "Odd Man", 99, 12))
        body = run()
        _refused(body)
        assert "roster entries with no name or position (ESPN ids 108)" in body["errors"][0]

    @pytest.mark.usefixtures("espn")
    def test_a_counterparty_matching_two_teams_lists_them(self):
        body = run(counterparty_team="o")
        _refused(body)
        assert body["errors"] == ["counterparty_team 'o' matches 2 teams, not one"]
        assert body["teams"] == {str(MY_TEAM): "adverse possession",
                                 str(THEIR_TEAM): "Post Closing King"}

    @pytest.mark.usefixtures("espn")
    def test_your_own_team_is_not_a_counterparty(self):
        body = run(counterparty_team=str(MY_TEAM))
        _refused(body)
        assert body["errors"] == [f"team {MY_TEAM} is your own team"]


class TestWithoutALeague:
    def test_the_window_must_be_given(self, espn):
        body = run(league_id="", counterparty_team="", counterparty_slot=7)
        _refused(body)
        assert any("pass first_week and last_week" in e for e in body["errors"])
        assert espn.calls == []

    @pytest.mark.usefixtures("espn")
    def test_a_team_name_needs_a_league(self):
        body = run(league_id="", counterparty_slot=7, first_week=1, last_week=14)
        _refused(body)
        assert "counterparty_team names an ESPN team, which needs league_id" in body["errors"]
