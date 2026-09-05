"""Replay a draft pick by pick through the model, for every team.

At each pick the model is asked what it would take for the team on the clock,
given that team's roster and the pool left at that moment. The answer is set
against what was actually taken: the model's rank of the real pick, the points
the model thinks were left on the table, and the reach against ADP. The same
pass scores the survival model: every "X% chance he lasts to your next pick"
the model gave a team is checked at that team's next pick.

Limits, stated because they bound what the numbers mean: projections and ADP
are today's, not as of the pick (news since then moves both); kickers, defenses
and players the board cannot model are `off_board` and score nothing.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import model
from .board import DraftState
from .config import LeagueSettings
from .names import normalize as norm_name

CALIBRATION_BINS = ((0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01))
# Picks at a position before its own median reach is used instead of the room's.
DRIFT_MIN_PICKS = 8


def room_drift(board: pd.DataFrame, state: DraftState, last: int = 0) -> dict:
    """How far ahead of ADP this room drafts: the median of (ADP - pick number)
    over the recorded picks the board prices, positive when players go earlier
    than ADP. `last` restricts it to the most recent picks (0 = all). Feed the
    median to recommend(adp_shift=...) so survival odds match this room."""
    adp = dict(zip(board["_key"], board["adp"])) if "_key" in board.columns else {}
    pos_of = dict(zip(board["_key"], board["position"])) if "_key" in board.columns else {}
    picks = sorted(state.picks, key=lambda p: p["overall"])
    if last:
        picks = picks[-last:]
    reaches: list[float] = []
    by_pos: dict[str, list[float]] = {}
    for p in picks:
        k = norm_name(p["name"])
        if k in adp and pd.notna(adp[k]):
            r = float(adp[k]) - p["overall"]
            reaches.append(r)
            by_pos.setdefault(str(pos_of.get(k)), []).append(r)
    if not reaches:
        return {"median_reach": 0.0, "mean_reach": 0.0, "n": 0, "by_position": {}, "shift": {}}
    median = round(float(np.median(reaches)), 1)
    by_position = {pos: {"median": round(float(np.median(v)), 1), "n": len(v)}
                   for pos, v in sorted(by_pos.items())}
    # A position's own median once it has enough picks to mean something,
    # else the room's. The replay showed QB reaches (Mahomes at 47, Goff at
    # 65) that the room-wide number cannot see.
    shift = {pos: (d["median"] if d["n"] >= DRIFT_MIN_PICKS else median)
             for pos, d in by_position.items()}
    return {"median_reach": median, "mean_reach": round(float(np.mean(reaches)), 1),
            "n": len(reaches), "by_position": by_position, "shift": shift}


def replay_draft(board: pd.DataFrame, state: DraftState, league: LeagueSettings,
                 candidates: int = 10, adp_shift: float | dict[str, float] = 0.0) -> dict:
    """Score every recorded pick against the model and return per-pick rows,
    per-team totals, and the survival model's calibration. `adp_shift` is
    passed to recommend() so the calibration can be read with and without the
    room's drift applied."""
    b = board.copy()
    if "_key" not in b.columns:
        b["_key"] = b["name"].map(norm_name)
    b = b.set_index("_key", drop=False)
    picks = sorted(state.picks, key=lambda p: p["overall"])
    taken_at: dict[str, int] = {norm_name(p["name"]): p["overall"] for p in picks}
    rosters: dict[int, dict[str, int]] = {}
    rows: list[dict] = []
    forecasts: list[tuple[float, bool, int, str]] = []
    taken: set[str] = set()

    for p in picks:
        overall, slot = p["overall"], p["slot"]
        key = norm_name(p["name"])
        pool = b[~b["_key"].isin(taken)]
        later = [n for n in league.picks_for_slot(slot) if n > overall]
        next_pick = later[0] if later else None
        recs = model.recommend(pool, league, current_pick=overall, next_pick=next_pick,
                               roster=rosters.get(slot, {}), top_n=len(pool),
                               adp_shift=adp_shift)
        where = np.flatnonzero(recs.index.to_numpy() == key)
        rnd = (overall - 1) // league.teams + 1
        row = {
            "pick": overall, "round": rnd, "slot": slot,
            "actual": p["name"], "position": p.get("position"), "actual_proj": None,
            "actual_espn_proj": None, "actual_rank": None, "choice_percentile": None,
            "off_board": True, "model_pick": None, "model_pick_proj": None,
            "proj_gap": None, "pick_regret": None, "reach": None, "market_z": None,
            "need_mult": None, "role_mult": None, "p_available_next": None,
        }
        if len(recs):
            top = recs.iloc[0]
            row["model_pick"] = top["name"]
            row["model_pick_proj"] = round(float(top["proj_points"]), 1)
            if len(where):
                i = int(where[0])
                actual = recs.iloc[i]
                espn = actual.get("espn_proj")
                adp = float(actual["adp"])
                row.update({
                    "position": actual["position"],
                    "actual_proj": round(float(actual["proj_points"]), 1),
                    "actual_espn_proj": (round(float(espn), 1)
                                         if espn is not None and pd.notna(espn) else None),
                    "actual_rank": i + 1,
                    # Share of the pool the pick beat: 1.0 is the model's top choice.
                    "choice_percentile": round(1 - i / max(1, len(recs) - 1), 3),
                    "off_board": False,
                    "proj_gap": round(float(top["proj_points"] - actual["proj_points"]), 1),
                    # In the model's own units at this pick: best pick_value minus taken.
                    "pick_regret": round(float(top["pick_value"] - actual["pick_value"]), 2),
                    "reach": round(adp - overall, 1),
                    # Reach in units of the survival model's own ADP spread, so a 12-pick
                    # reach at pick 20 and at pick 180 are comparable.
                    "market_z": round((adp - overall) / max(model.ADP_SD_FLOOR,
                                                            model.ADP_SD_RATE * adp), 2),
                    "need_mult": round(float(actual["need_mult"]), 2),
                    "role_mult": round(float(actual["role_mult"]), 2),
                    "p_available_next": (round(float(actual["p_available_next"]), 2)
                                         if next_pick is not None else None),
                })
        rows.append(row)
        # Survival forecasts made at this pick, checked at this team's next pick.
        if next_pick is not None and next_pick <= len(picks):
            for k, r in recs.head(candidates).iterrows():
                p_avail = float(r["p_available_next"])
                if np.isfinite(p_avail):
                    gone_before = taken_at.get(str(k), 10**9) < next_pick
                    forecasts.append((p_avail, not gone_before, rnd, str(r["position"])))
        pos = row["position"]
        if pos:
            rosters.setdefault(slot, {})[pos] = rosters.get(slot, {}).get(pos, 0) + 1
        taken.add(key)

    per_pick = pd.DataFrame(rows)
    teams = _team_totals(per_pick, league, state.my_slot)
    return {
        "picks_scored": len(rows), "adp_shift": adp_shift,
        "room_drift": room_drift(board, state),
        "overall": _overall(per_pick, forecasts),
        "teams": teams.to_dict(orient="records"),
        # The dict rows, not the frame: a frame turns None into NaN.
        "picks": rows,
    }


