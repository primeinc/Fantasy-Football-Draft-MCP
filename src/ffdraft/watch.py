"""Hold the ESPN draft room socket open and push each pick to the MCP client.

Claude Code delivers `notifications/claude/channel` events into the session as
`<channel>` messages (docs/data-sources.md, "ESPN live draft socket"). The loop
here owns the socket; `server.watch_draft` supplies the notify callable.

The watch is also the only process that sees the market as it stood during the
draft. ESPN's ADP, PPR rank and projection all move afterwards, so a replay run
days later prices every pick with numbers nobody had at the time. On the INIT
snapshot and on every SELECTED it writes a small parquet of the market for the
players still available, and `replay.replay_draft(as_of=True)` reads them back.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import unquote_plus

import pandas as pd
from websockets.asyncio.client import connect

from . import board as bd
from . import espn_live, model
from .config import STATE_DIR, LeagueSettings

log = logging.getLogger(__name__)

# Rows per snapshot: the players still on the board, cheapest ADP first. Deep
# enough to cover anything the room will take in the next few rounds without
# writing the whole board once per pick -- at 300 rows a full draft's snapshots
# are a couple of megabytes.
SNAPSHOT_ROWS = 300
# What is worth keeping: the three market numbers that move, plus the key the
# board joins on. ESPN's own player id is not a board column (`board.espn_maps`
# resolves that separately) and a replay does not need it to price a pick.
SNAPSHOT_MARKET = ("adp", "espn_rank", "espn_proj")


def snapshot_dir(league_id: str) -> Path:
    """Where one league's as-of snapshots live: one parquet per pick number."""
    return STATE_DIR / f"snapshots_{league_id}"


def snapshot_path(league_id: str, pick: int) -> Path:
    return snapshot_dir(league_id) / f"{pick}.parquet"


def write_snapshot(board_df: pd.DataFrame | None, taken: set[str], league_id: str,
                   pick: int, rows: int | None = None) -> Path | None:
    """The market as the board holds it now, for the players still available,
    filed under the pick that is on the clock.

    Returns the path written, or None when there was nothing to write. Never
    raises: this runs inside the socket loop, and losing a snapshot must not
    cost the pick that arrived with it."""
    if board_df is None or board_df.empty or "_key" not in board_df.columns:
        return None
    try:
        avail = board_df[~board_df["_key"].isin(taken)]
        cols = ["_key", *[c for c in SNAPSHOT_MARKET if c in avail.columns]]
        if "player_id" in avail.columns:
            cols.insert(1, "player_id")
        if "adp" in avail.columns:
            avail = avail.sort_values("adp", na_position="last")
        # Read here, not bound as a default, so the bound stays adjustable.
        out = avail[cols].head(SNAPSHOT_ROWS if rows is None else rows).reset_index(drop=True)
        path = snapshot_path(league_id, pick)
        path.parent.mkdir(parents=True, exist_ok=True)
        out.to_parquet(path, index=False)
        return path
    except Exception:
        log.exception("could not write the as-of snapshot for pick %s", pick)
        return None


def resolve_snapshots(league_id_or_dir: str | Path) -> Path:
    """Where to read snapshots from. A `Path`, or a string that names a
    directory, is the directory itself -- a replay can be pointed at a copied
    set. Anything else is a league id. The string case matters because
    `draft_replay` takes its argument over MCP, where every value is a string,
    so a caller passing a path must not be sent down the league-id branch and
    silently read nothing."""
    if isinstance(league_id_or_dir, Path):
        return league_id_or_dir
    text = str(league_id_or_dir)
    candidate = Path(text)
    if candidate.is_dir() or "/" in text or "\\" in text:
        return candidate
    return snapshot_dir(text)


def read_snapshot(league_id_or_dir: str | Path, pick: int) -> pd.DataFrame | None:
    """One pick's snapshot, or None when it was never written."""
    path = resolve_snapshots(league_id_or_dir) / f"{pick}.parquet"
    if not path.is_file():
        return None
    try:
        return pd.read_parquet(path)
    except Exception:
        log.exception("could not read the as-of snapshot at %s", path)
        return None


