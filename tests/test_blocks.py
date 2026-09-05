"""The paired-backtest harness reports blocks, never a mean on its own.

The spread between two disjoint seed blocks of the same configuration is this
harness's own noise, and it is the size of the effects it is used to measure.
These tests pin the reporting contract that makes that visible.
"""
import numpy as np
import pandas as pd

from ffdraft import adp


def score(names):
    """A roster's weekly-lineup dict, with the score encoded in the names."""
    return {"points": float(sum(float(n) for n in names)), "empty_slots": 0}


def pair_from(deltas):
    """A draft_pair whose `on` roster beats `off` by deltas[trial_seed]."""
    def pair(trial_seed):
        gain = deltas[trial_seed]
        return (["100"], ["100"] if gain == 0 else [str(100 + gain)])
    return pair


def run(deltas, n_trials=2, blocks=2, seed=0, said=None):
    return adp._paired_blocks(pair_from(deltas), score, n_trials, blocks, seed,
                              (said.append if said is not None else lambda _m: None),
                              "test")


class TestBlocks:
    def test_blocks_use_disjoint_seeds(self):
        seen = []

        def pair(trial_seed):
            seen.append(trial_seed)
            return (["100"], ["101"])

        adp._paired_blocks(pair, score, n_trials=3, blocks=2, seed=10,
                           say=lambda _m: None, label="t")
        # Block 0 takes seeds 10-12, block 1 takes 13-15: a second block extends
        # the sample rather than repeating it.
        assert seen == [10, 11, 12, 13, 14, 15]

    def test_each_block_reports_its_own_improvement(self):
        out = run({0: 10, 1: 10, 2: -4, 3: -4})
        assert [b["improvement"] for b in out["blocks"]] == [10.0, -4.0]
        assert out["block_improvements"] == [10.0, -4.0]
        assert [b["seed_from"] for b in out["blocks"]] == [0, 2]

    def test_the_spread_between_blocks_is_reported_beside_the_mean(self):
        out = run({0: 10, 1: 10, 2: -4, 3: -4})
        assert out["improvement"] == 3.0
        # The number the improvement has to be read against.
        assert out["block_spread"] == 14.0

    def test_blocks_that_disagree_in_sign_do_not_agree(self):
        # +18.4 on one block and -21.3 on the next was the real case: a mean of
        # -1.5 would have read as a finding.
        assert run({0: 18, 1: 18, 2: -21, 3: -21})["blocks_agree"] is False

    def test_blocks_that_agree_in_sign_agree(self):
        assert run({0: 18, 1: 18, 2: 4, 3: 4})["blocks_agree"] is True
        assert run({0: -18, 1: -18, 2: -4, 3: -4})["blocks_agree"] is True

    def test_a_block_at_exactly_zero_agrees_with_nothing(self):
        # No change is no evidence, in either direction.
        assert run({0: 10, 1: 10, 2: 0, 3: 0})["blocks_agree"] is False

    def test_a_tie_is_not_a_loss(self):
        # Half these trials draft the identical roster. Counting an abstention
        # as a failure drives any conservative term toward a 50% win rate.
        out = run({0: 5, 1: 0, 2: 5, 3: 0})
        assert out["trials_changed"] == 2
        assert out["trials_improved_of_changed"] == 2
        assert sum(b["trials_improved"] for b in out["blocks"]) == 2

    def test_the_progress_lines_name_the_block_and_the_ties(self):
        said: list[str] = []
        run({0: 5, 1: 0, 2: 5, 3: 0}, said=said)
        assert any("block 1/2 trial 1/2" in line for line in said)
        assert any("identical rosters" in line for line in said)
        assert any("of the trials it changed" in line for line in said)

    def test_one_block_still_reports_a_spread_of_zero_not_an_absent_one(self):
        out = run({0: 7, 1: 7}, blocks=1)
        assert out["block_spread"] == 0.0
        assert out["blocks_agree"] is True
        assert len(out["blocks"]) == 1

    def test_no_blocks_reports_nothing_rather_than_a_number(self):
        out = adp._block_summary([])
        assert out["improvement"] is None
        assert out["block_spread"] is None
        assert out["blocks_agree"] is False


