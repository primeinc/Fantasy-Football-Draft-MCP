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

from pathlib import Path

import numpy as np
import pandas as pd

from . import choice, model
from .board import DraftState, lineup_value
from .config import LeagueSettings
from .names import normalize as norm_name

CALIBRATION_BINS = ((0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01))
# Picks at a position before its own median reach is used instead of the room's.
DRIFT_MIN_PICKS = 8
# The market columns an as-of replay takes from the snapshot instead of the
# board. Everything else -- projections, roles, consistency -- is the model's
# own and was never a moving ESPN number.
AS_OF_COLUMNS = ("adp", "espn_rank", "espn_proj")


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


def _key_rows(b: pd.DataFrame) -> dict[str, list[int]]:
    """Normalised name -> the board rows carrying it, in board order. Usually one
    row per name; the point of the list is the times it is not."""
    out: dict[str, list[int]] = {}
    for row, key in zip(b["_row"], b["_key"]):
        out.setdefault(str(key), []).append(int(row))
    return out


def _row_for(rows_of_key: dict[str, list[int]], pos_of: dict[int, object], key: str,
             taken: set[int], position: str | None) -> tuple[int | None, bool]:
    """The board row a recorded pick refers to, and whether the choice was
    arbitrary.

    A pick carries a name, not an id, so a name on two rows is settled by the
    pick's own position where it has one. Where it does not -- some sync paths
    record no position -- the first untaken row wins, which is a coin flip, and
    worse: if an earlier pick resolved arbitrarily to the row a later pick wanted
    the later one silently takes a wrong-position row. The second return value
    says a guess was made, so the caller can report how often it happened
    instead of the replay quietly pretending it knew.
    """
    free = [r for r in rows_of_key.get(key, ()) if r not in taken]
    if not free:
        return None, False
    if position:
        for r in free:
            if str(pos_of.get(r)) == str(position):
                return r, False
    return free[0], len(free) > 1


def _rows_for_picks(b: pd.DataFrame, picks: list[dict]) -> tuple[dict[int, int | None], list[int]]:
    """Resolve every recorded pick to a board row, once, in draft order. Doing it
    up front means a pick can be looked up before the walk reaches it, which the
    survival forecasts need. Returns the rows and the picks whose row was a
    guess between two same-name candidates."""
    rows_of_key = _key_rows(b)
    # numpy int64 keys here, Python int keys in `rows_of_key`. The lookups cross
    # between them and work because the two hash and compare equal -- load
    # bearing, so do not "tidy" one side without the other.
    pos_of = dict(zip(b["_row"], b["position"]))
    seen: set[int] = set()
    out: dict[int, int | None] = {}
    guessed: list[int] = []
    for p in picks:
        row, arbitrary = _row_for(rows_of_key, pos_of, norm_name(p["name"]), seen,
                                  p.get("position"))
        out[p["overall"]] = row
        if arbitrary:
            guessed.append(p["overall"])
        if row is not None:
            seen.add(row)
    return out, guessed


def _as_of_coverage(rows: list[dict], snapshots: str | Path | None) -> dict:
    """What an as-of replay actually managed to price from snapshots. Reported
    because the honest answer is usually "some of it": the watch only snapshots
    from the moment it connects, and each snapshot holds the top
    `watch.SNAPSHOT_ROWS` available players, not the whole board."""
    with_snap = [r for r in rows if r["snapshot"]]
    scored = [r for r in rows if r["snapshot"] and r["pool_rows"]]
    return {
        # The directory's own name, not its absolute path: the answer goes back
        # over MCP and a home directory carries the user's account name.
        "snapshots": (Path(str(snapshots)).name if snapshots is not None else None),
        "picks": len(rows),
        "picks_with_snapshot": len(with_snap),
        "coverage": round(len(with_snap) / len(rows), 3) if rows else None,
        "first_pick_with_snapshot": (min(r["pick"] for r in with_snap) if with_snap else None),
        "last_pick_with_snapshot": (max(r["pick"] for r in with_snap) if with_snap else None),
        # How much of the pool each snapshot reached, and how often the player
        # actually taken was in it -- the pick whose price the replay is scoring.
        "mean_pool_share": (round(float(np.mean([r["rows_from_snapshot"] / r["pool_rows"]
                                                 for r in scored])), 3) if scored else None),
        "actual_pick_covered": sum(1 for r in with_snap if r["actual_covered"]),
        "picks_without_snapshot": [r["pick"] for r in rows if not r["snapshot"]][:20],
    }


