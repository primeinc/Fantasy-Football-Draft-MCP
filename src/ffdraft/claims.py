"""Waiver claims as replacement pairs, derived from the in-season read surfaces.

`ADD x -> DROP y`, with the evidence for both sides. Every input is read by
another surface and named in the output: my roster and ESPN's lineup slots
(mRoster), the league's starting slot counts (mSettings
`rosterSettings.lineupSlotCounts`), the pool for the week just played (ESPN's
applied totals) and for the week claimed for (ESPN's projections, status,
waiver clear time, the undroppable flag), the schedule for byes, and ESPN's
scoreboard for games not yet final.

Nothing here is a fitted score and no ordering here is backtested. Candidates
are ordered by ESPN's projection for the claim week, then last week's ESPN
points. The drop is the bench player with the lowest claim-week projection whose
played-week game is final: a bench player still to play is held, because his
result is evidence the drop decision has not seen, and a claim whose drop a held
player could replace says so in `drop_provisional`. A starter ESPN has slotted is
never offered as a drop.
"""
from __future__ import annotations

import pandas as pd

from .board import espn_league_get
from .config import CURRENT_SEASON, OUT_STATUSES
from .pool import acquirable

BENCH_SLOT = 20
# Not out, not safe. ESPN moved Kyler Murray from OUT to QUESTIONABLE on the
# evening of his week 1 concussion (2026-09-13), with the report unchanged: the
# status describes the next game, and a questionable starter with nobody behind
# him is the insurance case, not a need.
AT_RISK_STATUSES = ("QUESTIONABLE", "DAY_TO_DAY")
SLOT_POSITIONS = {"0": "QB", "2": "RB", "4": "WR", "6": "TE", "16": "DST", "17": "K"}
CLAIM_ORDER_NOTE = (
    "Claims are listed in the order to enter them; each names its drop. ESPN processes a "
    "team's claims in its set order when waivers clear. Whether a later claim that names a "
    "drop an earlier successful claim already used is skipped has not been verified against "
    "this league.")
BASIS = {
    "needs": ("a starting slot count from mSettings lineupSlotCounts not met by rostered "
              "players who are neither OUT, INJURY_RESERVE, DOUBTFUL, SUSPENSION or NA nor on "
              "bye in the claim week"),
    "candidates": ("acquirable in ESPN's pool (FREEAGENT or WAIVERS, no team), not out; ordered "
                   "by ESPN's projection for the claim week, then last week's ESPN points "
                   "(unmeasured: no backtest covers this ordering); a player on the NFL team "
                   "of the starter the need is for is always listed, with `same_team_as`, "
                   "because his claim-week projection assumes that starter plays"),
    "drops": ("ESPN BENCH slot only; lowest ESPN claim-week projection first, a player with "
              "none after those with one; a player whose played-week game is not final on "
              "ESPN's scoreboard is held, and every bench player is held when the scoreboard "
              "cannot say; undroppable players and a player whose loss leaves his position "
              "short are not offered"),
    "upgrades": "same position as the drop, projected above it for the claim week",
}


def fetch_settings(league_id: str, season: int = CURRENT_SEASON, swid: str | None = None,
                   espn_s2: str | None = None) -> dict:
    """The league's mSettings `settings` object."""
    resp = espn_league_get(league_id, season, {"view": "mSettings"}, swid, espn_s2)
    resp.raise_for_status()
    return resp.json().get("settings") or {}


def required_starters(settings: dict) -> dict[str, int]:
    """Position -> starting slots, from ESPN's own slot counts. Flex slots are
    not a position requirement and are left out."""
    counts = (settings.get("rosterSettings") or {}).get("lineupSlotCounts") or {}
    return {pos: int(counts[sid]) for sid, pos in SLOT_POSITIONS.items()
            if int(counts.get(sid) or 0) > 0}


def byes(schedule: pd.DataFrame, season: int, week: int) -> set[str]:
    """Teams with a regular-season game in `season` and none in `week`."""
    games = schedule[schedule["season"] == season]
    if "game_type" in games.columns:
        games = games[games["game_type"] == "REG"]
    teams = set(games["home_team"]) | set(games["away_team"])
    this_week = games[games["week"] == week]
    playing = set(this_week["home_team"]) | set(this_week["away_team"])
    return {str(t) for t in teams - playing}


