import pandas as pd
import pytest

from ffdraft import board, replay
from ffdraft.config import LeagueSettings


def _board():
    b = pd.DataFrame({
        "name": ["RB One", "RB Two", "WR One", "WR Two", "QB One", "TE One"],
        "position": ["RB", "RB", "WR", "WR", "QB", "TE"],
        "team": ["A"] * 6,
        "proj_points": [300.0, 250.0, 240.0, 200.0, 320.0, 150.0],
        "draft_score": [300.0, 250.0, 240.0, 200.0, 150.0, 100.0],
        "adp": [1.0, 3.0, 2.0, 6.0, 20.0, 30.0],
        "pos_rank": [1, 2, 1, 2, 1, 1], "overall_rank": [1, 3, 2, 5, 4, 6],
        "consistency": [0.5] * 6, "adj_ppg": [15.0] * 6,
    })
    b["_key"] = b["name"].map(board.norm_name)
    return b


def test_replay_scores_each_pick_and_calibrates(tmp_path, monkeypatch):
    monkeypatch.setattr(board, "STATE_DIR", tmp_path)
    league = LeagueSettings(name="t", teams=2, rounds=3, draft_slot=1,
                            starters={"QB": 1, "RB": 1, "WR": 1, "TE": 0, "FLEX": 0,
                                      "K": 0, "DST": 0})
    st = board.DraftState(league)
    st.record("WR Two", 1, 1)          # slot 1 passes on RB One
    st.record("RB One", 2, 2)
    st.record("Some Kicker", 3, 2, position="K")
    st.record("RB Two", 4, 1)

    out = replay.replay_draft(_board(), st, league, candidates=3)
    assert out["picks_scored"] == 4
    p = {r["pick"]: r for r in out["picks"]}
    assert p[1]["actual"] == "WR Two" and p[1]["actual_rank"] > 1
    assert p[1]["model_pick"] == "RB One"
    assert p[1]["proj_gap"] == 100.0 and p[1]["reach"] == 5.0
    assert p[1]["market_z"] == 1.0                      # 5 picks early / sd floor 5
    assert p[1]["pick_regret"] > 0 and 0.0 <= p[1]["choice_percentile"] < 1.0
    assert p[1]["need_mult"] > 0 and p[1]["role_mult"] == 1.0
    assert 0.0 <= p[1]["p_available_next"] <= 1.0
    assert p[2]["actual_rank"] == 1 and p[2]["proj_gap"] == 0.0
    assert p[2]["pick_regret"] == 0.0 and p[2]["choice_percentile"] == 1.0
    assert p[3]["off_board"] and p[3]["actual_rank"] is None and p[3]["position"] == "K"
    assert p[4]["slot"] == 1

    teams = {t["slot"]: t for t in out["teams"]}
    assert teams[2]["model_matches"] == 1 and teams[2]["off_board"] == 1
    assert teams[1]["picks"] == 2 and teams[1]["proj_left_on_table"] >= 100.0
    assert teams[1]["mine"] is True

    o = out["overall"]
    assert o["on_board_picks"] == 3 and o["off_board_picks"] == 1
    # Forecasts exist for picks whose team picked again inside the record:
    # pick 1 (slot 1 next at 4) and pick 2 (slot 2 next at 3), 3 candidates each.
    assert o["survival_forecasts"] == 6
    assert 0.0 <= o["survival_brier"] <= 1.0 and o["survival_log_loss"] > 0
    assert sum(c["n"] for c in o["survival_calibration"]) == 6
    assert sum(r["n"] for r in o["survival_by_round"]) == 6
    assert sum(r["n"] for r in o["survival_by_position"]) == 6
    assert o["biggest_reaches"][0]["actual"] == "WR Two"
    assert o["biggest_regrets"][0]["actual"] == "WR Two"
    assert o["biggest_regrets"][0]["model_pick"] == "RB One"
    # Reaches: WR Two 6-1=5, RB One 1-2=-1, RB Two 3-4=-1 -> median -1. Under
    # DRIFT_MIN_PICKS at every position, so each position's shift is the room's.
    d = out["room_drift"]
    assert (d["median_reach"], d["mean_reach"], d["n"]) == (-1.0, 1.0, 3)
    assert d["by_position"] == {"RB": {"median": -1.0, "n": 2}, "WR": {"median": 5.0, "n": 1}}
    assert d["shift"] == {"RB": -1.0, "WR": -1.0}


