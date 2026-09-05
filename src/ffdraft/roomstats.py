"""Who was in the ESPN draft room, how long, and who talked.

Two sources reduce to the same thing. A running `DraftWatch` keeps every socket
line with a receive timestamp (`DraftWatch.lines`), the INIT snapshot's online
flags, and the league directory; `espn_dump.dump_draft` writes the same lines to
`live/lines.jsonl`, the snapshot to `live/init.json` and the member list to
`read_api/mTeam.json`. `read_api/kona_league_communication.json` adds league
activity outside the room, one topic per change with an author and a date.

The socket identifies people by SWID. SWIDs are the join key here and nothing
else: every number in the output is reported against a team and owner name, and
an unknown SWID is reported as `UNKNOWN_LABEL`, never echoed.

Timestamps are epoch milliseconds. Clock hours are the machine's local time,
which is the office's, since the report exists to say when people were around.
"""
from __future__ import annotations

import datetime as dt
import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote_plus

from . import board as bd

# Shown in place of an owner name we cannot resolve. Never the SWID.
UNKNOWN_LABEL = "unknown member"
# Hours listed under `top_hours` per member.
TOP_HOURS = 3
# A gap between two picks longer than this is a draft pause (a break, a
# lunch, the room going away), not a member thinking. It is still reported
# under `slowest_seconds`, but is left out of the median and the mean.
PICK_GAP_CAP_SECONDS = 1800.0


def _iso(ms: int | None) -> str | None:
    """Epoch ms as a local-time ISO string to the second."""
    if ms is None:
        return None
    return dt.datetime.fromtimestamp(ms / 1000).isoformat(timespec="seconds")


def _hour(ms: int) -> str:
    return f"{dt.datetime.fromtimestamp(ms / 1000).hour:02d}"


@dataclass
class RoomLog:
    """Everything one draft yields about who was present, from either source."""

    # (receive ms, socket line), oldest first.
    lines: list[tuple[int, str]] = field(default_factory=list)
    # ESPN team id -> whether an owner was already in the room at the first line.
    online_at_start: dict[int, bool] = field(default_factory=dict)
    # ESPN team id -> {"name": str, "owners": [str]}.
    directory: dict[int, dict] = field(default_factory=dict)
    # Owner SWID (upper, no braces) -> display name. Never emitted.
    member_names: dict[str, str] = field(default_factory=dict)
    # (ms, author SWID) for each league activity topic, from the read API.
    activity: list[tuple[int, str]] = field(default_factory=list)
    source: str = "unknown"

    def owner_name(self, swid: str) -> str:
        return self.member_names.get(str(swid).strip("{}").upper()) or UNKNOWN_LABEL

    def team_of_owner(self, swid: str) -> int | None:
        key = str(swid).strip("{}").upper()
        name = self.member_names.get(key)
        for team_id, entry in self.directory.items():
            if name and name in (entry.get("owners") or []):
                return team_id
        return None

    def team_label(self, team_id: int) -> str:
        entry = self.directory.get(team_id) or {}
        return entry.get("name") or f"team {team_id}"

    def owners(self, team_id: int) -> list[str]:
        return list((self.directory.get(team_id) or {}).get("owners") or [])


def from_watch(watch: Any, member_names: dict[str, str] | None = None) -> RoomLog:
    """The log a running watch is holding.

    `DraftWatch.lines` is the superset of `presence` and `chat` — both are
    derived from it — so the lines are parsed and the two lists are used only
    to recover a watch whose lines were cleared. The watch keeps names by team,
    not by SWID, so `member_names` is optional: without it a chat line is
    attributed to the team's owner, which is the same person unless the team is
    co-owned.
    """
    lines = list(getattr(watch, "lines", []) or [])
    if not lines:
        lines = _lines_from_presence_and_chat(watch)
    online = {int(t): bool(on) for t, on in (getattr(watch, "online", {}) or {}).items()}
    directory = {int(t): dict(d) for t, d in (getattr(watch, "directory", {}) or {}).items()}
    return RoomLog(lines=lines, online_at_start=online, directory=directory,
                   member_names=dict(member_names or {}), source="watch")