def pending_teams(games: list[dict], scoreboard_week: int | None,
                  played_week: int) -> set[str] | None:
    """Teams whose `played_week` game is not final: none once the scoreboard has
    moved past that week, None when it shows an earlier week or no week."""
    if scoreboard_week is not None and scoreboard_week > played_week:
        return set()
    if scoreboard_week != played_week:
        return None
    return {str(t) for g in games if g.get("state") != "post" for t in g.get("teams") or ()}


def merge_weeks(claim_rows: list[dict], played_rows: list[dict]) -> list[dict]:
    """Claim-week pool rows with the played week's ESPN actual and projection
    beside them, joined on ESPN id."""
    played = {r["espn_id"]: r for r in played_rows}
    return [{**r, "claim_week_proj": r["week_proj"],
             "claim_week_injury_status": r.get("period_injury_status"),
             "last_week_points": played.get(r["espn_id"], {}).get("week_points"),
             "last_week_proj": played.get(r["espn_id"], {}).get("week_proj")}
            for r in claim_rows]


def _unavailable(row: dict, bye_teams: set[str]) -> str | None:
    if row["injury_status"] in OUT_STATUSES:
        return str(row["injury_status"])
    if row["pro_team"] in bye_teams:
        return "bye"
    return None


def _why(row: dict, reason: str | None) -> str:
    """"Kyler Murray: OUT", with ESPN's claim-week status beside it when that
    period lists him differently -- the need is then conditional on which holds."""
    text = f"{row['player']}: {reason}"
    listed = row.get("claim_week_injury_status")
    if reason != "bye" and listed and listed != reason:
        text += f" now; ESPN lists {listed} for the claim week"
    return text


def needs(mine: list[dict], required: dict[str, int], bye_teams: set[str]) -> list[dict]:
    """Starting positions the roster cannot fill in the claim week, and why.
    `starter_teams` maps the NFL team of each injured starter to his name."""
    out = []
    for pos, count in required.items():
        at = [r for r in mine if r["position"] == pos]
        available = [r for r in at if _unavailable(r, bye_teams) is None]
        if len(available) < count:
            why = [_why(r, _unavailable(r, bye_teams)) for r in at if _unavailable(r, bye_teams)]
            out.append({"position": pos, "short": count - len(available), "required": count,
                        "why": why or [f"no {pos} on the roster"],
                        "starter_teams": {r["pro_team"]: r["player"] for r in at
                                          if _unavailable(r, bye_teams) not in (None, "bye")}})
    return out


def no_backup(mine: list[dict], required: dict[str, int], bye_teams: set[str]) -> list[str]:
    """Positions filled exactly, with nobody behind the starters."""
    return [pos for pos, count in required.items()
            if len([r for r in mine if r["position"] == pos
                    and _unavailable(r, bye_teams) is None]) == count]


def at_risk(mine: list[dict], required: dict[str, int], bye_teams: set[str]) -> list[dict]:
    """Positions with nobody behind the starters where a starter is QUESTIONABLE
    or DAY_TO_DAY: the claims these produce are insurance, conditional on him
    missing the game."""
    out = []
    for pos in no_backup(mine, required, bye_teams):
        risky = [r for r in mine if r["position"] == pos and r["injury_status"] in AT_RISK_STATUSES]
        if risky:
            out.append({"position": pos, "short": 0, "required": required[pos],
                        "why": [_why(r, str(r["injury_status"])) for r in risky],
                        "starter_teams": {r["pro_team"]: r["player"] for r in risky}})
    return out


def _evidence(row: dict) -> dict:
    return {"player": row["player"], "position": row["position"], "pro_team": row["pro_team"],
            "status": row["status"], "waiver_clears": row["waiver_clears"],
            "percent_owned": row["percent_owned"], "injury_status": row["injury_status"],
            "last_week_points": row["last_week_points"],
            "last_week_proj": row.get("last_week_proj"),
            "claim_week_proj": row["claim_week_proj"], "espn_id": row["espn_id"]}


def _none_last(value: float | None) -> tuple[bool, float]:
    return value is None, 0.0 if value is None else value


