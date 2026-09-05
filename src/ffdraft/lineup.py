"""Which of a roster's players start, and which sit.

Written because two callers were answering it by guessing. `waivers` took the
bench as a positional slice of board-ordered rows -- "everyone outside my top
eight by rank" -- which on an ordinary receiver-heavy roster calls five
receivers starters and offers the team's only kicker, only defense and only
tight end as droppable. `drop_candidate` then picks the lowest bench value of
those three, so the tool could recommend dropping the only kicker and leave a
starting slot that cannot be filled. #44's `set_lineup` needs the same answer
for a different reason, so it is one function rather than a second guess.

The slot counts come from `league.starters`, which `board.espn_league_context`
parses out of mSettings' `lineupSlotCounts`. Nothing here hardcodes a slot map:
a league with three receivers and a superflex gets three receivers and a
superflex because its settings said so.
"""
from __future__ import annotations

from collections.abc import Mapping

import pandas as pd

from .board import is_position
from .config import OUT_STATUSES, LeagueSettings

# SEASON_GAMES is imported rather than restated: I wrote `= 17` here first,
# which would have been a second copy of the number `roles` already owns, in the
# same hour I was objecting to exactly that. A heavier import is worth it.
from .roles import SEASON_GAMES

# The slot a player fills, named on each starter. FLEX and superflex say so
# rather than reporting the player's own position, because "your third receiver
# is starting in the flex" is a different fact from "you start three receivers"
# and only the first is true.
SLOT_COLUMN = "lineup_slot_filled"
FLEX_SLOT = "FLEX"
SUPERFLEX_SLOT = "SUPERFLEX"


def placeable(rows: pd.DataFrame) -> pd.Series:
    """Whether each row carries a position that can fill a slot.

    Through `board.is_position`, which is the one place that rule lives -- it had
    been written separately four times and disagreed with itself three of them.
    A row whose board position is missing arrives here as NaN, and NaN matches
    no slot, so such a row can never be selected as a starter, which means that
    without this it falls into the bench and is offered as droppable. That is the
    waiver defect wearing a different coat: the player might be the only kicker
    on the roster, and the board being broken about him is not a reason to cut
    him. Flagged by lena.
    """
    if "position" not in rows.columns or rows.empty:
        return pd.Series(False, index=rows.index)
    return rows["position"].map(is_position)


def _take(pool: pd.DataFrame, eligible: tuple[str, ...], count: int,
          value: str) -> list[int]:
    """Positions (not index labels) of the best `count` available from `eligible`.

    Positional throughout, because a caller can hand us a frame with duplicate
    index labels -- `pd.concat` without `ignore_index` produces one -- and `.loc`
    on a duplicated label silently returns more rows than it was asked for. The
    roster frames in this repo happen to be clean today; that is a property of
    their construction, not of this function's inputs.
    """
    if count <= 0 or pool.empty:
        return []
    chunk = pool[pool["position"].isin(eligible)]
    if chunk.empty:
        return []
    return list(chunk.sort_values(value, ascending=False).head(count)["_pos"])


