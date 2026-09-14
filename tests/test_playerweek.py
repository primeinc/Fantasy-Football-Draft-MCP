"""`player_week`: ESPN's applied line, the opponent, and nflverse usage for one week.

ESPN stat rows are the live week 1 shape of 2026-09-13 (Carson Wentz: 0:19,
1:12, 3:133, 4:3, no key 20). nflverse frames carry the columns
`sources.weekly_stats` and `sources.snap_counts` publish.
"""
import json

import pandas as pd

from ffdraft import playerweek, pool, server, sources
from ffdraft.board import _ESPN_POSITION_NAMES


def _wentz(status="WAIVERS"):
    return {"id": 2573079, "status": status, "onTeamId": 0,
            "waiverProcessDate": 1789542000000,
            "player": {"id": 2573079, "fullName": "Carson Wentz", "defaultPositionId": 1,
                       "proTeamId": 16, "injuryStatus": "ACTIVE",
                       "stats": [{"seasonId": 2026, "scoringPeriodId": 1, "statSourceId": 0,
                                  "statSplitTypeId": 1, "appliedTotal": 19.42,
                                  "stats": {"0": 19.0, "1": 12.0, "3": 133.0, "4": 3.0,
                                            "23": 3.0, "24": 1.0, "210": 1.0}},
                                 {"seasonId": 2026, "scoringPeriodId": 1, "statSourceId": 1,
                                  "statSplitTypeId": 1, "appliedTotal": 0.53, "stats": {}}]}}


def _schedule():
    return pd.DataFrame([{"season": 2026, "week": 1, "game_type": "REG",
                          "home_team": "MIN", "away_team": "GB"},
                         {"season": 2026, "week": 2, "game_type": "REG",
                          "home_team": "CHI", "away_team": "MIN"}])


def _weekly():
    rows = [("Christian Watson", "GB", 8, 0, 6, 32.7), ("Jayden Reed", "GB", 4, 0, 2, 5.0),
            ("MarShawn Lloyd", "GB", 1, 13, 1, 3.7), ("Other Team", "MIN", 9, 20, 5, 11.0)]
    return pd.DataFrame([{"season": 2026, "week": 1, "season_type": "REG",
                          "player_display_name": n, "recent_team": t, "targets": tg,
                          "carries": c, "receptions": r, "fantasy_points_ppr": p}
                         for n, t, tg, c, r, p in rows])


def _snaps():
    return pd.DataFrame([{"season": 2026, "week": 1, "game_type": "REG",
                          "player": "Christian Watson", "team": "GB",
                          "offense_snaps": 58, "offense_pct": 0.87}])


class TestParts:
    def test_espn_line_names_what_espn_sent_and_leaves_out_the_rest(self):
        line = playerweek.espn_line(_wentz()["player"], 2026, 1)
        assert line == {"pass_attempts": 19.0, "pass_completions": 12.0, "pass_yards": 133.0,
                        "pass_tds": 3.0, "carries": 3.0, "rush_yards": 1.0}
        assert "interceptions" not in line

    def test_no_row_for_the_week_is_none(self):
        assert playerweek.espn_line(_wentz()["player"], 2026, 2) is None

    def test_opponent_home_away_and_bye(self):
        assert playerweek.opponent(_schedule(), 2026, 1, "GB") == "@ MIN"
        assert playerweek.opponent(_schedule(), 2026, 1, "MIN") == "vs GB"
        assert playerweek.opponent(_schedule(), 2026, 2, "GB") is None

    def test_usage_shares_are_of_the_teams_week_with_snaps(self):
        u = playerweek.nflverse_usage(_weekly(), _snaps(), 2026, 1, "Christian Watson", "GB")
        assert u is not None
        assert u["targets"] == 8.0 and u["target_share"] == round(8 / 13, 3)
        assert u["carry_share"] == 0.0
        assert u["offense_snaps"] == 58 and u["offense_pct"] == 0.87

    def test_a_player_the_published_week_lacks_is_none(self):
        assert playerweek.nflverse_usage(_weekly(), _snaps(), 2026, 1, "Carson Wentz", "MIN") is None

    def test_no_snap_row_leaves_snaps_none(self):
        u = playerweek.nflverse_usage(_weekly(), _snaps(), 2026, 1, "MarShawn Lloyd", "GB")
        assert u is not None
        assert u["carries"] == 13.0 and u["offense_snaps"] is None

    def test_a_missing_count_is_none_taken_not_nan(self):
        weekly = _weekly()
        weekly.loc[weekly["player_display_name"] == "MarShawn Lloyd", "targets"] = float("nan")
        u = playerweek.nflverse_usage(weekly, None, 2026, 1, "MarShawn Lloyd", "GB")
        assert u is not None
        assert u["targets"] == 0.0 and u["target_share"] == 0.0

    def test_published_weeks(self):
        assert playerweek.published_weeks(_weekly(), 2026) == [1]
        assert playerweek.published_weeks(_weekly(), 2025) == []


class TestTool:
    def run(self, monkeypatch, weekly_raises=False, **kw):
        monkeypatch.setattr(pool, "fetch_pool", lambda *a, **k: [_wentz()])
        monkeypatch.setattr(sources, "schedules", _schedule)

        def weekly(*a, **k):
            if weekly_raises:
                raise RuntimeError("no weekly stats loaded")
            return _weekly()
        monkeypatch.setattr(sources, "weekly_stats", weekly)
        monkeypatch.setattr(sources, "snap_counts", lambda *a, **k: _snaps())
        return json.loads(server.player_week("123", 1, **kw))

    def test_a_named_player_comes_back_with_every_part(self, monkeypatch):
        out = self.run(monkeypatch, names="wentz, Nobody")
        wentz = out["players"][0]
        assert wentz["opponent"] == "vs GB" and wentz["week_points"] == 19.42
        assert wentz["espn_line"]["pass_tds"] == 3.0
        assert wentz["nflverse"] is None
        assert out["not_found"] == ["Nobody"] and out["nflverse_weeks"] == [1]
        assert out["unread"] == {} and out["matches_dropped"] == {}

    def test_matches_past_three_are_named_not_dropped_silently(self, monkeypatch):
        out = self.run(monkeypatch, names="Carson Wentz")
        assert len(out["players"]) == 1
        many = [{**_wentz(), "player": {**_wentz()["player"], "fullName": f"Carson Wentz {i}"}}
                for i in range(5)]
        monkeypatch.setattr(pool, "fetch_pool", lambda *a, **k: many)
        out = json.loads(server.player_week("123", 1, "wentz"))
        assert len(out["players"]) == 3
        assert out["matches_dropped"] == {"wentz": ["Carson Wentz 3", "Carson Wentz 4"]}

    def test_an_unpublished_nflverse_season_is_named(self, monkeypatch):
        out = self.run(monkeypatch, weekly_raises=True, names="Carson Wentz")
        assert "nflverse_weekly" in out["unread"]
        assert out["nflverse_weeks"] is None and out["players"][0]["espn_line"]

    def test_names_are_required(self, monkeypatch):
        out = self.run(monkeypatch, names=" , ")
        assert "players" not in out and "names is required" in out["error"]

    def test_the_pool_row_is_positioned_through_the_board_table(self):
        row = playerweek.player_week_row(_wentz(), 2026, 1, _ESPN_POSITION_NAMES,
                                         None, None, None)
        assert row["position"] == "QB" and row["opponent"] is None and row["nflverse"] is None
