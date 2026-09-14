"""`governor.controller_state`: fantasy preempts engineering, and idle is computed.

Times are 2026-09-13 Eastern: DAL@NYG kicks off 20:20 (00:20Z Monday).
"""
import json

import pytest

from ffdraft import governor, live, pool, rosters, server

NOW = "2026-09-13T22:00:00Z"  # 18:00 ET


def game(name, teams, state, date="2026-09-14T00:20Z", detail=""):
    return {"name": name, "teams": set(teams), "state": state, "date": date, "detail": detail}


def player(name, team, started=True, locked=False, status="ACTIVE"):
    return {"player": name, "pro_team": team, "started": started, "locked": locked,
            "injury_status": status}


def state(games=(), mine=(), theirs=(), clears=None, points=(), unread=None, now=NOW, byes=()):
    return governor.controller_state(now, list(games), list(mine), list(theirs), clears,
                                     list(points), unread or {}, set(byes))


class TestModes:
    def test_a_started_player_on_bye_is_actionable(self):
        # Absent from the scoreboard is not the test; the schedule's bye list is.
        out = state([game("DAL @ NYG", {"DAL", "NYG"}, "pre")], [player("Puka Nacua", "LA")],
                    byes={"LA"})
        assert out["mode"] == "HOT"
        assert out["fantasy_actionable"] == ["Puka Nacua starts and LA is on bye"]

    def test_a_benched_or_locked_player_on_bye_is_not(self):
        out = state([], [player("Nacua", "LA", started=False), player("Kupp", "LA", locked=True)],
                    byes={"LA"})
        assert out["fantasy_actionable"] == []

    def test_a_team_missing_from_the_scoreboard_is_not_a_bye(self):
        assert state([], [player("Nacua", "LA")])["fantasy_actionable"] == []

    def test_a_scoreboard_ahead_of_the_league_week_adds_nothing_from_my_roster(self):
        out = governor.controller_state(NOW, [game("DAL @ NYG", {"DAL", "NYG"}, "pre")],
                                        [player("Nacua", "LA"), player("Tracy", "NYG", status="OUT")],
                                        [], None, [], {}, {"LA"}, scoreboard_ahead=True)
        assert out["fantasy_actionable"] == [] and out["mode"] != "HOT"

    def test_an_out_starter_before_his_lock_is_hot(self):
        out = state([game("DAL @ NYG", {"DAL", "NYG"}, "pre")],
                    [player("Tyrone Tracy Jr.", "NYG", status="OUT")])
        assert out["mode"] == "HOT"
        assert out["fantasy_actionable"] == [
            "Tyrone Tracy Jr. starts, is OUT, and locks 2026-09-13 20:20 ET"]
        assert out["engineering"]["allowed"] is False

    def test_an_out_bench_player_is_not_actionable(self):
        out = state([game("DAL @ NYG", {"DAL", "NYG"}, "pre")],
                    [player("Tyrone Tracy Jr.", "NYG", started=False, status="OUT")])
        assert out["fantasy_actionable"] == []
        # Inactives at 18:50 ET is 50 minutes out: 35 minutes of budget.
        assert out["next_required_attention"]["at"] == "2026-09-13 18:50 ET"
        assert out["idle_budget_minutes"] == 35 and out["mode"] == "IDLE"
        assert out["engineering"] == {"allowed": True, "max_minutes": 35, "max_risk_class": "A",
                                      "lease_expires": "2026-09-13 18:35 ET"}

    def test_attention_within_thirty_minutes_is_hot(self):
        out = state([game("DAL @ NYG", {"DAL", "NYG"}, "pre")],
                    [player("Tracy", "NYG", started=False)],
                    now="2026-09-13T22:30:00Z")
        assert out["mode"] == "HOT"

    def test_my_game_in_progress_is_watch_even_with_everyone_locked(self):
        out = state([game("DAL @ NYG", {"DAL", "NYG"}, "in", detail="2nd Quarter")],
                    [player("Tracy", "NYG", locked=True)])
        assert out["mode"] == "WATCH"
        assert out["observing"] == ["DAL @ NYG 2nd Quarter"]
        assert out["engineering"]["allowed"] is False

    def test_a_game_nobody_of_mine_or_my_opponents_plays_in_is_ignored(self):
        out = state([game("KC @ DEN", {"KC", "DEN"}, "in")], [player("Tracy", "NYG", locked=True)],
                    [player("Mahomes", "BUF", locked=True)])
        assert out["observing"] == [] and out["mode"] == "DEEP_IDLE"

    def test_my_opponents_game_is_watched(self):
        out = state([game("KC @ DEN", {"KC", "DEN"}, "in")], [], [player("Chiefs D/ST", "KC")])
        assert out["mode"] == "WATCH"

    def test_a_decision_point_names_teams_to_watch_and_sets_attention(self):
        points = [{"at": "2026-09-14T03:47:00Z", "what": "Tracy final", "teams": ["SEA"]}]
        out = state([game("SEA @ ARI", {"SEA", "ARI"}, "in")], points=points)
        assert out["mode"] == "WATCH"
        out = state(points=points)
        assert out["next_required_attention"] == {"at": "2026-09-13 23:47 ET",
                                                  "why": "decision point: Tracy final"}
        assert out["mode"] == "DEEP_IDLE" and out["engineering"]["max_risk_class"] == "B"
        assert out["engineering"]["max_minutes"] == governor.MAX_LEASE_MINUTES

    def test_past_decision_points_and_past_waivers_are_ignored(self):
        out = state(clears=1789333200000,  # 2026-09-13T21:00Z
                    points=[{"at": "2026-09-13T20:00:00Z", "what": "old", "teams": ["SEA"]}])
        assert out["next_required_attention"] is None and out["mode"] == "DEEP_IDLE"

    def test_the_waiver_clear_is_attention(self):
        out = state(clears=1789542000000)
        assert out["next_required_attention"] == {"at": "2026-09-16 03:00 ET",
                                                  "why": "waivers process"}

    def test_an_unreadable_scoreboard_is_degraded(self):
        out = state(unread={"scoreboard": "RuntimeError: 503"})
        assert out["mode"] == "DEGRADED" and out["engineering"]["allowed"] is False

    def test_an_unreadable_decision_file_is_reported_not_degraded(self):
        out = state(unread={"decision_points": "ValueError"})
        assert out["mode"] == "DEEP_IDLE"
        assert out["degraded_capabilities"] == {"decision_points": "ValueError"}

    def test_the_engineering_minimum_is_the_budget_floor(self, monkeypatch):
        # Inactives 18:50 ET, now 18:19: 31 minutes out, 16 of budget.
        tick = ([game("DAL @ NYG", {"DAL", "NYG"}, "pre")], [player("Tracy", "NYG", started=False)])
        out = state(*tick, now="2026-09-13T22:19:00Z")
        assert out["mode"] == "IDLE" and out["idle_budget_minutes"] == 16
        assert out["engineering"]["allowed"] is True
        monkeypatch.setattr(governor, "MIN_ENGINEERING_MINUTES", 20)
        out = state(*tick, now="2026-09-13T22:19:00Z")
        assert out["mode"] == "IDLE" and out["engineering"]["allowed"] is False


