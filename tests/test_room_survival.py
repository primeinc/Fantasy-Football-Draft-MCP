"""K and D/ST survival from the room's own take-rate rather than from ESPN ADP.

The live fault this covers: at pick 132 of the recorded draft the recommender
headlined the Houston Texans D/ST with a 0.11 chance of lasting 25 picks, in a
room that had taken one defense in 125. Both halves are wrong -- the number is a
false statement about the room, and it drove the headline.
"""
import math

import pandas as pd

from ffdraft import model
from ffdraft.board import DraftState
from ffdraft.config import LeagueSettings

DataFrame = model.pd.DataFrame


def _league():
    return LeagueSettings(name="t", teams=16, draft_slot=4, rounds=14,
                          starters={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 0,
                                    "K": 1, "DST": 1})


# The live room at pick 125: one K and one D/ST taken, everything else skill.
ROOM = {"WR": 47, "RB": 40, "QB": 19, "TE": 17, "K": 1, "DST": 1}
PICKS = 125


def _board():
    """Three defenses and three receivers, shaped like the live board at 132.

    The defenses carry an ADP the room has already blown past; the receivers do
    not. That gap is what makes ADP survival and the room's record disagree.
    """
    rows = []
    for i, (name, score, adp) in enumerate((("D1", 30.0, 93.0), ("D2", 25.0, 99.0),
                                            ("D3", 20.0, 104.0))):
        rows.append({"name": name, "_key": name.lower(), "position": "DST", "team": f"T{i}",
                     "adp": adp, "draft_score": score, "proj_points": 120.0 + score,
                     "consistency": 0.5, "bye_week": 8, "drafted": False})
    for i, (name, score, adp) in enumerate((("W1", 50.0, 140.0), ("W2", 45.0, 150.0),
                                            ("W3", 40.0, 160.0))):
        rows.append({"name": name, "_key": name.lower(), "position": "WR", "team": f"R{i}",
                     "adp": adp, "draft_score": score, "proj_points": 180.0 + score,
                     "consistency": 0.5, "bye_week": 9, "drafted": False})
    return DataFrame(rows)


# Every starting slot but K and D/ST is full, so the two counted positions carry
# the open-slot boost. This is the roster the live fault occurred on.
ROSTER = {"QB": 1, "RB": 2, "WR": 2, "TE": 1}


class TestForcedTakes:
    """The floor: what the league can no longer defer past your next pick."""

    def test_zero_while_the_remainder_can_still_absorb_every_slot(self):
        # 15 defenses to fill, 92 picks left, your next pick 25 away: all 15
        # could go in the 67 picks after it, so none is forced into the horizon.
        assert model.forced_takes(slots_left=15, picks_left=92, horizon=25) == 0

    def test_rises_once_the_tail_is_too_short_to_hold_them(self):
        # 28 picks left and 25 of them before your next: only 3 can come after,
        # so 12 of the 15 are forced inside.
        assert model.forced_takes(slots_left=15, picks_left=28, horizon=25) == 12

    def test_is_monotone_in_the_horizon(self):
        seen = [model.forced_takes(15, 40, h) for h in range(0, 41)]
        assert seen == sorted(seen), seen
        assert seen[0] == 0 and seen[-1] == 15

    def test_a_position_with_nothing_left_to_fill_forces_nothing(self):
        assert model.forced_takes(slots_left=0, picks_left=4, horizon=4) == 0


class TestExpectedTakers:
    """How many of the position go before your next turn."""

    def test_is_the_observed_rate_over_the_horizon_while_nothing_is_forced(self):
        # 1 defense in 125 picks, 25 picks to your next turn.
        assert abs(model.expected_takers(1 / 125, 25, 15, 92) - 25 / 125) < 1e-12

    def test_the_floor_takes_over_at_the_end(self):
        # 15 defenses into 28 remaining picks with your next turn 25 away: 12
        # cannot wait, and that is far above anything the room's rate predicts.
        assert model.expected_takers(1 / 125, 25, 15, 28) == 12.0

    def test_no_horizon_is_no_takers(self):
        assert model.expected_takers(1 / 125, 0, 15, 1) == 0.0


