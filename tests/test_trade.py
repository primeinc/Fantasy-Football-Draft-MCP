"""Trade evaluator: both sides, on a fixture two-for-one, with the spread."""
import numpy as np
import pandas as pd
import pytest

from ffdraft import adp, board, roles, trade
from ffdraft.config import LeagueSettings


def _league(**kw) -> LeagueSettings:
    return LeagueSettings(name="t", teams=12, draft_slot=1, rounds=14, **kw)


def _board(rows: list[dict]) -> pd.DataFrame:
    b = pd.DataFrame(rows)
    b["_key"] = b["name"].map(board.norm_name)
    return b


# One elite receiver against two ordinary ones: the two-for-one the task names.
#
# Every rostered `exp_games` is below 17 on purpose. At 17 the availability draw
# always succeeds, every trial is identical, `block_spread` comes out 0.0 and the
# whole seed-block apparatus passes its tests without ever running. A fixture
# that cannot vary cannot test a harness whose job is to measure variation.
#
# The four "Waiver" rows are on no roster, so they are the free agents the draft
# record's evaluation starts in a hole. Priced low, so the fixture's trades are
# decided by the rostered players.
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
    {"name": "Waiver QB", "position": "QB", "adj_ppg": 1.0, "exp_games": 17.0, "bye_week": 7,
     "proj_points": 17.0, "adp": 200.0},
    {"name": "Waiver RB", "position": "RB", "adj_ppg": 1.0, "exp_games": 17.0, "bye_week": 8,
     "proj_points": 17.0, "adp": 201.0},
    {"name": "Waiver WR", "position": "WR", "adj_ppg": 1.0, "exp_games": 17.0, "bye_week": 10,
     "proj_points": 17.0, "adp": 202.0},
    {"name": "Waiver TE", "position": "TE", "adj_ppg": 1.0, "exp_games": 17.0, "bye_week": 11,
     "proj_points": 17.0, "adp": 203.0},
]

MINE = ["My QB", "My RB", "My RB2", "Elite WR", "My TE"]
THEIRS = ["Their QB", "Their RB", "Their RB2", "Good WR", "Okay WR", "Their TE", "Their WR"]
ROWS = {r["name"]: r for r in FIXTURE}


def _picks(names: list[str], slot: int, start: int) -> list[dict]:
    return [{"overall": start + i, "slot": slot, "name": n, "player_id": None,
             "position": ROWS[n]["position"]} for i, n in enumerate(names)]


def _free(name: str, position: str, rate: float, bye: int) -> trade.Player:
    return trade.Player(name=name, key=f"{trade.WAIVER_KEY}{name}", position=position,
                        adj_ppg=rate, exp_games=17.0, bye_week=bye, basis="fixture")


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

    def test_no_games_to_divide_by_is_not_a_derived_row(self):
        """No division happened, so the result is a BASIS_NONE outcome and must
        not wear the derived label. `model.project` clips exp_games to [7, 17],
        but `resolve` takes any board and fixtures are built by hand."""
        b = _board([{"name": "Zero Games", "position": "WR", "adj_ppg": np.nan,
                     "exp_games": 0.0, "bye_week": None, "proj_points": 100.0,
                     "adp": 90.0}])
        player = trade.resolve(b, ["Zero Games"])[0][0]

        assert player.adj_ppg == 0.0
        assert player.basis == trade.BASIS_NONE

    def test_availability_comes_from_the_same_mapping_the_board_uses(self):
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
                             get=["Good WR", "Okay WR"], first_week=1, last_week=14,
                             n_trials=40, blocks=2, seed=0)

        assert out["ok"] is True, out.get("errors")
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
        """Two startable receivers beat one elite one and a free agent.

        This is the trade being measured, not a tautology: the elite receiver
        scores more per week than either of the two, and still loses because only
        one of him can occupy one slot, and the free agent in the other is worth 1.
        """
        out = trade.evaluate(fixture_board, by_slot, _league(), my_slot=1,
                             counterparty_slot=2, give=["Elite WR"],
                             get=["Good WR", "Okay WR"], first_week=1, last_week=14,
                             n_trials=40, blocks=2, seed=0)

        assert out["you"]["improvement"] > 0
        assert out["you"]["blocks_agree"] is True
        assert "gains" in out["you"]["verdict"]

    def test_depth_after_the_trade_is_reported_per_position(self, fixture_board, by_slot):
        out = trade.evaluate(fixture_board, by_slot, _league(), my_slot=1,
                             counterparty_slot=2, give=["Elite WR"],
                             get=["Good WR", "Okay WR"], first_week=1, last_week=14,
                             n_trials=10, blocks=2, seed=0)

        before, after = out["you"]["depth_before"], out["you"]["depth_after"]
        assert before["WR"]["rostered"] == 1 and after["WR"]["rostered"] == 2
        assert after["WR"]["spare"] == after["WR"]["rostered"] - after["WR"]["starts"]

    def test_the_counterparty_tendencies_come_from_their_draft_record(self, fixture_board,
                                                                     by_slot):
        out = trade.evaluate(fixture_board, by_slot, _league(), my_slot=1,
                             counterparty_slot=2, give=["Elite WR"],
                             get=["Good WR", "Okay WR"], first_week=1, last_week=14,
                             n_trials=10, blocks=2, seed=0)

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
        {"name": "RB Free", "position": "RB", "adj_ppg": 1.0, "exp_games": 17.0,
         "bye_week": 9, "proj_points": 17.0, "adp": 200.0},
        {"name": "WR Free", "position": "WR", "adj_ppg": 1.0, "exp_games": 17.0,
         "bye_week": 9, "proj_points": 17.0, "adp": 201.0},
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
                             give=["RB Spare"], get=["WR Spare"], first_week=1,
                             last_week=14, n_trials=60, blocks=2, seed=0)

        assert out["ok"] is True, out.get("errors")
        assert out["you"]["improvement"] > 0, out["you"]["verdict"]
        assert out["counterparty"]["improvement"] > 0, out["counterparty"]["verdict"]
        assert out["you"]["blocks_agree"] and out["counterparty"]["blocks_agree"]

    def test_the_two_deltas_are_not_one_number_with_two_signs(self):
        b, picks, league = self._setup()
        out = trade.evaluate(b, picks, league, my_slot=1, counterparty_slot=2,
                             give=["RB Spare"], get=["WR Spare"], first_week=1,
                             last_week=14, n_trials=60, blocks=2, seed=0)

        mine, theirs = out["you"]["improvement"], out["counterparty"]["improvement"]
        assert mine != -theirs, "the sides were scored as a transfer, not on their lineups"


