"""MCP server: a live fantasy football draft analyst.

Run with:  python -m ffdraft.server
"""
from __future__ import annotations

import copy
import json
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

try:  # mcp SDK >= 2.0
    from mcp.server.mcpserver import Context, MCPServer

    class _Server(MCPServer):
        """MCPServer that also declares the Claude Code channel capability.

        MCPServer builds its initialization options without experimental
        capabilities, and `claude/channel` must be present at initialize for
        Claude Code to accept `notifications/claude/channel` pushes from
        `watch_draft`. The session must be started with
        `claude --dangerously-load-development-channels server:<name>`.
        """

        CHANNEL_CAPS = {"claude/channel": {}}

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            # The 2026-07-28 `server/discover` probe derives capabilities from server
            # state and ignores initialization options, so declare there as well.
            from mcp import types as _types

            low = self._lowlevel_server
            default_discover = low._handle_discover

            async def discover(ctx, params):
                result = await default_discover(ctx, params)
                result.capabilities.experimental = dict(self.CHANNEL_CAPS)
                return result

            low.add_request_handler("server/discover", _types.RequestParams, discover)

        async def run_stdio_async(self) -> None:
            import asyncio as _asyncio

            from mcp.server.lowlevel.server import NotificationOptions
            from mcp.server.stdio import stdio_server

            # Started as a task, not awaited, so a slow ESPN join never delays the
            # handshake. The point of resuming is that the socket is live again
            # within seconds of the process starting, which does not require the
            # client to have finished connecting.
            _asyncio.create_task(resume_watches(), name="ffdraft-resume-watches")
            async with stdio_server() as (read_stream, write_stream):
                await self._lowlevel_server.run(
                    read_stream, write_stream,
                    self._lowlevel_server.create_initialization_options(
                        # tools.listChanged: reload_code re-registers tools and
                        # sends notifications/tools/list_changed; Claude Code
                        # refreshes the tool list without a reconnect.
                        notification_options=NotificationOptions(tools_changed=True),
                        experimental_capabilities=dict(self.CHANNEL_CAPS)),
                )
except ImportError:  # mcp SDK 1.x
    from mcp.server.fastmcp import Context  # ty: ignore[unresolved-import]
    from mcp.server.fastmcp import FastMCP as _Server  # ty: ignore[unresolved-import]

from . import adp as adp_mod
from . import board as bd
from . import espn_live, features, model, names, sources, trade, watchstore
from .config import (
    CURRENT_SEASON,
    DATA_DIR,
    SPECIAL_POSITIONS,
    STATE_DIR,
    LeagueSettings,
    ModelWeights,
    Scoring,
    delete_league,
    load_settings,
    save_settings,
    set_active,
)
from .config import list_leagues as cfg_list_leagues

mcp = _Server(
    "fantasy-draft-analyst",
    instructions=(
        "While watch_draft is running, this server pushes ESPN draft-room events as "
        "channel messages: one per pick, with a recommendation once the user is within a "
        "few picks of the clock. On a pick event, tell the user in one line who was taken "
        "and how far away their turn is; when the message carries a recommendation, "
        "relay it. Call who_should_i_pick for the full reasoning when the user is on the "
        "clock. Do not call sync_draft while a watch is running; the watch keeps the "
        "board current."
    ),
)
# Process state survives reload_code: importlib.reload re-executes this module
# in the same namespace, and a running draft watch must not be dropped by a
# code reload. `globals().get` keeps the existing objects on a re-execution.
# Background draft-room watchers keyed by league id (see watch.py).
_WATCHES: dict[str, Any] = globals().get("_WATCHES", {})

_CACHE: dict[str, Any] = globals().get("_CACHE", {"league": None, "weights": None, "adp_csv": {}})
# Boards are keyed by the settings that actually change them, so a 10-team full-PPR
# league and a 13-team half-PPR league each keep their own and switching between
# them is instant rather than an eight-second rebuild.
_BOARDS: dict[str, pd.DataFrame] = globals().get("_BOARDS", {})


def _scoring_label(league: LeagueSettings) -> str:
    """ppr / half_ppr / standard. Anything unusual is treated as standard, which is
    the conservative choice: it assumes no reception credit rather than inventing one."""
    r = float(league.scoring.rec)
    if r >= 0.9:
        return "ppr"
    if r >= 0.35:
        return "half_ppr"
    return "standard"


def _board_path(league: LeagueSettings) -> Path:
    return DATA_DIR / f"board_{league.cache_key()}.parquet"


# ---------------------------------------------------------------- internals

def _settings() -> tuple[LeagueSettings, ModelWeights]:
    if _CACHE["league"] is None:
        _CACHE["league"], _CACHE["weights"] = load_settings()
    return _CACHE["league"], _CACHE["weights"]


def _build_board(force: bool = False) -> pd.DataFrame:
    league, weights = _settings()
    key = league.cache_key()
    path = _board_path(league)

    if not force and key in _BOARDS:
        return _BOARDS[key]
    if not force and path.exists():
        b = bd.rekey(pd.read_parquet(path))
        changed = False
        if "bye_week" not in b.columns:
            b = _attach_byes(b)
            changed = True
        if not {"carry_share", "role_entropy", "entropy_basis",
                "contingent_points"} <= set(b.columns):
            b = _attach_roles(b, league)
            changed = True
        # A board priced off consensus before ESPN ADP was configured, or keyed
        # by an older normaliser, is repriced in place: projections stay, the
        # market columns are joined again.
        stale_key = int(b["key_version"].iloc[0]) != names.KEY_VERSION \
            if "key_version" in b.columns and len(b) else True
        # The market columns were derived by rules that may since have changed
        # (attach_adp's join), which nothing else here would notice: a board
        # cached by an older join still has adp_match and espn rows on every
        # row, so every clause below passes and the stale prices survive.
        stale_join = int(b["market_join_version"].iloc[0]) != bd.MARKET_JOIN_VERSION \
            if "market_join_version" in b.columns and len(b) else True
        repriced = bd.espn_adp_configured() and "adp_source" in b.columns and (
            not (b["adp_source"] == "espn").any()
            or "espn_rank" not in b.columns
            or "adp_match" not in b.columns)
        if stale_key or stale_join or repriced:
            b = _price_board(bd.strip_adp(b), league, weights)
            changed = True
        if changed:
            b.to_parquet(path, index=False)
        _BOARDS[key] = b
        return b

    tbl = model.build_player_table(league, weights)
    proj = model.project(tbl, league, weights)
    proj = _price_board(_attach_byes(proj), league, weights)
    proj = _attach_roles(proj, league)
    proj.to_parquet(path, index=False)
    _BOARDS[key] = proj
    return proj


def _price_board(proj: pd.DataFrame, league: LeagueSettings,
                 weights: ModelWeights | None = None) -> pd.DataFrame:
    try:
        adp = bd.load_adp(
            csv_path=(_CACHE["adp_csv"] or {}).get(league.name),
            superflex=bool(getattr(league, "superflex", 0)),
        )
    except Exception as exc:
        print(f"ADP unavailable ({type(exc).__name__}); using model rank as proxy")
        adp = None
    # A reprice starts from whatever is cached, which may already carry the K and
    # D/ST rows added below; drop them and rebuild from the list just fetched.
    proj = proj[~proj["position"].isin(SPECIAL_POSITIONS)] if "position" in proj.columns \
        else proj
    proj = bd.attach_adp(proj, adp)
    proj["key_version"] = names.KEY_VERSION
    priced = bd.convert_adp_format(proj, _scoring_label(league))
    return _add_special_teams(priced, adp, league, weights or _settings()[1])


def _add_special_teams(board: pd.DataFrame, adp: pd.DataFrame | None,
                       league: LeagueSettings, weights: ModelWeights) -> pd.DataFrame:
    """Append the ESPN-projected kickers and defenses to a priced board.

    They join here rather than in `model.build_player_table` because they have
    no modelled features at all: nothing upstream of `project()` has a row for
    them, and adding an empty one would push a NaN through every multiplier.
    They are scored against the board only after the board exists.

    `overall_rank` and `adp_delta` are re-derived over the combined board. A
    kicker that outranks two hundred players has to be in that ranking or
    `value_picks` and `adp_delta` would be reading a board that no longer
    matches the one the recommender uses.
    """
    special = bd.espn_special_teams(adp)
    if special.empty:
        return board
    special = model.score_special_teams(special, board, league, weights)
    special["bye_week"] = special["team"].map(features.team_bye_weeks(CURRENT_SEASON))
    for col in ("key_version", "market_join_version", "adp_format"):
        if col in board.columns and len(board):
            special[col] = board[col].iloc[0]
    # Appending rows that have no value for a boolean flag widens the column to
    # object, and pandas refuses to mask with an object column holding None --
    # `b[b["is_rookie"]]` raises for every caller, not just the new rows. Any
    # flag the appended rows do not carry is False for them by construction:
    # they are not rookies, not off a depth chart, not drafted yet.
    flags = {c for c in board.columns if board[c].dtype == bool}
    flags.update({"is_rookie", "off_roster"} & set(board.columns))
    out = pd.concat([board, special], ignore_index=True)
    for col in flags:
        out[col] = out[col].fillna(False).astype(bool)
    out["overall_rank"] = out["draft_score"].rank(ascending=False, method="min").astype(int)
    out["adp_delta"] = out["adp"] - out["overall_rank"]
    return out.sort_values("draft_score", ascending=False).reset_index(drop=True)


def _attach_byes(b: pd.DataFrame) -> pd.DataFrame:
    b = b.copy()
    b["bye_week"] = b["team"].map(features.team_bye_weeks(CURRENT_SEASON))
    return b


def _attach_roles(b: pd.DataFrame, league: LeagueSettings) -> pd.DataFrame:
    """The named opportunity shares and the role-entropy score.

    Both are read-only columns: nothing in `pick_value` depends on them unless
    `model_settings` opens a `roles.py` weight. Entropy needs `espn_proj`, so
    this runs after the board is priced.
    """
    from . import roles

    try:
        b = roles.attach_opportunity(b, league.scoring, te_bonus=league.te_premium_bonus)
    except Exception as exc:
        print(f"opportunity shares unavailable ({type(exc).__name__}: {exc})")
    try:
        b = roles.attach_role_entropy(b)
    except Exception as exc:
        print(f"role entropy unavailable ({type(exc).__name__}: {exc})")
    # Depth charts have to be read off the whole board: an available pool would
    # promote a backup to starter the moment the starter is drafted.
    return roles.attach_handcuffs(b)


def _state() -> bd.DraftState:
    league, _ = _settings()
    return bd.DraftState(league)


def _mark_drafted(b: pd.DataFrame, state: bd.DraftState) -> pd.DataFrame:
    b = b.copy()
    b["drafted"] = b["_key"].isin(state.taken_keys())
    return b


def _jsonable(value: Any) -> Any:
    """Replace NaN with null anywhere in a hand-built payload.

    NaN is not JSON. Python's own module writes it as a bare `NaN` literal,
    which its own parser reads back and every conforming client rejects, so the
    failure is invisible from inside the process and total from outside it.
    `who_should_i_pick` emitted `"espn_injury": NaN` and became unparseable the
    moment a team defense reached the list — ESPN files no injury status for a
    defense, and NaN is truthy, so no `or`-guard catches it. `_rows` already
    does this for table output; hand-built dicts had nothing.
    """
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    if value is None or isinstance(value, (str, int)):
        return value
    try:
        return None if pd.isna(value) else value
    except (TypeError, ValueError):
        return value


# A tool result over the client's limit is not shown truncated -- it is not
# shown at all, so an answer that overruns is unreadable rather than verbose.
# `stream_kdst(week=1)` came back at 69,512 characters and the user could not
# read the tool (#52). 20,000 sits under the limit with room for whatever the
# client wraps around the payload, which we do not control and cannot measure
# from here.
PAYLOAD_LIMIT = 20_000


def _longest_list(payload: Any) -> tuple[str, Any, Any, list] | None:
    """The list whose serialised form is longest, with the container holding it.

    Lists are what grow: every payload in this module that has ever been too
    big was too big because a table had a row per player. Trimming the longest
    one keeps the head of a ranking, which is the part an answer is about,
    where dropping a whole key would keep the footnotes and lose the answer.
    """
    best: tuple[int, str, Any, Any, list] | None = None

    def walk(node: Any, holder: Any, key: Any, path: str) -> None:
        nonlocal best
        if isinstance(node, list):
            # `holder is None` only at the root, which has nothing to assign
            # back into. `_under_the_cap` wraps a bare list before it gets here,
            # so this is the belt to that braces (lena).
            if len(node) > 1 and holder is not None:
                size = len(json.dumps(node, default=str))
                if best is None or size > best[0]:
                    best = (size, path, holder, key, node)
            for i, item in enumerate(node):
                walk(item, node, i, f"{path}[{i}]")
        elif isinstance(node, dict):
            for k, v in node.items():
                walk(v, node, k, f"{path}.{k}" if path else str(k))

    walk(payload, None, None, "")
    return None if best is None else (best[1], best[2], best[3], best[4])


