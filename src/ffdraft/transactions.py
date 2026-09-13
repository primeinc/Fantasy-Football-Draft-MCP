"""League transactions as ESPN records them: adds, drops, trades, waiver runs.

`mTransactions2` answers for one scoring period, the response's own
`scoringPeriodId`. Read live on 2026-09-13, league 1734659820, week 1: 231
transactions -- DRAFT 224, ROSTER 4, WAIVER 2, FREEAGENT 1 -- every one
EXECUTED. Items carry `type` (DRAFT, ADD, DROP, LINEUP), `playerId`,
`fromTeamId`/`toTeamId` (0 is free agency) and lineup slot ids. A WAIVER is
`executionType` PROCESS with `processDate` and a `relatedTransactionId`; a
move a manager made directly has only `proposedDate`. `memberId` is a SWID or
the processor's name and is never printed. A failed claim was not in that
pull, so how ESPN records one is unobserved.
"""
from __future__ import annotations

import requests

from .board import espn_cookies, espn_league_url
from .config import CURRENT_SEASON
from .pool import eastern

TXN_SHAPE = ("mTransactions2 shape verified against the live league, 2026-09-13; "
             "a failed waiver claim has not been observed")


def fetch_transactions(league_id: str, season: int = CURRENT_SEASON, week: int | None = None,
                       swid: str | None = None, espn_s2: str | None = None) -> dict:
    """The mTransactions2 response for `week`, or the current period when omitted."""
    params: list[tuple[str, str]] = [("view", "mTransactions2")]
    if week:
        params.append(("scoringPeriodId", str(int(week))))
    resp = requests.get(espn_league_url(league_id, season), params=params,
                        cookies=espn_cookies(swid, espn_s2), timeout=30,
                        headers={"User-Agent": "ffdraft-mcp/1.0"})
    resp.raise_for_status()
    return resp.json()


def _team(team_names: dict[int, str], team_id) -> str | None:
    if team_id in (None, 0):
        return None
    return team_names.get(int(team_id), f"team {team_id}")


def _player(player_names: dict[int, str], player_id) -> str | None:
    if player_id is None:
        return None
    return player_names.get(int(player_id), f"player {player_id}")


def type_counts(payload: dict) -> dict[str, int]:
    """Every transaction in the period by ESPN type, hidden ones included."""
    counts: dict[str, int] = {}
    for t in payload.get("transactions") or []:
        counts[str(t.get("type"))] = counts.get(str(t.get("type")), 0) + 1
    return dict(sorted(counts.items()))


def transaction_rows(payload: dict, team_names: dict[int, str], player_names: dict[int, str],
                     include_lineup: bool = False, include_draft: bool = False) -> list[dict]:
    """Newest first. Each move is `[type, player, from_team, to_team]`, with
    None for free agency. Draft picks and transactions made only of lineup
    moves are left out unless asked for; a transaction carrying any add, drop
    or trade item is always kept, whatever its type."""
    keyed: list[tuple[int, dict]] = []
    for t in payload.get("transactions") or []:
        items = t.get("items") or []
        if not include_draft and t.get("type") == "DRAFT":
            continue
        if not include_lineup and items and all(i.get("type") == "LINEUP" for i in items):
            continue
        when = int(t.get("processDate") or t.get("proposedDate") or 0)
        keyed.append((when, {
            "when": eastern(when),
            "type": t.get("type"),
            "status": t.get("status"),
            "team_id": t.get("teamId"),
            "team": _team(team_names, t.get("teamId")),
            "bid": t.get("bidAmount") or None,
            "moves": [[i.get("type"), _player(player_names, i.get("playerId")),
                       _team(team_names, i.get("fromTeamId")),
                       _team(team_names, i.get("toTeamId"))] for i in items],
        }))
    keyed.sort(key=lambda pair: pair[0], reverse=True)
    return [row for _, row in keyed]
