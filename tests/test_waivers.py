"""One fixture week, three players: a breakout, an injury handcuff, a noise mover.

The three exist to be told apart. The breakout's role moved and stayed moved;
the handcuff's role has not moved at all and is worth something only because his
starter is out; the noise mover put up one loud week on unchanged usage, which is
the case a points-based waiver tool gets wrong.
"""
import json
from typing import Any

import numpy as np
import pandas as pd
import pytest

from ffdraft import board as bd
from ffdraft import waivers
from ffdraft.config import LeagueSettings, Scoring

SEASON = 2026
WEEK = 6


def league(**kw: Any) -> LeagueSettings:
    base: dict[str, Any] = {
        "name": "t", "teams": 16, "rounds": 14, "draft_slot": 4,
        "starters": {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 0, "K": 1, "DST": 1},
        "scoring": Scoring(),
    }
    base.update(kw)
    return LeagueSettings(**base)


def weekly():
    """Weeks 1-6 for three players and the team totals they are shares of.

    Breakout: 2 targets a week through week 4, then 9. Handcuff: 3 carries a
    week throughout. Noise: 5 targets a week throughout, but 28 points in week 6.
    """
    rows = []
    for wk in range(1, WEEK + 1):
        late = wk >= WEEK - waivers.RECENT_WEEKS + 1
        rows.append({"player_id": "00-breakout", "player_display_name": "Breakout Guy",
                     "recent_team": "AAA", "week": wk, "targets": 9 if late else 2,
                     "carries": 0, "fantasy_points_ppr": 14.0 if late else 4.0})
        rows.append({"player_id": "00-handcuff", "player_display_name": "Handcuff Guy",
                     "recent_team": "BBB", "week": wk, "targets": 0, "carries": 3,
                     "fantasy_points_ppr": 3.0})
        rows.append({"player_id": "00-noise", "player_display_name": "Noise Guy",
                     "recent_team": "CCC", "week": wk, "targets": 5, "carries": 0,
                     "fantasy_points_ppr": 28.0 if wk == WEEK else 6.0})
        # Team-mates, so every share has a denominator that does not move.
        for team, tg, ca in (("AAA", 30, 20), ("BBB", 30, 20), ("CCC", 30, 20)):
            rows.append({"player_id": f"00-rest-{team}", "player_display_name": f"Rest {team}",
                         "recent_team": team, "week": wk, "targets": tg, "carries": ca,
                         "fantasy_points_ppr": 10.0})
    out = pd.DataFrame(rows)
    out["season"] = SEASON
    out["season_type"] = "REG"
    return out


def snaps():
    rows = []
    for wk in range(1, WEEK + 1):
        late = wk >= WEEK - waivers.RECENT_WEEKS + 1
        rows.append({"player": "Breakout Guy", "week": wk,
                     "offense_pct": 0.75 if late else 0.25})
        rows.append({"player": "Handcuff Guy", "week": wk, "offense_pct": 0.20})
        rows.append({"player": "Noise Guy", "week": wk, "offense_pct": 0.55})
    out = pd.DataFrame(rows)
    out["season"] = SEASON
    out["game_type"] = "REG"
    return out


def changes():
    return waivers.role_change(weekly(), snaps(), SEASON, WEEK).set_index("name")


class TestRoleChange:
    def test_the_breakout_moved_and_the_noise_mover_did_not(self):
        c = changes()
        assert c.loc["Breakout Guy", "role_change"] > 0.3
        # 28 points on unchanged usage is the case a points-based tool buys.
        assert c.loc["Noise Guy", "role_change"] == pytest.approx(0.0, abs=1e-9)
        assert c.loc["Handcuff Guy", "role_change"] == pytest.approx(0.0, abs=1e-9)

    def test_the_breakout_moved_on_every_component(self):
        c = changes().loc["Breakout Guy"]
        assert c["target_share_change"] > 0.15
        assert c["snap_share_change"] == pytest.approx(0.50)
        assert c["carry_share_change"] == pytest.approx(0.0)

    def test_the_noise_mover_scored_without_the_role_moving(self):
        c = changes().loc["Noise Guy"]
        # Points went up, shares did not. Both facts are reported.
        assert c["recent_points"] > c["prior_points"] / waivers.PRIOR_WEEKS
        assert c["target_share_change"] == pytest.approx(0.0)
        assert c["snap_share_change"] == pytest.approx(0.0)

    def test_every_role_change_row_carries_the_measured_result(self):
        """It is measured now, and it went against the score.

        This assertion used to read `== waivers.UNMEASURED` and was true when it
        was written. The backtest is what changed it: a label saying "no evidence
        either way" would now be a false statement about a score whose evidence
        exists and is negative, which is worse than the honest absence it
        replaced.
        """
        ev = changes()["role_change_evidence"]
        assert (ev == waivers.ROLE_CHANGE_EVIDENCE).all()
        assert (ev != waivers.UNMEASURED).all()
        assert "NEGATIVE" in waivers.ROLE_CHANGE_EVIDENCE

    def test_a_player_with_no_prior_window_is_a_new_role_not_a_changed_one(self):
        w = weekly()
        w = w[~((w["player_id"] == "00-breakout") & (w["week"] < WEEK))]
        c = waivers.role_change(w, snaps(), SEASON, WEEK).set_index("name")
        assert c.loc["Breakout Guy", "prior_games"] == 0

    def test_a_player_who_has_not_played_recently_is_absent(self):
        w = weekly()
        w = w[~((w["player_id"] == "00-noise") & (w["week"] >= WEEK - 1))]
        assert "Noise Guy" not in set(waivers.role_change(w, snaps(), SEASON, WEEK)["name"])