class TestTheWindowIsStatedWhereAHumanReadsIt:
    def test_the_verdict_names_the_weeks_that_were_scored(self, fixture_board, by_slot):
        """The structured field and the sentence must not disagree, and the
        sentence names the bounds: "14 weeks" does not say whether week 1 or the
        playoffs were in the window."""
        out = trade.evaluate(fixture_board, by_slot, _league(), my_slot=1,
                             counterparty_slot=2, give=["Elite WR"],
                             get=["Good WR", "Okay WR"], n_trials=20, blocks=2,
                             seed=0, first_week=3, last_week=10)

        assert out["weeks"] == {"from": 3, "to": 10}
        for side in ("you", "counterparty"):
            said = out[side]["verdict"]
            assert "over weeks 3-10" in said, said
            assert "1-14" not in said

    def test_a_no_call_verdict_needs_no_window(self):
        summary = {"improvement": 3.0, "block_improvements": [12.0, -6.0],
                   "block_spread": 18.0, "blocks_agree": False, "blocks_agree_p_null": 0.5}
        assert "no call" in trade.verdict(summary, "you", last_week=10)


IRON = [{"name": "Iron QB", "position": "QB", "adj_ppg": 18.0, "exp_games": 17.0,
         "bye_week": 5, "proj_points": 306.0, "adp": 20.0}]


