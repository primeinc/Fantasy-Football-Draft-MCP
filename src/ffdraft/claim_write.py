"""A waiver claim's ESPN transaction, built and checked against the league, never sent.

The envelope is the one `lineup_write` read from ESPN's web client: a POST to
the league document's `/transactions/` on the writes host carrying
`{isLeagueManager, teamId, type, memberId, scoringPeriodId, executionType,
items}`. The claim's own fields are the shape ESPN recorded for claims this
league already processed, in `mTransactions2` on 2026-09-13: `type` WAIVER
(FREEAGENT for an outright add), an `ADD` item with `fromTeamId` 0,
`toTeamId` the team, `fromLineupSlotId` -1, `toLineupSlotId` 20, and a `DROP`
item the reverse. A processed record is not the request that created it, so
the request body is unverified and this module has no send path.
"""
from __future__ import annotations

from .board import norm_name
from .pool import ACQUIRABLE
from .rosters import BENCH_SLOT, IR_SLOT

CONTRACT_BASIS = (
    "UNVERIFIED request body: envelope from lineup_write (ESPN web client, 2026-09-05), "
    "items from executed WAIVER and FREEAGENT records in this league's mTransactions2 "
    "(2026-09-13); no claim request has been observed, so nothing is sent")


def resolve(rows: list[dict], name: str) -> tuple[dict | None, str | None]:
    """One pool row for `name`: an exact normalised match, else a unique
    substring match. Ambiguity and absence are refusals, never a guess."""
    key = norm_name(name)
    exact = [r for r in rows if norm_name(r["player"] or "") == key]
    if len(exact) == 1:
        return exact[0], None
    hits = exact or [r for r in rows if key and key in norm_name(r["player"] or "")]
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, f"no player named {name!r} in ESPN's pool"
    names = ", ".join(sorted(str(r["player"]) for r in hits[:6]))
    return None, f"{name!r} matches {len(hits)} players ({names}); name one"


def roster_capacity(settings: dict) -> int:
    """Roster spots from ESPN's slot counts, injured reserve excluded."""
    counts = (settings.get("rosterSettings") or {}).get("lineupSlotCounts") or {}
    return sum(int(v or 0) for k, v in counts.items() if str(k) != str(IR_SLOT))


def check(add: dict | None, drop: dict | None, team_id: int, slots: dict,
          roster_size: int, capacity: int) -> list[str]:
    """Every reason ESPN's rules, as this league states them, would refuse the claim."""
    refusals: list[str] = []
    if add is not None and (add["status"] not in ACQUIRABLE or add["on_team_id"] != 0):
        refusals.append(f"{add['player']} is {add['status']} on team {add['on_team_id']}, "
                        f"not claimable")
    if drop is None and roster_size >= capacity:
        refusals.append(f"your roster is full ({roster_size} of {capacity}); the claim must "
                        f"name a drop")
    if drop is not None:
        slot = slots.get(drop["espn_id"])
        if drop["on_team_id"] != team_id:
            refusals.append(f"{drop['player']} is not on your roster")
        elif drop.get("droppable") is False:
            refusals.append(f"{drop['player']} is on ESPN's undroppable list")
        elif slot != BENCH_SLOT:
            refusals.append(f"{drop['player']} is in ESPN lineup slot {slot}, not the bench")
    return refusals


def claim_transaction(team_id: int, swid: str, week: int, add: dict,
                      drop: dict | None) -> dict:
    """The body a claim would carry; WAIVER for a player on waivers, FREEAGENT
    for one who can be added outright."""
    items = [{"playerId": int(add["espn_id"]), "type": "ADD", "fromTeamId": 0,
              "toTeamId": int(team_id), "fromLineupSlotId": -1, "toLineupSlotId": BENCH_SLOT}]
    if drop is not None:
        items.append({"playerId": int(drop["espn_id"]), "type": "DROP",
                      "fromTeamId": int(team_id), "toTeamId": 0,
                      "fromLineupSlotId": BENCH_SLOT, "toLineupSlotId": -1})
    return {
        "isLeagueManager": False,
        "teamId": int(team_id),
        "type": "WAIVER" if add["status"] == "WAIVERS" else "FREEAGENT",
        "memberId": swid if swid.startswith("{") else f"{{{swid}}}",
        "scoringPeriodId": int(week),
        "executionType": "EXECUTE",
        "items": items,
    }
