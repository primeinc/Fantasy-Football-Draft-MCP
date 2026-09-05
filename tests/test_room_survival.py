"""K and D/ST survival from the room's own take-rate rather than from ESPN ADP.

The live fault this covers: at pick 132 of the recorded draft the recommender
headlined the Houston Texans D/ST with a 0.11 chance of lasting 25 picks, in a
room that had taken one defense in 125. Both halves are wrong -- the number is a
false statement about the room, and it drove the headline.
"""
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


class TestPickHazards:
    """One hazard per pick between now and your next turn."""

    def test_nobody_is_compelled_in_the_middle_of_the_draft(self):
        # At 132 every team still has far more picks left than counted slots to
        # fill, so the room's own habit is the only evidence.
        hz = model.pick_hazards(_league(), {}, 132, 157, "DST", 1 / 125)
        assert len(hz) == 24, len(hz)
        assert all(h == 1 / 125 for h in hz), hz

    def test_everyone_is_compelled_at_the_end(self):
        # By 197 no team has more picks left than the two slots it must fill, so
        # every pick is forced and splits between K and D/ST.
        hz = model.pick_hazards(_league(), {}, 196, 221, "DST", 1 / 125)
        assert len(hz) == 24
        assert all(h == 0.5 for h in hz), hz

    def test_a_team_that_already_holds_one_crosses_the_boundary_later(self):
        # The whole point of reading it per team. Slot 5 picks at 197 and 220
        # and already holds a kicker, so at 197 it has two picks against one
        # unfilled slot and can still defer, where its neighbours cannot. By
        # 220 it is down to one pick and must take the defense.
        league = _league()
        hz = model.pick_hazards(league, {5: {"K": 1}}, 196, 221, "DST", 1 / 125)
        by_pick = dict(zip(range(197, 221), hz))
        assert model.slot_for_pick(league, 197) == 5
        assert model.slot_for_pick(league, 220) == 5
        assert by_pick[197] == 1 / 125, by_pick[197]
        assert by_pick[220] == 1.0, by_pick[220]
        # A neighbour holding nothing is compelled at its own first pick here.
        assert by_pick[198] == 0.5, by_pick[198]

    def test_the_horizon_is_walked_rather_than_read_at_its_first_pick(self):
        # A horizon that straddles the boundary. Reading the forced count once
        # at 189 reports nothing forced; the picks late in the window are.
        hz = model.pick_hazards(_league(), {}, 189, 196, "DST", 1 / 125)
        assert len(hz) == 6
        assert min(hz) == 1 / 125 and max(hz) == 0.5, hz
        assert sum(hz) > 1.0, sum(hz)

    def test_a_position_already_filled_everywhere_forces_nothing(self):
        held = {slot: {"K": 1, "DST": 1} for slot in range(1, 17)}
        hz = model.pick_hazards(_league(), held, 196, 221, "DST", 1 / 125)
        assert all(h == 1 / 125 for h in hz), hz


class TestCountingSurvival:
    """P(K <= i) by index: if k go, the k best go."""

    def test_the_top_player_carries_the_whole_risk(self):
        # 24 picks at the room's rate. The best defense survives with P(K = 0)
        # and the second is nearly safe; a flat number would give them the same.
        hz = [1 / 125] * 24
        p = model.counting_survival(hz, 5, 15)
        assert abs(p[0] - (1 - 1 / 125) ** 24) < 1e-12
        assert p[0] < p[1] < p[2]
        assert 0.80 < p[0] < 0.84, p[0]
        assert p[1] > 0.98

    def test_rises_with_index_and_never_leaves_the_unit_interval(self):
        for hz in ([], [0.0] * 5, [1 / 125] * 24, [0.5] * 24, [1.0] * 24):
            p = model.counting_survival(hz, 31, 15)
            assert list(p) == sorted(p), (hz[:2], p)
            assert (p >= 0).all() and (p <= 1).all(), (hz[:2], p)

    def test_a_forced_end_of_draft_buries_the_top_of_the_board(self):
        p = model.counting_survival([0.5] * 24, 31, 15)
        assert p[0] < 0.001, p[0]
        assert p[20] > 0.99, p[20]

    def test_puts_no_mass_above_the_picks_that_exist(self):
        # The reason this is a Poisson binomial and not a Poisson: with 24 picks
        # at hazard 0.5, more than 24 cannot go, and everyone from index 24 on
        # is certain. A Poisson on the same mean leaves mass above it.
        p = model.counting_survival([0.5] * 24, 40, None)
        assert p[24] == 1.0, p[24]
        assert p[23] < 1.0

    def test_truncating_at_the_slots_left_is_what_caps_it(self):
        # The league cannot take more defenses than it has defense slots open.
        loose = model.counting_survival([0.5] * 24, 40, None)
        capped = model.counting_survival([0.5] * 24, 40, 15)
        assert capped[15] == 1.0, capped[15]
        assert capped[12] > loose[12]

    def test_no_takers_means_everyone_survives(self):
        p = model.counting_survival([0.0] * 10, 4, 15)
        assert (p == 1.0).all(), p

    def test_an_empty_position_returns_an_empty_array(self):
        assert len(model.counting_survival([0.5] * 4, 0, 15)) == 0


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
