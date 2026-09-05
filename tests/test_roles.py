from typing import Any

import numpy as np
import pandas as pd
import pytest

from ffdraft import model, roles
from ffdraft.config import LeagueSettings, Scoring


def league(**kw: Any) -> LeagueSettings:
    """The user's league: 16 teams, no FLEX, so a bench player starts only when
    the men ahead of him at his own position are out."""
    base: dict[str, Any] = {
        "name": "t", "teams": 16, "rounds": 14, "draft_slot": 4,
        "starters": {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 0, "K": 1, "DST": 1},
        "scoring": Scoring(),
    }
    base.update(kw)
    return LeagueSettings(**base)


def board(rows):
    df = pd.DataFrame(rows)
    for col, default in (("adj_ppg", 10.0), ("exp_games", 17.0), ("injury_risk", 0.2),
                         ("bye_week", np.nan), ("draft_score", 50.0), ("adp", 50.0),
                         ("drafted", False)):
        if col not in df.columns:
            df[col] = default
    return df


class TestStartProbability:
    def test_a_man_with_an_open_slot_ahead_of_him_always_starts(self):
        assert roles.start_probability([], slots=2) == 1.0
        assert roles.start_probability([(17.0, None)], slots=2) == 1.0

    def test_a_position_with_no_slots_never_starts_anyone(self):
        assert roles.start_probability([], slots=0) == 0.0

    def test_the_floor_applies_to_the_series_not_the_raw_probability(self):
        # start_probability answers the modelled question exactly, including 0.
        assert roles.start_probability([(17.0, None), (17.0, None)], slots=2) == 0.0
        # start_probabilities is what reaches a pick_value, and it never claims
        # a certainty the model cannot have: a slot also opens through a trade,
        # a cut or a benching, none of which are modelled.
        avail = board([{"name": "RB4", "position": "RB", "proj_points": 50.0}])
        mine = board([{"name": "a", "position": "RB", "proj_points": 300.0},
                      {"name": "b", "position": "RB", "proj_points": 250.0}])
        assert roles.start_probabilities(avail, league(), mine).iloc[0] == \
            roles.START_PROB_FLOOR

    def test_a_third_rb_starts_only_when_one_of_the_two_ahead_is_out(self):
        # Both ahead available every week: the RB3 never starts.
        assert roles.start_probability([(17.0, None), (17.0, None)], slots=2) == 0.0
        # One of them out every week: he always starts.
        assert roles.start_probability([(17.0, None), (0.0, None)], slots=2) == 1.0

    def test_injury_risk_alone_gives_the_backup_his_weeks(self):
        # Two starters at 15 of 17 games: P(both available) = (15/17)^2, so the
        # RB3 starts the rest.
        p = roles.start_probability([(15.0, None), (15.0, None)], slots=2)
        assert p == pytest.approx(1 - (15 / 17) ** 2)

    def test_a_bye_ahead_of_him_is_a_week_he_starts(self):
        # Perfectly healthy starters, one on bye in week 3 of 14.
        p = roles.start_probability([(17.0, None), (17.0, 3.0)], slots=2,
                                    weeks=roles.FANTASY_WEEKS)
        assert p == pytest.approx(1 / roles.FANTASY_WEEKS)

    def test_two_starters_sharing_a_bye_is_the_same_week_not_two(self):
        stacked = roles.start_probability([(17.0, 3.0), (17.0, 3.0)], slots=2)
        split = roles.start_probability([(17.0, 3.0), (17.0, 4.0)], slots=2)
        assert stacked == pytest.approx(1 / roles.FANTASY_WEEKS)
        assert split == pytest.approx(2 / roles.FANTASY_WEEKS)
        assert split > stacked

    def test_the_poisson_binomial_matches_a_hand_computed_case(self):
        # Three men ahead of a TE2 in a one-slot position: he starts when all
        # three are out, which for a one-slot position means "fewer than 1
        # available".
        assert roles._fewer_than([0.5, 0.5, 0.5], 1) == pytest.approx(0.125)
        # Fewer than 2 of three coin flips available: none or exactly one.
        assert roles._fewer_than([0.5, 0.5, 0.5], 2) == pytest.approx(0.5)

    def test_slots_count_superflex_for_quarterbacks_and_never_flex(self):
        assert roles.position_slots(league())["QB"] == 1
        assert roles.position_slots(league(superflex=1))["QB"] == 2
        # A FLEX slot is not counted, so start probability is a lower bound.
        flexed = league(starters={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1,
                                  "K": 1, "DST": 1})
        assert roles.position_slots(flexed)["RB"] == 2