def test_room_drift_uses_a_positions_own_median_once_it_has_enough_picks(tmp_path, monkeypatch):
    monkeypatch.setattr(board, "STATE_DIR", tmp_path)
    monkeypatch.setattr(replay, "DRIFT_MIN_PICKS", 2)
    league = LeagueSettings(name="t", teams=2, rounds=3, draft_slot=1)
    st = board.DraftState(league)
    st.record("WR Two", 1, 1)
    st.record("RB One", 2, 2)
    st.record("RB Two", 3, 2)
    d = replay.room_drift(_board(), st)
    # Reaches 5, -1, 0: room median 0. RB has two picks (median -0.5); WR one
    # pick, so it takes the room's.
    assert d["shift"] == {"RB": -0.5, "WR": 0.0}
    b = _board()
    plain = model_recs(b, league, 0.0)
    per_pos = model_recs(b, league, d["shift"])
    # RB shift is negative (they go after ADP here), so RB survival rises; a
    # position with no shift entry is untouched.
    assert per_pos["RB Two"] > plain["RB Two"]
    assert per_pos["QB One"] == plain["QB One"]


def test_predict_pick_follows_the_list_a_team_follows(tmp_path, monkeypatch):
    monkeypatch.setattr(board, "STATE_DIR", tmp_path)
    league = LeagueSettings(name="t", teams=2, rounds=3, draft_slot=1,
                            starters={"QB": 1, "RB": 1, "WR": 1, "TE": 0, "FLEX": 0,
                                      "K": 0, "DST": 0})
    b = _board()
    # ESPN's list disagrees with the model: it likes QB One first, TE One last.
    b["espn_rank"] = [3, 4, 2, 5, 1, 6]
    st = board.DraftState(league)
    st.record("QB One", 1, 1)      # slot 1 took ESPN's #1: passed 0
    st.record("RB One", 2, 2)      # slot 2 took ESPN's #3 with #2 available: passed 1
    st.record("WR One", 3, 2)      # slot 2 again, on the clock now is pick 4 = slot 1

    out = replay.predict_pick(b, st, league, slot=1)
    assert out["on_the_clock"] == 4 and out["next_pick"] == 5
    assert out["roster"] == {"QB": 1}
    assert out["tendency"]["median_espn_passes"] == 0.0 and out["tendency"]["follows_espn_list"]
    assert [e["player"] for e in out["espn_list"]][:2] == ["RB Two", "WR Two"]
    assert out["predicted"] == {"player": "RB Two", "position": "RB",
                                "basis": "ESPN list order at an open starting slot"}
    assert out["should"][0]["player"] == "RB Two"
    assert [h["espn_passes"] for h in out["history"]] == [0]

    t = replay.team_tendency(b, st, 2)
    assert [h["espn_passes"] for h in t["picks"]] == [1, 0]
    assert t["positions"] == {"RB": 1, "WR": 1}


def model_recs(b, league, shift):
    from ffdraft import model

    out = model.recommend(b, league, current_pick=10, next_pick=20, top_n=6, adp_shift=shift)
    return out.set_index("name")["p_available_next"]