class TestProjectionLag:
    def test_usage_ahead_of_the_projection_is_the_buy_case(self):
        out = waivers.projection_lag(pd.Series({"Breakout Guy": 6.0, "Noise Guy": 9.0}),
                                     pd.Series({"Breakout Guy": 14.0, "Noise Guy": 8.0}))
        assert out.loc["Breakout Guy", "projection_lag"] == pytest.approx(8.0)
        assert out.loc["Noise Guy", "projection_lag"] == pytest.approx(-1.0)
        assert (out["projection_lag_evidence"] == waivers.UNMEASURED).all()


class TestContingentValue:
    def board(self):
        return pd.DataFrame({
            "name": ["Starter Back", "Handcuff Guy", "Other Back"],
            "position": ["RB", "RB", "RB"],
            "team": ["BBB", "BBB", "CCC"],
            "proj_points": [250.0, 90.0, 200.0],
            "adj_ppg": [16.0, 6.0, 13.0],
            "exp_games": [14.0, 17.0, 17.0],
            "injury_risk": [0.3, 0.2, 0.2],
        })

    def test_a_handcuff_is_worth_nothing_while_his_starter_plays(self):
        out = waivers.contingent_value(self.board(), out_now=set())
        assert out.loc[1, "contingent_value"] == 0.0
        assert out.loc[1, "contingent_points"] > 0

    def test_the_starter_going_out_switches_the_contingency_on(self):
        out = waivers.contingent_value(self.board(), out_now={"Starter Back"})
        assert out.loc[1, "starter_is_out"]
        assert out.loc[1, "contingent_value"] == out.loc[1, "contingent_points"] > 0
        # The starter himself is nobody's handcuff.
        assert out.loc[0, "contingent_value"] == 0.0

    def test_questionable_is_not_out(self):
        # By Friday it describes half the league.
        assert waivers.starters_out(self.board(), {"Starter Back": "QUESTIONABLE"}) == set()
        assert waivers.starters_out(self.board(), {"Starter Back": "OUT"}) == {"Starter Back"}
        assert waivers.starters_out(self.board(), {"A": "INJURY_RESERVE"}) == {"A"}
        assert waivers.starters_out(self.board(), {"A": None}) == set()

    def test_contingent_value_says_it_is_unmeasured(self):
        out = waivers.contingent_value(self.board(), out_now={"Starter Back"})
        assert (out["contingent_value_evidence"] == waivers.UNMEASURED).all()


class TestLeagueRules:
    def settings(self, **acq):
        base = {"isUsingAcquisitionBudget": False, "acquisitionType": "WAIVERS_TRADITIONAL",
                "acquisitionBudget": 100, "minimumBid": 1}
        base.update(acq)
        return {"acquisitionSettings": base,
                "rosterSettings": {"lineupSlotCounts": {"20": 6}, "isBenchUnlimited": True,
                                   "isUsingUndroppableList": True,
                                   "positionLimits": {"1": 4, "2": 8}}}

    def test_a_populated_budget_does_not_mean_faab_is_on(self):
        # acquisitionBudget 100 and minimumBid 1 sit there inert. Reading them
        # first is how a tool recommends bids to a waiver-order league.
        r = waivers.league_rules_from_settings(self.settings())
        assert r.uses_faab is False
        assert r.budget == 100 and r.minimum_bid == 1
        assert "FAAB is off" in r.priority_basis
        assert waivers.claim_priority(1, r)["faab_bid"] is None

    def test_faab_is_priced_when_the_league_actually_uses_it(self):
        r = waivers.league_rules_from_settings(
            self.settings(isUsingAcquisitionBudget=True, acquisitionType="FAAB"))
        assert r.uses_faab is True
        assert "FAAB bid out of 100" in r.priority_basis
        assert waivers.claim_priority(1, r)["faab_bid"] == 1

    def test_the_bench_is_the_slot_count_not_the_unlimited_flag(self):
        r = waivers.league_rules_from_settings(self.settings())
        assert r.bench_slots == 6
        assert r.uses_undroppable_list is True

    def test_missing_settings_do_not_invent_a_budget(self):
        r = waivers.league_rules_from_settings({})
        assert r.uses_faab is False and r.budget == 0 and r.bench_slots == 0