def drop_snapshots_above(league_id: str, pick: int) -> list[int]:
    """Delete the snapshots for picks after `pick` and return what went. Called
    on UNDONE: a rolled-back pick's file describes a board state that no longer
    happened, and leaving it would let a later as-of replay price a pick from a
    world the draft backed out of."""
    root = snapshot_dir(league_id)
    gone: list[int] = []
    if not root.is_dir():
        return gone
    for path in root.glob("*.parquet"):
        if not path.stem.isdigit() or int(path.stem) <= pick:
            continue
        try:
            path.unlink()
            gone.append(int(path.stem))
        except OSError:
            log.exception("could not remove the stale as-of snapshot at %s", path)
    return sorted(gone)

Notify = Callable[[str, dict[str, str]], Awaitable[None]]

# ESPN's draft client pings every 15 s on a timer, first ping 1 s after connect
# (draft.js: pingInterval=15e3, firstPing after Xe=1e3). The server drops clients
# that stay quiet, and inbound CLOCK ticks do not count as activity.
PING_SECONDS = 15.0
FIRST_PING_SECONDS = 1.0
# Consecutive failed sessions before the watch gives up. Each attempt is a real
# connect bounded by open_timeout; a session that reaches INIT resets the count.
MAX_FAILED_SESSIONS = 5
# Push a recommendation with the pick event once the user is this close to the clock.
RECOMMEND_WITHIN = 3