class TestBenchValue:
    def test_bench_value_is_start_probability_times_points(self):
        avail = board([{"name": "RB3", "position": "RB", "proj_points": 150.0}])
        mine = board([{"name": "RB1", "position": "RB", "proj_points": 300.0,
                       "exp_games": 17.0},
                      {"name": "RB2", "position": "RB", "proj_points": 250.0,
                       "exp_games": 17.0}])
        out = roles.bench_values(avail, league(), mine)
        # Two full-time starters ahead of him at a two-slot position, so the
        # modelled paths give him nothing and he is left at the floor.
        assert out["p_start"].iloc[0] == roles.START_PROB_FLOOR
        assert out["bench_value"].iloc[0] == pytest.approx(150.0 * roles.START_PROB_FLOOR)

    def test_an_empty_roster_leaves_a_candidate_at_his_full_value(self):
        avail = board([{"name": "RB1", "position": "RB", "proj_points": 150.0}])
        out = roles.bench_values(avail, league(), None)
        assert out["p_start"].iloc[0] == 1.0
        assert out["bench_value"].iloc[0] == 150.0

    def test_only_the_men_who_project_above_him_count_as_ahead(self):
        avail = board([{"name": "RB new", "position": "RB", "proj_points": 280.0}])
        mine = board([{"name": "RB1", "position": "RB", "proj_points": 300.0},
                      {"name": "RB2", "position": "RB", "proj_points": 100.0}])
        # Only one man projects above him, and the position starts two, so he is
        # a starter, not a bench body.
        assert roles.bench_values(avail, league(), mine)["p_start"].iloc[0] == 1.0

    def test_a_different_position_on_the_roster_is_irrelevant(self):
        avail = board([{"name": "WR3", "position": "WR", "proj_points": 150.0}])
        mine = board([{"name": "RB1", "position": "RB", "proj_points": 300.0},
                      {"name": "RB2", "position": "RB", "proj_points": 250.0}])
        assert roles.bench_values(avail, league(), mine)["p_start"].iloc[0] == 1.0


