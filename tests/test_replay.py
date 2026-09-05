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
    assert out["substitutions_made"] == 2

    # RB One goes to slot 1 at pick 1 in the simulation, so he is not there for
    # slot 2 at pick 2: the substitution really moved the pool.
    assert [r["player"] for r in out["model_roster"]][0] == "RB One"
    assert [r["player"] for r in out["real_roster"]] == ["WR Two", "RB Two"]

    # The kicker is off the board: mirrored, not predicted, and worth nothing.
    d = out["divergence"]
    assert d["mirrored_off_board"] == 1
    assert d["other_team_picks"] == 1 and d["other_team_picks_changed"] <= 1

    # Real starters: WR Two 200 + RB Two 250, the QB slot left empty.
    assert out["starters_proj"]["real"] == 450
    assert out["open_starter_slots"]["real"] == 1
    assert out["starters_proj"]["delta"] == (out["starters_proj"]["model"]
                                            - out["starters_proj"]["real"])
    # Nothing was written back: the recorded draft is untouched.
    assert [p["name"] for p in st.picks] == ["WR Two", "RB One", "Some Kicker", "RB Two"]


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
    # Pick 3 was a kicker: mirrored even at the model's own slot, since the board
    # holds nobody to put in his place.
    mirrored = [s for s in out["substitutions"] if s["pick"] == 3][0]
    assert mirrored["same"] is True and mirrored["model"] == "Some Kicker"
    assert mirrored["basis"].startswith("mirrored") and mirrored["model_proj"] is None
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