class DraftWatch:
    def __init__(self, league_id: str, season: int, team_id: int, swid: str, espn_s2: str,
                 league: LeagueSettings, board_df, notify: Notify,
                 directory: dict[int, dict] | None = None, bye_weight: float = 0.0,
                 refresh: Callable[[], tuple] | None = None) -> None:
        self.league_id = league_id
        self.bye_weight = bye_weight
        # Returns (board, bye_weight) as they are NOW; without it the watch keeps
        # recommending off the board it was started with.
        self.refresh = refresh
        # ESPN team id -> {"name": team name, "owners": [display names]}.
        self.directory = directory or {}
        # ESPN team id -> whether an owner is in the draft room. Seeded from the
        # INIT snapshot's owner flags, then JOINED/LEFT lines.
        self.online: dict[int, bool] = {}
        # (epoch ms, team id, owner SWID, text), newest last. Room chat only.
        self.chat: list[tuple[int, int, str, str]] = []
        # (epoch ms, team id, "joined"|"left"), newest last, from this connection on.
        self.presence: list[tuple[int, int, str]] = []
        self.season = season
        self.team_id = team_id
        self.swid = swid
        self.espn_s2 = espn_s2
        self.league = league
        self.board = board_df
        self.notify = notify
        self.state = bd.DraftState(league)
        self.slot_of: dict[int, int] = {}
        self.espn_map, self.pos_map = bd.espn_maps()
        self.picks_seen = 0
        self.connected = False
        self.last_line = ""
        # Every line received since the watch started, with a receive timestamp:
        # the only timestamped pick record ESPN lets anyone keep.
        self.lines: list[tuple[int, str]] = []
        self.init_b64: str | None = None
        # Pick numbers whose as-of market snapshot this watch has written, and
        # how many writes have failed in a row (0 once one succeeds again).
        self.snapshots: list[int] = []
        self.snapshot_failures = 0
        # Set once the INIT snapshot has been applied; callers wait on this.
        self.ready = asyncio.Event()
        # `LEFT <team> <swid> 2` for our own team precedes a duplicate-connection close.
        self.bumped = False
        self.ws = None
        # Set when a SELECTED for our own team arrives after select(); carries the pick.
        self.own_pick: asyncio.Future | None = None
        # Our pick queue as ESPN last echoed it (DRAFT_LIST line); None until seen.
        self.queue: list[int] | None = None
        self.queue_echo: asyncio.Future | None = None

    # -- socket loop

    async def run(self) -> None:
        failures = 0
        while True:
            try:
                await self._session()
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.ws = None
                log.exception("draft watch session for league %s ended", self.league_id)
                if self.bumped:
                    # ESPN allows one draft-room connection per team. The user opened
                    # the room elsewhere; reconnecting would only throw them out again.
                    await self.notify(
                        "draft watch paused: the draft room was opened from another "
                        "location (your browser). Picks are not being tracked. Close the "
                        "room and call watch_draft again to resume.",
                        {"league": self.league_id, "event": "paused"})
                    return
                if self.ready.is_set():
                    failures = 0
                failures += 1
                if failures >= MAX_FAILED_SESSIONS:
                    await self.notify(
                        f"draft watch stopped after {failures} failed connections; last error "
                        f"{type(exc).__name__}: {exc}. Call watch_draft again to restart.",
                        {"league": self.league_id, "event": "stopped"})
                    raise
                await self.notify(
                    f"draft watch disconnected ({type(exc).__name__}: {exc}); "
                    f"reconnecting, attempt {failures + 1} of {MAX_FAILED_SESSIONS}",
                    {"league": self.league_id, "event": "disconnect"})

    async def _session(self) -> None:
        token = espn_live.draft_security_token(self.league_id, self.season, self.team_id,
                                               self.swid, self.espn_s2)
        uri = (f"wss://fantasydraft.espn.com/game-1/league-{self.league_id}/JOIN"
               f"?1=1&2={self.league_id}&3={self.team_id}&4={self.swid}&5={token}"
               f"&6=false&7=false&8=KONA&nocache={random.randint(0, 10**6)}")
        headers = {"Cookie": f"SWID={self.swid}; espn_s2={self.espn_s2}",
                   "Origin": "https://fantasy.espn.com"}
        self.ready.clear()
        self.bumped = False
        async with connect(uri, additional_headers=headers, user_agent_header="Mozilla/5.0",
                           open_timeout=15) as ws:
            self.connected = True
            self.ws = ws
            loop = asyncio.get_running_loop()
            next_ping = loop.time() + FIRST_PING_SECONDS
            while True:
                wait = next_ping - loop.time()
                if wait <= 0:
                    await ws.send(f"PING {int(loop.time() * 1000)}\n")
                    next_ping = loop.time() + PING_SECONDS
                    continue
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=wait)
                except TimeoutError:
                    continue
                if isinstance(msg, (bytes, bytearray)):
                    msg = msg.decode("utf-8", "replace")
                for line in msg.split("\n"):
                    line = line.strip()
                    if line:
                        await self.handle_line(line)

    # -- wire handling

    async def handle_line(self, line: str) -> None:
        self.last_line = line
        self.lines.append((int(time.time() * 1000), line))
        fields = line.split(" ")
        kind = fields[0]
        if kind == "INIT" and len(fields) > 1:
            self.init_b64 = fields[1]
            init = espn_live.decode_init(fields[1])
            self.slot_of = espn_live.slot_by_team(init)
            teams = init.league.draft_teams if init.league is not None else []
            self.online = {t.team_id: any(o is not None and o.is_online for o in t.owners)
                           for t in teams if t is not None}
            picks = espn_live.picks_from_init(init)
            self.state.reset()
            for p in picks:
                self.state.record(self._name(p["player_id"]), p["overall"],
                                  self.slot_of.get(p["team_id"]),
                                  position=bd._espn_player_position(p["player_id"], self.pos_map))
            self.picks_seen = len(picks)
            self.ready.set()
            await self._snapshot()
            s = self.state.summary()
            await self.notify(
                f"draft room joined: {s['picks_made']} picks made, pick {s['on_the_clock']} "
                f"on the clock, your next pick is {s['my_next_pick']} "
                f"({s['picks_until_my_turn']} away).",
                {"league": self.league_id, "event": "snapshot",
                 "on_the_clock": str(s["on_the_clock"])})
            if self.board is not None:
                audit = bd.audit_state(self.board, self.state)
                if not audit["ok"]:
                    await self.notify("draft audit FAILED after snapshot: "
                                      + " | ".join(audit["failures"]),
                                      {"league": self.league_id, "event": "audit_failed"})
        elif kind == "SELECTED" and len(fields) >= 3:
            team_id, pid = int(fields[1]), int(fields[2])
            overall = self.state.on_the_clock
            name = self._name(pid)
            self.state.record(name, overall, self.slot_of.get(team_id),
                              position=bd._espn_player_position(pid, self.pos_map))
            self.picks_seen += 1
            await self._snapshot()
            if team_id == self.team_id and self.own_pick and not self.own_pick.done():
                self.own_pick.set_result({"overall": overall, "player_id": pid, "name": name})
            await self._announce_pick(overall, team_id, name)
        elif kind == "SELECTING" and len(fields) >= 2:
            # ESPN naming the team that has just gone on the clock: the event the
            # as-of snapshot actually wants, rather than a state inferred after
            # SELECTED. It also arrives when the clock reopens after an UNDONE,
            # so the rewrite self-corrects. Rewriting the same pick is harmless.
            await self._snapshot()
        elif kind == "UNDONE" and len(fields) >= 2:
            keep = int(fields[1])
            self.state.picks = [p for p in self.state.picks if p["overall"] <= keep]
            self.state.save()
            # A rolled-back pick's snapshot describes a board state that no
            # longer happened; the reopened pick's is rewritten on SELECTING.
            dropped = drop_snapshots_above(self.league_id, keep)
            self.snapshots = [p for p in self.snapshots if p <= keep]
            await self.notify(f"pick {keep + 1} undone; board rolled back to {keep} picks."
                              + (f" {len(dropped)} as-of snapshots dropped." if dropped else ""),
                              {"league": self.league_id, "event": "undone"})
        elif kind == "JOINED" and len(fields) >= 3:
            team = int(fields[1])
            self.online[team] = True
            self.presence.append((int(time.time() * 1000), team, "joined"))
        elif kind == "LEFT" and len(fields) >= 4:
            team = int(fields[1])
            if (team == self.team_id and fields[2].strip("{}").upper()
                    == self.swid.strip("{}").upper() and fields[3] == "2"):
                self.bumped = True
            else:
                self.online[team] = False
                self.presence.append((int(time.time() * 1000), team, "left"))
        elif kind == "CHAT" and len(fields) >= 5:
            self.chat.append((int(fields[3]), int(fields[1]), fields[2], unquote_plus(fields[4])))
        elif kind == "DRAFT_LIST":
            self.queue = [int(f) for f in fields[1:] if f.lstrip("-").isdigit()]
            if self.queue_echo and not self.queue_echo.done():
                self.queue_echo.set_result(list(self.queue))
        elif kind == "ERROR":
            if self.own_pick and not self.own_pick.done():
                self.own_pick.set_exception(RuntimeError(line))
                return
            raise RuntimeError(line)

    async def _snapshot(self) -> None:
        """File the market for the pick now on the clock, so `<pick>.parquet` is
        the board as it stood when that pick was made.

        The board is re-read first, the way `_recommendation` does. `self.board`
        is otherwise the board the watch was *constructed* with, and refreshing
        it only inside `_recommendation` -- which runs on a handful of picks --
        would have written the same stale ADP into every file while the coverage
        block reported success. `server.watch_draft`'s refresh is a cache lookup,
        so the per-pick cost is a dict hit.

        Written after INIT (the seed), after SELECTED, and again on SELECTING,
        which is ESPN naming the team that has just gone on the clock and is
        therefore the event this wants. SELECTING also arrives when the clock
        reopens after an UNDONE, so the rewrite self-corrects. Picks before the
        watch connected have no snapshot at all, which is what
        `replay(as_of=True)` reports as coverage."""
        if self.refresh is not None:
            self.board, self.bye_weight = self.refresh()
        pick = self.state.on_the_clock
        path = write_snapshot(self.board, self.state.taken_keys(), self.league_id, pick)
        if path is None and self.board is not None:
            # Silence is the failure mode: a watch writing nothing for two hours
            # is invisible from inside the draft room. Say so once, then stay
            # quiet until it works again.
            self.snapshot_failures += 1
            if self.snapshot_failures == 1:
                await self.notify(
                    f"as-of market snapshots stopped writing at pick {pick}; the replay "
                    "will price these picks with today's numbers. The draft is unaffected.",
                    {"league": self.league_id, "event": "snapshot_failed", "pick": str(pick)})
            return
        self.snapshot_failures = 0
        if path is not None and pick not in self.snapshots:
            self.snapshots.append(pick)

    # -- queries

    def room(self, chat_limit: int = 10) -> dict:
        """Who is in the draft room and what was said, with names from the directory."""
        label = self.team_label
        online = sorted(t for t, on in self.online.items() if on)
        return {
            "connected": self.connected,
            "online": [{"team_id": t, "slot": self.slot_of.get(t), "team": label(t)} for t in online],
            "offline_count": sum(1 for on in self.online.values() if not on),
            "chat": [{"at_ms": ts, "team": label(t), "text": text}
                     for ts, t, _owner, text in self.chat[-chat_limit:]],
            "recent": [{"at_ms": ts, "team": label(t), "event": ev}
                       for ts, t, ev in self.presence[-chat_limit:]],
            "upcoming": self.upcoming(),
            # Next to picks_made, so "picks_made 122, as_of_snapshots 0" is one
            # line to read: it catches a snapshot writer that has quietly stopped.
            "as_of_snapshots": len(self.snapshots),
            "snapshot_write_failures": self.snapshot_failures,
            **self.state.summary(),
        }

    def team_label(self, team_id: int) -> str:
        d = self.directory.get(team_id, {})
        owners = ", ".join(d.get("owners") or [])
        return f"{d.get('name') or f'team {team_id}'}" + (f" ({owners})" if owners else "")

    def upcoming(self, count: int = 5) -> list[dict]:
        """The next picks in order: who is on the clock and who follows, by name,
        with whether each is in the room right now."""
        team_of_slot = {slot: team for team, slot in self.slot_of.items()}
        total = self.league.teams * self.league.rounds
        out = []
        for overall in range(self.state.on_the_clock, min(total, self.state.on_the_clock + count - 1) + 1):
            slot = self.state.slot_for_pick(overall)
            team = team_of_slot.get(slot)
            out.append({"pick": overall, "slot": slot, "team_id": team,
                        "team": self.team_label(team) if team is not None else f"slot {slot}",
                        "online": bool(self.online.get(team)) if team is not None else None,
                        "mine": slot == self.state.my_slot})
        return out

    # -- actions

    async def set_queue(self, player_ids: list[int], timeout: float = 10.0) -> list[int]:
        """Replace our pick queue: `DRAFT_LIST id id ...` (an empty list clears it),
        exactly what the room sends for add, remove and reorder. Returns the list
        ESPN echoes back."""
        if self.ws is None:
            raise RuntimeError("draft watch is not connected")
        self.queue_echo = asyncio.get_running_loop().create_future()
        await self.ws.send("DRAFT_LIST" + "".join(f" {pid}" for pid in player_ids) + "\n")
        try:
            return await asyncio.wait_for(self.queue_echo, timeout=timeout)
        finally:
            self.queue_echo = None

    async def select(self, player_id: int, timeout: float = 10.0) -> dict:
        """Make our pick: `SELECT <playerId>`, then wait for the server's SELECTED
        for our team (or its ERROR line). Only valid while connected and on the clock."""
        if self.ws is None:
            raise RuntimeError("draft watch is not connected")
        self.own_pick = asyncio.get_running_loop().create_future()
        await self.ws.send(f"SELECT {player_id}\n")
        try:
            return await asyncio.wait_for(self.own_pick, timeout=timeout)
        finally:
            self.own_pick = None

    async def _announce_pick(self, overall: int, team_id: int, name: str) -> None:
        s = self.state.summary()
        until = s["picks_until_my_turn"]
        mine = self.slot_of.get(team_id) == self.state.my_slot
        text = (f"pick {overall}: {'you' if mine else f'team {team_id}'} took {name}. "
                f"Pick {s['on_the_clock']} on the clock; your turn in {until}.")
        if until <= RECOMMEND_WITHIN and not mine:
            text += " " + self._recommendation()
        await self.notify(text, {"league": self.league_id, "event": "pick",
                                 "pick": str(overall), "picks_until_my_turn": str(until)})

    def _recommendation(self) -> str:
        if self.refresh is not None:
            self.board, self.bye_weight = self.refresh()
        if self.board is None:
            return "No recommendation: no board."
        b = self.board.copy()
        b["drafted"] = b["_key"].isin(self.state.taken_keys())
        nxt = self.state.next_pick_for_me()
        if nxt is None:
            return "No recommendation: you have no picks left."
        after = self.state.pick_after_next()
        roster = self.state.my_roster(b)
        from .replay import room_drift

        recs = model.recommend(b, self.league, current_pick=nxt, next_pick=after,
                               roster=roster, top_n=3, mine=self.state.my_rows(b),
                               bye_weight=self.bye_weight,
                               adp_shift=room_drift(b, self.state)["shift"])
        if recs.empty:
            return "No recommendation: board empty."
        names = [f"{r['name']} ({r['position']}, {float(r['p_available_next']):.0%} lasts"
                 + (f", bye stacks with {r['bye_conflicts']}" if r.get("bye_conflicts") else "")
                 + ")" for _, r in recs.iterrows()]
        return "Recommend: " + "; then ".join(names) + "."

    def _name(self, pid: int) -> str:
        """Board spelling when the board knows the player, else the crosswalk's.
        Recording the board's spelling keeps taken_keys aligned with the board
        the way sync_draft does, whatever the two sources call him."""
        raw = bd._espn_player_name(pid, self.espn_map)
        if self.board is None or raw.startswith("ESPN#") or raw.endswith(" D/ST"):
            return raw
        row = bd.match_player(raw, self.board)
        return row["name"] if row is not None else raw


