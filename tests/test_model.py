"""Draft logic: survival probability, roster need, and scoring-format conversion."""
import numpy as np
import pandas as pd

from ffdraft.board import FORMAT_SHIFT_DAMPING, convert_adp_format, synthetic_adp
from ffdraft.config import LeagueSettings
from ffdraft.model import (
    _discount,
    _positional_need,
    apply_current_team,
    expected_best_at_next_pick,
    recommend,
    survival_probability,
    survival_probability_vec,
    touchdown_luck_multiplier,
)


class TestDiscount:
    def test_a_discount_lowers_a_value_of_either_sign(self):
        # A multiplier of 0.2 means "move this by 80% of its own size", applied
        # by reflection below zero.
        out = _discount(pd.Series([10.0, -10.0]), pd.Series([0.2, 0.2]))
        assert abs(out[0] - 2.0) < 1e-9
        assert abs(out[1] + 18.0) < 1e-9
        # Both moved down the list, which is what a discount has to mean.
        assert out[0] < 10.0 and out[1] < -10.0

    def test_the_penalty_is_bounded(self):
        # Dividing would be unbounded: need_mult bottoms out at 0.02, so a
        # negative value could be inflated fiftyfold, which is invisible in a
        # ranking and ruinous in replay's per-team sum of pick_regret.
        out = _discount(pd.Series([-10.0, -10.0]), pd.Series([0.02, 0.0]))
        assert all(abs(v) <= 20.0 for v in out)

    def test_a_boost_raises_a_value_of_either_sign(self):
        out = _discount(pd.Series([10.0, -10.0]), pd.Series([1.3, 1.3]))
        assert out[0] > 10.0 and out[1] > -10.0

    def test_a_neutral_multiplier_changes_nothing(self):
        values = pd.Series([10.0, 0.0, -10.0])
        assert list(_discount(values, pd.Series([1.0, 1.0, 1.0]))) == [10.0, 0.0, -10.0]

    def test_a_heavier_discount_cannot_promote_a_worse_candidate(self):
        # The live case at pick 123: Daniel Jones, marginal_value -20.9, at a
        # need of 0.04 for a second quarterback, against Juwan Johnson at -10.4
        # and a tight-end need of 0.28. Multiplied, the quarterback lands nearer
        # zero and outranks the better candidate purely because he was
        # discounted harder.
        qb, te = -20.9, -10.4
        mult = pd.Series([0.04, 0.28])
        naive = pd.Series([qb, te]) * mult
        assert naive[0] > naive[1]
        fixed = _discount(pd.Series([qb, te]), mult)
        assert fixed[1] > fixed[0]

    def test_a_zero_multiplier_does_not_produce_inf(self):
        out = _discount(pd.Series([-10.0]), pd.Series([0.0]))
        assert np.isfinite(out[0])

    def test_recommend_buries_a_backup_quarterback(self):
        league = LeagueSettings(name="t", teams=12)
        b = pd.DataFrame({
            "name": ["Real WR", "Backup QB A", "Backup QB B"],
            "position": ["WR", "QB", "QB"], "team": ["A", "B", "C"],
            "proj_points": [150.0, 300.0, 250.0], "draft_score": [20.0, 60.0, 40.0],
            "adp": [80.0, 120.0, 140.0], "pos_rank": [30, 12, 20],
            "overall_rank": [90, 130, 160], "consistency": [0.5, 0.5, 0.5],
            "adj_ppg": [10.0, 18.0, 15.0], "drafted": [False, False, False],
        })
        b["_key"] = b["name"].map(lambda n: n.lower())
        # A quarterback already rostered: BACKUP_DECAY["QB"] puts need at 0.04.
        out = recommend(b, league, current_pick=100, next_pick=120,
                        roster={"QB": 1, "RB": 2, "WR": 2, "TE": 1}, top_n=3)
        assert out["name"].iloc[0] == "Real WR"
        assert out["name"].tolist()[1:] == ["Backup QB A", "Backup QB B"]


