"""ESPN's player pool as this league holds it: who can be added, and how.

`kona_player_info` with `waivers.POOL_FILTER` returns every player in the pool
with the league's own view of him. Read live on 2026-09-13, week 1, league
1734659820: 1038 entries, 433 FREEAGENT, 380 WAIVERS, 225 ONTEAM. Each entry
carries `status`, `onTeamId` (0 when no team holds him), `waiverProcessDate`
(epoch ms; 2026-09-16T07:00Z on every WAIVERS row that day) and `player`,
whose `stats` rows are keyed by `(seasonId, scoringPeriodId, statSourceId,
statSplitTypeId)`: source 0 is what happened, source 1 is ESPN's projection,
split 1 is a single scoring period.

A player absent from every roster is not thereby addable; `status` and
`onTeamId` together are the fact, and this module reads both.

Status and waiver time belong to the pull with no scoring period. Paired
capture 2026-09-13 21:31 ET, week 1 current (`dump_draft(period=2)`, `just
period-diff`): of 1041 entries the week 2 pull had all 380 WAIVERS players
FREEAGENT and 409 different `waiverProcessDate`s; `onTeamId` and
`injuryStatus` matched in every entry, and in all 225 mRoster entries. So a
caller takes status and waiver time from `fetch_pool` with no week, and a
week's totals from a pull for that week, through `stats`.

A pull for the current period is the pull with no week. Paired capture
2026-09-14 03:04 ET, mStatus scoringPeriodId 1 (`dump_draft(period=1)`, `just
period-diff`): all 1041 entries matched in every field, stat rows keyed by
`(seasonId, scoringPeriodId, statSourceId, statSplitTypeId, externalId)` and
rankings included; only the order of those lists differed.
"""
from __future__ import annotations

import pandas as pd

from .board import _ESPN_TEAM_ABBR, espn_league_get, espn_season_projection
from .config import CURRENT_SEASON
from .waivers import POOL_FILTER

POOL_SHAPE = "kona_player_info entry shape verified in-season against the live league, 2026-09-13"
ACQUIRABLE = ("FREEAGENT", "WAIVERS")
ACTUAL_SOURCE = 0
PROJECTED_SOURCE = 1
SINGLE_PERIOD_SPLIT = 1


def fetch_pool(league_id: str, season: int = CURRENT_SEASON, week: int | None = None,
               swid: str | None = None, espn_s2: str | None = None) -> list[dict]:
    """Every pool entry for the league, stats carried for `week` when given."""
    params = {"view": "kona_player_info"}
    if week:
        params["scoringPeriodId"] = str(int(week))
    resp = espn_league_get(league_id, season, params, swid, espn_s2, player_filter=POOL_FILTER)
    resp.raise_for_status()
    return resp.json().get("players") or []


def next_waiver_clear(league_id: str, season: int = CURRENT_SEASON, swid: str | None = None,
                      espn_s2: str | None = None) -> int | None:
    """The earliest `waiverProcessDate` (epoch ms) among up to 25 WAIVERS entries,
    None when nobody is on waivers. A small pull: the full pool is 4 MB."""
    flt = {"players": {**POOL_FILTER["players"], "filterStatus": {"value": ["WAIVERS"]},
                       "limit": 25}}
    resp = espn_league_get(league_id, season, {"view": "kona_player_info"}, swid, espn_s2,
                           player_filter=flt)
    resp.raise_for_status()
    dates = [int(e["waiverProcessDate"]) for e in resp.json().get("players") or []
             if e.get("status") == "WAIVERS" and e.get("waiverProcessDate")]
    return min(dates) if dates else None


def week_total(player: dict, season: int, week: int, source: int) -> float | None:
    """ESPN's applied fantasy total for one scoring period, or None when ESPN
    carries no row for it. None is "no row", never zero: a player with no game
    yet and a player who scored nothing are different facts."""
    for stat in player.get("stats") or []:
        if (stat.get("seasonId") == season and stat.get("scoringPeriodId") == week
                and stat.get("statSourceId") == source
                and stat.get("statSplitTypeId") == SINGLE_PERIOD_SPLIT):
            total = stat.get("appliedTotal")
            return None if total is None else round(float(total), 2)
    return None


def eastern(ms) -> str | None:
    """Epoch milliseconds as "YYYY-MM-DD HH:MM ET", the form every deadline in
    this codebase prints in."""
    if ms in (None, 0):
        return None
    stamp = pd.Timestamp(int(ms), unit="ms", tz="UTC").tz_convert("America/New_York")
    return stamp.strftime("%Y-%m-%d %H:%M ET")


def pool_rows(players: list[dict], positions: dict[str, str], season: int,
              week: int, stats: list[dict] | None = None) -> list[dict]:
    """One row per pool entry, every player whatever his status.

    `players` supplies status, ownership, waiver clear time and injury; pass
    the current pull. `stats`, when given, is a pull for `week`: the week's
    totals and ESPN's injury status for that period come from it by ESPN id,
    and a player it lacks has no totals rather than the current pull's.
    """
    period = (None if stats is None
              else {e.get("id"): (e.get("player") or {}) for e in stats})
    rows = []
    for entry in players or []:
        player = entry.get("player") or {}
        own = player.get("ownership") or {}
        pos_id = player.get("defaultPositionId")
        pro = player.get("proTeamId")
        status = entry.get("status")
        pid = entry.get("id", player.get("id"))
        source = player if period is None else period.get(pid, {})
        rows.append({
            "espn_id": pid,
            "player": player.get("fullName"),
            "position": (positions.get(str(pos_id))
                         or (None if pos_id is None else f"position {pos_id}")),
            "pro_team": None if pro in (None, 0) else _ESPN_TEAM_ABBR.get(int(pro), f"team {pro}"),
            "status": status,
            "on_team_id": entry.get("onTeamId") or 0,
            "waiver_clears": eastern(entry.get("waiverProcessDate")) if status == "WAIVERS" else None,
            "injury_status": player.get("injuryStatus"),
            # ESPN's undroppable list; None when the entry does not carry it.
            "droppable": player.get("droppable"),
            "percent_owned": own.get("percentOwned"),
            "percent_change": own.get("percentChange"),
            "week_points": week_total(source, season, week, ACTUAL_SOURCE),
            "week_proj": week_total(source, season, week, PROJECTED_SOURCE),
            # ESPN's full-season projection from the current pull, None when it
            # files none. Not week-scoped, so never read from `stats`.
            "season_proj": espn_season_projection(player, season),
            "period_injury_status": None if period is None else source.get("injuryStatus"),
        })
    return rows


def acquirable(rows: list[dict]) -> list[dict]:
    """Rows a team can claim or add now: FREEAGENT or WAIVERS, and held by nobody."""
    return [r for r in rows if r["status"] in ACQUIRABLE and r["on_team_id"] == 0]