def starting_lineup(rows: pd.DataFrame, league: LeagueSettings,
                    value: str = "proj_points") -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fill the league's starting slots from a roster. Returns (starters, bench).

    Base slots first, best available at each by `value`, then FLEX from whatever
    is left among `league.flex_eligible`, then superflex including quarterbacks.
    Filling the base slots first is what stops a fourth receiver taking the flex
    while the tight end slot sits empty, since the base slot has no alternative
    and the flex does.

    `value` is the column the lineup maximises. It defaults to the season
    projection, which is the right question for "who is on my bench"; `set_lineup`
    passes a weekly number for "who do I start in week 9". The two questions have
    the same shape and differ only in what is being maximised, which is why this
    takes a column rather than assuming one.

    A stand-in from #40 sorts on his replacement-level projection like anyone
    else and can start, which is correct: an unpriced kicker is still the only
    kicker you have, and a lineup that left the slot empty because the board
    could not price him would be a worse answer than starting him.

    A slot with nobody eligible is left unfilled rather than filled with somebody
    ineligible, and the caller can see it by comparing the starters' slots
    against the league's. That is the case worth surfacing -- an empty starting
    slot is a real problem for the user -- and it is not this function's job to
    hide it by promoting a player who cannot play there.
    """
    if rows.empty or "position" not in rows.columns:
        empty = rows.iloc[0:0].copy()
        empty[SLOT_COLUMN] = pd.Series(dtype="object")
        return empty, rows.copy()

    pool = rows.copy()
    pool["_pos"] = range(len(pool))
    if value not in pool.columns:
        pool[value] = 0.0
    pool[value] = pd.to_numeric(pool[value], errors="coerce").fillna(0.0)
    pool = pool[placeable(rows).to_numpy()]

    filled: dict[int, str] = {}

    def remaining() -> pd.DataFrame:
        return pool[~pool["_pos"].isin(list(filled))]

    for position, count in league.starters.items():
        if position == FLEX_SLOT or not count:
            continue
        for pos in _take(remaining(), (position,), int(count), value):
            filled[pos] = position
    for pos in _take(remaining(), tuple(league.flex_eligible),
                     int(league.starters.get(FLEX_SLOT, 0)), value):
        filled[pos] = FLEX_SLOT
    for pos in _take(remaining(), tuple(league.flex_eligible) + ("QB",),
                     int(league.superflex or 0), value):
        filled[pos] = SUPERFLEX_SLOT

    starting = sorted(filled)
    starters = rows.iloc[starting].copy()
    starters[SLOT_COLUMN] = [filled[p] for p in starting]
    bench = rows.iloc[[p for p in range(len(rows)) if p not in filled]].copy()
    return starters, bench


WEEK_VALUE = "week_points"
WEEK_BASIS = "week_points_basis"

# Why a player is worth what he is worth this week, per row. A fallback applied
# silently is #39's defect in a new place: the number is not wrong, it just
# cannot be told apart from a measured one. 639 of the 1036 players in the live
# pull carry no weekly projection at all, so the fallback is the ordinary case
# here rather than the exception.
BASIS_BYE = "bye week: he does not play"
BASIS_OUT = "ESPN has him out"
BASIS_ESPN = "ESPN's projection for this week"
BASIS_PER_GAME = "the board's per-game rate; ESPN has no weekly projection"
BASIS_SEASON = "the board's season projection spread over the season"
BASIS_NONE = "nothing to price him with"


def week_value(rows: pd.DataFrame, week: int,
               espn_weekly: Mapping[str, float] | None = None) -> pd.DataFrame:
    """What each player is worth in `week`, and on what basis, per row.

    Order: a bye or an ESPN out-status is zero whatever else is known, because
    a player who does not play scores nothing and no projection changes that.
    Then ESPN's own weekly number if the pull carried one for him, then the
    board's per-game rate, then the season projection spread over the season.

    `OUT_STATUSES` comes from `config` -- the same list `waivers` uses, and
    deliberately not including QUESTIONABLE, which by Friday describes half the
    league and would bench a starter on a coin flip.

    Returns the frame with two columns added rather than a bare series, because
    the basis has to travel with the number. A caller that sees only the value
    cannot tell a measured week from a season average divided by seventeen.
    """
    out = rows.copy()
    if out.empty:
        out[WEEK_VALUE] = pd.Series(dtype=float)
        out[WEEK_BASIS] = pd.Series(dtype="object")
        return out

    weekly = {str(k): float(v) for k, v in (espn_weekly or {}).items()}
    ids = (out["espn_id"].astype("object") if "espn_id" in out.columns
           else pd.Series([None] * len(out), index=out.index))
    bye = (pd.to_numeric(out["bye_week"], errors="coerce")
           if "bye_week" in out.columns else pd.Series(float("nan"), index=out.index))
    status = (out["espn_injury"].astype("object") if "espn_injury" in out.columns
              else pd.Series([None] * len(out), index=out.index))
    per_game = (pd.to_numeric(out["adj_ppg"], errors="coerce")
                if "adj_ppg" in out.columns else pd.Series(float("nan"), index=out.index))
    season = (pd.to_numeric(out["proj_points"], errors="coerce")
              if "proj_points" in out.columns
              else pd.Series(float("nan"), index=out.index))

    values, bases = [], []
    for i in out.index:
        if pd.notna(bye.at[i]) and int(bye.at[i]) == int(week):
            values.append(0.0), bases.append(BASIS_BYE)
        elif is_out(status.at[i]):
            values.append(0.0), bases.append(BASIS_OUT)
        elif str(ids.at[i]) in weekly:
            values.append(weekly[str(ids.at[i])]), bases.append(BASIS_ESPN)
        elif pd.notna(per_game.at[i]):
            values.append(float(per_game.at[i])), bases.append(BASIS_PER_GAME)
        elif pd.notna(season.at[i]):
            values.append(float(season.at[i]) / SEASON_GAMES), bases.append(BASIS_SEASON)
        else:
            values.append(0.0), bases.append(BASIS_NONE)
    out[WEEK_VALUE] = values
    out[WEEK_BASIS] = bases
    return out


def is_out(status: object) -> bool:
    """Whether ESPN's injury status means he does not play."""
    return isinstance(status, str) and status.upper() in OUT_STATUSES


def unfilled_slots(starters: pd.DataFrame, league: LeagueSettings) -> dict[str, int]:
    """Starting slots the roster could not fill, by slot.

    The answer `waivers` actually needs: a claim that fills an empty starting
    slot is worth more than one that improves a bench, and a drop that empties
    one is the recommendation this whole module exists to prevent.
    """
    want = {p: int(c) for p, c in league.starters.items() if c}
    if league.superflex:
        want[SUPERFLEX_SLOT] = int(league.superflex)
    have = ({} if starters.empty or SLOT_COLUMN not in starters.columns
            else starters[SLOT_COLUMN].value_counts().to_dict())
    return {slot: n - int(have.get(slot, 0)) for slot, n in want.items()
            if n - int(have.get(slot, 0)) > 0}


