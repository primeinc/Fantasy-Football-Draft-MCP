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

import json
import os

import pandas as pd
import requests

from .board import (
    UNPRICED,
    espn_cookies,
    espn_league_url,
    is_position,
    norm_name,
    with_stand_ins,
)
from .config import CURRENT_SEASON

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
    position = positions.get(str(player.get("defaultPositionId")) or "")
    return {
        "espn_id": None if pid is None else str(pid),
        "name": str(player.get("fullName") or ""),
        # Through the one rule, so an unmapped id yields no position rather than
        # something that fails a slot match later and looks like a data gap.
        "position": position if is_position(position) else None,
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


def fetch_roster_teams(league_id: str, season: int = CURRENT_SEASON,
                       week: int | None = None, swid: str | None = None,
                       espn_s2: str | None = None) -> list[dict]:
    """The mRoster view's teams, each carrying its roster entries.

    `week` becomes `scoringPeriodId`, which is what makes this a question about
    a particular week rather than about now -- #47 evaluating a trade in week 9
    needs the rosters as they stood, not as they stand. Omitted, ESPN answers
    for the current period.

    The only function here that touches the network, so a test replaces this one
    and everything else is pure. Cookies and URL come from `board`, which had
    five copies of that construction before this and does not need a sixth.
    """
    # BOTH views, and mTeam is not optional. mRoster's team objects carry only
    # `id` and `roster` -- no `owners` -- so `my_team_id` against an mRoster-only
    # response always returns None and every caller refuses with "no team in this
    # league is owned by ESPN_SWID". Found by running this against the live
    # league rather than by reading it: the fixture supplies `owners` because I
    # wrote the fixture from the shape I expected.
    params: list[tuple[str, str]] = [("view", "mRoster"), ("view", "mTeam")]
    if week:
        params.append(("scoringPeriodId", str(int(week))))
    resp = requests.get(espn_league_url(league_id, season), params=params,
                        cookies=espn_cookies(swid, espn_s2), timeout=30,
                        headers={"User-Agent": "ffdraft-mcp/1.0"})
    resp.raise_for_status()
    return resp.json().get("teams") or []


def my_team_id(teams: list[dict], swid: str | None = None) -> int | None:
    """The team owned by ESPN_SWID, or None when it owns none in this league.

    The same owner match `board.sync_espn` makes, braces stripped from both
    sides because ESPN wraps them in the cookie and not always in the payload.
    None rather than a raise: a caller reading every roster does not need to own
    one, and only the caller knows whether not owning a team is a failure.
    """
    target = (swid or os.environ.get("ESPN_SWID") or "").strip("{}").upper()
    if not target:
        return None
    for team in teams:
        owners = [str(o).strip("{}").upper() for o in (team.get("owners") or [])]
        if target in owners and team.get("id") is not None:
            return int(team["id"])
    return None


def fetch_weekly_projections(league_id: str, season: int = CURRENT_SEASON,
                             week: int | None = None, swid: str | None = None,
                             espn_s2: str | None = None) -> dict[str, float]:
    """ESPN's own projection for one week, keyed by player id as a string.

    `kona_player_info` carries `(statSourceId 1, statSplitTypeId 1)` rows whose
    `scoringPeriodId` is the week, but only for the period the pull asks for --
    one capture holds one week, so pricing week 9 means requesting week 9. The
    live pull carried a weekly row for 397 of 1036 players, which is why every
    caller of this needs a stated fallback rather than treating a miss as zero.
    """
    # The filter is load-bearing, not tuning. Without it ESPN answers with a
    # default slice -- 36 players against the live league -- so nearly every
    # rostered player misses his weekly row and falls back, silently and for a
    # reason that has nothing to do with him. `waivers.POOL_FILTER` is the same
    # filter `espn_dump` captures with, so the pull here and the pull the field
    # shapes were documented from are the same pull.
    from .waivers import POOL_FILTER

    params: dict[str, str] = {"view": "kona_player_info"}
    if week:
        params["scoringPeriodId"] = str(int(week))
    resp = requests.get(espn_league_url(league_id, season), params=params,
                        cookies=espn_cookies(swid, espn_s2), timeout=30,
                        headers={"User-Agent": "ffdraft-mcp/1.0",
                                 "X-Fantasy-Source": "kona",
                                 "X-Fantasy-Filter": json.dumps(POOL_FILTER)})
    resp.raise_for_status()
    return weekly_projections(resp.json().get("players") or [], week)


def weekly_projections(players: list[dict], week: int | None) -> dict[str, float]:
    """The pure half: player id -> projected points for `week`.

    Only `statSourceId` 1 (projected, not actual) and `statSplitTypeId` 1 (a
    single scoring period, not a season total) count. Taking a season row here
    would hand the lineup a number seventeen times too large without failing.
    """
    out: dict[str, float] = {}
    for entry in players:
        player = (entry.get("player") or {})
        pid = player.get("id", entry.get("id"))
        if pid is None:
            continue
        for stat in player.get("stats") or []:
            if (stat.get("statSourceId") == 1 and stat.get("statSplitTypeId") == 1
                    and (week is None or stat.get("scoringPeriodId") == week)):
                total = stat.get("appliedTotal")
                if total is not None:
                    out[str(pid)] = float(total)
                break
    return out


def rosters_by_team(teams: list[dict], board: pd.DataFrame,
                    positions: dict[str, str]) -> dict[int, pd.DataFrame]:
    """Every team's roster in the board's row shape, keyed by ESPN team id.

    Team id rather than draft slot, at freddy's request and for his reason: a
    slot is a draft-time concept that means nothing in week 9, and a team is the
    unit that holds players and makes trades. Owner would be worse still, since
    a team can have co-managers.

    A team with no entries gets an empty frame rather than being omitted, so a
    caller iterating teams sees all of them and an empty roster is visibly empty
    rather than missing. Today that is every team: the draft has not finished.
    """
    out: dict[int, pd.DataFrame] = {}
    for team in teams:
        team_id = team.get("id")
        if team_id is None:
            continue
        entries = (team.get("roster") or {}).get("entries") or []
        out[int(team_id)] = roster_rows(entries, board, positions)
    return out


def read_rosters(league_id: str, board: pd.DataFrame, positions: dict[str, str],
                 season: int = CURRENT_SEASON, week: int | None = None,
                 swid: str | None = None, espn_s2: str | None = None,
                 ) -> dict[int, pd.DataFrame]:
    """Fetch and parse in one call: team id -> roster rows. See the two halves."""
    return rosters_by_team(
        fetch_roster_teams(league_id, season, week, swid, espn_s2), board, positions)


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
