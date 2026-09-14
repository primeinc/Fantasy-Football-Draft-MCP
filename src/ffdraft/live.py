"""What is happening on the field right now, in fantasy terms.

Three ESPN surfaces, each read whole and joined on ESPN's own ids:

- the NFL scoreboard (`site.web.api`), for which games are in progress and
  the score and clock; a competitor's `team.id` is ESPN's proTeamId, mapped
  through `board._ESPN_TEAM_ABBR` so the abbreviation agrees with the board
  (the scoreboard says LAR and WSH where the board says LA and WAS);
- a game's summary, for the box-score line of every athlete who has one;
- the league document with mMatchupScore + mRoster for the scoring period,
  for every fantasy team's players, their applied points so far
  (`statSourceId 0`) and ESPN's projection (`statSourceId 1`), and each
  matchup's live and projected totals.

Nothing here is a model. It is the game as ESPN scores it, printed with
the fantasy team each player belongs to, so "how is everyone doing" is a
read rather than a guess.
"""
from __future__ import annotations

import requests

from .board import _ESPN_TEAM_ABBR, espn_league_get

SCOREBOARD_URL = "https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
SUMMARY_URL = "https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/summary"
HEADERS = {"User-Agent": "ffdraft-mcp/1.0", "Accept": "application/json"}
BENCH_SLOT, IR_SLOT = 20, 21
LIVE_BASIS = ("ESPN scoreboard for score and clock; ESPN game summary for box lines; "
              "the league's mMatchupScore+mRoster for applied points, projections and "
              "matchup totals")


def fetch_scoreboard(timeout: float = 30.0) -> dict:
    resp = requests.get(SCOREBOARD_URL, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def fetch_summary(event_id: str, timeout: float = 30.0) -> dict:
    resp = requests.get(SUMMARY_URL, params={"event": str(event_id)}, headers=HEADERS,
                        timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def fetch_league_status(league_id: str, season: int, timeout: float = 30.0) -> dict:
    """The league's own periods: top-level `scoringPeriodId` and `status`
    (`currentMatchupPeriod`, `latestScoringPeriod`), the fields espn-api reads
    (refs/cwendt94/espn-api league.py). ESPN's NFL scoreboard week is not these."""
    resp = espn_league_get(league_id, season, {"view": "mStatus"}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def fetch_league_live(league_id: str, season: int, week: int, timeout: float = 30.0) -> dict:
    resp = espn_league_get(league_id, season,
                           [("view", "mMatchupScore"), ("view", "mRoster"),
                            ("view", "mTeam"), ("scoringPeriodId", str(int(week)))],
                           timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _abbr(team: dict) -> str:
    """The board's abbreviation for a scoreboard competitor, by ESPN team id."""
    tid = team.get("id")
    fallback = str(team.get("abbreviation") or tid)
    if tid is None:
        return fallback
    try:
        return _ESPN_TEAM_ABBR.get(int(tid), fallback)
    except (TypeError, ValueError):
        return fallback


def games(scoreboard: dict) -> list[dict]:
    """Every game on the scoreboard: state (`pre`/`in`/`post`), clock, score."""
    out = []
    for ev in scoreboard.get("events") or []:
        comp = (ev.get("competitions") or [{}])[0]
        status = (ev.get("status") or {}).get("type") or {}
        sides = {}
        for c in comp.get("competitors") or []:
            sides[c.get("homeAway")] = {"team": _abbr(c.get("team") or {}),
                                        "score": c.get("score")}
        out.append({"event_id": str(ev.get("id")), "name": ev.get("shortName"),
                    "date": ev.get("date"),
                    "state": status.get("state"), "detail": status.get("detail"),
                    "home": sides.get("home"), "away": sides.get("away"),
                    "teams": {s["team"] for s in sides.values()}})
    return out


def box_lines(summary: dict) -> dict[str, dict[str, dict[str, str]]]:
    """Athlete display name -> stat group -> {label: value}, from a game summary."""
    out: dict[str, dict[str, dict[str, str]]] = {}
    for team in (summary.get("boxscore") or {}).get("players") or []:
        for grp in team.get("statistics") or []:
            labels = grp.get("labels") or []
            for a in grp.get("athletes") or []:
                name = str((a.get("athlete") or {}).get("displayName") or "")
                out.setdefault(name, {})[str(grp.get("name"))] = dict(zip(labels, a.get("stats") or []))
    return out


def _applied(player: dict, week: int, source: int) -> float | None:
    for s in player.get("stats") or []:
        if s.get("scoringPeriodId") == week and s.get("statSourceId") == source:
            v = s.get("appliedTotal")
            return None if v is None else round(float(v), 1)
    return None


def league_players(payload: dict, week: int) -> list[dict]:
    """Every rostered player in the league with his fantasy team, slot, pro team,
    applied points so far and ESPN's projection for the period."""
    names = {t["id"]: t.get("name") for t in payload.get("teams") or []}
    out = []
    for t in payload.get("teams") or []:
        for e in (t.get("roster") or {}).get("entries") or []:
            p = (e.get("playerPoolEntry") or {}).get("player") or {}
            slot = e.get("lineupSlotId")
            out.append({
                "fantasy_team_id": t["id"], "fantasy_team": names.get(t["id"]),
                "player": p.get("fullName"), "espn_id": str(p.get("id")),
                "pro_team": _ESPN_TEAM_ABBR.get(p.get("proTeamId")),
                "started": slot not in (BENCH_SLOT, IR_SLOT, None),
                "live": _applied(p, week, 0), "proj": _applied(p, week, 1),
            })
    return out


def matchups(payload: dict, week: int) -> list[dict]:
    names = {t["id"]: t.get("name") for t in payload.get("teams") or []}
    out = []
    for m in payload.get("schedule") or []:
        if m.get("matchupPeriodId") != week:
            continue
        row = {"matchup_id": m.get("id")}
        for side in ("home", "away"):
            x = m.get(side) or {}
            row[side] = {"team_id": x.get("teamId"), "team": names.get(x.get("teamId")),
                         "live": round(float(x.get("totalPointsLive") or 0.0), 1),
                         "proj": round(float(x.get("totalProjectedPointsLive") or 0.0), 1)}
        out.append(row)
    return out