class TestAgreementIsNotAPass:
    def test_agreement_carries_what_it_is_worth_under_the_null(self):
        # Two blocks of a term that does nothing agree in sign half the time,
        # so `blocks_agree: true` at the default is one coin flip. The field
        # sits beside it so a reader does not need the docs open.
        assert run({0: 5, 1: 5, 2: 5, 3: 5})["blocks_agree_p_null"] == 0.5
        assert run({i: 5 for i in range(8)}, blocks=4)["blocks_agree_p_null"] == 0.125
        assert run({0: 5, 1: 5}, blocks=1)["blocks_agree_p_null"] == 1.0

    def test_the_verdict_refuses_the_word_pass(self):
        out = {"seasons": [run({0: 5, 1: 5, 2: 5, 3: 5})], "blocks_agree": True}
        out["seasons"][0]["season"] = 2024
        verdict = adp.block_verdict(out)
        assert "not a pass" in verdict
        assert "0.5" in verdict

    def test_the_verdict_says_disagreement_supports_nothing(self):
        out = {"seasons": [run({0: 18, 1: 18, 2: -21, 3: -21})], "blocks_agree": False}
        assert "supports nothing" in adp.block_verdict(out)

    def test_nothing_scored_is_not_a_verdict(self):
        assert adp.block_verdict({"seasons": [], "blocks_agree": False}) == \
            "nothing scored: no verdict"


class TestSummaryShape:
    def test_the_summary_carries_every_field_the_callers_print(self):
        out = run({0: 5, 1: 5, 2: 5, 3: 5})
        for key in ("blocks", "improvement", "block_improvements", "block_spread",
                    "blocks_agree", "trials_improved_of_changed", "trials_changed",
                    "players_swapped"):
            assert key in out, key
        for key in ("block", "seed_from", "n_trials", "weekly_points_off",
                    "weekly_points_on", "improvement", "trials_improved",
                    "trials_changed", "trials_improved_of_changed", "players_swapped",
                    "empty_slots_off", "empty_slots_on"):
            assert key in out["blocks"][0], key

    def test_default_blocks_is_more_than_one(self):
        # The whole point: a single block cannot show its own noise.
        assert adp.DEFAULT_BLOCKS >= 2


class TestTheCalibrationRule:
    """Points only when the blocks agree in sign and each block beats its own
    mean out of sample; ordinal otherwise.

    The rule lived in a docstring, and a rule that only lives in prose decays
    into decoration. `adp.margin_unit` is now the one place it is decided and
    the only place the word "points" can be obtained, so these pin the verdict
    rather than the sentence.
    """

    def test_both_clauses_met_earns_points(self):
        out = adp.margin_unit(True, [True, True], 2)
        assert out["unit"] == adp.UNIT_POINTS
        assert "beats its own mean" in out["unit_reason"]

    def test_disagreeing_signs_are_ordinal_whatever_the_scores_say(self):
        out = adp.margin_unit(False, [True, True], 2)
        assert out["unit"] == adp.UNIT_ORDINAL
        assert "disagree in sign" in out["unit_reason"]

    def test_agreement_alone_does_not_buy_points(self):
        # The second clause is what separated defences from kickers: both had
        # agreeing blocks, only defences beat their own mean.
        out = adp.margin_unit(True, [True, False], 2)
        assert out["unit"] == adp.UNIT_ORDINAL
        assert "does not beat its own mean" in out["unit_reason"]

    def test_evidence_not_offered_is_unproven_not_waived(self):
        # The direction that makes this an enforcement rather than a suggestion.
        # A caller who cannot produce out-of-sample scores cannot get the word
        # "points" out of this function at all.
        out = adp.margin_unit(True, None, 2)
        assert out["unit"] == adp.UNIT_ORDINAL
        assert "unproven rather than waived" in out["unit_reason"]

    def test_a_score_per_block_or_none_at_all(self):
        # Two blocks and one flag is not evidence about both of them.
        out = adp.margin_unit(True, [True], 2)
        assert out["unit"] == adp.UNIT_ORDINAL
        assert "1 out-of-sample results for 2 blocks" in out["unit_reason"]

    def test_no_blocks_is_ordinal(self):
        assert adp.margin_unit(True, [], 0)["unit"] == adp.UNIT_ORDINAL

    def test_the_flag_read_survives_every_shape_a_frame_returns(self):
        # `x is True` fails on np.True_ and plain truthiness passes NaN, so the
        # read is measured rather than assumed. Six states, one answer each.
        assert adp._flag(True) is True
        assert adp._flag(np.True_) is True
        assert adp._flag(False) is False
        assert adp._flag(np.False_) is False
        assert adp._flag(np.nan) is False
        assert adp._flag(pd.NA) is False

    def test_a_numpy_flag_does_not_silently_fail_the_second_clause(self):
        # What the read is for: `variance_explained > 0` on a numpy scalar gives
        # np.True_, and an identity test against True would call it a failure.
        assert adp.margin_unit(True, [np.True_, np.True_], 2)["unit"] == adp.UNIT_POINTS


