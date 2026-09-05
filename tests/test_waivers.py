"""One fixture week, three players: a breakout, an injury handcuff, a noise mover.

The three exist to be told apart. The breakout's role moved and stayed moved;
the handcuff's role has not moved at all and is worth something only because his
starter is out; the noise mover put up one loud week on unchanged usage, which is
the case a points-based waiver tool gets wrong.
"""
from typing import Any

import numpy as np
import pandas as pd
import pytest

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

    def test_every_role_change_row_says_it_is_unmeasured(self):
        assert (changes()["role_change_evidence"] == waivers.UNMEASURED).all()

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

    def test_the_breakout_outranks_the_noise_mover(self):
        # Noise Guy scored 34 in the window against Breakout Guy's 28. A tool
        # ranking on points buys him; this one ranks on the role having moved.
        names = [c["player"] for c in self.claims()]
        assert names[0] == "Breakout Guy"
        assert names.index("Breakout Guy") < names.index("Noise Guy")

    def test_the_noise_mover_is_reported_with_a_zero_role_change(self):
        noise = next(c for c in self.claims() if c["player"] == "Noise Guy")
        assert noise["role_change"] == 0.0
        assert noise["snap_share_change"] == 0.0

    def test_the_handcuff_carries_a_live_contingency_and_names_the_starter(self):
        hc = next(c for c in self.claims() if c["player"] == "Handcuff Guy")
        assert hc["contingent_value"] > 0
        assert hc["handcuff_for"] == "Starter Back"
        # And nothing at all while the starter plays.
        healthy = next(c for c in self.claims(out_now=()) if c["player"] == "Handcuff Guy")
        assert healthy["contingent_value"] == 0.0
        assert healthy["handcuff_for"] is None

    def test_the_contingency_does_not_reorder_the_list(self):
        # It qualifies a claim; it does not outrank a role that actually moved.
        assert self.claims()[0]["player"] == "Breakout Guy"

    def test_every_claim_carries_a_priority_in_this_league_s_units(self):
        for c in self.claims():
            assert c["claim_priority"]["faab_bid"] is None
            assert "FAAB is off" in c["claim_priority"]["basis"]
        assert [c["claim_priority"]["order"] for c in self.claims()] == [1, 2, 3]

    def test_every_claim_names_a_drop(self):
        for c in self.claims():
            assert c["drop"]["player"] == "Deep Back"

    def test_every_claim_says_what_is_measured_and_what_is_not(self):
        c = self.claims()[0]
        assert c["evidence"]["role_change"] == waivers.UNMEASURED
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

    def test_an_empty_pool_makes_no_claims(self):
        rules = waivers.league_rules_from_settings({})
        assert waivers.rank_claims(pd.DataFrame(), changes().reset_index(),
                                   pd.DataFrame(), league(), rules, None, None) == []


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

    def test_an_empty_bench_names_nobody(self):
        assert waivers.drop_candidate(pd.DataFrame(), league())["player"] is None
