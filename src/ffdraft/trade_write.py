"""A trade proposal on ESPN: the transaction ESPN's web client sends, checked against the rosters.

Read from ESPN's web client, fantasy app bundle `cdn1.espn.net/kona/5a90d30cd38d-1.490/
_next/static/commons/main-82d208d52efd2b467c49.js` and the trade page
`.../page/football/team/trade.js`, retrieved 2026-09-15. The trade page's
`sendTrade` calls `proposeTrade` with `fromTeamId` the proposer, `toTeamId` the
partner and `expirationDate: moment().add(days, "days")`, `days` one of the
modal's options 1 through 7. `proposeTrade` builds one `TRADE_PROPOSAL`
transaction whose `teamId` is the proposer, one item per player
`{playerId, type: "TRADE", fromTeamId, toTeamId}`, and `saveTransaction` POSTs
the model's `get()` to the league document's `/transactions/`, the endpoint and
headers `lineup_write` sends with. For a TRADE_PROPOSAL `get()` is

    {isLeagueManager, teamId, type, memberId, scoringPeriodId,
     executionType: "EXECUTE", items, expirationDate, comment}

`scoringPeriodId` is the league's `status.latestScoringPeriod`. A moment
serialises as its UTC ISO string with milliseconds. ESPN's record of a pending
proposal in this league's `mTransactions2` (2026-09-15) holds the proposer in
`teamId`, the same four item fields, and `expirationDate` as epoch milliseconds
two days after `proposedDate`.

A roster the trade would push past capacity needs DROP items; the client adds
them through a roster fix this module does not build, so that is a refusal.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import lineup_write
from .board import norm_name
from .claim_write import resolve

EXPIRY_DAYS = range(1, 8)
CONTRACT_BASIS = ("ESPN web client kona 5a90d30cd38d-1.490 (main bundle proposeTrade, "
                  "transaction model get(), trade page sendTrade), read 2026-09-15; item and "
                  "expiration fields match this league's pending TRADE_PROPOSAL records")


def resolve_team(teams: list[dict], partner: str, my_id: int) -> tuple[dict | None, str | None]:
    """One team for `partner`: its id, else a unique substring of its name or an
    owner's name. Your own team, ambiguity and absence are refusals."""
    key = partner.strip().lower()
    hits = [t for t in teams if key == str(t["team_id"])]
    if not hits and key:
        hits = [t for t in teams if key in t["team"].lower()
                or any(key in o.lower() for o in t["owners"])]
    if not hits:
        return None, f"no team named {partner!r} in this league"
    if len(hits) > 1:
        names = ", ".join(sorted(t["team"] for t in hits))
        return None, f"{partner!r} matches {len(hits)} teams ({names}); name one"
    if hits[0]["team_id"] == my_id:
        return None, "that is your own team"
    return hits[0], None


def resolve_players(rows: list[dict], names: list[str], owner: str,
                    exact: bool = False) -> tuple[list[dict], list[str]]:
    """Each name matched on one team's roster rows; every miss is a refusal
    naming whose roster it was looked for on. `exact` refuses a substring
    match, so a send never moves a player the caller did not name in full."""
    found: list[dict] = []
    refusals: list[str] = []
    for name in names:
        row, why = resolve(rows, name)
        if row is None:
            refusals.append(str(why).replace("in ESPN's pool", f"on {owner}"))
        elif exact and norm_name(row["player"]) != norm_name(name):
            refusals.append(f"{name!r} is not a full name on {owner}; a send needs "
                            f"{row['player']!r}")
        elif row["espn_id"] is None:
            refusals.append(f"{row['player']}: ESPN gave no player id")
        elif any(r["espn_id"] == row["espn_id"] for r in found):
            refusals.append(f"{row['player']} is named twice")
        else:
            found.append(row)
    return found, refusals


def check(give: list[dict], get: list[dict], roster_size: int, capacity: int,
          days: int) -> list[str]:
    """Every reason the proposal would not be sent as built."""
    refusals: list[str] = []
    if not give and not get:
        refusals.append("the trade names no players")
    if days not in EXPIRY_DAYS:
        refusals.append(f"days is {days}; ESPN offers 1 through 7")
    after = roster_size - len(give) + len(get)
    if after > capacity:
        refusals.append(f"your roster would hold {after} of {capacity}; the trade needs a drop, "
                        f"which this tool does not send")
    return refusals


def redact(value, swid: str):
    """`value` with every string carrying the SWID replaced by "<SWID>"."""
    key = swid.strip("{}").upper()
    if isinstance(value, dict):
        return {k: redact(v, swid) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, swid) for v in value]
    if isinstance(value, str) and key and key in value.upper():
        return "<SWID>"
    return value


def expiration(now: datetime, days: int) -> str:
    """`moment().add(days, "days")` as JSON carries it: UTC ISO with milliseconds."""
    at = now.astimezone(timezone.utc) + timedelta(days=days)
    return at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}Z"


def trade_transaction(team_id: int, partner_id: int, swid: str, period: int,
                      give: list[dict], get: list[dict], expires: str, comment: str) -> dict:
    """The body ESPN's client sends for a trade proposal, field for field."""
    items = ([{"playerId": int(r["espn_id"]), "type": "TRADE", "fromTeamId": int(team_id),
               "toTeamId": int(partner_id)} for r in give]
             + [{"playerId": int(r["espn_id"]), "type": "TRADE", "fromTeamId": int(partner_id),
                 "toTeamId": int(team_id)} for r in get])
    return {
        "isLeagueManager": False,
        "teamId": int(team_id),
        "type": "TRADE_PROPOSAL",
        "memberId": swid if swid.startswith("{") else f"{{{swid}}}",
        "scoringPeriodId": int(period),
        "executionType": "EXECUTE",
        "items": items,
        "expirationDate": expires,
        "comment": comment,
    }


# The outcome vocabulary and the reconcile rule are `lineup_write`'s, shared with
# every other write; only the retry sentence is this module's, because where to
# look is particular to a trade.
CONFIRMED = lineup_write.CONFIRMED
REJECTED = lineup_write.REJECTED
UNKNOWN_AFTER_SEND = lineup_write.UNKNOWN_AFTER_SEND
reconcile = lineup_write.reconcile_new_id
NO_RETRY = ("do not resend: the offer may exist on ESPN; check the pending offers on ESPN's "
            "trade page before proposing again")


def matching_proposals(payloads: list[dict], body: dict) -> list[dict]:
    """Every PENDING TRADE_PROPOSAL across mTransactions2 payloads whose proposer
    and player moves are exactly the body's, each ESPN id once."""
    want = sorted((i["playerId"], i["fromTeamId"], i["toTeamId"]) for i in body["items"])
    seen: dict[str, dict] = {}
    for payload in payloads:
        for t in payload.get("transactions") or []:
            if (t.get("type") == "TRADE_PROPOSAL" and t.get("status") == "PENDING"
                    and t.get("teamId") == body["teamId"]
                    and sorted((i.get("playerId"), i.get("fromTeamId"), i.get("toTeamId"))
                               for i in t.get("items") or []) == want):
                seen.setdefault(str(t.get("id")), t)
    return list(seen.values())


