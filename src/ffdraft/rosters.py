"""The in-season ESPN roster reader: mRoster entries into the board's row shape.

One reader, two consumers. #44's `set_lineup` needs the roster it is choosing a
lineup from; #47's trade evaluator needs both sides of a trade as they stand
after waivers. Both used to have to invent it, and #47 currently reads the draft
record, which is the right source in week 1 and the wrong one in week 9.

SHAPE UNVERIFIED. Measured on the 11:37 capture: `mRoster` returns
`roster.entries: []` for all 16 teams while `draftDetail` reads
`drafted=False, inProgress=True`, which is the same rule that withholds picks
from the read API mid-draft. The `roster` and `tradeReservedEntries` keys are
present and empty, so the container is right and nothing inside it has been
seen. Everything here is written against ESPN's documented entry shape and the
fields `kona_player_info` already decodes, and none of it has been exercised
against a populated roster. A green fixture here is not evidence the live parse
works: the fixture and the parser were written from the same reading, so they
agree with each other by construction. `UNVERIFIED_SHAPE` says so on the output.

`mTeam` carries no roster at all -- its keys are team metadata. It is the right
view for `tradeBlock` and `waiverRank` and the wrong one for players.
"""
from __future__ import annotations

import pandas as pd

from .board import UNPRICED, norm_name, with_stand_ins

# ESPN's lineupSlotId on a roster entry: where the team currently has him, not
# what he is eligible for. `board._ESPN_SLOT_NAMES` is the same table for the
# settings payload; this one is the roster's own use of it, and BENCH/IR are the
# two that decide whether a player is currently started.
BENCH_SLOT = 20
IR_SLOT = 21

# Said on every frame this module returns, until the first post-draft pull.
UNVERIFIED_SHAPE = "unverified-shape: no populated ESPN roster has been read yet"

# How a row got its position and projection, per player rather than per frame.
# A fallback applied silently is the #39 defect in a new place: the number is
# not wrong, but the reader cannot tell it apart from a measured one.
MATCHED_BY_ID = "espn id"
MATCHED_BY_NAME = "normalised name"
UNMATCHED = "not on the board"


def _player_of(entry: dict) -> dict:
    """The player record inside a roster entry, however deeply ESPN nests it."""
    return ((entry.get("playerPoolEntry") or {}).get("player") or {}) or {}


def entry_facts(entry: dict, positions: dict[str, str]) -> dict:
    """The fields the board's row shape needs, out of one mRoster entry.

    `positions` maps ESPN's `defaultPositionId` to a position name; pass
    `board._ESPN_POSITION_NAMES`. A player whose position id is not in it has no
    position at all here rather than a guessed one, which is what keeps him out
    of a lineup slot he may not be eligible for.
    """
    player = _player_of(entry)
    pid = player.get("id", entry.get("playerId"))
    return {
        "espn_id": None if pid is None else str(pid),
        "name": str(player.get("fullName") or ""),
        "position": positions.get(str(player.get("defaultPositionId")) or ""),
        "lineup_slot": entry.get("lineupSlotId"),
        "espn_injury": player.get("injuryStatus"),
        "injured": bool(player.get("injured", False)),
    }


def roster_rows(entries: list[dict], board: pd.DataFrame, positions: dict[str, str],
                ) -> pd.DataFrame:
    """One team's mRoster entries as board rows, unpriced players included.

    Matched by ESPN id first and normalised name second, and `matched_by` says
    which. The id is the join that cannot go wrong on a name; the name is the
    fallback for a board built without `espn_id`. Both are recorded because a
    name join that silently produced nothing is the bug that cost lena a third of
    a signal on #45, and the only reason her fixture caught it was that it
    asserted an exact number.

    A player the board does not carry becomes a replacement-level stand-in
    through the same `board.with_stand_ins` the draft record uses, so a roster
    read from ESPN and a roster read from the draft record price an unknown
    player identically. He keeps his ESPN position, which is the field that
    makes him placeable at all -- `record_pick` files no position for a player
    off the board, and this is where that gap closes.
    """
    facts = [entry_facts(e, positions) for e in entries]
    if not facts or "_key" not in board.columns:
        empty = board.iloc[0:0].copy()
        empty[UNPRICED] = pd.Series(dtype=bool)
        return empty

    by_id = ({} if "espn_id" not in board.columns
             else {str(v): k for k, v in board["espn_id"].dropna().items()})
    by_key = {k: i for i, k in board["_key"].items()}

    taken: dict[int, str] = {}
    unmatched: list[tuple[str, str]] = []
    for f in facts:
        idx = by_id.get(f["espn_id"] or "")
        how = MATCHED_BY_ID
        if idx is None:
            idx = by_key.get(norm_name(f["name"]))
            how = MATCHED_BY_NAME
        if idx is None:
            if f["name"] and f["position"]:
                unmatched.append((f["name"], f["position"]))
            continue
        taken[idx] = how

    rows = board.loc[list(taken)].copy()
    rows[UNPRICED] = False
    # dtype spelled out: with no matches at all this column is built from an
    # empty list, which pandas makes float64, and the stand-in assignment below
    # then raises rather than storing a string. A roster where the board matches
    # nobody is not a corner case here -- it is a board built before the ADP
    # join, or a team of players the model does not carry.
    rows["matched_by"] = pd.Series([taken[i] for i in rows.index],
                                   index=rows.index, dtype="object")
    out = with_stand_ins(rows, board, unmatched)
    out.loc[out[UNPRICED], "matched_by"] = UNMATCHED

    # ESPN's own view of the player, which the board either lacks or holds from
    # a different pull: where the team has him now, and today's injury status.
    live = {norm_name(f["name"]): f for f in facts if f["name"]}
    keys = out["_key"].map(norm_name)
    out["lineup_slot"] = [live.get(k, {}).get("lineup_slot") for k in keys]
    out["espn_injury"] = [live.get(k, {}).get("espn_injury") for k in keys]
    out["shape"] = UNVERIFIED_SHAPE
    return out.reset_index(drop=True)


def started(rows: pd.DataFrame) -> pd.DataFrame:
    """The players currently in the starting lineup, by ESPN's own slotting.

    Bench and IR are the two slots that are not a start. Anything else is a
    lineup slot, including the flex ones, so this does not need to know the
    league's slot table to answer the question ESPN has already answered.
    """
    if "lineup_slot" not in rows.columns or rows.empty:
        return rows.iloc[0:0]
    slot = pd.to_numeric(rows["lineup_slot"], errors="coerce")
    return rows[slot.notna() & ~slot.isin([BENCH_SLOT, IR_SLOT])]
