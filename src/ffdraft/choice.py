"""Walk-forward prediction of what the room will take next.

Four predictors compete, each a conditional logit over the players available
at a pick: P(i) = exp(s_i) / sum_j exp(s_j), s = X w. Three are one-feature
baselines (ESPN's list order, ADP order, the model's order); the fourth blends
those with roster need, the current positional run, and injury status. Every
predictor is scored prequentially: at pick t it is fitted on picks 1..t-1
only, scores pick t, and then learns from it. Nothing from this draft leaks
backward, so the top-k and log-loss numbers are honest out-of-sample.

Team-specific effects are deliberately absent: eight picks per team cannot
support them without shrinking to the league weights anyway.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

RANK_FEATURES = ("log_espn_rank", "log_adp_rank", "log_model_rank")
PREDICTORS: dict[str, tuple[str, ...]] = {
    "espn_list": ("log_espn_rank",),
    "adp": ("log_adp_rank",),
    "model": ("log_model_rank",),
    "blend": ("log_espn_rank", "log_adp_rank", "log_model_rank", "need_mult",
              "position_run", "injury"),
}
# Before any pick has been seen a rank feature carries -1 (prefer the top of the
# list); everything else 0. L2 keeps a 120-observation fit from running off.
PRIOR_RANK_WEIGHT = -1.0
L2 = 0.02
FIT_STEPS = 25
STEP_SIZE = 0.15
RUN_WINDOW = 5
TOP_K = (1, 3, 5)


def features(recs: pd.DataFrame, recent_positions: list[str]) -> pd.DataFrame:
    """One row per available player, `recs` sorted by the model's pick_value.
    Ranks are among the available players only, so the features mean the same
    thing at pick 5 and pick 120."""
    n = len(recs)
    out = pd.DataFrame(index=recs.index)
    espn = pd.to_numeric(recs.get("espn_rank"), errors="coerce") if "espn_rank" in recs.columns \
        else pd.Series(np.nan, index=recs.index)
    out["log_espn_rank"] = np.log1p(espn.rank(method="min", na_option="bottom").to_numpy())
    out["log_adp_rank"] = np.log1p(pd.to_numeric(recs["adp"], errors="coerce")
                                   .rank(method="min", na_option="bottom").to_numpy())
    out["log_model_rank"] = np.log1p(np.arange(1, n + 1, dtype=float))
    need = recs["need_mult"] if "need_mult" in recs.columns else pd.Series(1.0, index=recs.index)
    out["need_mult"] = pd.to_numeric(need, errors="coerce").fillna(1.0).to_numpy()
    window = recent_positions[-RUN_WINDOW:]
    share = {pos: window.count(pos) / len(window) for pos in set(window)} if window else {}
    out["position_run"] = recs["position"].map(share).fillna(0.0).to_numpy()
    inj = recs["espn_injury"] if "espn_injury" in recs.columns else pd.Series(None, index=recs.index)
    out["injury"] = (~inj.isna() & (inj != "ACTIVE")).astype(float).to_numpy()
    return out


class ConditionalLogit:
    def __init__(self, cols: tuple[str, ...]) -> None:
        self.cols = cols
        self.w = np.array([PRIOR_RANK_WEIGHT if c in RANK_FEATURES else 0.0 for c in cols])
        self.train: list[tuple[np.ndarray, int]] = []

    def probabilities(self, x: np.ndarray) -> np.ndarray:
        s = x @ self.w
        s = s - s.max()
        p = np.exp(s)
        return p / p.sum()

    def learn(self, x: np.ndarray, chosen: int) -> None:
        self.train.append((x, chosen))
        for _ in range(FIT_STEPS):
            grad = np.zeros_like(self.w)
            for xi, ci in self.train:
                p = self.probabilities(xi)
                grad += xi[ci] - p @ xi
            grad = grad / len(self.train) - 2 * L2 * self.w
            self.w = self.w + STEP_SIZE * grad


class WalkForward:
    """Runs every predictor prequentially and keeps the score sheet."""

    def __init__(self) -> None:
        self.models = {name: ConditionalLogit(cols) for name, cols in PREDICTORS.items()}
        self.rows: list[dict] = []

    def observe(self, recs: pd.DataFrame, chosen_key: str | None,
                recent_positions: list[str], pick: int) -> dict:
        """Score the predictors on this pick, then let them learn from it.
        Returns each predictor's rank of and probability for the real pick."""
        f = features(recs, recent_positions)
        keys = list(recs.index)
        result: dict = {"pick": pick, "scored": chosen_key in keys}
        chosen = keys.index(chosen_key) if chosen_key in keys else None
        for name, m in self.models.items():
            x = f[list(m.cols)].to_numpy(dtype=float)
            p = m.probabilities(x)
            order = np.argsort(-p)
            entry = {"top": [str(recs["name"].iloc[i]) for i in order[:3]]}
            if chosen is not None:
                entry["rank"] = int(np.flatnonzero(order == chosen)[0]) + 1
                entry["p"] = round(float(p[chosen]), 4)
                entry["log_loss"] = round(float(-np.log(max(p[chosen], 1e-12))), 3)
                m.learn(x, chosen)
            result[name] = entry
        self.rows.append(result)
        return result

    def probabilities(self, recs: pd.DataFrame, recent_positions: list[str],
                      name: str = "blend") -> np.ndarray:
        """One predictor's probability over the players available in `recs`, in
        `recs` order. The predictor is used as it stands, so a caller replaying a
        draft gets the fit from the picks it has fed in so far and nothing later."""
        m = self.models[name]
        return m.probabilities(features(recs, recent_positions)[list(m.cols)]
                               .to_numpy(dtype=float))

    def forecast(self, recs: pd.DataFrame, recent_positions: list[str], top: int = 5) -> dict:
        """Each predictor's view of the pick on the clock: top players with
        probabilities, and the blend's probability by position."""
        f = features(recs, recent_positions)
        out: dict = {}
        for name, m in self.models.items():
            p = m.probabilities(f[list(m.cols)].to_numpy(dtype=float))
            order = np.argsort(-p)[:top]
            out[name] = [{"player": str(recs["name"].iloc[i]),
                          "position": str(recs["position"].iloc[i]),
                          "p": round(float(p[i]), 3)} for i in order]
        p = self.models["blend"].probabilities(f[list(PREDICTORS["blend"])].to_numpy(dtype=float))
        by_pos = pd.Series(p, index=recs["position"].to_numpy()).groupby(level=0).sum()
        out["position_probabilities"] = {str(k): round(float(v), 3)
                                         for k, v in by_pos.sort_values(ascending=False).items()}
        out["weights"] = {name: dict(zip(m.cols, np.round(m.w, 3).tolist()))
                          for name, m in self.models.items()}
        return out

    def summary(self) -> dict:
        scored = [r for r in self.rows if r["scored"]]
        out: dict = {"picks_scored": len(scored), "predictors": {}}
        for name in PREDICTORS:
            ranks = np.array([r[name]["rank"] for r in scored], dtype=float)
            losses = np.array([r[name]["log_loss"] for r in scored], dtype=float)
            out["predictors"][name] = {
                "log_loss": (round(float(losses.mean()), 3) if len(losses) else None),
                **{f"top{k}": (round(float((ranks <= k).mean()), 3) if len(ranks) else None)
                   for k in TOP_K},
                "median_rank": (float(np.median(ranks)) if len(ranks) else None),
            }
        return out