def test_room_drift_last_n_and_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(board, "STATE_DIR", tmp_path)
    league = LeagueSettings(name="t", teams=2, rounds=3, draft_slot=1)
    st = board.DraftState(league)
    assert replay.room_drift(_board(), st) == {"median_reach": 0.0, "mean_reach": 0.0, "n": 0,
                                               "by_position": {}, "shift": {}}
    st.record("WR Two", 1, 1)
    st.record("RB One", 2, 2)
    st.record("RB Two", 3, 2)
    # RB One 1-2 = -1, RB Two 3-3 = 0.
    d = replay.room_drift(_board(), st, last=2)
    assert (d["median_reach"], d["mean_reach"], d["n"]) == (-0.5, -0.5, 2)


def test_adp_shift_lowers_survival_odds():
    from ffdraft import model

    league = LeagueSettings(name="t", teams=16)
    b = _board()
    plain = model.recommend(b, league, current_pick=10, next_pick=20, top_n=6)
    shifted = model.recommend(b, league, current_pick=10, next_pick=20, top_n=6, adp_shift=8.0)
    p0 = plain.set_index("name")["p_available_next"]
    p8 = shifted.set_index("name")["p_available_next"]
    assert p8["TE One"] < p0["TE One"] and p8["QB One"] < p0["QB One"]


def _twin_board() -> pd.DataFrame:
    """Two real players under one normalised key at different positions, which a
    position-aware market join lets onto the board."""
    b = _board()
    twin = pd.DataFrame({
        "name": ["Alex Twin", "Alex Twin"], "position": ["RB", "TE"], "team": ["B", "B"],
        "proj_points": [260.0, 90.0], "draft_score": [260.0, 90.0], "adp": [4.0, 40.0],
        "pos_rank": [3, 2], "overall_rank": [4, 7], "consistency": [0.5, 0.5],
        "adj_ppg": [15.0, 15.0],
    })
    twin["_key"] = twin["name"].map(board.norm_name)
    return pd.concat([b, twin], ignore_index=True)


def test_replay_holds_a_same_name_pair_as_two_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(board, "STATE_DIR", tmp_path)
    league = LeagueSettings(name="t", teams=2, rounds=3, draft_slot=1,
                            starters={"QB": 1, "RB": 1, "WR": 1, "TE": 0, "FLEX": 0,
                                      "K": 0, "DST": 0})
    st = board.DraftState(league)
    st.record("Alex Twin", 1, 1, position="RB")   # the RB row, 260 points
    st.record("RB One", 2, 2)
    st.record("Alex Twin", 3, 1, position="TE")   # the TE row, 90 points

    out = replay.replay_draft(_twin_board(), st, league, candidates=3)
    p = {r["pick"]: r for r in out["picks"]}
    # Both picks score, and each against its own row. Keyed by name, pick 1 would
    # have taken both rows out of the pool and pick 3 would have read off_board.
    assert p[1]["off_board"] is False and p[1]["actual_proj"] == 260.0
    assert p[3]["off_board"] is False and p[3]["actual_proj"] == 90.0
    assert p[1]["position"] == "RB" and p[3]["position"] == "TE"
    # Pick 1 took the RB row at ADP 4, pick 3 the TE row at ADP 40.
    assert p[1]["reach"] == 3.0 and p[3]["reach"] == 37.0
    assert out["overall"]["off_board_picks"] == 0

    # And the pool really lost one row at a time: at pick 2 the TE twin is still
    # a candidate the model can name.
    everyone = replay.replay_draft(_twin_board(), st, league, candidates=100)
    assert everyone["picks_scored"] == 3