class TestSurvival:
    def test_a_player_going_before_your_pick_is_gone(self):
        assert survival_probability(adp=5, current_pick=20, next_pick=33) < 0.05

    def test_a_late_adp_player_survives(self):
        assert survival_probability(adp=120, current_pick=20, next_pick=33) > 0.9

    def test_probability_falls_as_the_wait_lengthens(self):
        short = survival_probability(adp=40, current_pick=20, next_pick=25)
        long = survival_probability(adp=40, current_pick=20, next_pick=60)
        assert short > long

    def test_always_a_probability(self):
        for adp in (1, 10, 50, 100, 250):
            for nxt in (12, 40, 90):
                p = survival_probability(adp, 10, nxt)
                assert 0.0 <= p <= 1.0

    def test_vectorised_matches_scalar(self):
        adps = np.array([3.0, 25.0, 60.0, 140.0])
        vec = survival_probability_vec(adps, 20, 33)
        scal = [survival_probability(a, 20, 33) for a in adps]
        assert np.allclose(vec, scal, atol=1e-9)

    def test_missing_adp_does_not_produce_nan(self):
        out = survival_probability_vec(np.array([np.nan, 30.0]), 10, 20)
        assert not np.isnan(out).any()

    def test_a_player_far_past_his_adp_is_not_written_off(self):
        # The live case: a defense with an ADP of 93 still on the board at pick
        # 157. A Gaussian tail calls that certain to end, which then told
        # expected_best_at_next_pick that waiting at the position was worth its
        # worst player.
        normal = survival_probability(adp=93, current_pick=157, next_pick=164,
                                      tail="normal")
        logistic = survival_probability(adp=93, current_pick=157, next_pick=164,
                                        tail="logistic")
        assert normal < 0.35
        assert logistic > 0.45
        assert logistic > normal

    def test_the_tail_hazard_is_roughly_constant_once_well_past_adp(self):
        # An exponential tail means "he has slid this far, so the next seven
        # picks look like the last seven" -- the same wait costs about the same
        # whether he is 60 or 100 picks past his ADP.
        far = survival_probability(adp=40, current_pick=160, next_pick=167,
                                   tail="logistic")
        further = survival_probability(adp=40, current_pick=200, next_pick=207,
                                       tail="logistic")
        assert abs(far - further) < 0.02
        # The normal tail has no such limit: it keeps collapsing.
        n_far = survival_probability(adp=40, current_pick=160, next_pick=167,
                                     tail="normal")
        n_further = survival_probability(adp=40, current_pick=200, next_pick=207,
                                         tail="normal")
        assert n_further < n_far

    def test_the_middle_of_the_distribution_is_preserved(self):
        # Only the tail is meant to change; near the ADP the two shapes should
        # give nearly the same answer.
        for adp, current, nxt in ((60, 55, 62), (100, 95, 108), (30, 28, 34)):
            a = survival_probability(adp, current, nxt, tail="normal")
            b = survival_probability(adp, current, nxt, tail="logistic")
            assert abs(a - b) < 0.10

    def test_both_tails_stay_probabilities_and_stay_monotone(self):
        for tail in ("normal", "logistic"):
            previous = 1.1
            for nxt in (21, 30, 45, 70, 120, 250):
                p = survival_probability(40, 20, nxt, tail=tail)
                assert 0.0 <= p <= 1.0
                assert p <= previous
                previous = p


class TestPositionalNeed:
    def test_empty_starting_slot_is_a_premium(self):
        need = _positional_need(LeagueSettings(teams=12), {})
        assert need["RB"] > 1.0 and need["WR"] > 1.0

    def test_backup_quarterback_is_nearly_worthless_in_one_qb(self):
        need = _positional_need(LeagueSettings(teams=12), {"QB": 1, "RB": 2, "WR": 2, "TE": 1})
        assert need["QB"] < 0.3

    def test_third_quarterback_is_worthless(self):
        need = _positional_need(LeagueSettings(teams=12), {"QB": 2, "RB": 2, "WR": 2, "TE": 1})
        assert need["QB"] < 0.05

    def test_superflex_keeps_the_second_quarterback_valuable(self):
        roster = {"QB": 1, "RB": 2, "WR": 2, "TE": 1}
        one_qb = _positional_need(LeagueSettings(teams=12), roster)["QB"]
        sflex = _positional_need(LeagueSettings(teams=12, superflex=1), roster)["QB"]
        assert sflex > one_qb * 3
        assert sflex > 1.0  # it's still a starting slot

    def test_running_back_depth_holds_value(self):
        """Backs get hurt constantly, so bench backs actually enter lineups."""
        roster = {"QB": 1, "RB": 3, "WR": 2, "TE": 1}
        need = _positional_need(LeagueSettings(teams=12), roster)
        assert need["RB"] > need["QB"]

    def test_roster_cap_shuts_a_position_off(self):
        need = _positional_need(LeagueSettings(teams=12), {"WR": 9})
        assert need["WR"] < 0.05


class TestOpportunityCost:
    def test_value_of_waiting_reflects_who_survives(self):
        board = pd.DataFrame([
            {"position": "QB", "draft_score": 100.0, "p_available_next": 0.9},
            {"position": "QB", "draft_score": 95.0, "p_available_next": 0.95},
            {"position": "RB", "draft_score": 100.0, "p_available_next": 0.01},
            {"position": "RB", "draft_score": 40.0, "p_available_next": 0.99},
        ])
        fallback = expected_best_at_next_pick(board)
        # Quarterbacks survive, so waiting costs almost nothing.
        assert fallback["QB"] > 90
        # The elite back will be gone; waiting drops you to a much worse player.
        assert fallback["RB"] < 60

    def test_empty_position_is_handled(self):
        board = pd.DataFrame([{"position": "TE", "draft_score": 10.0,
                               "p_available_next": 0.0}])
        out = expected_best_at_next_pick(board)
        assert np.isfinite(out["TE"])