class TestFreeAgents:
    def payload(self, **over):
        base = {"id": 1, "fullName": "A Player", "defaultPositionId": 3,
                "injuryStatus": "ACTIVE", "droppable": True,
                "ownership": {"percentOwned": 4.2, "percentChange": 1.1,
                              "percentStarted": 0.3}}
        base.update(over)
        return base

    def test_only_unrostered_players_are_in_the_pool(self):
        rows = [{"status": "FREEAGENT", "onTeamId": 0, "player": self.payload(id=1)},
                {"status": "WAIVERS", "onTeamId": 0, "player": self.payload(id=2)},
                {"status": "ONTEAM", "onTeamId": 7, "player": self.payload(id=3)}]
        out = waivers.free_agents(rows)
        assert set(out["espn_id"]) == {1, 2}

    def test_the_ownership_move_is_carried_through(self):
        out = waivers.free_agents(
            [{"status": "FREEAGENT", "onTeamId": 0, "player": self.payload()}])
        assert out.loc[0, "percent_change"] == 1.1
        assert out.loc[0, "percent_owned"] == 4.2

    def test_a_missing_droppable_flag_is_not_droppable_true(self):
        out = waivers.free_agents([{"status": "FREEAGENT", "onTeamId": 0,
                                    "player": self.payload(droppable=None)}])
        assert out.loc[0, "droppable"] is None

    def test_an_empty_payload_is_an_empty_pool(self):
        assert waivers.free_agents([]).empty


class TestATradedPlayerIsOnePlayer:
    """A player traded mid-window used to produce one row per team.

    Found by the milestone-3 backtest rather than by reading: Cam Akers appeared
    twice in one ranked ten. It is not a cosmetic duplicate. `rank_claims` does
    `by_name.loc[name]`, which returns a FRAME for a duplicated label, and
    `float()` of a two-row Series raises -- so `waiver_targets` crashed with a
    traceback the first time a traded player sat on waivers, which is one of the
    commonest ways to end up there. Six players in 2024 week 10 alone.
    """

    def weekly_with_a_trade(self):
        w = weekly()
        # The move must land INSIDE a window, not between two. A trade in the gap
        # gives him one team per window and no duplicate at all: the first
        # version of this fixture moved him at WEEK-1, which is the start of the
        # recent window, and both tests passed against the defect. The control
        # run is what found that, not the tests.
        moved = (w["player_display_name"] == "Noise Guy") & (w["week"] >= WEEK)
        w.loc[moved, "recent_team"] = "DDD"
        # The new team needs team-mates, or his share of it is 1.0 by default.
        extra = []
        for wk in range(1, WEEK + 1):
            extra.append({"player_id": "00-rest-DDD", "player_display_name": "Rest DDD",
                          "recent_team": "DDD", "week": wk, "targets": 30, "carries": 20,
                          "fantasy_points_ppr": 10.0, "season": SEASON,
                          "season_type": "REG"})
        return pd.concat([w, pd.DataFrame(extra)], ignore_index=True)

    def test_a_trade_does_not_split_him_in_two(self):
        c = waivers.role_change(self.weekly_with_a_trade(), snaps(), SEASON, WEEK)
        assert len(c) == c["player_id"].nunique(), "one row per player"
        assert (c["name"] == "Noise Guy").sum() == 1

    def test_rank_claims_does_not_raise_on_him(self):
        """The crash itself, at the surface that crashed."""
        c = waivers.role_change(self.weekly_with_a_trade(), snaps(), SEASON, WEEK)
        pool = pd.DataFrame([{"name": "Noise Guy", "position": "WR",
                              "percent_owned": 1.0, "percent_change": 0.1}])
        bench = pd.DataFrame({"name": ["Spare Guy"], "position": ["WR"],
                              "proj_points": [40.0], "exp_games": [17.0],
                              "injury_risk": [0.2], "bye_week": [np.nan]})
        claims = waivers.rank_claims(pool, c, pd.DataFrame(), league(),
                                     waivers.LeagueRules(), bench, bench, limit=5)
        assert isinstance(claims, list)

    def test_a_week_he_missed_does_not_shrink_his_role(self):
        """The other half of the same fix: the denominator is now the team's
        totals in the weeks he PLAYED, not the whole window. Counting the weeks
        he was out put availability inside a measure of role, which is the one
        thing it must not contain."""
        w = weekly()
        out_week = w[(w["player_display_name"] == "Breakout Guy")
                     & (w["week"] == WEEK - 1)].index
        played = waivers.role_change(w, snaps(), SEASON, WEEK).set_index("name")
        missed = waivers.role_change(w.drop(index=out_week), snaps(), SEASON,
                                     WEEK).set_index("name")
        assert missed.loc["Breakout Guy", "recent_target_share"] == pytest.approx(
            played.loc["Breakout Guy", "recent_target_share"], abs=1e-9)


