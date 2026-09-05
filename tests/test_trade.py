"""Trade evaluator: both sides, on a fixture two-for-one, with the spread."""
import numpy as np
import pandas as pd
import pytest

from ffdraft import board, roles, trade
from ffdraft.config import LeagueSettings


def _league(**kw) -> LeagueSettings:
    return LeagueSettings(name="t", teams=12, draft_slot=1, rounds=14, **kw)


def _board(rows: list[dict]) -> pd.DataFrame:
    b = pd.DataFrame(rows)
    b["_key"] = b["name"].map(board.norm_name)
    return b


# One elite receiver against two ordinary ones: the two-for-one the task names.
#
# Every `exp_games` is below 17 on purpose. At 17 the availability draw always
# succeeds, every trial is identical, `block_spread` comes out 0.0 and the whole
# seed-block apparatus passes its tests without ever running. A fixture that
# cannot vary cannot test a harness whose job is to measure variation.
FIXTURE = [
    {"name": "Elite WR", "position": "WR", "adj_ppg": 20.0, "exp_games": 15.5, "bye_week": 7,
     "proj_points": 310.0, "adp": 3.0},
    {"name": "Good WR", "position": "WR", "adj_ppg": 12.0, "exp_games": 16.0, "bye_week": 9,
     "proj_points": 192.0, "adp": 30.0},
    {"name": "Okay WR", "position": "WR", "adj_ppg": 11.0, "exp_games": 15.0, "bye_week": 11,
     "proj_points": 165.0, "adp": 45.0},
    {"name": "My QB", "position": "QB", "adj_ppg": 18.0, "exp_games": 16.5, "bye_week": 5,
     "proj_points": 297.0, "adp": 20.0},
    {"name": "My RB", "position": "RB", "adj_ppg": 14.0, "exp_games": 13.5, "bye_week": 6,
     "proj_points": 189.0, "adp": 15.0},
    {"name": "My RB2", "position": "RB", "adj_ppg": 9.0, "exp_games": 14.5, "bye_week": 8,
     "proj_points": 130.5, "adp": 60.0},
    {"name": "My TE", "position": "TE", "adj_ppg": 8.0, "exp_games": 16.0, "bye_week": 10,
     "proj_points": 128.0, "adp": 70.0},
    {"name": "Their QB", "position": "QB", "adj_ppg": 17.0, "exp_games": 16.0, "bye_week": 12,
     "proj_points": 272.0, "adp": 25.0},
    {"name": "Their RB", "position": "RB", "adj_ppg": 13.0, "exp_games": 14.0, "bye_week": 6,
     "proj_points": 182.0, "adp": 18.0},
    {"name": "Their RB2", "position": "RB", "adj_ppg": 10.0, "exp_games": 15.0, "bye_week": 9,
     "proj_points": 150.0, "adp": 55.0},
    {"name": "Their TE", "position": "TE", "adj_ppg": 7.0, "exp_games": 16.0, "bye_week": 13,
     "proj_points": 112.0, "adp": 80.0},
    {"name": "Their WR", "position": "WR", "adj_ppg": 10.0, "exp_games": 15.0, "bye_week": 5,
     "proj_points": 150.0, "adp": 40.0},
]

MINE = ["My QB", "My RB", "My RB2", "Elite WR", "My TE"]
THEIRS = ["Their QB", "Their RB", "Their RB2", "Good WR", "Okay WR", "Their TE", "Their WR"]


def _picks(names: list[str], slot: int, start: int) -> list[dict]:
    rows = {r["name"]: r for r in FIXTURE}
    return [{"overall": start + i, "slot": slot, "name": n, "player_id": None,
             "position": rows[n]["position"]} for i, n in enumerate(names)]


@pytest.fixture
def fixture_board():
    return _board(FIXTURE)


@pytest.fixture
def by_slot():
    return {1: _picks(MINE, 1, 1), 2: _picks(THEIRS, 2, 20)}


