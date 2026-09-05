"""Set the week's lineup on ESPN: the moves, the transaction, the send.

Read from ESPN's own web client rather than from a community port. In the
fantasy app's main bundle (`cdn1.espn.net/kona/2d26c1207d60-1.487/_next/static/
commons/main-8f4fae007004824a918c.js`, retrieved 2026-09-05) the writes host
is `https://lm-api-writes.<game>.<domain>.com`, every write goes to
`<host>/apis/v3/<path>`, and a lineup change is one POST to the league
document's path plus `/transactions/`, whose body is the transaction model's
`get()`:

    {isLeagueManager, teamId, type: "ROSTER", memberId: <SWID>,
     scoringPeriodId: <week>, executionType: "EXECUTE",
     items: [{playerId, type: "LINEUP", fromLineupSlotId, toLineupSlotId}, ...]}

with `Content-Type: application/json`, `X-Fantasy-Source: kona` and
`X-Fantasy-Platform: espn-fantasy-web` on the request. `memberId` is the
`profile.swid`, braces included, which is the same string the cookie carries.

Three refusals, each named in the plan rather than raised: a player the board
knows but ESPN did not give an id, a move into a slot the player is not
eligible for, and a move touching a player ESPN reports as lineup-locked. A
plan with any refusal sends nothing.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pandas as pd

from .board import READS_HOST, espn_cookies, espn_league_url

WRITES_HOST = READS_HOST.replace("lm-api-reads", "lm-api-writes")
HEADERS = {"User-Agent": "ffdraft-mcp/1.0", "Accept": "application/json",
           "Content-Type": "application/json", "X-Fantasy-Source": "kona",
           "X-Fantasy-Platform": "espn-fantasy-web"}

# The league's slot names, as `lineup.starting_lineup` fills them, to ESPN's
# lineupSlotId. The reverse of board._ESPN_SLOT_NAMES for the slots a lineup
# fills, plus the two a player leaves a lineup for.
SLOT_IDS = {"QB": 0, "RB": 2, "WR": 4, "TE": 6, "DST": 16, "K": 17,
            "FLEX": 23, "SUPERFLEX": 7, "OP": 7, "BENCH": 20, "IR": 21}
BENCH_SLOT = SLOT_IDS["BENCH"]
IR_SLOT = SLOT_IDS["IR"]
SLOT_COLUMN = "lineup_slot_filled"


def transaction_url(league_id: str, season: int) -> str:
    """The league document's path on the writes host, plus `/transactions/`."""
    return espn_league_url(league_id, season).replace(READS_HOST, WRITES_HOST) + "/transactions/"


def lineup_transaction(team_id: int, swid: str, week: int, items: list[dict]) -> dict:
    """The body ESPN's client sends for a lineup change, field for field."""
    return {
        "isLeagueManager": False,
        "teamId": int(team_id),
        "type": "ROSTER",
        "memberId": swid if swid.startswith("{") else f"{{{swid}}}",
        "scoringPeriodId": int(week),
        "executionType": "EXECUTE",
        "items": items,
    }


def _slot_id_for(slot_name: str) -> int | None:
    return SLOT_IDS.get(str(slot_name))


def plan_moves(starters: pd.DataFrame, roster: pd.DataFrame) -> dict:
    """The LINEUP items that turn `roster`'s current slots into `starters`.

    `starters` is `lineup.starting_lineup`'s first frame, each row carrying the
    slot it fills in `lineup_slot_filled`. `roster` is every row of the team as
    `rosters.roster_rows` returns them: `espn_id`, `lineup_slot` (where ESPN has
    him now), `eligible_slots` (where ESPN allows him), `lineup_locked`.

    A player already in his target slot produces no item. A player in a
    starting slot whom the lineup does not start goes to the bench. Injured
    reserve is never touched: moving a player off IR is a roster move with
    its own rules, and nothing here decides it.
    """
    refusals: list[str] = []
    items: list[dict] = []
    before: dict[str, int | None] = {}
    after: dict[str, int | None] = {}
    by_name = {str(r["name"]): r for _, r in roster.iterrows()}
    target: dict[str, int] = {}
    for _, s in starters.iterrows():
        name = str(s["name"])
        slot_id = _slot_id_for(s.get(SLOT_COLUMN))
        if slot_id is None:
            refusals.append(f"{name}: no ESPN slot id for {s.get(SLOT_COLUMN)!r}")
            continue
        target[name] = slot_id
    for name, row in by_name.items():
        current = row.get("lineup_slot")
        current = None if pd.isna(current) else int(current)
        before[name] = current
        if current == IR_SLOT:
            after[name] = current
            continue
        want = target.get(name, BENCH_SLOT)
        after[name] = want
        if current == want:
            continue
        pid = row.get("espn_id")
        if pid is None or (isinstance(pid, float) and pd.isna(pid)) or str(pid) == "":
            refusals.append(f"{name}: ESPN gave no player id, so he cannot be moved")
            continue
        eligible = row.get("eligible_slots")
        eligible = list(eligible) if isinstance(eligible, (list, tuple)) else None
        if eligible is not None and want not in eligible:
            refusals.append(f"{name}: slot {want} is not among his eligible slots {eligible}")
            continue
        if bool(row.get("lineup_locked", False)):
            refusals.append(f"{name}: ESPN reports his lineup slot locked")
            continue
        items.append({"playerId": int(str(pid)), "type": "LINEUP",
                      "fromLineupSlotId": current, "toLineupSlotId": want})
    unknown_lock = [n for n, r in by_name.items()
                    if "lineup_locked" not in r.index or pd.isna(r.get("lineup_locked"))]
    return {"items": items, "refusals": refusals, "before": before, "after": after,
            "lock_status_unknown_for": unknown_lock}


def send(league_id: str, season: int, payload: dict, swid: str | None = None,
         espn_s2: str | None = None, post: Callable[..., Any] | None = None) -> dict:
    """POST the transaction and return what ESPN answered, status included.

    `post` is `requests.post` unless a test supplies a spy. Nothing here
    interprets the answer beyond parsing it: the caller re-reads the roster,
    because what ESPN holds afterwards is the only fact worth reporting.
    """
    import requests

    poster = post or requests.post
    resp = poster(transaction_url(league_id, season), json=payload,
                  cookies=espn_cookies(swid, espn_s2), headers=HEADERS, timeout=30)
    body: Any
    try:
        body = resp.json()
    except ValueError:
        body = (getattr(resp, "text", "") or "")[:500]
    return {"status": int(getattr(resp, "status_code", 0)), "body": body}