def test_replay_reports_the_picks_whose_row_it_had_to_guess(tmp_path, monkeypatch):
    monkeypatch.setattr(board, "STATE_DIR", tmp_path)
    league = LeagueSettings(name="t", teams=2, rounds=3, draft_slot=1,
                            starters={"QB": 1, "RB": 1, "WR": 1, "TE": 0, "FLEX": 0,
                                      "K": 0, "DST": 0})
    st = board.DraftState(league)
    # No position recorded, and the name is on two rows: the row is a coin flip
    # and the replay says so rather than pretending it knew.
    st.record("Alex Twin", 1, 1)
    st.record("RB One", 2, 2)
    out = replay.replay_draft(_twin_board(), st, league, candidates=3)
    assert out["ambiguous_name_picks"] == [1]

    # With the position recorded there is nothing to guess.
    st2 = board.DraftState(league)
    st2.record("Alex Twin", 1, 1, position="TE")
    st2.record("RB One", 2, 2)
    assert replay.replay_draft(_twin_board(), st2, league)["ambiguous_name_picks"] == []
    # And a name on one row is never a guess, position or not.
    st3 = board.DraftState(league)
    st3.record("RB One", 1, 1)
    assert replay.replay_draft(_board(), st3, league)["ambiguous_name_picks"] == []


def test_lineup_value_refuses_a_projection_without_a_position(tmp_path, monkeypatch):
    monkeypatch.setattr(board, "STATE_DIR", tmp_path)
    league = LeagueSettings(name="t", teams=2, rounds=3, draft_slot=1,
                            starters={"QB": 1, "RB": 1, "WR": 1, "TE": 0, "FLEX": 0,
                                      "K": 0, "DST": 0})
    # Both or neither. Resolving the position by name here would reintroduce the
    # ambiguity the projection was passed to close.
    with pytest.raises(ValueError, match="carries proj_points but no position"):
        board.lineup_value(_board(), [{"name": "RB One", "proj_points": 300.0}], league)
    # Neither is the recorded-pick case and still works.
    assert board.lineup_value(_board(), [{"name": "RB One", "position": "RB"}],
                              league)["starters_proj"] == 300


def test_lineup_value_treats_a_nan_projection_as_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(board, "STATE_DIR", tmp_path)
    league = LeagueSettings(name="t", teams=2, rounds=3, draft_slot=1,
                            starters={"QB": 1, "RB": 1, "WR": 1, "TE": 0, "FLEX": 0,
                                      "K": 0, "DST": 0})
    b = _board()
    b.loc[b["name"] == "WR One", "proj_points"] = float("nan")
    picks = [{"name": "RB One", "position": "RB"}, {"name": "WR One", "position": "WR"}]
    v = board.lineup_value(b, picks, league)
    # NaN is truthy, so `proj.get(key) or 0.0` would have returned it and made
    # the whole total NaN rather than counting the unprojected starter as 0.
    assert v["starters_proj"] == 300
    assert v["bench_proj"] == 0 and v["picks"] == 2
    # The slot is still filled by him -- he is on the roster, he is just worth 0.
    assert v["open_starter_slots"] == 1