class TestAvailabilityIsNotChargedTwice:
    def test_a_week_scores_the_per_game_rate_not_the_season_projection(self):
        """`proj_points` is `adj_ppg * exp_games`, so a week that paid out
        `proj_points` would charge the injury discount a second time.

        Availability is pinned at 1 here so the arithmetic is exact and this test
        measures the rate rather than the draws.
        """
        b = _board([{"name": "Iron QB", "position": "QB", "adj_ppg": 18.0,
                     "exp_games": 17.0, "bye_week": 5, "proj_points": 306.0, "adp": 20.0}])
        players, missing = trade.resolve(b, ["Iron QB"])
        assert missing == [] and players[0].adj_ppg == 18.0
        # 14 fantasy weeks, one of them his bye, nothing else on the roster.
        out = trade.simulate_season(players, _league(), seed=0)
        assert out["points"] == pytest.approx(18.0 * 13, abs=0.05)

    def test_a_missing_per_game_rate_is_recovered_from_the_projection(self):
        # A kicker or a defense carries no adj_ppg; proj_points / exp_games is the
        # identity read backwards, not a new assumption.
        b = _board([{"name": "K Man", "position": "K", "adj_ppg": np.nan,
                     "exp_games": 17.0, "bye_week": None, "proj_points": 170.0, "adp": 150.0}])
        player = trade.resolve(b, ["K Man"])[0][0]
        assert player.adj_ppg == pytest.approx(10.0)
        assert player.basis == trade.BASIS_DERIVED

    def test_the_fallback_is_named_per_player_not_applied_quietly(self):
        """A roster can mix bases, and which rows were derived is what a reader
        needs to weigh a delta built from them."""
        b = _board([
            {"name": "Real WR", "position": "WR", "adj_ppg": 12.0, "exp_games": 16.0,
             "bye_week": 9, "proj_points": 192.0, "adp": 30.0},
            {"name": "K Man", "position": "K", "adj_ppg": np.nan, "exp_games": 17.0,
             "bye_week": None, "proj_points": 170.0, "adp": 150.0},
            {"name": "Unknown", "position": "TE", "adj_ppg": np.nan, "exp_games": 17.0,
             "bye_week": None, "proj_points": np.nan, "adp": 200.0},
        ])
        roster, _ = trade.resolve(b, ["Real WR", "K Man", "Unknown"])

        out = trade.priced_by(roster)
        assert out["counts"] == {trade.BASIS_BOARD: 1, trade.BASIS_DERIVED: 1,
                                 trade.BASIS_NONE: 1}
        named = {r["name"]: r["basis"] for r in out["not_from_the_board"]}
        assert named == {"K Man": trade.BASIS_DERIVED, "Unknown": trade.BASIS_NONE}
        # A player with no projection at all is worth 0, not NaN: one NaN would
        # turn a whole roster's season into NaN.
        assert roster[2].adj_ppg == 0.0

    def test_availability_comes_from_the_same_mapping_the_board_uses(self, fixture_board):
        hurt = _board([{"name": "Fragile", "position": "WR", "adj_ppg": 10.0,
                        "exp_games": 8.5, "bye_week": None, "proj_points": 85.0, "adp": 50.0}])
        player = trade.resolve(hurt, ["Fragile"])[0][0]
        assert player.weekly_availability == roles.weekly_availability(8.5)
        assert player.weekly_availability == pytest.approx(0.5)


class TestPairing:
    def test_a_player_the_trade_does_not_touch_has_the_same_season_on_both_sides(self):
        """The draws are keyed by (seed, player, week), not taken from a stream.

        With a sequential RNG, removing one player shifts every later draw and the
        before/after difference measures the reshuffle instead of the trade.
        """
        assert trade._available(7, "my-qb", 3) == trade._available(7, "my-qb", 3)
        assert trade._available(7, "my-qb", 3) != trade._available(8, "my-qb", 3)
        assert trade._available(7, "my-qb", 3) != trade._available(7, "my-rb", 3)

    def test_an_untouched_roster_scores_identically_under_the_same_seed(self, fixture_board):
        players, _ = trade.resolve(fixture_board, MINE)
        a = trade.simulate_season(players, _league(), seed=11)
        b = trade.simulate_season(list(reversed(players)), _league(), seed=11)
        assert a == b, "roster order changed the season, so the draws are not keyed"


