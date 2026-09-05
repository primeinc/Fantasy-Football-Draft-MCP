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

import pandas as pd

from .config import LeagueSettings

# The slot a player fills, named on each starter. FLEX and superflex say so
# rather than reporting the player's own position, because "your third receiver
# is starting in the flex" is a different fact from "you start three receivers"
# and only the first is true.
SLOT_COLUMN = "lineup_slot_filled"
FLEX_SLOT = "FLEX"
SUPERFLEX_SLOT = "SUPERFLEX"


def placeable(rows: pd.DataFrame) -> pd.Series:
    """Whether each row carries a position that can fill a slot.

    A position is a non-empty string, the same question `DraftState._position_of`
    asks. A row whose board position is missing arrives here as NaN, and NaN
    matches no slot, so such a row can never be selected as a starter -- which
    means that without this it falls into the bench and is offered as droppable.
    That is the waiver defect wearing a different coat: the player might be the
    only kicker on the roster, and the board being broken about him is not a
    reason to cut him. Flagged by lena.
    """
    if "position" not in rows.columns or rows.empty:
        return pd.Series(False, index=rows.index)
    return rows["position"].map(lambda p: isinstance(p, str) and bool(p))


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