class TestAsOfReplay:
    """The snapshots the watch files, read back by a replay. Written here, not
    by a watch: opening the draft socket is not something a test may do."""

    def _league(self) -> LeagueSettings:
        return LeagueSettings(name="t", teams=2, rounds=3, draft_slot=1,
                              starters={"QB": 1, "RB": 1, "WR": 1, "TE": 0, "FLEX": 0,
                                        "K": 0, "DST": 0})

    def _state(self, tmp_path, monkeypatch) -> board.DraftState:
        monkeypatch.setattr(board, "STATE_DIR", tmp_path)
        st = board.DraftState(self._league())
        st.record("WR Two", 1, 1)
        st.record("RB One", 2, 2)
        st.record("RB Two", 3, 2)
        return st

    def _write(self, league_id: str, pick: int, keys: list[str], adp: list[float]) -> None:
        from ffdraft import watch

        watch.write_snapshot(
            pd.DataFrame({"_key": keys, "adp": adp, "name": keys, "espn_rank": [1] * len(keys)}),
            set(), league_id, pick)

    def test_as_of_prices_a_pick_from_its_snapshot_and_reports_coverage(
            self, tmp_path, monkeypatch):
        from ffdraft import watch

        monkeypatch.setattr(watch, "STATE_DIR", tmp_path)
        st = self._state(tmp_path, monkeypatch)
        root = watch.snapshot_dir("snaps")
        # Only pick 1 was snapshotted, and in it WR Two went at ADP 1 rather
        # than today's 6, so the reach the replay scores changes.
        self._write("snaps", 1, ["wr two", "rb one"], [1.0, 3.0])

        plain = replay.replay_draft(_board(), st, self._league())
        aged = replay.replay_draft(_board(), st, self._league(), as_of=True, snapshots=root)
        assert {r["pick"]: r["reach"] for r in plain["picks"]}[1] == 5.0
        assert {r["pick"]: r["reach"] for r in aged["picks"]}[1] == 0.0

        cover = aged["as_of"]
        assert cover["picks"] == 3 and cover["picks_with_snapshot"] == 1
        assert cover["coverage"] == round(1 / 3, 3)
        assert cover["first_pick_with_snapshot"] == cover["last_pick_with_snapshot"] == 1
        assert cover["actual_pick_covered"] == 1
        assert cover["picks_without_snapshot"] == [2, 3]
        # Two of the six board rows were in the snapshot.
        assert cover["mean_pool_share"] == round(2 / 6, 3)
        # The picks with no snapshot are priced with today's numbers, unchanged.
        assert ({r["pick"]: r["reach"] for r in aged["picks"]}[2]
                == {r["pick"]: r["reach"] for r in plain["picks"]}[2])

    def test_a_player_the_snapshot_never_reached_keeps_todays_numbers(
            self, tmp_path, monkeypatch):
        from ffdraft import watch

        monkeypatch.setattr(watch, "STATE_DIR", tmp_path)
        st = self._state(tmp_path, monkeypatch)
        root = watch.snapshot_dir("snaps")
        # A snapshot that reached only RB One: WR Two is uncovered, so pick 1 is
        # scored against today's ADP for him and the coverage says so.
        self._write("snaps", 1, ["rb one"], [30.0])
        aged = replay.replay_draft(_board(), st, self._league(), as_of=True, snapshots=root)
        assert {r["pick"]: r["reach"] for r in aged["picks"]}[1] == 5.0
        cover = aged["as_of"]
        assert cover["picks_with_snapshot"] == 1 and cover["actual_pick_covered"] == 0

    def test_as_of_without_a_snapshot_location_is_refused(self, tmp_path, monkeypatch):
        st = self._state(tmp_path, monkeypatch)
        with pytest.raises(ValueError, match="as_of needs"):
            replay.replay_draft(_board(), st, self._league(), as_of=True)


def _counterfactual_league() -> LeagueSettings:
    return LeagueSettings(name="t", teams=2, rounds=3, draft_slot=1,
                          starters={"QB": 1, "RB": 1, "WR": 1, "TE": 0, "FLEX": 0,
                                    "K": 0, "DST": 0})


def _counterfactual_state(tmp_path, monkeypatch) -> board.DraftState:
    monkeypatch.setattr(board, "STATE_DIR", tmp_path)
    st = board.DraftState(_counterfactual_league())
    st.record("WR Two", 1, 1)          # slot 1 passes on RB One, the model's choice
    st.record("RB One", 2, 2)
    st.record("Some Kicker", 3, 2, position="K")
    st.record("RB Two", 4, 1)
    return st


