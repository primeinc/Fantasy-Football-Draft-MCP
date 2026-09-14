"""`league_rosters`: every team's players as ESPN holds them, without a browser.

The payload is the shape the live mRoster+mTeam response had on 2026-09-13
(league 1734659820): `teams[]` with `name`, `owners` and `roster.entries`;
`members[]` with `firstName`/`lastName`; a defense "Ravens D/ST" at position 16
filing no `injuryStatus`.
"""
import json

from ffdraft import rosters, server
from ffdraft.board import _ESPN_POSITION_NAMES, _ESPN_SLOT_NAMES, UNKNOWN_OWNER

SWID = "AAAA-1111"


def _entry(pid, name, pos_id, slot, pro_team=None, status="ACTIVE"):
    player = {"id": pid, "fullName": name, "defaultPositionId": pos_id}
    if pro_team is not None:
        player["proTeamId"] = pro_team
    if status is not None:
        player["injuryStatus"] = status
    return {"playerId": pid, "lineupSlotId": slot, "playerPoolEntry": {"player": player}}


def _payload():
    return {
        "teams": [
            {"id": 1, "name": "Office Pool Team", "owners": ["{BBBB-2222}"],
             "roster": {"entries": [
                 _entry(4429160, "De'Von Achane", 2, 2, pro_team=15),
                 _entry(3054850, "Alvin Kamara", 2, 20, pro_team=18, status="OUT")]}},
            {"id": 3, "name": "alpha home team", "owners": ["{AAAA-1111}"],
             "roster": {"entries": [
                 _entry(3917315, "Kyler Murray", 1, 0, pro_team=16, status="OUT"),
                 _entry(-16033, "Ravens D/ST", 16, 16, pro_team=33, status=None),
                 _entry(9001, "Mystery Man", 99, 24)]}},
            {"id": 2, "name": "Empty Team", "owners": ["{CCCC-3333}"],
             "roster": {"entries": []}},
        ],
        "members": [
            {"id": "{AAAA-1111}", "firstName": "Home", "lastName": "Owner"},
            {"id": "{BBBB-2222}", "firstName": "Pat", "lastName": "Example"},
            {"id": "{CCCC-3333}"},
        ],
    }


def _table(monkeypatch):
    monkeypatch.setenv("ESPN_SWID", SWID)
    return rosters.league_table(_payload(), _ESPN_POSITION_NAMES, _ESPN_SLOT_NAMES)


def _players(table, team_id):
    return next(t for t in table if t["team_id"] == team_id)["players"]


class TestLeagueTable:
    def test_every_team_is_listed_mine_first_then_by_id(self, monkeypatch):
        table = _table(monkeypatch)
        assert [t["team_id"] for t in table] == [3, 1, 2]
        assert [t["mine"] for t in table] == [True, False, False]

    def test_a_row_carries_espn_slot_and_status_as_filed(self, monkeypatch):
        table = _table(monkeypatch)
        assert _players(table, 3)[0] == ["Kyler Murray", "QB", "MIN", "QB", "OUT"]
        assert _players(table, 1)[1] == ["Alvin Kamara", "RB", "NO", "BENCH", "OUT"]

    def test_a_defense_with_no_status_keeps_none_not_active(self, monkeypatch):
        assert _players(_table(monkeypatch), 3)[1] == ["Ravens D/ST", "DST", "BAL", "DST", None]

    def test_an_unpublished_position_or_slot_prints_as_its_id(self, monkeypatch):
        assert _players(_table(monkeypatch), 3)[2] == [
            "Mystery Man", "position 99", None, "slot 24", "ACTIVE"]

    def test_an_empty_roster_is_listed_empty_not_dropped(self, monkeypatch):
        assert _players(_table(monkeypatch), 2) == []

    def test_owners_are_named_and_never_by_swid(self, monkeypatch):
        table = _table(monkeypatch)
        assert next(t for t in table if t["team_id"] == 1)["owners"] == ["Pat Example"]
        assert next(t for t in table if t["team_id"] == 2)["owners"] == [UNKNOWN_OWNER]
        text = json.dumps(table)
        assert "CCCC" not in text and "AAAA" not in text

    def test_no_swid_marks_no_team_as_mine(self, monkeypatch):
        monkeypatch.delenv("ESPN_SWID", raising=False)
        table = rosters.league_table(_payload(), _ESPN_POSITION_NAMES, _ESPN_SLOT_NAMES)
        assert not any(t["mine"] for t in table)
        assert [t["team_id"] for t in table] == [1, 2, 3]


class TestTool:
    def _run(self, monkeypatch, raises=None, **kw):
        captured: dict = {}

        def fetch(league_id, season, week):
            captured.update(league_id=league_id, season=season, week=week)
            if raises:
                raise raises
            return _payload()
        monkeypatch.setenv("ESPN_SWID", SWID)
        monkeypatch.setattr(rosters, "fetch_roster_payload", fetch)
        return json.loads(server.league_rosters("123", **kw)), captured

    def test_lists_every_team_with_its_columns_and_basis(self, monkeypatch):
        out, captured = self._run(monkeypatch)
        assert out["columns"] == ["player", "position", "pro_team", "slot", "status"]
        assert [t["team"] for t in out["teams"]] == [
            "alpha home team", "Office Pool Team", "Empty Team"]
        assert out["empty_rosters"] == ["Empty Team"]
        assert out["no_team_matches"] is None
        assert out["basis"]
        assert captured["week"] is None and out["week"] == "current"

    def test_a_week_becomes_the_scoring_period(self, monkeypatch):
        out, captured = self._run(monkeypatch, week=2)
        assert captured["week"] == 2 and out["week"] == 2

    def test_team_narrows_by_owner_name_or_id(self, monkeypatch):
        by_owner, _ = self._run(monkeypatch, team="pat ex")
        assert [t["team_id"] for t in by_owner["teams"]] == [1]
        by_id, _ = self._run(monkeypatch, team="3")
        assert [t["team_id"] for t in by_id["teams"]] == [3]
        by_name, _ = self._run(monkeypatch, team="EMPTY")
        assert [t["team_id"] for t in by_name["teams"]] == [2]

    def test_a_team_that_matches_nothing_says_so(self, monkeypatch):
        out, _ = self._run(monkeypatch, team="nobody")
        assert out["teams"] == [] and out["no_team_matches"] == "nobody"

    def test_an_unreadable_roster_is_an_error_not_an_empty_league(self, monkeypatch):
        out, _ = self._run(monkeypatch, raises=RuntimeError("401"))
        assert "teams" not in out
        assert out["error"] == "could not read ESPN's rosters: RuntimeError: 401"