class TestRankedClaims:
    """The deliverable: the three fixture players ordered, qualified and dropped."""

    def pool(self):
        return pd.DataFrame({
            "name": ["Breakout Guy", "Handcuff Guy", "Noise Guy"],
            "position": ["WR", "RB", "WR"],
            "percent_owned": [3.1, 1.4, 22.0],
            "percent_change": [2.6, 0.1, 4.0],
        })

    def board(self):
        return pd.DataFrame({
            "name": ["Starter Back", "Handcuff Guy"],
            "position": ["RB", "RB"], "team": ["BBB", "BBB"],
            "proj_points": [250.0, 90.0], "adj_ppg": [16.0, 6.0],
            "exp_games": [14.0, 17.0], "injury_risk": [0.3, 0.2],
        })

    def bench(self):
        return pd.DataFrame({
            "name": ["Deep Back"], "position": ["RB"], "proj_points": [90.0],
            "exp_games": [17.0], "bye_week": [np.nan], "droppable": [True]})

    def claims(self, out_now=("Starter Back",)):
        rules = waivers.league_rules_from_settings(
            {"acquisitionSettings": {"isUsingAcquisitionBudget": False,
                                     "acquisitionType": "WAIVERS_TRADITIONAL",
                                     "acquisitionBudget": 100, "minimumBid": 1},
             "rosterSettings": {"lineupSlotCounts": {"20": 6}}})
        return waivers.rank_claims(
            self.pool(), changes().reset_index(),
            waivers.contingent_value(self.board(), set(out_now)),
            league(), rules, mine=None, bench=self.bench())

    def test_the_noise_mover_is_not_a_claim_at_all(self):
        # He scored 34 in the window against the breakout's 28. A points-ranked
        # tool buys him. His role did not move and no starter of his is out, so
        # there is no reason to claim him and he is not in the list. The zero is
        # still inspectable in the role-change frame, which is where the
        # evidence lives.
        assert "Noise Guy" not in [c["player"] for c in self.claims()]
        assert changes().loc["Noise Guy", "role_change"] == pytest.approx(0.0, abs=1e-9)

    def test_a_claim_says_which_of_the_two_reasons_put_it_there(self):
        by_name = {c["player"]: c for c in self.claims()}
        assert by_name["Breakout Guy"]["reason"] == "role moved"
        assert by_name["Handcuff Guy"]["reason"] == "starter out"

    def test_the_handcuff_carries_a_live_contingency_and_names_the_starter(self):
        hc = next(c for c in self.claims() if c["player"] == "Handcuff Guy")
        assert hc["contingent_value"] > 0
        assert hc["handcuff_for"] == "Starter Back"
        # With his starter healthy he has no reason to be claimed either.
        assert "Handcuff Guy" not in [c["player"] for c in self.claims(out_now=())]

    def test_a_role_mover_still_outranks_a_contingency(self):
        # Listed rather than traded off, but role-movers are read first.
        assert self.claims()[0]["player"] == "Breakout Guy"

    def test_every_claim_carries_a_priority_in_this_league_s_units(self):
        for c in self.claims():
            assert c["claim_priority"]["faab_bid"] is None
            assert "FAAB is off" in c["claim_priority"]["basis"]
        assert [c["claim_priority"]["order"] for c in self.claims()] == [1, 2]

    def test_no_role_change_renders_as_negative_zero(self):
        # The share arithmetic leaves -0.0 behind, which reads like a signal.
        for c in self.claims():
            assert not str(c["role_change"]).startswith("-0.0")

    def test_every_claim_names_a_drop(self):
        for c in self.claims():
            assert c["drop"]["player"] == "Deep Back"

    def test_every_claim_says_what_is_measured_and_what_is_not(self):
        c = self.claims()[0]
        assert c["evidence"]["role_change"] == waivers.ROLE_CHANGE_EVIDENCE
        assert c["evidence"]["projection_lag"] == waivers.UNMEASURED
        assert c["evidence"]["contingent_value"] == waivers.UNMEASURED
        assert c["evidence"]["roster_need"] == waivers.UNMEASURED
        # The one score with a real result carries it verbatim.
        assert "0.381/0.529/0.707" in c["evidence"]["role_entropy"]
        assert c["shape"]["free_agent_pool"] == waivers.UNVERIFIED_SHAPE
        assert c["shape"]["ownership_move"] == waivers.UNVERIFIED_SHAPE

    def test_a_player_with_no_weekly_usage_is_not_claimable(self):
        pool = pd.concat([self.pool(), pd.DataFrame(
            [{"name": "Ghost", "position": "WR", "percent_owned": 0.0,
              "percent_change": 0.0}])], ignore_index=True)
        rules = waivers.league_rules_from_settings({})
        out = waivers.rank_claims(pool, changes().reset_index(),
                                  waivers.contingent_value(self.board(), set()),
                                  league(), rules, None, self.bench())
        assert "Ghost" not in [c["player"] for c in out]

    def padded(self, handcuff_last: bool):
        """A real Tuesday pool: two players with a reason and twelve quiet ones.

        The quiet twelve all tie at role_change 0.000, which is what most of a
        free-agent pool looks like. marge's reproduction.
        """
        quiet = pd.DataFrame([{"name": f"Quiet {i:02d}", "position": "WR",
                               "percent_owned": 1.0, "percent_change": 0.0}
                              for i in range(12)])
        base = self.pool()
        base = base[base["name"] != "Noise Guy"]
        first, hc = base.iloc[[0]], base[base["name"] == "Handcuff Guy"]
        pool = (pd.concat([first, quiet, hc], ignore_index=True) if handcuff_last
                else pd.concat([first, hc, quiet], ignore_index=True))
        noise = changes().reset_index()
        noise = noise[noise["name"] == "Noise Guy"]
        ch = pd.concat([changes().reset_index()]
                       + [noise.assign(name=f"Quiet {i:02d}") for i in range(12)],
                       ignore_index=True)
        rules = waivers.league_rules_from_settings({})
        return waivers.rank_claims(
            pool, ch, waivers.contingent_value(self.board(), {"Starter Back"}),
            league(), rules, mine=None, bench=self.bench(), limit=8)

    def test_a_live_contingency_survives_truncation(self):
        # It used to be read after .head(limit), so a handcuff -- whose
        # role_change is 0.000 by construction -- sat in a block of ties with no
        # breaker and was cut before his 30-point contingency was ever looked at.
        for handcuff_last in (False, True):
            names = [c["player"] for c in self.padded(handcuff_last)]
            assert "Handcuff Guy" in names, f"handcuff_last={handcuff_last}"

    def test_the_answer_does_not_depend_on_the_order_of_the_pool(self):
        # quicksort is not stable, so the tie block's order -- and therefore
        # which claims the user saw at all -- was decided by input row order.
        assert [c["player"] for c in self.padded(handcuff_last=False)] == \
            [c["player"] for c in self.padded(handcuff_last=True)]

    def test_players_with_no_reason_do_not_crowd_out_one_with_a_reason(self):
        names = [c["player"] for c in self.padded(handcuff_last=True)]
        assert names[0] == "Breakout Guy"
        assert "Handcuff Guy" in names
        # The quiet twelve have neither a moved role nor a live starter.
        assert not any(n.startswith("Quiet") for n in names)

    def report(self, pool, out_now=("Starter Back",)):
        rules = waivers.league_rules_from_settings({})
        return waivers.waiver_report(
            pool, changes().reset_index(),
            waivers.contingent_value(self.board(), set(out_now)),
            league(), rules, mine=None, bench=self.bench())

    def test_a_quiet_week_and_a_broken_pull_are_told_apart(self):
        # Both return no claims, and they are the two most different answers the
        # tool has: nothing worth claiming, versus the free-agent pull returned
        # nothing usable. The pool's shape is unverified, so a malformed pull is
        # a live possibility and a quiet week is when it would be silent.
        quiet = pd.DataFrame([{"name": f"Quiet {i:02d}", "position": "WR",
                               "percent_owned": 1.0, "percent_change": 0.0}
                              for i in range(12)])
        quiet_week = self.report(quiet, out_now=())
        broken = self.report(pd.DataFrame())
        assert quiet_week["claims"] == [] and broken["claims"] == []
        assert quiet_week["census"]["considered"] == 12
        assert quiet_week["census"]["status"] == "ok"
        assert broken["census"]["considered"] == 0
        assert broken["census"]["status"] == "no free agents in the pool"

    def test_the_census_shows_the_filter_did_work(self):
        # With no-reason players excluded from the list, this is the only place
        # left that shows how many were looked at.
        out = self.report(self.pool())
        assert out["census"]["considered"] == 3
        assert out["census"]["role_moved"] == 1
        assert out["census"]["starter_out"] == 1
        assert out["census"]["claimed"] == 2

    def test_a_pool_with_no_name_column_is_a_broken_pull_not_a_quiet_week(self):
        out = self.report(pd.DataFrame({"espn_id": [1, 2]}))
        assert out["census"]["status"] == "no free agents in the pool"

    def test_an_empty_pool_makes_no_claims(self):
        rules = waivers.league_rules_from_settings({})
        assert waivers.rank_claims(pd.DataFrame(), changes().reset_index(),
                                   pd.DataFrame(), league(), rules, None, None) == []


