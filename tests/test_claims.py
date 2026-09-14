"""`waiver_candidates`: ADD x -> DROP y pairs derived from the read surfaces.

A slotted starter is never a drop (Christian Watson, ESPN WR slot, 32.7 points
in week 1), and a bench player whose game has not been played (Tyrone Tracy,
NYG on Sunday night) is held rather than offered.
"""
import json

import pandas as pd

from ffdraft import claims, live, pool, rosters, server, sources

SWID = "AAAA-1111"
OUT_STATUS = "OUT"


def _row(pid, name, pos, team, status="ONTEAM", on_team=3, proj=None, last=None,
         injury="ACTIVE", droppable=True):
    return {"espn_id": pid, "player": name, "position": pos, "pro_team": team,
            "status": status, "on_team_id": on_team,
            "waiver_clears": "2026-09-16 03:00 ET" if status == "WAIVERS" else None,
            "injury_status": injury, "droppable": droppable, "percent_owned": 1.0,
            "percent_change": 0.0, "week_points": None, "week_proj": proj,
            "claim_week_proj": proj, "last_week_points": last, "last_week_proj": None}


def _mine():
    return [_row(1, "Kyler Murray", "QB", "MIN", proj=14.0, last=-0.4, injury=OUT_STATUS),
            _row(2, "Christian Watson", "WR", "GB", proj=12.0, last=32.7),
            _row(3, "Jerry Jeudy", "WR", "CLE", proj=7.0, last=5.0),
            _row(4, "Tyrone Tracy Jr.", "RB", "NYG", proj=3.0, last=None),
            _row(5, "Breece Hall", "RB", "NYJ", proj=16.0, last=18.0),
            _row(6, "Rhamondre Stevenson", "RB", "NE", proj=13.0, last=14.5),
            _row(7, "MarShawn Lloyd", "RB", "GB", proj=9.0, last=3.7)]


SLOTS = {1: 0, 2: 4, 3: 20, 4: 20, 5: 2, 6: 2, 7: 20}
REQUIRED = {"QB": 1, "RB": 2, "WR": 1}


def _pool():
    return _mine() + [
        _row(10, "Jacoby Brissett", "QB", "ARI", status="WAIVERS", on_team=0, proj=17.1, last=16.48),
        _row(11, "Carson Wentz", "QB", "MIN", status="WAIVERS", on_team=0, proj=9.0, last=19.22),
        _row(12, "Tua Tagovailoa", "QB", "ATL", status="WAIVERS", on_team=0, proj=15.0,
             injury=OUT_STATUS),
        _row(13, "Devaughn Vele", "WR", "NO", status="WAIVERS", on_team=0, proj=11.0, last=19.9),
        _row(14, "Held Elsewhere", "QB", "KC", status="ONTEAM", on_team=9, proj=20.0),
    ]


class TestRules:
    def test_required_starters_come_from_espn_slot_counts(self):
        settings = {"rosterSettings": {"lineupSlotCounts": {
            "0": 1, "2": 2, "4": 2, "6": 1, "16": 1, "17": 1, "20": 6, "21": 1, "23": 0}}}
        assert claims.required_starters(settings) == {
            "QB": 1, "RB": 2, "WR": 2, "TE": 1, "DST": 1, "K": 1}

    def test_byes_are_teams_with_a_season_but_no_game_that_week(self):
        schedule = pd.DataFrame([
            {"season": 2026, "week": 1, "game_type": "REG", "home_team": "MIN", "away_team": "GB"},
            {"season": 2026, "week": 2, "game_type": "REG", "home_team": "CHI", "away_team": "MIN"},
            {"season": 2026, "week": 1, "game_type": "REG", "home_team": "CHI", "away_team": "DET"}])
        assert claims.byes(schedule, 2026, 2) == {"GB", "DET"}

    def test_pending_needs_the_scoreboard_to_show_the_played_week(self):
        games = [{"state": "pre", "teams": {"NYG", "DAL"}}, {"state": "post", "teams": {"GB", "MIN"}}]
        assert claims.pending_teams(games, 1, 1) == {"NYG", "DAL"}
        assert claims.pending_teams(games, 2, 1) is None

    def test_merge_puts_last_week_beside_the_claim_week(self):
        merged = claims.merge_weeks([{"espn_id": 1, "week_proj": 9.0, "week_points": None}],
                                    [{"espn_id": 1, "week_proj": 0.5, "week_points": 19.2}])
        assert merged[0]["claim_week_proj"] == 9.0 and merged[0]["last_week_points"] == 19.2


