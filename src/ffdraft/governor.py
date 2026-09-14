"""Whether a tick belongs to fantasy operations or to engineering.

Three facts, each from a read:

  actionable   a started player of mine who is not locked and either is on bye
               (nfldata schedule) or is OUT, INJURY_RESERVE, DOUBTFUL,
               SUSPENSION or NA before his game: a lineup move ESPN still
               accepts. None while the scoreboard is ahead of the league week.
  observing    a game in progress with a player of mine, of my opponent, or of
               a team a pending decision point names.
  attention    the earliest of: the inactives list for a game with an unlocked
               player of mine (kickoff minus INACTIVES_LEAD_MINUTES), the waiver
               clear time, the next decision point, and a recheck while a game
               is being observed.

Mode, first match wins:

  DEGRADED   the scoreboard or the league could not be read
  HOT        actionable, or attention within HOT_MINUTES
  WATCH      observing
  DEEP_IDLE  idle budget of at least DEEP_IDLE_MINUTES, or nothing scheduled
  IDLE       otherwise

The idle budget is attention minus now minus SAFETY_MARGIN_MINUTES. Engineering
is allowed only in IDLE and DEEP_IDLE with at least MIN_ENGINEERING_MINUTES of
budget; the lease ends at attention minus the margin, capped at
MAX_LEASE_MINUTES. Risk class A in IDLE, up to B in DEEP_IDLE (`improve`).
Fantasy preempts: every tick starts from a fresh state.

Nothing here reads the network; `server.controller_state` supplies the reads.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .board import _ESPN_TEAM_ABBR
from .config import OUT_STATUSES, STATE_DIR

HOT_MINUTES = 30
SAFETY_MARGIN_MINUTES = 15
MIN_ENGINEERING_MINUTES = 15
DEEP_IDLE_MINUTES = 180
MAX_LEASE_MINUTES = 120
WATCH_RECHECK_MINUTES = 15
# NFL teams publish inactives 90 minutes before kickoff; an OUT or inactive
# starter is first knowable then.
INACTIVES_LEAD_MINUTES = 90
BENCH_SLOT, IR_SLOT = 20, 21
DECISION_POINTS = STATE_DIR / "decision_points.json"
CONTROLLER_STATE = STATE_DIR / "controller-state.json"
BASIS = {
    "actionable": "started (ESPN lineup slot not BENCH/IR), lineupLocked not true, and either "
                  "the team is on bye in the nfldata schedule or injuryStatus is in "
                  + "/".join(OUT_STATUSES) + " with the game state pre",
    "observing": "ESPN scoreboard state `in` for a team of mine, my opponent's, or a "
                 "pending decision point's",
    "attention": f"kickoff minus {INACTIVES_LEAD_MINUTES} min for games with an unlocked "
                 f"player of mine (inactives); waiverProcessDate; decision points in "
                 f"{DECISION_POINTS.name}; now plus {WATCH_RECHECK_MINUTES} min while "
                 f"observing",
    "budget": f"attention minus now minus {SAFETY_MARGIN_MINUTES} min",
}


def when(value) -> pd.Timestamp | None:
    """A UTC timestamp from an ISO string or epoch milliseconds, None when unreadable."""
    if value is None:
        return None
    try:
        ts = (pd.Timestamp(int(value), unit="ms", tz="UTC") if isinstance(value, (int, float))
              else pd.Timestamp(str(value)))
    except (TypeError, ValueError):
        return None
    if not isinstance(ts, pd.Timestamp) or pd.isna(ts):
        return None
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def eastern(ts: pd.Timestamp) -> str:
    return ts.tz_convert("America/New_York").strftime("%Y-%m-%d %H:%M ET")


def roster(payload: dict, team_id: int) -> list[dict]:
    """One fantasy team's players from an mRoster payload."""
    team = next((t for t in payload.get("teams") or [] if t.get("id") == team_id), {})
    out = []
    for e in (team.get("roster") or {}).get("entries") or []:
        entry = e.get("playerPoolEntry") or {}
        p = entry.get("player") or {}
        slot = e.get("lineupSlotId")
        out.append({"player": p.get("fullName"), "pro_team": _ESPN_TEAM_ABBR.get(p.get("proTeamId")),
                    "started": slot not in (BENCH_SLOT, IR_SLOT, None),
                    "locked": entry.get("lineupLocked"),
                    "injury_status": p.get("injuryStatus")})
    return out


def opponent_id(payload: dict, week: int, team_id: int) -> int | None:
    """My opponent's team id in `week`, from the league schedule."""
    for m in payload.get("schedule") or []:
        if m.get("matchupPeriodId") != week:
            continue
        home, away = (m.get("home") or {}).get("teamId"), (m.get("away") or {}).get("teamId")
        if team_id == home:
            return away
        if team_id == away:
            return home
    return None


def load_decision_points(path: Path | None = None) -> tuple[list[dict], str | None]:
    """The decision points on disk, and why they could not be read. A missing
    file is no decision points, not an error."""
    path = path or DECISION_POINTS
    if not path.exists():
        return [], None
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [], f"{type(exc).__name__}: {exc}"
    if not isinstance(rows, list):
        return [], f"{path.name} is not a JSON list"
    return [r for r in rows if isinstance(r, dict)], None


