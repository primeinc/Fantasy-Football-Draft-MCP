import numpy as np
import pandas as pd

from ffdraft import board, choice, replay
from ffdraft.config import LeagueSettings


def _pool(n: int = 12) -> pd.DataFrame:
    names = [f"P{i}" for i in range(n)]
    b = pd.DataFrame({
        "name": names, "position": (["RB", "WR"] * n)[:n], "team": ["A"] * n,
        "proj_points": np.linspace(300, 100, n), "draft_score": np.linspace(300, 100, n),
        "adp": np.arange(1, n + 1, dtype=float), "pos_rank": np.arange(1, n + 1),
        "overall_rank": np.arange(1, n + 1), "consistency": [0.5] * n, "adj_ppg": [10.0] * n,
        # ESPN's list is the model's order reversed: the two disagree completely.
        "espn_rank": np.arange(n, 0, -1), "espn_injury": ["ACTIVE"] * n,
    })
    b["_key"] = b["name"].map(board.norm_name)
    return b


def test_features_rank_among_available_only():
    recs = _pool(4).set_index("_key", drop=False)
    f = choice.features(recs, ["RB", "RB", "WR"])
    assert f["log_model_rank"].tolist() == [np.log1p(i) for i in (1, 2, 3, 4)]
    assert f["log_espn_rank"].tolist() == [np.log1p(i) for i in (4, 3, 2, 1)]
    assert f["position_run"].tolist() == [2 / 3, 1 / 3, 2 / 3, 1 / 3]
    assert f["injury"].tolist() == [0.0] * 4


def test_walk_forward_learns_the_list_the_room_follows(tmp_path, monkeypatch):
    # A room that always takes the top of ESPN's list. Prequentially, the
    # espn_list predictor is right from the first pick (its prior already
    # prefers the top of its list); the model predictor, whose order is the
    # reverse, is wrong until it learns to invert itself; the blend learns a
    # negative weight on ESPN rank and ends up right too.
    monkeypatch.setattr(board, "STATE_DIR", tmp_path)
    league = LeagueSettings(name="t", teams=2, rounds=5, draft_slot=1,
                            starters={"QB": 0, "RB": 1, "WR": 1, "TE": 0, "FLEX": 0,
                                      "K": 0, "DST": 0})
    b = _pool(12)
    st = board.DraftState(league)
    by_espn = b.sort_values("espn_rank")["name"].tolist()
    for i, name in enumerate(by_espn[:8], start=1):
        st.record(name, i)

    out = replay.replay_draft(b, st, league, candidates=3)
    s = out["predictors"]["predictors"]
    assert out["predictors"]["picks_scored"] == 8
    assert s["espn_list"]["top1"] == 1.0
    assert s["model"]["top1"] < 1.0
    assert s["blend"]["log_loss"] < s["model"]["log_loss"]
    fc = out["forecast"]
    assert fc["pick"] == 9 and fc["slot"] == 1
    assert fc["espn_list"][0]["player"] == by_espn[8]
    assert abs(sum(fc["position_probabilities"].values()) - 1.0) < 1e-6
    assert fc["weights"]["blend"]["log_espn_rank"] < 0
    rows = out["predictor_rows"]
    assert len(rows) == 8 and all(r["scored"] for r in rows)
    assert all(r["espn_list"]["rank"] == 1 for r in rows)


def test_probabilities_expose_the_fit_so_far_over_an_arbitrary_pool():
    # What counterfactual_draft asks for: the blend's distribution over a pool it
    # chooses, from the predictor as it stands, with no learning as a side effect.
    wf = choice.WalkForward()
    recs = _pool(6).set_index("_key", drop=False)
    p = wf.probabilities(recs, [])
    assert p.shape == (6,) and abs(float(p.sum()) - 1.0) < 1e-9
    assert wf.models["blend"].train == [] and wf.rows == []

    before = wf.probabilities(recs, []).copy()
    by_espn = recs.sort_values("espn_rank")
    wf.observe(by_espn, str(by_espn["_key"].iloc[0]), [], 1)
    after = wf.probabilities(recs, [])
    # One observation of a room following ESPN's list moves the blend toward it.
    assert not np.allclose(before, after)
    # The pool is the caller's: a shorter one gets a shorter distribution.
    assert wf.probabilities(recs.head(3), []).shape == (3,)


def test_unscored_pick_does_not_train():
    wf = choice.WalkForward()
    recs = _pool(5).set_index("_key", drop=False)
    r = wf.observe(recs, None, [], 1)
    assert r["scored"] is False and "rank" not in r["espn_list"]
    assert wf.models["espn_list"].train == []
    assert wf.summary()["picks_scored"] == 0