def _lines_from_presence_and_chat(watch: Any) -> list[tuple[int, str]]:
    """Rebuild socket lines from the watch's own presence and chat lists."""
    out: list[tuple[int, str]] = []
    for ms, team, event in getattr(watch, "presence", []) or []:
        out.append((int(ms), f"{'JOINED' if event == 'joined' else 'LEFT'} {int(team)} - 0"))
    for ms, team, owner, text in getattr(watch, "chat", []) or []:
        out.append((int(ms), f"CHAT {int(team)} {owner} {int(ms)} {text}"))
    out.sort(key=lambda row: row[0])
    return out


def from_dump(dump_dir: str | Path) -> RoomLog:
    """The log a dump directory holds (`espn_dump.dump_draft` output)."""
    root = Path(dump_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"no such dump directory: {root}")
    lines: list[tuple[int, str]] = []
    jsonl = root / "live" / "lines.jsonl"
    if jsonl.is_file():
        for raw in jsonl.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            row = json.loads(raw)
            lines.append((int(row["ms"]), str(row["line"])))
    lines.sort(key=lambda row: row[0])

    online: dict[int, bool] = {}
    init_path = root / "live" / "init.json"
    if init_path.is_file():
        init = json.loads(init_path.read_text(encoding="utf-8"))
        for team in ((init.get("league") or {}).get("draft_teams") or []):
            if not team:
                continue
            owners = [o for o in (team.get("owners") or []) if o]
            online[int(team["team_id"])] = any(o.get("is_online") for o in owners)

    directory: dict[int, dict] = {}
    members: dict[str, str] = {}
    mteam = root / "read_api" / "mTeam.json"
    if mteam.is_file():
        data = json.loads(mteam.read_text(encoding="utf-8"))
        directory = bd.league_directory_from_mteam(data)
        members = bd.mteam_member_names(data)

    activity: list[tuple[int, str]] = []
    comms = root / "read_api" / "kona_league_communication.json"
    if comms.is_file():
        payload = json.loads(comms.read_text(encoding="utf-8"))
        for topic in ((payload.get("communication") or {}).get("topics") or []):
            author = topic.get("author")
            date = topic.get("date")
            if author and date:
                activity.append((int(date), str(author)))
        activity.sort(key=lambda row: row[0])

    return RoomLog(lines=lines, online_at_start=online, directory=directory,
                   member_names=members, activity=activity, source=f"dump {root.name}")


def find_dump(search_dir: str | Path = ".") -> Path | None:
    """The newest `espn_dump_*` directory under `search_dir`, if there is one."""
    root = Path(search_dir)
    if root.is_dir() and (root / "live").is_dir():
        return root
    dumps = sorted((p for p in root.glob("espn_dump_*") if p.is_dir()), key=lambda p: p.name)
    return dumps[-1] if dumps else None


# -- parsing


@dataclass
class _Events:
    joins: dict[int, int] = field(default_factory=dict)
    leaves: dict[int, int] = field(default_factory=dict)
    sessions: dict[int, list[tuple[int, int]]] = field(default_factory=dict)
    # Teams whose last session was still open at the final line.
    open_at_end: set[int] = field(default_factory=set)
    chats: list[tuple[int, int, str, str]] = field(default_factory=list)
    picks: dict[int, list[int]] = field(default_factory=dict)
    pick_seconds: dict[int, list[float]] = field(default_factory=dict)
    stamps: dict[int, list[int]] = field(default_factory=dict)


