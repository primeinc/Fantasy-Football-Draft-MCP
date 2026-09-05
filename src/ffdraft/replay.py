"""Replay a draft pick by pick through the model, for every team.

At each pick the model is asked what it would take for the team on the clock,
given that team's roster and the pool left at that moment. The answer is set
against what was actually taken: the model's rank of the real pick, the points
the model thinks were left on the table, and the reach against ADP. The same
pass scores the survival model: every "X% chance he lasts to your next pick"
the model gave a team is checked at that team's next pick.

`counterfactual_draft` is the same walk with the model intervening: at one
team's turns it takes the model's pick instead, that pick changes what is left
for everyone after it, and the rest of the room drafts per the walk-forward
blend predictor. Its output is a simulation, labelled as one.

Limits, stated because they bound what the numbers mean: projections and ADP
are today's, not as of the pick (news since then moves both); kickers, defenses
and players the board cannot model are `off_board` and score nothing.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import choice, model
from .board import DraftState, lineup_value
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
                 candidates: int = 10, adp_shift: float | dict[str, float] = 0.0,
                 walk_forward: bool = True) -> dict:
    """Score every recorded pick against the model and return per-pick rows,
    per-team totals, and the survival model's calibration. `adp_shift` is
    passed to recommend() so the calibration can be read with and without the
    room's drift applied. With `walk_forward` the choice predictors are scored
    prequentially as well and a forecast for the pick on the clock is returned."""
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
    wf = choice.WalkForward() if walk_forward else None
    recent_positions: list[str] = []

    def recommend_for(slot: int, overall: int, pool: pd.DataFrame) -> tuple[pd.DataFrame, int | None]:
        later = [n for n in league.picks_for_slot(slot) if n > overall]
        next_pick = later[0] if later else None
        recs = model.recommend(pool, league, current_pick=overall, next_pick=next_pick,
                               roster=rosters.get(slot, {}), top_n=len(pool),
                               adp_shift=adp_shift)
        return recs, next_pick

    for p in picks:
        overall, slot = p["overall"], p["slot"]
        key = norm_name(p["name"])
        pool = b[~b["_key"].isin(taken)]
        recs, next_pick = recommend_for(slot, overall, pool)
        if wf is not None and len(recs):
            wf.observe(recs, key if key in recs.index else None, recent_positions, overall)
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
            recent_positions.append(str(pos))
        taken.add(key)

    per_pick = pd.DataFrame(rows)
    teams = _team_totals(per_pick, league, state.my_slot)
    out = {
        "picks_scored": len(rows), "adp_shift": adp_shift,
        "room_drift": room_drift(board, state),
        "overall": _overall(per_pick, forecasts),
        "teams": teams.to_dict(orient="records"),
        # The dict rows, not the frame: a frame turns None into NaN.
        "picks": rows,
    }
    if wf is not None:
        out["predictors"] = wf.summary()
        out["predictor_rows"] = wf.rows
        on_clock = state.on_the_clock
        if on_clock <= league.teams * league.rounds:
            slot = state.slot_for_pick(on_clock)
            pool = b[~b["_key"].isin(taken)]
            recs, next_pick = recommend_for(slot, on_clock, pool)
            out["forecast"] = {"pick": on_clock, "slot": slot, "next_pick": next_pick,
                               **wf.forecast(recs, recent_positions)}
    return out


# What a team other than the model does at its turn in a counterfactual.
COUNTERFACTUAL_POLICIES = ("argmax", "sample")


def counterfactual_draft(board: pd.DataFrame, state: DraftState, league: LeagueSettings,
                         slot: int, adp_shift: float | dict[str, float] | None = None,
                         policy: str = "argmax", seed: int = 0) -> dict:
    """Simulate the draft again with the model drafting for `slot`.

    `replay_draft` is observational: it scores the real picks and changes
    nothing. This one intervenes. At each of `slot`'s turns the model picks for
    that team's simulated roster and the room's drift, and that pick changes what
    is left for everyone after it. Every other team takes the blend predictor's
    choice among the players still available -- `choice.WalkForward`, fitted
    prequentially on the *real* picks up to that point, so nothing from later in
    the draft leaks backward. `policy="argmax"` takes the predictor's likeliest
    player and is deterministic; `policy="sample"` draws from its distribution
    with `seed`.

    A real pick the board cannot model (a kicker, a defense, a player with no
    projection) is mirrored, not predicted: the simulated team takes the same
    player, worth 0 projected points, because the board holds nobody the
    predictor could have named in his place. Predicting one instead would eat a
    modelled player who was really still there, and bias the comparison.

    This is a simulation, not a measurement. It assumes every other team behaves
    like the predictor, and it prices the whole draft with today's projections
    and ADP. `adp_shift` defaults to the room drift over the whole record, which
    is the same number `draft_replay` reports and is therefore not as-of either.
    """
    if policy not in COUNTERFACTUAL_POLICIES:
        raise ValueError(f"policy must be one of {COUNTERFACTUAL_POLICIES}, got {policy!r}")
    b = board.copy()
    if "_key" not in b.columns:
        b["_key"] = b["name"].map(norm_name)
    b = b.set_index("_key", drop=False)
    if adp_shift is None:
        adp_shift = room_drift(board, state)["shift"]
    proj_of = dict(zip(b["_key"], b["proj_points"]))
    pos_of = dict(zip(b["_key"], b["position"]))
    rng = np.random.default_rng(seed)
    wf = choice.WalkForward()
    picks = sorted(state.picks, key=lambda p: p["overall"])

    real_taken: set[str] = set()
    real_rosters: dict[int, dict[str, int]] = {}
    real_recent: list[str] = []
    sim_taken: set[str] = set()
    sim_rosters: dict[int, dict[str, int]] = {}
    sim_recent: list[str] = []
    sim_picks: list[dict] = []
    subs: list[dict] = []
    other_total = other_changed = mirrored = 0

    def recs_for(team_slot: int, overall: int, taken: set[str],
                 rosters: dict[int, dict[str, int]]) -> pd.DataFrame:
        pool = b[~b["_key"].isin(taken)]
        if pool.empty:
            return pool
        later = [n for n in league.picks_for_slot(team_slot) if n > overall]
        return model.recommend(pool, league, current_pick=overall,
                               next_pick=(later[0] if later else None),
                               roster=rosters.get(team_slot, {}), top_n=len(pool),
                               adp_shift=adp_shift)

    def count(rosters: dict[int, dict[str, int]], team_slot: int, pos: str) -> None:
        held = rosters.setdefault(team_slot, {})
        held[pos] = held.get(pos, 0) + 1

    for p in picks:
        overall, team_slot = p["overall"], p["slot"]
        key = norm_name(p["name"])
        on_board = key in proj_of
        # The simulated world, decided with the predictor as it stands: fitted on
        # the real picks before this one and nothing after.
        sim_recs = recs_for(team_slot, overall, sim_taken, sim_rosters)
        if not on_board or sim_recs.empty:
            take_key, take_name = key, p["name"]
            take_pos = p.get("position")
            take_proj = None
            basis = "mirrored: the board cannot model this pick"
            mirrored += 1
        elif team_slot == slot:
            row = sim_recs.iloc[0]
            take_key, take_name = str(sim_recs.index[0]), str(row["name"])
            take_pos, take_proj = str(row["position"]), round(float(row["proj_points"]), 1)
            basis = "model recommendation for the simulated roster"
        else:
            p_blend = wf.probabilities(sim_recs, sim_recent)
            i = (int(np.argmax(p_blend)) if policy == "argmax"
                 else int(rng.choice(len(p_blend), p=p_blend)))
            row = sim_recs.iloc[i]
            take_key, take_name = str(sim_recs.index[i]), str(row["name"])
            take_pos, take_proj = str(row["position"]), round(float(row["proj_points"]), 1)
            basis = f"walk-forward blend, {policy}"
            other_total += 1
            if take_key != key:
                other_changed += 1
        sim_taken.add(take_key)
        if take_pos:
            count(sim_rosters, team_slot, str(take_pos))
            sim_recent.append(str(take_pos))
        sim_picks.append({"overall": overall, "slot": team_slot, "name": take_name,
                          "position": take_pos})
        if team_slot == slot:
            subs.append({
                "pick": overall, "round": (overall - 1) // league.teams + 1,
                "real": p["name"],
                "real_position": (str(pos_of[key]) if on_board else p.get("position")),
                "real_proj": (round(float(proj_of[key]), 1) if on_board else None),
                "model": take_name, "model_position": take_pos, "model_proj": take_proj,
                "same": take_key == key, "basis": basis,
            })
        # The real world: score the predictor on what actually happened, then let
        # it learn from it.
        real_recs = recs_for(team_slot, overall, real_taken, real_rosters)
        if len(real_recs):
            wf.observe(real_recs, key if key in real_recs.index else None, real_recent, overall)
        real_pos = str(pos_of[key]) if on_board else p.get("position")
        if real_pos:
            count(real_rosters, team_slot, str(real_pos))
            real_recent.append(str(real_pos))
        real_taken.add(key)

    def roster_rows(rows: list[dict]) -> list[dict]:
        out = []
        for q in rows:
            k = norm_name(q["name"])
            out.append({"pick": q["overall"], "round": (q["overall"] - 1) // league.teams + 1,
                        "player": q["name"], "position": q.get("position"),
                        "proj_points": (round(float(proj_of[k]), 1) if k in proj_of else None)})
        return out

    sim_slot_picks = [q for q in sim_picks if q["slot"] == slot]
    real_slot_picks = [q for q in picks if q["slot"] == slot]
    sim_value = lineup_value(b, sim_slot_picks, league)
    real_value = lineup_value(b, real_slot_picks, league)
    return {
        "simulation": True,
        "note": ("Simulated draft, not a measurement. The model drafts for slot "
                 f"{slot}; every other team takes the walk-forward blend predictor's "
                 f"{policy} choice, fitted prequentially on the real picks. Picks the "
                 "board cannot model are mirrored. Projections and ADP are today's."),
        "slot": slot, "mine": slot == state.my_slot, "policy": policy, "seed": seed,
        "picks_replayed": len(picks), "adp_shift": adp_shift,
        "model_roster": roster_rows(sim_slot_picks),
        "real_roster": roster_rows(real_slot_picks),
        "starters_proj": {"model": sim_value["starters_proj"],
                          "real": real_value["starters_proj"],
                          "delta": sim_value["starters_proj"] - real_value["starters_proj"]},
        "bench_proj": {"model": sim_value["bench_proj"], "real": real_value["bench_proj"]},
        "open_starter_slots": {"model": sim_value["open_starter_slots"],
                               "real": real_value["open_starter_slots"]},
        "substitutions": subs,
        "substitutions_made": sum(1 for s in subs if not s["same"]),
        "divergence": {"other_team_picks": other_total,
                       "other_team_picks_changed": other_changed,
                       "mirrored_off_board": mirrored},
    }


# A team whose median pick passed at most this many better-ranked players on
# ESPN's own list is drafting from that list.
SHEET_FOLLOWER_PASSES = 3


def team_tendency(board: pd.DataFrame, state: DraftState, slot: int) -> dict:
    """How one team has been choosing: for each of its picks, how many players
    ranked higher on ESPN's list were still available (`espn_passes`), and
    which positions it has taken. ESPN rank is today's, not the pick's."""
    b = board.copy()
    if "_key" not in b.columns:
        b["_key"] = b["name"].map(norm_name)
    rank = dict(zip(b["_key"], pd.to_numeric(b.get("espn_rank"), errors="coerce"))) \
        if "espn_rank" in b.columns else {}
    pos_of = dict(zip(b["_key"], b["position"]))
    taken: set[str] = set()
    rows = []
    for p in sorted(state.picks, key=lambda x: x["overall"]):
        k = norm_name(p["name"])
        if p["slot"] == slot:
            r = rank.get(k)
            passes = None
            if r is not None and pd.notna(r):
                passes = int(sum(1 for kk, rr in rank.items()
                                 if kk not in taken and pd.notna(rr) and rr < r))
            rows.append({"pick": p["overall"], "player": p["name"],
                         "position": pos_of.get(k) or p.get("position"),
                         "espn_rank": (int(r) if r is not None and pd.notna(r) else None),
                         "espn_passes": passes})
        taken.add(k)
    passes = [r["espn_passes"] for r in rows if r["espn_passes"] is not None]
    positions: dict[str, int] = {}
    for r in rows:
        if r["position"]:
            pos = str(r["position"])
            positions[pos] = positions.get(pos, 0) + 1
    median = float(np.median(passes)) if passes else None
    return {"slot": slot, "picks": rows, "positions": positions,
            "median_espn_passes": median,
            "follows_espn_list": (median is not None and median <= SHEET_FOLLOWER_PASSES)}


def predict_pick(board: pd.DataFrame, state: DraftState, league: LeagueSettings,
                 slot: int, adp_shift: float | dict[str, float] = 0.0) -> dict:
    """For the team drafting at `slot`: what the model would take for its roster
    (`should`), what ESPN's list says next (`espn_list`), the team's tendency,
    and a prediction that follows whichever list the team has been following."""
    b = board.copy()
    if "_key" not in b.columns:
        b["_key"] = b["name"].map(norm_name)
    taken = state.taken_keys()
    pool = b[~b["_key"].isin(taken)].copy()
    roster: dict[str, int] = {}
    pos_of = dict(zip(b["_key"], b["position"]))
    for p in state.picks:
        if p["slot"] == slot:
            pos = pos_of.get(norm_name(p["name"])) or p.get("position")
            if pos:
                roster[pos] = roster.get(pos, 0) + 1
    overall = state.on_the_clock
    later = [n for n in league.picks_for_slot(slot) if n > overall]
    next_pick = later[0] if later else None
    recs = model.recommend(pool, league, current_pick=overall, next_pick=next_pick,
                           roster=roster, top_n=5, adp_shift=adp_shift)
    should = [{"player": r["name"], "position": r["position"],
               "proj_points": round(float(r["proj_points"]), 1),
               "pick_value": round(float(r["pick_value"]), 2)} for _, r in recs.iterrows()]
    espn_list: list[dict] = []
    if "espn_rank" in pool.columns:
        ranked = pool[pd.to_numeric(pool["espn_rank"], errors="coerce").notna()]
        ranked = ranked.sort_values("espn_rank").head(8)
        espn_list = [{"player": r["name"], "position": r["position"],
                      "espn_rank": int(r["espn_rank"]),
                      "adp": round(float(r["adp"]), 1)} for _, r in ranked.iterrows()]
    tendency = team_tendency(b, state, slot)
    caps = model.ROSTER_CAP
    open_slots = {pos: n - roster.get(pos, 0) for pos, n in league.starters.items()
                  if n and roster.get(pos, 0) < n and pos in ("QB", "RB", "WR", "TE")}
    if tendency["follows_espn_list"] and espn_list:
        # Best on ESPN's list at a position they can still use; an empty
        # starting slot first.
        usable = [e for e in espn_list if roster.get(e["position"], 0) < caps.get(e["position"], 6)]
        needed = [e for e in usable if e["position"] in open_slots]
        choice = (needed or usable or espn_list)[0]
        basis = ("ESPN list order at an open starting slot" if needed
                 else "ESPN list order at a position they can still roster")
    elif should:
        choice, basis = should[0], "model recommendation for their roster"
    else:
        choice, basis = {}, "no pool"
    return {"slot": slot, "on_the_clock": overall, "next_pick": next_pick, "roster": roster,
            "open_starter_slots": open_slots, "should": should, "espn_list": espn_list,
            "tendency": {k: v for k, v in tendency.items() if k != "picks"},
            "history": tendency["picks"],
            "predicted": {"player": choice.get("player"), "position": choice.get("position"),
                          "basis": basis}}


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