class TestHandcuffs:
    def depth(self):
        return board([
            {"name": "Starter", "position": "RB", "team": "SF", "proj_points": 250.0,
             "adj_ppg": 16.0, "exp_games": 14.0, "injury_risk": 0.3},
            {"name": "Backup", "position": "RB", "team": "SF", "proj_points": 90.0,
             "adj_ppg": 6.0, "exp_games": 17.0},
            {"name": "Third", "position": "RB", "team": "SF", "proj_points": 20.0,
             "adj_ppg": 1.0, "exp_games": 17.0},
            {"name": "Elsewhere", "position": "RB", "team": "KC", "proj_points": 200.0,
             "adj_ppg": 13.0, "exp_games": 17.0},
        ])

    def test_the_starter_is_insured_against_not_insured(self):
        hc = roles.handcuff_table(self.depth())
        assert pd.isna(hc.loc[0, "starter"])
        assert hc.loc[0, "contingent_points"] == 0.0
        assert hc.loc[0, "ev_handcuff"] == 250.0

    def test_the_direct_backup_inherits_the_games_the_starter_misses(self):
        hc = roles.handcuff_table(self.depth())
        # Starter plays 14 of 17, so 3 games are vacated at a 10 ppg upgrade.
        assert hc.loc[1, "starter"] == "Starter"
        assert hc.loc[1, "starter_games_missed"] == 3.0
        assert hc.loc[1, "contingent_points"] == 30.0
        assert hc.loc[1, "ev_handcuff"] == 120.0
        assert hc.loc[1, "starter_injury_risk"] == 0.3

    def test_third_on_the_depth_chart_has_no_contingent_value(self):
        # Otherwise the term rewards whoever is worst: the gap to the starter is
        # widest for the man least likely to inherit anything.
        hc = roles.handcuff_table(self.depth())
        assert hc.loc[2, "depth_rank"] == 3
        assert hc.loc[2, "contingent_points"] == 0.0
        assert hc.loc[2, "ev_handcuff"] == 20.0

    def test_a_lone_player_at_his_team_and_position_is_the_starter(self):
        hc = roles.handcuff_table(self.depth())
        assert hc.loc[3, "depth_rank"] == 1
        assert hc.loc[3, "contingent_points"] == 0.0

    def test_a_board_without_the_columns_yields_an_empty_frame(self):
        hc = roles.handcuff_table(pd.DataFrame({"name": ["x"]}))
        assert list(hc.index) == [0]
        assert pd.isna(hc.loc[0, "contingent_points"])


