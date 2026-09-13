"""`league_free_agents`: ESPN's acquirable pool for the league, as ESPN states it.

Entry shape as the live in-season `kona_player_info` returned it on 2026-09-13
(league 1734659820, week 1): `status`, `onTeamId`, `waiverProcessDate` on the
entry; `ownership` and `stats` on the player; stats rows keyed by season,
scoring period, source (0 actual, 1 projected) and split (1 one period).
"""
import json

from ffdraft import pool, server
from ffdraft.board import _ESPN_POSITION_NAMES

WAIVER_MS = 1789542000000  # 2026-09-16T07:00Z, the live WAIVERS rows' process date


def _stat(source, total, week=1, season=2026, split=1):
    return {"seasonId": season, "scoringPeriodId": week, "statSourceId": source,
            "statSplitTypeId": split, "appliedTotal": total}


def _entry(pid, name, pos_id, pro_team, status="WAIVERS", on_team=0, actual=None,
           projected=None, owned=1.0, injury="ACTIVE"):
    stats = [_stat(1, 0.0, week=0, split=0)]
    if actual is not None:
        stats.append(_stat(0, actual))
    if projected is not None:
        stats.append(_stat(1, projected))
    return {"id": pid, "status": status, "onTeamId": on_team,
            "waiverProcessDate": WAIVER_MS if status == "WAIVERS" else 0,
            "player": {"id": pid, "fullName": name, "defaultPositionId": pos_id,
                       "proTeamId": pro_team, "injuryStatus": injury,
                       "ownership": {"percentOwned": owned, "percentChange": 0.1},
                       "stats": stats}}


def _players():
    return [
        _entry(2578570, "Jacoby Brissett", 1, 22, actual=16.48, projected=13.15, owned=5.09),
        _entry(2573079, "Carson Wentz", 1, 16, actual=19.42, projected=0.53, owned=0.06),
        _entry(4569559, "Devaughn Vele", 3, 18, actual=19.9, projected=9.31, owned=12.87),
        _entry(3917315, "Kyler Murray", 1, 16, status="ONTEAM", on_team=3, actual=-0.4,
               projected=16.4, owned=90.0, injury="OUT"),
        _entry(9001, "Idle Free Agent", 2, 0, status="FREEAGENT", owned=0.0),
        _entry(9002, "Oddity", 99, 5, status="FREEAGENT"),
    ]


class TestPoolRows:
    def rows(self):
        return pool.pool_rows(_players(), _ESPN_POSITION_NAMES, 2026, 1)

    def test_a_waiver_row_carries_its_clear_time_in_eastern(self):
        brissett = self.rows()[0]
        assert brissett["waiver_clears"] == "2026-09-16 03:00 ET"
        assert brissett["status"] == "WAIVERS" and brissett["pro_team"] == "ARI"

    def test_week_actual_and_projection_come_from_their_own_rows(self):
        wentz = self.rows()[1]
        assert wentz["week_points"] == 19.42 and wentz["week_proj"] == 0.53

    def test_no_row_for_the_week_is_none_not_zero(self):
        idle = next(r for r in self.rows() if r["player"] == "Idle Free Agent")
        assert idle["week_points"] is None and idle["week_proj"] is None
        assert idle["pro_team"] is None and idle["waiver_clears"] is None

    def test_an_unpublished_position_prints_as_its_id(self):
        odd = next(r for r in self.rows() if r["player"] == "Oddity")
        assert odd["position"] == "position 99"

    def test_a_projection_from_another_period_is_not_this_week(self):
        row = pool.pool_rows([_entry(1, "Other Week", 2, 1, projected=None)],
                             _ESPN_POSITION_NAMES, 2026, 1)[0]
        assert row["week_proj"] is None

    def test_acquirable_excludes_a_rostered_player(self):
        names = [r["player"] for r in pool.acquirable(self.rows())]
        assert "Kyler Murray" not in names and "Jacoby Brissett" in names

    def test_a_waiver_status_held_by_a_team_is_not_acquirable(self):
        held = pool.pool_rows([_entry(5, "Held", 2, 1, status="WAIVERS", on_team=4)],
                              _ESPN_POSITION_NAMES, 2026, 1)
        assert pool.acquirable(held) == []


class TestTool:
    def run(self, monkeypatch, raises=None, **kw):
        captured: dict = {}

        def fetch(league_id, season, week):
            captured.update(league_id=league_id, season=season, week=week)
            if raises:
                raise raises
            return _players()
        monkeypatch.setattr(pool, "fetch_pool", fetch)
        return json.loads(server.league_free_agents("123", 1, **kw)), captured

    def column(self, out, name):
        i = out["columns"].index(name)
        return [row[i] for row in out["players"]]

    def test_default_lists_acquirable_by_week_points_with_census(self, monkeypatch):
        out, captured = self.run(monkeypatch)
        assert captured["week"] == 1
        assert self.column(out, "player")[:3] == ["Devaughn Vele", "Carson Wentz",
                                                 "Jacoby Brissett"]
        assert "Kyler Murray" not in self.column(out, "player")
        assert out["census"] == {"FREEAGENT": 2, "ONTEAM": 1, "WAIVERS": 3}
        assert out["acquirable"] == 5 and out["matched"] == 5
        assert out["shape"] == pool.POOL_SHAPE and out["basis"]

    def test_none_sorts_last_not_first(self, monkeypatch):
        out, _ = self.run(monkeypatch)
        assert self.column(out, "week_points")[-2:] == [None, None]

    def test_position_narrows(self, monkeypatch):
        out, _ = self.run(monkeypatch, position="qb")
        assert self.column(out, "player") == ["Carson Wentz", "Jacoby Brissett"]

    def test_names_report_the_rostered_and_the_unknown(self, monkeypatch):
        out, _ = self.run(monkeypatch, names="wentz, Kyler Murray, Nobody Atall")
        assert self.column(out, "player") == ["Carson Wentz"]
        assert out["not_acquirable"] == [{"player": "Kyler Murray", "status": "ONTEAM",
                                          "on_team_id": 3}]
        assert out["not_found"] == ["Nobody Atall"]

    def test_sort_by_ownership(self, monkeypatch):
        out, _ = self.run(monkeypatch, sort="percent_owned", limit=1)
        assert self.column(out, "player") == ["Devaughn Vele"]
        assert out["matched"] == 5 and len(out["players"]) == 1

    def test_an_unknown_sort_is_refused(self, monkeypatch):
        out, _ = self.run(monkeypatch, sort="vibes")
        assert "players" not in out and "vibes" in out["error"]

    def test_an_unreadable_pool_is_an_error_not_an_empty_wire(self, monkeypatch):
        out, _ = self.run(monkeypatch, raises=RuntimeError("403"))
        assert "players" not in out
        assert out["error"] == "could not read ESPN's player pool: RuntimeError: 403"