def _under_the_cap(payload: Any, dumps_kwargs: dict) -> str:
    """Trim the longest lists until the answer fits, and say what was trimmed.

    The backstop, not the plan. Every tool here is expected to shape its own
    answer to a size a person can read -- `stream_kdst` ranks a top few rather
    than the field -- and this exists because no amount of shaping can promise a
    bound when the data behind it can always grow. Enforced at the one exit so a
    new handler inherits the guarantee instead of being trusted to remember it.

    What comes back is still valid JSON and still the head of every table, with
    `truncated` naming each path and how much of it survived. A payload that
    cannot be made to fit even with every list at one row is reported as that
    rather than as a mangled answer.

    THE NOTE IS INSIDE THE MEASUREMENT. It used to be added after the size check
    and returned without re-checking, so the sentence explaining the trim could
    push the answer back over the limit and this returned it anyway -- the cap
    failing in the case it exists for. It is not a narrow window: the note costs
    230 to 400 characters at `indent=2`, and because each pass halves exactly one
    list, a payload of many small tables descends in small steps and tends to
    stop just under the line, which is precisely where it lands. Found by lena,
    reproduced at 21,064 characters against a 20,000 cap on 40 tables of 30 rows.

    There is no pass counter. Every pass strictly shrinks one list and a list
    drops out at length one, so the loop is monotone and cannot cycle;
    `_longest_list` returning None is the real terminating condition. A count of
    400 was not a safety net but a second exit, and it fell through into the
    message below -- which then said "even with every table cut to one row" about
    a payload that was still shrinking. An honest-looking sentence about a state
    the code never reached is the failure this whole file exists to avoid.
    """
    trimmed = copy.deepcopy(payload)
    if not isinstance(trimmed, dict):
        # A bare array has nowhere to carry the note that it was cut, and
        # cutting it silently is the one thing this must not do. Wrapping is a
        # visible change of shape, which is the point: no handler emits a
        # top-level list today, and one that starts to will see this rather
        # than the `TypeError` the old code raised from inside the one exit.
        trimmed = {"items": trimmed}
    original: dict[str, int] = {}
    kept: dict[str, int] = {}
    while True:
        text = json.dumps(_with_the_note(trimmed, original, kept), **dumps_kwargs)
        if len(text) <= PAYLOAD_LIMIT:
            return text
        found = _longest_list(trimmed)
        if found is None:
            return json.dumps({"error": f"this answer does not fit the client's "
                                        f"{PAYLOAD_LIMIT:,}-character limit even "
                                        f"with every table cut to one row",
                               "limit": PAYLOAD_LIMIT}, **dumps_kwargs)
        path, holder, key, rows = found
        original.setdefault(path, len(rows))
        keep = max(1, len(rows) // 2)
        kept[path] = keep
        holder[key] = rows[:keep]


def _with_the_note(trimmed: dict, original: dict[str, int],
                   kept: dict[str, int]) -> dict:
    """`trimmed` with the note that says what was cut, ready to be measured.

    A shallow copy, so the note never reaches `_longest_list` and cannot become
    the table this trims next. An answer that already has a `truncated` key of
    its own keeps it, under a name that says which one is which -- overwriting a
    tool's own field to report on the tool would be its own small lie.
    """
    if not kept:
        return trimmed
    note = {
        "note": f"cut to fit the client's {PAYLOAD_LIMIT:,}-character limit, "
                f"longest tables first; ask for fewer weeks or positions to see "
                f"a full one",
        "paths": {p: f"{kept[p]} of {original[p]}" for p in sorted(kept)},
    }
    out = dict(trimmed)
    if "truncated" in out:
        out["truncated_before_the_cap"] = out["truncated"]
    out["truncated"] = note
    return out


def _emit(payload: Any, **dumps_kwargs: Any) -> str:
    """The single JSON exit for every tool in this module.

    `_jsonable` existed and guarded exactly one of the 83 `json.dumps` calls
    here, which is the shape of the bug rather than a fix for it: the one
    payload someone had already been burned by. Every other handler that builds
    a dict from board values could still emit a bare `NaN`, and a payload is
    only ever one new column away from carrying one.

    `default=str` cannot stand in for this. `json.dumps` writes a float NaN
    itself and never consults `default`, so the sites that pass it were no
    safer than the sites that do not.

    Keyword arguments pass through untouched, so `indent` and `default` still
    mean what they meant at each call site; the only change is that the payload
    is sanitised first. `TestEveryPayloadLeavesThroughEmit` is what keeps a new
    handler from going around it.

    It is also where the size cap is enforced, for the same reason: a client
    that refuses an oversized result shows the user nothing, so every tool needs
    the bound and no tool should be trusted to remember it. `_under_the_cap`
    says what it cut.
    """
    safe = _jsonable(payload)
    text = json.dumps(safe, **dumps_kwargs)
    if len(text) <= PAYLOAD_LIMIT:
        return text
    return _under_the_cap(safe, dumps_kwargs)


def _rows(df: pd.DataFrame, cols: list[str], n: int) -> list[dict]:
    out = []
    for _, r in df.head(n).iterrows():
        d = {}
        for c in cols:
            v = r.get(c)
            if isinstance(v, (np.floating, float)):
                v = None if not np.isfinite(v) else round(float(v), 3)
            elif isinstance(v, (np.integer,)):
                v = int(v)
            elif isinstance(v, (np.bool_,)):
                v = bool(v)
            d[c] = v
        out.append(d)
    return out


# ---------------------------------------------------------------- tools

@mcp.tool()
def configure_league(name: str = "default", teams: int = 12, draft_slot: int = 6,
                     rounds: int = 16, scoring: str = "half_ppr", snake: bool = True,
                     qb: int = 1, rb: int = 2, wr: int = 2, te: int = 1, flex: int = 1,
                     superflex: int = 0, te_premium_bonus: float = 0.0,
                     consistency_weight: float = 0.35,
                     adp_csv_path: str | None = None) -> str:
    """Create or update a league, and make it the active one.

    Give each league a name and you can keep as many as you like side by side —
    a 10-team full PPR and a 13-team half PPR hold separate boards, separate
    replacement levels and separate in-progress drafts.

    scoring: ppr, half_ppr, or standard. Use superflex=1 for a second QB-eligible
    slot, and te_premium_bonus for extra points per tight end reception.
    consistency_weight trades expected points against week-to-week reliability
    (0 = pure upside, 1 = pure floor).
    """
    if not 1 <= draft_slot <= teams:
        return _emit({"error": f"draft_slot {draft_slot} is outside a {teams}-team league"})

    starters = {"QB": qb, "RB": rb, "WR": wr, "TE": te, "FLEX": flex, "K": 1, "DST": 1}
    league = LeagueSettings(
        name=name, teams=teams, rounds=rounds, draft_slot=draft_slot, snake=snake,
        scoring=Scoring.preset(scoring), starters=starters,
        superflex=superflex, te_premium_bonus=te_premium_bonus,
    )
    weights = ModelWeights(consistency_weight=consistency_weight)
    save_settings(league, weights)

    csvs = dict(_CACHE.get("adp_csv") or {})
    if adp_csv_path:
        csvs[name] = adp_csv_path
    _CACHE.update({"league": league, "weights": weights, "adp_csv": csvs})

    known, _ = cfg_list_leagues()
    reused = _board_path(league).exists()
    return _emit({
        "league": name, "active": True, "teams": teams, "your_slot": draft_slot,
        "scoring": scoring, "superflex": superflex,
        "your_picks": league.picks_for_slot()[:rounds],
        "replacement_levels": league.replacement_ranks(),
        "all_leagues": known,
        "board": "already cached for these settings" if reused
                 else "will build on your next query",
    }, indent=2)


@mcp.tool()
def list_leagues() -> str:
    """Every league you've set up, and which one is active."""
    known, active = cfg_list_leagues()
    out = []
    for nm in known:
        lg, _ = load_settings(nm)
        state = bd.DraftState(lg)
        out.append({
            "name": nm, "active": nm == active, "teams": lg.teams,
            "scoring": ("ppr" if lg.scoring.rec >= 1 else
                        "standard" if lg.scoring.rec == 0 else "half_ppr"),
            "your_slot": lg.draft_slot, "superflex": lg.superflex,
            "picks_recorded": len(state.picks),
        })
    return _emit({"active": active, "leagues": out}, indent=2)


@mcp.tool()
def switch_league(name: str) -> str:
    """Make a different league active. Its board and draft resume where you left them."""
    if not set_active(name):
        known, _ = cfg_list_leagues()
        return _emit({"error": f"no league named '{name}'", "available": known})
    league, weights = load_settings(name)
    _CACHE.update({"league": league, "weights": weights})
    state = bd.DraftState(league)
    return _emit({
        "active": name, "teams": league.teams, "your_slot": league.draft_slot,
        "scoring": ("ppr" if league.scoring.rec >= 1 else
                    "standard" if league.scoring.rec == 0 else "half_ppr"),
        "board": "cached" if _board_path(league).exists() else "will build on next query",
        **state.summary(),
    }, indent=2)


@mcp.tool()
def remove_league(name: str) -> str:
    """Delete a league and its draft history. The board cache is left alone, since
    other leagues with the same format may share it."""
    if not delete_league(name):
        known, _ = cfg_list_leagues()
        return _emit({"error": f"no league named '{name}'", "available": known})
    p = STATE_DIR / f"draft_{re.sub(r'[^A-Za-z0-9_-]', '_', name)}.json"
    if p.exists():
        p.unlink()
    if (_CACHE.get("league") or LeagueSettings()).name == name:
        _CACHE.update({"league": None, "weights": None})
    known, active = cfg_list_leagues()
    return _emit({"removed": name, "remaining": known, "active": active}, indent=2)


@mcp.tool()
def refresh_data(force_download: bool = False) -> str:
    """Rebuild the player board from source data. Run once before draft day."""
    if force_download:
        for p in sources.CACHE_DIR.glob("*.parquet"):
            p.unlink()
    sources.clear_memory_cache()
    features.clear_derived_cache()
    _BOARDS.clear()
    b = _build_board(force=True)
    return _emit({
        "players_modelled": len(b),
        "by_position": b["position"].value_counts().to_dict(),
        "seasons": sorted(int(s) for s in sources.weekly_stats()["season"].unique()),
        "datasets_cached": sources.cache_status(),
        "top_10": _rows(b, ["name", "position", "team", "proj_points", "consistency", "adp"], 10),
    }, indent=2, default=str)


@mcp.tool()
def best_available(position: str | None = None, limit: int = 15,
                   sort_by: str = "draft_score") -> str:
    """The next best players still on the board.

    sort_by: draft_score (balanced), vor (raw value), consistency (floor),
    proj_points, or value (biggest gap between ADP and model rank).
    """
    b = _mark_drafted(_build_board(), _state())
    avail = b[~b["drafted"]]
    if position:
        avail = avail[avail["position"] == position.upper()]
    key = {"value": "adp_delta"}.get(sort_by, sort_by)
    if key not in avail.columns:
        key = "draft_score"
    avail = avail.sort_values(key, ascending=False)
    cols = ["name", "position", "team", "bye_week", "overall_rank", "pos_rank", "adp",
            "adp_delta", "proj_points", "adj_ppg", "consistency", "startable_rate",
            "injury_risk", "vor"]
    return _emit({"sorted_by": key, "players": _rows(avail, cols, limit)}, indent=2)


@mcp.tool()
def who_should_i_pick(limit: int = 6) -> str:
    """The live draft-analyst call: who to take right now, and why.

    Weighs projected value, week-to-week consistency, your roster's open starting
    slots, and the odds each player survives to your next pick.
    """
    league, _ = _settings()
    state = _state()
    b = _mark_drafted(_build_board(), state)
    nxt = state.next_pick_for_me()
    on_clock = state.on_the_clock
    if nxt is not None and nxt > on_clock:
        current = nxt  # you're not up yet; evaluate for your actual next pick
    else:
        current = on_clock
    after = state.pick_after_next() if nxt == current else nxt

    roster = state.my_roster(b)
    _, weights = _settings()
    from .replay import room_drift

    drift = room_drift(b, state)
    recs = model.recommend(b, league, current_pick=current, next_pick=after,
                           roster=roster, top_n=limit, mine=state.my_rows(b),
                           bye_weight=weights.bye, adp_shift=drift["shift"],
                           room_picks=state.picks_by_position(b),
                           picks_so_far=len(state.picks),
                           room_held=state.held_by_slot(b))

    # Roster-dependent, so `player_report` cannot show them: what each candidate
    # is worth to *this* roster rather than to an average one. Reported, not
    # priced — the weights behind them are 0 (see `roles.py` and CHANGELOG.md).
    from . import roles

    mine = state.my_rows(b)
    bench = roles.bench_values(recs, league, mine)
    # The two halves of the model can still disagree about the same roster.
    # `roster` counts recorded picks by position; `mine` is the board rows
    # behind them. `need_mult` sees the counted total and treats the position as
    # filled, while `roles.bench_values` sees only the priced rows and treats
    # the slot as open.
    #
    # What splits them has moved. It was a pick the board could not price at all
    # -- one RB on the live record, three counted and two priced -- and #40
    # closed that: `my_rows` now stands such a pick in at replacement level, so
    # the two counts agree. What survives is narrower and stranger: a board row
    # that exists but carries no position. `my_roster` falls back to the
    # recorded position when the board's is blank, `my_rows` keeps the row as it
    # stands, so the pick is counted at RB and priced at nothing. That is a
    # malformed board row rather than an unmodelled player, and it is worth
    # saying out loud for that reason -- it means the board is wrong about
    # someone, not merely silent about him.
    #
    # It is not why an RB headlined, and an earlier version of this comment said
    # it was. `who_should_i_pick` passes no `role_weights`, so the bench numbers
    # are reported and never priced; and the one path by which the count does
    # reach the ranking runs the other way -- `need_mult` for RB is 0.518 on the
    # counted roster against 0.720 on the priced one, so counting the unpriced
    # back suppresses the position. What this note affects is what the user is
    # told about bench value, not what is recommended.
    priced = mine["position"].astype(str).value_counts().to_dict() if len(mine) else {}
    thin = {pos: (n, int(priced.get(pos, 0))) for pos, n in roster.items()
            if n > int(priced.get(pos, 0))}
    picks = []
    # Held as typed locals rather than read back out of the answer, whose values
    # are heterogeneous.
    head: tuple[str, str, float | None, float | None] | None = None
    for idx, r in recs.iterrows():
        bye = r.get("bye_week")
        pos = str(r["position"])
        survival = float(r["p_available_next"]) if pd.notna(r.get("p_available_next")) else None
        marginal = float(r["marginal_value"]) if pd.notna(r.get("marginal_value")) else None
        why = model.explain(r)
        if head is None:
            head = (str(r["name"]), why, survival, marginal)
        picks.append({
            # Every string field here is guarded on notna, not on truthiness:
            # NaN is truthy and `json.dumps` writes it as a bare NaN literal,
            # which is not JSON. ESPN files no injury status at all for a team
            # defense, and no team for a player it does not carry.
            "player": r["name"], "position": r["position"],
            "team": str(r["team"]) if pd.notna(r.get("team")) else None,
            "adp": round(float(r["adp"]), 1),
            "proj_points": round(float(r["proj_points"]), 1),
            "espn_proj": (round(float(r["espn_proj"]), 1)
                          if pd.notna(r.get("espn_proj")) else None),
            "espn_injury": (str(r["espn_injury"])
                            if pd.notna(r.get("espn_injury")) else None),
            "consistency": round(float(r["consistency"]), 3),
            # The four numbers the recommendation is actually made of, so the
            # reader never has to infer the comparison from a rank. `value_now`
            # is what taking him is worth over replacement;
            # `expected_best_at_next_pick` is what the position is expected to
            # still offer at your next turn; the difference between them is what
            # taking now buys, and it is negative whenever waiting is better.
            "value_now": round(float(r["draft_score"]), 1),
            "expected_best_at_next_pick": round(float(r["fallback_value"]), 1),
            "marginal_now_vs_wait": (round(marginal, 1) if marginal is not None else None),
            "survival": (round(survival, 2) if survival is not None else None),
            "why_now": model.urgency_note(survival, marginal, after),
            # Only when the counted roster is thicker than the priced one at
            # this candidate's position (#40).
            "roster_slot_note": (
                f"your {pos} count is {thin[pos][0]} but the board prices only "
                f"{thin[pos][1]} of them, so this is scored against a thinner "
                f"roster than the count suggests" if pos in thin else None),
            "survives_to_next_pick": round(float(r["p_available_next"]), 2),
            "starts_in_a_given_week": round(float(bench.at[idx, "p_start"]), 2),
            "bench_value": round(float(bench.at[idx, "bench_value"]), 1),
            "handcuff_for": r.get("starter") if pd.notna(r.get("starter")) else None,
            "contingent_points": (round(float(r["contingent_points"]), 1)
                                  if pd.notna(r.get("contingent_points")) else None),
            "role_entropy": (round(float(r["role_entropy"]), 2)
                             if pd.notna(r.get("role_entropy")) else None),
            # The two halves are reported beside the blend, and `entropy_basis`
            # names which of them this row's score rests on: only the churn half
            # has been tested against real projection error.
            "entropy_basis": (str(r["entropy_basis"]) or None
                              if pd.notna(r.get("entropy_basis")) else None),
            "proj_disagreement": (round(float(r["proj_disagreement"]), 2)
                                  if pd.notna(r.get("proj_disagreement")) else None),
            "role_churn": (round(float(r["role_churn"]), 2)
                           if pd.notna(r.get("role_churn")) else None),
            # NaN is truthy and json.dumps writes it as bare NaN, which is not
            # JSON, so this guard is notna rather than a falsiness check.
            "entropy_kind": (str(r["entropy_kind"]) or None
                             if pd.notna(r.get("entropy_kind")) else None),
            "bye_week": int(bye) if bye is not None and pd.notna(bye) else None,
            # Both guards, because they answer different questions. notna
            # first: NaN is truthy, so `or ""` alone would pass it straight
            # through, the same trap as espn_injury above. str() then keeps a
            # non-string value out of json.dumps, and the trailing `or ""`
            # normalises an empty result to the empty string.
            "bye_conflicts": (str(r["bye_conflicts"])
                              if pd.notna(r.get("bye_conflicts")) else "") or "",
            "why": why,
        })
    return _emit(_jsonable({
        "evaluating_pick": current,
        "round": (current - 1) // league.teams + 1,
        "your_next_pick_after_this": after,
        "picks_you_wait": (after - current) if after else None,
        "your_roster": roster,
        "room_drift": {**drift, "note": "median picks before ADP this room drafts, by "
                                        "position where it has enough picks; survival "
                                        "odds are shifted by `shift`"},
        "recommendations": picks,
        "headline": (model.headline(*head) if head is not None else "Board empty"),
        "roster_note": (
            "; ".join(f"{pos}: {n} counted, {p} priced" for pos, (n, p) in sorted(thin.items()))
            + " — the board carries a row for one of these players but records no "
              "position on it, so he counts toward the position you drafted him at "
              "and is priced at none, and the roster these are scored against is "
              "thinner than the count. That is the board being wrong about someone, "
              "not merely missing him: worth reporting to whoever built it"
            if thin else None),
    }), indent=2)


@mcp.tool()
def record_pick(player_name: str, overall_pick: int | None = None,
                team_slot: int | None = None) -> str:
    """Log a pick that just happened. Use after every pick if you aren't auto-syncing."""
    state = _state()
    b = _build_board()
    row = bd.match_player(player_name, b)
    resolved = row["name"] if row is not None else player_name
    # Store the position, the way `sync_draft` does. It was reported in the
    # answer below and thrown away, which was invisible until something counted
    # picks by position: `plan_my_draft` decides whether a required position can
    # still be exhausted by comparing what is left against what the league has
    # already taken, and a draft logged by hand answered "none taken" for every
    # position. Late in such a draft that turns into `continue` and the position
    # is dropped from the plan entirely -- the bug #26 exists to fix, reappearing
    # for anyone not auto-syncing.
    pick = state.record(resolved, overall_pick, team_slot,
                        position=(str(row["position"]) if row is not None else None))
    return _emit({
        "recorded": pick,
        "matched_to": resolved if row is not None else "no model match (logged as typed)",
        "position": (row["position"] if row is not None else None),
        **state.summary(),
    }, indent=2)


@mcp.tool()
def sync_draft(platform: str, league_id: str | None = None, draft_id: str | None = None,
               pasted_board: str | None = None, season: int = CURRENT_SEASON) -> str:
    """Pull the current draft board from your platform.

    platform="sleeper" with draft_id -- fully automatic, public API.
    platform="espn" with league_id -- works for public leagues; private ones need
      ESPN_SWID and ESPN_S2 environment variables from a logged-in browser session.
      While a draft is in progress the picks come from the draft room socket, which
      needs those cookies and briefly disconnects the browser draft room.
    platform="paste" with pasted_board -- paste the drafted list from any site.
    """
    entry = _WATCHES.get(str(league_id))
    if entry is not None and entry[0].connected:
        return _emit({"error": "a draft watch is connected for this league and keeps the "
                                    "board current; stop_watch first if you really want a resync",
                           **entry[0].state.summary()})
    state = _state()
    b = _build_board()
    platform = platform.lower()
    picks: list[dict[str, Any]]

    if platform == "sleeper":
        if not draft_id:
            return _emit({"error": "draft_id required for Sleeper"})
        picks = bd.sync_sleeper(draft_id)
    elif platform == "espn":
        if not league_id:
            return _emit({"error": "league_id required for ESPN"})
        picks = bd.sync_espn(league_id, season)
    elif platform == "paste":
        if not pasted_board:
            return _emit({"error": "pasted_board text required"})
        names = bd.parse_pasted_board(pasted_board)
        picks = [{"overall": i + 1, "slot": None, "name": n} for i, n in enumerate(names)]
    else:
        return _emit({"error": f"unknown platform '{platform}'"})

    state.reset()
    unmatched = []
    for p in picks:
        name = str(p["name"])
        row = bd.match_player(name, b)
        if row is None:
            unmatched.append(name)
        overall = p.get("overall")
        slot = p.get("slot")
        state.record(str(row["name"]) if row is not None else name,
                     int(overall) if overall is not None else None,
                     int(slot) if slot is not None else None,
                     position=(str(row["position"]) if row is not None
                               else (str(p["position"]) if p.get("position") else None)))
    audit = bd.audit_state(b, state)
    return _emit({
        "platform": platform, "picks_synced": len(picks),
        "unmatched_names": unmatched[:20],
        **state.summary(),
        "audit": {"ok": audit["ok"], "failures": audit["failures"]},
    }, indent=2)


@mcp.tool()
def league_rules(league_id: str, season: int = CURRENT_SEASON) -> str:
    """The ESPN league's rules as ESPN states them: draft format, roster slots and
    position limits, every scoring value, regular season and playoff weeks and
    seeding, waiver mode and timing, trade rules, lineup lock, tiebreakers, plus
    the season's bye-week topology (teams on bye per week, byes inside the
    playoffs). First-party: read from the league settings, never assumed."""
    return _emit(bd.espn_league_rules(league_id, season), indent=2, default=str)


@mcp.tool()
def draft_audit(limit: int = 10) -> str:
    """Check the invariants a recommendation depends on: board keys match the
    normaliser, pick numbers are contiguous, no player recorded twice, your picks
    sit on your slot's schedule, and no drafted player appears in the top
    recommendations. Run it whenever a recommendation looks wrong."""
    league, weights = _settings()
    state = _state()
    b = _mark_drafted(_build_board(), state)
    nxt = state.next_pick_for_me()
    recs = None
    if nxt is not None:
        recs = model.recommend(b, league, current_pick=nxt, next_pick=state.pick_after_next(),
                               roster=state.my_roster(b), top_n=limit, mine=state.my_rows(b),
                               bye_weight=weights.bye,
                               room_picks=state.picks_by_position(b),
                               picks_so_far=len(state.picks),
                               room_held=state.held_by_slot(b))
    out = bd.audit_state(b, state, recs)
    # Board rows the market join could not price are the Estimé shape: a
    # synthetic ADP where a real one may exist under another spelling.
    out["market_join"] = bd.market_join_report(b, limit)
    return _emit(out, indent=2, default=str)


@mcp.tool()
def draft_status(ctx: Context = None) -> str:
    """Where the draft stands and what your roster looks like."""
    # Stays sync: `on_the_clock` calls this directly, and making it a coroutine
    # would break that call rather than await it. Taking a `ctx` does not require
    # async, and `_attach_session` is safe with or without a running loop.
    if ctx is not None:
        _attach_session(ctx.session)
    state = _state()
    b = _mark_drafted(_build_board(), state)
    mine = [p for p in state.picks if p["slot"] == state.my_slot]
    idx = b.set_index("_key")
    detail = []
    for p in mine:
        k = bd.norm_name(p["name"])
        r = idx.loc[k] if k in idx.index else None
        # A player the board cannot price used to report a null position beside
        # a roster_counts that already counted him, from the recorded position
        # this now falls back to. Two fields in one response disagreeing about
        # whether the man is on the team reads as a bug in whichever the user
        # happens to check second.
        pos = r["position"] if r is not None else p.get("position")
        priced = r is not None
        detail.append({
            "pick": p["overall"], "player": p["name"],
            "position": str(pos) if pos else None,
            "proj_points": (round(float(r["proj_points"]), 1) if priced else None),
            # Said rather than implied by the null: he holds his slot, and the
            # lineup model counts him at replacement level for the position.
            "priced_by_the_board": priced,
            "counted_at": (None if priced else
                           round(bd.replacement_points(b, str(pos)), 1) if pos
                           else None),
        })
    return _emit({**state.summary(), "my_team": detail,
                       "roster_counts": state.my_roster(b)}, indent=2)


@mcp.tool()
def undo_pick() -> str:
    """Remove the most recent pick — for when someone mis-enters the board."""
    state = _state()
    removed = state.undo()
    return _emit({"removed": removed, **state.summary()}, indent=2)


@mcp.tool()
def reset_draft() -> str:
    """Clear all recorded picks and start fresh."""
    state = _state()
    state.reset()
    return _emit({"reset": True, **state.summary()}, indent=2)


@mcp.tool()
def separation_report(position: str = "WR", player_name: str | None = None,
                      limit: int = 20) -> str:
    """Separation and route efficiency, plus the season-long matchup each player draws.

    avg_separation is NFL Next Gen Stats tracking data: yards of daylight between
    receiver and nearest defender when the ball arrives. YPRR and TPRR use routes
    estimated from snap share times team dropbacks. Only players who cleared 250
    routes and 50 targets in a season are included, so these are real workloads
    rather than flattering part-time rates.

    `matchup_z` is the receiver's own team's schedule difficulty for the upcoming
    season, from the same opponent-defense data that drives the model's schedule
    adjustment: positive means an easier slate (opponents allow more fantasy points
    to the position), negative means a tougher one. This is the open-data stand-in
    for a WR/CB matchup chart -- team-level and season-long rather than man-coverage
    and week-to-week, since which specific corner covers which receiver on a given
    snap isn't in any open dataset (that needs per-play charting only commercial
    providers do).

    matchup_z is informational only -- players are ranked by sep_score (talent), not
    by a blended score. A backtest (see matchup_backtest) found that folding schedule
    difficulty into a combined ranking made it a worse predictor of actual finish
    than talent alone for WR, so it isn't blended into the sort here.

    Man-versus-zone splits are not reproducible from open data — that needs
    per-play coverage charting.
    """
    from . import separation as sep_mod

    prof = sep_mod.separation_profile()
    prof = prof[prof["qualified"]]
    if prof.empty:
        return _emit({"error": "no qualified players"})

    league, _ = _settings()
    dfn = features.defense_ratings(sc=league.scoring)
    sos = features.strength_of_schedule(CURRENT_SEASON, dfn)
    pos = position.upper()
    sos_col = f"sos_{pos}_z"
    sos_cols = ["team"] + ([sos_col] if sos_col in sos.columns else [])

    if player_name:
        row = bd.match_player(player_name, _build_board())
        target = bd.norm_name(row["name"]) if row is not None else bd.norm_name(player_name)
        hist = prof[prof["_key"] == target].sort_values("season")
        if sos_col in sos.columns:
            hist = hist.merge(sos[sos_cols], on="team", how="left") \
                       .rename(columns={sos_col: "matchup_z"})
        cols = ["season", "team", "avg_separation", "avg_cushion", "yprr", "tprr",
                "rec_targets", "rec_yards", "routes_est", "sep_score"]
        if "matchup_z" in hist.columns:
            cols.append("matchup_z")
        return _emit({
            "player": player_name,
            "by_season": _rows(hist, cols, 6),
        }, indent=2, default=str)

    recent = int(prof["season"].max())
    cur = prof[(prof["season"] == recent) & (prof["position"] == pos)].copy()
    cols = ["name", "team", "avg_separation", "avg_cushion", "yprr",
            "tprr", "rec_targets", "routes_est", "sep_score"]
    if sos_col in sos.columns and not cur.empty:
        cur = cur.merge(sos[sos_cols], on="team", how="left").rename(columns={sos_col: "matchup_z"})
        cols.append("matchup_z")
    cur = cur.sort_values("sep_score", ascending=False)
    return _emit({
        "season": recent, "position": pos, "schedule_season": CURRENT_SEASON,
        "note": "sep_score is a within-season z-score blending separation, YPRR, TPRR "
                "and YAC over expected -- players are ranked by this. matchup_z is "
                "informational only (positive = easier upcoming schedule for the "
                "position, negative = tougher): a backtest (matchup_backtest) found "
                "blending it into the ranking made predictions worse for WR, not "
                "better, so it's shown for reference but not part of the sort.",
        "players": _rows(cur, cols, limit),
    }, indent=2, default=str)


@mcp.tool()
def value_picks(limit: int = 20, direction: str = "undervalued") -> str:
    """Where the model disagrees with the draft market, on draftable players only.

    Positive gap means the model ranks a player higher than the room does — the
    players you can wait on and still get. Negative means the market is paying more
    than the model thinks they're worth.
    """
    b = _mark_drafted(_build_board(), _state())
    avail = b[~b["drafted"]].copy()
    # Only players the market actually ranks. A synthetic fallback ADP means nobody
    # is drafting him, so calling him "undervalued" is meaningless — that is how a
    # fringe receiver kept surfacing next to real draft picks.
    if "adp_source" in avail.columns:
        consensus = avail["adp_source"].astype(str).str.startswith("consensus") | \
                    avail["adp_source"].astype(str).str.contains("ecr|csv|espn", case=False)
        if consensus.any():
            avail = avail[consensus]
    avail = avail[avail["adp"] <= 220]
    avail["market_gap"] = avail["adp"] - avail["overall_rank"]
    asc = direction.lower().startswith("over")
    out = avail.sort_values("market_gap", ascending=asc)
    cols = ["name", "position", "team", "adp", "overall_rank", "pos_rank", "market_gap",
            "proj_points", "consistency", "injury_risk", "sep_score"]
    return _emit({
        "direction": direction,
        "adp_source": str(avail["adp_source"].mode().iloc[0]) if "adp_source" in avail else "n/a",
        "note": "market_gap > 0 means the model likes him more than his draft cost",
        "players": _rows(out, cols, limit),
    }, indent=2, default=str)


@mcp.tool()
def on_the_clock(platform: str, league_id: str | None = None, draft_id: str | None = None,
                 pasted_board: str | None = None, season: int = CURRENT_SEASON,
                 limit: int = 6) -> str:
    """The full on-the-clock workflow in one call: sync, status, pick, value, matchup.

    Runs, in order:
    1. sync_draft — a fresh pull from your platform, no cached state.
    2. draft_status — round, on-the-clock, and your roster, confirmed against the sync.
    3. who_should_i_pick — the recommendation, reasoning, and survival odds.
    4. value_picks — market-value context, scoped to this round and next.
    5. separation_report — only when the top recommendation is a WR or TE, that
       player's route efficiency and schedule context.

    Use this instead of calling each tool separately when you're on the clock and
    want the full picture in one shot. platform/league_id/draft_id/pasted_board/season
    are exactly sync_draft's arguments.
    """
    sync = json.loads(sync_draft(platform, league_id, draft_id, pasted_board, season))
    if "error" in sync:
        return _emit({"step": "sync_draft", **sync}, indent=2)

    status = json.loads(draft_status())
    rec = json.loads(who_should_i_pick(limit=limit))

    league, _ = _settings()
    rnd = rec.get("round", 1)
    lo, hi = (rnd - 1) * league.teams + 1, (rnd + 1) * league.teams
    pool = json.loads(value_picks(limit=100, direction="undervalued"))
    in_window = [p for p in pool.get("players", [])
                if p.get("adp") is not None and lo <= p["adp"] <= hi]

    result = {
        "sync": sync,
        "draft_status": status,
        "recommendation": rec,
        "value_picks_this_round": {
            "round_window": f"picks {lo}-{hi}",
            "direction": pool.get("direction"),
            "players": in_window[:8],
        },
    }

    picks = rec.get("recommendations") or []
    if picks and picks[0].get("position") in ("WR", "TE"):
        result["separation_report"] = json.loads(
            separation_report(position=picks[0]["position"], player_name=picks[0]["player"]))

    return _emit(result, indent=2)


@mcp.tool()
def draft_value_history(seasons: str = "2021,2022,2023,2024", group_by: str = "draft_round") -> str:
    """Backtest: how preseason consensus rank compared to where players actually finished.

    Value is measured in points against what that draft slot actually returned, so
    "did RB5 capital buy RB5 production?" Rank movement would be unfair to early
    picks and would label whole rounds as busts, because undrafted breakouts push
    every drafted player down the final standings.
    """
    league, _ = _settings()
    yrs = [int(s) for s in seasons.split(",") if s.strip()]
    hist = adp_mod.value_history(yrs, league.scoring)
    if hist.empty:
        return _emit({"error": "no ECR history available"})
    rates = adp_mod.hit_rates(hist, group_by)
    return _emit({
        "seasons": yrs,
        "players_analysed": int((hist["ecr"] <= adp_mod.DRAFTABLE_ECR_CUTOFF).sum()),
        "definitions": {"hit": "scored >=115% of the points that draft slot returned",
                        "bust": "scored <=70%"},
        "by_" + group_by: _rows(rates, list(rates.columns), 30),
    }, indent=2, default=str)


@mcp.tool()
def matchup_backtest(seasons: str = "2021,2022,2023,2024", position: str = "WR",
                     top_n: int = 24) -> str:
    """Backtest: does talent + schedule difficulty predict finish better than talent alone?

    This checks, against real seasons, whether blending schedule difficulty into a
    receiver's talent score improves how well it predicts actual finish. For each
    season, talent (`talent_z`) is that player's separation score from the *prior*
    season only, and matchup difficulty (`matchup_z`) is the same leakage-free
    strength_of_schedule the live recommender uses, both compared to actual fantasy
    points scored.

    A positive `improvement_corr` / `improvement_precision` means the schedule
    adjustment earns its keep. Near zero or negative means talent alone predicts
    just as well or better -- which is what a 2021-2024 WR backtest found, so
    separation_report ranks by talent (sep_score) alone and shows matchup_z as
    reference only. Re-run this if the underlying model changes.
    """
    league, _ = _settings()
    yrs = [int(s) for s in seasons.split(",") if s.strip()]
    hist = adp_mod.matchup_value_backtest(yrs, position.upper(), league.scoring)
    if hist.empty:
        return _emit({"error": "no matchup backtest data available for those seasons"})
    summary = adp_mod.matchup_backtest_summary(hist, top_n)

    swing = hist.copy()
    swing["swing"] = swing["matchup_z"].abs()
    swing_cols = ["name", "season", "team", "talent_z", "matchup_z",
                 "matchup_adjusted_score", "points", "finish_pos_rank"]
    biggest_swings = swing.sort_values("swing", ascending=False)

    return _emit({
        "position": position.upper(),
        "summary": summary,
        "interpretation": (
            "corr is Spearman rank correlation against actual fantasy points; "
            "top_n_precision is, of each metric's predicted top-N players, what "
            "share actually finished top-N that season, averaged across seasons"
        ),
        "biggest_schedule_swings": _rows(biggest_swings, swing_cols, 15),
    }, indent=2, default=str)


@mcp.tool()
def redzone_shift_backtest(seasons: str = "2022,2023,2024,2025", position: str = "WR",
                          top_n: int = 24) -> str:
    """Backtest: does a team's red zone play-calling identity improve on the
    touchdown-luck signal alone at predicting next season's fantasy points?

    Same idea and same discipline as matchup_backtest, applied to the
    `redzone_identity_shift` feature surfaced (informational only) through
    `team_context`. For each season, `talent_z` is the existing touchdown-luck
    signal `m_td_luck` already uses (a player's own prior-season red zone role vs.
    his position's baseline, z-scored), and `matchup_z` here is that player's
    team's red zone identity shift from that same prior season, z-scored across
    teams -- both leakage-free, both compared to real fantasy points scored.

    A positive `improvement_corr`/`improvement_precision` would mean the shift
    adjustment earns its keep and is worth wiring into `m_td_luck`. A 2022-2025
    run found the opposite for both WR (improvement_corr -0.006 across 300
    player-seasons) and TE (-0.053 across 117): red zone identity shift makes the
    prediction *worse*, not better -- the same conclusion matchup_backtest reached
    for schedule difficulty. This is why the shift stays informational-only in
    `team_context` rather than feeding `draft_score`. Re-run this if the underlying
    model or feature changes; only WR/TE are supported, since a pass-rate shift has
    no defensible sign for a running back.
    """
    league, _ = _settings()
    yrs = [int(s) for s in seasons.split(",") if s.strip()]
    hist = adp_mod.redzone_shift_backtest(yrs, position.upper(), league.scoring)
    if hist.empty:
        return _emit({"error": "no red zone shift backtest data available for those seasons"})
    summary = adp_mod.matchup_backtest_summary(hist, top_n)

    swing = hist.copy()
    swing["swing"] = swing["matchup_z"].abs()
    swing_cols = ["name", "season", "team", "talent_z", "matchup_z",
                 "matchup_adjusted_score", "points", "finish_pos_rank"]
    biggest_swings = swing.sort_values("swing", ascending=False)

    return _emit({
        "position": position.upper(),
        "summary": summary,
        "interpretation": (
            "corr is Spearman rank correlation against actual fantasy points; "
            "top_n_precision is, of each metric's predicted top-N players, what "
            "share actually finished top-N that season, averaged across seasons; "
            "matchup_z here is the team's red zone identity shift, not schedule"
        ),
        "biggest_shift_swings": _rows(biggest_swings, swing_cols, 15),
    }, indent=2, default=str)


@mcp.tool()
def draft_backtest(league_id: str, season: int, top_n: int = 3) -> str:
    """Replay a real past ESPN draft: the algorithm's pick, the true hindsight-best
    pick, and what you actually took, round by round.

    Give it a past season and your ESPN league id (auto-detects your team and
    draft slot from ESPN_SWID/ESPN_S2) and it rebuilds the board leak-free for
    that season -- only data from strictly before it, the same discipline
    matchup_backtest uses -- then replays the real draft in order. At each of
    your picks it reports three things: what who_should_i_pick's algorithm would
    have recommended given the real board at that exact moment, the true
    hindsight-optimal pick by value over replacement (QB capped at 1 -- a second
    quarterback can't start, so it isn't ranked against real RB/WR/TE need), and
    what you actually took. All three are scored on real points from that season.

    Each of the three picks also carries a value verdict (preseason ECR against
    actual finish -- the value_picks steal/bust framing, against real outcomes
    instead of projections) and team context (that player's team's O-line,
    pace, and schedule difficulty for the season being tested, leak-free --
    what team_context reports, but for a past season instead of always today).

    K/DST aren't modelled anywhere in this tool, so those rounds report your
    actual pick only, same as everywhere else. Only ESPN is supported.
    """
    out = adp_mod.draft_backtest(league_id, season, top_n=top_n)
    return _emit(out, indent=2, default=str)


@mcp.tool()
def mock_draft(season: int, n_trials: int = 30, top_n: int = 5) -> str:
    """Monte Carlo mock draft: the live algorithm against many simulated
    opponents, averaged, using your active league's settings.

    No real draft needed -- the other teams are bots that pick by that season's
    real preseason ADP with realistic reach/fall noise (bigger swings plausible
    late, tight consensus at the top) rather than following it exactly, so
    who's actually on the board at your turn varies draw to draw. Your slot
    (from your active league's draft_slot) runs the exact same recommend()
    logic who_should_i_pick uses live. The board is leak-free -- only data from
    strictly before `season` feeds the projections, same discipline
    draft_backtest uses -- so passing the current season runs this against the
    real live board (this year's projections, history through last season)
    instead of a past, already-decided one.

    Scored on real points from `season` when they exist; for a season that
    hasn't been played yet, falls back to the model's own proj_points instead
    (check `scored_on` in the result) -- a forecast of the algorithm's typical
    outcome, not a validated backtest.

    One draw can make the algorithm look better or worse than its true average
    just from bot luck, which is why this runs n_trials and reports the mean,
    not a single result. For each round it also reports the most common picks
    and how often each showed up -- rounds with no real consensus (usually
    round 6+, once enough upstream bot randomness has compounded) should be
    read as "plausible outcomes," not "the pick."

    K/DST aren't modelled, so only skill-position rounds are simulated
    (your league's total rounds minus its K and DST starting slots).
    """
    league, weights = _settings()
    out = adp_mod.mock_draft(league, weights, season, n_trials=n_trials, top_n=top_n)
    return _emit(out, indent=2, default=str)


@mcp.tool()
def bye_backtest(seasons: str = "2022,2023,2024,2025", n_trials: int = 20,
                 bye_weight: float = 0.08, blocks: int = adp_mod.DEFAULT_BLOCKS) -> str:
    """Backtest: does the bye-week stacking penalty win more weekly lineup points?

    Paired mock drafts per season and seed, once with bye_weight 0 and once with
    the given weight, identical bots and noise, scored as the best legal lineup
    each regular-season week on real box scores.

    Run in `blocks` disjoint blocks of `n_trials`, and every block's improvement
    is reported. Read `improvement` against `block_spread`, the distance between
    two blocks of the same configuration: when `blocks_agree` is false the
    improvement is inside the harness's own noise and supports nothing. A
    positive improvement whose blocks agree means the penalty earns its keep and
    belongs in `model_settings` for this league.
    """
    import logging

    league, weights = _settings()
    yrs = [int(s) for s in seasons.split(",") if s.strip()]
    lines: list[str] = []

    def progress(msg: str) -> None:
        lines.append(msg)
        logging.getLogger(__name__).info("bye_backtest: %s", msg)

    out = adp_mod.bye_backtest(league, weights, yrs, n_trials=n_trials,
                               bye_weight=bye_weight, blocks=blocks, progress=progress)
    out["progress"] = lines
    return _emit(out, indent=2, default=str)


@mcp.tool()
def champion_strategies(league_id: str, seasons: str = "2020,2021,2022,2023,2024,2025") -> str:
    """What actually won your ESPN league, season by season, and which specific
    pick made the difference.

    For each season, finds whichever team finished 1st and pulls their real
    draft. Every pick gets a value verdict -- preseason ECR against actual
    finish, the same steal/bust framing value_picks and draft_backtest use --
    so this answers "what draft-cost bet actually paid off for the winner,"
    not just "what did the champion draft." Reports each champion's opening two
    picks, first QB/TE round, RB/WR volume, and biggest steal, plus
    cross-season patterns: how often champions opened RB-RB, and the median
    round of their first QB.

    biggest_steal also explains *why* it was a steal: usage_trend is that
    player's real early- vs. late-season carries/targets/target share (a role
    expansion actually visible in the box scores), and team_environment is his
    team's O-line ranks, pace, and pass/rush split that season. Most value
    picks turn out to be a volume or role story, not raw talent beating a
    forecast -- this is what shows it concretely.

    ECR history only goes back to 2020 -- seasons before that get position and
    timing data but no value verdicts or steal context. ESPN only.
    """
    yrs = [int(s) for s in seasons.split(",") if s.strip()]
    out = adp_mod.champion_strategies(league_id, yrs)
    return _emit(out, indent=2, default=str)


@mcp.tool()
def persistent_value_players(seasons: str = "2021,2022,2023,2024",
                             min_seasons: int = 3, limit: int = 20) -> str:
    """Players who beat their draft cost repeatedly, not once.

    One outperformance is a season; three is a trait. This is the closest the data
    comes to naming players the market persistently misprices.
    """
    league, _ = _settings()
    yrs = [int(s) for s in seasons.split(",") if s.strip()]
    hist = adp_mod.value_history(yrs, league.scoring)
    if hist.empty:
        return _emit({"error": "no ECR history available"})
    rep = adp_mod.repeat_value_players(hist, min_seasons)
    cols = ["name", "position", "seasons", "hits", "busts", "hit_rate",
            "avg_value_ratio", "avg_ecr", "avg_games"]
    return _emit({
        "min_seasons": min_seasons,
        "best_value": _rows(rep, cols, limit),
        "worst_value": _rows(rep.tail(limit).iloc[::-1], cols, limit),
    }, indent=2, default=str)


@mcp.tool()
def rookie_report(limit: int = 20, position: str | None = None) -> str:
    """Projected rookies for this season, from draft capital and landing spot.

    Rookies have no NFL history, so they're projected off a curve fitted to how draft
    pick converted to first-year production across the last ten classes, then adjusted
    for the offence they landed in. Consistency is deliberately low for all of them:
    rookie roles move mid-season and the floor is a healthy scratch.

    Treat these as the widest error bars on the board.
    """
    b = _mark_drafted(_build_board(), _state())
    r = b[b.get("is_rookie", False) == True]  # noqa: E712
    if position:
        r = r[r["position"] == position.upper()]
    if r.empty:
        return _emit({"error": "no rookies on the board — draft class may not be published yet"})
    r = r.sort_values("draft_score", ascending=False)
    cols = ["name", "position", "team", "pick", "draft_round", "college", "adp",
            "overall_rank", "proj_points", "adj_ppg", "exp_games", "consistency",
            "drafted"]
    return _emit({
        "rookies": len(r),
        "note": "pick is NFL draft position; adp is fantasy market cost",
        "players": _rows(r, [c for c in cols if c in r.columns], limit),
    }, indent=2, default=str)


@mcp.tool()
def resolve_names(names_csv: str) -> str:
    """Check how names resolve against the board — useful before trusting a paste sync.

    Reports the match type for each name so silent mismatches surface. A name that
    fails to resolve looks like a player who scored zero, which is the single most
    damaging failure mode in this whole pipeline.
    """
    b = _build_board()
    queries = [q.strip() for q in names_csv.split(",") if q.strip()]
    out = []
    for q in queries:
        row, how = bd.match_player_verbose(q, b)
        out.append({
            "query": q,
            "resolved_to": (row["name"] if row is not None else None),
            "position": (row["position"] if row is not None else None),
            "team": (str(row["team"]) if row is not None else None),
            "match_type": how,
        })
    return _emit({
        "resolved": sum(1 for o in out if o["resolved_to"]),
        "of": len(out),
        "results": out,
    }, indent=2, default=str)


@mcp.tool()
def prewarm(verbose: bool = True) -> str:
    """Build every cache before draft day so nothing computes while you're on the clock.

    The first query of a session pays for downloading and modelling five seasons.
    Every query after it is served from memory. Run this an hour before your draft,
    not during it.
    """
    import time as _time

    timings, t0 = {}, _time.time()
    steps = [
        ("play_by_play", lambda: sources.play_by_play()),
        ("weekly_stats", lambda: sources.weekly_stats()),
        ("snap_counts", lambda: sources.snap_counts()),
        ("injuries", lambda: sources.injuries()),
        ("rosters", lambda: sources.weekly_rosters()),
        ("schedules", lambda: sources.schedules()),
        ("board", lambda: _build_board()),
        ("oline", lambda: features.oline_ratings()),
        ("pace", lambda: features.team_pace_and_split()),
    ]
    for name, fn in steps:
        s = _time.time()
        try:
            fn()
            timings[name] = round(_time.time() - s, 2)
        except Exception as exc:
            timings[name] = f"failed: {type(exc).__name__}"

    b = _build_board()
    out = {
        "total_seconds": round(_time.time() - t0, 1),
        "players": len(b),
        "rookies": int(b.get("is_rookie", pd.Series(dtype=bool)).sum()),
        "ready": True,
        "note": "All subsequent tool calls are served from memory.",
    }
    if verbose:
        out["step_seconds"] = timings
        out["disk_cache"] = sources.cache_status()
    return _emit(out, indent=2, default=str)


@mcp.tool()
def player_report(player_name: str) -> str:
    """Full breakdown of one player: production, role, environment, injury, consistency."""
    b = _build_board()
    r = bd.match_player(player_name, b)
    if r is None:
        return _emit({"error": f"no match for '{player_name}'"})
    fields = ["name", "position", "team", "age", "overall_rank", "pos_rank", "adp", "adp_delta",
              "proj_points", "adj_ppg", "baseline_ppg", "exp_games",
              "consistency", "startable_rate", "spike_rate", "floor", "ceiling", "fp_cv",
              "target_share", "carry_share", "redzone_share", "snap_share", "touches",
              "role_entropy", "proj_disagreement", "role_churn", "entropy_kind",
              "entropy_basis",
              "starter", "depth_rank", "starter_injury_risk", "starter_games_missed",
              "standalone_points", "contingent_points", "ev_handcuff",
              "injury_risk", "games_missed_rate", "report_rate", "heavy_seasons", "recent_burden",
              "run_block_rank", "pass_block_rank", "plays_per_game", "neutral_pass_rate",
              "rush_rate", "divisional_games",
              "sep_score", "avg_separation", "avg_cushion", "yprr", "tprr", "yac_oe", "adot",
              "is_rookie", "pick", "draft_round", "college",
              "rz_touches", "rz_td", "rz_td_rate", "rz_baseline_rate",
              "m_oline", "m_volume", "m_schedule", "m_divisional", "m_injury", "m_age",
              "m_separation", "m_td_luck", "m_coverage_trend", "vor"]
    out = _rows(pd.DataFrame([r]), [f for f in fields if f in r.index], 1)[0]
    out["summary"] = model.explain(r)
    return _emit(out, indent=2)


@mcp.tool()
def compare_players(names: str) -> str:
    """Compare 2-4 players head to head. Pass a comma-separated list."""
    b = _build_board()
    rows = []
    for n in [x.strip() for x in names.split(",") if x.strip()][:4]:
        r = bd.match_player(n, b)
        if r is not None:
            rows.append(r)
    if not rows:
        return _emit({"error": "no matches"})
    df = pd.DataFrame(rows)
    cols = ["name", "position", "team", "adp", "proj_points", "adj_ppg", "consistency",
            "startable_rate", "spike_rate", "injury_risk", "exp_games", "vor", "draft_score"]
    best = df.sort_values("draft_score", ascending=False).iloc[0]
    return _emit({
        "players": _rows(df.sort_values("draft_score", ascending=False), cols, 4),
        "verdict": f"{best['name']} — {model.explain(best)}",
    }, indent=2)


@mcp.tool()
def team_context(team: str) -> str:
    """Offensive environment for an NFL team: O-line, pace, run/pass split, schedule,
    drive efficiency, and red zone play-calling identity.

    `drive_efficiency` and `redzone_identity` are informational context, not folded into
    any player's projection or draft_score -- same convention as `matchup_z` in
    separation_report. `drive_efficiency.pct_td` is the share of that team's drives
    ending in a touchdown (a multiplier on how many scoring chances its players get,
    already reflected in their raw points, so treat this as a confidence check on a
    role rather than an extra adjustment). `redzone_identity.shift` is that team's
    neutral-field pass rate minus its red zone pass rate: a large positive shift means
    the offense gets meaningfully more run-heavy inside the 20 (receiving volume there,
    and the touchdown equity that comes with it, is less trustworthy for that team's
    pass catchers); near zero or negative means the passing game keeps its role even in
    the scoring area.
    """
    league, _ = _settings()
    # No pbp argument: that routes through the memoised builders instead of
    # recomputing a full pass over play-by-play on every call.
    ol = features.oline_ratings()
    pace = features.team_pace_and_split()
    dfn = features.defense_ratings(sc=league.scoring)
    sos = features.strength_of_schedule(CURRENT_SEASON, dfn)
    drive_eff = features.team_drive_efficiency()
    rz_shift = features.redzone_identity_shift()
    t = team.upper()
    recent = int(pace["season"].max())
    out = {
        "team": t,
        "oline": _rows(ol[(ol["team"] == t) & (ol["season"] == recent)],
                       ["season", "run_block_rank", "pass_block_rank", "adj_line_yards",
                        "stuff_rate", "sack_rate"], 1),
        "oline_history": _rows(ol[ol["team"] == t].sort_values("season"),
                               ["season", "run_block_rank", "pass_block_rank"], 6),
        "pace_and_split": _rows(pace[(pace["team"] == t) & (pace["season"] == recent)],
                                ["plays_per_game", "pass_rate", "rush_rate",
                                 "neutral_pass_rate", "off_epa"], 1),
        "schedule": _rows(sos[sos["team"] == t],
                          ["divisional_games"] + [c for c in sos.columns if c.endswith("_z")], 1),
        "drive_efficiency": _rows(
            drive_eff[(drive_eff.get("team") == t) & (drive_eff.get("season", pd.Series(dtype=int)) == recent)]
            if not drive_eff.empty else drive_eff,
            ["season", "drives", "pct_td", "pct_fg", "pct_punt"], 1),
        "redzone_identity": _rows(
            rz_shift[(rz_shift.get("team") == t) & (rz_shift.get("season", pd.Series(dtype=int)) == recent)]
            if not rz_shift.empty else rz_shift,
            ["season", "neutral_pass_rate", "rz_pass_rate", "shift"], 1),
    }
    return _emit(out, indent=2, default=str)


@mcp.tool()
def defense_report(position: str = "RB", limit: int = 32) -> str:
    """Defensive rankings against a position — fantasy points allowed, 5-year view.

    Rank 1 = toughest matchup. This is what drives the schedule adjustment.
    """
    league, _ = _settings()
    dfn = features.defense_ratings(sc=league.scoring)
    pos = position.upper()
    col = f"fpa_{pos}"
    if col not in dfn.columns:
        return _emit({"error": f"no data for position {pos}"})
    recent = int(dfn["season"].max())
    cur = dfn[dfn["season"] == recent][["team", col, f"{col}_rank", "def_epa_play", "def_rank"]]
    multi = dfn.groupby("team")[col].mean().rename(f"{col}_5yr_avg").reset_index()
    multi[f"{col}_5yr_rank"] = multi[f"{col}_5yr_avg"].rank(method="min").astype(int)
    out = cur.merge(multi, on="team").sort_values(f"{col}_5yr_rank")
    return _emit({
        "position": pos, "recent_season": recent,
        "note": "rank 1 = allows fewest fantasy points = toughest matchup",
        "defenses": _rows(out, list(out.columns), limit),
    }, indent=2)


def _unfilled_starters(league: LeagueSettings, roster: dict[str, int]) -> dict[str, int]:
    """Required starting slots this roster has not filled yet, by position."""
    return {pos: slots - roster.get(pos, 0)
            for pos, slots in league.starters.items()
            if slots and pos != "FLEX" and roster.get(pos, 0) < slots}


def _absorbed_by(pos: str, league: LeagueSettings, drafted: Counter, from_pick: int,
                 pick: int) -> int:
    """How many of `pos` the rest of the league has taken by `pick`, counted into
    a board that already excludes the ones taken so far.

    Anchored on what has actually happened rather than projected from the start
    of the draft: the league needs `starters * teams` of the position, `drafted`
    of them are gone, and the remainder is absorbed evenly over the picks left.
    At `from_pick` this is 0 — the best one on the board really is available
    now — and it rises to the whole remainder by the last pick, so it is
    non-decreasing in `pick` by construction, which is the property #32 wants.

    The board it indexes into is the *available* pool, which is why `drafted` is
    subtracted rather than added: those players are not in the list to be
    skipped over a second time. Measured on the live board, 32 defenses exist,
    31 are available and 1 is taken.
    """
    total_need = league.starters.get(pos, 0) * league.teams
    remaining = max(0, total_need - drafted.get(pos, 0))
    picks_left_in_draft = league.teams * league.rounds - from_pick
    if picks_left_in_draft <= 0 or remaining <= 0:
        return 0
    elapsed = min(1.0, max(0.0, (pick - from_pick) / picks_left_in_draft))
    return int(remaining * elapsed)


def _plan_pool(avail: pd.DataFrame, taken: set[str], from_pick: int, pick: int,
               league: LeagueSettings, roster: dict[str, int],
               state: bd.DraftState, picks_left: int) -> pd.DataFrame:
    """Who is realistically still there at `pick`, for the plan's simulation.

    Availability is the recommender's own survival model rather than the hard
    `adp > pick - 1.1*sqrt(pick)` cut this used to apply, so the plan and
    `who_should_i_pick` answer "is he available" the same way. Everyone here has
    already been confirmed available *now*, so the question is conditional and
    `survival_probability_vec` is exactly it.

    The survival model is per player, and that is not sufficient on its own. It
    is driven by ADP, and ESPN's ADP for kickers and defenses does not describe
    a real room: every available K and D/ST has an ADP between 93 and 171, while
    this room has taken one of each in 122 picks. Both the old cut and a
    survival threshold therefore empty those positions completely from pick 189
    on — 0 of 62 — so the plan could not fill a required K or D/ST slot at any
    pick, and ended a 14-round draft with both empty.
    That is a per-position question wearing a per-player answer, so a required
    position is answered by counting instead. The rest of the league still needs
    `starters * teams` of it and has taken some already; the remainder is
    absorbed over the picks that are left, so by the target pick it has taken

        (total_need - taken_so_far) * (pick - from_pick) / (last_pick - from_pick)

    of what is on the board now, and the plan is offered the best one after
    those. No ADP, no threshold, no fitted constant — the two inputs are
    `league.starters[pos] * league.teams` and the recorded picks' own positions.

    This is applied at *every* turn, which is #32 and otto's finding. It used to
    apply only once the availability filter had emptied the position, so what the
    plan was offered depended on ADP until the filter gave up and on counting
    afterwards. Offered index across a live slot's seven remaining picks, roster
    held fixed:

        DST   before  0, 1, 5, 9, 15, 15, 15     after  0, 1, 5, 6, 9, 10, 14
        K     before  0, 0, 7, 8, 15, 15, 15     after  0, 1, 5, 6, 9, 10, 14

    The live record's before column happens to be monotone, so the fault does not
    show there — but it is not hypothetical: on the fixture in
    `tests/test_board.py` the before row runs 0, 0, 0, 0, 0, 0, 0, 0, 2, 8, 10,
    16, 18, 12, offering the best player for eight straight turns and then
    *improving* from 18 to 12 at the final pick, which cannot happen in a draft.
    The regression test for that fails without this function.

    One thing that is easy to get wrong about these numbers, and that I did get
    wrong before measuring: the change makes the mid-draft offer
    *better*, not worse — index 9 to index 6 at pick 164 — because the survival
    filter is biased against exactly the defenses worth having: "best defense"
    and "earliest ADP" are the same players, so they are the first it discards.
    The filter no longer decides required positions at all, which is what otto's
    threshold table showed it was never usefully doing.

    The count of what the league has taken comes from the recorded picks' own
    positions, so a pick logged without one makes this more conservative, not
    less: `taken_so_far` falls, the remainder to absorb rises, and the plan is
    offered someone deeper. That is why `record_pick` has to store the position
    it resolves, which it did not until lena's 053290b — without it the anchor
    reads zero for every position and this silently reverts to projecting from
    the start of the draft.
    """
    pool = avail[~avail["_key"].isin(taken)]
    if pool.empty:
        return pool
    survives = model.survival_probability_vec(pool["adp"].to_numpy(), from_pick, pick)
    keep = pool[survives >= PLAN_SURVIVAL]
    unfilled = _unfilled_starters(league, roster)
    drafted_by_position = Counter(str(p.get("position")) for p in state.picks)
    for pos in unfilled:
        chunk = pool[pool["position"] == pos].sort_values("draft_score", ascending=False)
        if chunk.empty:
            continue
        absorbed = _absorbed_by(pos, league, drafted_by_position, from_pick, pick)
        if absorbed >= len(chunk):
            continue  # the league really can exhaust this position; the filter stands
        # Replace whatever the per-player filter said about this position: for a
        # slot the plan is required to fill, counting is the answer at every
        # turn, not a fallback once the filter has emptied it.
        keep = pd.concat([keep[keep["position"] != pos], chunk.iloc[absorbed:]])

    # With no more picks than empty required slots, every remaining pick has to
    # fill one -- that is arithmetic, not a preference, and the recommender
    # cannot see it because `draft_score` is value over replacement. The
    # sixteenth-best kicker is worth about zero over a replacement kicker and
    # about a hundred and forty points over the empty slot he would otherwise
    # leave, and only the second comparison is the one available at the last
    # pick. Without this the plan spent its final pick on a fifth receiver worth
    # 22.8 over replacement, and started no kicker at all.
    if unfilled and picks_left <= sum(unfilled.values()):
        forced = keep[keep["position"].isin(unfilled)]
        if not forced.empty:
            return forced
    return keep


# A player the survival model puts below this is treated as gone by that pick.
# "More likely than not to still be there" is the plainest reading of a
# simulation that has to commit to one roster; the cut it replaces was
# `adp > pick - 1.1*sqrt(pick)`, whose meaning nobody could state.
#
# This constant does NOT make the required-position rule work, and reading it
# that way is the misunderstanding worth heading off. otto measured the K/D-ST
# rows the filter alone keeps at each of a live slot's remaining picks:
#
#   threshold   125  132  157  164  189  196  221
#   0.0          62   62   62   62   62   62   62
#   0.2          62   62   55   55   45   44    0
#   0.5          62   61   49   45    0    0    0
#   0.8          62   55    0    0    0    0    0
#
# Every non-zero threshold empties both positions before the last pick, and 0.0
# is no filter at all. So the counting rule below does all of the work on
# whether a required slot can be filled; the threshold decides something else
# entirely -- which real players the plan believes can still reach it. At 0.0 it
# plans Ja'Marr Chase in the sixth round, which is a fantasy; at 0.95 it plans
# around Cam Skattebo, which is over-conservative. 0.5 is a policy choice about
# realism, and it was not tuned to make kickers and defenses come out right.
PLAN_SURVIVAL = 0.5


@mcp.tool()
def plan_my_draft(strategy: str = "balanced") -> str:
    """Simulate your whole draft from your slot and return the projected lineup.

    Runs the board forward pick by pick, using ADP to model who realistically falls
    to you at each turn, and applies the same recommendation logic at every stop.
    strategy: balanced, zero_rb, hero_rb, or robust_rb.
    """
    league, _ = _settings()
    state = _state()
    b = _mark_drafted(_build_board(), state).copy()
    b = b[~b["drafted"]]

    tilt = {
        "zero_rb": {"RB": 0.72, "WR": 1.12, "TE": 1.05, "QB": 0.95},
        "hero_rb": {"RB": 1.0, "WR": 1.06, "TE": 1.0, "QB": 0.95},
        "robust_rb": {"RB": 1.16, "WR": 0.94, "TE": 0.98, "QB": 0.92},
        "balanced": {},
    }.get(strategy, {})

    my_picks = [p for p in state.my_picks() if p >= state.on_the_clock]
    roster: dict[str, int] = dict(state.my_roster(b))
    taken: set[str] = set()
    plan: list[dict[str, Any]] = []

    for i, pick in enumerate(my_picks):
        nxt = my_picks[i + 1] if i + 1 < len(my_picks) else None
        # Who is realistically gone by this pick, on the recommender's own
        # survival model rather than a separate ADP rule (see _plan_pool).
        pool = _plan_pool(b, taken, state.on_the_clock, pick, league, roster, state,
                          picks_left=len(my_picks) - i).copy()
        if pool.empty:
            break
        if strategy == "hero_rb" and i > 0 and roster.get("RB", 0) >= 1:
            pool = pool.copy()
            pool.loc[pool["position"] == "RB", "draft_score"] *= 0.7
        for pos, mult in tilt.items():
            pool.loc[pool["position"] == pos, "draft_score"] *= mult

        recs = model.recommend(pool, league, current_pick=pick, next_pick=nxt,
                               roster=roster, top_n=3)
        if recs.empty:
            break
        top = recs.iloc[0]
        taken.add(top["_key"])
        roster[top["position"]] = roster.get(top["position"], 0) + 1
        plan.append({
            "round": (pick - 1) // league.teams + 1, "pick": pick,
            "player": top["name"], "position": top["position"], "team": top.get("team"),
            "adp": round(float(top["adp"]), 1),
            "proj_points": round(float(top["proj_points"]), 1),
            "consistency": round(float(top["consistency"]), 3),
            "alternates": [r["name"] for _, r in recs.iloc[1:].iterrows()],
        })

    total = sum(float(p["proj_points"]) for p in plan)
    return _emit({
        "strategy": strategy, "your_slot": state.my_slot,
        "projected_starters_points": round(total, 1),
        "final_roster": roster, "plan": plan,
        "caveat": "ADP-driven simulation of an average draft room. Your league will "
                  "deviate — use who_should_i_pick live rather than following this script.",
    }, indent=2)


@mcp.tool()
def model_settings(consistency_weight: float | None = None, injury_weight: float | None = None,
                   oline_weight: float | None = None, schedule_weight: float | None = None,
                   pace_weight: float | None = None, td_luck_weight: float | None = None,
                   qb_boost: float | None = None, coverage_trend_weight: float | None = None,
                   bye_weight: float | None = None) -> str:
    """Tune how much each factor moves a player. Rebuilds the board.

    bye_weight cuts a candidate's pick_value by that fraction for every player
    you already hold at the same position with the same bye week, and half that
    for other positions. Default 0; who_should_i_pick reports bye_week and
    bye_conflicts either way.

    td_luck_weight controls how hard a player's red zone touchdown rate gets
    regressed toward what his position converts on average (player_report shows
    rz_touches/rz_td/rz_td_rate/rz_baseline_rate so you can see the raw numbers
    behind the adjustment). Set it to 0 to score players on raw history with no
    touchdown-luck correction at all.

    qb_boost is different from the others: they all adjust the projection from a
    real per-player signal (O-line, pace, etc.); qb_boost is a direct fractional
    lift on QB draft_score you supply because you believe the position is worth
    more than the projection says, not because of any single player's own inputs.
    Comes from champion_strategies/draft_backtest analysis: check whether QB has
    actually beaten its draft cost across your league's real history (not just
    hit rate in general -- that alone doesn't justify this) before setting it
    above 0. It stacks with, and doesn't replace, the roster-need discount that
    already stops the model from wanting a second QB once you have one.

    coverage_trend_weight is the same kind of supplied belief as qb_boost, and
    also defaults to 0. It rewards WR/TE with a short-area profile (high TPRR,
    low aDOT) over boundary/vertical receivers, and RBs with real receiving role
    (target_share), on the theory that 2025's shift to zone coverage and, later,
    back to base personnel (linebackers instead of nickel corners covering the
    slot/backfield) favors those archetypes. Unlike separation or td_luck, this
    isn't backed by a per-player real signal this project can backtest -- open
    data has no man/zone or personnel-package split (see separation.py) -- so
    treat any nonzero value as an opinion you're choosing to weight in, not a
    validated adjustment, and re-check the underlying rates each season.
    """
    league, weights = _settings()
    for name, val in [("consistency_weight", consistency_weight), ("injury", injury_weight),
                      ("oline", oline_weight), ("schedule", schedule_weight),
                      ("pace_volume", pace_weight), ("td_luck", td_luck_weight),
                      ("qb_boost", qb_boost), ("coverage_trend", coverage_trend_weight),
                      ("bye", bye_weight)]:
        if val is not None:
            setattr(weights, name, float(val))
    save_settings(league, weights)
    _CACHE.update({"weights": weights})
    _BOARDS.pop(league.cache_key(), None)
    p = _board_path(league)
    if p.exists():
        p.unlink()
    return _emit({"league": league.name, "weights": weights.__dict__,
                       "board": "will rebuild on next query"}, indent=2)


@mcp.tool()
async def watch_draft(league_id: str, season: int = CURRENT_SEASON, ctx: Context = None) -> str:
    """Hold the ESPN draft room open and push every pick into this session as it
    happens, with a recommendation once you are within three picks of the clock.

    Needs ESPN_SWID and ESPN_S2 and a team you own in the league. ESPN allows one
    draft-room connection per team: starting this closes the browser draft room
    ("Duplicate Connection"), and opening the room again pauses the watch. Use
    make_pick to draft without the room. Events reach Claude only when the session was started
    with `claude --dangerously-load-development-channels server:<this server's name>`;
    otherwise they are dropped and the board is still kept current for
    who_should_i_pick and draft_status. One watch per league; calling again
    replaces it.
    """
    import asyncio
    import os
    import traceback

    from mcp import types as _types

    from . import watch

    swid, espn_s2 = os.environ.get("ESPN_SWID"), os.environ.get("ESPN_S2")
    if not (swid and espn_s2):
        return _emit({"error": "watch_draft needs ESPN_SWID and ESPN_S2"})
    try:
        ctx_info = bd.espn_league_context(league_id, season, swid, espn_s2)
        if ctx_info["my_team_id"] is None:
            return _emit({"error": "no team owned by ESPN_SWID in this league"})
        league, weights = _settings()
        board_df = _build_board()
        # The session outlives this request; a notification with no related request
        # id rides the connection's standalone channel.
        session = ctx.session
        channel_event = _types.Notification[dict[str, Any], str]

        async def notify(content: str, meta: dict[str, str]) -> None:
            await session.send_notification(channel_event(
                method="notifications/claude/channel",
                params={"content": content, "meta": meta}))

        await stop_watch(league_id)
        directory = bd.espn_league_directory(league_id, season, swid, espn_s2)
        w = watch.DraftWatch(league_id, season, int(ctx_info["my_team_id"]), swid, espn_s2,
                             league, board_df, notify, directory=directory,
                             bye_weight=weights.bye,
                             refresh=_watch_refresh)
    except Exception as exc:
        # The MCP SDK hides tool tracebacks behind "Error executing tool".
        return _emit({"error": f"{type(exc).__name__}: {exc}",
                           "traceback": traceback.format_exc()})
    task = asyncio.create_task(w.run(), name=f"draft-watch-{league_id}")
    _WATCHES[league_id] = (w, task)
    _attach_session(session)
    # Written after the watch is live, so a record only ever describes a watch
    # that actually started.
    watchstore.save(watchstore.WatchRecord(
        league_id=league_id, team_id=int(ctx_info["my_team_id"]), season=season))
    return _emit({
        "watching": league_id, "team_id": ctx_info["my_team_id"],
        "draft_slot": ctx_info["draft_slot"], "league_name": ctx_info["league_name"],
        "note": "picks arrive as channel messages; stop with stop_watch",
    }, indent=2)


@mcp.tool()
async def make_pick(league_id: str, player_name: str) -> str:
    """Make your pick in the ESPN draft over the running watch's socket.

    Sends `SELECT <playerId>` exactly as the draft room does and waits for
    ESPN's acceptance. Needs an active watch_draft for the league, and it must
    be your turn. Irreversible once ESPN accepts it. Confirm the player with the
    user before calling this.
    """
    import traceback

    w, err = _watch_or_error(league_id)
    if err:
        return err
    espn_id, resolved = bd.resolve_espn_id(player_name, _build_board(), w.espn_map)
    if espn_id is None:
        return _emit({"error": resolved})
    s = w.state.summary()
    if s["on_the_clock"] != s["my_next_pick"]:
        return _emit({"error": f"not your turn: pick {s['on_the_clock']} is on the clock, "
                                    f"yours is {s['my_next_pick']}"})
    try:
        accepted = await w.select(int(espn_id))
    except TimeoutError:
        return _emit({"error": "ESPN did not confirm the pick within 10s; check the room"})
    except Exception as exc:
        return _emit({"error": f"{type(exc).__name__}: {exc}",
                           "traceback": traceback.format_exc()})
    return _emit({"picked": accepted, "resolved": resolved, "resolved_from": player_name,
                       **w.state.summary()}, indent=2)


def _watch_or_error(league_id: str):
    entry = _WATCHES.get(league_id)
    if entry is None:
        return None, _emit({"error": "no active watch for this league; call watch_draft first"})
    w, _task = entry
    if not w.connected:
        return None, _emit({"error": "draft watch is not connected right now"})
    return w, None


# How long `set_draft_queue` waits for ESPN's first queue echo on a fresh
# connection before refusing to merge. ESPN sends one unprompted: 3.7 seconds
# after INIT on the 2026-09-05 join. Ten gives that a wide margin without
# leaving a caller hanging, and the refusal is what happens if it never comes.
QUEUE_ECHO_WAIT_SECONDS = 10.0
# How long a resumed watch waits for ESPN's INIT before reporting that it could
# not come back. Generous next to the sub-second joins seen in practice, and
# bounded so a room that never answers produces a refusal rather than a task that
# never finishes and a resume that never says either way.
RESUME_READY_SECONDS = 30.0
# Past this, a held channel message is stamped with its age. A message held long
# enough for the draft to have moved is not a delayed message, it is one whose
# subject has changed.
HELD_MESSAGE_STAMP_SECONDS = 60.0

# A session to push channel events through, and anything said before there was
# one. There is NO session at server start: sessions are built per request and
# what outlives them is the connection's standalone channel, which a session
# object holds a handle to. So a resume that happens before any client request
# has nowhere to speak, and its message waits here until a tool call hands over
# a session. This is a property of the protocol, not a policy: the watch's
# socket is live from the moment it resumes either way, and only the telling is
# deferred.
_SESSION: Any = globals().get("_SESSION")
_PENDING_CHANNEL: list[tuple[str, dict, float]] = globals().get("_PENDING_CHANNEL", [])
_log = logging.getLogger(__name__)


def _attach_session(session: Any) -> Any:
    """Adopt a session for later channel pushes and flush anything waiting.

    Returns the flush task, or None when there was nothing to flush or no loop to
    flush on. Returned rather than fired and forgotten so a caller that needs the
    flush finished can await the handle instead of guessing how many scheduler
    turns it takes.

    Callable from a sync tool, which FastMCP may run off the event loop: with no
    running loop there is nothing to schedule on, so the messages stay queued for
    the next attach that has one. Raising here would fail a tool call over an
    undelivered notification.
    """
    global _SESSION
    _SESSION = session
    if not _PENDING_CHANNEL:
        return None
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    return loop.create_task(_flush_channel(), name="ffdraft-flush-channel")


async def _flush_channel() -> None:
    """Send what was held, saying how long it waited.

    A held message is not a delayed message: its subject has moved. "47 picks
    made, your next pick is 130" was true when a watch resumed and may be twenty
    picks stale by the time a client first speaks. Stamping the age is what stops
    a reader taking it as current; it applies to every held message, because a
    pick event held for the same window is stale in the same way.
    """
    import time as _time

    pending, _PENDING_CHANNEL[:] = list(_PENDING_CHANNEL), []
    now = _time.time()
    for content, meta, held_at in pending:
        waited = max(0.0, now - held_at)
        if waited >= HELD_MESSAGE_STAMP_SECONDS:
            content = (f"{content} [held {waited / 60:.0f} min waiting for this "
                       "session; the draft may have moved since]")
        await _channel(content, meta)


async def _channel(content: str, meta: dict[str, str]) -> None:
    """Push one channel event, or hold it until a session exists.

    Failures are held rather than raised: this is called from the watch's socket
    loop and from startup, and neither may die because a notification could not
    be delivered.
    """
    import time as _time

    from mcp import types as _types

    if _SESSION is None:
        _PENDING_CHANNEL.append((content, dict(meta), _time.time()))
        return
    try:
        await _SESSION.send_notification(_types.Notification[dict[str, Any], str](
            method="notifications/claude/channel",
            params={"content": content, "meta": meta}))
    except Exception:
        _log.exception("could not send a channel event; holding it")
        _PENDING_CHANNEL.append((content, dict(meta), _time.time()))


async def _await_first_echo(w, timeout: float | None = None) -> list[int] | None:
    """Wait for ESPN to echo the queue on this connection, up to `timeout`.

    Waits on `queue_seen`, the watch's own event, rather than sampling `queue` on
    a timer: the event is set the moment an echo lands, so there is no interval
    to guess at and no window where the queue is there but this has not looked
    yet. It is a separate event from `queue_echo`, which `set_queue` owns and
    replaces per call and which this must not disturb.

    Returns the queue, or None if the timeout passed with no echo.

    `timeout` defaults to the module constant read at call time, not bound at
    definition, so the wait can be shortened for a test without the default
    freezing a ten-second pause into every run.
    """
    import asyncio as _asyncio

    timeout = QUEUE_ECHO_WAIT_SECONDS if timeout is None else timeout
    try:
        await _asyncio.wait_for(w.queue_seen.wait(), timeout=timeout)
    except (TimeoutError, _asyncio.TimeoutError):
        return None
    return w.queue


def _drafted_by_pick(w) -> dict[int, int]:
    """ESPN player id -> the overall pick that took him, from the watch's own log.

    ESPN sends no `DRAFT_LIST` when a pick removes someone from your queue, so
    the last echo keeps naming players who are gone. The watch already holds
    what is needed to say which: the INIT snapshot's picks plus every SELECTED
    line since, which is exactly `espn_live.replay_picks`.

    Returns an empty map rather than raising. This annotates a report; a queue
    that cannot be annotated is still a queue worth showing.
    """
    if not getattr(w, "init_b64", None):
        return {}
    try:
        init = espn_live.decode_init(w.init_b64)
        picks = espn_live.replay_picks(init, [line for _ts, line in w.lines])
    except Exception:
        _log.exception("could not read the pick log for league %s", w.league_id)
        return {}
    return {p["player_id"]: p["overall"] for p in picks if p.get("player_id") is not None}


def _queue_rows(w, ids: list[int], drafted: dict[int, int] | None = None) -> list[dict]:
    taken = drafted or {}
    return [{"rank": i + 1, "espn_id": pid, "name": bd._espn_player_name(pid, w.espn_map),
             # None when he is still available. Present on every row rather than
             # only the drafted ones, so "not drafted" is a stated fact and not
             # an absent key a reader has to interpret.
             "drafted_at": taken.get(pid)}
            for i, pid in enumerate(ids)]


@mcp.tool()
async def draft_queue(league_id: str) -> str:
    """Your ESPN pick queue (what autopick uses), as ESPN last echoed it over the
    watch's socket, and what is left of it.

    ESPN sends no `DRAFT_LIST` when a pick takes someone off your queue, so the
    last echo keeps naming players who are gone: at pick 135 the echo still had
    Jayden Reed at rank 3, taken thirteen picks earlier. Autopick skips them, so
    nothing breaks, but the echo alone states a queue ESPN will not use.

    `as_echoed` is what ESPN last said, verbatim, with `drafted_at` on every row
    naming the pick that took him or null if he is still there. `effective` is
    what autopick would actually draw from. `source` says where the list came
    from."""
    w, err = _watch_or_error(league_id)
    if err:
        return err
    drafted = _drafted_by_pick(w)
    # The echo history is deliberately NOT annotated: it records what ESPN said
    # at the time, and marking those rows with what has happened since would make
    # a log of the past disagree with itself.
    history = [{"at_ms": ts, "connection": conn, "size": len(ids),
                "queue": _queue_rows(w, ids)} for ts, conn, ids in w.queue_echoes]
    if w.queue is None:
        return _emit({"source": "none", "as_echoed": [], "effective": [],
                      "echoes": history,
                      "connection": w.connection,
                      "note": "ESPN has not sent a DRAFT_LIST on this connection, so "
                              "the queue it holds is unknown; set_draft_queue will "
                              "refuse to merge into it. ESPN normally echoes within "
                              "seconds of joining."}, indent=2)
    # Every echo, not just the latest: ESPN sends the whole list rather than a
    # change, so the only way to answer "when did X leave my queue" is to compare
    # consecutive echoes. Each row carries its connection, because a list that
    # shrank across a reconnect was not necessarily edited by anyone.
    still_there = [pid for pid in w.queue if pid not in drafted]
    return _emit({
        "source": "socket",
        "as_echoed": _queue_rows(w, w.queue, drafted),
        # The queue autopick would actually draw from. Ranks renumber, because a
        # rank is a position in the list that will be used, not in the one ESPN
        # last happened to send.
        "effective": _queue_rows(w, still_there, drafted),
        "drafted_since_the_echo": len(w.queue) - len(still_there),
        "connection": w.connection, "echoes": history,
        # Observation, not a source. See watch._check_init_queue.
        "init_queue_checks": w.init_queue_checks}, indent=2)


@mcp.tool()
async def set_draft_queue(league_id: str, player_names: str, replace: bool = False) -> str:
    """Put these players at the front of your ESPN pick queue, keeping the rest.

    Comma-separated names, in the order you want them. This is what ESPN
    autopicks from if you miss your clock. Confirm the order with the user before
    calling this.

    The queue has two authors: the user, in the ESPN app, and this server. ESPN's
    protocol has no add or remove, only `DRAFT_LIST` carrying the whole list, so
    anything the user queued and this call does not send is gone. By default the
    queue ESPN last echoed is read first and everything already on it is kept
    behind the names given here.

    `replace=True` sends only these players and drops the rest. It is the old
    behaviour, it is now something you have to ask for, and the result names
    every player it removed.

    With no echo yet on this connection the existing queue is unknown, and a
    merge is refused rather than guessed: sending anyway is exactly how a queue
    the user built gets overwritten without either of us noticing."""
    w, err = _watch_or_error(league_id)
    if err:
        return err
    b = _build_board()
    ids, unresolved = [], []
    for raw in [n.strip() for n in player_names.split(",") if n.strip()]:
        pid, why = bd.resolve_espn_id(raw, b, w.espn_map)
        if pid is None:
            unresolved.append(why)
        else:
            ids.append(pid)
    if unresolved:
        return _emit({"error": "unresolved names; nothing sent", "unresolved": unresolved})
    return _emit(await merge_queue_ids(w, ids, replace=replace, league_id=league_id), indent=2)


async def merge_queue_ids(w, ids: list[int], replace: bool = False,
                          league_id: str = "") -> dict:
    """Put `ids` at the front of the queue ESPN holds, keeping the rest.

    Ids, not names. Name resolution belongs to the tool, where a human typed the
    names; a caller that already has ESPN ids must not be made to render them
    back into text and re-resolve them. That round trip is lossy in exactly the
    population it matters for: `resolve_espn_id` gates on the crosswalk, so a
    player on the board but absent from it -- a kicker, a rookie, a Tuesday
    callup, which is who gets added in the app mid-draft -- comes back
    unresolved, and one of those anywhere in the list refuses the whole send.
    The ids the merge preserves are precisely the ones that never went through
    the crosswalk in the first place.

    What this contributes is the merge itself: wait for ESPN's echo, union with
    what is live, report against what ESPN accepted rather than what was sent.
    """
    if not replace and w.queue is None:
        # ESPN sends the first echo unprompted a few seconds after joining, so a
        # fresh connection is a brief window rather than a state to refuse from.
        # Waiting turns almost every refusal into a normal merge; refusing is
        # what is left when the echo genuinely never comes.
        await _await_first_echo(w)
    existing = list(w.queue) if w.queue is not None else None
    if not replace and existing is None:
        return {
            "error": ("no queue echo on this connection yet; pass replace=True to send "
                      "yours, which overwrites whatever the user holds in the app"),
            "why": ("ESPN's protocol sends the whole queue rather than a change, so "
                    "merging into a queue nobody has seen means guessing at it. "
                    "Sending now would replace what the user built without either of "
                    "us being able to say what was lost."),
            "do": (f"waited {QUEUE_ECHO_WAIT_SECONDS:.0f}s for ESPN's own echo, which "
                   "normally arrives within seconds of joining, and none came; the "
                   "user can touch the queue in the app to force one"),
            "would_send": _queue_rows(w, ids)}

    if replace:
        send = ids
    else:
        # Ours first, in the order asked for, then everything the user already had
        # that we are not already sending.
        send = ids + [pid for pid in (existing or []) if pid not in set(ids)]
    try:
        accepted = await w.set_queue(send)
    except TimeoutError:
        return {"error": "ESPN did not echo the queue within 10s",
                "sent": _queue_rows(w, send)}
    # Both of these read ESPN's echo, not what we meant to send. A merge intends
    # to remove nothing, but ESPN drops ids it rejects -- an already-drafted
    # player is the ordinary case -- and reporting the intent would say
    # `removed: []` while one of the user's players was gone. Describing our own
    # intent instead of the outcome is the defect this tool exists to end.
    kept = [pid for pid in accepted if pid in set(existing or []) and pid not in set(ids)]
    removed = [pid for pid in (existing or []) if pid not in set(accepted)]
    # What ESPN accepted, so a resume after a restart re-sends the queue that is
    # actually live rather than the one this server started with. Only on a send
    # that landed: replacing a good record with the result of a partial failure
    # would degrade the very thing the record exists to protect.
    if league_id and accepted:
        watchstore.update_queue(league_id, accepted, from_user=len(kept))
    return {
        "mode": "replace" if replace else "merge",
        "sent": _queue_rows(w, send),
        "accepted": _queue_rows(w, accepted),
        "added": _queue_rows(w, ids),
        "kept_from_the_users_queue": _queue_rows(w, kept),
        "removed": _queue_rows(w, removed),
        "queue_before": _queue_rows(w, existing or []),
        "echoes_seen": len(w.queue_echoes),
        "accepted_ids": list(accepted),
    }


@mcp.tool()
async def draft_room(league_id: str, chat_limit: int = 10, ctx: Context = None) -> str:
    """Who is in the ESPN draft room right now and the latest room chat, from the
    running watch's socket. Names come from the league's member list."""
    # Takes a session for the channel. A watch resumed at server start has no
    # session to speak through -- there is none until a client sends something --
    # so its "resumed" message waits for the first tool call that brings one.
    if ctx is not None:
        _attach_session(ctx.session)
    entry = _WATCHES.get(league_id)
    if entry is None:
        return _emit({"error": "no active watch for this league; call watch_draft first"})
    w, _task = entry
    return _emit(w.room(chat_limit), indent=2, default=str)


def _waiver_inputs(league_id: str, week: int, season: int):
    """Everything `waivers.waiver_report` needs, assembled from board and ESPN.

    Separate from the tool so the tool is composition: this is the only part
    that touches the network or the caches, and a test replaces it whole.
    """
    from . import lineup, sources, waivers

    league, _ = _settings()
    board = _build_board()
    state = _state()
    players, settings = waivers.fetch_pool_and_settings(league_id, season)
    pool = waivers.free_agents(players)
    if not pool.empty:
        # ESPN's numeric position id, mapped through the board's own table rather
        # than a second copy of it. The copy that stood here was int-keyed while
        # `board._ESPN_POSITION_NAMES` is string-keyed, so the two had already
        # diverged in the only way that matters -- a change to either could not
        # reach the other. Found by marge; it is the `_discount` fork at its
        # beginning.
        #
        # The ids are normalised through `to_numeric` because the column's dtype
        # is decided by the payload: a pull where every row carries the field
        # gives int, and one missing row anywhere makes it float, at which point
        # `str(v)` is "2.0" and matches nothing. A row the table does not cover
        # keeps an empty position rather than a guessed one.
        ids = pd.to_numeric(pool["position_id"], errors="coerce")
        pool["position"] = ["" if pd.isna(v)
                            else bd._ESPN_POSITION_NAMES.get(str(int(v)), "")
                            for v in ids]
    changes = waivers.role_change(sources.weekly_stats([season]),
                                  sources.snap_counts([season]), season, week)
    injury = {} if pool.empty else dict(zip(pool["name"], pool["injury_status"]))
    contingency = waivers.contingent_value(board, waivers.starters_out(board, injury))
    mine = state.my_rows(board)
    # The bench by the league's own slots, not by rank order. What stood here was
    # `mine.iloc[sum(starters):]`, and `my_rows` carries the BOARD's order, so it
    # meant "outside my top n by rank" -- which stops being "not a starter" the
    # moment a roster is unbalanced across positions, and rosters are always
    # unbalanced because people draft best available. On an ordinary
    # receiver-heavy roster it called TE1, K1 and DST1 the bench and offered the
    # only defense as the drop, in a row that simultaneously reported he starts
    # every week. Found by marge; `lineup.droppable` is the shared answer, and
    # #44's set_lineup asks the same question of the same function.
    #
    # `mine` goes to `drop_candidate` WHOLE, alongside the bench, and has to stay
    # that way: `roles.start_probabilities` reads it for "the players I already
    # hold at his position who project for more points than he does". A `mine`
    # trimmed to the bench would price a deep bench player as though the starters
    # ahead of him were absent -- this same defect from the other end.
    bench = lineup.droppable(mine, league) if len(mine) else mine
    # Rows the lineup cannot place carry no usable position, so they are neither
    # starters nor droppable. Reported rather than dropped from the answer: an
    # empty list means the roster is understood, and a non-empty one is a board
    # defect for whoever built it, not a set of players to cut.
    stranded = lineup.unplaceable(mine) if len(mine) else mine
    return {"pool": pool, "changes": changes, "contingency": contingency,
            "league": league, "rules": waivers.league_rules_from_settings(settings),
            "mine": mine, "bench": bench,
            "unplaceable": [str(n) for n in stranded.get("name", [])]}


@mcp.tool()
def waiver_targets(league_id: str, week: int, season: int = CURRENT_SEASON,
                   limit: int = 8) -> str:
    """Who to claim off waivers this week, at what priority, dropping whom.

    A player is in the list for one of two named reasons: his role moved — snap
    and target share against the previous three weeks — or a starter ahead of him
    is out. The two are listed rather than traded off, because trading them off
    needs a rate nobody has measured. A player with neither reason is not a
    claim and is not listed; `census` says how many were considered, so an empty
    list is readable as a quiet week rather than a broken pull.

    **Read the labels.** Three of the four scores carry `unmeasured` in every
    row: role change, projection lag and contingent value have no backtest, so
    the ranking is by a quantity whose predictive value is unknown. Role entropy
    carries its real result. `shape` marks the free-agent pool and the ownership
    move `unverified-shape`: the capture these were written against was taken
    mid-draft, when ESPN reports every player as a free agent, so the split this
    selects on has not been exercised against a real in-season pull.

    **Claim priority is a waiver order, not a bid**, when
    `isUsingAcquisitionBudget` is false — which it is in this league.
    `acquisitionBudget` and `minimumBid` are populated and inert beside it, and
    reading them first is how a tool recommends FAAB to a league that does not
    use it. **Every claim names a drop**, because `isBenchUnlimited` is true
    while there are six bench slots and the slot count is the fact. A drop is
    checked against ESPN's undroppable list (`player.droppable`); a player the
    pull did not carry is offered with `undroppable_checked` false rather than
    assumed droppable.

    The drop comes from the players `lineup.droppable` says the league's slots
    are filled without -- by position against `league.starters`, never by board
    rank. Rank order is not lineup order, and on a receiver-heavy roster the two
    differ by the whole tail: the only kicker and the only defense are the lowest
    rows on any roster and are starters on every one of them. Roster rows the
    lineup cannot place are named in `unplaceable_on_my_roster` rather than being
    treated as spare.
    """
    from . import waivers

    try:
        parts = _waiver_inputs(league_id, week, season)
    except Exception as exc:
        return _emit({"error": f"could not assemble the waiver inputs: "
                               f"{type(exc).__name__}: {exc}"}, indent=2)
    out = waivers.waiver_report(parts["pool"], parts["changes"], parts["contingency"],
                                parts["league"], parts["rules"], parts["mine"],
                                parts["bench"], limit=limit)
    return _emit({"week": week, "season": season,
                  "claim_priority_basis": parts["rules"].priority_basis,
                  "bench_slots": parts["rules"].bench_slots,
                  # Empty on a roster the board understands. Non-empty means
                  # those players were left out of both the lineup and the drop
                  # candidates, which is worth seeing rather than inferring.
                  "unplaceable_on_my_roster": parts["unplaceable"], **out}, indent=2)


@mcp.tool()
def draft_room_stats(league_id: str = "", dump_dir: str = "") -> str:
    """Who was in the ESPN draft room, for how long, and who talked. Per member,
    by team and owner name: minutes in the room, joins and leaves with each
    session, messages sent (and the last one), busiest hours in local time,
    first and last seen, picks made and the seconds each took from the clock
    starting, plus league activity from the read API. Uses the running watch for
    `league_id` when there is one, else the dump directory (`dump_dir`, or the
    newest `espn_dump_*` under the working directory). `table` is the same
    numbers as plain text and `definitions` says what each number means, which
    is worth reading: `clock_to_pick` cannot tell an autopick from a person, and
    `active_hours` counts league activity that `first_seen` deliberately does
    not. SWIDs are never reported."""
    from . import roomstats

    entry = _WATCHES.get(league_id) if league_id else None
    if entry is not None:
        w, _task = entry
        log = roomstats.from_watch(w)
    else:
        root = roomstats.find_dump(dump_dir or ".")
        if root is None:
            return _emit({"error": "no watch for this league and no espn_dump_* directory; "
                                        "call watch_draft or dump_draft first, or pass dump_dir"})
        log = roomstats.from_dump(root)
    stats = roomstats.room_stats(log)
    stats["table"] = roomstats.format_table(stats)
    return _emit(stats, indent=2)


@mcp.tool()
def draft_replay(league_id: str = "", picks: int = 0, as_of: bool = False) -> str:
    """Replay every recorded pick through the model for the team that made it:
    the model's choice at that moment, the model's rank of the real pick,
    projected points left on the table, and the reach against ADP. Totals per
    team, and the survival model's calibration (predicted vs observed odds a
    player lasted to that team's next pick, with Brier score against the base
    rate). `picks` limits the per-pick rows returned (0 = all). Projections
    and ADP are today's; kickers, defenses and unmodelled players are
    `off_board`.

    With `as_of` each pick is priced from the market snapshot the watch wrote
    when that pick was on the clock — ESPN's ADP, PPR rank and projection as
    they stood then, not as they stand now — for the league whose `league_id`
    you pass. Snapshots only exist from the moment a watch first connected and
    reach the top few hundred available players, so the answer carries an
    `as_of` block saying how many picks were covered and how much of each pool;
    anything uncovered keeps today's numbers."""
    from . import replay, watch

    state = _state()
    b = _build_board()
    league = _settings()[0]
    drift = replay.room_drift(b, state)["shift"]
    snapshots = watch.snapshot_dir(league_id) if league_id else None
    if as_of and snapshots is None:
        return _emit({"error": "as_of needs league_id: snapshots are filed per league "
                                    "under ~/.ffdraft/state/snapshots_<league>/"})
    out = replay.replay_draft(b, state, league, adp_shift=drift,
                              as_of=as_of, snapshots=snapshots)
    out["calibration_without_shift"] = replay.replay_draft(b, state, league)["overall"]
    entry = _WATCHES.get(league_id) if league_id else None
    if entry is not None:
        w, _task = entry
        labels = {slot: w.team_label(team) for team, slot in w.slot_of.items()}
        for t in out["teams"]:
            t["team"] = labels.get(t["slot"], f"slot {t['slot']}")
    if picks:
        out["picks"] = out["picks"][-picks:]
    return _emit(out, indent=2, default=str)


@mcp.tool()
def stream_kdst(league_id: str, week: int, season: int = CURRENT_SEASON,
                look_ahead: int = 2, top: int = 0, detail: bool = False) -> str:
    """Which kicker and defence to start or pick up **this week**, by that
    week's matchup — not by season projection and not by the draft's supply
    model, neither of which answers "is this a good week for this player".

    Ranked on the implied points the book has posted for the game: a defence
    wants an opponent expected to score little, a kicker wants his own offence
    expected to score a lot. `line_basis` on every row says whether a line
    existed; books post them a few weeks out, and a row without one is never
    given a season number in its place.

    **Read `margin_units` per position before the margins.** The score is
    calibrated to this league's own K and D/ST scoring bands against real
    results, in two disjoint blocks of weeks, and a margin is reported in points
    only when every coefficient keeps its sign across both blocks *and* each
    block predicts the other better than its own average does. Where it does
    not, the ranking still stands and the margin is withheld rather than dressed
    up as points. On the current data defences calibrate and kickers do not.

    `look_ahead` weeks are returned beside this one, so a waiver claim can be
    judged against the bye it has to cover. Weather is not available: `temp` and
    `wind` are recorded after kickoff, so only the stadium roof is known in
    advance.

    `top` is how many rows per position the asked week returns, 8 by default,
    with `ranked_of` giving the full field. The look-ahead weeks carry only the
    columns a bye question needs. `detail=true` restores the whole field, the
    full look-ahead rows and the calibration evidence behind each verdict, and
    is the reason the default is small rather than the whole of it."""
    from . import stream

    league, _ = _settings()
    state = _state()
    b = _mark_drafted(_build_board(), state)
    try:
        rules = bd.espn_league_rules(league_id, season)
        scoring = rules["scoring"]
        bands = (scoring.get("slot_overrides") or {}).get("DST") or {}
        items = scoring.get("kicker_and_dst_items") or {}
    except Exception as exc:
        return _emit({"error": f"could not read this league's K/D-ST scoring: "
                               f"{type(exc).__name__}: {exc}"})
    if not bands:
        return _emit({"error": "this league publishes no D/ST scoring bands; the "
                               "ranking would have nothing to calibrate against"})

    free = b[~b["drafted"]]
    available = {
        "DST": [str(t) for t in free[free["position"] == "DST"]["team"].dropna().unique()],
        "K": [str(n) for n in free[free["position"] == "K"]["name"]],
    }
    team_of = {str(r["name"]): str(r["team"])
               for _, r in free[free["position"] == "K"].iterrows()
               if pd.notna(r.get("team"))}
    mine = state.my_rows(b)
    starters = {}
    for pos in ("DST", "K"):
        held = mine[mine["position"] == pos]
        if len(held):
            row = held.iloc[0]
            starters[pos] = str(row["team"]) if pos == "DST" else str(row["name"])
    out = stream.stream_kdst(season, week, bands, items, available,
                             history_seasons=[season - 1], starters=starters,
                             team_of=team_of, look_ahead=max(1, look_ahead))
    return _emit(_jsonable(stream.compact(out, top=top or stream.TOP_N,
                                          detail=detail)), indent=2)


@mcp.tool()
def draft_retrospective(league_id: str = "", slot: int = 0, around: int = 2) -> str:
    """Your draft, pick by pick, against what the model would have taken.

    Each of your picks is replayed twice: priced from the market snapshot the
    watch recorded at that pick, and priced from today's board. `basis` says
    which per row, because snapshots only exist from the pick at which a watch
    first connected — earlier picks cannot be priced as of the time and fall
    back to today's board. Read `as_of_coverage` before the deltas.

    `your_pick_edge` is your pick's projection minus the model's, so positive
    means your pick projects more. A projection, not a result.
    `your_pick_edge_actual` is the same comparison on real box scores and is
    null on every row until the season has played a week; `delta_basis` names
    which the table is standing on. `room_around` gives the picks either side
    of each of yours, so a reach or a run is visible.

    `league_id` locates the snapshots; without it every row is priced from
    today's board. `slot` defaults to yours."""
    from . import replay, watch

    state = _state()
    b = _build_board()
    league = _settings()[0]
    out = replay.draft_retrospective(
        b, state, league, slot=(slot or None), around=around,
        snapshots=(watch.snapshot_dir(league_id) if league_id else None))
    entry = _WATCHES.get(league_id) if league_id else None
    if entry is not None:
        w, _task = entry
        team_of_slot = {s: t for t, s in w.slot_of.items()}
        if out["slot"] in team_of_slot:
            out["team"] = w.team_label(team_of_slot[out["slot"]])
    return _emit(_jsonable(out), indent=2)


@mcp.tool()
def draft_counterfactual(slot: int = 0, league_id: str = "", policy: str = "argmax",
                         seed: int = 0) -> str:
    """SIMULATION. Replay the draft with the model drafting for `slot` (yours by
    default): at each of that team's turns the model picks for its simulated
    roster, that pick changes what is left for everyone after it, and every
    other team takes the walk-forward blend predictor's choice among the players
    still available — fitted prequentially on the real picks, so nothing from
    later in the draft leaks in. `policy` is `argmax` (the likeliest player,
    deterministic) or `sample` (drawn from the distribution, with `seed`).

    Returns three rosters for that slot — `model_roster`, `control_roster` (the
    same simulated room with the team mirroring its real picks) and
    `real_roster` — plus `starters_proj` for each. **Read
    `starters_proj.delta_vs_control`**: it holds the room fixed and is the
    intervention alone. `delta_vs_real` also carries the difference between the
    predictor's room and the real one, which `divergence` sizes and which is
    usually the larger term. `substitutions` gives every one of that team's
    turns with the real, model and control picks side by side.

    Real picks the board cannot model (kickers, defenses) are mirrored rather
    than predicted for the other teams; at the target slot the model picks from
    the board every turn. This is not a measurement: it assumes the rest of the
    room behaves like the predictor and prices everything with today's
    projections and ADP."""
    from . import replay

    state = _state()
    b = _build_board()
    league = _settings()[0]
    slot = slot or state.my_slot
    out = replay.counterfactual_draft(b, state, league, slot, policy=policy, seed=seed)
    entry = _WATCHES.get(league_id) if league_id else None
    if entry is not None:
        w, _task = entry
        team_of_slot = {s: t for t, s in w.slot_of.items()}
        if slot in team_of_slot:
            out["team"] = w.team_label(team_of_slot[slot])
    return _emit(out, indent=2, default=str)


@mcp.tool()
def predict_pick(league_id: str = "", slot: int = 0) -> str:
    """For the team on the clock (or `slot`): what the model would take for
    their roster (`should`), the next names on ESPN's own list (`espn_list`),
    how that team has been choosing (median number of better-ranked ESPN
    players it passed on, positions taken), and a prediction that follows
    whichever list the team follows. Names come from the running watch for
    `league_id`. ESPN rank is today's."""
    from . import replay

    state = _state()
    b = _build_board()
    league = _settings()[0]
    slot = slot or state.slot_for_pick(state.on_the_clock)
    shift = replay.room_drift(b, state)["shift"]
    out = replay.predict_pick(b, state, league, slot, adp_shift=shift)
    if slot == state.slot_for_pick(state.on_the_clock):
        # The walk-forward predictors, scored out of sample on every pick so
        # far, and their forecast for this one.
        rp = replay.replay_draft(b, state, league, adp_shift=shift)
        out["forecast"], out["predictors"] = rp.get("forecast"), rp["predictors"]
    entry = _WATCHES.get(league_id) if league_id else None
    if entry is not None:
        w, _task = entry
        team_of_slot = {s: t for t, s in w.slot_of.items()}
        if slot in team_of_slot:
            out["team"] = w.team_label(team_of_slot[slot])
    return _emit(out, indent=2, default=str)


@mcp.tool()
def draft_strength(league_id: str = "") -> str:
    """Every team's draft so far, ranked by projected starter points under the
    league's starting slots, with bench projection, open starter slots and pick
    count. Team names come from the running watch for `league_id` when there
    is one; otherwise teams are labelled by draft slot."""
    state = _state()
    b = _build_board()
    labels: dict[int, str] = {}
    entry = _WATCHES.get(league_id) if league_id else None
    if entry is not None:
        w, _task = entry
        labels = {slot: w.team_label(team) for team, slot in w.slot_of.items()}
    tbl = bd.team_strength(b, state, labels)
    return _emit({"picks_made": len(state.picks), "my_slot": state.my_slot,
                       "teams": tbl.to_dict(orient="records")}, indent=2)


@mcp.tool()
def evaluate_trade(give: str, get: str, counterparty_slot: int = 0,
                   league_id: str = "", n_trials: int = 0, blocks: int = 0,
                   seed: int = 0) -> str:
    """Score a proposed trade for both sides over the rest of the season.

    `give` and `get` are comma-separated player names: `give` leaves your roster,
    `get` arrives on it. `counterparty_slot` is their draft slot; with a running
    watch for `league_id` the teams are named too.

    Each side is simulated week by week on its own starting lineup, with byes and
    injury availability, and reported as points before and after with the spread
    between disjoint seed blocks beside it. A side whose blocks disagree in sign
    is reported as no call rather than as a win: that difference is inside the
    harness's own noise. Both sides can gain, because the same player is worth
    different points to two different lineups.

    Rosters come from the draft record, so a player added after the draft is not
    on it yet."""
    state = _state()
    b = _build_board()
    give_names = [n.strip() for n in give.split(",") if n.strip()]
    get_names = [n.strip() for n in get.split(",") if n.strip()]
    by_slot: dict[int, list[dict]] = {}
    for p in state.picks:
        by_slot.setdefault(p["slot"], []).append(p)
    if counterparty_slot == state.my_slot:
        return _emit({"ok": False,
                      "errors": [f"counterparty_slot {counterparty_slot} is your own slot"]},
                     indent=2)
    out = trade.evaluate(
        b, by_slot, state.league, state.my_slot, counterparty_slot,
        give_names, get_names,
        n_trials=n_trials or trade.DEFAULT_TRIALS,
        blocks=blocks or trade.DEFAULT_BLOCKS, seed=seed)
    entry = _WATCHES.get(league_id) if league_id else None
    if entry is not None and out.get("ok"):
        w, _task = entry
        labels = {slot: w.team_label(team) for team, slot in w.slot_of.items()}
        out["you"]["team"] = labels.get(state.my_slot)
        out["counterparty"]["team"] = labels.get(counterparty_slot)
    return _emit(out, indent=2, default=str)


@mcp.tool()
async def dump_draft(league_id: str, out_dir: str = ".", season: int = CURRENT_SEASON) -> str:
    """Write everything ESPN reports about this league's draft under
    `<out_dir>/espn_dump_<league>_<season>_<stamp>/`: every read-API view as
    its own JSON file, the player pool with ownership and ADP, league history,
    and the draft room's INIT snapshot raw and decoded, plus a timestamped log
    of every socket line the running watch has received. `live/state.json` is
    the draft as it stands now; `live/init.json` and `live/picks.json` are the
    join snapshot, which the socket never resends. The manifest carries each
    file's as-of pick count and whether the state reconciles with the read
    API's `mDraftDetail`. Uses the watch's socket when one is running;
    otherwise opens the room once, which bumps any other connection for your
    team. Returns the manifest with the absolute path."""
    import asyncio
    import os

    from . import espn_dump

    entry = _WATCHES.get(league_id)
    init_b64 = lines = None
    team_id = None
    if entry is not None:
        w, _task = entry
        init_b64, lines = w.init_b64, list(w.lines)
    else:
        info = bd.espn_league_context(league_id, season, os.environ.get("ESPN_SWID"),
                                      os.environ.get("ESPN_S2"))
        team_id = info["my_team_id"]
    manifest = await asyncio.to_thread(
        espn_dump.dump_draft, league_id, out_dir, season, None, None, init_b64, lines, team_id)
    return _emit(manifest, indent=2)


@mcp.tool()
async def stop_watch(league_id: str) -> str:
    """Stop the draft-room watch for a league."""
    # Cleared even when no watch is running here: the record may have been left
    # by a process that has since died, and this is the user saying stop.
    watchstore.mark_stopped(league_id)
    entry = _WATCHES.pop(league_id, None)
    if entry is None:
        return _emit({"stopped": False, "watching": sorted(_WATCHES)})
    w, task = entry
    task.cancel()
    # as_of_snapshots next to picks_seen on purpose: "picks_seen 122,
    # as_of_snapshots 0" is the one line that says the market was never recorded.
    return _emit({"stopped": True, "league": league_id, "picks_seen": w.picks_seen,
                       "as_of_snapshots": len(w.snapshots),
                       "snapshot_write_failures": w.snapshot_failures,
                       "last_line": w.last_line[:80]})


# Package modules in dependency order, so a reload of each sees reloaded
# dependencies. server.py itself is reloaded last, in place.
RELOAD_ORDER = ("names", "config", "sources", "features", "rookies", "separation",
                "model", "adp", "board", "espn_live", "espn_dump", "choice", "replay",
                "watch", "roomstats", "roles", "lineup", "rosters", "stream",
                "trade", "waivers", "watchstore")


def _sync_tools(live: Any, fresh: Any) -> dict[str, list[str]]:
    """Make the running server's tool registry match a freshly imported one:
    every tool re-registered from the new function objects, tools that no
    longer exist removed. The running server object is what the transport
    holds; a re-executed module builds a new one that nothing serves."""
    live_names = {t.name for t in live._tool_manager.list_tools()}
    fresh_tools = fresh._tool_manager.list_tools()
    fresh_names = {t.name for t in fresh_tools}
    for name in sorted(live_names - fresh_names):
        live.remove_tool(name)
    for t in fresh_tools:
        if t.name in live_names:
            live.remove_tool(t.name)
        live.add_tool(t.fn, name=t.name, title=t.title, description=t.description,
                      annotations=t.annotations, icons=t.icons, meta=t.meta)
    return {"added": sorted(fresh_names - live_names),
            "removed": sorted(live_names - fresh_names),
            "reloaded": sorted(fresh_names & live_names)}


def _watch_refresh() -> tuple:
    """The board and bye weight as they are NOW, for a running watch.

    A module-level function rather than a lambda built at the call site, and
    named in `watch.REBOUND_CODE`, so a reload can put the current one onto a
    watch that predates it. Two call sites built the identical lambda before,
    which is the duplication rule's own example: two copies agree until one is
    changed.
    """
    return (_build_board(), _settings()[1].bye)


def _migrate_watches(errors: dict[str, str]) -> dict[str, Any]:
    """Bring every live watch onto the reloaded class.

    `reload_code` promises the running watch survives, and until now that was
    true only of the object's identity. The instance still pointed at the class
    object the old module defined, so it kept running the old methods, and it
    lacked every attribute added to `__init__` since the draft started. Measured
    live: `draft_queue` raised on a missing `queue_echoes`, and the reader loop
    would have raised on ESPN's next echo.

    Never raises. A reload that cannot migrate must still return, because the
    alternative is a half-reloaded server.
    """
    from . import watch as watch_mod

    out: dict[str, Any] = {"migrated": {}, "failed": {}}
    for league_id, entry in list(_WATCHES.items()):
        try:
            w, _task = entry
        except (TypeError, ValueError) as exc:
            # Not skipped quietly. This function's whole job is to say what it
            # could not do, and an entry it cannot even unpack is the loudest
            # version of that.
            out["failed"][league_id] = f"unusable registry entry: {exc}"
            errors[f"watch:{league_id}"] = f"unusable registry entry: {exc}"
            continue
        try:
            # `_channel` and `_watch_refresh` are looked up here, in the module
            # that has just been reloaded, so they are the current bodies. A
            # callable held in an attribute is not touched by rebinding
            # `__class__`, so without this the watch keeps running the old ones.
            report = watch_mod.migrate_instance(
                w, watch_mod.DraftWatch,
                code={"notify": _channel, "refresh": _watch_refresh})
            report["record"] = _ensure_watch_record(w, league_id)
            out["migrated"][league_id] = report
        except Exception as exc:
            _log.exception("could not migrate the watch for league %s", league_id)
            out["failed"][league_id] = f"{type(exc).__name__}: {exc}"
            errors[f"watch:{league_id}"] = f"{type(exc).__name__}: {exc}"
    return out


def _ensure_watch_record(w: Any, league_id: str) -> str:
    """Give a live watch a resume record if it has none.

    A watch started before the record existed -- or by any path that does not
    write one -- runs perfectly and would not come back after a restart, and
    nothing said so: `update_queue` and `mark_stopped` both answer None when
    there is no record, so the silence looked like success.

    An existing record is left alone, which is what makes `stop_watch` win: it
    clears the flag rather than deleting the file, so a stopped watch is
    `present` here and is not resurrected by the next reload.
    """
    try:
        if watchstore.load(league_id) is not None:
            return "present"
        watchstore.save(watchstore.WatchRecord(
            league_id=league_id,
            team_id=int(getattr(w, "team_id", 0) or 0),
            season=int(getattr(w, "season", CURRENT_SEASON) or CURRENT_SEASON),
            queue=list(getattr(w, "queue", None) or []),
        ))
        return "written"
    except Exception as exc:
        _log.exception("could not write a resume record for league %s", league_id)
        return f"failed: {type(exc).__name__}: {exc}"


def reload_package() -> dict[str, Any]:
    """Re-import every ffdraft module from disk, this one last, and point the
    module's `mcp` back at the server object the transport is serving, with
    its tools replaced by the reloaded functions. Watches, caches and boards
    persist (see the globals().get guards above). Returns what changed and
    any module that failed to import, which leaves the previous code in place
    for that module."""
    import importlib
    import sys

    live = mcp
    errors: dict[str, str] = {}
    for name in RELOAD_ORDER:
        module = sys.modules.get(f"ffdraft.{name}")
        if module is None:
            continue
        try:
            importlib.reload(module)
        except Exception as exc:
            errors[name] = f"{type(exc).__name__}: {exc}"
    watches = _migrate_watches(errors)
    me = sys.modules[__name__]
    # Launched as `python -m ffdraft.server` -- which is how .mcp.json starts it
    # -- this module runs as `__main__` and `ffdraft.server` is never entered
    # into sys.modules at all. `importlib.reload` resolves `__spec__.name` and
    # requires that name to map to this module, so it raised "module
    # ffdraft.server not in sys.modules" while every package module around it
    # reloaded fine: the live process then served new package code under the old
    # server.py, and reported success for the modules it did manage.
    #
    # Registering the module under its spec name is what an ordinary import
    # would have left behind. Reload then renames the module to that name as
    # well (`_init_module_attrs` runs before the body), so a second reload finds
    # it by the ordinary route and this branch does not fire again.
    #
    # One thing in the package reads `__name__` after startup and so changes
    # with it: `logging.getLogger(__name__)` in bye_backtest's progress
    # callback, whose records move from a logger named `__main__` to one named
    # `ffdraft.server`. Nothing configures logging by name anywhere -- no
    # basicConfig, dictConfig, setLevel or handler bound to a name -- so both
    # propagate to the root identically and only the record's `name` field
    # differs. The other reads are this line, the entry-point guard below, and
    # watch.py's own module logger, which is unaffected.
    spec = getattr(me, "__spec__", None)
    if spec is not None and sys.modules.get(spec.name) is not me:
        sys.modules[spec.name] = me
    try:
        importlib.reload(me)
    except Exception as exc:
        errors["server"] = f"{type(exc).__name__}: {exc}"
        return {"errors": errors, "tools": None}
    # The module namespace, not attribute access: the re-executed module bound
    # a fresh server object here, and the transport serves `live`.
    changes = _sync_tools(live, me.__dict__["mcp"])
    me.__dict__["mcp"] = live
    return {"errors": errors, "tools": changes, "watches": watches}


@mcp.tool()
async def reload_code(ctx: Context = None) -> str:
    """Reload this server's code from disk without a reconnect: every ffdraft
    module is re-imported, the tool list is rebuilt from the new functions,
    and `notifications/tools/list_changed` is sent so Claude Code refreshes
    it. A module that fails to import keeps its previous code and is reported.

    The running draft watch, its socket and your ESPN queue survive. The watch is
    moved onto the reloaded class, so its own methods become the new code; the
    state the new `__init__` sets is added; and the two callables it holds -- the
    channel notifier and the board refresh -- are rebound to this module's
    current ones, since rebinding a class does not touch a function stored in an
    attribute. Everything the draft built (picks, queue, lines, snapshots) is
    left exactly as it was. A live watch with no resume record on disk is given
    one, so a later restart brings it back.

    `watches` in the result says per league what was added, what was rebound,
    whether a record was written or already present, and names anything that
    could not be rebuilt rather than guessing at it."""
    import traceback

    try:
        result = reload_package()
    except Exception as exc:
        return _emit({"error": f"{type(exc).__name__}: {exc}",
                           "traceback": traceback.format_exc()})
    if result["tools"] is not None and ctx is not None:
        await ctx.session.send_tool_list_changed()
        result["notified"] = "notifications/tools/list_changed"
    return _emit(result, indent=2)


async def resume_watch(record: watchstore.WatchRecord) -> dict:
    """Bring one persisted watch back: join the room, then re-send its queue.

    The queue goes through `set_draft_queue`'s merge path, not straight down the
    socket, so anything the user has queued in the ESPN app since the old process
    died is kept. That path waits for ESPN's own echo first, which is the whole
    reason it is safe to re-send a queue nobody has looked at in minutes.

    Returns what happened. Nothing here raises: this runs at server start, and a
    league that cannot be resumed must not stop the server or the other leagues.
    """
    import asyncio
    import os

    from . import watch as watch_mod

    out: dict = {"league_id": record.league_id, "resumed": False}
    swid, espn_s2 = os.environ.get("ESPN_SWID"), os.environ.get("ESPN_S2")
    if not (swid and espn_s2):
        return await _refused(out, "ESPN_SWID and ESPN_S2 are not set")
    if record.league_id in _WATCHES:
        # A client can call watch_draft for this league while the resume is still
        # joining: the resume task starts before the transport does. Overwriting
        # _WATCHES would leak a socket, and ESPN answers two connections on one
        # team with `LEFT <team> <swid> 2`, which the watch reads as a pause -- so
        # the survivor can pause itself.
        return await _refused(out, "a watch for this league is already running")
    try:
        ctx_info = bd.espn_league_context(record.league_id, record.season, swid, espn_s2)
        ok, why = watchstore.resumable(record, draft_complete=bool(ctx_info.get("drafted")))
        if not ok:
            return await _refused(out, why, quiet=why.startswith("stopped by"))
        league, weights = _settings()
        directory = bd.espn_league_directory(record.league_id, record.season, swid, espn_s2)
        w = watch_mod.DraftWatch(
            record.league_id, record.season, record.team_id, swid, espn_s2,
            league, _build_board(), _channel, directory=directory,
            bye_weight=weights.bye,
            refresh=_watch_refresh)
        task = asyncio.create_task(w.run(), name=f"draft-watch-{record.league_id}")
        _WATCHES[record.league_id] = (w, task)
        ready = asyncio.ensure_future(w.ready.wait())
        # Whichever comes first: INIT, or the watch dying. The timeout decides
        # what is SAID now, not what the watch is: the socket is up either way
        # and it is the socket that stops picks being missed, which is the one
        # loss a draft cannot recover from. Cancelling here to make a "not
        # resumed" message true would throw away the thing worth keeping.
        done, _pending = await asyncio.wait(
            {ready, task}, timeout=RESUME_READY_SECONDS,
            return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            ready.cancel()
            raise RuntimeError("the watch stopped before ESPN sent INIT")
        if ready not in done:
            # Still joining. The state says a watch exists because one does, and
            # the message says so too rather than claiming a failure that would
            # contradict what `draft_room` and `draft_status` will answer.
            #
            # This future is dropped: `_finish_resume` builds its own. Left
            # pending it outlives the call and a room that never sends INIT ends
            # the process with "Task was destroyed but it is pending".
            ready.cancel()
            asyncio.create_task(_finish_resume(w, task, record),
                                name=f"ffdraft-finish-resume-{record.league_id}")
            # Not `resumed: False`. The watch is up; what is outstanding is the
            # draft state. A boolean that says "no" beside a `why` saying "joined"
            # is the same field-against-field contradiction this branch exists to
            # remove, and living in a field nobody reads yet is not a defence.
            out["resumed"] = "joining"
            out["why"] = (f"joined; ESPN had not sent INIT after "
                          f"{RESUME_READY_SECONDS:.0f}s, so the picks and the queue "
                          f"are still to come")
            await _channel(
                f"watch rejoined league {record.league_id} but ESPN has not sent the "
                f"draft state yet; picks are being recorded and the queue will be "
                f"re-sent when it arrives",
                {"league": record.league_id, "event": "resuming"})
            return out
    except Exception as exc:
        _log.exception("could not resume the watch for league %s", record.league_id)
        _drop_watch(record.league_id)
        return await _refused(out, f"{type(exc).__name__}: {exc}")

    summary = w.state.summary()
    out.update({"resumed": True, "picks_made": summary["picks_made"],
                "my_next_pick": summary["my_next_pick"]})
    if record.queue:
        try:
            # Ids straight through. Rendering them into names and re-resolving
            # would drop the whole queue over one player the crosswalk lacks, and
            # the entries a merge preserves are exactly the ones that never went
            # through the crosswalk.
            sent = await merge_queue_ids(w, list(record.queue),
                                         league_id=record.league_id)
            out["queue"] = {"entries": len(sent.get("accepted") or []),
                            "from_the_user": len(sent.get("kept_from_the_users_queue") or []),
                            "error": sent.get("error")}
        except Exception as exc:
            _log.exception("could not re-send the queue for league %s", record.league_id)
            out["queue"] = {"error": f"{type(exc).__name__}: {exc}"}
    await _channel(_resume_message(out), {"league": record.league_id, "event": "resumed"})
    return out


def _drop_watch(league_id: str) -> None:
    """Take a half-started watch back out and cancel it.

    A refusal must not leave an entry behind: `draft_room` and `draft_status`
    answer from `_WATCHES`, so a watch the user has been told does not exist
    would still be answering questions.
    """
    entry = _WATCHES.pop(league_id, None)
    if entry is None:
        return
    _w, task = entry
    if task is not None and hasattr(task, "cancel"):
        task.cancel()


async def _finish_resume(w, task, record: watchstore.WatchRecord) -> None:
    """Finish a resume whose INIT was slow: re-send the queue and say so.

    Without this the slow-INIT path leaves a live watch and a queue that is never
    restored, which is half the feature silently missing -- the shape of the
    defect this whole task is about. Bounded by the watch's own life: if the
    watch stops before INIT arrives there is nothing to finish and this exits.
    """
    import asyncio

    ready = asyncio.ensure_future(w.ready.wait())
    done, pending = await asyncio.wait({ready, task}, return_when=asyncio.FIRST_COMPLETED)
    for p in pending:
        p.cancel()
    if ready not in done:
        return
    out: dict = {"league_id": record.league_id, "resumed": True}
    try:
        summary = w.state.summary()
        out.update({"picks_made": summary["picks_made"],
                    "my_next_pick": summary["my_next_pick"]})
        if record.queue:
            sent = await merge_queue_ids(w, list(record.queue), league_id=record.league_id)
            out["queue"] = {"entries": len(sent.get("accepted") or []),
                            "from_the_user": len(sent.get("kept_from_the_users_queue") or []),
                            "error": sent.get("error")}
    except Exception as exc:
        _log.exception("could not finish the resume for league %s", record.league_id)
        await _channel(f"watch for league {record.league_id} joined, but finishing the "
                       f"resume failed: {type(exc).__name__}: {exc}",
                       {"league": record.league_id, "event": "resume_failed"})
        return
    await _channel(_resume_message(out), {"league": record.league_id, "event": "resumed"})


async def _refused(out: dict, why: str, quiet: bool = False) -> dict:
    """Record a refusal and say it out loud.

    Returning the reason to a caller that discards it is the same silence this
    module exists to end: `resume_watches` is started as a task and nobody reads
    its list. A user whose watch did not come back is owed the reason on the
    channel, not in a log nobody reads on stdio.

    `quiet` is for the one refusal the user already knows about, their own
    `stop_watch`. Announcing that on every start would be noise forever, for
    every league they have ever stopped.
    """
    out = {**out, "why": why}
    if not quiet:
        await _channel(f"watch NOT resumed for league {out['league_id']}: {why}",
                       {"league": str(out["league_id"]), "event": "not_resumed"})
    return out


def _resume_message(out: dict) -> str:
    """The one line the user gets. Says what came back and what it cost them."""
    head = (f"watch resumed after restart: {out['picks_made']} picks made, "
            f"your next pick is {out['my_next_pick']}")
    queue = out.get("queue")
    if queue is None:
        return head + "; no queue to re-send"
    if queue.get("error"):
        return head + f"; the queue was NOT re-sent ({queue['error']})"
    return (head + f"; queue re-sent, {queue['entries']} entries, "
            f"{queue['from_the_user']} of them yours")


async def resume_watches() -> list[dict]:
    """Resume every persisted watch. Reported, never raised.

    Sequential on purpose: ESPN allows one draft-room connection per team, and
    joining several rooms at once is a good way to bump something.
    """
    records, skipped = watchstore.load_all()
    out = []
    for name in skipped:
        # A record nobody can read is a watch that will not come back. It has no
        # league id to name, so the file is what gets named.
        out.append(await _refused({"league_id": name, "resumed": False},
                                  "its record could not be read"))
    for record in records:
        out.append(await resume_watch(record))
    return out


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