class TestTheModelHook:
    def avail(self):
        return board([
            {"name": "RB1", "position": "RB", "team": "SF", "proj_points": 250.0,
             "adj_ppg": 16.0, "exp_games": 14.0, "draft_score": 90.0},
            {"name": "RB2", "position": "RB", "team": "SF", "proj_points": 90.0,
             "adj_ppg": 6.0, "draft_score": 10.0},
            {"name": "WR1", "position": "WR", "team": "KC", "proj_points": 200.0,
             "adj_ppg": 13.0, "draft_score": 70.0},
        ])

    def test_zero_weights_leave_the_recommendation_bit_identical(self):
        a = model.recommend(self.avail(), league(), current_pick=1, next_pick=32, top_n=5)
        b = model.recommend(self.avail(), league(), current_pick=1, next_pick=32, top_n=5,
                            role_weights={"start_prob": 0.0, "handcuff": 0.0})
        c = model.recommend(self.avail(), league(), current_pick=1, next_pick=32, top_n=5,
                            role_weights=None)
        assert a["name"].tolist() == b["name"].tolist() == c["name"].tolist()
        assert np.allclose(a["pick_value"], b["pick_value"])
        assert np.allclose(a["pick_value"], c["pick_value"])

    def test_a_start_probability_of_zero_is_bounded_not_infinite(self):
        # A bench player's pick_value is negative and p_start can be exactly 0.
        # The reflection sends him to twice his own magnitude — as far down as
        # the rule allows — rather than dividing by an epsilon, which would
        # inflate one pick enough to dominate any sum built from pick_value.
        values = pd.Series([-4.0, 4.0])
        out = model._discount(values, pd.Series([0.0, 0.0]))
        assert list(out) == [-8.0, 0.0]
        assert np.isfinite(out).all()
        # And the multiplier is monotone: a smaller one is never a promotion.
        worse = model._discount(pd.Series([-4.0]), pd.Series([0.25]))
        better = model._discount(pd.Series([-4.0]), pd.Series([0.75]))
        assert worse[0] < better[0] < 0

    def test_the_start_probability_term_is_additive_in_draft_score(self):
        # (m - 1) * draft_score * need is exactly the difference between
        # recommend's pick_value and the same arithmetic on m * draft_score, so
        # the points-side scaling is available without a multiplier on a value
        # whose zero is not "worthless".
        avail = board([{"name": "RB3", "position": "RB", "proj_points": 150.0,
                        "draft_score": 40.0, "need_mult": 0.5}])
        mine = board([{"name": "a", "position": "RB", "proj_points": 300.0},
                      {"name": "b", "position": "RB", "proj_points": 250.0}])
        adj = roles.start_prob_adjustment(avail, league(), mine, start_prob_weight=1.0)
        assert adj.iloc[0] == pytest.approx((roles.START_PROB_FLOOR - 1) * 40.0 * 0.5)
        # At weight 0 it is zero to the bit.
        assert roles.start_prob_adjustment(avail, league(), mine,
                                           start_prob_weight=0.0).iloc[0] == 0.0

    def test_a_player_below_replacement_is_not_lifted_by_playing_less(self):
        # draft_score is value over replacement, not points, so m * draft_score
        # is only the right statement while it is positive. Unclipped, players
        # below replacement rose toward zero and a receiver whose own value had
        # not changed fell from rank 1 to rank 13.
        avail = board([{"name": "bad", "position": "RB", "proj_points": 20.0,
                        "draft_score": -30.0, "need_mult": 1.0}])
        mine = board([{"name": "a", "position": "RB", "proj_points": 300.0},
                      {"name": "b", "position": "RB", "proj_points": 250.0}])
        assert roles.start_prob_adjustment(avail, league(), mine,
                                           start_prob_weight=1.0).iloc[0] == 0.0

    def test_a_candidate_who_can_hardly_start_falls_below_the_negative_field(self):
        # The trap: pick_value's zero means "as good as waiting", and almost the
        # whole board sits below it, so a multiplier that scales a positive
        # pick_value toward zero leaves it above everything negative. Measured on
        # the live board at pick 125 with two ironman backs held: Woody Marks
        # went from rank 3 of 575 to rank 204.
        rows = [{"name": "bench RB", "position": "RB", "proj_points": 150.0,
                 "draft_score": 60.0, "adp": 130.0}]
        rows += [{"name": f"filler{i}", "position": "WR", "proj_points": 40.0,
                  "draft_score": -20.0 - i, "adp": 140.0 + i} for i in range(8)]
        avail = board(rows)
        mine = board([{"name": "a", "position": "RB", "proj_points": 400.0,
                       "exp_games": 17.0},
                      {"name": "b", "position": "RB", "proj_points": 380.0,
                       "exp_games": 17.0}])
        before = model.recommend(avail, league(), current_pick=125, next_pick=132,
                                 roster={"RB": 2}, mine=mine, top_n=len(avail))
        after = model.recommend(avail, league(), current_pick=125, next_pick=132,
                                roster={"RB": 2}, mine=mine, top_n=len(avail),
                                role_weights={"start_prob": 1.0})
        assert (before["pick_value"] < 0).sum() >= len(avail) - 2
        assert before["name"].iloc[0] == "bench RB"
        assert after["name"].iloc[-1] == "bench RB"

    def test_the_start_probability_weight_only_scales_down(self):
        mine = board([{"name": "held1", "position": "RB", "proj_points": 400.0},
                      {"name": "held2", "position": "RB", "proj_points": 380.0}])
        mult = roles.pick_value_multiplier(self.avail(), league(), mine,
                                           start_prob_weight=1.0)
        # Both running backs sit behind two full-time starters; the receiver does not.
        assert mult.iloc[0] < 1.0 and mult.iloc[1] < 1.0
        assert mult.iloc[2] == 1.0
        assert (mult <= 1.0).all()

    def test_the_handcuff_bonus_is_added_not_multiplied(self):
        # A deep bench player's pick_value is negative; multiplying by his
        # handcuff case would push him further down, which is backwards.
        avail = roles.attach_handcuffs(self.avail())
        bonus = roles.pick_value_bonus(avail, league(), None, handcuff_weight=1.0)
        assert bonus.iloc[0] == 0.0
        assert bonus.iloc[1] == pytest.approx(30.0)
        assert (bonus >= 0).all()

    def test_holding_the_starter_doubles_the_contingency(self):
        avail = roles.attach_handcuffs(self.avail())
        mine = board([{"name": "RB1", "position": "RB", "proj_points": 250.0}])
        alone = roles.pick_value_bonus(avail, league(), None, handcuff_weight=1.0)
        holding = roles.pick_value_bonus(avail, league(), mine, handcuff_weight=1.0)
        assert holding.iloc[1] == pytest.approx(alone.iloc[1] * (1 + roles.HANDCUFF_HELD_BONUS))

    def test_contingent_points_a_roster_can_never_start_are_not_points(self):
        # Without the gate the term is largest for a backup quarterback — his
        # starter has the best per-game output on the board — and it lands after
        # need_mult has already discounted him, walking past the rule that stops
        # the model rostering a second QB. A run at weight 1 drafted Cooper Rush,
        # Gardner Minshew, Michael Pratt and Tyler Huntley.
        avail = roles.attach_handcuffs(board([
            {"name": "QB1", "position": "QB", "team": "DAL", "proj_points": 340.0,
             "adj_ppg": 22.0, "exp_games": 15.0},
            {"name": "QB2", "position": "QB", "team": "DAL", "proj_points": 90.0,
             "adj_ppg": 6.0},
        ]))
        mine = board([{"name": "my QB", "position": "QB", "proj_points": 320.0,
                       "exp_games": 17.0}])
        assert avail.loc[1, "contingent_points"] == pytest.approx(32.0)
        # One quarterback held, one starting slot: the backup almost never plays.
        gated = roles.pick_value_bonus(avail, league(), mine, handcuff_weight=1.0)
        assert gated.iloc[1] < 0.15 * avail.loc[1, "contingent_points"]
        # With the slot open he keeps the whole contingency.
        assert roles.pick_value_bonus(avail, league(), None, handcuff_weight=1.0).iloc[1] \
            == pytest.approx(32.0)

    def test_the_gate_does_not_apply_to_a_starter_i_already_hold(self):
        # His absence is what pays the contingency AND what opens the lineup
        # slot: one event, not two. Gating on the slot squared a probability
        # contingent_points had already applied, which made holding the starter
        # LOWER his handcuff's value than not holding him.
        avail = roles.attach_handcuffs(board([
            {"name": "RB1", "position": "RB", "team": "SF", "proj_points": 250.0,
             "adj_ppg": 16.0, "exp_games": 14.0},
            {"name": "RB2", "position": "RB", "team": "SF", "proj_points": 90.0,
             "adj_ppg": 6.0},
        ]))
        # A full RB group, so a bench back's start probability is well under 1.
        full = board([{"name": "RB1", "position": "RB", "proj_points": 250.0,
                       "exp_games": 14.0},
                      {"name": "other", "position": "RB", "proj_points": 240.0,
                       "exp_games": 14.0}])
        assert roles.start_probabilities(avail, league(), full).iloc[1] < 0.5
        held = roles.pick_value_bonus(avail, league(), full, handcuff_weight=1.0)
        # 30 contingent points, ungated because RB1 is mine, doubled for holding him.
        assert held.iloc[1] == pytest.approx(60.0)
        # Not holding him: the two events are independent and the gate applies.
        others = board([{"name": "x", "position": "RB", "proj_points": 250.0,
                         "exp_games": 14.0},
                        {"name": "y", "position": "RB", "proj_points": 240.0,
                         "exp_games": 14.0}])
        unheld = roles.pick_value_bonus(avail, league(), others, handcuff_weight=1.0)
        assert 0 < unheld.iloc[1] < 30.0
        assert held.iloc[1] > unheld.iloc[1]

    def test_the_weights_reach_pick_value_through_recommend(self):
        avail = roles.attach_handcuffs(self.avail())
        off = model.recommend(avail, league(), current_pick=1, next_pick=32, top_n=5)
        on = model.recommend(avail, league(), current_pick=1, next_pick=32, top_n=5,
                             role_weights={"handcuff": 1.0})
        by_name = {r["name"]: r["pick_value"] for _, r in on.iterrows()}
        was = {r["name"]: r["pick_value"] for _, r in off.iterrows()}
        assert by_name["RB2"] == pytest.approx(was["RB2"] + 30.0)
        assert by_name["WR1"] == pytest.approx(was["WR1"])