class TestReads:
    def test_roster_and_opponent_from_the_league_payload(self):
        payload = {"teams": [{"id": 3, "roster": {"entries": [
            {"lineupSlotId": 20, "playerPoolEntry": {"lineupLocked": True, "player": {
                "fullName": "Tyrone Tracy Jr.", "proTeamId": 19, "injuryStatus": "ACTIVE"}}}]}}],
            "schedule": [{"matchupPeriodId": 1, "home": {"teamId": 9}, "away": {"teamId": 3}}]}
        assert governor.roster(payload, 3) == [{"player": "Tyrone Tracy Jr.", "pro_team": "NYG",
                                               "started": False, "locked": True,
                                               "injury_status": "ACTIVE"}]
        assert governor.opponent_id(payload, 1, 3) == 9
        assert governor.opponent_id(payload, 2, 3) is None

    def test_decision_points_round_trip_and_need_a_zone(self, tmp_path):
        path = tmp_path / "points.json"
        assert governor.load_decision_points(path) == ([], None)
        row = governor.add_decision_point("2026-09-13T23:47:00-04:00", "Tracy final", ["NYG"], path)
        assert row["at"] == "2026-09-14T03:47:00+00:00"
        assert governor.load_decision_points(path) == ([row], None)
        with pytest.raises(ValueError):
            governor.add_decision_point("2026-09-13T23:47:00", "no zone", [], path)
        path.write_text("{", encoding="utf-8")
        err = governor.load_decision_points(path)[1]
        assert err is not None and err.startswith("JSONDecodeError")


class TestTool:
    def test_the_tool_composes_the_reads_and_writes_the_state(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ESPN_SWID", "AAAA")
        monkeypatch.setattr(governor, "CONTROLLER_STATE", tmp_path / "state.json")
        monkeypatch.setattr(governor, "DECISION_POINTS", tmp_path / "none.json")
        # The scoreboard has rolled to week 2; the league is still in period 1.
        monkeypatch.setattr(live, "fetch_scoreboard", lambda *a, **k: {"week": {"number": 2},
                                                                       "events": []})
        monkeypatch.setattr(live, "fetch_league_status", lambda *a, **k: {
            "scoringPeriodId": 1, "status": {"currentMatchupPeriod": 1}})
        periods: list = []
        monkeypatch.setattr(governor, "opponent_id",
                            lambda payload, week, team: periods.append(week))
        monkeypatch.setattr(live, "fetch_league_live", lambda *a, **k: {
            "teams": [{"id": 3, "owners": ["{AAAA}"], "roster": {"entries": []}}], "schedule": []})
        monkeypatch.setattr(rosters, "my_team_id", lambda teams, swid=None: 3)
        monkeypatch.setattr(pool, "next_waiver_clear", lambda *a, **k: None)
        import pandas as pd

        from ffdraft import sources
        monkeypatch.setattr(sources, "schedules", lambda: pd.DataFrame(
            [{"season": 2026, "week": 1, "game_type": "REG", "home_team": "MIN",
              "away_team": "GB"}]))
        out = json.loads(server.controller_state("123"))
        assert out["week"] == 1 and out["scoreboard_week"] == 2 and periods == [1]
        assert out["mode"] == "DEEP_IDLE"
        assert json.loads((tmp_path / "state.json").read_text())["mode"] == "DEEP_IDLE"

    def test_a_scoreboard_failure_degrades(self, monkeypatch, tmp_path):
        monkeypatch.setattr(governor, "CONTROLLER_STATE", tmp_path / "state.json")
        monkeypatch.setattr(governor, "DECISION_POINTS", tmp_path / "none.json")

        def boom(*a, **k):
            raise RuntimeError("503")
        monkeypatch.setattr(live, "fetch_scoreboard", boom)
        monkeypatch.setattr(live, "fetch_league_status", boom)
        monkeypatch.setattr(pool, "next_waiver_clear", lambda *a, **k: None)
        out = json.loads(server.controller_state("123"))
        assert out["mode"] == "DEGRADED" and "scoreboard" in out["degraded_capabilities"]