def _apply_snapshot(pool: pd.DataFrame, snap: pd.DataFrame) -> tuple[pd.DataFrame, set[str]]:
    """Overwrite the pool's market columns with the values a snapshot recorded,
    for the rows it covers. Rows the snapshot does not carry keep today's
    numbers; the returned set is the keys that were covered, which is what makes
    an as-of replay's coverage reportable rather than assumed."""
    out = pool.copy()
    snap = snap.drop_duplicates("_key")
    idx = snap.set_index("_key")
    covered = set(idx.index) & set(out["_key"])
    for col in AS_OF_COLUMNS:
        if col in idx.columns and col in out.columns:
            mapped = out["_key"].map(idx[col])
            out[col] = out[col].where(mapped.isna(), mapped)
    return out, covered


def replay_draft(board: pd.DataFrame, state: DraftState, league: LeagueSettings,
                 candidates: int = 10, adp_shift: float | dict[str, float] = 0.0,
                 walk_forward: bool = True, team_effects: bool | None = None,
                 as_of: bool = False, snapshots: str | Path | None = None) -> dict:
    """Score every recorded pick against the model and return per-pick rows,
    per-team totals, and the survival model's calibration. `adp_shift` is
    passed to recommend() so the calibration can be read with and without the
    room's drift applied. With `walk_forward` the choice predictors are scored
    prequentially as well and a forecast for the pick on the clock is returned.
    `team_effects` adds `choice`'s per-team predictor to that score sheet,
    defaulting to `choice.TEAM_EFFECTS` (off).

    With `as_of` each pick is priced from the market snapshot the watch wrote
    when that pick was on the clock (`watch.write_snapshot`) instead of today's
    ADP, ESPN rank and ESPN projection. `snapshots` is the league id or the
    directory holding them. Coverage is reported, never assumed: a pick with no
    snapshot, and a player the snapshot did not reach, keep today's numbers and
    are counted as uncovered in the `as_of` block."""
    b = board.copy()
    if "_key" not in b.columns:
        b["_key"] = b["name"].map(norm_name)
    # Keyed by board row, not by normalised name. Two rows can share a key -- the
    # same player listed twice, or two real players with one name at different
    # positions, which the position-aware market join now lets onto the board --
    # and a name-keyed pool removes both when one is taken.
    b = b.reset_index(drop=True)
    b["_row"] = np.arange(len(b), dtype=int)
    b = b.set_index("_row", drop=False)
    as_of_from: str | Path | None = None
    if as_of:
        if snapshots is None:
            raise ValueError("as_of needs `snapshots`: the league id or the snapshot directory")
        as_of_from = snapshots
    picks = sorted(state.picks, key=lambda p: p["overall"])
    # Which board row each recorded pick refers to, resolved once in draft order:
    # a pick carries a name, so a duplicate key is settled by position and by
    # what the picks before it already took. None for a pick the board cannot
    # model at all.
    row_at, guessed_rows = _rows_for_picks(b, picks)
    taken_at: dict[int, int] = {r: p["overall"] for p in picks
                                if (r := row_at[p["overall"]]) is not None}
    rosters: dict[int, dict[str, int]] = {}
    rows: list[dict] = []
    forecasts: list[tuple[float, bool, int, str]] = []
    taken: set[int] = set()
    wf = choice.WalkForward(team_effects=team_effects) if walk_forward else None
    recent_positions: list[str] = []

    def recommend_for(slot: int, overall: int, pool: pd.DataFrame) -> tuple[pd.DataFrame, int | None]:
        later = [n for n in league.picks_for_slot(slot) if n > overall]
        next_pick = later[0] if later else None
        recs = model.recommend(pool, league, current_pick=overall, next_pick=next_pick,
                               roster=rosters.get(slot, {}), top_n=len(pool),
                               adp_shift=adp_shift)
        return recs, next_pick

    as_of_rows: list[dict] = []
    for p in picks:
        overall, slot = p["overall"], p["slot"]
        key = norm_name(p["name"])
        taken_row = row_at[overall]
        pool = b[~b["_row"].isin(taken)]
        as_of_row: dict | None = None
        if as_of_from is not None:
            from .watch import read_snapshot

            snap = read_snapshot(as_of_from, overall)
            covered: set[str] = set()
            if snap is not None:
                pool, covered = _apply_snapshot(pool, snap)
            as_of_row = {"pick": overall, "snapshot": snap is not None,
                         "pool_rows": len(pool), "rows_from_snapshot": len(covered),
                         "actual_covered": key in covered}
            as_of_rows.append(as_of_row)
        recs, next_pick = recommend_for(slot, overall, pool)
        if wf is not None and len(recs):
            wf.observe(recs, taken_row if taken_row in recs.index else None,
                       recent_positions, overall, slot)
        where = (np.flatnonzero(recs.index.to_numpy() == taken_row)
                 if taken_row is not None else np.array([], dtype=int))
        rnd = (overall - 1) // league.teams + 1
        row = {
            "pick": overall, "round": rnd, "slot": slot,
            "actual": p["name"], "position": p.get("position"), "actual_proj": None,
            "actual_espn_proj": None, "actual_rank": None, "choice_percentile": None,
            "off_board": True, "model_pick": None, "model_pick_proj": None,
            "proj_gap": None, "pick_regret": None, "reach": None, "market_z": None,
            "need_mult": None, "role_mult": None, "p_available_next": None,
        }
        if as_of_row is not None:
            # Per row, not only in the coverage block: a reader scanning these
            # will not cross-reference, and "priced as of this pick" versus
            # "silently priced with today's ADP" is a per-row difference.
            row["as_of"] = as_of_row["actual_covered"]
            row["as_of_pool_share"] = (round(as_of_row["rows_from_snapshot"]
                                             / as_of_row["pool_rows"], 3)
                                       if as_of_row["pool_rows"] else None)
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
                    gone_before = taken_at.get(int(k), 10**9) < next_pick
                    forecasts.append((p_avail, not gone_before, rnd, str(r["position"])))
        pos = row["position"]
        if pos:
            rosters.setdefault(slot, {})[pos] = rosters.get(slot, {}).get(pos, 0) + 1
            recent_positions.append(str(pos))
        if taken_row is not None:
            taken.add(taken_row)

    per_pick = pd.DataFrame(rows)
    teams = _team_totals(per_pick, league, state.my_slot)
    out = {
        "picks_scored": len(rows), "adp_shift": adp_shift,
        "room_drift": room_drift(board, state),
        "overall": _overall(per_pick, forecasts),
        # Picks whose board row was a guess between two rows sharing a name,
        # because the recorded pick carried no position to settle it. Normally
        # empty; reported rather than assumed, the same way off_board is.
        "ambiguous_name_picks": guessed_rows,
        "teams": teams.to_dict(orient="records"),
        # The dict rows, not the frame: a frame turns None into NaN.
        "picks": rows,
    }
    if as_of:
        out["as_of"] = _as_of_coverage(as_of_rows, snapshots)
    if wf is not None:
        out["predictors"] = wf.summary()
        out["predictor_rows"] = wf.rows
        on_clock = state.on_the_clock
        if on_clock <= league.teams * league.rounds:
            slot = state.slot_for_pick(on_clock)
            pool = b[~b["_row"].isin(taken)]
            recs, next_pick = recommend_for(slot, on_clock, pool)
            out["forecast"] = {"pick": on_clock, "slot": slot, "next_pick": next_pick,
                               **wf.forecast(recs, recent_positions, slot=slot)}
    return out


