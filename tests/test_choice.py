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


def test_team_effects_are_off_by_default_and_opt_in_adds_the_pair():
    assert choice.TEAM_EFFECTS is False
    assert set(choice.WalkForward().models) == set(choice.PREDICTORS)
    on = choice.WalkForward(team_effects=True)
    # The control comes with it: without blend_pos, blend_team's extra position
    # intercepts would be credited to the team deviations.
    assert set(on.models) == set(choice.PREDICTORS) | {"blend_pos", "blend_team"}
    assert on.models["blend_pos"].cols == on.models["blend_team"].cols
    assert not isinstance(on.models["blend_pos"], choice.TeamConditionalLogit)
    recs = _pool(6).set_index("_key", drop=False)
    on.observe(recs, str(recs["_key"].iloc[0]), [], 1, 3)
    assert set(on.summary()["predictors"]) == set(on.models)


def test_position_indicators_are_features_the_plain_blend_never_sees():
    recs = _pool(4).set_index("_key", drop=False)
    f = choice.features(recs, [])
    # _pool alternates RB, WR.
    assert f["is_RB"].tolist() == [1.0, 0.0, 1.0, 0.0]
    assert f["is_WR"].tolist() == [0.0, 1.0, 0.0, 1.0]
    assert f["is_QB"].tolist() == [0.0] * 4 and f["is_TE"].tolist() == [0.0] * 4
    assert not set(choice.POSITION_FEATURES) & set(choice.PREDICTORS["blend"])


def test_a_team_deviation_moves_only_that_team_and_shrinks_with_the_penalty():
    recs = _pool(8).set_index("_key", drop=False)
    x = choice.features(recs, [])[list(choice.TEAM_PREDICTOR_FEATURES)].to_numpy(dtype=float)

    def fit(team_l2: float) -> choice.TeamConditionalLogit:
        m = choice.TeamConditionalLogit(choice.TEAM_PREDICTOR_FEATURES, team_l2=team_l2)
        # Slot 3 keeps taking the model's last man; slot 5 takes its first.
        for _ in range(4):
            m.learn(x, len(recs) - 1, 3)
            m.learn(x, 0, 5)
        return m

    loose, tight = fit(0.02), fit(5.0)
    assert set(loose.deviations) == {3, 5}
    assert np.linalg.norm(tight.deviations[3]) < np.linalg.norm(loose.deviations[3])
    # A slot that never picked has no deviation and scores on the league weights.
    assert np.allclose(loose.weights_for(9), loose.w)
    assert np.allclose(loose.weights_for(None), loose.w)
    assert not np.allclose(loose.weights_for(3), loose.w)
    # Slot 3 was pushed toward the bottom of the model's order, slot 5 the top,
    # so they disagree about the last man on the board.
    assert loose.probabilities(x, 3)[-1] > loose.probabilities(x, 5)[-1]
    # Only TEAM_FEATURES may deviate: need, run and injury stay league-wide.
    assert set(loose.team_cols) == set(choice.TEAM_FEATURES)


def test_unscored_pick_does_not_train():
    wf = choice.WalkForward()
    recs = _pool(5).set_index("_key", drop=False)
    r = wf.observe(recs, None, [], 1)
    assert r["scored"] is False and "rank" not in r["espn_list"]
    assert wf.models["espn_list"].train == []
    assert wf.summary()["picks_scored"] == 0