class TestCurrentTeam:
    def test_depth_chart_overrides_a_stale_team(self):
        """A player traded since he last played a game should show the new team."""
        tbl = pd.DataFrame([{"player_id": "p1", "name": "Trade Guy", "team": "OLD"}])
        dc = pd.DataFrame([{"player_id": "p1", "team": "NEW"}])
        out = apply_current_team(tbl, dc)
        assert out.loc[0, "team"] == "NEW"

    def test_player_missing_from_depth_chart_keeps_last_known_team(self):
        tbl = pd.DataFrame([{"player_id": "p1", "name": "Rookie", "team": "OLD"}])
        dc = pd.DataFrame([{"player_id": "p2", "team": "NEW"}])
        out = apply_current_team(tbl, dc)
        assert out.loc[0, "team"] == "OLD"

    def test_empty_depth_chart_is_a_no_op(self):
        tbl = pd.DataFrame([{"player_id": "p1", "name": "X", "team": "OLD"}])
        out = apply_current_team(tbl, pd.DataFrame(columns=["player_id", "team"]))
        assert out.loc[0, "team"] == "OLD"

    def test_none_depth_chart_is_a_no_op(self):
        tbl = pd.DataFrame([{"player_id": "p1", "name": "X", "team": "OLD"}])
        out = apply_current_team(tbl, None)
        assert out.loc[0, "team"] == "OLD"

    def test_multiple_players_only_matched_ones_move(self):
        tbl = pd.DataFrame([
            {"player_id": "p1", "name": "Traded", "team": "OLD"},
            {"player_id": "p2", "name": "Stayed", "team": "SAME"},
        ])
        dc = pd.DataFrame([
            {"player_id": "p1", "team": "NEW"},
            {"player_id": "p2", "team": "SAME"},
        ])
        out = apply_current_team(tbl, dc).set_index("player_id")
        assert out.loc["p1", "team"] == "NEW"
        assert out.loc["p2", "team"] == "SAME"


class TestTouchdownLuck:
    """touchdown_luck_multiplier is a cross-sectional z-score, like every other
    environment multiplier in project() -- it needs a real spread of players to
    compare against, so single-player cases are exercised as one row in a small
    board rather than in isolation.
    """

    def test_overperformer_gets_discounted_relative_to_the_field(self):
        # Player 0 converted way more red zone touches than baseline predicts;
        # players 1-2 landed close to it.
        touches = pd.Series([20.0, 20.0, 20.0])
        td = pd.Series([10.0, 4.0, 5.0])         # baseline expects 4 on 20 touches
        baseline = pd.Series([0.20, 0.20, 0.20])
        m = touchdown_luck_multiplier(touches, td, baseline, weight=0.06)
        assert m.iloc[0] < 1.0
        assert m.iloc[0] < m.iloc[1]

    def test_underperformer_gets_boosted_relative_to_the_field(self):
        touches = pd.Series([20.0, 20.0, 20.0])
        td = pd.Series([1.0, 4.0, 5.0])          # baseline expects 4 on 20 touches
        baseline = pd.Series([0.20, 0.20, 0.20])
        m = touchdown_luck_multiplier(touches, td, baseline, weight=0.06)
        assert m.iloc[0] > 1.0
        assert m.iloc[0] > m.iloc[1]

    def test_small_sample_is_pinned_neutral_even_in_a_skewed_field(self):
        """A two-touch, two-score '100%' sample sits at exactly 1.0, regardless of
        how much variance the qualifying players around it carry."""
        touches = pd.Series([2.0, 20.0, 20.0])
        td = pd.Series([2.0, 10.0, 1.0])
        baseline = pd.Series([0.20, 0.20, 0.20])
        m = touchdown_luck_multiplier(touches, td, baseline, weight=0.06, min_touches=8)
        assert m.iloc[0] == 1.0

    def test_weight_zero_disables_the_adjustment(self):
        touches = pd.Series([20.0, 20.0])
        td = pd.Series([10.0, 1.0])
        baseline = pd.Series([0.20, 0.20])
        m = touchdown_luck_multiplier(touches, td, baseline, weight=0.0)
        assert (m == 1.0).all()

    def test_never_exceeds_the_configured_weight(self):
        touches = pd.Series([50.0, 50.0, 50.0])
        td = pd.Series([49.0, 0.0, 10.0])   # one huge overperformer, one huge underperformer
        baseline = pd.Series([0.20, 0.20, 0.20])
        m = touchdown_luck_multiplier(touches, td, baseline, weight=0.06)
        assert ((m - 1.0).abs() <= 0.06 + 1e-9).all()

    def test_missing_baseline_does_not_produce_nan(self):
        touches = pd.Series([20.0, 20.0])
        td = pd.Series([5.0, 8.0])
        baseline = pd.Series([np.nan, 0.20])
        m = touchdown_luck_multiplier(touches, td, baseline, weight=0.06)
        assert np.isfinite(m).all()

    def test_uniform_field_is_neutral(self):
        """Everyone matches the baseline exactly -- no spread, no adjustment."""
        touches = pd.Series([20.0, 30.0, 40.0])
        td = pd.Series([4.0, 6.0, 8.0])     # each exactly 20%
        baseline = pd.Series([0.20, 0.20, 0.20])
        m = touchdown_luck_multiplier(touches, td, baseline, weight=0.06)
        assert (m == 1.0).all()