class TestRoleEntropy:
    def frame(self, **kw):
        # "Settled" agrees to within a rounding error rather than exactly: two
        # bit-identical projections are one number, not two that agree.
        rows = {"name": ["Settled", "Disputed", "Unknown"],
                "proj_points": [200.0, 200.0, 200.0],
                "espn_proj": [199.9999, 100.0, np.nan]}
        rows.update(kw)
        return pd.DataFrame(rows)

    def churn(self, values):
        return pd.DataFrame({"player": ["Settled", "Disputed", "Unknown"],
                             "role_churn": values})

    def test_agreement_scores_zero_and_a_factor_of_two_scores_one(self):
        out = roles.role_entropy(self.frame(), self.churn([np.nan] * 3))
        assert out.loc[0, "role_entropy"] == pytest.approx(0.0, abs=1e-5)
        # ESPN at half the model is exactly the full-disagreement scale.
        assert out.loc[1, "role_entropy"] == pytest.approx(1.0)

    def test_disagreement_is_symmetric(self):
        half = roles.role_entropy(self.frame(espn_proj=[200.0, 100.0, np.nan]),
                                  self.churn([np.nan] * 3))
        double = roles.role_entropy(self.frame(espn_proj=[200.0, 400.0, np.nan]),
                                    self.churn([np.nan] * 3))
        assert half.loc[1, "proj_disagreement"] == pytest.approx(
            double.loc[1, "proj_disagreement"])

    def test_the_two_parts_are_averaged_and_a_player_with_neither_is_nan(self):
        out = roles.role_entropy(self.frame(), self.churn([1.0, np.nan, np.nan]))
        # Settled: no disagreement, full churn -> the mean of 0 and 1.
        assert out.loc[0, "role_entropy"] == pytest.approx(0.5)
        assert pd.isna(out.loc[2, "role_entropy"])

    def test_the_direction_of_the_disagreement_is_named(self):
        out = roles.role_entropy(self.frame(espn_proj=[200.0, 100.0, 400.0]),
                                 self.churn([np.nan] * 3))
        # The brief's split: uncertain because upside has not resolved is not
        # the same bet as uncertain because the player barely has a job.
        assert out.loc[0, "entropy_kind"] == ""
        assert out.loc[1, "entropy_kind"] == "role in doubt"
        assert out.loc[2, "entropy_kind"] == "unresolved upside"

    def test_a_projection_copied_from_espn_is_not_agreement(self):
        # A kicker or a team defense is priced from ESPN's own projection, so
        # its ratio is exactly 1 by construction. Scored as agreement it becomes
        # the most certain role on the board.
        b = pd.DataFrame({"name": ["HOU D/ST", "real WR"],
                          "proj_points": [129.1, 200.0], "espn_proj": [129.1, 199.0]})
        out = roles.role_entropy(b, self.churn([np.nan, np.nan, np.nan]))
        assert pd.isna(out.loc[0, "proj_disagreement"])
        assert pd.isna(out.loc[0, "role_entropy"])
        assert out.loc[0, "entropy_basis"] == ""
        # A real pair of estimates that nearly agree still scores.
        assert out.loc[1, "proj_disagreement"] > 0

    def test_the_score_says_which_halves_it_rests_on(self):
        # Only the churn half has been tested against real projection error, so
        # a blended number that does not name its halves lets the untested one
        # borrow the tested one's credibility.
        out = roles.role_entropy(self.frame(), self.churn([1.0, np.nan, 0.5]))
        assert out.loc[0, "entropy_basis"] == roles.ENTROPY_BASIS_BOTH
        assert out.loc[1, "entropy_basis"] == roles.ENTROPY_BASIS_DISAGREEMENT
        assert out.loc[2, "entropy_basis"] == roles.ENTROPY_BASIS_CHURN
        # Nothing to score, nothing to name.
        assert roles.role_entropy(self.frame(), self.churn([np.nan] * 3)).loc[
            2, "entropy_basis"] == ""

    def test_explain_names_a_one_sided_basis_but_not_a_two_sided_one(self):
        row = pd.Series({"name": "x", "position": "WR", "pos_rank": 3,
                         "proj_points": 200.0, "adj_ppg": 12.0, "role_entropy": 0.5,
                         "entropy_basis": roles.ENTROPY_BASIS_CHURN})
        assert roles.ENTROPY_BASIS_CHURN in model.explain(row)
        row["entropy_basis"] = roles.ENTROPY_BASIS_BOTH
        assert roles.ENTROPY_BASIS_BOTH not in model.explain(row)

    def test_explain_surfaces_entropy_and_the_named_shares(self):
        row = pd.Series({"name": "x", "position": "WR", "pos_rank": 3,
                         "proj_points": 200.0, "adj_ppg": 12.0, "role_entropy": 0.8,
                         "entropy_kind": "role in doubt", "role_churn": 0.9,
                         "target_share": 0.21, "carry_share": 0.0,
                         "redzone_share": 0.11, "snap_share": 0.85})
        text = model.explain(row)
        assert "role uncertainty 0.80" in text
        assert "role in doubt" in text
        assert "snap share moved week to week" in text
        assert "share of team: targets 21%, carries 0%, red zone 11%, snaps 85%" in text

    def test_explain_says_nothing_when_the_columns_are_absent(self):
        row = pd.Series({"name": "x", "position": "WR", "pos_rank": 3,
                         "proj_points": 200.0, "adj_ppg": 12.0})
        text = model.explain(row)
        assert "role uncertainty" not in text
        assert "share of team" not in text

    def test_explain_never_prints_a_nan(self):
        # A kicker or defense has no per-player history at all: every share is
        # null and so is the entropy label. NaN is truthy, which is how `explain`
        # once announced "ESPN status nan" on every defense.
        row = pd.Series({"name": "SF D/ST", "position": "DST", "pos_rank": 1,
                         "proj_points": 120.0, "adj_ppg": 8.0,
                         "role_entropy": 0.3, "entropy_kind": np.nan,
                         "role_churn": np.nan, "target_share": np.nan,
                         "carry_share": np.nan, "redzone_share": np.nan,
                         "snap_share": np.nan})
        text = model.explain(row)
        assert "nan" not in text.lower()
        assert "role uncertainty 0.30" in text
        assert "share of team" not in text