def add_decision_point(at: str, what: str, teams: list[str],
                       path: Path | None = None) -> dict:
    """Append one decision point; `at` must parse as a timestamp with a zone."""
    path = path or DECISION_POINTS
    ts = when(at)
    if ts is None or pd.Timestamp(at).tzinfo is None:
        raise ValueError(f"decision point time {at!r} must be ISO 8601 with a UTC offset")
    rows, err = load_decision_points(path)
    if err:
        raise ValueError(f"{path} is unreadable: {err}")
    row = {"at": ts.isoformat(), "what": what, "teams": sorted(set(teams))}
    rows.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return row


def write_state(state: dict, path: Path | None = None) -> None:
    path = path or CONTROLLER_STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1), encoding="utf-8")


def controller_state(at: object, games: list[dict], mine: list[dict],
                     theirs: list[dict], waiver_clears, decision_points: list[dict],
                     unread: dict[str, str], bye_teams: set[str] | frozenset[str] = frozenset(),
                     scoreboard_ahead: bool = False) -> dict:
    """The mode for this tick, the next moment fantasy needs attention, and what
    engineering may do until then. `at` is now, as anything `when` reads.
    `bye_teams` comes from the schedule, never from absence on the scoreboard.
    `scoreboard_ahead` means the scoreboard shows a later week than the league's
    scoring period: its games say nothing about this period's lineup, so my
    roster contributes no actionable and no inactives attention."""
    now = when(str(at))
    if now is None:
        raise ValueError(f"unreadable time {at!r}")
    by_team = {team: g for g in games for team in g.get("teams") or ()}
    attention: list[tuple[pd.Timestamp, str]] = []
    actionable: list[str] = []

    for p in [] if scoreboard_ahead else mine:
        if p["pro_team"] in bye_teams:
            if p["started"] and p["locked"] is not True:
                actionable.append(f"{p['player']} starts and {p['pro_team']} is on bye")
            continue
        g = by_team.get(p["pro_team"])
        kick = None if g is None else when(g.get("date"))
        if g is None or g.get("state") != "pre" or p["locked"] is True or kick is None or kick <= now:
            continue
        if p["started"] and p["injury_status"] in OUT_STATUSES:
            actionable.append(f"{p['player']} starts, is {p['injury_status']}, and locks "
                              f"{eastern(kick)}")
        attention.append((kick - pd.Timedelta(minutes=INACTIVES_LEAD_MINUTES),
                          f"inactives for {g.get('name')} ({p['player']} unlocked)"))

    clears = when(waiver_clears)
    if clears is not None and clears > now:
        attention.append((clears, "waivers process"))

    dependency: set[str] = set()
    for point in decision_points:
        at = when(point.get("at"))
        if at is None or at <= now:
            continue
        attention.append((at, f"decision point: {point.get('what')}"))
        dependency |= {str(t) for t in point.get("teams") or ()}

    relevant = ({p["pro_team"] for p in mine} | {p["pro_team"] for p in theirs} | dependency) - {None}
    observing = [f"{g.get('name')} {g.get('detail') or ''}".strip() for g in games
                 if g.get("state") == "in" and set(g.get("teams") or ()) & relevant]
    # A deadline, not the recheck, decides HOT: watching a game is not a reason
    # to call the tick urgent.
    deadline = min((t for t, _ in attention), default=None)
    if observing:
        attention.append((now + pd.Timedelta(minutes=WATCH_RECHECK_MINUTES),
                          f"recheck: {observing[0]} in progress"))

    attention.sort(key=lambda a: a[0])
    nxt = attention[0] if attention else None
    budget = (None if nxt is None
              else int((nxt[0] - now).total_seconds() // 60) - SAFETY_MARGIN_MINUTES)

    degraded = {k: v for k, v in unread.items() if k in ("scoreboard", "league")}
    if degraded:
        mode = "DEGRADED"
    elif actionable or (deadline is not None
                        and deadline - now <= pd.Timedelta(minutes=HOT_MINUTES)):
        mode = "HOT"
    elif observing:
        mode = "WATCH"
    elif budget is None or budget >= DEEP_IDLE_MINUTES:
        mode = "DEEP_IDLE"
    else:
        mode = "IDLE"

    allowed = mode in ("IDLE", "DEEP_IDLE") and (budget is None or budget >= MIN_ENGINEERING_MINUTES)
    minutes = min(MAX_LEASE_MINUTES, MAX_LEASE_MINUTES if budget is None else budget) if allowed else 0
    return {
        "mode": mode,
        "as_of": eastern(now),
        "fantasy_actionable": actionable,
        "observing": observing,
        "next_required_attention": None if nxt is None else {"at": eastern(nxt[0]), "why": nxt[1]},
        "attention_queue": [{"at": eastern(t), "why": why} for t, why in attention[:10]],
        "idle_budget_minutes": budget,
        "engineering": {
            "allowed": allowed,
            "max_minutes": minutes,
            "max_risk_class": ("B" if mode == "DEEP_IDLE" else "A") if allowed else None,
            "lease_expires": eastern(now + pd.Timedelta(minutes=minutes)) if allowed else None,
        },
        "degraded_capabilities": dict(unread),
        "basis": BASIS,
    }