class TestCountingSurvival:
    """P(K <= i) by index: if k go, the k best go."""

    def test_the_top_player_carries_the_whole_risk(self):
        # With 0.2 defenses expected, the best one survives with P(K = 0) and
        # the second is nearly safe. A flat number would give them the same.
        p = model.counting_survival(0.2, 5)
        assert abs(p[0] - math.exp(-0.2)) < 1e-12
        assert p[0] < p[1] < p[2]
        assert 0.80 < p[0] < 0.83, p[0]
        assert p[1] > 0.98

    def test_rises_with_index_and_never_leaves_the_unit_interval(self):
        for takers in (0.0, 0.2, 4.0, 12.0, 40.0):
            p = model.counting_survival(takers, 31)
            assert list(p) == sorted(p), (takers, p)
            assert (p >= 0).all() and (p <= 1).all(), (takers, p)

    def test_a_forced_end_of_draft_buries_the_top_of_the_board(self):
        # 12 expected takers: the best twelve defenses are gone, and the top one
        # is not close to surviving.
        p = model.counting_survival(12.0, 31)
        assert p[0] < 0.001, p[0]
        assert p[11] < 0.65 and p[20] > 0.98, (p[11], p[20])

    def test_no_takers_means_everyone_survives(self):
        p = model.counting_survival(0.0, 4)
        assert (p == 1.0).all(), p

    def test_an_empty_position_returns_an_empty_array(self):
        assert len(model.counting_survival(3.0, 0)) == 0


class TestRecommend:
    """What the room record does to the ranking the user actually sees."""

    def test_a_deferring_room_stops_the_defense_headlining(self):
        board, league = _board(), _league()
        before = model.recommend(board, league, current_pick=132, next_pick=157,
                                 roster=ROSTER, top_n=6)
        after = model.recommend(board, league, current_pick=132, next_pick=157,
                                roster=ROSTER, top_n=6,
                                room_picks=ROOM, picks_so_far=PICKS)
        # The fault, reproduced: on ADP the defense outranks every receiver.
        assert before["name"].iloc[0] == "D1", before[["name", "pick_value"]]
        # And repaired: waiting is nearly free, so the receiver goes first.
        assert after["name"].iloc[0] == "W1", after[["name", "pick_value"]]
        d1 = after.set_index("name").loc["D1"]
        assert d1["pick_value"] < before.set_index("name").loc["D1", "pick_value"]

    def test_the_defense_becomes_urgent_again_at_the_end(self):
        # Pick 196 with your last at 221: 15 defenses must fit in 28 picks.
        board, league = _board(), _league()
        out = model.recommend(board, league, current_pick=196, next_pick=221,
                              roster=ROSTER, top_n=6,
                              room_picks=ROOM, picks_so_far=PICKS)
        assert out["name"].iloc[0] == "D1", out[["name", "pick_value"]]
        assert float(out.set_index("name").loc["D1", "p_available_next"]) < 0.01

    def test_only_the_counted_positions_move(self):
        board, league = _board(), _league()
        before = model.recommend(board, league, current_pick=132, next_pick=157,
                                 roster=ROSTER, top_n=6).set_index("name")
        after = model.recommend(board, league, current_pick=132, next_pick=157,
                                roster=ROSTER, top_n=6,
                                room_picks=ROOM, picks_so_far=PICKS).set_index("name")
        for wr in ("W1", "W2", "W3"):
            assert (before.loc[wr, "p_available_next"]
                    == after.loc[wr, "p_available_next"]), wr
        for dst in ("D1", "D2", "D3"):
            assert before.loc[dst, "p_available_next"] != after.loc[dst, "p_available_next"]

    def test_the_defenses_do_not_all_get_the_same_number(self):
        # The flat form this replaced gave the best defense and the worst the
        # same survival, which is false in the direction that decides the pick.
        board, league = _board(), _league()
        out = model.recommend(board, league, current_pick=132, next_pick=157,
                              roster=ROSTER, top_n=6,
                              room_picks=ROOM, picks_so_far=PICKS).set_index("name")
        d1, d2, d3 = (float(out.loc[n, "p_available_next"]) for n in ("D1", "D2", "D3"))
        assert d1 < d2 < d3, (d1, d2, d3)

    def test_without_the_room_record_nothing_changes(self):
        # Every replay, backtest and simulation calls recommend without it, and
        # their published numbers must not move because this landed.
        board, league = _board(), _league()
        a = model.recommend(board, league, current_pick=132, next_pick=157,
                            roster=ROSTER, top_n=6)
        b = model.recommend(board, league, current_pick=132, next_pick=157,
                            roster=ROSTER, top_n=6, room_picks=None, picks_so_far=0)
        assert a["name"].tolist() == b["name"].tolist()
        assert model.np.allclose(a["pick_value"], b["pick_value"])

    def test_an_empty_record_is_not_a_rate_of_zero(self):
        # Before the draft starts there is no room to read, and a rate of 0/0
        # must not become "survives with certainty".
        board, league = _board(), _league()
        a = model.recommend(board, league, current_pick=1, next_pick=32,
                            roster={}, top_n=6)
        b = model.recommend(board, league, current_pick=1, next_pick=32,
                            roster={}, top_n=6, room_picks={}, picks_so_far=0)
        assert model.np.allclose(a["pick_value"], b["pick_value"])

    def test_the_why_string_names_the_record_behind_the_number(self):
        board, league = _board(), _league()
        out = model.recommend(board, league, current_pick=132, next_pick=157,
                              roster=ROSTER, top_n=6,
                              room_picks=ROOM, picks_so_far=PICKS).set_index("name")
        why = model.explain(out.loc["D1"])
        assert "room has taken 1 of 16 DST in 125 picks" in why, why
        # A receiver is still priced off ADP, so it must not claim otherwise.
        assert "room has taken" not in model.explain(out.loc["W1"])


