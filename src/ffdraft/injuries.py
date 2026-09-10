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

from urllib.parse import urlsplit

import pandas as pd
import requests

from .board import _ESPN_TEAM_ABBR
from .names import normalize as norm_name

INJURIES_URL = "https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/injuries"
FEED_BASIS = "ESPN injuries feed, per-entry date and the feed's own timestamp"
COLUMNS = ("team", "espn_id", "name", "_key", "status", "fantasy_status", "injury", "date",
           "return_date", "comment")
JOINED_BY_ID = "espn athlete id"
JOINED_BY_NAME = ("team and normalised name (fallback: the feed entry carried no id "
                  "the roster knows)")
NOT_JOINED = "no feed entry"


def athlete_id(athlete: dict) -> str | None:
    """ESPN's athlete id, the same id the fantasy roster carries as the player id.

    The feed's athlete object has no `id` field; the id is the path segment
    after `id` in its player-card link (`/nfl/player/_/id/4569173/...`). Read
    by splitting the path, not by pattern: the segment either follows `id` or
    there is no id.
    """
    for link in athlete.get("links") or []:
        parts = [p for p in urlsplit(str(link.get("href") or "")).path.split("/") if p]
        if "id" in parts:
            i = parts.index("id")
            if i + 1 < len(parts) and parts[i + 1].isdigit():
                return parts[i + 1]
    return None


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
                "team": abbr, "espn_id": athlete_id(athlete),
                "name": name, "_key": norm_name(name),
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

    Joined on ESPN's athlete id first -- the id the fantasy roster entry
    carries -- and `joined_by` says so. The (team, normalised name) join is
    the fallback for a feed entry with no id or a roster row with none, and
    it is labelled as the fallback rather than passing for the id join: a
    suffix, a rename or two men with one name are exactly what it can get
    wrong. A player with no entry is `in_feed: False` rather than omitted:
    "not on the list" is a fact about the list, not a medical status, and it
    has to read as that rather than as a lookup that found nothing.
    """
    by_id: dict[str, pd.Series] = {}
    by_team_key: dict[tuple[str, str], pd.Series] = {}
    by_key: dict[str, pd.Series] = {}
    if not feed.empty:
        newest = feed.sort_values("date", ascending=False)
        by_id = {str(r["espn_id"]): r
                 for _, r in newest.dropna(subset=["espn_id"]).drop_duplicates("espn_id").iterrows()}
        by_team_key = {(str(r["team"]), str(r["_key"])): r
                       for _, r in newest.drop_duplicates(["team", "_key"]).iterrows()}
        by_key = {str(r["_key"]): r for _, r in newest.drop_duplicates(["_key"]).iterrows()}
    out = []
    for _, row in roster.iterrows():
        key = norm_name(str(row["name"]))
        team = row.get("team")
        team = None if team is None or pd.isna(team) else str(team)
        pid = _text(row.get("espn_id"))
        hit, joined_by = None, NOT_JOINED
        if pid is not None and pid in by_id:
            hit, joined_by = by_id[pid], JOINED_BY_ID
        if hit is None:
            hit = by_team_key.get((team, key)) if team else None
            if hit is None:
                hit = by_key.get(key)
            if hit is not None:
                joined_by = JOINED_BY_NAME
        item = {"player": str(row["name"]), "team": team,
                "roster_status": _text(row.get("espn_injury")), "in_feed": hit is not None,
                "joined_by": joined_by}
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
    """Whether the two sources say the same thing. None when either side has
    nothing to say: a feed note without a fantasy status (an ACTIVE note has
    none), or a roster entry with no status at all (ESPN files none for a
    defense). A missing status is not ACTIVE, and is not compared as one."""
    if feed_fantasy is None or (isinstance(feed_fantasy, float) and pd.isna(feed_fantasy)):
        return None
    if roster_status is None or (isinstance(roster_status, float) and pd.isna(roster_status)):
        return None
    return str(roster_status).upper() == str(feed_fantasy).upper()
