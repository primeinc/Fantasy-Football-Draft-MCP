"""Walk-forward prediction of what the room will take next.

Four predictors compete, each a conditional logit over the players available
at a pick: P(i) = exp(s_i) / sum_j exp(s_j), s = X w. Three are one-feature
baselines (ESPN's list order, ADP order, the model's order); the fourth blends
those with roster need, the current positional run, and injury status. Every
predictor is scored prequentially: at pick t it is fitted on picks 1..t-1
only, scores pick t, and then learns from it. Nothing from this draft leaks
backward, so the top-k and log-loss numbers are honest out-of-sample.

A fifth predictor, `blend_team`, adds a per-team deviation on the rank features
and on a set of position indicators, shrunk to the league weights by a much
stronger L2. It is OFF by default (`TEAM_EFFECTS`) because on the live record it
does not pay: see the numbers in CHANGELOG.md and `just teameffects`. Eight picks
per team is too little to move a weight further than the penalty pulls it back.
"""
from __future__ import annotations

from collections.abc import Hashable

import numpy as np
import pandas as pd

RANK_FEATURES = ("log_espn_rank", "log_adp_rank", "log_model_rank")
# Position indicators, the other half of what a team-specific effect can say:
# "this team reaches for quarterbacks" is a preference the league weights have
# no way to express.
POSITIONS = ("QB", "RB", "WR", "TE")
POSITION_FEATURES = tuple(f"is_{p}" for p in POSITIONS)
BLEND_FEATURES = ("log_espn_rank", "log_adp_rank", "log_model_rank", "need_mult",
                  "position_run", "injury")
PREDICTORS: dict[str, tuple[str, ...]] = {
    "espn_list": ("log_espn_rank",),
    "adp": ("log_adp_rank",),
    "model": ("log_model_rank",),
    "blend": BLEND_FEATURES,
}
# The team-effects predictor: the blend's features plus league-level position
# intercepts, so a per-team position deviation is a deviation from the room's own
# positional taste rather than from zero.
TEAM_PREDICTOR = "blend_team"
TEAM_PREDICTOR_FEATURES = BLEND_FEATURES + POSITION_FEATURES
# The control. Same features as `blend_team`, no team deviations, so the pair
# isolates what the deviations are worth: `blend_pos` minus `blend` is what the
# league-level position intercepts buy, and `blend_team` minus `blend_pos` is
# what being per-team buys on top. Without it a comparison against the plain
# blend credits the deviations with the intercepts' work.
TEAM_CONTROL = "blend_pos"
# The columns a team may deviate on. Need, run and injury stay league-wide:
# they are already per-team quantities (need_mult is computed from that team's
# roster), so a deviation on them would be fitting the same thing twice.
TEAM_FEATURES = RANK_FEATURES + POSITION_FEATURES
# Before any pick has been seen a rank feature carries -1 (prefer the top of the
# list); everything else 0. L2 keeps a 120-observation fit from running off.
PRIOR_RANK_WEIGHT = -1.0
L2 = 0.02
# The shrinkage. A team's deviation is penalised an order of magnitude harder
# than the league weight it deviates from, so seven or eight picks have to be
# emphatic before the deviation survives the penalty.
TEAM_L2 = 0.5
# Whether the team-effects predictor runs at all. Off: on the live record it is
# worse out of sample than the plain blend (CHANGELOG).
TEAM_EFFECTS = False
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
    pos = recs["position"].astype(str)
    for p in POSITIONS:
        out[f"is_{p}"] = (pos == p).astype(float).to_numpy()
    return out


class ConditionalLogit:
    def __init__(self, cols: tuple[str, ...]) -> None:
        self.cols = cols
        self.w = np.array([PRIOR_RANK_WEIGHT if c in RANK_FEATURES else 0.0 for c in cols])
        self.train: list[tuple[np.ndarray, int]] = []

    def probabilities(self, x: np.ndarray, slot: int | None = None) -> np.ndarray:
        s = x @ self.weights_for(slot)
        s = s - s.max()
        p = np.exp(s)
        return p / p.sum()

    def weights_for(self, slot: int | None = None) -> np.ndarray:
        """The weights this predictor scores `slot`'s pick with. The league
        weights, here; `TeamConditionalLogit` adds that team's deviation."""
        return self.w

    def learn(self, x: np.ndarray, chosen: int, slot: int | None = None) -> None:
        self.train.append((x, chosen))
        for _ in range(FIT_STEPS):
            grad = np.zeros_like(self.w)
            for xi, ci in self.train:
                p = self.probabilities(xi)
                grad += xi[ci] - p @ xi
            grad = grad / len(self.train) - 2 * L2 * self.w
            self.w = self.w + STEP_SIZE * grad