class TestTheReplicationDoor:
    """The second clause asks whether a fit generalises. A harness that fitted
    nothing cannot be asked it, so the clause is inapplicable there rather than
    failed -- and answering ordinal would turn a quantity of points into an
    ordering, which is less true and not more careful.

    The door is narrow on purpose: the caller has to declare the unit its inputs
    already carry, because that declaration is the whole argument.
    """

    def test_a_replication_harness_earns_points_on_sign_agreement(self):
        out = adp.margin_unit(True, None, 2, adp.HARNESS_REPLICATION, adp.UNIT_POINTS)
        assert out["unit"] == adp.UNIT_POINTS
        assert "nothing was fitted" in out["unit_reason"]
        # The reason names the door, so a reader sees which argument the number
        # rests on instead of inferring it from the module that produced it.
        assert out["harness"] == adp.HARNESS_REPLICATION

    def test_the_spread_says_what_it_does_not_cover(self):
        # freddy's caveat, in the output rather than a docstring. A replication's
        # inputs may come out of something fitted, so its spread is replication
        # noise and not the error bar on the answer. That distinction reads
        # identically to the whole uncertainty unless something says otherwise.
        out = adp.margin_unit(True, None, 2, adp.HARNESS_REPLICATION, adp.UNIT_POINTS)
        assert out["spread_covers"] == adp.SPREAD_REPLICATION_ONLY
        # Both halves, because the second is the one that stops the misreading:
        # what the number is, and what it is not.
        assert "replication noise only" in out["spread_covers"]
        assert "not the projection's error" in out["spread_covers"]
        # The fitted path makes no such claim, because there the held-out score
        # is the generalisation evidence.
        assert "spread_covers" not in adp.margin_unit(True, [True, True], 2)

    def test_the_declaration_is_what_opens_it(self):
        out = adp.margin_unit(True, None, 2, adp.HARNESS_REPLICATION)
        assert out["unit"] == adp.UNIT_ORDINAL
        assert "must declare the unit" in out["unit_reason"]

    def test_disagreeing_signs_close_it_like_anyone_else(self):
        out = adp.margin_unit(False, None, 2, adp.HARNESS_REPLICATION, adp.UNIT_POINTS)
        assert out["unit"] == adp.UNIT_ORDINAL
        assert "disagree in sign" in out["unit_reason"]

    def test_the_strict_path_is_the_default(self):
        # Saying nothing gets the fitted rule, so the door has to be asked for
        # by name rather than fallen through.
        assert adp.margin_unit(True, None, 2)["unit"] == adp.UNIT_ORDINAL
        assert adp.margin_unit(True, None, 2)["harness"] == adp.HARNESS_FITTED

    def test_an_unrecognised_harness_is_ordinal_not_trusted(self):
        out = adp.margin_unit(True, None, 2, "vibes", adp.UNIT_POINTS)
        assert out["unit"] == adp.UNIT_ORDINAL
        assert "unknown harness" in out["unit_reason"]


class TestBlockAgreementCarriesTheVerdict:
    def test_agreeing_gains_without_out_of_sample_scores_are_ordinal(self):
        out = adp.block_agreement([5.0, 4.0])
        assert out["blocks_agree"] is True
        assert out["unit"] == adp.UNIT_ORDINAL
        assert out["beats_own_mean"] is None

    def test_agreeing_gains_with_the_scores_are_points(self):
        out = adp.block_agreement([5.0, 4.0], [True, True])
        assert out["unit"] == adp.UNIT_POINTS
        assert out["beats_own_mean"] == [True, True]

    def test_the_verdict_sits_beside_the_mean_it_qualifies(self):
        # One dict, so there is no way to read `improvement` without the field
        # that says what it may be called.
        out = adp.block_agreement([5.0, 4.0])
        for key in ("improvement", "block_spread", "blocks_agree",
                    "blocks_agree_p_null", "unit", "unit_reason"):
            assert key in out, key
