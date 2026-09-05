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
    return {"picks_scored": n_picks, "picks": picks,
            "teams": [{"slot": s, "picks": n_picks // 16, "regret": 12.5} for s in range(1, 17)],
            "predictors": {"blend": {"mean_log_loss": 2.2}},
            "predictor_rows": [dict(pred, pick=i + 1) for i in range(n_picks)],
            "overall": {"brier": 0.2}}


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


def test_detail_returns_everything_and_picks_widens_the_window():
    full = _full_replay(224)
    assert replay.compact_for_client(full, detail=True) is full
    assert len(replay.compact_for_client(full, picks=100)["picks"]) == 100