class TestTheWindowIsTheRestOfTheSeason:
    """A trade weighed in week 2 is about weeks 2 through the last playoff week.

    The live miss: on 2026-09-15 a three-for-three was scored over weeks 1-14,
    crediting a played week, dropping playoff weeks 15-17, and treating a
    concussed quarterback as a preseason injury rate. Availability is pinned at
    1 (`exp_games` 17) so each figure is exact arithmetic on the window.
    """

    def test_weeks_before_the_window_are_not_scored(self):
        players, _ = trade.resolve(_board(IRON), ["Iron QB"])
        out = trade.simulate_season(players, _league(), seed=0, first_week=9, last_week=17)
        # Weeks 9-17, bye 5 outside the window: nine games.
        assert out["points"] == pytest.approx(18.0 * 9, abs=0.05)

    def test_playoff_weeks_past_the_default_are_scored(self):
        players, _ = trade.resolve(_board(IRON), ["Iron QB"])
        out = trade.simulate_season(players, _league(), seed=0, first_week=15, last_week=17)
        assert out["points"] == pytest.approx(18.0 * 3, abs=0.05)

    def test_a_known_absence_scores_zero_whatever_the_draw(self):
        players, _ = trade.resolve(_board(IRON), ["Iron QB"])
        out = trade.simulate_season(players, _league(), seed=0, first_week=9,
                                    last_week=17, out={players[0].key: frozenset({9, 10})})
        assert out["points"] == pytest.approx(18.0 * 7, abs=0.05)

    def test_evaluate_reports_the_window_and_the_absence(self, fixture_board, by_slot):
        out = trade.evaluate(fixture_board, by_slot, _league(), my_slot=1,
                             counterparty_slot=2, give=["Elite WR"],
                             get=["Good WR", "Okay WR"], n_trials=10, blocks=2, seed=0,
                             first_week=6, last_week=17,
                             out={"My QB": frozenset({4, 6, 7})})
        assert out["ok"] is True, out.get("errors")
        assert out["weeks"] == {"from": 6, "to": 17}
        # Week 4 is outside the window, so it moved nothing and is not reported.
        assert out["known_out"] == {"My QB": [6, 7]}
        assert "weeks 6-17" in out["you"]["verdict"]

    def test_a_known_absence_lowers_the_side_that_holds_him(self, fixture_board, by_slot):
        healthy = trade.evaluate(fixture_board, by_slot, _league(), my_slot=1,
                                 counterparty_slot=2, give=["Elite WR"],
                                 get=["Good WR", "Okay WR"], n_trials=20, blocks=2,
                                 seed=0, first_week=2, last_week=17)
        hurt = trade.evaluate(fixture_board, by_slot, _league(), my_slot=1,
                              counterparty_slot=2, give=["Elite WR"],
                              get=["Good WR", "Okay WR"], n_trials=20, blocks=2,
                              seed=0, first_week=2, last_week=17,
                              out={"My QB": frozenset(range(2, 6))})
        assert hurt["you"]["blocks"][0]["points_before"] \
            < healthy["you"]["blocks"][0]["points_before"]
        assert hurt["counterparty"]["blocks"] == healthy["counterparty"]["blocks"]

    def test_an_absence_for_nobody_on_either_roster_is_refused(self, fixture_board, by_slot):
        out = trade.evaluate(fixture_board, by_slot, _league(), my_slot=1,
                             counterparty_slot=2, give=["Elite WR"], get=["Good WR"],
                             first_week=1, last_week=14,
                             out={"Kyler Muray": frozenset({2})})
        assert out["ok"] is False
        assert any("marked out but is on neither roster" in e for e in out["errors"])

    @pytest.mark.parametrize("first, last", [(10, 9), (0, 5), (2, 99), (19, 19)])
    def test_a_window_outside_the_season_is_refused(self, fixture_board, by_slot, first, last):
        out = trade.evaluate(fixture_board, by_slot, _league(), my_slot=1,
                             counterparty_slot=2, give=["Elite WR"], get=["Good WR"],
                             first_week=first, last_week=last)
        assert out["ok"] is False
        assert any("is not a window inside weeks 1-18" in e for e in out["errors"])


class TestTheHarnessArgumentsAreBounded:
    @pytest.mark.parametrize("trials, blocks, bad", [(-1, 2, "n_trials -1"),
                                                     (0, 2, "n_trials 0"),
                                                     (10, -1, "blocks -1"),
                                                     (10, 0, "blocks 0"),
                                                     (trade.MAX_TRIALS + 1, 2, "n_trials"),
                                                     (10, trade.MAX_BLOCKS + 1, "blocks")])
    def test_a_count_outside_its_bounds_is_refused(self, fixture_board, by_slot, trials,
                                                   blocks, bad):
        # Negative trials made NaN block means and negative blocks made no blocks
        # at all, both under ok:true.
        out = trade.evaluate(fixture_board, by_slot, _league(), my_slot=1,
                             counterparty_slot=2, give=["Elite WR"], get=["Good WR"],
                             first_week=1, last_week=14, n_trials=trials, blocks=blocks)
        assert out["ok"] is False
        assert any(e.startswith(bad) and "is outside" in e for e in out["errors"])