def _parse(log: RoomLog) -> _Events:
    """Replay the socket lines into presence sessions, chat and pick timings."""
    ev = _Events()
    if not log.lines:
        return ev
    start_ms, end_ms = log.lines[0][0], log.lines[-1][0]
    open_since: dict[int, int] = {}
    for team_id, was_online in sorted(log.online_at_start.items()):
        if was_online:
            open_since[team_id] = start_ms

    last_pick_ms: int | None = None
    for ms, line in log.lines:
        fields = line.split(" ")
        kind = fields[0]
        if kind == "INIT":
            last_pick_ms = None
        elif kind == "JOINED" and len(fields) >= 2 and fields[1].isdigit():
            team = int(fields[1])
            ev.joins[team] = ev.joins.get(team, 0) + 1
            ev.stamps.setdefault(team, []).append(ms)
            open_since.setdefault(team, ms)
        elif kind == "LEFT" and len(fields) >= 2 and fields[1].isdigit():
            team = int(fields[1])
            ev.leaves[team] = ev.leaves.get(team, 0) + 1
            ev.stamps.setdefault(team, []).append(ms)
            since = open_since.pop(team, None)
            if since is not None:
                ev.sessions.setdefault(team, []).append((since, ms))
        elif kind == "CHAT" and len(fields) >= 5 and fields[1].isdigit():
            team = int(fields[1])
            # ESPN replays room chat on join with its original send time in
            # field 3; the receive stamp would collapse the whole history
            # into the moment the watch connected.
            sent = int(fields[3]) if fields[3].isdigit() else ms
            text = unquote_plus(" ".join(fields[4:]))
            ev.chats.append((sent, team, fields[2], text))
            ev.stamps.setdefault(team, []).append(sent)
        elif kind == "SELECTED" and len(fields) >= 3 and fields[1].isdigit():
            team = int(fields[1])
            ev.picks.setdefault(team, []).append(ms)
            ev.stamps.setdefault(team, []).append(ms)
            if last_pick_ms is not None:
                # ESPN starts the next team's clock the moment the previous
                # pick lands, so the gap between consecutive SELECTED lines
                # is that team's time on the clock.
                ev.pick_seconds.setdefault(team, []).append((ms - last_pick_ms) / 1000.0)
            last_pick_ms = ms
        elif kind == "UNDONE":
            # The rolled-back pick's clock is not comparable to the next one.
            last_pick_ms = None

    for team, since in sorted(open_since.items()):
        # The log ends, not the session: it is closed at the last line and the
        # team is marked as still in the room.
        ev.sessions.setdefault(team, []).append((since, max(since, end_ms)))
        ev.open_at_end.add(team)
    return ev


def _clock_summary(seconds: list[float]) -> dict | None:
    if not seconds:
        return None
    timed = [s for s in seconds if s <= PICK_GAP_CAP_SECONDS]
    return {
        "n": len(seconds),
        "n_timed": len(timed),
        "median_seconds": round(statistics.median(timed), 1) if timed else None,
        "mean_seconds": round(statistics.fmean(timed), 1) if timed else None,
        "fastest_seconds": round(min(seconds), 1),
        "slowest_seconds": round(max(seconds), 1),
    }