class TestEntropyBacktest:
    def frame(self, entropies, actuals, proj=100.0):
        return pd.DataFrame({"name": [f"p{i}" for i in range(len(entropies))],
                             "proj_points": proj, "role_entropy": entropies}), actuals

    def test_it_bins_by_entropy_and_reports_the_spread(self, monkeypatch):
        # Six players: the low-entropy three land on their projection, the
        # high-entropy three miss it by half.
        b, actuals = self.frame([0.1, 0.1, 0.1, 0.9, 0.9, 0.9],
                                [100.0, 100.0, 100.0, 150.0, 50.0, 150.0])
        monkeypatch.setattr(roles, "_actual_points",
                            lambda *a, **k: dict(zip(b["name"], actuals)))
        out = roles.entropy_error_backtest(b, 2025, league(), bins=2)
        assert out["n"] == 6
        assert [r["abs_pct_error"] for r in out["bins"]] == [0.0, 0.5]
        assert out["spread"] == 0.5

    def test_a_player_the_season_never_saw_is_left_out(self, monkeypatch):
        b, _ = self.frame([0.1, 0.9], [0.0, 0.0])
        monkeypatch.setattr(roles, "_actual_points", lambda *a, **k: {"p0": 100.0})
        assert roles.entropy_error_backtest(b, 2025, league(), bins=2)["n"] == 1

    def test_players_below_the_projection_floor_are_left_out(self, monkeypatch):
        b, _ = self.frame([0.1, 0.9], [0.0, 0.0], proj=10.0)
        monkeypatch.setattr(roles, "_actual_points",
                            lambda *a, **k: {"p0": 10.0, "p1": 10.0})
        # Below min_points a percentage error is dominated by the denominator.
        assert roles.entropy_error_backtest(b, 2025, league(), bins=2)["n"] == 0

    def test_nothing_scorable_reports_nothing_rather_than_a_number(self, monkeypatch):
        b, _ = self.frame([np.nan, np.nan], [0.0, 0.0])
        monkeypatch.setattr(roles, "_actual_points", lambda *a, **k: {})
        out = roles.entropy_error_backtest(b, 2025, league())
        assert out == {"season": 2025, "n": 0, "bins": [], "spread": None}