def test_counterfactual_substitutes_the_models_pick_and_moves_the_pool(tmp_path, monkeypatch):
    st = _counterfactual_state(tmp_path, monkeypatch)
    out = replay.counterfactual_draft(_board(), st, _counterfactual_league(), slot=1)

    assert out["simulation"] is True
    assert "not a measurement" in out["note"]
    assert (out["slot"], out["mine"], out["policy"], out["picks_replayed"]) == (1, True, "argmax", 4)

    subs = {s["pick"]: s for s in out["substitutions"]}
    assert set(subs) == {1, 4}                      # only slot 1's turns are substituted
    assert subs[1]["real"] == "WR Two" and subs[1]["real_proj"] == 200.0
    assert subs[1]["model"] == "RB One" and subs[1]["model_proj"] == 300.0
    assert subs[1]["same"] is False
    assert subs[1]["basis"] == "model recommendation for the simulated roster"
    assert subs[1]["control"] == "WR Two" and subs[1]["control_is_real"] is True
    assert out["substitutions_made"] == 2

    # RB One goes to slot 1 at pick 1 in the simulation, so he is not there for
    # slot 2 at pick 2: the substitution really moved the pool.
    assert [r["player"] for r in out["model_roster"]][0] == "RB One"
    assert [r["player"] for r in out["real_roster"]] == ["WR Two", "RB Two"]

    # The kicker is off the board: mirrored for the other team, not predicted.
    d = out["divergence"]
    assert d["mirrored_off_board"] == 1 and d["pool_exhausted"] == 0
    assert d["other_team_picks"] == 1 and d["other_team_picks_changed"] <= 1

    # Real starters: WR Two 200 + RB Two 250, the QB slot left empty.
    assert out["starters_proj"]["real"] == 450
    assert out["open_starter_slots"]["real"] == 1
    s = out["starters_proj"]
    assert s["delta_vs_control"] == s["model"] - s["control"]
    assert s["delta_vs_real"] == s["model"] - s["real"]
    assert "delta" not in s                       # the unqualified delta was the confound
    # Nothing was written back: the recorded draft is untouched.
    assert [p["name"] for p in st.picks] == ["WR Two", "RB One", "Some Kicker", "RB Two"]


def test_counterfactual_control_arm_holds_the_room_fixed(tmp_path, monkeypatch):
    st = _counterfactual_state(tmp_path, monkeypatch)
    out = replay.counterfactual_draft(_board(), st, _counterfactual_league(), slot=1)
    # The control drafts slot 1's real players inside the predictor's room, so
    # its roster is the real one whenever those players are still there.
    control = [r["player"] for r in out["control_roster"]]
    assert control[0] == "WR Two"
    assert len(control) == len(out["real_roster"]) == len(out["model_roster"])
    # Where the control had to deviate it says so, and it is counted.
    unavailable = out["divergence"]["control_picks_unavailable"]
    assert unavailable == sum(1 for s in out["substitutions"] if not s["control_is_real"])
    # The two deltas are different questions and both are named.
    s = out["starters_proj"]
    assert set(s) == {"model", "control", "real", "delta_vs_control", "delta_vs_real"}
    assert set(out["bench_proj"]) == {"model", "control", "real"}


def test_counterfactual_lets_the_model_pick_where_the_real_team_went_off_board(
        tmp_path, monkeypatch):
    monkeypatch.setattr(board, "STATE_DIR", tmp_path)
    league = _counterfactual_league()
    st = board.DraftState(league)
    st.record("Some Kicker", 1, 1, position="K")   # slot 1 spends its turn off the board
    st.record("RB One", 2, 2)
    out = replay.counterfactual_draft(_board(), st, league, slot=1)
    sub = out["substitutions"][0]
    # The model is not denied the turn: the real pick scores 0 whatever happens,
    # so nothing is taken from the comparison by letting it choose.
    assert sub["real"] == "Some Kicker" and sub["real_proj"] is None
    assert sub["model"] != "Some Kicker" and sub["model_proj"] is not None
    assert sub["basis"] == "model recommendation for the simulated roster"
    # The control still mirrors it, worth nothing.
    assert sub["control"] == "Some Kicker" and sub["control_proj"] is None
    assert out["starters_proj"]["control"] == 0
    assert out["starters_proj"]["delta_vs_control"] > 0
    # Off-board mirroring at the target slot belongs to the control arm only.
    assert out["divergence"]["mirrored_off_board"] == 0