def drop_options(mine: list[dict], slots: dict, required: dict[str, int], bye_teams: set[str],
                 pending: set[str] | None) -> tuple[list[dict], list[dict]]:
    """Bench players who could be dropped, cheapest first, and the bench players
    who cannot be, each with the reason."""
    options, excluded = [], []
    for row in mine:
        if slots.get(row["espn_id"]) != BENCH_SLOT:
            continue
        if row.get("droppable") is False:
            excluded.append({"player": row["player"], "reason": "on ESPN's undroppable list"})
            continue
        count = required.get(str(row["position"]), 0)
        others = [r for r in mine if r["position"] == row["position"] and r is not row
                  and _unavailable(r, bye_teams) is None]
        if count and _unavailable(row, bye_teams) is None and len(others) < count:
            excluded.append({"player": row["player"],
                             "reason": f"dropping him leaves {row['position']} short"})
            continue
        if pending is None:
            hold = "ESPN's scoreboard cannot say whether his game this week is final"
        elif row["pro_team"] in pending:
            hold = f"his {row['pro_team']} game this week is not final"
        else:
            hold = None
        options.append({**_evidence(row), "hold": hold})
    options.sort(key=lambda r: (r["hold"] is not None, _none_last(r["claim_week_proj"]),
                                _none_last(r["last_week_points"])))
    return options, excluded


def candidates(merged: list[dict], position: str, limit: int,
               starter_teams: dict[str, str] | None = None) -> list[dict]:
    """Acquirable players at `position` who are not out, best first, plus any
    player on a `starter_teams` team below the cut, marked `same_team_as`."""
    teams = starter_teams or {}
    rows = [r for r in acquirable(merged)
            if r["position"] == position and r["injury_status"] not in OUT_STATUSES]
    rows.sort(key=lambda r: (r["claim_week_proj"] is None, -(r["claim_week_proj"] or 0.0),
                             r["last_week_points"] is None, -(r["last_week_points"] or 0.0)))
    cut = max(1, limit)
    picked = rows[:cut] + [r for r in rows[cut:] if r["pro_team"] in teams]
    return [{**_evidence(r), "same_team_as": teams.get(r["pro_team"])} for r in picked]


def _provisional(drop: dict, drops: list[dict]) -> str | None:
    """Held bench players who could be a cheaper drop than `drop` once their games end."""
    held = [d["player"] for d in drops if d["hold"] is not None
            and (d["claim_week_proj"] is None or drop["claim_week_proj"] is None
                 or d["claim_week_proj"] < drop["claim_week_proj"])]
    if not held:
        return None
    return (f"{', '.join(held)} held and projected below {drop['player']}: re-run once those "
            f"games are final and before waivers clear; the drop can change")


def plan(need_rows: list[dict], drops: list[dict], merged: list[dict], limit: int,
         drops_taken: int = 0) -> list[dict]:
    """Ordered claims: for each need, its best candidates, each naming the
    cheapest drop not already given to an earlier need. `drops_taken` skips
    the drops an earlier plan (the needs, for the insurance plan) has used.
    `fallback_for` names the claim immediately before, at the same need."""
    usable = [d for d in drops if d["hold"] is None]
    claims: list[dict] = []
    for k, need in enumerate(need_rows, start=drops_taken):
        drop = usable[k] if k < len(usable) else None
        cands = candidates(merged, need["position"], limit, need.get("starter_teams"))
        for rank, cand in enumerate(cands):
            claims.append({
                "order": len(claims) + 1, "need": need["position"],
                "add": cand["player"], "drop": None if drop is None else drop["player"],
                "fallback_for": None if rank == 0 else claims[-1]["add"],
                "drop_provisional": None if drop is None else _provisional(drop, drops),
                "add_evidence": cand,
                "no_drop_reason": (None if drop is not None else
                                   "every bench player is held, undroppable, or needed at "
                                   "his position"),
            })
    return claims


def upgrades(merged: list[dict], drops: list[dict], limit: int) -> list[dict]:
    """Optional: acquirable players projected above the cheapest usable drop at
    their own position."""
    cheapest: dict[str, dict] = {}
    for d in drops:
        if d["hold"] is None and d["claim_week_proj"] is not None:
            cheapest.setdefault(d["position"], d)
    pairs = []
    for r in acquirable(merged):
        drop = cheapest.get(r["position"])
        if (drop is not None and r["claim_week_proj"] is not None
                and r["injury_status"] not in OUT_STATUSES
                and r["claim_week_proj"] > drop["claim_week_proj"]):
            pairs.append((r, drop))
    pairs.sort(key=lambda p: -(p[0]["claim_week_proj"] - p[1]["claim_week_proj"]))
    return [{"add": r["player"], "position": r["position"], "drop": d["player"],
             "margin": round(r["claim_week_proj"] - d["claim_week_proj"], 2),
             "add_evidence": _evidence(r)} for r, d in pairs[:max(1, limit)]]