class TestAnEmptySlotIsAFreeAgentNotZero:
    """A hole a bye or injury opens is filled from waivers in a real league.

    The live miss: scoring holes at 0 credited every backup with a full game
    against nothing, and a TE downgrade for a backup QB read as a gain for both
    sides. Availability is pinned at 1 so each figure is exact.
    """

    QB_ONLY = LeagueSettings(name="t", teams=12, draft_slot=1, rounds=14,
                             starters={"QB": 1, "RB": 0, "WR": 0, "TE": 0, "FLEX": 0,
                                       "K": 0, "DST": 0})
    RB_PAIR = LeagueSettings(name="t", teams=12, draft_slot=1, rounds=14,
                             starters={"QB": 0, "RB": 2, "WR": 0, "TE": 0, "FLEX": 0,
                                       "K": 0, "DST": 0})

    def test_a_bye_week_scores_the_free_agent(self):
        players, _ = trade.resolve(_board(IRON), ["Iron QB"])
        out = trade.simulate_season(players, self.QB_ONLY, seed=0,
                                    waiver=[_free("FA QB", "QB", 10.0, 9)])
        # Weeks 1-14: thirteen of his games and one free-agent start on his bye.
        assert out["points"] == pytest.approx(18.0 * 13 + 10.0, abs=0.05)
        assert out["empty_slots"] == 1

    def test_a_starter_below_the_free_agent_is_streamed_over(self):
        weak = [{**IRON[0], "name": "Weak QB", "adj_ppg": 8.0, "proj_points": 136.0}]
        players, _ = trade.resolve(_board(weak), ["Weak QB"])
        out = trade.simulate_season(players, self.QB_ONLY, seed=0,
                                    waiver=[_free("FA QB", "QB", 10.0, 9)])
        # The free agent every week but his own bye (9), where Weak QB starts.
        assert out["points"] == pytest.approx(10.0 * 13 + 8.0, abs=0.05)

    def test_a_backup_is_worth_only_his_margin_over_the_free_agent(self):
        rows = IRON + [{"name": "Backup QB", "position": "QB", "adj_ppg": 15.0,
                        "exp_games": 17.0, "bye_week": 9, "proj_points": 255.0, "adp": 150.0}]
        b = _board(rows)
        alone, _ = trade.resolve(b, ["Iron QB"])
        both, _ = trade.resolve(b, ["Iron QB", "Backup QB"])
        free = [_free("FA QB", "QB", 10.0, 11)]
        gain = (trade.simulate_season(both, self.QB_ONLY, seed=0, waiver=free)["points"]
                - trade.simulate_season(alone, self.QB_ONLY, seed=0, waiver=free)["points"])
        # He plays only the starter's bye, and the free agent would have scored 10.
        assert gain == pytest.approx(15.0 - 10.0, abs=0.05)

    def test_two_holes_at_one_position_take_two_different_free_agents(self):
        # The cloning defect: one best free agent at a scalar rate filled every
        # hole at his position, so two empty RB slots scored him twice.
        free = [_free("FA RB One", "RB", 10.0, 9), _free("FA RB Two", "RB", 6.0, 10)]
        out = trade.simulate_season([], self.RB_PAIR, seed=0, first_week=1, last_week=1,
                                    waiver=free)
        assert out["points"] == pytest.approx(16.0)
        alone = trade.simulate_season([], self.RB_PAIR, seed=0, first_week=1, last_week=1,
                                      waiver=free[:1])
        assert alone["points"] == pytest.approx(10.0)
        assert alone["empty_slots"] == 2

    def test_a_free_agent_on_his_bye_is_not_started(self):
        out = trade.simulate_season([], self.QB_ONLY, seed=0, first_week=9, last_week=9,
                                    waiver=[_free("FA QB", "QB", 10.0, 9),
                                            _free("FA QB2", "QB", 4.0, 7)])
        assert out["points"] == pytest.approx(4.0)

    def test_no_free_agents_leave_holes_at_zero(self):
        players, _ = trade.resolve(_board(IRON), ["Iron QB"])
        out = trade.simulate_season(players, self.QB_ONLY, seed=0)
        assert out["points"] == pytest.approx(18.0 * 13, abs=0.05)


