"""One player's week, from every surface that has it, each part named.

ESPN's pool entry carries the fantasy total and the stat row it was applied
from (`stats` keyed by ESPN statId, source 0, split 1, the week's scoring
period). Read live on 2026-09-13: Carson Wentz week 1 carried 0:19, 1:12,
3:133, 4:3 and no key 20; Christian Watson 53:6, 42:147, 43:2, 58:8. The names
are the espn-api map (`refs/cwendt94/espn-api/espn_api/football/constant.py`,
`PLAYER_STATS_MAP`). Usage ESPN does not publish -- snaps, a player's share of
his team's targets and carries -- comes from nflverse, which is a separate
release on its own clock; a week it has not published is said, not guessed.
"""
from __future__ import annotations

import pandas as pd

from .board import norm_name
from .pool import ACTUAL_SOURCE, SINGLE_PERIOD_SPLIT, pool_rows

# ESPN statId -> name, the usage and scoring lines a week is read by.
ESPN_LINE_STATS = {
    0: "pass_attempts", 1: "pass_completions", 3: "pass_yards", 4: "pass_tds",
    20: "interceptions", 64: "sacked", 19: "pass_2pt",
    23: "carries", 24: "rush_yards", 25: "rush_tds", 26: "rush_2pt",
    58: "targets", 53: "receptions", 42: "rec_yards", 43: "rec_tds", 59: "yards_after_catch",
    44: "rec_2pt", 68: "fumbles", 72: "fumbles_lost",
    83: "fg_made", 84: "fg_attempted", 86: "pat_made", 87: "pat_attempted",
    120: "points_allowed", 127: "yards_allowed", 99: "sacks", 95: "def_interceptions",
    96: "fumble_recoveries", 94: "def_tds",
}


def espn_line(player: dict, season: int, week: int) -> dict | None:
    """The named stats in ESPN's actual row for the week, or None when there is
    no row. Only what ESPN sent: an id it did not send is not a zero here."""
    for stat in player.get("stats") or []:
        if (stat.get("seasonId") == season and stat.get("scoringPeriodId") == week
                and stat.get("statSourceId") == ACTUAL_SOURCE
                and stat.get("statSplitTypeId") == SINGLE_PERIOD_SPLIT):
            raw = stat.get("stats") or {}
            return {name: raw[str(sid)] for sid, name in ESPN_LINE_STATS.items()
                    if str(sid) in raw}
    return None


def opponent(schedule: pd.DataFrame, season: int, week: int, team: str | None) -> str | None:
    """"@ MIN" or "vs GB" for `team` in `week`, None on a bye or with no team."""
    if team is None or schedule.empty:
        return None
    games = schedule[(schedule["season"] == season) & (schedule["week"] == week)]
    if "game_type" in games.columns:
        games = games[games["game_type"] == "REG"]
    for _, g in games.iterrows():
        if g["home_team"] == team:
            return f"vs {g['away_team']}"
        if g["away_team"] == team:
            return f"@ {g['home_team']}"
    return None


def _share(part: float, whole: float) -> float | None:
    return None if not whole else round(float(part) / float(whole), 3)


def _count(value) -> float:
    """A usage count, with nflverse's missing value read as none taken. `NaN or
    0.0` is NaN, because NaN is truthy, so the test is `isna`."""
    number = pd.to_numeric(value, errors="coerce")
    return 0.0 if pd.isna(number) else float(number)


def nflverse_usage(weekly: pd.DataFrame, snaps: pd.DataFrame | None, season: int, week: int,
                   name: str, team: str | None) -> dict | None:
    """Targets, carries and their share of the team's week, plus snaps, for one
    player; None when the published week does not carry him. Joined by
    normalised name and, when both sides have one, team -- nflverse keys weekly
    stats and snaps by name, not by ESPN id."""
    w = weekly[(weekly["season"] == season) & (weekly["week"] == week)]
    if "season_type" in w.columns:
        w = w[w["season_type"] == "REG"]
    key = norm_name(name)
    mine = w[w["player_display_name"].map(norm_name) == key]
    if team is not None and len(mine) > 1:
        mine = mine[mine["recent_team"] == team]
    if mine.empty:
        return None
    row = mine.iloc[0]
    team_week = w[w["recent_team"] == row["recent_team"]]
    targets = _count(row.get("targets"))
    carries = _count(row.get("carries"))
    out = {
        "team": row["recent_team"],
        "targets": targets, "carries": carries,
        "receptions": _count(row.get("receptions")),
        "target_share": _share(targets, sum(_count(v) for v in team_week["targets"])),
        "carry_share": _share(carries, sum(_count(v) for v in team_week["carries"])),
        "ppr_points": row.get("fantasy_points_ppr"),
        "offense_snaps": None, "offense_pct": None,
    }
    if snaps is not None and not snaps.empty:
        s = snaps[(snaps["season"] == season) & (snaps["week"] == week)]
        if "game_type" in s.columns:
            s = s[s["game_type"] == "REG"]
        hit = s[s["player"].map(norm_name) == key]
        if len(hit) > 1:
            hit = hit[hit["team"] == row["recent_team"]]
        if not hit.empty:
            out["offense_snaps"] = hit.iloc[0]["offense_snaps"]
            out["offense_pct"] = hit.iloc[0]["offense_pct"]
    return out


def published_weeks(weekly: pd.DataFrame, season: int) -> list[int]:
    """The regular-season weeks nflverse has published for `season`."""
    w = weekly[weekly["season"] == season]
    if "season_type" in w.columns:
        w = w[w["season_type"] == "REG"]
    return sorted(int(x) for x in w["week"].dropna().unique())


def player_week_row(entry: dict, season: int, week: int, positions: dict[str, str],
                    schedule: pd.DataFrame | None, weekly: pd.DataFrame | None,
                    snaps: pd.DataFrame | None, period: dict | None = None) -> dict:
    """Everything known about one pool entry's `week`. `entry` is from the
    current pull; `period`, when given, is the same player's entry from a pull
    for `week` and supplies the week's numbers (see `pool`)."""
    base = pool_rows([entry], positions, season, week,
                     stats=None if period is None else [period])[0]
    player = (entry if period is None else period).get("player") or {}
    return {
        "player": base["player"], "position": base["position"], "pro_team": base["pro_team"],
        "espn_id": base["espn_id"], "status": base["status"], "on_team_id": base["on_team_id"],
        "injury_status": base["injury_status"],
        "week_injury_status": base["period_injury_status"],
        "opponent": None if schedule is None else opponent(schedule, season, week, base["pro_team"]),
        "week_points": base["week_points"], "week_proj": base["week_proj"],
        "espn_line": espn_line(player, season, week),
        "nflverse": (None if weekly is None
                     else nflverse_usage(weekly, snaps, season, week,
                                         str(base["player"] or ""), base["pro_team"])),
    }