class TestNeedsAndDrops:
    def test_an_out_starter_with_no_backup_is_a_need(self):
        assert claims.needs(_mine(), REQUIRED, set()) == [
            {"position": "QB", "short": 1, "required": 1, "why": ["Kyler Murray: OUT"]}]

    def test_a_different_claim_week_status_is_named_beside_the_current_one(self):
        mine = _mine()
        mine[0]["claim_week_injury_status"] = "QUESTIONABLE"
        assert claims.needs(mine, REQUIRED, set())[0]["why"] == [
            "Kyler Murray: OUT now; ESPN lists QUESTIONABLE for the claim week"]

    def test_a_bye_makes_a_need(self):
        needs = claims.needs(_mine(), REQUIRED, {"GB", "CLE"})
        assert {"position": "WR", "short": 1, "required": 1,
                "why": ["Christian Watson: bye", "Jerry Jeudy: bye"]} in needs

    def test_a_slotted_starter_is_never_a_drop_and_an_unplayed_game_is_held(self):
        drops, excluded = claims.drop_options(_mine(), SLOTS, REQUIRED, set(), {"NYG", "DAL"})
        assert [d["player"] for d in drops] == ["Jerry Jeudy", "MarShawn Lloyd", "Tyrone Tracy Jr."]
        assert drops[-1]["hold"] == "his NYG game this week is not final"
        assert "Christian Watson" not in [d["player"] for d in drops] + [e["player"] for e in excluded]

    def test_without_a_scoreboard_answer_nobody_is_held(self):
        drops, _ = claims.drop_options(_mine(), SLOTS, REQUIRED, set(), None)
        assert drops[0]["player"] == "Tyrone Tracy Jr." and drops[0]["hold"] is None

    def test_undroppable_and_position_short_are_excluded_with_reasons(self):
        mine = _mine()
        mine[2]["droppable"] = False
        slots = {**SLOTS, 5: 20}
        required = {"QB": 1, "RB": 4, "WR": 1}
        drops, excluded = claims.drop_options(mine, slots, required, set(), set())
        assert {"player": "Jerry Jeudy", "reason": "on ESPN's undroppable list"} in excluded
        assert {"player": "Breece Hall", "reason": "dropping him leaves RB short"} in excluded
        assert drops == []

    def test_no_backup_lists_positions_filled_exactly(self):
        assert claims.no_backup(_mine(), {"QB": 1, "RB": 2, "WR": 1}, set()) == []
        assert claims.no_backup(_mine(), {"QB": 1, "WR": 2}, set()) == ["WR"]


class TestAtRisk:
    def test_a_questionable_starter_with_no_backup_is_at_risk_not_a_need(self):
        # Kyler Murray, 2026-09-13 evening: ESPN moved him OUT -> QUESTIONABLE.
        mine = _mine()
        mine[0]["injury_status"] = "QUESTIONABLE"
        assert claims.needs(mine, REQUIRED, set()) == []
        assert claims.at_risk(mine, REQUIRED, set()) == [
            {"position": "QB", "short": 0, "required": 1, "why": ["Kyler Murray: QUESTIONABLE"]}]

    def test_a_questionable_player_with_a_backup_is_not_at_risk(self):
        mine = _mine()
        mine[1]["injury_status"] = "QUESTIONABLE"
        assert claims.at_risk(mine, REQUIRED, set()) == []

    def test_insurance_takes_the_drop_after_the_ones_the_needs_used(self):
        drops, _ = claims.drop_options(_mine(), SLOTS, REQUIRED, set(), set())
        plan = claims.plan([{"position": "QB", "short": 0, "required": 1, "why": []}],
                           drops, _pool(), 1, drops_taken=1)
        assert [d["player"] for d in drops][:2] == ["Tyrone Tracy Jr.", "Jerry Jeudy"]
        assert plan[0]["drop"] == "Jerry Jeudy"


class TestPlan:
    def test_claims_rank_candidates_with_one_drop_and_fallbacks(self):
        drops, _ = claims.drop_options(_mine(), SLOTS, REQUIRED, set(), {"NYG"})
        plan = claims.plan(claims.needs(_mine(), REQUIRED, set()), drops, _pool(), 3)
        assert [(c["add"], c["drop"], c["fallback_for"]) for c in plan] == [
            ("Jacoby Brissett", "Jerry Jeudy", None),
            ("Carson Wentz", "Jerry Jeudy", "Jacoby Brissett")]
        assert plan[0]["add_evidence"]["last_week_points"] == 16.48

    def test_candidates_exclude_the_out_and_the_rostered(self):
        names = [c["player"] for c in claims.candidates(_pool(), "QB", 5)]
        assert names == ["Jacoby Brissett", "Carson Wentz"]

    def test_no_usable_drop_is_said(self):
        plan = claims.plan([{"position": "QB", "short": 1, "required": 1, "why": []}], [],
                           _pool(), 1)
        assert plan[0]["drop"] is None and plan[0]["no_drop_reason"]

    def test_upgrades_are_measured_against_the_cheapest_usable_drop(self):
        drops, _ = claims.drop_options(_mine(), SLOTS, REQUIRED, set(), {"NYG"})
        ups = claims.upgrades(_pool(), drops, 3)
        assert [(u["add"], u["drop"], u["margin"]) for u in ups] == [
            ("Jacoby Brissett", "Jerry Jeudy", 10.1), ("Devaughn Vele", "Jerry Jeudy", 4.0),
            ("Carson Wentz", "Jerry Jeudy", 2.0)]