class TestPicksByPosition:
    def test_counts_the_board_spelling_and_falls_back_to_the_record(self, tmp_path,
                                                                    monkeypatch):
        from ffdraft import board as bd

        monkeypatch.setattr(bd, "STATE_DIR", tmp_path)
        st = DraftState(_league(), name="counts")
        st.record("D1", overall=1, team_slot=1)
        st.record("W1", overall=2, team_slot=2)
        # A kicker the board does not carry: the recorded position is all there is.
        st.record("Some Kicker", overall=3, team_slot=3, position="K")
        assert st.picks_by_position(_board()) == {"DST": 1, "WR": 1, "K": 1}

    def test_a_pick_with_no_position_anywhere_is_not_counted(self, tmp_path, monkeypatch):
        from ffdraft import board as bd

        monkeypatch.setattr(bd, "STATE_DIR", tmp_path)
        st = DraftState(_league(), name="unknown")
        st.record("Nobody At All", overall=1, team_slot=1)
        assert st.picks_by_position(_board()) == {}


def test_the_column_is_present_even_when_the_record_is_not():
    # explain() reads survival_source off the row; a caller that never passes a
    # room record must still get a row it can explain.
    board, league = _board(), _league()
    out = model.recommend(board, league, current_pick=132, next_pick=157,
                          roster=ROSTER, top_n=3)
    assert "survival_source" in out.columns
    assert (out["survival_source"] == "").all()
    assert isinstance(model.explain(out.iloc[0]), str)


def test_no_next_pick_still_carries_the_column():
    board, league = _board(), _league()
    out = model.recommend(board, league, current_pick=224, next_pick=None,
                          roster=ROSTER, top_n=3, room_picks=ROOM, picks_so_far=PICKS)
    assert "survival_source" in out.columns
    assert pd.notna(out["p_available_next"]).all()
