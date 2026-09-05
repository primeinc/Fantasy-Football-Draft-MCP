"""draft_replay ships a view a client will show, not the whole replay.

Measured on the live board: 201,673 characters, ten times PAYLOAD_LIMIT,
predictor_rows 66,899 and picks 52,244. It reached the client only because
the exit trimmed it, and a trim is not a shape.
"""
from __future__ import annotations

import json

from ffdraft import replay, server


def _full_replay(n_picks: int) -> dict:
    row = {"pick": 0, "round": 1, "slot": 1, "actual": "A Player Name", "position": "WR",
           "actual_proj": 123.4, "actual_espn_proj": 111.1, "actual_rank": 7,
           "choice_percentile": 0.83, "off_board": False, "model_pick": "Another Player",
           "model_pick_proj": 140.2, "proj_gap": 16.8, "pick_regret": 3.3, "reach": -4.0,
           "market_z": 0.12, "need_mult": 1.0, "role_mult": 0.97, "p_available_next": 0.44}
    picks = [dict(row, pick=i + 1, round=(i // 16) + 1, slot=(i % 16) + 1)
             for i in range(n_picks)]
    pred = {"pick": 0, "scored": True,
            **{name: {"rank": 3, "p": 0.12, "log_loss": 2.1} for name in
               ("adp", "espn_list", "model", "blend", "blend_team")}}
    overall = {
        "on_board_picks": n_picks - 3, "off_board_picks": 3, "model_match_rate": 0.12,
        "top3_rate": 0.33, "median_rank": 11.0, "survival_brier": 0.137,
        "survival_log_loss": 0.426,
        "survival_calibration": [{"p_range": f"{i/5:.1f}", "n": 300, "predicted": 0.5,
                                  "observed": 0.5} for i in range(5)],
        "survival_by_round": [{"round": r, "n": 160, "brier": 0.1, "brier_baseline": 0.25,
                               "log_loss": 0.3, "predicted": 0.5, "observed": 0.5}
                              for r in range(1, 15)],
        "survival_by_position": [{"position": p, "n": 200, "brier": 0.1,
                                  "brier_baseline": 0.25, "log_loss": 0.3,
                                  "predicted": 0.5, "observed": 0.5}
                                 for p in ("QB", "RB", "WR", "TE", "K", "DST")],
        "biggest_reaches": [{"pick": i, "slot": 1, "actual": "Some Player Name",
                             "reach": 60.0, "market_z": 2.5} for i in range(5)],
        "biggest_values": [{"pick": i, "slot": 1, "actual": "Some Player Name",
                            "reach": -30.0, "market_z": -1.5} for i in range(5)],
        "biggest_regrets": [{"pick": i, "slot": 1, "actual": "Some Player Name",
                             "model_pick": "Other Player Name", "pick_regret": 250.0}
                            for i in range(5)],
    }
    team = {"slot": 0, "mine": False, "picks": 14, "model_matches": 1, "top3": 4,
            "mean_rank": 25.4, "mean_choice_percentile": 0.961, "off_board": 1,
            "proj_left_on_table": 86.3, "pick_regret": 131.96, "mean_reach": 7.0,
            "mean_market_z": 0.2, "mean_need_mult": 1.0, "mean_urgency_waste": 0.33,
            "team": "A Team Name That Is Fairly Long (Owner Name)"}
    return {"picks_scored": n_picks, "picks": picks, "adp_shift": {"RB": 2.8, "WR": 4.1},
            "room_drift": {"median_reach": 3.7, "shift": {"RB": 2.8, "WR": 4.1}},
            "teams": [dict(team, slot=s) for s in range(1, 17)],
            "predictors": {"picks_scored": n_picks, "predictors": {
                name: {"log_loss": 3.2, "top1": 0.18, "top3": 0.47, "top5": 0.55,
                       "median_rank": 4.0} for name in ("espn_list", "adp", "model", "blend")}},
            "predictor_rows": [dict(pred, pick=i + 1) for i in range(n_picks)],
            "overall": overall, "calibration_without_shift": dict(overall)}


def test_default_view_keeps_the_reader_keys_and_drops_the_predictor_rows():
    out = replay.compact_for_client(_full_replay(224))
    assert "predictor_rows" not in out
    assert "predictors" in out and "teams" in out
    assert len(out["picks"]) == replay.DEFAULT_PICK_WINDOW
    assert out["picks"][-1]["pick"] == 224
    assert set(out["picks"][0]) <= set(replay.COMPACT_PICK_KEYS)
    assert "showing" in out


def test_a_full_room_fits_the_cap_by_shape_not_by_trim():
    out = replay.compact_for_client(_full_replay(224))
    assert len(json.dumps(out, indent=2)) < server.PAYLOAD_LIMIT
    # The per-team totals are what a reader compares; live they were the table
    # the exit cut while two copies of the calibration tables stayed.
    assert len(out["teams"]) == 16
    assert "survival_by_round" not in out["overall"]
    assert set(out["calibration_without_shift"]) == {"model_match_rate", "top3_rate",
                                                     "median_rank", "survival_brier",
                                                     "survival_log_loss"}


def test_detail_returns_everything_and_picks_widens_the_window():
    full = _full_replay(224)
    assert replay.compact_for_client(full, detail=True) is full
    assert len(replay.compact_for_client(full, picks=100)["picks"]) == 100