class TestAttach:
    def test_attaching_entropy_replaces_rather_than_duplicates(self):
        b = pd.DataFrame({"name": ["a"], "proj_points": [100.0], "espn_proj": [99.9999],
                          "role_entropy": [0.9]})
        out = roles.attach_role_entropy(b, pd.DataFrame({"player": [], "role_churn": []}))
        assert list(out.columns).count("role_entropy") == 1
        assert out.loc[0, "role_entropy"] == pytest.approx(0.0, abs=1e-5)

    def test_attaching_opportunity_needs_a_player_id(self):
        b = pd.DataFrame({"name": ["a"]})
        assert roles.attach_opportunity(b, Scoring()) is b

    def test_rows_with_no_player_id_come_back_null_and_keep_their_index(
            self, monkeypatch):
        # Kickers and defenses are priced from ESPN alone and carry no
        # player_id. They must come back null, the row count must not move, and
        # the index must survive, because entropy and handcuffs join on it.
        shares = pd.DataFrame({"player_id": ["00-1"], "target_share": [0.11],
                               "carry_share": [0.0], "redzone_share": [0.05],
                               "snap_share": [0.9]})
        monkeypatch.setattr(roles, "opportunity_shares", lambda *a, **k: shares)
        b = pd.DataFrame({"name": ["A", "SF D/ST"], "player_id": ["00-1", np.nan],
                          "target_share": [0.2, np.nan]}, index=[7, 9])
        out = roles.attach_opportunity(b, Scoring())
        assert len(out) == 2
        assert list(out.index) == [7, 9]
        assert out.loc[7, "target_share"] == 0.11
        assert pd.isna(out.loc[9, "target_share"])

    def test_the_role_terms_are_finite_for_a_row_with_no_history(self):
        avail = roles.attach_handcuffs(board([
            {"name": "SF D/ST", "position": "DST", "team": "SF", "proj_points": 120.0,
             "adj_ppg": np.nan, "exp_games": np.nan, "injury_risk": np.nan},
            {"name": "KC K", "position": "K", "team": "KC", "proj_points": 130.0,
             "adj_ppg": np.nan, "exp_games": np.nan, "injury_risk": np.nan},
        ]))
        mult = roles.pick_value_multiplier(avail, league(), None, start_prob_weight=1.0)
        bonus = roles.pick_value_bonus(avail, league(), None, handcuff_weight=1.0)
        assert mult.notna().all() and bonus.notna().all()
        assert (mult == 1.0).all() and (bonus == 0.0).all()

    def test_attaching_handcuffs_replaces_rather_than_duplicates(self):
        b = board([{"name": "A", "position": "RB", "team": "SF", "proj_points": 200.0},
                   {"name": "B", "position": "RB", "team": "SF", "proj_points": 100.0}])
        b["contingent_points"] = 999.0
        out = roles.attach_handcuffs(b)
        assert list(out.columns).count("contingent_points") == 1
        assert out.loc[0, "contingent_points"] == 0.0
