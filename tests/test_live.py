"""`live_scores`: the game on the field, per fantasy team, from ESPN's own
applied totals. Built 2026-09-09 at halftime of NE@SEA because the loop was
reporting "no change" while the user's back was on the field."""
import json

from ffdraft import live, server

SCOREBOARD = {"events": [
    {"id": "401", "shortName": "NE @ SEA",
     "status": {"type": {"state": "in", "detail": "Halftime"}},
     "competitions": [{"competitors": [
         {"homeAway": "home", "team": {"id": 26, "abbreviation": "SEA"}, "score": "0"},
         {"homeAway": "away", "team": {"id": 17, "abbreviation": "NE"}, "score": "7"}]}]},
    {"id": "402", "shortName": "SF VS LAR",
     "status": {"type": {"state": "pre", "detail": "Thu, September 10th at 8:35 PM EDT"}},
     "competitions": [{"competitors": [
         {"homeAway": "home", "team": {"id": 14, "abbreviation": "LAR"}, "score": "0"},
         {"homeAway": "away", "team": {"id": 25, "abbreviation": "SF"}, "score": "0"}]}]},
]}

SUMMARY = {"boxscore": {"players": [
    {"team": {"abbreviation": "NE"}, "statistics": [
        {"name": "rushing", "labels": ["CAR", "YDS", "TD"],
         "athletes": [{"athlete": {"displayName": "Rhamondre Stevenson"},
                       "stats": ["9", "18", "0"]}]}]}]}}


def _player(name, pid, pro, slot, live_pts, proj):
    return {"lineupSlotId": slot, "playerPoolEntry": {"player": {
        "id": pid, "fullName": name, "proTeamId": pro,
        "stats": [{"scoringPeriodId": 1, "statSourceId": 0, "appliedTotal": live_pts},
                  {"scoringPeriodId": 1, "statSourceId": 1, "appliedTotal": proj}]}}}


LEAGUE = {
    "teams": [
        {"id": 3, "name": "adverse possession", "owners": ["{ME}"],
         "roster": {"entries": [_player("Rhamondre Stevenson", 4569173, 17, 2, 3.3, 14.0),
                                _player("Ja'Marr Chase", 4362628, 4, 4, 0.0, 19.9)]}},
        {"id": 7, "name": "Kyle C's Star Team", "owners": ["{K}"],
         "roster": {"entries": [_player("Drake Maye", 4431452, 17, 0, 10.2, 16.3),
                                _player("Romeo Doubs", 4569000, 17, 20, 0.0, 8.2)]}},
        {"id": 9, "name": "Tina's Top Team", "owners": ["{T}"], "roster": {"entries": []}},
    ],
    "schedule": [
        {"id": 5, "matchupPeriodId": 1,
         "home": {"teamId": 3, "totalPointsLive": 3.3, "totalProjectedPointsLive": 103.5},
         "away": {"teamId": 9, "totalPointsLive": 0.0, "totalProjectedPointsLive": 95.5}},
        {"id": 4, "matchupPeriodId": 1,
         "home": {"teamId": 7, "totalPointsLive": 10.2, "totalProjectedPointsLive": 94.5},
         "away": {"teamId": 9, "totalPointsLive": 0.0, "totalProjectedPointsLive": 91.4}},
        {"id": 99, "matchupPeriodId": 2, "home": {"teamId": 3}, "away": {"teamId": 7}},
    ],
}


class TestTheReads:
    def test_games_carry_state_score_and_the_boards_abbreviation(self):
        g = live.games(SCOREBOARD)
        assert g[0]["state"] == "in" and g[0]["away"] == {"team": "NE", "score": "7"}
        # LAR on the scoreboard is LA on the board; the id decides.
        assert g[1]["home"]["team"] == "LA" and g[1]["state"] == "pre"

    def test_box_lines_are_labelled(self):
        lines = live.box_lines(SUMMARY)
        assert lines["Rhamondre Stevenson"]["rushing"] == {"CAR": "9", "YDS": "18", "TD": "0"}

    def test_league_players_and_matchups_for_the_period_only(self):
        ps = live.league_players(LEAGUE, 1)
        maye = next(p for p in ps if p["player"] == "Drake Maye")
        assert (maye["fantasy_team"], maye["pro_team"], maye["started"], maye["live"]) == (
            "Kyle C's Star Team", "NE", True, 10.2)
        doubs = next(p for p in ps if p["player"] == "Romeo Doubs")
        assert doubs["started"] is False
        ms = live.matchups(LEAGUE, 1)
        assert [m["matchup_id"] for m in ms] == [5, 4]


class TestTheTool:
    def wire(self, monkeypatch, scoreboard=SCOREBOARD):
        monkeypatch.setenv("ESPN_SWID", "{ME}")
        monkeypatch.setattr(live, "fetch_league_live", lambda *a, **k: LEAGUE)
        monkeypatch.setattr(live, "fetch_scoreboard", lambda *a, **k: scoreboard)
        monkeypatch.setattr(live, "fetch_summary", lambda *a, **k: SUMMARY)

    def test_the_game_in_progress_is_told_per_fantasy_team_with_my_box_line(self, monkeypatch):
        self.wire(monkeypatch)
        out = json.loads(server.live_scores("1", 1))
        assert out["no_game_in_progress"] is False
        assert len(out["games"]) == 1
        g = out["games"][0]
        assert g["game"] == "NE @ SEA" and g["detail"] == "Halftime"
        mine = g["players_by_fantasy_team"]["adverse possession"]
        assert mine[0]["player"] == "Rhamondre Stevenson" and mine[0]["live"] == 3.3
        assert mine[0]["box"]["rushing"]["YDS"] == "18"
        # Chase is not in this game.
        assert all(p["player"] != "Ja'Marr Chase" for p in mine)
        kyle = g["players_by_fantasy_team"]["Kyle C's Star Team"]
        assert [p["player"] for p in kyle] == ["Drake Maye", "Romeo Doubs"]
        assert "box" not in kyle[0]
        assert out["matchups"][0]["mine"] is True and out["matchups"][0]["matchup_id"] == 5
        assert out["next_kickoff"] == "Thu, September 10th at 8:35 PM EDT"

    def test_no_game_on_is_said_not_inferred(self, monkeypatch):
        pre = {"events": [dict(SCOREBOARD["events"][1])]}
        self.wire(monkeypatch, scoreboard=pre)
        out = json.loads(server.live_scores("1", 1))
        assert out["no_game_in_progress"] is True and out["games"] == []
        assert out["matchups"][0]["home"]["live"] == 3.3

    def test_an_unreadable_scoreboard_is_an_error(self, monkeypatch):
        self.wire(monkeypatch)
        monkeypatch.setattr(live, "fetch_scoreboard",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("403")))
        out = json.loads(server.live_scores("1", 1))
        assert "could not read the scoreboard" in out["error"]