# What a team other than the model does at its turn in a counterfactual.
COUNTERFACTUAL_POLICIES = ("argmax", "sample")


class _Arm:
    """One simulated timeline: who has been taken, each team's roster, the
    positional run, and the picks made. Players are held by board row, not by
    name key, because two board rows can share a normalised key (the same player
    listed twice, or two real players with one name at different positions) and
    a name-keyed pool would remove both at once."""

    def __init__(self) -> None:
        self.taken: set[int] = set()
        self.rosters: dict[int, dict[str, int]] = {}
        self.recent: list[str] = []
        self.picks: list[dict] = []
        self.counts: dict[str, int] = {}

    def bump(self, name: str) -> None:
        self.counts[name] = self.counts.get(name, 0) + 1

    def take(self, overall: int, slot: int, row: int | None, name: str,
             position: str | None, proj: float | None) -> None:
        if row is not None:
            self.taken.add(row)
        if position:
            held = self.rosters.setdefault(slot, {})
            held[str(position)] = held.get(str(position), 0) + 1
            self.recent.append(str(position))
        self.picks.append({"overall": overall, "slot": slot, "name": name,
                           "position": position, "proj_points": proj, "row": row})


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

    Three timelines run in step. The *real* one is only used to fit the
    predictor. The *model* arm is the intervention. The *control* arm is the same
    simulated room with the target team mirroring its real picks instead, which
    is what makes the answer readable: `starters_proj.delta_vs_control` holds the
    room fixed and is the intervention alone, while `delta_vs_real` also carries
    the difference between the predictor's room and the real one. With the
    predictor agreeing with the real room on only a fraction of picks, that
    second number is mostly the room, so read the first.

    A pick the board cannot model (a kicker, a defense, a player with no
    projection) is mirrored for the *other* teams: the board holds nobody the
    predictor could have named in his place, so predicting one would eat a
    modelled player who really was still there. It is NOT mirrored at the target
    slot, where the model picks from the board every turn -- the real pick scores
    0 there whatever happens, so nothing is taken from the comparison, and those
    are exactly the turns a substitution is most likely to be worth points.

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
    b = b.reset_index(drop=True)
    b["_row"] = np.arange(len(b), dtype=int)
    b = b.set_index("_row", drop=False)
    if adp_shift is None:
        adp_shift = room_drift(board, state)["shift"]
    proj_of = dict(zip(b["_row"], b["proj_points"]))
    pos_of = dict(zip(b["_row"], b["position"]))
    rows_of_key = _key_rows(b)
    rng = np.random.default_rng(seed)
    wf = choice.WalkForward()
    picks = sorted(state.picks, key=lambda p: p["overall"])
    real, arm_model, arm_control = _Arm(), _Arm(), _Arm()
    subs: list[dict] = []

    def row_for(arm: _Arm | None, key: str, taken: set[int],
                position: str | None) -> int | None:
        # Per timeline: each arm has taken different players, so the same
        # recorded pick can resolve to a different row in each of them, and an
        # arbitrary choice between two rows sharing a name is counted per arm.
        row, arbitrary = _row_for(rows_of_key, pos_of, key, taken, position)
        if arbitrary and arm is not None:
            arm.bump("ambiguous_name_rows")
        return row

    def recs_for(arm: _Arm, team_slot: int, overall: int) -> pd.DataFrame:
        pool = b[~b["_row"].isin(arm.taken)]
        if pool.empty:
            return pool
        later = [n for n in league.picks_for_slot(team_slot) if n > overall]
        return model.recommend(pool, league, current_pick=overall,
                               next_pick=(later[0] if later else None),
                               roster=arm.rosters.get(team_slot, {}), top_n=len(pool),
                               adp_shift=adp_shift)

    def predicted(recs: pd.DataFrame, arm: _Arm, team_slot: int) -> int:
        p_blend = wf.probabilities(recs, arm.recent, slot=team_slot)
        return (int(np.argmax(p_blend)) if policy == "argmax"
                else int(rng.choice(len(p_blend), p=p_blend)))

    def from_recs(recs: pd.DataFrame, i: int) -> tuple[int, str, str, float]:
        r = recs.iloc[i]
        return (int(recs.index[i]), str(r["name"]), str(r["position"]),
                round(float(r["proj_points"]), 1))

    def step(arm: _Arm, p: dict, model_drafts_target: bool) -> dict:
        """Decide and record this pick for one simulated timeline."""
        overall, team_slot = p["overall"], p["slot"]
        key = norm_name(p["name"])
        on_board = key in rows_of_key
        real_row = row_for(arm, key, arm.taken, p.get("position"))
        recs = recs_for(arm, team_slot, overall)
        if recs.empty:
            # A different event from an unmodelled pick, and it keeps its own
            # name and counter: the board ran out, so there is nobody to choose.
            arm.bump("pool_exhausted")
            arm.take(overall, team_slot, None, p["name"], p.get("position"), None)
            return {"name": p["name"], "position": p.get("position"), "proj": None,
                    "row": None, "basis": "the simulated pool is empty"}
        if team_slot == slot and model_drafts_target:
            row, name, pos, proj = from_recs(recs, 0)
            basis = "model recommendation for the simulated roster"
        elif team_slot == slot and real_row is not None:
            row, name = real_row, p["name"]
            pos, proj = str(pos_of[real_row]), round(float(proj_of[real_row]), 1)
            basis = "control: the real pick, still available"
        elif team_slot == slot and not on_board:
            row, name, pos, proj = None, p["name"], p.get("position"), None
            arm.bump("mirrored_off_board")
            basis = "control: the real pick, off the board"
        elif team_slot == slot:
            row, name, pos, proj = from_recs(recs, predicted(recs, arm, team_slot))
            arm.bump("control_picks_unavailable")
            basis = f"control: the real pick was already gone; walk-forward blend, {policy}"
        elif not on_board:
            row, name, pos, proj = None, p["name"], p.get("position"), None
            arm.bump("mirrored_off_board")
            basis = "mirrored: the board cannot model this pick"
        else:
            row, name, pos, proj = from_recs(recs, predicted(recs, arm, team_slot))
            arm.bump("other_team_picks")
            if row != real_row:
                arm.bump("other_team_picks_changed")
            basis = f"walk-forward blend, {policy}"
        arm.take(overall, team_slot, row, name, pos, proj)
        return {"name": name, "position": pos, "proj": proj, "row": row, "basis": basis}

    for p in picks:
        overall, team_slot = p["overall"], p["slot"]
        key = norm_name(p["name"])
        # Both arms are decided with the predictor as it stands: fitted on the
        # real picks before this one and nothing after.
        took = step(arm_model, p, model_drafts_target=True)
        control = step(arm_control, p, model_drafts_target=False)
        if team_slot == slot:
            real_row = row_for(None, key, real.taken, p.get("position"))
            subs.append({
                "pick": overall, "round": (overall - 1) // league.teams + 1,
                "real": p["name"],
                "real_position": (str(pos_of[real_row]) if real_row is not None
                                  else p.get("position")),
                "real_proj": (round(float(proj_of[real_row]), 1)
                              if real_row is not None else None),
                "model": took["name"], "model_position": took["position"],
                "model_proj": took["proj"], "basis": took["basis"],
                "control": control["name"], "control_proj": control["proj"],
                "control_is_real": control["name"] == p["name"],
                "same": took["name"] == p["name"],
            })
        # The real timeline: score the predictor on what actually happened, then
        # let it learn from it. Nothing here reaches either simulated arm except
        # through the fitted predictor.
        real_row = row_for(real, key, real.taken, p.get("position"))
        real_recs = recs_for(real, team_slot, overall)
        if len(real_recs):
            wf.observe(real_recs, real_row if real_row in real_recs.index else None,
                       real.recent, overall, team_slot)
        real.take(overall, team_slot, real_row, p["name"],
                  (str(pos_of[real_row]) if real_row is not None else p.get("position")),
                  (round(float(proj_of[real_row]), 1) if real_row is not None else None))

    def roster_rows(rows: list[dict]) -> list[dict]:
        return [{"pick": q["overall"], "round": (q["overall"] - 1) // league.teams + 1,
                 "player": q["name"], "position": q.get("position"),
                 "proj_points": (round(float(proj_of[q["row"]]), 1)
                                 if q.get("row") is not None else None)}
                for q in rows]

    def mine(arm: _Arm) -> list[dict]:
        return [q for q in arm.picks if q["slot"] == slot]

    # Scored by exactly the logic that scores a recorded draft. Every pick here
    # carries the projection and position of the board row it actually took, so
    # a duplicate normalised name cannot resolve to the other row.
    values = {name: lineup_value(b, mine(arm), league)
              for name, arm in (("model", arm_model), ("control", arm_control),
                                ("real", real))}
    starters = {name: v["starters_proj"] for name, v in values.items()}
    return {
        "simulation": True,
        "note": ("Simulated draft, not a measurement. The model drafts for slot "
                 f"{slot}; every other team takes the walk-forward blend predictor's "
                 f"{policy} choice, fitted prequentially on the real picks. The control "
                 "arm is the same simulated room with slot "
                 f"{slot} mirroring its real picks, so delta_vs_control is the "
                 "intervention with the room held fixed; delta_vs_real also carries the "
                 "difference between the predictor's room and the real one, which the "
                 "`divergence` block sizes. Where the predictor's room had already taken "
                 "a real pick the control falls back to the predictor too, so read "
                 "divergence.control_picks_unavailable before the delta: the more of "
                 "them, the less the control is the real drafter. Picks the board cannot "
                 "model are mirrored for the other teams. Projections and ADP are "
                 "today's."),
        "slot": slot, "mine": slot == state.my_slot, "policy": policy, "seed": seed,
        "picks_replayed": len(picks), "adp_shift": adp_shift,
        "model_roster": roster_rows(mine(arm_model)),
        "control_roster": roster_rows(mine(arm_control)),
        "real_roster": roster_rows(mine(real)),
        "starters_proj": {**starters,
                          "delta_vs_control": starters["model"] - starters["control"],
                          "delta_vs_real": starters["model"] - starters["real"]},
        "bench_proj": {name: v["bench_proj"] for name, v in values.items()},
        "open_starter_slots": {name: v["open_starter_slots"] for name, v in values.items()},
        "substitutions": subs,
        "substitutions_made": sum(1 for s in subs if not s["same"]),
        "divergence": {
            "other_team_picks": arm_model.counts.get("other_team_picks", 0),
            "other_team_picks_changed": arm_model.counts.get("other_team_picks_changed", 0),
            "mirrored_off_board": arm_model.counts.get("mirrored_off_board", 0),
            "pool_exhausted": arm_model.counts.get("pool_exhausted", 0),
            "control_picks_unavailable": arm_control.counts.get("control_picks_unavailable", 0),
            # Times a recorded pick with no position had to be resolved to one of
            # two board rows sharing its name. Normally 0.
            "ambiguous_name_rows": real.counts.get("ambiguous_name_rows", 0),
        },
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