class TestSyntheticAdp:
    def test_quarterbacks_and_tight_ends_slide_past_their_value(self):
        """A room does not draft in value order: the QB1 goes far later than RB1."""
        assert synthetic_adp("QB", 1) > synthetic_adp("RB", 1) * 5
        assert synthetic_adp("TE", 1) > synthetic_adp("WR", 1) * 3

    def test_monotonic_within_a_position(self):
        for pos in ("QB", "RB", "WR", "TE"):
            vals = [synthetic_adp(pos, r) for r in range(1, 30)]
            assert vals == sorted(vals)


class TestFormatConversion:
    @staticmethod
    def _board():
        """A board with realistic depth. Format conversion works on rank shifts, so
        a three-player board can't move anyone — the ordering has nowhere to go."""
        rows = []
        for i in range(60):   # receivers: high reception volume
            rows.append({"name": f"WR{i}", "position": "WR",
                         "proj_points": 240 - i * 2.5,
                         "receptions": 95 - i})
        for i in range(50):   # backs: mixed, some pass-catching some not
            rows.append({"name": f"RB{i}", "position": "RB",
                         "proj_points": 250 - i * 3.0,
                         "receptions": (70 - i) if i % 2 == 0 else 15})
        for i in range(24):   # quarterbacks: zero receptions
            rows.append({"name": f"QB{i}", "position": "QB",
                         "proj_points": 380 - i * 6.0, "receptions": 0})
        b = pd.DataFrame(rows)
        b["receptions"] = b["receptions"].clip(lower=0)
        b = b.sort_values("proj_points", ascending=False).reset_index(drop=True)
        b["overall_rank"] = np.arange(1, len(b) + 1)
        b["adp"] = b["overall_rank"].astype(float)
        # proj_points here are league-format points; PPR adds the missing credit.
        return b

    def _converted(self, label, gap):
        b = self._board()
        b["proj_points_ppr"] = b["proj_points"] + gap * b["receptions"]
        return convert_adp_format(b, label).set_index("name")

    def test_ppr_league_leaves_rankings_untouched(self):
        b = self._board()
        b["proj_points_ppr"] = b["proj_points"]
        out = convert_adp_format(b, "ppr")
        assert out["adp"].equals(b["adp"])
        assert out["adp_format"].iloc[0] == "ppr"

    def test_reception_heavy_players_fall_in_standard(self):
        out = self._converted("standard", 1.0)
        # WR0 catches 95 passes; RB1 catches 15.
        assert out.loc["WR0", "adp"] > out.loc["WR0", "adp_ppr"]
        assert out.loc["RB1", "adp"] < out.loc["RB1", "adp_ppr"]

    def test_quarterbacks_move_less_than_receivers(self):
        out = self._converted("standard", 1.0)
        qb_move = float(abs(out.loc["QB0", "adp"] - out.loc["QB0", "adp_ppr"]))
        wr_move = float(abs(out.loc["WR0", "adp"] - out.loc["WR0", "adp_ppr"]))
        assert qb_move < wr_move

    def test_half_ppr_shift_is_smaller_than_standard(self):
        half = self._converted("half_ppr", 0.5)
        std = self._converted("standard", 1.0)
        assert abs(half.loc["WR0", "adp_shift"]) < abs(std.loc["WR0", "adp_shift"])

    def test_shift_is_damped_not_applied_whole(self):
        assert 0 < FORMAT_SHIFT_DAMPING < 1

    def test_adp_never_goes_below_one(self):
        for label, gap in [("half_ppr", 0.5), ("standard", 1.0)]:
            out = self._converted(label, gap)
            assert (out["adp"] >= 1.0).all()

    def test_missing_ppr_column_is_a_no_op(self):
        b = self._board().drop(columns=[])
        out = convert_adp_format(b, "standard")
        assert out["adp"].equals(b["adp"])