def room_stats(log: RoomLog) -> dict:
    """Per member: time in the room, joins, messages, busiest hours, first and
    last seen, and the time each pick took from the clock starting."""
    ev = _parse(log)
    window_from = log.lines[0][0] if log.lines else None
    window_to = log.lines[-1][0] if log.lines else None

    activity_by_team: dict[int, list[int]] = {}
    activity_unmatched = 0
    for ms, swid in log.activity:
        team = log.team_of_owner(swid)
        if team is None:
            activity_unmatched += 1
            continue
        activity_by_team.setdefault(team, []).append(ms)

    team_ids = sorted(set(log.directory) | set(ev.sessions) | set(ev.joins) | set(ev.leaves)
                      | set(ev.picks) | set(activity_by_team)
                      | {team for _ms, team, _swid, _text in ev.chats})

    members: list[dict[str, Any]] = []
    for team_id in team_ids:
        sessions = ev.sessions.get(team_id, [])
        minutes = sum(to - since for since, to in sessions) / 60000.0
        chats = [(ms, swid, text) for ms, team, swid, text in ev.chats if team == team_id]
        by_owner: dict[str, int] = {}
        for _ms, swid, _text in chats:
            name = log.owner_name(swid)
            if name == UNKNOWN_LABEL and len(log.owners(team_id)) == 1:
                name = log.owners(team_id)[0]
            by_owner[name] = by_owner.get(name, 0) + 1
        # First and last seen are room events only. Hours are room events plus
        # league activity, because a dump taken without a running watch holds
        # one instant of room presence and days of activity topics.
        room = sorted(ev.stamps.get(team_id, []))
        hours: dict[str, int] = {}
        for ms in room + activity_by_team.get(team_id, []):
            key = _hour(ms)
            hours[key] = hours.get(key, 0) + 1
        top = sorted(hours.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_HOURS]
        acts = activity_by_team.get(team_id, [])
        members.append({
            "team_id": team_id,
            "team": log.team_label(team_id),
            "owners": log.owners(team_id),
            "minutes_in_room": round(minutes, 1),
            "joins": ev.joins.get(team_id, 0),
            "leaves": ev.leaves.get(team_id, 0),
            "in_room_at_start": bool(log.online_at_start.get(team_id)),
            "in_room_at_end": team_id in ev.open_at_end,
            "sessions": [{"from": _iso(since), "to": _iso(to),
                          "minutes": round((to - since) / 60000.0, 1),
                          "still_open": team_id in ev.open_at_end and n == len(sessions) - 1}
                         for n, (since, to) in enumerate(sessions)],
            "messages": len(chats),
            "messages_by_owner": by_owner,
            "last_message": chats[-1][2] if chats else None,
            "first_seen": _iso(room[0]) if room else None,
            "last_seen": _iso(room[-1]) if room else None,
            "active_hours": hours,
            "top_hours": [f"{h}:00" for h, _n in top],
            "picks": len(ev.picks.get(team_id, [])),
            "clock_to_pick": _clock_summary(ev.pick_seconds.get(team_id, [])),
            "league_activity": {"count": len(acts), "first": _iso(min(acts)) if acts else None,
                                "last": _iso(max(acts)) if acts else None},
        })
    members.sort(key=lambda m: (-m["minutes_in_room"], -m["messages"], m["team"]))

    all_seconds = [s for team in ev.pick_seconds.values() for s in team]
    return {
        "source": log.source,
        "window": {"from": _iso(window_from), "to": _iso(window_to),
                   "minutes": round((window_to - window_from) / 60000.0, 1)
                   if window_from is not None and window_to is not None else None,
                   "lines": len(log.lines)},
        "totals": {
            "members": len(members),
            "in_room_at_start": sum(1 for m in members if m["in_room_at_start"]),
            "messages": sum(m["messages"] for m in members),
            "picks_seen": sum(m["picks"] for m in members),
            "clock_to_pick": _clock_summary(all_seconds),
            "league_activity_topics": len(log.activity),
            "league_activity_unmatched": activity_unmatched,
        },
        "members": members,
    }


def format_table(stats: dict, width: int = 22) -> str:
    """The same numbers as a plain-text table, for pasting into an email."""
    window = stats["window"]
    out = [f"ESPN draft room — {stats['source']}"]
    if window["from"]:
        out.append(f"{window['from']} to {window['to']}  ({window['minutes']} minutes of socket, "
                   f"{window['lines']} lines)")
    totals = stats["totals"]
    clock = totals["clock_to_pick"]
    out.append(f"{totals['members']} members, {totals['messages']} messages, "
               f"{totals['picks_seen']} picks observed"
               + (f", median {clock['median_seconds']}s on the clock" if clock else ""))
    if not totals["picks_seen"] and not (window["minutes"] or 0):
        out.append("No watch was running: room presence is the instant the snapshot was taken, "
                   "and the busiest hour comes from league activity.")
    out.append("")
    head = (f"{'member':<{width}} {'team':<{width}} {'mins':>6} {'joins':>5} {'msgs':>5} "
            f"{'picks':>5} {'s/pick':>7} {'busiest':>8} {'first seen':>19}")
    out.append(head)
    out.append("-" * len(head))
    for m in stats["members"]:
        owners = ", ".join(m["owners"]) or UNKNOWN_LABEL
        clock = m["clock_to_pick"]
        seconds = "" if clock is None or clock["median_seconds"] is None else f"{clock['median_seconds']:.0f}"
        out.append(f"{owners[:width]:<{width}} {m['team'][:width]:<{width}} "
                   f"{m['minutes_in_room']:>6.1f} {m['joins']:>5} {m['messages']:>5} "
                   f"{m['picks']:>5} {seconds:>7} "
                   f"{(m['top_hours'][0] if m['top_hours'] else ''):>8} "
                   f"{(m['first_seen'] or ''):>19}")
    return "\n".join(out)
