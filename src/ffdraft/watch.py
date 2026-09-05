"""Hold the ESPN draft room socket open and push each pick to the MCP client.

Claude Code delivers `notifications/claude/channel` events into the session as
`<channel>` messages (docs/data-sources.md, "ESPN live draft socket"). The loop
here owns the socket; `server.watch_draft` supplies the notify callable.
"""
from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable

from websockets.asyncio.client import connect

from . import board as bd
from . import espn_live, model
from .config import LeagueSettings

Notify = Callable[[str, dict[str, str]], Awaitable[None]]

PING_SECONDS = 30.0
# Consecutive failed sessions before the watch gives up. Each attempt is a real
# connect bounded by open_timeout; a session that reaches INIT resets the count.
MAX_FAILED_SESSIONS = 5
# Push a recommendation with the pick event once the user is this close to the clock.
RECOMMEND_WITHIN = 3


class DraftWatch:
    def __init__(self, league_id: str, season: int, team_id: int, swid: str, espn_s2: str,
                 league: LeagueSettings, board_df, notify: Notify) -> None:
        self.league_id = league_id
        self.season = season
        self.team_id = team_id
        self.swid = swid
        self.espn_s2 = espn_s2
        self.league = league
        self.board = board_df
        self.notify = notify
        self.state = bd.DraftState(league)
        self.slot_of: dict[int, int] = {}
        self.espn_map = _espn_name_map()
        self.picks_seen = 0
        self.connected = False
        self.last_line = ""
        # Set once the INIT snapshot has been applied; callers wait on this.
        self.ready = asyncio.Event()
        # `LEFT <team> <swid> 2` for our own team precedes a duplicate-connection close.
        self.bumped = False

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
            while True:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=PING_SECONDS)
                except TimeoutError:
                    await ws.send(f"PING {random.randint(0, 10**9)}\n")
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
        fields = line.split(" ")
        kind = fields[0]
        if kind == "INIT" and len(fields) > 1:
            init = espn_live.decode_init(fields[1])
            self.slot_of = espn_live.slot_by_team(init)
            picks = espn_live.picks_from_init(init)
            self.state.reset()
            for p in picks:
                self.state.record(self._name(p["player_id"]), p["overall"],
                                  self.slot_of.get(p["team_id"]))
            self.picks_seen = len(picks)
            self.ready.set()
            s = self.state.summary()
            await self.notify(
                f"draft room joined: {s['picks_made']} picks made, pick {s['on_the_clock']} "
                f"on the clock, your next pick is {s['my_next_pick']} "
                f"({s['picks_until_my_turn']} away).",
                {"league": self.league_id, "event": "snapshot",
                 "on_the_clock": str(s["on_the_clock"])})
        elif kind == "SELECTED" and len(fields) >= 3:
            team_id, pid = int(fields[1]), int(fields[2])
            overall = self.state.on_the_clock
            name = self._name(pid)
            self.state.record(name, overall, self.slot_of.get(team_id))
            self.picks_seen += 1
            await self._announce_pick(overall, team_id, name)
        elif kind == "UNDONE" and len(fields) >= 2:
            keep = int(fields[1])
            self.state.picks = [p for p in self.state.picks if p["overall"] <= keep]
            self.state.save()
            await self.notify(f"pick {keep + 1} undone; board rolled back to {keep} picks.",
                              {"league": self.league_id, "event": "undone"})
        elif kind == "LEFT" and len(fields) >= 4:
            if (int(fields[1]) == self.team_id and fields[2].strip("{}").upper()
                    == self.swid.strip("{}").upper() and fields[3] == "2"):
                self.bumped = True
        elif kind == "ERROR":
            raise RuntimeError(line)

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
        b = self.board.copy()
        b["drafted"] = b["_key"].isin(self.state.taken_keys())
        nxt = self.state.next_pick_for_me()
        after = self.state.pick_after_next()
        roster = self.state.my_roster(b)
        recs = model.recommend(b, self.league, current_pick=nxt, next_pick=after,
                               roster=roster, top_n=3)
        if recs.empty:
            return "No recommendation: board empty."
        names = [f"{r['name']} ({r['position']}, {float(r['p_available_next']):.0%} lasts)"
                 for _, r in recs.iterrows()]
        return "Recommend: " + "; then ".join(names) + "."

    def _name(self, pid: int) -> str:
        return bd._espn_player_name(pid, self.espn_map)


def _espn_name_map() -> dict:
    x = bd._id_crosswalk()
    return x.dropna(subset=["espn_id"]).set_index("espn_id")["full_name"].to_dict()