class TestTwoForOne:
    def test_both_sides_are_scored_and_the_spread_is_reported(self, fixture_board, by_slot):
        out = trade.evaluate(fixture_board, by_slot, _league(), my_slot=1,
                             counterparty_slot=2, give=["Elite WR"],
                             get=["Good WR", "Okay WR"], n_trials=40, blocks=2, seed=0)

        assert out["ok"] is True
        for side in ("you", "counterparty"):
            block = out[side]
            assert len(block["blocks"]) == 2
            assert block["blocks_agree_p_null"] == 0.5
            assert len(block["block_improvements"]) == 2
            # The seed blocks are disjoint, not a repeat of the same trials.
            assert block["blocks"][0]["seed_from"] != block["blocks"][1]["seed_from"]
            # And they really are different samples: a spread of exactly 0 means
            # the availability draws never varied and the harness did not run.
            assert block["block_spread"] > 0, f"{side} blocks are identical"
            assert block["blocks"][0]["points_before"] != block["blocks"][1]["points_before"]

    def test_the_side_that_fills_a_starting_slot_gains(self, fixture_board, by_slot):
        """Two startable receivers beat one in a lineup with room for three.

        This is the trade being measured, not a tautology: the elite receiver
        scores more per week than either of the two, and still loses because only
        one of him can occupy one slot.
        """
        out = trade.evaluate(fixture_board, by_slot, _league(), my_slot=1,
                             counterparty_slot=2, give=["Elite WR"],
                             get=["Good WR", "Okay WR"], n_trials=40, blocks=2, seed=0)

        assert out["you"]["improvement"] > 0
        assert out["you"]["blocks_agree"] is True
        assert "gains" in out["you"]["verdict"]

    def test_depth_after_the_trade_is_reported_per_position(self, fixture_board, by_slot):
        out = trade.evaluate(fixture_board, by_slot, _league(), my_slot=1,
                             counterparty_slot=2, give=["Elite WR"],
                             get=["Good WR", "Okay WR"], n_trials=10, blocks=2, seed=0)

        before, after = out["you"]["depth_before"], out["you"]["depth_after"]
        assert before["WR"]["rostered"] == 1 and after["WR"]["rostered"] == 2
        assert after["WR"]["spare"] == after["WR"]["rostered"] - after["WR"]["starts"]

    def test_the_counterparty_tendencies_come_from_their_draft_record(self, fixture_board,
                                                                     by_slot):
        out = trade.evaluate(fixture_board, by_slot, _league(), my_slot=1,
                             counterparty_slot=2, give=["Elite WR"],
                             get=["Good WR", "Okay WR"], n_trials=10, blocks=2, seed=0)

        tend = out["counterparty"]["tendencies"]
        assert tend["picks"] == len(THEIRS)
        assert tend["by_position"]["RB"] == 2
        assert tend["mean_adp_delta"] is not None