def _team_totals(per_pick: pd.DataFrame, league: LeagueSettings, my_slot: int) -> pd.DataFrame:
    out = []
    for slot in range(1, league.teams + 1):
        t = per_pick[per_pick["slot"] == slot]
        on = t[~t["off_board"]]
        def mean(col: str, nd: int = 1, rows: pd.DataFrame = on) -> float | None:
            return round(float(rows[col].mean()), nd) if len(rows) else None

        out.append({
            "slot": slot, "mine": slot == my_slot, "picks": len(t),
            "model_matches": int((on["actual_rank"] == 1).sum()),
            "top3": int((on["actual_rank"] <= 3).sum()),
            "mean_rank": mean("actual_rank"),
            "mean_choice_percentile": mean("choice_percentile", 3),
            "off_board": int(t["off_board"].sum()),
            "proj_left_on_table": round(float(on["proj_gap"].sum()), 1),
            "pick_regret": round(float(on["pick_regret"].sum()), 2),
            "mean_reach": mean("reach"),
            "mean_market_z": mean("market_z", 2),
            "mean_need_mult": mean("need_mult", 2),
            # Mean survival odds of what they took: high means they spent picks on
            # players who would have been there next turn.
            "mean_urgency_waste": mean("p_available_next", 2),
        })
    return pd.DataFrame(out).sort_values("pick_regret").reset_index(drop=True)