class TestTheTool:
    """`waiver_targets` on the fixture week, with the network replaced.

    The payload has to survive a strict parser: `json.dumps` writes a float NaN
    as a bare `NaN` literal, Python reads it back, and every conforming client
    rejects it — so the failure is invisible from inside the process and total
    from outside. `_emit` is the single exit that sanitises, and
    `test_json_payloads` keeps a handler from going around it.
    """

    def _reject(self, constant):
        raise AssertionError(f"payload carried a bare {constant}, which is not JSON")

    def wire(self, monkeypatch, pool_rows, board=None, starters=None):
        from ffdraft import server, sources

        players = [{"status": "FREEAGENT", "onTeamId": 0,
                    "player": {"id": i, "fullName": n, "defaultPositionId": p,
                               "injuryStatus": s, "droppable": True,
                               "ownership": {"percentOwned": o, "percentChange": ch,
                                             "percentStarted": 0.1}}}
                   for i, (n, p, s, o, ch) in enumerate(pool_rows)]
        settings = {"acquisitionSettings": {"isUsingAcquisitionBudget": False,
                                            "acquisitionType": "WAIVERS_TRADITIONAL",
                                            "acquisitionBudget": 100, "minimumBid": 1},
                    "rosterSettings": {"lineupSlotCounts": {"20": 6},
                                       "isBenchUnlimited": True,
                                       "isUsingUndroppableList": True,
                                       "positionLimits": {}}}
        monkeypatch.setattr(waivers, "fetch_pool_and_settings",
                            lambda *a, **k: (players, settings))
        monkeypatch.setattr(sources, "weekly_stats", lambda *a, **k: weekly())
        monkeypatch.setattr(sources, "snap_counts", lambda *a, **k: snaps())
        # A board with a hole in the columns the claim rows are built from, so
        # the round trip is exercised rather than asserted.
        if board is None:
            board = pd.DataFrame({
                "name": ["Starter Back", "Handcuff Guy", "Bench Man"],
                "position": ["RB", "RB", "WR"], "team": ["BBB", "BBB", np.nan],
                "proj_points": [250.0, 90.0, 40.0], "adj_ppg": [16.0, 6.0, np.nan],
                "exp_games": [14.0, 17.0, 17.0], "injury_risk": [0.3, 0.2, np.nan],
                "bye_week": [np.nan, 7, np.nan], "draft_score": [90.0, 10.0, np.nan],
                "adp": [20.0, 150.0, np.nan],
            })
        board = board.copy()
        board["_key"] = board["name"].map(bd.norm_name)
        slots = starters or {"QB": 1, "RB": 1, "WR": 0, "TE": 0,
                             "FLEX": 0, "K": 0, "DST": 0}
        monkeypatch.setattr(server, "_build_board", lambda *a, **k: board)
        monkeypatch.setattr(server, "_settings",
                            lambda: (league(starters=slots), None))

        class FakeState:
            def my_rows(self, b):
                return b

        monkeypatch.setattr(server, "_state", lambda: FakeState())
        return server

    def rows(self):
        return [("Breakout Guy", 3, "ACTIVE", 3.1, 2.6),
                ("Handcuff Guy", 2, "ACTIVE", 1.4, 0.1),
                ("Noise Guy", 3, "ACTIVE", 22.0, 4.0),
                ("Starter Back", 2, "OUT", 99.0, -0.2)]

    def test_the_tool_round_trips_strict_json(self, monkeypatch):
        server = self.wire(monkeypatch, self.rows())
        raw = server.waiver_targets("1", WEEK)
        out = json.loads(raw, parse_constant=self._reject)
        assert out["week"] == WEEK
        assert "error" not in out

    def test_the_tool_reports_the_census_and_the_waiver_order(self, monkeypatch):
        server = self.wire(monkeypatch, self.rows())
        out = json.loads(server.waiver_targets("1", WEEK), parse_constant=self._reject)
        assert out["census"]["considered"] == 4
        assert "FAAB is off" in out["claim_priority_basis"]
        assert out["bench_slots"] == 6

    def test_the_tool_carries_the_labels_into_the_payload(self, monkeypatch):
        server = self.wire(monkeypatch, self.rows())
        out = json.loads(server.waiver_targets("1", WEEK), parse_constant=self._reject)
        assert out["claims"], "the fixture week must produce a claim to label"
        ev = out["claims"][0]["evidence"]
        assert ev["role_change"] == waivers.ROLE_CHANGE_EVIDENCE
        assert ev["projection_lag"] == waivers.UNMEASURED
        assert "0.381/0.529/0.707" in ev["role_entropy"]
        assert out["claims"][0]["shape"]["free_agent_pool"] == waivers.UNVERIFIED_SHAPE

    def test_the_round_trip_would_catch_a_regression(self, monkeypatch):
        """The control. Turn the sanitising off and the payload must break.

        A round trip that cannot fail proves nothing. Measured when written: the
        bare NaN is `handcuff_for`, which `rank_claims` fills by `.map` and which
        is missing for every claim that is not a handcuff — so the field is NaN
        on the ordinary case rather than the exotic one.
        """
        server = self.wire(monkeypatch, self.rows())
        monkeypatch.setattr(server, "_emit",
                            lambda payload, **kw: json.dumps(payload, **kw))
        with pytest.raises(AssertionError):
            json.loads(server.waiver_targets("1", WEEK), parse_constant=self._reject)

    def test_the_fixture_week_tells_the_three_apart_through_the_tool(self, monkeypatch):
        """The whole point of the fixture, asserted at the exit rather than inside.

        The breakout is a claim because his role moved, the handcuff because his
        starter is out, and the noise mover -- 28 points on unchanged usage -- is
        not a claim at all. Each reason is named in the row.
        """
        server = self.wire(monkeypatch, self.rows())
        out = json.loads(server.waiver_targets("1", WEEK), parse_constant=self._reject)
        by_name = {c["player"]: c for c in out["claims"]}
        assert "Noise Guy" not in by_name, "a loud week on flat usage is not a claim"
        assert by_name["Breakout Guy"]["reason"] == "role moved"
        assert by_name["Handcuff Guy"]["reason"] == "starter out"
        assert by_name["Handcuff Guy"]["handcuff_for"] == "Starter Back"
        assert out["claims"][0]["player"] == "Breakout Guy"
        assert [c["claim_priority"]["order"] for c in out["claims"]] == [1, 2]
        assert all(c["claim_priority"]["faab_bid"] is None for c in out["claims"])
        assert all(c["drop"]["player"] == "Bench Man" for c in out["claims"])

    def test_positions_come_from_the_board_map_even_when_an_id_is_missing(
            self, monkeypatch):
        """One map, one key type, and a dtype the payload gets to decide.

        The copy that stood in `_waiver_inputs` was int-keyed while the board's
        is string-keyed. That was a maintenance fork, not a wrong answer: it
        computed the same positions, and a change to either could never reach
        the other.

        The dtype hazard belongs to the fix rather than to what it replaced, and
        saying so is the point of this test. Measured: one row without the field
        makes the whole column float64, `.map` with an int-keyed dict still
        resolves `3.0` (Python hashes it equal to `3`), and the string lookup
        does not -- `str(3.0)` is "3.0" and matches nothing, silently, as a blank
        position column rather than a raise. With every id present the column is
        int64 and `str(v)` works, so the broken version ships green and fails on
        the first pull that is missing a field. `int()` is what holds it.
        """
        rows = self.rows() + [("No Position Guy", None, "ACTIVE", 0.4, 0.0)]
        server = self.wire(monkeypatch, rows)
        out = json.loads(server.waiver_targets("1", WEEK), parse_constant=self._reject)
        found = {c["player"]: c["position"] for c in out["claims"]}
        assert found["Breakout Guy"] == "WR"
        assert found["Handcuff Guy"] == "RB"
        assert bd._ESPN_POSITION_NAMES["3"] == "WR", (
            "the board's map is the one source; if its keys stop being strings "
            "the tool's lookup goes silently blank rather than raising")

    def unbalanced(self):
        """A receiver-heavy roster in BOARD order, which is what `my_rows` gives.

        Not a corner case: this is what drafting best-available produces, and it
        is the shape in which "outside my top eight by rank" and "not a starter"
        come apart. The only kicker and the only defense are the two lowest rows
        on it, as they are on every roster ever assembled.
        """
        rows = [("WR One", "WR", 260.0), ("WR Two", "WR", 240.0),
                ("WR Three", "WR", 210.0), ("WR Four", "WR", 195.0),
                ("RB One", "RB", 190.0), ("RB Two", "RB", 175.0),
                ("QB One", "QB", 170.0), ("WR Five", "WR", 160.0),
                ("TE One", "TE", 150.0), ("Kicker One", "K", 130.0),
                ("DST One", "DST", 110.0)]
        board = pd.DataFrame(rows, columns=["name", "position", "proj_points"])
        board["team"] = "AAA"
        board["adj_ppg"] = board["proj_points"] / 17.0
        board["exp_games"] = 17.0
        board["injury_risk"] = 0.2
        board["bye_week"] = np.nan
        board["draft_score"] = board["proj_points"] - 100.0
        board["adp"] = np.arange(1.0, len(board) + 1.0)
        return board

    REAL_STARTERS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 0, "K": 1, "DST": 1}

    def test_the_only_kicker_and_defense_are_never_the_drop(self, monkeypatch):
        """The defect the rank slice shipped, asserted at the tool's exit.

        `mine.iloc[8:]` on board order called TE One, Kicker One and DST One the
        bench, and `drop_candidate` takes the lowest bench value of those three --
        which is a kicker or a defense on any roster, by construction. The tool
        could tell the user to drop their only defense to make room for a claim
        and leave a starting slot nothing can fill.

        Measured on this exact roster before the fix: drop was DST One, bench
        value 110.0. The league starts one of him.
        """
        server = self.wire(monkeypatch, self.rows(), board=self.unbalanced(),
                           starters=self.REAL_STARTERS)
        out = json.loads(server.waiver_targets("1", WEEK), parse_constant=self._reject)
        assert out["claims"], "the fixture week must produce a claim to carry a drop"
        dropped = {c["drop"]["player"] for c in out["claims"]}
        assert not dropped & {"Kicker One", "DST One", "TE One"}, (
            f"offered a player the league starts as the drop: {sorted(dropped)}")
        assert dropped <= {"WR Three", "WR Four", "WR Five"}, (
            f"the drop must come from the receivers nothing starts: {sorted(dropped)}")

    def test_a_drop_is_never_a_player_the_same_row_says_starts(self, monkeypatch):
        """The general property, not this roster's shape.

        `starts_in_a_given_week` and the drop recommendation are computed from the
        same roster and were free to contradict each other: the offered drop came
        back with p_start 1.0, correctly saying he starts every week, in the row
        recommending he be cut. Neither number was wrong; the set they were taken
        over was. This fails on any future bench that admits a starter, whatever
        the position, where the assertion above only catches this one shape.
        """
        server = self.wire(monkeypatch, self.rows(), board=self.unbalanced(),
                           starters=self.REAL_STARTERS)
        out = json.loads(server.waiver_targets("1", WEEK), parse_constant=self._reject)
        for claim in out["claims"]:
            drop = claim["drop"]
            assert drop["starts_in_a_given_week"] < 1.0, (
                f"{drop['player']} is offered as the drop while the same row says "
                f"he starts {drop['starts_in_a_given_week']:.2f} of the time")

    def test_a_roster_row_with_no_position_is_named_not_cut(self, monkeypatch):
        """A row the lineup cannot place is not therefore spare.

        It matches no slot, so it can never be a starter, so a bench taken as the
        complement of the starters swallows it -- and it may be the only kicker on
        the roster with a broken board row. It is reported instead.
        """
        board = self.unbalanced()
        board.loc[board["name"] == "Kicker One", "position"] = np.nan
        server = self.wire(monkeypatch, self.rows(), board=board,
                           starters=self.REAL_STARTERS)
        out = json.loads(server.waiver_targets("1", WEEK), parse_constant=self._reject)
        assert out["unplaceable_on_my_roster"] == ["Kicker One"]
        assert all(c["drop"]["player"] != "Kicker One" for c in out["claims"])

    def test_an_empty_pool_still_round_trips_and_says_it_is_broken(self, monkeypatch):
        server = self.wire(monkeypatch, [])
        out = json.loads(server.waiver_targets("1", WEEK), parse_constant=self._reject)
        assert out["claims"] == []
        assert out["census"]["status"] == "no free agents in the pool"

    def test_a_failed_pull_is_an_error_payload_not_a_traceback(self, monkeypatch):
        from ffdraft import server

        monkeypatch.setattr(server, "_waiver_inputs",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        out = json.loads(server.waiver_targets("1", WEEK), parse_constant=self._reject)
        assert "could not assemble the waiver inputs" in out["error"]
        assert "boom" in out["error"]


class TestDropCandidate:
    def bench(self, **over):
        b = pd.DataFrame({
            "name": ["Deep Back", "Real Starter", "Locked Man"],
            "position": ["RB", "WR", "RB"],
            "proj_points": [90.0, 240.0, 150.0],
            "exp_games": [17.0, 17.0, 17.0],
            "bye_week": [np.nan, np.nan, np.nan],
            "droppable": [True, True, True],
        })
        for k, v in over.items():
            b[k] = v
        return b

    def held(self):
        return pd.DataFrame({"name": ["RB1", "RB2"], "position": ["RB", "RB"],
                             "proj_points": [300.0, 280.0], "exp_games": [17.0, 17.0],
                             "bye_week": [np.nan, np.nan]})

    def test_the_drop_is_the_lowest_bench_value_not_the_lowest_projection(self):
        out = waivers.drop_candidate(self.bench(), league(), self.held())
        assert out["player"] == "Deep Back"
        assert out["undroppable_checked"] is True

    def test_a_player_the_league_forbids_is_never_offered(self):
        out = waivers.drop_candidate(self.bench(droppable=[False, True, True]),
                                     league(), self.held())
        assert out["player"] != "Deep Back"

    def test_an_all_undroppable_bench_says_so_instead_of_naming_someone(self):
        out = waivers.drop_candidate(self.bench(droppable=False), league(), self.held())
        assert out["player"] is None
        assert "undroppable" in out["reason"]

    def test_a_player_the_pull_did_not_carry_is_offered_but_flagged(self):
        out = waivers.drop_candidate(self.bench(droppable=[None, True, True]),
                                     league(), self.held())
        assert out["player"] == "Deep Back"
        assert out["undroppable_checked"] is False
        assert "unchecked" in out["reason"]

    def unpriced_bench(self, deep_projection: float):
        """The shape `my_rows` returns once #40 lands: a stand-in for a roster
        player the board cannot price, at the position's replacement level with
        vor 0, no bye_week and no exp_games."""
        return pd.DataFrame({
            "name": ["Deep Back", "Real Starter", "Unpriced Man"],
            "position": ["RB", "WR", "RB"],
            "proj_points": [deep_projection, 240.0, 120.0],
            "exp_games": [17.0, 17.0, np.nan],
            "bye_week": [np.nan, np.nan, np.nan],
            "vor": [0.0, 80.0, 0.0],
            "droppable": [True, True, True],
            "unpriced": [False, False, True],
        })

    def test_a_drop_resting_on_a_stand_in_says_so(self):
        # Every real bench player above replacement, so the stand-in is cheapest.
        out = waivers.drop_candidate(self.unpriced_bench(180.0), league(), self.held())
        assert out["player"] == "Unpriced Man"
        assert out["projection_basis"] == "replacement-level stand-in"
        assert "cannot price this player" in out["reason"]

    def test_a_stand_in_is_not_automatically_the_cheapest_drop(self):
        # Replacement level is above a genuinely deep bench player, so a real
        # one at 90 still beats a stand-in at 120.
        out = waivers.drop_candidate(self.unpriced_bench(90.0), league(), self.held())
        assert out["player"] == "Deep Back"
        assert out["projection_basis"] == "board projection"

    def test_a_bench_from_mixed_sources_does_not_invent_a_stand_in(self):
        # Concatenating a frame that carries the flag with one that does not
        # gives object dtype and NaN in the rows that lacked it. NaN is truthy,
        # so bool() labelled a real board-priced player a stand-in -- telling
        # the user his figure is not a projection of his own when it is.
        flagged = self.unpriced_bench(180.0).iloc[[0]]
        unflagged = pd.DataFrame({
            "name": ["From Another Source"], "position": ["RB"],
            "proj_points": [40.0], "exp_games": [17.0], "bye_week": [np.nan],
            "droppable": [True]})
        mixed = pd.concat([flagged, unflagged], ignore_index=True)
        assert mixed["unpriced"].isna().any(), "the fixture must reproduce the NaN"
        out = waivers.drop_candidate(mixed, league(), self.held())
        assert out["player"] == "From Another Source"
        assert out["projection_basis"] == "board projection"
        assert "cannot price this player" not in out["reason"]

    def test_a_board_that_prices_everyone_has_no_stand_ins(self):
        out = waivers.drop_candidate(self.bench(), league(), self.held())
        assert out["projection_basis"] == "board projection"

    def test_an_empty_bench_names_nobody(self):
        assert waivers.drop_candidate(pd.DataFrame(), league())["player"] is None