class TeamConditionalLogit(ConditionalLogit):
    """League weights plus a per-team deviation, shrunk to the league weights.

    Team `t` scores with `w + d_t`, where `d_t` covers `TEAM_FEATURES` only (the
    rank features and the position indicators) and is zero for a team that has
    not picked yet. Both are fitted by ascending the same penalised average
    log-likelihood: `(1/n) sum_i log P(chosen_i) - L2*||w||^2 - TEAM_L2*sum_t
    ||d_t||^2`. Because every deviation shares the one `1/n` scale while
    carrying its own much larger penalty, a team needs consistent evidence
    across its handful of picks before its deviation stays away from zero --
    which is the whole point of shrinking to the league weights rather than
    fitting sixteen separate models.
    """

    def __init__(self, cols: tuple[str, ...], team_l2: float | None = None) -> None:
        super().__init__(cols)
        self.team_cols = tuple(c for c in cols if c in TEAM_FEATURES)
        self.team_idx = np.array([cols.index(c) for c in self.team_cols], dtype=int)
        # Read at construction, not bound as a default, so a caller sweeping the
        # shrinkage (`just teameffects <l2>`) actually changes it.
        self.team_l2 = TEAM_L2 if team_l2 is None else team_l2
        self.deviations: dict[int, np.ndarray] = {}
        self.team_train: list[tuple[np.ndarray, int, int | None]] = []

    def weights_for(self, slot: int | None = None) -> np.ndarray:
        d = self.deviations.get(slot) if slot is not None else None
        if d is None:
            return self.w
        w = self.w.copy()
        w[self.team_idx] += d
        return w

    def learn(self, x: np.ndarray, chosen: int, slot: int | None = None) -> None:
        self.team_train.append((x, chosen, slot))
        n = len(self.team_train)
        for _ in range(FIT_STEPS):
            grad = np.zeros_like(self.w)
            team_grad: dict[int, np.ndarray] = {}
            for xi, ci, ti in self.team_train:
                p = self.probabilities(xi, ti)
                g = xi[ci] - p @ xi
                grad += g
                if ti is not None:
                    acc = team_grad.get(ti)
                    if acc is None:
                        acc = np.zeros(len(self.team_idx))
                        team_grad[ti] = acc
                    acc += g[self.team_idx]
            self.w = self.w + STEP_SIZE * (grad / n - 2 * L2 * self.w)
            for ti, g in team_grad.items():
                d = self.deviations.get(ti)
                if d is None:
                    d = np.zeros(len(self.team_idx))
                self.deviations[ti] = d + STEP_SIZE * (g / n - 2 * self.team_l2 * d)


class WalkForward:
    """Runs every predictor prequentially and keeps the score sheet.

    With `team_effects` the `blend_team` predictor joins them, scored on exactly
    the same picks in the same order as the plain `blend`, so the two log losses
    are directly comparable. It defaults to `TEAM_EFFECTS`, which is off.
    """

    def __init__(self, team_effects: bool | None = None) -> None:
        self.team_effects = TEAM_EFFECTS if team_effects is None else team_effects
        models: dict[str, ConditionalLogit] = {
            name: ConditionalLogit(cols) for name, cols in PREDICTORS.items()}
        if self.team_effects:
            models[TEAM_CONTROL] = ConditionalLogit(TEAM_PREDICTOR_FEATURES)
            models[TEAM_PREDICTOR] = TeamConditionalLogit(TEAM_PREDICTOR_FEATURES)
        self.models = models
        self.rows: list[dict] = []

    def observe(self, recs: pd.DataFrame, chosen_key: Hashable | None,
                recent_positions: list[str], pick: int, slot: int | None = None) -> dict:
        """Score the predictors on this pick, then let them learn from it.
        Returns each predictor's rank of and probability for the real pick.
        `chosen_key` is whatever labels `recs` -- a name key when the caller
        indexes by name, a board row when it indexes by row -- or None when the
        pick is not in the pool, which scores nothing and trains nothing.
        `slot` is the team on the clock; only `blend_team` uses it."""
        f = features(recs, recent_positions)
        keys = list(recs.index)
        result: dict = {"pick": pick, "slot": slot, "scored": chosen_key in keys}
        chosen = keys.index(chosen_key) if chosen_key in keys else None
        for name, m in self.models.items():
            x = f[list(m.cols)].to_numpy(dtype=float)
            p = m.probabilities(x, slot)
            order = np.argsort(-p)
            entry = {"top": [str(recs["name"].iloc[i]) for i in order[:3]]}
            if chosen is not None:
                entry["rank"] = int(np.flatnonzero(order == chosen)[0]) + 1
                entry["p"] = round(float(p[chosen]), 4)
                entry["log_loss"] = round(float(-np.log(max(p[chosen], 1e-12))), 3)
                m.learn(x, chosen, slot)
            result[name] = entry
        self.rows.append(result)
        return result

    def probabilities(self, recs: pd.DataFrame, recent_positions: list[str],
                      name: str = "blend", slot: int | None = None) -> np.ndarray:
        """One predictor's probability over the players available in `recs`, in
        `recs` order. The predictor is used as it stands, so a caller replaying a
        draft gets the fit from the picks it has fed in so far and nothing later."""
        m = self.models[name]
        return m.probabilities(features(recs, recent_positions)[list(m.cols)]
                               .to_numpy(dtype=float), slot)

    def forecast(self, recs: pd.DataFrame, recent_positions: list[str], top: int = 5,
                 slot: int | None = None) -> dict:
        """Each predictor's view of the pick on the clock: top players with
        probabilities, and the blend's probability by position. `slot` is the
        team on the clock; only `blend_team` uses it."""
        f = features(recs, recent_positions)
        out: dict = {}
        for name, m in self.models.items():
            p = m.probabilities(f[list(m.cols)].to_numpy(dtype=float), slot)
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
        team = self.models.get(TEAM_PREDICTOR)
        if isinstance(team, TeamConditionalLogit):
            out["team_deviations"] = {
                str(s): dict(zip(team.team_cols, np.round(d, 3).tolist()))
                for s, d in sorted(team.deviations.items())}
        return out

    def summary(self) -> dict:
        scored = [r for r in self.rows if r["scored"]]
        out: dict = {"picks_scored": len(scored), "predictors": {}}
        for name in self.models:
            ranks = np.array([r[name]["rank"] for r in scored], dtype=float)
            losses = np.array([r[name]["log_loss"] for r in scored], dtype=float)
            out["predictors"][name] = {
                "log_loss": (round(float(losses.mean()), 3) if len(losses) else None),
                **{f"top{k}": (round(float((ranks <= k).mean()), 3) if len(ranks) else None)
                   for k in TOP_K},
                "median_rank": (float(np.median(ranks)) if len(ranks) else None),
            }
        return out