class TestWaiverCandidates:
    RB_PAIR = TestAnEmptySlotIsAFreeAgentNotZero.RB_PAIR

    def _pool(self):
        return [{"player": "A", "position": "RB", "per_game": 10.0, "bye_week": 5},
                {"player": "B", "position": "RB", "per_game": 9.0, "bye_week": 5},
                {"player": "C", "position": "RB", "per_game": 8.0, "bye_week": 6},
                {"player": "D", "position": "RB", "per_game": 7.0, "bye_week": 7},
                {"player": "E", "position": "RB", "per_game": 6.0, "bye_week": 8}]

    def test_candidates_are_distinct_and_cover_every_week_s_byes(self):
        # Week 5 has A and B on bye, so two RB slots need C and D as well; E can
        # never start in a two-slot week once four are listed.
        players, basis, errors = trade.waiver_candidates(self._pool(), self.RB_PAIR, 5, 5, "s")
        assert errors == []
        assert [p.name for p in players] == ["A", "B", "C", "D"]
        assert len({p.key for p in players}) == 4
        assert [c["player"] for c in basis["RB"]["candidates"]] == ["A", "B", "C", "D"]

    def test_a_week_with_nobody_on_bye_needs_only_the_slot_count(self):
        players, _, _ = trade.waiver_candidates(self._pool(), self.RB_PAIR, 9, 9, "s")
        assert [p.name for p in players] == ["A", "B"]

    def test_flex_eligibility_widens_the_list(self):
        league = _league()  # RB 2 + FLEX 1
        players, _, _ = trade.waiver_candidates(self._pool(), league, 9, 9, "s")
        assert [p.name for p in players if p.position == "RB"] == ["A", "B", "C"]

    def test_a_row_with_no_bye_or_no_rate_is_not_a_candidate(self):
        pool = [{"player": "Unsigned", "position": "RB", "per_game": 12.0, "bye_week": None},
                {"player": "Zero", "position": "RB", "per_game": 0.0, "bye_week": 6},
                {"player": "Real", "position": "RB", "per_game": 3.0, "bye_week": 6}]
        players, _, _ = trade.waiver_candidates(pool, self.RB_PAIR, 9, 9, "s")
        assert [p.name for p in players] == ["Real"]

    def test_a_scored_position_with_no_free_agent_is_an_error(self):
        _, _, errors = trade.waiver_candidates([], self.RB_PAIR, 1, 14, "espn")
        assert errors == ["no free agent at RB with a projection and a bye week in the "
                          "pool (espn)"]

    def test_the_board_pool_is_the_players_on_no_roster(self, fixture_board):
        rostered = {board.norm_name(n) for n in MINE + THEIRS}
        names = [r["player"] for r in trade.board_pool(fixture_board, rostered)]
        assert sorted(names) == ["Waiver QB", "Waiver RB", "Waiver TE", "Waiver WR"]

    def test_evaluate_names_the_free_agents_and_their_source(self, fixture_board, by_slot):
        out = trade.evaluate(fixture_board, by_slot, _league(), my_slot=1,
                             counterparty_slot=2, give=["Elite WR"], get=["Good WR"],
                             first_week=1, last_week=14, n_trials=10, blocks=2, seed=0)
        assert out["waiver"]["QB"]["source"] == trade.WAIVER_FROM_BOARD
        assert [c["player"] for c in out["waiver"]["QB"]["candidates"]] == ["Waiver QB"]

    def test_a_pool_passed_in_is_the_espn_source(self, fixture_board, by_slot):
        pool = [{"player": f"FA {p}", "position": p, "per_game": 2.0, "bye_week": 6}
                for p in ("QB", "RB", "WR", "TE")]
        out = trade.evaluate(fixture_board, by_slot, _league(), my_slot=1,
                             counterparty_slot=2, give=["Elite WR"], get=["Good WR"],
                             first_week=1, last_week=14, n_trials=10, blocks=2, seed=0,
                             pool=pool)
        assert out["waiver"]["TE"] == {"source": trade.WAIVER_FROM_ESPN,
                                       "candidates": [{"player": "FA TE", "per_game": 2.0,
                                                       "bye_week": 6}]}

    def test_an_empty_pool_refuses_the_evaluation(self, fixture_board, by_slot):
        out = trade.evaluate(fixture_board, by_slot, _league(), my_slot=1,
                             counterparty_slot=2, give=["Elite WR"], get=["Good WR"],
                             first_week=1, last_week=14, pool=[])
        assert out["ok"] is False
        assert sum("no free agent at" in e for e in out["errors"]) == 4


def _live(names: list[str]) -> list[dict]:
    return [{"name": n, "position": ROWS[n]["position"], "bye_week": ROWS[n]["bye_week"]}
            for n in names]