class TestBothSidesAreScoredOnTheirOwnLineup:
    """The claim the tool makes in prose, tested rather than asserted.

    In the two-for-one above every receiver starts on whichever roster holds him,
    so the trade is a pure transfer and the two deltas come out exact mirrors.
    That is correct there and proves nothing about the general case, where a
    player's worth depends on the lineup he lands in.
    """

    # I am deep at running back and thin at receiver; they are the reverse. Each
    # side sends a man who was riding the bench and starts the one it receives.
    SURPLUS = [
        {"name": "RB One", "position": "RB", "adj_ppg": 15.0, "exp_games": 16.0,
         "bye_week": 6, "proj_points": 240.0, "adp": 10.0},
        {"name": "RB Two", "position": "RB", "adj_ppg": 13.0, "exp_games": 16.0,
         "bye_week": 8, "proj_points": 208.0, "adp": 22.0},
        {"name": "RB Spare", "position": "RB", "adj_ppg": 11.0, "exp_games": 15.0,
         "bye_week": 10, "proj_points": 165.0, "adp": 50.0},
        {"name": "WR Lonely", "position": "WR", "adj_ppg": 12.0, "exp_games": 16.0,
         "bye_week": 9, "proj_points": 192.0, "adp": 35.0},
        {"name": "WR One", "position": "WR", "adj_ppg": 15.0, "exp_games": 16.0,
         "bye_week": 5, "proj_points": 240.0, "adp": 12.0},
        {"name": "WR Two", "position": "WR", "adj_ppg": 13.0, "exp_games": 16.0,
         "bye_week": 7, "proj_points": 208.0, "adp": 24.0},
        {"name": "WR Spare", "position": "WR", "adj_ppg": 11.5, "exp_games": 15.0,
         "bye_week": 11, "proj_points": 172.5, "adp": 48.0},
        {"name": "RB Lonely", "position": "RB", "adj_ppg": 12.5, "exp_games": 16.0,
         "bye_week": 12, "proj_points": 200.0, "adp": 33.0},
    ]
    A = ["RB One", "RB Two", "RB Spare", "WR Lonely"]
    B = ["WR One", "WR Two", "WR Spare", "RB Lonely"]

    def _setup(self):
        rows = {r["name"]: r for r in self.SURPLUS}
        picks = {
            1: [{"overall": i + 1, "slot": 1, "name": n, "player_id": None,
                 "position": rows[n]["position"]} for i, n in enumerate(self.A)],
            2: [{"overall": i + 20, "slot": 2, "name": n, "player_id": None,
                 "position": rows[n]["position"]} for i, n in enumerate(self.B)],
        }
        # Two RB and two WR slots, no flex: a third back is bench and nothing else.
        league = LeagueSettings(name="t", teams=12, draft_slot=1, rounds=14,
                                starters={"QB": 0, "RB": 2, "WR": 2, "TE": 0, "FLEX": 0,
                                          "K": 0, "DST": 0})
        return _board(self.SURPLUS), picks, league

    def test_a_surplus_for_surplus_swap_can_gain_for_both_sides(self):
        b, picks, league = self._setup()
        out = trade.evaluate(b, picks, league, my_slot=1, counterparty_slot=2,
                             give=["RB Spare"], get=["WR Spare"],
                             n_trials=60, blocks=2, seed=0)

        assert out["ok"] is True
        assert out["you"]["improvement"] > 0, out["you"]["verdict"]
        assert out["counterparty"]["improvement"] > 0, out["counterparty"]["verdict"]
        assert out["you"]["blocks_agree"] and out["counterparty"]["blocks_agree"]

    def test_the_two_deltas_are_not_one_number_with_two_signs(self):
        b, picks, league = self._setup()
        out = trade.evaluate(b, picks, league, my_slot=1, counterparty_slot=2,
                             give=["RB Spare"], get=["WR Spare"],
                             n_trials=60, blocks=2, seed=0)

        mine, theirs = out["you"]["improvement"], out["counterparty"]["improvement"]
        assert mine != -theirs, "the sides were scored as a transfer, not on their lineups"


class TestRefusals:
    def test_blocks_that_disagree_are_not_called_a_win(self):
        summary = {"improvement": 3.0, "block_improvements": [12.0, -6.0],
                   "block_spread": 18.0, "blocks_agree": False, "blocks_agree_p_null": 0.5}
        said = trade.verdict(summary, "you")
        assert "no call" in said
        assert "noise" in said
        assert "gains" not in said

    def test_a_player_on_the_wrong_roster_stops_the_evaluation(self, fixture_board, by_slot):
        out = trade.evaluate(fixture_board, by_slot, _league(), my_slot=1,
                             counterparty_slot=2, give=["Their RB"], get=["Good WR"])
        assert out["ok"] is False
        assert any("not on your roster" in e for e in out["errors"])

    def test_a_name_with_no_board_row_stops_the_evaluation(self, fixture_board, by_slot):
        picks = {1: by_slot[1] + [{"overall": 99, "slot": 1, "name": "Ghost",
                                   "player_id": None, "position": "WR"}],
                 2: by_slot[2]}
        out = trade.evaluate(fixture_board, picks, _league(), my_slot=1,
                             counterparty_slot=2, give=["Ghost"], get=["Good WR"])
        assert out["ok"] is False
        assert any("no board row for" in e and "Ghost" in e for e in out["errors"])

    def test_an_empty_trade_is_refused(self, fixture_board, by_slot):
        out = trade.evaluate(fixture_board, by_slot, _league(), my_slot=1,
                             counterparty_slot=2, give=[], get=[])
        assert out["ok"] is False
        assert any("at least one player" in e for e in out["errors"])