def _entry(row):
    """A pool entry that `pool.pool_rows` reads back into `row` for `week`."""
    stats = []
    if row["claim_week_proj"] is not None:
        stats.append({"seasonId": 2026, "scoringPeriodId": 2, "statSourceId": 1,
                      "statSplitTypeId": 1, "appliedTotal": row["claim_week_proj"]})
    if row["last_week_points"] is not None:
        stats.append({"seasonId": 2026, "scoringPeriodId": 1, "statSourceId": 0,
                      "statSplitTypeId": 1, "appliedTotal": row["last_week_points"]})
    team_ids = {"MIN": 16, "GB": 9, "CLE": 5, "NYG": 19, "NYJ": 20, "NE": 17, "ARI": 22,
                "ATL": 1, "NO": 18, "KC": 12}
    positions = {"QB": 1, "RB": 2, "WR": 3}
    return {"id": row["espn_id"], "status": row["status"], "onTeamId": row["on_team_id"],
            "waiverProcessDate": 1789542000000 if row["status"] == "WAIVERS" else 0,
            "player": {"id": row["espn_id"], "fullName": row["player"],
                       "defaultPositionId": positions[row["position"]],
                       "proTeamId": team_ids[row["pro_team"]], "injuryStatus": row["injury_status"],
                       "droppable": row["droppable"], "ownership": {"percentOwned": 1.0},
                       "stats": stats}}


def _event(away, home, state):
    ids = {"NYG": 19, "DAL": 6, "GB": 9, "MIN": 16}
    return {"id": "1", "shortName": f"{away} @ {home}", "status": {"type": {"state": state}},
            "competitions": [{"competitors": [
                {"homeAway": "home", "team": {"id": str(ids[home])}, "score": "0"},
                {"homeAway": "away", "team": {"id": str(ids[away])}, "score": "0"}]}]}


class TestTool:
    def run(self, monkeypatch, scoreboard_week=1, pool_rows=None):
        rows = _pool() if pool_rows is None else pool_rows
        monkeypatch.setenv("ESPN_SWID", SWID)
        payload = {"teams": [{"id": 3, "name": "adverse possession", "owners": ["{AAAA-1111}"],
                              "waiverRank": 12, "roster": {"entries": [
                                  {"playerId": pid, "lineupSlotId": slot}
                                  for pid, slot in SLOTS.items()]}}]}
        monkeypatch.setattr(rosters, "fetch_roster_payload", lambda *a, **k: payload)
        monkeypatch.setattr(pool, "fetch_pool",
                            lambda league_id, season, week=None: [_entry(r) for r in rows])
        monkeypatch.setattr(claims, "fetch_settings", lambda *a, **k: {
            "rosterSettings": {"lineupSlotCounts": {"0": 1, "2": 2, "4": 1, "20": 6}}})
        teams = ["MIN", "GB", "CLE", "NYG", "NYJ", "NE", "ARI", "ATL", "NO", "KC"]
        monkeypatch.setattr(sources, "schedules", lambda: pd.DataFrame(
            [{"season": 2026, "week": 2, "game_type": "REG", "home_team": teams[i],
              "away_team": teams[i + 1]} for i in range(0, 10, 2)]))
        monkeypatch.setattr(live, "fetch_scoreboard", lambda *a, **k: {
            "week": {"number": scoreboard_week},
            "events": [_event("DAL", "NYG", "pre"), _event("GB", "MIN", "post")]})
        return json.loads(server.waiver_candidates("123", 2))

    def test_the_week_two_qb_claim_pairs_with_a_bench_drop_not_watson_or_tracy(self, monkeypatch):
        out = self.run(monkeypatch)
        assert out["needs"][0]["position"] == "QB" and out["waiver_rank"] == 12
        assert [(c["add"], c["drop"]) for c in out["claims"]] == [
            ("Jacoby Brissett", "Jerry Jeudy"), ("Carson Wentz", "Jerry Jeudy")]
        assert out["drop_options"][-1]["player"] == "Tyrone Tracy Jr."
        assert out["drop_options"][-1]["hold"]
        assert out["unread"] == {} and out["roster_not_in_pool"] == []
        assert out["basis"] == claims.BASIS

    def test_a_questionable_starter_gets_insurance_claims_not_needs(self, monkeypatch):
        rows = _pool()
        rows[0]["injury_status"] = "QUESTIONABLE"
        out = self.run(monkeypatch, pool_rows=rows)
        assert out["needs"] == [] and out["at_risk"][0]["position"] == "QB"
        assert [(c["add"], c["drop"]) for c in out["insurance"]] == [
            ("Jacoby Brissett", "Jerry Jeudy"), ("Carson Wentz", "Jerry Jeudy")]

    def test_a_scoreboard_on_another_week_is_named_in_unread(self, monkeypatch):
        out = self.run(monkeypatch, scoreboard_week=2)
        assert "scoreboard" in out["unread"]
        assert all(d["hold"] is None for d in out["drop_options"])