class TestLiveRostersAreKeyedByTeam:
    """Rosters read from ESPN are keyed by team id and carry every move since
    the draft; the counterparty's draft picks still supply their tendencies."""

    def test_sides_are_reported_and_named_by_team_id(self, fixture_board, by_slot):
        live = {3: _live(MINE), 2: _live(THEIRS)}
        out = trade.evaluate(fixture_board, live, _league(), my_slot=3, counterparty_slot=2,
                             give=["Elite WR"], get=["Good WR", "Okay WR"], first_week=1,
                             last_week=14, n_trials=10, blocks=2, seed=0,
                             roster_key="team_id", counterparty_picks=by_slot[2])
        assert out["ok"] is True, out.get("errors")
        assert out["you"]["team_id"] == 3 and out["counterparty"]["team_id"] == 2
        assert "slot" not in out["you"] and "slot" not in out["counterparty"]
        assert out["counterparty"]["verdict"].startswith("team 2")
        assert out["counterparty"]["tendencies"]["picks"] == len(THEIRS)

    def test_a_player_moved_since_the_draft_is_on_his_new_roster(self, fixture_board, by_slot):
        # "Their WR" was drafted by slot 2 and now sits on team 3. The draft
        # record would refuse to let team 3 give him; the live roster does not.
        live = {3: _live(MINE + ["Their WR"]),
                2: _live([n for n in THEIRS if n != "Their WR"])}
        out = trade.evaluate(fixture_board, live, _league(), my_slot=3, counterparty_slot=2,
                             give=["Their WR"], get=["Good WR"], first_week=1, last_week=14,
                             n_trials=10, blocks=2, seed=0, roster_key="team_id",
                             counterparty_picks=by_slot[2])
        assert out["ok"] is True, out.get("errors")
        refused = trade.evaluate(fixture_board, by_slot, _league(), my_slot=1,
                                 counterparty_slot=2, give=["Their WR"], get=["Good WR"],
                                 first_week=1, last_week=14)
        assert refused["ok"] is False

    def test_a_refusal_names_the_team_not_a_slot(self, fixture_board):
        live = {3: _live(MINE), 2: _live(THEIRS)}
        out = trade.evaluate(fixture_board, live, _league(), my_slot=3, counterparty_slot=2,
                             give=["Their RB"], get=["My QB"], first_week=1, last_week=14,
                             roster_key="team_id")
        assert out["ok"] is False
        assert any("(team 3)" in e for e in out["errors"])
        assert any("team 2's roster" in e for e in out["errors"])

    def test_no_draft_record_for_the_counterparty_is_said(self, fixture_board):
        live = {3: _live(MINE), 2: _live(THEIRS)}
        out = trade.evaluate(fixture_board, live, _league(), my_slot=3, counterparty_slot=2,
                             give=["Elite WR"], get=["Good WR"], first_week=1, last_week=14,
                             n_trials=10, blocks=2, seed=0, roster_key="team_id",
                             counterparty_picks=[])
        assert out["counterparty"]["tendencies"]["note"] == "no draft record for this team"


class TestParseOut:
    def test_weeks_ranges_and_lists(self):
        parsed, errors = trade.parse_out("Kyler Murray:2-3;6, Ja'Marr Chase:4")
        assert errors == []
        assert parsed == {"Kyler Murray": frozenset({2, 3, 6}),
                          "Ja'Marr Chase": frozenset({4})}

    def test_empty_text_is_no_absences(self):
        assert trade.parse_out("") == ({}, [])

    @pytest.mark.parametrize("text", ["Kyler Murray", "Kyler Murray:two", ":3",
                                      "Kyler Murray:4-2", "Kyler Murray:0",
                                      "Kyler Murray:17-19"])
    def test_a_malformed_absence_is_named_not_dropped(self, text):
        parsed, errors = trade.parse_out(text)
        assert parsed == {}
        assert len(errors) == 1
        assert repr(text) in errors[0]


