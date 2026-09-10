"""ESPN's dated injury feed, joined to a roster.

The decision surface for an injury is a status WITH a date. The fantasy
roster entry carries `injuryStatus` and nothing about when it was set; a web
search's synthesized answer carries neither and conflates weeks -- on
2026-09-09 one reported a receiver "questionable" from a January report.
This feed is ESPN's own per-team list: every entry has `status`,
`details.fantasyStatus`, an ISO `date`, and the report line behind it, and the
payload carries its own `timestamp`. Official inactives arrive here too
(`fantasyStatus` INACTIVE, ~90 minutes before kickoff).

`INJURIES_URL` is the `site.web.api` host: the `site.api` host answers the
same path with an Akamai 403 to this client (checked 2026-09-09, both hosts,
two user agents).
"""
from __future__ import annotations

import pandas as pd
import requests

from .board import _ESPN_TEAM_ABBR
from .names import normalize as norm_name

INJURIES_URL = "https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/injuries"
FEED_BASIS = "ESPN injuries feed, per-entry date and the feed's own timestamp"
COLUMNS = ("team", "name", "_key", "status", "fantasy_status", "injury", "date",
           "return_date", "comment")


def fetch_injuries(timeout: float = 30.0) -> dict:
    """The feed as ESPN serves it. The only function here that touches the network."""
    resp = requests.get(INJURIES_URL, timeout=timeout,
                        headers={"User-Agent": "ffdraft-mcp/1.0", "Accept": "application/json"})
    resp.raise_for_status()
    return resp.json()


def parse_injuries(payload: dict) -> tuple[pd.DataFrame, str | None]:
    """One row per (team, player) entry, and the feed's timestamp.

    The team id is ESPN's proTeamId, mapped through the board's own table so
    the row joins a roster on the abbreviation the board uses. An entry whose
    team is not in that table is kept with the raw id as its team rather than
    dropped: the row is still a dated fact about a named player.
    """
    rows = []
    for team in payload.get("injuries") or []:
        tid = team.get("id")
        try:
            abbr = _ESPN_TEAM_ABBR.get(int(tid), str(tid))
        except (TypeError, ValueError):
            abbr = str(tid)
        for entry in team.get("injuries") or []:
            athlete = entry.get("athlete") or {}
            details = entry.get("details") or {}
            name = str(athlete.get("displayName") or "")
            rows.append({
                "team": abbr, "name": name, "_key": norm_name(name),
                "status": entry.get("status"),
                "fantasy_status": (details.get("fantasyStatus") or {}).get("abbreviation"),
                "injury": details.get("type"),
                "date": entry.get("date"),
                "return_date": details.get("returnDate"),
                "comment": entry.get("shortComment") or entry.get("longComment") or "",
            })
    frame = pd.DataFrame(rows, columns=list(COLUMNS))
    return frame, payload.get("timestamp")


def for_roster(feed: pd.DataFrame, roster: pd.DataFrame) -> list[dict]:
    """Each roster row's newest feed entry, or an explicit absence.

    Joined on (team, normalised name) first, on the name alone when the roster
    row has no team. A player with no entry is reported as `in_feed: False`
    rather than omitted: "not on the list" is the ordinary state for a healthy
    player, and it has to be readable as that rather than as a lookup that
    found nothing.
    """
    if feed.empty:
        by_team_key: dict[tuple[str, str], pd.Series] = {}
        by_key: dict[str, pd.Series] = {}
    else:
        newest = feed.sort_values("date", ascending=False)
        by_team_key = {(str(r["team"]), str(r["_key"])): r
                       for _, r in newest.drop_duplicates(["team", "_key"]).iterrows()}
        by_key = {str(r["_key"]): r for _, r in newest.drop_duplicates(["_key"]).iterrows()}
    out = []
    for _, row in roster.iterrows():
        key = norm_name(str(row["name"]))
        team = row.get("team")
        team = None if team is None or pd.isna(team) else str(team)
        hit = by_team_key.get((team, key)) if team else None
        if hit is None:
            hit = by_key.get(key)
        item = {"player": str(row["name"]), "team": team,
                "roster_status": _text(row.get("espn_injury")), "in_feed": hit is not None}
        if hit is not None:
            item.update({
                "feed_status": _text(hit["status"]),
                "feed_fantasy_status": _text(hit["fantasy_status"]),
                "injury": _text(hit["injury"]), "as_of": _text(hit["date"]),
                "return_date": _text(hit["return_date"]), "comment": _text(hit["comment"]),
                # The roster's word against the feed's. They disagree when the
                # fantasy roster has not caught up with the report, and that is
                # the case worth seeing.
                "agrees_with_roster": _agrees(row.get("espn_injury"), hit["fantasy_status"]),
            })
        out.append(item)
    return out


def _text(v) -> str | None:
    return None if v is None or (isinstance(v, float) and pd.isna(v)) else str(v)


def _agrees(roster_status, feed_fantasy) -> bool | None:
    """Whether the two sources say the same thing, None when the feed entry
    carries no fantasy status (an ACTIVE note has none)."""
    if feed_fantasy is None or (isinstance(feed_fantasy, float) and pd.isna(feed_fantasy)):
        return None
    return str(roster_status or "ACTIVE").upper() == str(feed_fantasy).upper()
