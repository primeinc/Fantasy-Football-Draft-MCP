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


def room_drift(board: pd.DataFrame, state: DraftState, last: int = 0) -> dict:
    """How far ahead of ADP this room drafts: the median of (ADP - pick number)
    over the recorded picks the board prices, positive when players go earlier
    than ADP. `last` restricts it to the most recent picks (0 = all). Feed the
    median to recommend(adp_shift=...) so survival odds match this room."""
    adp = dict(zip(board["_key"], board["adp"])) if "_key" in board.columns else {}
    picks = sorted(state.picks, key=lambda p: p["overall"])
    if last:
        picks = picks[-last:]
    reaches = [float(adp[k]) - p["overall"] for p in picks
               if (k := norm_name(p["name"])) in adp and pd.notna(adp[k])]
    if not reaches:
        return {"median_reach": 0.0, "mean_reach": 0.0, "n": 0}
    return {"median_reach": round(float(np.median(reaches)), 1),
            "mean_reach": round(float(np.mean(reaches)), 1), "n": len(reaches)}


def replay_draft(board: pd.DataFrame, state: DraftState, league: LeagueSettings,
                 candidates: int = 10, adp_shift: float = 0.0) -> dict:
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
    forecasts: list[tuple[float, bool]] = []
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
        row = {
            "pick": overall, "round": (overall - 1) // league.teams + 1, "slot": slot,
            "actual": p["name"], "position": p.get("position"), "actual_proj": None,
            "actual_espn_proj": None, "actual_rank": None, "off_board": True,
            "model_pick": None, "model_pick_proj": None, "proj_gap": None, "reach": None,
        }
        if len(recs):
            top = recs.iloc[0]
            row["model_pick"] = top["name"]
            row["model_pick_proj"] = round(float(top["proj_points"]), 1)
            if len(where):
                actual = recs.iloc[int(where[0])]
                espn = actual.get("espn_proj")
                row.update({
                    "position": actual["position"],
                    "actual_proj": round(float(actual["proj_points"]), 1),
                    "actual_espn_proj": (round(float(espn), 1)
                                         if espn is not None and pd.notna(espn) else None),
                    "actual_rank": int(where[0]) + 1, "off_board": False,
                    "proj_gap": round(float(top["proj_points"] - actual["proj_points"]), 1),
                    "reach": round(float(actual["adp"]) - overall, 1),
                })
        rows.append(row)
        # Survival forecasts made at this pick, checked at this team's next pick.
        if next_pick is not None and next_pick <= len(picks):
            for k, r in recs.head(candidates).iterrows():
                p_avail = float(r["p_available_next"])
                if np.isfinite(p_avail):
                    gone_before = taken_at.get(str(k), 10**9) < next_pick
                    forecasts.append((p_avail, not gone_before))
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
        out.append({
            "slot": slot, "mine": slot == my_slot, "picks": len(t),
            "model_matches": int((on["actual_rank"] == 1).sum()),
            "top3": int((on["actual_rank"] <= 3).sum()),
            "mean_rank": (round(float(on["actual_rank"].mean()), 1) if len(on) else None),
            "off_board": int(t["off_board"].sum()),
            "proj_left_on_table": round(float(on["proj_gap"].sum()), 1),
            "mean_reach": (round(float(on["reach"].mean()), 1) if len(on) else None),
        })
    return pd.DataFrame(out).sort_values("proj_left_on_table").reset_index(drop=True)


def _overall(per_pick: pd.DataFrame, forecasts: list[tuple[float, bool]]) -> dict:
    on = per_pick[~per_pick["off_board"]]
    cal = []
    brier = brier_base = None
    if forecasts:
        f = pd.DataFrame(forecasts, columns=["p", "survived"])
        for lo, hi in CALIBRATION_BINS:
            chunk = f[(f["p"] >= lo) & (f["p"] < hi)]
            if len(chunk):
                cal.append({"p_range": f"{lo:.1f}-{min(hi, 1.0):.1f}", "n": len(chunk),
                            "predicted": round(float(chunk["p"].mean()), 2),
                            "observed": round(float(chunk["survived"].mean()), 2)})
        survived = f["survived"].astype(float)
        brier = round(float(((f["p"] - survived) ** 2).mean()), 3)
        brier_base = round(float(((survived.mean() - survived) ** 2).mean()), 3)
    cols = ["pick", "slot", "actual", "reach"]
    return {
        "on_board_picks": len(on), "off_board_picks": int(per_pick["off_board"].sum()),
        "model_match_rate": (round(float((on["actual_rank"] == 1).mean()), 3) if len(on) else None),
        "top3_rate": (round(float((on["actual_rank"] <= 3).mean()), 3) if len(on) else None),
        "median_rank": (float(on["actual_rank"].median()) if len(on) else None),
        "survival_forecasts": len(forecasts),
        "survival_brier": brier, "survival_brier_baseline": brier_base,
        "survival_calibration": cal,
        "biggest_reaches": on.sort_values("reach", ascending=False).head(5)[cols].to_dict(orient="records"),
        "biggest_values": on.sort_values("reach").head(5)[cols].to_dict(orient="records"),
    }