class TestTheUnitIsNotThisModulesToChoose:
    """`adp.margin_unit` decides what the number may be called, and both the
    structured field and the sentence read that verdict rather than assuming.

    This harness fits nothing -- the blocks re-run one simulation on disjoint
    seeds and its inputs are already in points -- so it goes through the
    declared replication path rather than the fitted one. The declaration is
    what makes that legitimate, so the test that matters is the one where it is
    missing.
    """

    def test_a_declared_replication_may_say_points(self, fixture_board, by_slot):
        out = trade.evaluate(fixture_board, by_slot, _league(), my_slot=1,
                             counterparty_slot=2, give=["Elite WR"],
                             get=["Good WR", "Okay WR"], first_week=1, last_week=14,
                             n_trials=20, blocks=2, seed=0)
        yours = out["you"]
        assert yours["harness"] == adp.HARNESS_REPLICATION
        assert yours["unit"] == adp.UNIT_POINTS
        assert "points over" in yours["verdict"]
        # And says what the spread beside it does not cover. `adj_ppg` comes out
        # of `model.project`, which fits things; replicating the availability
        # draws does not move that error, so a reader taking the spread as the
        # error bar on the trade would be wrong.
        assert yours["spread_covers"] == adp.SPREAD_REPLICATION_ONLY
        assert "Spread is replication noise only, not the projection's error." \
            in yours["verdict"]

    def test_an_ordinal_verdict_takes_the_unit_off_the_number(self):
        # The gate from the caller's side. Same agreeing blocks, no declaration,
        # so the rule answers ordinal and the sentence may not call the size
        # points. It still states the size, labelled, because for this harness
        # the number is the answer -- withholding it leaves nothing.
        summary = {"improvement": 34.5, "block_improvements": [36.2, 32.9],
                   "block_spread": 3.3, "blocks_agree": True, "blocks_agree_p_null": 0.5,
                   **adp.margin_unit(True, None, 2, adp.HARNESS_REPLICATION)}
        said = trade.verdict(summary, "you", last_week=10)
        assert "34.5 (ordinal)" in said
        assert "points" not in said
        # And the sentence says which clause failed, not just that one did.
        assert "must declare the unit" in said

    def test_the_sentence_cannot_disagree_with_the_field(self):
        # Both readings come from one verdict, so there is no arrangement where
        # `unit` says ordinal and the prose prints a points figure.
        for beats, harness, unit in ((None, adp.HARNESS_FITTED, adp.UNIT_ORDINAL),
                                     ([True, True], adp.HARNESS_FITTED, adp.UNIT_POINTS)):
            summary = {"improvement": 12.0, "block_improvements": [13.0, 11.0],
                       "block_spread": 2.0, "blocks_agree": True,
                       "blocks_agree_p_null": 0.5,
                       **adp.margin_unit(True, beats, 2, harness)}
            said = trade.verdict(summary, "you", last_week=14)
            assert summary["unit"] == unit
            assert ("12.0 points" in said) is (unit == adp.UNIT_POINTS), said

    def test_a_no_call_obeys_the_unit_too(self):
        # The no-call branch printed "points" whatever `unit` said, in exactly the
        # branch where the number is least a quantity.
        base = {"improvement": 3.0, "block_improvements": [12.0, -6.0],
                "block_spread": 18.0, "blocks_agree": False, "blocks_agree_p_null": 0.5}
        ordinal = trade.verdict({**base, "unit": adp.UNIT_ORDINAL}, "you")
        assert "+3.0 (ordinal)" in ordinal and "points" not in ordinal
        unlabelled = trade.verdict(base, "you")
        assert "points" not in unlabelled
        points = trade.verdict({**base, "unit": adp.UNIT_POINTS}, "you")
        assert "+3.0 points" in points


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
                             counterparty_slot=2, give=["Their RB"], get=["Good WR"],
                             first_week=1, last_week=14)
        assert out["ok"] is False
        assert any("not on your roster" in e for e in out["errors"])

    def test_a_traded_name_with_no_board_row_stops_the_evaluation(self, fixture_board,
                                                                  by_slot):
        picks = {1: by_slot[1] + [{"overall": 99, "slot": 1, "name": "Ghost",
                                   "player_id": None, "position": "WR", "bye_week": 6}],
                 2: by_slot[2]}
        out = trade.evaluate(fixture_board, picks, _league(), my_slot=1,
                             counterparty_slot=2, give=["Ghost"], get=["Good WR"],
                             first_week=1, last_week=14,
                             espn_season={board.norm_name("Ghost"): 150.0})
        assert out["ok"] is False
        assert any("no board row for: Ghost" in e for e in out["errors"])
        assert any("priced only from the board" in e for e in out["errors"])

    def test_a_kicker_in_the_trade_is_refused(self, by_slot):
        # The lineup never scores K or DST, so a trade with one in it was valued
        # as if he were not in it.
        b = _board(FIXTURE + [{"name": "K Man", "position": "K", "adj_ppg": np.nan,
                               "exp_games": 17.0, "bye_week": 9, "proj_points": 150.0,
                               "adp": 150.0}])
        picks = {1: by_slot[1] + [{"overall": 90, "slot": 1, "name": "K Man",
                                   "player_id": None, "position": "K"}],
                 2: by_slot[2]}
        out = trade.evaluate(b, picks, _league(), my_slot=1, counterparty_slot=2,
                             give=["K Man"], get=["Good WR"], first_week=1, last_week=14)
        assert out["ok"] is False
        assert any("'K Man' plays K, which the simulated lineup does not score" in e
                   for e in out["errors"])

    def test_a_bystander_the_draft_record_cannot_price_refuses(self, fixture_board, by_slot):
        # A replacement-level stand-in was priced at a level with no bye week.
        # Without a live league there is no ESPN projection to price him from, so
        # the trade is not scored rather than scored around a guess.
        picks = {1: by_slot[1] + [{"overall": 99, "slot": 1, "name": "Bystander",
                                   "player_id": None, "position": "RB"}],
                 2: by_slot[2]}
        out = trade.evaluate(fixture_board, picks, _league(), my_slot=1,
                             counterparty_slot=2, give=["Elite WR"],
                             get=["Good WR", "Okay WR"], first_week=1, last_week=14)
        assert out["ok"] is False
        assert any(e.startswith("'Bystander' is on a roster and cannot be priced (no board "
                                "row, no ESPN projections without a live league, no bye week)")
                   for e in out["errors"])

    def test_a_bystander_espn_projects_is_priced_with_his_bye(self, fixture_board, by_slot):
        picks = {1: by_slot[1] + [{"overall": 99, "slot": 1, "name": "Bystander",
                                   "player_id": None, "position": "RB", "bye_week": 6}],
                 2: by_slot[2]}
        out = trade.evaluate(fixture_board, picks, _league(), my_slot=1,
                             counterparty_slot=2, give=["Elite WR"],
                             get=["Good WR", "Okay WR"], first_week=1, last_week=14,
                             n_trials=10, blocks=2, seed=0,
                             espn_season={board.norm_name("Bystander"): 170.0})
        assert out["ok"] is True, out.get("errors")
        mine = out["stand_ins"]["yours"]
        assert [s["basis"] for s in mine] == [trade.BASIS_ESPN_STAND_IN]
        # Weeks 1-14 minus his bye in week 6: thirteen games at 170 / 17.
        assert mine[0]["bye_week"] == 6
        assert mine[0]["points"] == pytest.approx(170.0 / roles.SEASON_GAMES * 13, abs=0.1)
        assert out["stand_ins"]["theirs"] == []
        note = out["you"]["spread_note"]
        assert note is not None and "Bystander" in note
        assert f"{mine[0]['points']:.0f} points" in note
        assert out["counterparty"]["spread_note"] is None

    def test_an_espn_stand_in_scores_nothing_on_his_bye(self):
        league = TestAnEmptySlotIsAFreeAgentNotZero.RB_PAIR
        players, missing = trade.resolve(_board(IRON), ["Bystander"],
                                         {"bystander": {"position": "RB", "bye_week": 6}},
                                         espn_season={"bystander": 170.0})
        assert missing == []
        assert trade.simulate_season(players, league, seed=0, first_week=6,
                                     last_week=6)["points"] == 0.0

    def test_a_bystander_espn_projects_without_a_bye_is_missing(self):
        players, missing = trade.resolve(_board(IRON), ["Bystander"],
                                         {"bystander": {"position": "RB", "bye_week": None}},
                                         espn_season={"bystander": 170.0})
        assert players == [] and missing == ["Bystander"]

    def test_a_bystander_espn_does_not_project_is_missing(self):
        players, missing = trade.resolve(_board(IRON), ["Bystander"],
                                         {"bystander": {"position": "RB", "bye_week": 6}},
                                         espn_season={"somebody else": 99.0})
        assert players == [] and missing == ["Bystander"]

    def test_a_defense_nothing_can_price_is_listed_and_does_not_refuse(
            self, fixture_board, by_slot):
        # No simulated lineup has a defense slot, so he moves neither total.
        picks = {1: by_slot[1] + [{"overall": 99, "slot": 1, "name": "Ravens D/ST",
                                   "player_id": None, "position": "DST"}],
                 2: by_slot[2]}
        out = trade.evaluate(fixture_board, picks, _league(), my_slot=1,
                             counterparty_slot=2, give=["Elite WR"],
                             get=["Good WR", "Okay WR"], first_week=1, last_week=14,
                             n_trials=10, blocks=2, seed=0)
        assert out["ok"] is True, out.get("errors")
        assert out["not_scored"] == [{"player": "Ravens D/ST", "position": "DST"}]
        assert out["stand_ins"]["yours"] == []

    def test_a_bystander_with_no_position_refuses(self, fixture_board, by_slot):
        # Dropping him silently shrank the roster under ok:true; a missing player
        # can change who starts, so the delta is not the trade's.
        picks = {1: by_slot[1] + [{"overall": 99, "slot": 1, "name": "Nobody",
                                   "player_id": None, "position": None}],
                 2: by_slot[2]}
        out = trade.evaluate(fixture_board, picks, _league(), my_slot=1,
                             counterparty_slot=2, give=["Elite WR"],
                             get=["Good WR", "Okay WR"], first_week=1, last_week=14,
                             espn_season={"nobody": 50.0})
        assert out["ok"] is False
        assert any(e.startswith("'Nobody' is on a roster and cannot be priced (no board row, "
                                "no position, no bye week)") for e in out["errors"])

    def test_a_board_player_with_no_bye_refuses(self, fixture_board, by_slot):
        b = fixture_board.copy()
        b.loc[b["name"] == "My TE", "bye_week"] = np.nan
        out = trade.evaluate(b, by_slot, _league(), my_slot=1, counterparty_slot=2,
                             give=["Elite WR"], get=["Good WR"], first_week=1, last_week=14)
        assert out["ok"] is False
        assert "'My TE' has no bye week, so he would score in the week he has no game" \
            in out["errors"]

    def test_an_empty_trade_is_refused(self, fixture_board, by_slot):
        out = trade.evaluate(fixture_board, by_slot, _league(), my_slot=1,
                             counterparty_slot=2, give=[], get=[], first_week=1,
                             last_week=14)
        assert out["ok"] is False
        assert any("at least one player" in e for e in out["errors"])