def test_counterfactual_holds_players_by_board_row_not_by_name(tmp_path, monkeypatch):
    monkeypatch.setattr(board, "STATE_DIR", tmp_path)
    b = _twin_board()
    league = _counterfactual_league()
    st = board.DraftState(league)
    st.record("Alex Twin", 1, 1, position="RB")
    st.record("RB One", 2, 2)
    st.record("WR One", 3, 2)
    st.record("Alex Twin", 4, 1, position="TE")

    out = replay.counterfactual_draft(b, st, league, slot=1)
    # Slot 1's two recorded picks resolve to two different board rows: the RB by
    # position at pick 1, the TE at pick 4. Held by name, pick 1 would have taken
    # both rows out of the pool and pick 4 would have had no row left to price.
    real = out["real_roster"]
    assert [r["player"] for r in real] == ["Alex Twin", "Alex Twin"]
    assert [r["proj_points"] for r in real] == [260.0, 90.0]
    # Scored off those exact rows, not off whichever one the name resolves to:
    # the RB fills the one RB slot for 260 and the TE fills nothing (TE: 0, no
    # flex). Resolved by name both picks would read TE 90 and score 0.
    assert out["starters_proj"]["real"] == 260
    # The model took two different players; nothing was double-counted.
    model_rows = [r["player"] for r in out["model_roster"]]
    assert len(model_rows) == 2 and len(set(model_rows)) == 2


def test_counterfactual_argmax_is_deterministic_and_sample_is_seeded(tmp_path, monkeypatch):
    st = _counterfactual_state(tmp_path, monkeypatch)
    league = _counterfactual_league()
    first = replay.counterfactual_draft(_board(), st, league, slot=1)
    again = replay.counterfactual_draft(_board(), st, league, slot=1)
    assert first["model_roster"] == again["model_roster"]

    s1 = replay.counterfactual_draft(_board(), st, league, slot=1, policy="sample", seed=7)
    s2 = replay.counterfactual_draft(_board(), st, league, slot=1, policy="sample", seed=7)
    assert s1["model_roster"] == s2["model_roster"]
    assert (s1["policy"], s1["seed"]) == ("sample", 7)

    with pytest.raises(ValueError, match="policy must be one of"):
        replay.counterfactual_draft(_board(), st, league, slot=1, policy="vibes")


def test_counterfactual_for_another_slot_hands_your_turns_to_the_predictor(tmp_path, monkeypatch):
    st = _counterfactual_state(tmp_path, monkeypatch)
    out = replay.counterfactual_draft(_board(), st, _counterfactual_league(), slot=2)
    assert [s["pick"] for s in out["substitutions"]] == [2, 3]
    # Pick 3 was a kicker. At the model's own slot that turn is not mirrored:
    # the real pick scores 0 whatever happens, so the model gets to choose.
    kicker = [s for s in out["substitutions"] if s["pick"] == 3][0]
    assert kicker["real"] == "Some Kicker" and kicker["real_proj"] is None
    assert kicker["same"] is False and kicker["model"] != "Some Kicker"
    assert kicker["basis"] == "model recommendation for the simulated roster"
    assert kicker["control"] == "Some Kicker" and kicker["control_proj"] is None
    # Slot 1's turns now belong to the predictor, not the model.
    assert out["divergence"]["other_team_picks"] == 2
    assert out["mine"] is False


def test_lineup_value_scores_a_simulated_roster_like_a_recorded_one(tmp_path, monkeypatch):
    monkeypatch.setattr(board, "STATE_DIR", tmp_path)
    picks = [{"name": "RB One", "position": "RB"}, {"name": "WR One", "position": "WR"},
             {"name": "Some Kicker", "position": "K"}]
    v = board.lineup_value(_board(), picks, _counterfactual_league())
    # RB One 300 + WR One 240; the kicker fills no starting slot this league has,
    # and no flex slot exists to hold him, so he is neither starter nor bench.
    assert v == {"starters_proj": 540, "bench_proj": 0, "open_starter_slots": 1, "picks": 3}
