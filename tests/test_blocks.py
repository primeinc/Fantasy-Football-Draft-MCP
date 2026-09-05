"""The paired-backtest harness reports blocks, never a mean on its own.

The spread between two disjoint seed blocks of the same configuration is this
harness's own noise, and it is the size of the effects it is used to measure.
These tests pin the reporting contract that makes that visible.
"""
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