def _score(f: pd.DataFrame) -> dict:
    """Brier, log loss and base-rate Brier for a frame of (p, survived)."""
    survived = f["survived"].astype(float)
    p = f["p"].clip(1e-6, 1 - 1e-6)
    return {"n": len(f),
            "brier": round(float(((f["p"] - survived) ** 2).mean()), 3),
            "brier_baseline": round(float(((survived.mean() - survived) ** 2).mean()), 3),
            "log_loss": round(float(-(survived * np.log(p) + (1 - survived) * np.log(1 - p)).mean()), 3),
            "predicted": round(float(f["p"].mean()), 3),
            "observed": round(float(survived.mean()), 3)}


def _overall(per_pick: pd.DataFrame, forecasts: list[tuple[float, bool, int, str]]) -> dict:
    on = per_pick[~per_pick["off_board"]]
    cal: list[dict] = []
    by_round: list[dict] = []
    by_position: list[dict] = []
    score: dict = {"n": 0, "brier": None, "brier_baseline": None, "log_loss": None}
    if forecasts:
        f = pd.DataFrame(forecasts, columns=["p", "survived", "round", "position"])
        for lo, hi in CALIBRATION_BINS:
            chunk = f[(f["p"] >= lo) & (f["p"] < hi)]
            if len(chunk):
                cal.append({"p_range": f"{lo:.1f}-{min(hi, 1.0):.1f}", "n": len(chunk),
                            "predicted": round(float(chunk["p"].mean()), 2),
                            "observed": round(float(chunk["survived"].mean()), 2)})
        score = _score(f)
        by_round = [{"round": int(g["round"].iloc[0]), **_score(g)}
                    for _r, g in f.groupby("round")]
        by_position = [{"position": str(g["position"].iloc[0]), **_score(g)}
                       for _pos, g in f.groupby("position")]
    cols = ["pick", "slot", "actual", "reach", "market_z"]
    return {
        "on_board_picks": len(on), "off_board_picks": int(per_pick["off_board"].sum()),
        "model_match_rate": (round(float((on["actual_rank"] == 1).mean()), 3) if len(on) else None),
        "top3_rate": (round(float((on["actual_rank"] <= 3).mean()), 3) if len(on) else None),
        "median_rank": (float(on["actual_rank"].median()) if len(on) else None),
        "mean_choice_percentile": (round(float(on["choice_percentile"].mean()), 3) if len(on) else None),
        "survival_forecasts": score["n"],
        "survival_brier": score["brier"], "survival_brier_baseline": score["brier_baseline"],
        "survival_log_loss": score["log_loss"],
        "survival_calibration": cal,
        "survival_by_round": by_round,
        "survival_by_position": by_position,
        "biggest_reaches": on.sort_values("market_z", ascending=False).head(5)[cols].to_dict(orient="records"),
        "biggest_values": on.sort_values("market_z").head(5)[cols].to_dict(orient="records"),
        "biggest_regrets": on.sort_values("pick_regret", ascending=False).head(5)[
            ["pick", "slot", "actual", "model_pick", "pick_regret"]].to_dict(orient="records"),
    }