def slot_alternatives(starter: pd.Series, bench: pd.DataFrame,
                      league: LeagueSettings, value: str) -> list[dict]:
    """Who else could fill this starter's slot, and what it would cost.

    Eligibility is the slot's, not the player's: a FLEX starter's alternatives
    are everyone flex-eligible on the bench, while a K starter's are the other
    kickers. `costs` is negative because these are alternatives to a lineup that
    already maximises the total -- if any were positive the lineup would be
    wrong, which makes the sign a check on the caller rather than decoration.
    """
    slot = str(starter.get(SLOT_COLUMN) or "")
    if slot == FLEX_SLOT:
        eligible = tuple(league.flex_eligible)
    elif slot == SUPERFLEX_SLOT:
        eligible = tuple(league.flex_eligible) + ("QB",)
    else:
        eligible = (slot,)
    if bench.empty or "position" not in bench.columns:
        return []
    chunk = bench[bench["position"].isin(eligible)]
    mine = float(starter.get(value) or 0.0)
    return [{"player": str(r["name"]),
             "week_points": round(float(r.get(value) or 0.0), 1),
             "costs": round(float(r.get(value) or 0.0) - mine, 1)}
            for _, r in chunk.sort_values(value, ascending=False).iterrows()]


def why_started(row: pd.Series, alternatives: list[dict], value: str) -> str:
    """Why this player is in this slot, in the numbers, direction spelled out.

    The #39 pattern: never leave the comparison to a minus sign. "3.2 more than"
    and "the only one eligible" are different reasons to start somebody and the
    reader should not have to work out which one applies.
    """
    points = float(row.get(value) or 0.0)
    basis = str(row.get(WEEK_BASIS) or "")
    head = f"{points:.1f} expected in {str(row.get(SLOT_COLUMN))} ({basis})"
    if not alternatives:
        return f"{head}; the only one you have who can fill the slot"
    best = alternatives[0]
    if points <= 0:
        return (f"{head}; {best['player']} is the alternative at "
                f"{best['week_points']:.1f} and nobody here scores")
    return (f"{head}; {best['week_points']:.1f} more than "
            f"{best['player']}" if best["costs"] > 0 else
            f"{head}; {abs(best['costs']):.1f} more than the next best, "
            f"{best['player']} at {best['week_points']:.1f}")


def against_espn(mine: pd.DataFrame, espn_started: pd.DataFrame,
                 value: str) -> dict:
    """This lineup against the one ESPN currently has set, on the same roster.

    The control the task asks for. Two lineups over one roster differ only in
    who is in and who is out, so the comparison is set arithmetic plus the
    points those swaps are worth -- and the points are in OUR valuation, since
    that is the number being defended. A positive `gain` is what this lineup
    claims over ESPN's; if it is negative the recommendation is wrong and the
    field says so rather than being omitted.
    """
    if espn_started.empty:
        return {"espn_lineup_known": False, "bench": [], "start": [], "gain": None,
                "note": "ESPN has no lineup set for this week yet"}
    ours, theirs = set(mine["name"]), set(espn_started["name"])
    points = {str(r["name"]): float(r.get(value) or 0.0)
              for _, r in pd.concat([mine, espn_started]).iterrows()}
    start = sorted(ours - theirs)
    bench = sorted(theirs - ours)
    return {"espn_lineup_known": True,
            "start": [{"player": p, "week_points": round(points.get(p, 0.0), 1)}
                      for p in start],
            "bench": [{"player": p, "week_points": round(points.get(p, 0.0), 1)}
                      for p in bench],
            "gain": round(sum(points.get(p, 0.0) for p in start)
                          - sum(points.get(p, 0.0) for p in bench), 1)}


def droppable(rows: pd.DataFrame, league: LeagueSettings,
              value: str = "proj_points") -> pd.DataFrame:
    """Everyone who can be dropped without emptying a starting slot.

    The bench, by the lineup above rather than by rank order. A player is here
    because the league's slots are filled without him, which is the only sense
    of "droppable" that is safe to hand a waiver tool.

    Nothing special happens to a #40 stand-in: he is droppable when the lineup
    does not need him, like anyone else. Worth saying because the opposite is a
    tempting shortcut -- a player the board cannot price looks like the safe
    cut, and he is exactly as safe as the lineup says he is. The only kicker on
    the roster is a starter whether or not the board has a projection for him.

    A row with no usable position is NOT here. It cannot fill a slot, so the
    lineup never selects it, so it would otherwise fall into the bench and be
    offered as the cut -- and it may be the only kicker on the roster with a
    broken board row. A player this function cannot place is a player it cannot
    clear, and saying nothing about him is the honest answer.
    """
    bench = starting_lineup(rows, league, value)[1]
    return bench[placeable(bench).to_numpy()]


def unplaceable(rows: pd.DataFrame) -> pd.DataFrame:
    """Roster rows carrying no usable position, so no slot can be reasoned about.

    Separated rather than hidden: an empty frame here means the roster is fully
    understood, and a non-empty one is a defect in the board worth reporting to
    whoever built it, not a set of players to cut.
    """
    return rows[~placeable(rows).to_numpy()]
