"""Draft board state and live sync with drafting platforms."""
from __future__ import annotations

import io
import json
import os
import re
import time

import numpy as np
import pandas as pd
import requests

from . import names, sources
from .config import CURRENT_SEASON, SPECIAL_POSITIONS, STATE_DIR, LeagueSettings

# Name handling lives in names.py so every join in the codebase resolves identically.
norm_name = names.normalize

_INDEX_CACHE: dict[str, names.PlayerIndex] = {}

# ESPN's proTeamId -> franchise name, from the proTeamSchedules view (stable
# reference data, not worth a network round trip on every sync). Team defenses
# in draft picks are encoded as -(16000 + proTeamId) rather than a real playerId.
_ESPN_PRO_TEAMS = {
    1: "Atlanta Falcons", 2: "Buffalo Bills", 3: "Chicago Bears",
    4: "Cincinnati Bengals", 5: "Cleveland Browns", 6: "Dallas Cowboys",
    7: "Denver Broncos", 8: "Detroit Lions", 9: "Green Bay Packers",
    10: "Tennessee Titans", 11: "Indianapolis Colts", 12: "Kansas City Chiefs",
    13: "Las Vegas Raiders", 14: "Los Angeles Rams", 15: "Miami Dolphins",
    16: "Minnesota Vikings", 17: "New England Patriots", 18: "New Orleans Saints",
    19: "New York Giants", 20: "New York Jets", 21: "Philadelphia Eagles",
    22: "Arizona Cardinals", 23: "Pittsburgh Steelers", 24: "Los Angeles Chargers",
    25: "San Francisco 49ers", 26: "Seattle Seahawks", 27: "Tampa Bay Buccaneers",
    28: "Washington Commanders", 29: "Carolina Panthers", 30: "Jacksonville Jaguars",
    33: "Baltimore Ravens", 34: "Houston Texans",
}


def _board_fingerprint(table: pd.DataFrame) -> str:
    """Cheap content signature. Keying on id() would be wrong as well as slow —
    CPython recycles ids, so a rebuilt board can land on a freed id and get served
    a stale index belonging to a different set of players."""
    if "name" not in table.columns or table.empty:
        return f"empty:{len(table)}"
    names_col = table["name"]
    return f"{len(table)}:{hash(names_col.iloc[0])}:{hash(names_col.iloc[-1])}"


def player_index(table: pd.DataFrame) -> names.PlayerIndex:
    """Alias index for a board, cached on the board's contents."""
    key = _board_fingerprint(table)
    idx = _INDEX_CACHE.get(key)
    if idx is None:
        idx = names.PlayerIndex(table)
        _INDEX_CACHE.clear()   # only one board is live at a time
        _INDEX_CACHE[key] = idx
    return idx


def match_player(query: str, table: pd.DataFrame,
                 position: str | None = None) -> pd.Series | None:
    """Resolve a free-text name to a row, tolerating nicknames, suffixes and typos."""
    row, _ = player_index(table).resolve(query, position)
    return row


def match_player_verbose(query: str, table: pd.DataFrame,
                         position: str | None = None) -> tuple[pd.Series | None, str]:
    """Same, but also reports how the match was made."""
    return player_index(table).resolve(query, position)


# ---------------------------------------------------------------- ADP

FANTASYPROS_ADP = {
    "half_ppr": "https://www.fantasypros.com/nfl/adp/half-point-ppr-overall.php",
    "ppr": "https://www.fantasypros.com/nfl/adp/ppr-overall.php",
    "standard": "https://www.fantasypros.com/nfl/adp/overall.php",
}


def load_espn_adp(league_id: str, season: int = CURRENT_SEASON,
                  swid: str | None = None, espn_s2: str | None = None) -> pd.DataFrame:
    """ESPN's own average draft position for every rostered-or-not player, from the
    league's `kona_player_info` view: `player.ownership.averageDraftPosition`.

    This is the list your ESPN opponents draft from, so it is the right input to
    survival odds in an ESPN league; consensus rank is a different market.
    """
    swid = swid or os.environ.get("ESPN_SWID")
    espn_s2 = espn_s2 or os.environ.get("ESPN_S2")
    cookies = {}
    if swid and espn_s2:
        cookies = {"SWID": swid if swid.startswith("{") else f"{{{swid}}}",
                   "espn_s2": espn_s2}
    url = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}"
           f"/segments/0/leagues/{league_id}")
    flt = {"players": {"filterStatus": {"value": ["FREEAGENT", "WAIVERS", "ONTEAM"]},
                       "limit": 1000,
                       "sortDraftRanks": {"sortPriority": 100, "sortAsc": True, "value": "PPR"}}}
    resp = requests.get(url, params={"view": "kona_player_info"}, cookies=cookies, timeout=30,
                        headers={"User-Agent": "ffdraft-mcp/1.0", "X-Fantasy-Source": "kona",
                                 "X-Fantasy-Filter": json.dumps(flt)})
    resp.raise_for_status()
    rows = []
    for entry in resp.json().get("players") or []:
        p = entry.get("player") or {}
        adp = (p.get("ownership") or {}).get("averageDraftPosition")
        if adp is None or not p.get("fullName"):
            continue
        rows.append({"name": p["fullName"], "adp": float(adp), "espn_id": str(p.get("id")),
                     "pro_team_id": p.get("proTeamId"),
                     "position": _ESPN_POSITION_NAMES.get(str(p.get("defaultPositionId"))),
                     "espn_rank": ((p.get("draftRanksByRankType") or {}).get("PPR") or {}).get("rank"),
                     "percent_owned": (p.get("ownership") or {}).get("percentOwned"),
                     "espn_proj": espn_season_projection(p, season),
                     "espn_injury": p.get("injuryStatus")})
    out = pd.DataFrame(rows, columns=["name", "adp", "espn_id", "pro_team_id", "position",
                                      "espn_rank", "percent_owned", "espn_proj",
                                      "espn_injury"])
    out["_key"] = out["name"].map(norm_name)
    out["source"] = "espn_adp"
    out = out.dropna(subset=["adp"]).drop_duplicates("_key").reset_index(drop=True)
    out["adp_undrafted"] = undrafted_adp_mask(out["adp"])
    return out


# ESPN fills `averageDraftPosition` with a placeholder for players its
# population does not draft, rather than leaving it null. On the live 2026 list
# 823 of 999 rows land in one 4-pick-wide bin: 260 share the value 169.99 and
# 208 share 170.00 exactly. A number 468 players share is not a draft position
# -- only one player can be taken at each pick -- it is "undrafted" written as a
# pick number, and reading it as one tells the survival model that most of the
# board is about to disappear.
#
# The placeholder is found rather than hardcoded, because its value is ESPN's
# default draft length and moves with it: the most-repeated ADP, accepted as a
# placeholder only when more players share it than could possibly share a real
# pick. UNDRAFTED_ADP_TOLERANCE is the half-width of the run around it that is
# treated the same way, and it is policy, not a fitted number -- ESPN's average
# smears the fill across neighbouring hundredths, and 1.0 pick is the width that
# takes the spike (794 of 999 rows, all with a median 0.03% roster rate) while
# leaving the nearest genuinely-drafted players outside it (Dalton Schultz at
# 168.87 and 18.4% owned, Calvin Ridley at 168.94 and 25.1%).
UNDRAFTED_MIN_TIES = 20
UNDRAFTED_ADP_TOLERANCE = 1.0
# adp_source for a row ESPN carries but declines to price, as distinct from
# "modelled", which means the market join found no row for him at all.
UNDRAFTED_SOURCE = "undrafted"


def undrafted_adp_mask(adp: pd.Series) -> pd.Series:
    """Which ESPN ADPs are the "nobody drafts him" placeholder, not a position.

    Returns all-False when no value repeats often enough to be a fill, so a
    market frame with genuinely continuous ADPs (a pasted CSV, consensus ECR)
    is never touched.
    """
    values = pd.to_numeric(adp, errors="coerce")
    counts = values.round(2).value_counts()
    if counts.empty or counts.iloc[0] < UNDRAFTED_MIN_TIES:
        return pd.Series(False, index=adp.index)
    placeholder = float(counts.index[0])
    return (values - placeholder).abs() <= UNDRAFTED_ADP_TOLERANCE


def espn_season_projection(player: dict, season: int) -> float | None:
    """ESPN's full-season projection under the league's scoring: the stats entry
    with statSourceId 1 (projected), scoringPeriodId 0 (season), this season."""
    for st in player.get("stats") or []:
        if (st.get("seasonId") == season and st.get("statSourceId") == 1
                and st.get("scoringPeriodId") == 0 and st.get("appliedTotal") is not None):
            return float(st["appliedTotal"])
    return None


def espn_adp_configured() -> bool:
    """ESPN_LEAGUE_ID plus both cookies are set, so load_adp would prefer ESPN ADP."""
    return all(os.environ.get(k) for k in ("ESPN_LEAGUE_ID", "ESPN_SWID", "ESPN_S2"))


def strip_adp(board: pd.DataFrame) -> pd.DataFrame:
    """The board without its market columns, ready for attach_adp again."""
    return board.drop(columns=[c for c in ("adp", "adp_source", "adp_delta", "adp_format",
                                           "adp_match", "market_join_version",
                                           "adp_undrafted",
                                           "espn_proj", "espn_injury", "espn_rank")
                               if c in board.columns])


def load_adp(fmt: str = "half_ppr", csv_path: str | None = None,
             season: int = CURRENT_SEASON, superflex: bool = False,
             espn_league_id: str | None = None) -> pd.DataFrame:
    """Draft-cost estimates, in order of preference.

    1. A CSV you export from your own platform — always best, because ADP is
       league- and format-specific and your room is what you're drafting against.
    2. ESPN's own ADP when an ESPN league id and cookies are available (env
       ESPN_LEAGUE_ID, ESPN_SWID, ESPN_S2): the list your ESPN opponents draft from.
    3. FantasyPros preseason expert consensus rank, mirrored by dynastyprocess as a
       parquet going back to 2019. This is the reliable path: a direct data file
       rather than an HTML page that changes layout and blocks scripted requests.
    4. FantasyPros' live HTML page, as a last resort.
    """
    if csv_path:
        df = pd.read_csv(csv_path)
        cols = {c.lower().strip(): c for c in df.columns}
        name_c = next((cols[c] for c in ("name", "player", "player_name") if c in cols), None)
        adp_c = next((cols[c] for c in ("adp", "avg", "average", "rank") if c in cols), None)
        if not name_c or not adp_c:
            raise ValueError("ADP CSV needs a name column and an adp column")
        out = df[[name_c, adp_c]].rename(columns={name_c: "name", adp_c: "adp"})
        out["adp"] = pd.to_numeric(out["adp"], errors="coerce")
        out["_key"] = out["name"].map(norm_name)
        out["source"] = "csv"
        return out.dropna(subset=["adp"])

    espn_league_id = espn_league_id or os.environ.get("ESPN_LEAGUE_ID")
    if espn_league_id and season == CURRENT_SEASON and os.environ.get("ESPN_SWID") \
            and os.environ.get("ESPN_S2"):
        try:
            espn = load_espn_adp(espn_league_id, season)
            if len(espn) >= 100:
                return espn
            print(f"ESPN ADP returned {len(espn)} players; using consensus")
        except Exception as exc:
            print(f"ESPN ADP unavailable ({type(exc).__name__}); using consensus")

    try:
        from .adp import preseason_ecr
        ecr = preseason_ecr(season, superflex=superflex)
        if not ecr.empty:
            ecr = ecr[["name", "position", "adp", "_key"]].copy()
            ecr["source"] = "fantasypros_ecr_superflex" if superflex else "fantasypros_ecr"
            return ecr
    except Exception as exc:
        print(f"ECR history unavailable ({type(exc).__name__}); trying live page")

    url = FANTASYPROS_ADP.get(fmt, FANTASYPROS_ADP["half_ppr"])
    resp = requests.get(url, timeout=20, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
    })
    resp.raise_for_status()
    tables = pd.read_html(io.StringIO(resp.text))
    df = max(tables, key=len)
    cols = {str(c).lower(): c for c in df.columns}
    if not any("player" in c for c in cols):
        # As of 2026-09 the ADP table is rendered client-side; the only static
        # table is the sources legend. Fail loudly so the caller's fallback to
        # model rank is visible in the log instead of a silent empty frame.
        raise RuntimeError("FantasyPros ADP page has no server-rendered player table")
    name_c = next(cols[c] for c in cols if "player" in c)
    adp_c = next((cols[c] for c in cols if c in ("avg", "avg.", "adp")), df.columns[-1])
    out = df[[name_c, adp_c]].copy()
    out.columns = ["name", "adp"]
    # FantasyPros appends team and bye to the name cell.
    out["name"] = out["name"].astype(str).str.replace(r"\s*\([^)]*\)", "", regex=True)
    out["name"] = out["name"].str.replace(r"\s+[A-Z]{2,3}\s*\(?\d*\)?$", "", regex=True).str.strip()
    out["adp"] = pd.to_numeric(out["adp"], errors="coerce")
    out["_key"] = out["name"].map(norm_name)
    out["source"] = "fantasypros_html"
    return out.dropna(subset=["adp"])


# Typical 12-team draft position by positional rank, as adp = a * rank^b.
# Fitted to the shape of real half-PPR boards. This matters because draft rooms do
# not draft in value order: QBs and TEs slide well past their raw value, and using
# model rank as a stand-in for ADP would assume the room agrees with the model —
# which would make the whole opportunity-cost calculation circular.
# How much of the pure points-arithmetic shift to apply when converting consensus
# rankings between scoring formats. Below 1.0 because draft rooms price things the
# format doesn't change — consistency, positional scarcity, name recognition.
FORMAT_SHIFT_DAMPING = 0.6

SYNTHETIC_ADP_CURVE = {
    "RB": (2.00, 1.12),
    "WR": (2.80, 1.03),
    "TE": (18.0, 0.80),
    "QB": (22.0, 0.68),
}


def synthetic_adp(position: str, pos_rank: float, seasons_stale: float = 0.0) -> float:
    """Draft-cost estimate for a player missing from real ADP, from the model's own
    positional rank -- with no real market behind it, this is only trustworthy for
    someone still actually in the league.

    seasons_stale is how far behind the board's freshest player this one's last
    active season is (0 for someone who played as recently as anyone else on the
    board). A big flat penalty per season, not a multiplier on the base estimate:
    real drafters don't discount a year-old star by some percentage, they stop
    trusting him at all, because "didn't play last year" could mean retired, hurt
    long-term, or out of the league, and the box scores alone can't tell which.
    Without this, a retired player's still-strong last-known form could earn him
    the single best synthetic ADP on the board -- his own rank produces its own
    inflated market price -- which is what let a running back retired for two
    seasons come back as the model's runaway top recommendation in a backtest.
    """
    a, b = SYNTHETIC_ADP_CURVE.get(position, (3.0, 1.05))
    base = a * max(1.0, pos_rank) ** b
    return float(base + 200.0 * max(0.0, seasons_stale))


def audit_state(board: pd.DataFrame, state: DraftState,
                recommendations: pd.DataFrame | None = None) -> dict:
    """Invariants between the board, the recorded picks and the recommendation.

    failures break a recommendation; warnings are expected gaps (kickers,
    defenses and players with no modelled season never resolve). A recorded
    name the board holds under another spelling is a failure: that player
    still ranks as available.
    """
    failures, warnings = [], []
    keys = board["name"].map(norm_name) if "name" in board.columns else pd.Series(dtype=str)
    board_keys = set(keys)
    if "_key" in board.columns and not (board["_key"] == keys).all():
        failures.append("board _key column disagrees with the current normaliser; rekey it")

    picks = state.picks
    overalls = [p["overall"] for p in picks]
    if overalls != list(range(1, len(picks) + 1)):
        failures.append(f"pick numbers are not contiguous 1..{len(picks)}: "
                        f"{[o for i, o in enumerate(overalls) if o != i + 1][:5]}")

    seen, dupes = set(), []
    for p in picks:
        k = norm_name(p["name"])
        if k in seen and not p["name"].startswith("ESPN#"):
            dupes.append(p["name"])
        seen.add(k)
    if dupes:
        failures.append(f"players recorded twice: {dupes[:5]}")

    unresolved = [p["name"] for p in picks if norm_name(p["name"]) not in board_keys]
    misspelled = []
    if "name" in board.columns and "position" in board.columns:
        for n in unresolved:
            if n.endswith(" D/ST") or n.startswith("ESPN#"):
                continue
            row = match_player(n, board)
            if row is not None:
                misspelled.append(f"{n} -> board has {row['name']!r}")
    if misspelled:
        failures.append("recorded under a spelling the board does not key: " + "; ".join(misspelled[:6]))
    not_modelled = [n for n in unresolved if not any(n == m.split(" -> ")[0] for m in misspelled)]
    if not_modelled:
        warnings.append(f"{len(not_modelled)} picks not on the board (K, DST, unmodelled): "
                        f"{not_modelled[:8]}")

    mine = [p for p in picks if p["slot"] == state.my_slot]
    expected_mine = [n for n in state.my_picks() if n <= len(picks)]
    if [p["overall"] for p in mine] != expected_mine:
        failures.append(f"your picks {[p['overall'] for p in mine]} != slot {state.my_slot}'s "
                        f"scheduled picks {expected_mine}")

    if recommendations is not None and not recommendations.empty and "_key" in recommendations.columns:
        taken = state.taken_keys()
        leaked = [n for n, k in zip(recommendations["name"], recommendations["_key"]) if k in taken]
        if leaked:
            failures.append(f"drafted players in recommendations: {leaked}")
        if "espn_proj" in recommendations.columns:
            from .model import ROLE_DISAGREEMENT, ROLE_FLOOR, ROLE_UNKNOWN_RANK

            r = recommendations
            ratio = pd.to_numeric(r["espn_proj"], errors="coerce") / r["proj_points"]
            low = r[ratio.notna() & (ratio < ROLE_DISAGREEMENT)]
            if not low.empty:
                warnings.append("ESPN projects far below the model (role changed?): "
                                + "; ".join(f"{n} {e:.0f} vs {p:.0f}" for n, e, p in
                                            zip(low["name"], low["espn_proj"], low["proj_points"])))
            unknown = r[pd.to_numeric(r["espn_proj"], errors="coerce").isna()]
            if not unknown.empty:
                rank = (pd.to_numeric(unknown["espn_rank"], errors="coerce")
                        if "espn_rank" in unknown.columns
                        else pd.Series(np.nan, index=unknown.index))
                deep = rank.isna() | (rank > ROLE_UNKNOWN_RANK)
                if deep.any():
                    warnings.append(
                        f"no ESPN projection and no ESPN rank inside {ROLE_UNKNOWN_RANK:.0f} "
                        f"(role unknown, pick_value scaled to {ROLE_FLOOR:.0%}): "
                        + ", ".join(unknown.loc[deep, "name"].tolist()))
                if (~deep).any():
                    warnings.append(
                        "no ESPN projection, but ESPN still ranks them inside "
                        f"{ROLE_UNKNOWN_RANK:.0f} (left unscaled): "
                        + ", ".join(unknown.loc[~deep, "name"].tolist()))

    return {"ok": not failures, "failures": failures, "warnings": warnings,
            "picks": len(picks), "mine": len(mine), "unresolved": len(unresolved)}


def rekey(board: pd.DataFrame) -> pd.DataFrame:
    """Recompute `_key` from `name` with the current normaliser. A cached board
    carries the keys of whatever normaliser built it; draft state is keyed live,
    and the two must agree or drafted players show as available."""
    b = board.copy()
    if "name" in b.columns:
        b["_key"] = b["name"].map(norm_name)
    return b


# PlayerIndex match types the market join accepts beyond the exact key.
ALIAS_JOINS = ("alias", "lastname_initial")
# How the first pass priced a row. "exact" is the strongest join the market frame
# supports: key and position together when it carries positions, the key alone
# when it does not (a pasted CSV). "key_only" means the frame did carry positions
# and this row was still priced on the name alone, because the market held exactly
# one player under it -- worth reporting, since the two sides disagree on what he
# plays.
EXACT_JOIN = "exact"
KEY_ONLY_JOIN = "key_only"
# Bumped whenever attach_adp changes what a board row joins to. A cached board
# stamped with an older version is repriced on load (server._build_board), for
# the same reason names.KEY_VERSION exists: the projections in the parquet are
# still good, but the market columns beside them were derived by rules that no
# longer hold, and nothing else in the cache gate would notice.
MARKET_JOIN_VERSION = 5


def market_join_report(board: pd.DataFrame, limit: int = 10) -> dict:
    """Which board rows the market join could not price, strongest projection
    first, and which it priced through an alias. A modelled row with a real
    projection is the Estimé shape: a synthetic ADP standing in for a market
    value that may exist under another spelling."""
    if "adp_source" not in board.columns:
        return {"unjoined": [], "alias_joined": []}
    un = board[board["adp_source"] == "modelled"].sort_values("proj_points", ascending=False)
    cols = ["name", "position", "team", "proj_points", "adp"]
    unjoined = [{**{c: r[c] for c in cols if c in board.columns},
                 "proj_points": round(float(r["proj_points"]), 1),
                 "synthetic_adp": round(float(r["adp"]), 1)}
                for _, r in un.head(limit).iterrows()]
    for u in unjoined:
        u.pop("adp", None)
    alias, key_only = [], []
    alias_total = key_only_total = 0
    if "adp_match" in board.columns:
        # Both lists are capped like `unjoined`: a market frame that labels
        # positions differently across the board would otherwise print
        # hundreds of rows into an audit meant to be read.
        al = board[board["adp_match"].isin(ALIAS_JOINS)]
        alias_total = int(len(al))
        alias = [{"name": r["name"], "position": r["position"], "how": r["adp_match"],
                  "adp": round(float(r["adp"]), 1)} for _, r in al.head(limit).iterrows()]
        ko = board[board["adp_match"] == KEY_ONLY_JOIN]
        key_only_total = int(len(ko))
        key_only = [{"name": r["name"], "position": r["position"],
                     "adp": round(float(r["adp"]), 1)} for _, r in ko.head(limit).iterrows()]
    undrafted_total = int((board["adp_source"] == UNDRAFTED_SOURCE).sum())
    return {"unjoined": unjoined, "unjoined_total": int(len(un)),
            "alias_joined": alias, "alias_joined_total": alias_total,
            "key_only": key_only, "key_only_total": key_only_total,
            "undrafted_total": undrafted_total}


def _exact_market_join(b: pd.DataFrame, src: pd.DataFrame, extra: list[str]) -> pd.DataFrame:
    """Price board rows whose normalised name is in the market frame, position first.

    Two real players can share a full name at different positions -- the ESPN
    list carries Josh Allen the quarterback and Josh Allen the linebacker under
    one key -- so joining on the name alone hands whichever row happens to sort
    first to both board rows, and the second player is priced as the first.
    `PlayerIndex` already disambiguates on position for free-text lookups; this
    is the same rule for the bulk join.

    The name alone is still enough when the market holds exactly one player
    under it: then the only thing that can differ is the position *label*
    (fullback-ish tweeners the board calls TE and ESPN calls RB), and no other
    player can be picked by mistake. Those rows are recorded as `key_only` so
    the market-join report can show what was priced across a disagreement. A
    market frame with no position column at all -- a pasted CSV -- has nothing
    to be aware of and joins on the key as before.
    """
    cols = ["adp", *extra]
    if "position" in src.columns and "position" in b.columns:
        by_pos = src.drop_duplicates(["_key", "position"])
        same_pos = by_pos[by_pos["position"].notna()]
        b = b.merge(same_pos[["_key", "position", *cols]], on=["_key", "position"], how="left")
        b["adp_match"] = np.where(b["adp"].notna(), EXACT_JOIN, "none")
        per_key = by_pos.groupby("_key").size()
        lone = by_pos[by_pos["_key"].isin(per_key.index[per_key == 1])].set_index("_key")
        missing = b["adp"].isna()
        if missing.any() and not lone.empty:
            for c in cols:
                # Whole-column assignment, not .loc on the missing rows: the
                # merge leaves an all-NaN float column behind when nothing
                # joined, and writing strings (espn_injury) into it in place is
                # an incompatible-dtype set.
                b[c] = b[c].where(~missing, b["_key"].map(lone[c]))
            b["adp_match"] = np.where(missing & b["adp"].notna(), KEY_ONLY_JOIN, b["adp_match"])
        return b
    b = b.merge(src.drop_duplicates("_key")[["_key", *cols]], on="_key", how="left")
    b["adp_match"] = np.where(b["adp"].notna(), EXACT_JOIN, "none")
    return b


def attach_adp(board: pd.DataFrame, adp: pd.DataFrame | None) -> pd.DataFrame:
    """Join ADP onto the board, falling back to positional draft curves where missing."""
    b = board.copy()
    b["_key"] = b["name"].map(norm_name)
    if adp is not None and not adp.empty:
        extra = [c for c in ("espn_proj", "espn_injury", "espn_rank", "adp_undrafted")
                 if c in adp.columns]
        b = b.drop(columns=[c for c in extra if c in b.columns])
        src = adp.drop_duplicates(["_key", "position"]) if "position" in adp.columns \
            else adp.drop_duplicates("_key")
        b = _exact_market_join(b, src, extra)
        # Second pass through the alias index for what the exact key missed:
        # "Josh Palmer" on the board, "Joshua Palmer" at ESPN. Only alias and
        # last-name-plus-initial hits at the same position are taken; fuzzy and
        # ambiguous hits would join the wrong player silently.
        missing = b.index[b["adp"].isna()]
        hit_at: list[int] = []
        hit_rows: list[pd.Series] = []
        hit_how: list[str] = []
        if len(missing) and "position" in src.columns:
            idx = names.PlayerIndex(src)
            for i in missing:
                row, how = idx.resolve(str(b.at[i, "name"]), str(b.at[i, "position"]))
                # "exact" here is the query's key hitting one of the source
                # row's alias keys (the true exact key already missed above).
                if row is None or how not in ("exact", *ALIAS_JOINS):
                    continue
                if str(row.get("position")) != str(b.at[i, "position"]):
                    continue
                hit_at.append(int(i))
                hit_rows.append(row)
                hit_how.append("alias" if how == "exact" else how)
        if hit_at:
            # Whole-column assignment, for the same reason _exact_market_join
            # uses it: an exact pass that matched nothing at all leaves
            # espn_injury as an all-NaN float column, and writing strings into
            # it one cell at a time is an incompatible-dtype set.
            found = b.index.isin(hit_at)
            for c in ("adp", *extra):
                vals = pd.Series([r[c] for r in hit_rows], index=hit_at)
                b[c] = b[c].where(~found, vals.reindex(b.index))
            b["adp_match"] = b["adp_match"].where(
                ~found, pd.Series(hit_how, index=hit_at).reindex(b.index))
        label = "espn" if "source" in adp.columns and (adp["source"] == "espn_adp").any() \
            else "consensus"
        b["adp_source"] = np.where(b["adp"].notna(), label, "modelled")
        # A row the market carries but declines to price is not priced. Blanking
        # the placeholder here hands it to the same synthetic fallback that
        # already covers a row the join missed, so every consumer of `adp` --
        # survival odds, plan_my_draft's availability filter, the reach numbers
        # -- reads "no market price" instead of "about to be taken at 170".
        # adp_source keeps the two causes apart.
        if "adp_undrafted" in b.columns:
            blank = b["adp_undrafted"].fillna(False).astype(bool) & b["adp"].notna()
            b.loc[blank, "adp"] = np.nan
            b["adp_source"] = np.where(blank, UNDRAFTED_SOURCE, b["adp_source"])
    else:
        b["adp"] = np.nan
        b["adp_source"] = "modelled"
        b["adp_match"] = "none"
    b["market_join_version"] = MARKET_JOIN_VERSION

    if "last_season" in b.columns:
        freshest = b["last_season"].max()
        stale = (freshest - b["last_season"]).clip(lower=0).fillna(0)
    else:
        stale = pd.Series(0.0, index=b.index)
    # A player off every team's depth chart has no real path to touches even
    # though he may have played as recently as anyone else on the board (so
    # last_season alone reads him as fresh) -- treat it as one stale season's
    # worth of synthetic-ADP burial so a fallback estimate never hands him a
    # top-of-board fake market price. Real ADP (the branch above) already
    # reflects this correctly when it exists; this only guards the fallback.
    if "off_roster" in b.columns:
        stale = stale + b["off_roster"].fillna(False).astype(bool).astype(float)
    fallback = [synthetic_adp(p, r, s)
               for p, r, s in zip(b["position"], b["pos_rank"], stale)]
    b["adp"] = b["adp"].fillna(pd.Series(fallback, index=b.index))
    b["adp_delta"] = b["adp"] - b["overall_rank"]
    return b


def convert_adp_format(board: pd.DataFrame, scoring_label: str) -> pd.DataFrame:
    """Shift PPR consensus rankings into this league's scoring format.

    Published consensus is full PPR — that is the only overall redraft ranking
    FantasyPros publishes. Feeding it straight into a half-PPR or standard league
    misprices exactly the players the format is about: a back who catches 60 passes
    is worth far less without a full point per reception, while a touchdown-dependent
    early-down back becomes relatively more valuable.

    The market ranking stays the anchor, because it encodes talent, situation and
    injury news no model captures. Only the format delta is applied, and that delta
    is arithmetic rather than opinion: half PPR is PPR minus half a point per catch,
    and each player's reception volume comes from his own projection. The adjustment
    is expressed as a shift in rank positions so it composes cleanly with ADP.
    """
    b = board.copy()
    b["adp_format"] = scoring_label
    if scoring_label == "ppr" or "proj_points_ppr" not in b.columns:
        return b

    rank_ppr = b["proj_points_ppr"].rank(ascending=False, method="min")
    rank_fmt = b["proj_points"].rank(ascending=False, method="min")
    # Positive shift = this format devalues him relative to PPR, so he goes later.
    #
    # Damped rather than applied whole. The raw rank delta is what pure points
    # arithmetic implies, but real draft rooms move less than that: they also price
    # consistency, positional scarcity and name recognition, none of which change
    # with the scoring format. Undamped, Derrick Henry went from ADP 38 to 1.0 in
    # standard — right direction, absurd magnitude.
    b["adp_shift"] = (rank_fmt - rank_ppr).fillna(0.0) * FORMAT_SHIFT_DAMPING
    b["adp_ppr"] = b["adp"]
    b["adp"] = (b["adp"] + b["adp_shift"]).clip(lower=1.0)
    b["adp_delta"] = b["adp"] - b["overall_rank"]
    return b


# ---------------------------------------------------------------- draft state

# Marks a roster row the board could not price, so a caller can say so rather
# than report a projection of null beside a roster count that includes him.
UNPRICED = "unpriced"


def replacement_points(board: pd.DataFrame, position: str) -> float:
    """The board's own replacement level for a position, in projected points.

    `replacement_points` is the projection of the last player at the position
    anyone in the league would start, and `model.project` writes it per position,
    so it is constant within a position on a built board and any row carries it.
    Reading it back beats recomputing it: a second derivation of the same number
    is a second thing to keep in step.

    0 for a position the board does not carry at all, which is the same thing
    `vor` already means -- no value over a freely available alternative.
    """
    if "replacement_points" not in board.columns or "position" not in board.columns:
        return 0.0
    chunk = board.loc[board["position"] == position, "replacement_points"].dropna()
    return float(chunk.iloc[0]) if len(chunk) else 0.0


class DraftState:
    """Who's been taken, by whom, and whose turn it is.

    State is stored per league, so two drafts running in different leagues never
    read each other's picks.
    """

    def __init__(self, league: LeagueSettings, name: str | None = None):
        self.league = league
        key = re.sub(r"[^A-Za-z0-9_-]", "_", name or league.name or "default")
        self.path = STATE_DIR / f"draft_{key}.json"
        self.picks: list[dict] = []
        # Your slot comes from the league config, always. Reading it back from the
        # saved draft file meant reconfiguring to pick 11 and still being advised
        # for pick 6, because the stale value won.
        self.my_slot = league.draft_slot
        if self.path.exists():
            raw = json.loads(self.path.read_text())
            self.picks = raw.get("picks", [])
            # Picks recorded under a different league size describe a different
            # draft entirely; discard rather than misinterpret them.
            if raw.get("teams") not in (None, league.teams):
                self.picks = []

    def save(self) -> None:
        self.path.write_text(json.dumps({
            "picks": self.picks, "my_slot": self.my_slot,
            "teams": self.league.teams, "league": self.league.name,
            "updated": time.time(),
        }, indent=2))

    # -- mutation
    def record(self, player_name: str, overall: int | None = None,
               team_slot: int | None = None, player_id: str | None = None,
               position: str | None = None) -> dict:
        """position is kept for players the board does not model (a kicker, a back
        with no recent season) so your roster count still sees them."""
        overall = overall or (len(self.picks) + 1)
        slot = team_slot if team_slot is not None else self.slot_for_pick(overall)
        pick = {"overall": overall, "slot": slot, "name": player_name, "player_id": player_id,
                "position": position}
        self.picks = [p for p in self.picks if p["overall"] != overall] + [pick]
        self.picks.sort(key=lambda p: p["overall"])
        self.save()
        return pick

    def undo(self) -> dict | None:
        if not self.picks:
            return None
        p = self.picks.pop()
        self.save()
        return p

    def reset(self) -> None:
        self.picks = []
        self.save()

    # -- queries
    def slot_for_pick(self, overall: int) -> int:
        t = self.league.teams
        rnd = (overall - 1) // t + 1
        idx = (overall - 1) % t + 1
        return (t - idx + 1) if (self.league.snake and rnd % 2 == 0) else idx

    @property
    def on_the_clock(self) -> int:
        return len(self.picks) + 1

    def my_picks(self) -> list[int]:
        return self.league.picks_for_slot(self.my_slot)

    def next_pick_for_me(self, after: int | None = None) -> int | None:
        after = after or self.on_the_clock
        upcoming = [p for p in self.my_picks() if p >= after]
        return upcoming[0] if upcoming else None

    def pick_after_next(self) -> int | None:
        nxt = self.next_pick_for_me()
        if nxt is None:
            return None
        later = [p for p in self.my_picks() if p > nxt]
        return later[0] if later else None

    def taken_keys(self) -> set[str]:
        return {norm_name(p["name"]) for p in self.picks}

    def my_rows(self, board: pd.DataFrame) -> pd.DataFrame:
        """The roster the lineup model sees: your board rows, plus a stand-in for
        any player you hold that the board does not price.

        A player the board cannot price still occupies his roster slot. Dropping
        him told the lineup model the roster was a man short at his position, and
        `roles.start_probabilities` counts exactly that -- the men ahead of a
        candidate at his position -- so a candidate who should have queued behind
        him came back as a certain starter. In the live record MarShawn Lloyd at
        pick 93 has no board row, and the model saw two running backs where the
        roster holds three.

        The stand-in is priced at the board's own replacement level for his
        position, which is the least the roster can be assumed to hold: he is by
        definition someone you would not start over a replacement-level
        alternative, and `vor` of 0 says so. It is deliberately not a guess at
        what he is really worth -- the board has no opinion, and inventing one
        here would put a number in front of the user that nothing supports.

        `bye_week` is left missing rather than filled, so he stays out of the bye
        term instead of contributing a fabricated conflict. `unpriced` marks him
        for anything that needs to say so.

        A player with no position on the board *or* on the pick record cannot be
        placed at all and is still dropped, the same as `my_roster` drops him.
        """
        mine = [p for p in self.picks if p["slot"] == self.my_slot]
        if "_key" not in board.columns or not mine:
            return board.iloc[0:0]
        rows = board[board["_key"].isin([norm_name(p["name"]) for p in mine])].copy()
        rows[UNPRICED] = False
        priced = set(rows["_key"])
        missing = [p for p in mine
                   if norm_name(p["name"]) not in priced and p.get("position")]
        if not missing:
            return rows
        stand_ins = pd.DataFrame([{
            "_key": norm_name(p["name"]),
            "name": p["name"],
            "position": str(p["position"]),
            "proj_points": replacement_points(board, str(p["position"])),
            "replacement_points": replacement_points(board, str(p["position"])),
            "vor": 0.0,
            "draft_score": 0.0,
            "off_roster": False,
            "is_rookie": False,
            UNPRICED: True,
        } for p in missing]).reindex(columns=rows.columns)
        # reindex fills the columns the stand-in has no opinion about with NaN,
        # which turns a bool column into object and breaks every `frame[col]`
        # mask downstream -- the mistake the K/DST rows already made once.
        #
        # Taken from the frame's own dtypes rather than a list of column names.
        # A hardcoded list is a claim about which columns a board carries, and
        # the boards in this repo differ: the live one has `off_roster` and
        # `is_rookie`, a test fixture may have neither, and naming them raised
        # KeyError on the fixture that had neither. False is the neutral value
        # for a flag the stand-in cannot have an opinion about.
        for col in rows.select_dtypes(include="bool").columns:
            stand_ins[col] = stand_ins[col].fillna(False).astype(bool)
        return pd.concat([rows, stand_ins], ignore_index=True)

    @staticmethod
    def _board_positions(board: pd.DataFrame) -> dict:
        return board.set_index("_key")["position"].to_dict() \
            if "_key" in board.columns else {}

    @staticmethod
    def _position_of(board_positions: dict, pick: dict) -> str:
        """One pick's position: the board's spelling, the recorded one as fallback.

        The board is asked first so a name the draft record spells loosely still
        lands where the model prices it, and the recorded position is the
        fallback so a kicker the board does not model still counts.

        The guard is `isinstance(..., str)` rather than the bare `or` all three
        callers used to write. A board row carrying no position arrives here as
        NaN, NaN is truthy, and the `or` written to reach the fallback returned
        the NaN instead -- so the fallback never ran and a float went out of a
        function that promises a position. Three different symptoms, one cause:
        `my_roster` put a float key in a `dict[str, int]`, which reached the user
        through `who_should_i_pick`'s `your_roster` as a position named "NaN" and
        crashed the tool outright whenever another position was also thin, since
        its roster note sorts them and `'<'` does not order a float against a
        string. `picks_by_position` stringified it instead and invented a
        phantom position "nan" for `plan_my_draft` to compare supply against.
        `held_by_slot` dropped the pick, so a team holding a kicker the board
        forgot to classify was read as still needing one.

        Only a malformed board row reaches any of this, which is why nothing had.
        Shared rather than repeated so the three cannot drift again.
        """
        on_board = board_positions.get(norm_name(pick["name"]))
        if isinstance(on_board, str) and on_board:
            return on_board
        recorded = pick.get("position")
        return recorded if isinstance(recorded, str) and recorded else ""

    def picks_by_position(self, board: pd.DataFrame) -> dict[str, int]:
        """Every pick the room has made, counted by position."""
        idx = self._board_positions(board)
        counts: dict[str, int] = {}
        for p in self.picks:
            pos = self._position_of(idx, p)
            if pos:
                counts[pos] = counts.get(pos, 0) + 1
        return counts

    def held_by_slot(self, board: pd.DataFrame) -> dict[int, dict[str, int]]:
        """Counted positions each team already holds, by draft slot.

        What decides whether the team on the clock can still defer a kicker or a
        defense: its own unfilled slots against its own remaining picks.
        """
        idx = self._board_positions(board)
        out: dict[int, dict[str, int]] = {}
        for p in self.picks:
            pos = self._position_of(idx, p)
            if pos in SPECIAL_POSITIONS:
                team = out.setdefault(int(p["slot"]), {})
                team[pos] = team.get(pos, 0) + 1
        return out

    def my_roster(self, board: pd.DataFrame) -> dict[str, int]:
        """Your picks counted by position."""
        idx = self._board_positions(board)
        counts: dict[str, int] = {}
        for p in self.picks:
            if p["slot"] != self.my_slot:
                continue
            pos = self._position_of(idx, p)
            if pos:
                counts[pos] = counts.get(pos, 0) + 1
        return counts

    def summary(self) -> dict:
        return {
            "picks_made": len(self.picks),
            "on_the_clock": self.on_the_clock,
            "round": (self.on_the_clock - 1) // self.league.teams + 1,
            "my_slot": self.my_slot,
            "my_next_pick": self.next_pick_for_me(),
            "picks_until_my_turn": max(0, (self.next_pick_for_me() or 0) - self.on_the_clock),
        }


# ---------------------------------------------------------------- platform sync

def _id_crosswalk() -> pd.DataFrame:
    """gsis_id <-> espn_id / sleeper_id, from nflverse rosters.

    weekly_rosters has one row per player per week, and espn_id/sleeper_id are only
    reliably populated in some of those snapshots -- roughly a third of rows have a
    null espn_id even for players whose ID is known in other rows. Taking the first
    row per gsis_id (the old drop_duplicates) kept whichever snapshot happened to
    come first, which silently dropped the real ID for about a quarter of players --
    Bijan Robinson, Jahmyr Gibbs and De'Von Achane among them, verified against a
    2025 ESPN draft where they came back as unmatched ESPN#<id> picks. Grouping and
    taking the first non-null value per column, independently, uses whichever
    snapshot actually has the ID instead of gambling on row order.
    """
    r = sources.weekly_rosters()
    keep = [c for c in ("gsis_id", "espn_id", "sleeper_id", "full_name", "position") if c in r.columns]
    x = r[keep].dropna(subset=["gsis_id"])
    x = x.groupby("gsis_id", as_index=False).agg(
        lambda s: next((v for v in s if pd.notna(v)), np.nan))
    # Weekly rosters only cover the lookback seasons, so the current rookie class
    # has no row yet and its draft picks came back as ESPN#<id>. The players
    # master table carries espn_id for them; add whoever the rosters lack.
    p = sources.players()
    if "gsis_id" in p.columns and "espn_id" in p.columns:
        name_col = next((c for c in ("display_name", "full_name") if c in p.columns), None)
        extra = p.dropna(subset=["gsis_id", "espn_id"])
        extra = extra[~extra["gsis_id"].isin(x["gsis_id"])]
        cols = {"gsis_id": extra["gsis_id"], "espn_id": extra["espn_id"]}
        if name_col:
            cols["full_name"] = extra[name_col]
        if "position" in extra.columns:
            cols["position"] = extra["position"]
        x = pd.concat([x, pd.DataFrame(cols)], ignore_index=True)
    for c in ("espn_id", "sleeper_id"):
        if c in x.columns:
            x[c] = x[c].astype("string").str.replace(r"\.0$", "", regex=True)
    return x


def sync_sleeper(draft_id: str) -> list[dict]:
    """Pull picks from a Sleeper draft. Sleeper's draft API is public — no auth needed."""
    url = f"https://api.sleeper.app/v1/draft/{draft_id}/picks"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    xwalk = _id_crosswalk().set_index("sleeper_id")["full_name"].to_dict()
    out = []
    for p in resp.json():
        meta = p.get("metadata") or {}
        name = " ".join(filter(None, [meta.get("first_name"), meta.get("last_name")])).strip()
        name = name or xwalk.get(str(p.get("player_id")), "")
        out.append({
            "overall": p.get("pick_no"),
            "slot": p.get("draft_slot"),
            "name": name,
            "player_id": None,
            "position": meta.get("position"),
        })
    return sorted([o for o in out if o["name"]], key=lambda o: o["overall"] or 0)


def sync_espn(league_id: str, season: int = CURRENT_SEASON,
              swid: str | None = None, espn_s2: str | None = None) -> list[dict]:
    """Pull picks from an ESPN league's draft detail endpoint.

    Public leagues work with no credentials. Private leagues need the SWID and
    espn_s2 cookies from a logged-in browser session, passed here or set as the
    ESPN_SWID / ESPN_S2 environment variables.
    """
    swid = swid or os.environ.get("ESPN_SWID")
    espn_s2 = espn_s2 or os.environ.get("ESPN_S2")
    url = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}"
           f"/segments/0/leagues/{league_id}")
    cookies = {}
    if swid and espn_s2:
        cookies = {"SWID": swid if swid.startswith("{") else f"{{{swid}}}",
                   "espn_s2": espn_s2}
    resp = requests.get(url, params={"view": ["mDraftDetail", "mTeam", "kona_player_info"]},
                        cookies=cookies, timeout=20,
                        headers={"User-Agent": "ffdraft-mcp/1.0"})
    resp.raise_for_status()
    data = resp.json()
    detail = data.get("draftDetail") or {}
    picks = detail.get("picks") or []

    espn_map, pos_map = espn_maps()

    # The read API is blind while a draft is running: inProgress is true, every pick
    # carries playerId -1 and every roster is empty until the draft completes. The
    # picks live only in the draft room's socket snapshot, which needs the cookies.
    filled = [p for p in picks if p.get("playerId") not in (None, -1)]
    if detail.get("inProgress") and not filled and swid and espn_s2:
        return _sync_espn_live(league_id, season, data, swid, espn_s2, espn_map, pos_map)

    out = []
    for p in picks:
        # ESPN returns every slot in the draft order, filled or not -- a slot
        # nobody has picked yet comes back with playerId -1, not omitted. Treating
        # those as real (if unmatched) picks made the server think the draft was
        # far ahead of where it actually was.
        pid = p.get("playerId")
        if pid is None or pid == -1:
            continue
        out.append({
            "overall": p.get("overallPickNumber"),
            "slot": None,
            "name": _espn_player_name(pid, espn_map),
            "position": _espn_player_position(pid, pos_map),
            "player_id": None,
        })
    return sorted([o for o in out if o["overall"]], key=lambda o: o["overall"])


def _espn_player_name(pid: int, espn_map: dict) -> str:
    # Team defenses aren't players -- no gsis_id, so they're never in the
    # crosswalk. ESPN encodes them as -(16000 + proTeamId) instead.
    if pid < 0:
        return f"{_ESPN_PRO_TEAMS.get(-pid - 16000, f'ESPN#{pid}')} D/ST"
    return espn_map.get(str(pid), f"ESPN#{pid}")


def espn_maps(xwalk: pd.DataFrame | None = None) -> tuple[dict, dict]:
    """espn_id -> full_name and espn_id -> position from the crosswalk."""
    x = xwalk if xwalk is not None else _id_crosswalk()
    x = x.dropna(subset=["espn_id"]).set_index("espn_id")
    names = x["full_name"].to_dict()
    positions = x["position"].to_dict() if "position" in x.columns else {}
    return names, positions


def resolve_espn_id(name: str, board: pd.DataFrame, espn_map: dict) -> tuple[int | None, str]:
    """Free-text name -> ESPN player id, for SELECT and DRAFT_LIST.

    Board first (modelled players, fuzzy), then the crosswalk by normalised name
    (kickers and unmodelled players), then team defenses by city, nickname or
    "X D/ST". Returns (id, resolved name) or (None, reason)."""
    if "name" in board.columns and "position" in board.columns and len(board):
        row = match_player(name, board)
        if row is not None:
            key = norm_name(row["name"])
            pid = next((p for p, nm in espn_map.items() if norm_name(nm) == key), None)
            if pid is not None:
                return int(pid), str(row["name"])
    key = norm_name(name)
    pid = next((p for p, nm in espn_map.items() if norm_name(nm) == key), None)
    if pid is not None:
        return int(pid), str(espn_map[pid])
    wanted = key.replace(" d st", "").replace(" dst", "").replace(" defense", "").strip()
    for team_id, full in _ESPN_PRO_TEAMS.items():
        parts = norm_name(full).split()
        if wanted in (norm_name(full), parts[-1], " ".join(parts[:-1])):
            return -(16000 + team_id), f"{full} D/ST"
    return None, f"no board, crosswalk or defense match for '{name}'"


def _espn_player_position(pid: int, pos_map: dict) -> str | None:
    if pid < 0:
        return "DST"
    p = pos_map.get(str(pid))
    return str(p) if p is not None and pd.notna(p) else None


def _sync_espn_live(league_id: str, season: int, league_json: dict,
                    swid: str, espn_s2: str, espn_map: dict,
                    pos_map: dict | None = None) -> list[dict]:
    """Picks from the live draft room socket (see docs/data-sources.md).

    Joins as the team the SWID owns, reads the INIT snapshot plus any SELECTED
    lines that arrive in the first second, and leaves. Each pick's slot is the
    team's real draft position from the snapshot, not inferred from the overall
    pick number.
    """
    from . import espn_live

    target = swid.strip("{}")
    my_team = next((t for t in league_json.get("teams") or []
                    if target in [o.strip("{}") for o in t.get("owners", [])]), None)
    if my_team is None:
        raise RuntimeError("live draft sync needs a team owned by ESPN_SWID in this league")

    init, extra = espn_live.fetch_init(league_id, season, int(my_team["id"]), swid, espn_s2)
    slot_of = espn_live.slot_by_team(init)
    pos_map = pos_map or {}
    out = []
    for p in espn_live.picks_from_init(init):
        out.append({"overall": p["overall"], "slot": slot_of.get(p["team_id"]),
                    "name": _espn_player_name(p["player_id"], espn_map),
                    "position": _espn_player_position(p["player_id"], pos_map), "player_id": None})
    nxt = (out[-1]["overall"] if out else 0) + 1
    for line in extra:
        fields = line.split()
        if len(fields) >= 3 and fields[0] == "SELECTED":
            team_id, pid = int(fields[1]), int(fields[2])
            out.append({"overall": nxt, "slot": slot_of.get(team_id),
                        "name": _espn_player_name(pid, espn_map),
                        "position": _espn_player_position(pid, pos_map), "player_id": None})
            nxt += 1
    return out


# ESPN's lineupSlotCounts slot ids that count as a flex, and which positions each
# is eligible for. Used to translate a real ESPN roster into LeagueSettings.starters.
_ESPN_FLEX_SLOTS = {"3": ("RB", "WR"), "5": ("WR", "TE"), "23": ("RB", "WR", "TE"),
                   "7": ("QB", "RB", "WR", "TE")}
_ESPN_BASE_SLOTS = {"0": "QB", "2": "RB", "4": "WR", "6": "TE", "16": "DST", "17": "K"}


def espn_league_context(league_id: str, season: int = CURRENT_SEASON,
                        swid: str | None = None, espn_s2: str | None = None) -> dict:
    """Everything needed to configure a league and find yourself in it, read
    straight from ESPN: team count, scoring, roster starters, your draft slot.

    Used by draft_backtest so a season/league_id is enough to run -- no manual
    configure_league bookkeeping for a season you're not actively drafting.
    """
    swid = swid or os.environ.get("ESPN_SWID")
    espn_s2 = espn_s2 or os.environ.get("ESPN_S2")
    cookies = {}
    if swid and espn_s2:
        cookies = {"SWID": swid if swid.startswith("{") else f"{{{swid}}}",
                   "espn_s2": espn_s2}
    url = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}"
           f"/segments/0/leagues/{league_id}")
    resp = requests.get(url, params={"view": ["mTeam", "mSettings", "mDraftDetail"]},
                        cookies=cookies, timeout=20,
                        headers={"User-Agent": "ffdraft-mcp/1.0"})
    resp.raise_for_status()
    data = resp.json()
    settings = data.get("settings") or {}
    teams = data.get("teams") or []

    rec_item = next((i for i in settings.get("scoringSettings", {}).get("scoringItems", [])
                     if i.get("statId") == 53), None)
    rec_pts = float(rec_item["points"]) if rec_item else 0.0
    scoring = "ppr" if rec_pts >= 0.9 else "half_ppr" if rec_pts >= 0.35 else "standard"

    slot_counts = settings.get("rosterSettings", {}).get("lineupSlotCounts", {}) or {}
    starters = {"QB": 0, "RB": 0, "WR": 0, "TE": 0, "FLEX": 0, "K": 0, "DST": 0}
    for sid, count in slot_counts.items():
        if sid in _ESPN_BASE_SLOTS and count:
            starters[_ESPN_BASE_SLOTS[sid]] += count
        elif sid in _ESPN_FLEX_SLOTS and count:
            starters["FLEX"] += count  # sub-eligibility isn't tracked, same as configure_league
    roster_slots = sum(int(v) for v in slot_counts.values())

    my_team = None
    if swid:
        target = swid.strip("{}")
        my_team = next((t for t in teams if target in [o.strip("{}") for o in t.get("owners", [])]),
                       None)
    draft_slot = None
    if my_team is not None:
        picks = (data.get("draftDetail") or {}).get("picks") or []
        mine = sorted([p for p in picks if p.get("teamId") == my_team["id"]],
                      key=lambda p: p.get("overallPickNumber", 0))
        if mine:
            draft_slot = mine[0].get("roundPickNumber")

    return {
        "league_name": settings.get("name"),
        "teams": len(teams),
        "scoring": scoring,
        "starters": starters,
        "rounds": max(1, roster_slots),
        "my_team_id": my_team["id"] if my_team is not None else None,
        "draft_slot": draft_slot,
    }


# ESPN scoring statIds this model scores. Anything else is reported by id.
_ESPN_STAT_NAMES = {
    3: "passing_yards", 4: "passing_tds", 19: "passing_2pt", 20: "interceptions",
    24: "rushing_yards", 25: "rushing_tds", 26: "rushing_2pt", 42: "receiving_yards",
    43: "receiving_tds", 44: "receiving_2pt", 53: "receptions", 72: "fumbles_lost",
}
# Kicker and D/ST statIds, per espn-api (refs/cwendt94/espn-api football/constant.py).
_ESPN_KDST_NAMES = {
    74: "fg_made_50_plus", 77: "fg_made_40_49", 80: "fg_made_under_40", 85: "fg_missed",
    86: "pat_made", 88: "pat_missed", 201: "fg_made_60_plus",
    89: "points_allowed_0", 90: "points_allowed_1_6", 91: "points_allowed_7_13",
    92: "points_allowed_14_17", 121: "points_allowed_18_21", 122: "points_allowed_22_27",
    123: "points_allowed_28_34", 124: "points_allowed_35_45", 125: "points_allowed_46_plus",
    128: "yards_allowed_under_100", 129: "yards_allowed_100_199", 130: "yards_allowed_200_299",
    131: "yards_allowed_300_349", 132: "yards_allowed_350_399", 133: "yards_allowed_400_449",
    134: "yards_allowed_450_499", 135: "yards_allowed_500_549", 136: "yards_allowed_550_plus",
    93: "blocked_kick_td", 94: "defensive_td", 95: "interception", 96: "fumble_recovery",
    97: "blocked_kick", 98: "safety", 99: "sack", 101: "kick_return_td", 102: "punt_return_td",
    103: "int_return_td", 104: "fumble_return_td", 106: "forced_fumble",
    198: "fg_made_50_59", 205: "two_pt_return", 206: "two_pt_return", 209: "pat_return",
}
_ESPN_SLOT_NAMES = {"0": "QB", "2": "RB", "4": "WR", "6": "TE", "16": "DST", "17": "K",
                    "20": "BENCH", "21": "IR", "23": "FLEX", "7": "OP", "3": "RB/WR",
                    "5": "WR/TE"}
_ESPN_POSITION_NAMES = {"1": "QB", "2": "RB", "3": "WR", "4": "TE", "5": "K", "16": "DST"}

# ESPN's proTeamId -> the abbreviation the board and the nfldata schedule use,
# so a kicker or a defense gets the same team (and therefore the same bye week)
# as every other row. Los Angeles Rams are "LA" upstream, not "LAR".
_ESPN_TEAM_ABBR = {
    1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL", 7: "DEN", 8: "DET",
    9: "GB", 10: "TEN", 11: "IND", 12: "KC", 13: "LV", 14: "LA", 15: "MIA", 16: "MIN",
    17: "NE", 18: "NO", 19: "NYG", 20: "NYJ", 21: "PHI", 22: "ARI", 23: "PIT",
    24: "LAC", 25: "SF", 26: "SEA", 27: "TB", 28: "WAS", 29: "CAR", 30: "JAX",
    33: "BAL", 34: "HOU",
}


def espn_special_teams(adp: pd.DataFrame | None) -> pd.DataFrame:
    """Kicker and team-defense rows for the board, from the ESPN player list.

    The board is built from nflverse box scores, which carry no kicking and no
    team-defense production, so K and D/ST never reached it: the recommender
    saw them as off_board and had nothing to say in the two rounds where the
    league forces you to fill both slots. ESPN publishes a full-season
    projection for both under this league's own scoring -- for a defense that
    means the yards-allowed and points-allowed bands `league_rules` reads out
    of `pointsOverrides` -- so that projection is what the board uses.

    Only rows ESPN actually projects above zero are kept. A kicker projected at
    exactly 0 (ten of the 55 on the live list) is ESPN saying he has no job, not
    a player worth a pick.

    Defenses are named the way `_espn_player_name` records a drafted one --
    "Denver Broncos D/ST", where ESPN's own list says "Broncos D/ST" -- or the
    draft state and the board would key the same defense differently and a
    drafted defense would keep showing up as available.
    """
    cols = ["name", "position", "team", "adp", "espn_id", "espn_rank", "espn_proj",
            "espn_injury", "proj_points", "adj_ppg", "pos_rank", "adp_source",
            "adp_match", "_key"]
    if adp is None or adp.empty or "position" not in adp.columns:
        return pd.DataFrame(columns=cols)
    src = adp[adp["position"].isin(SPECIAL_POSITIONS)].copy()
    if src.empty or "espn_proj" not in src.columns:
        return pd.DataFrame(columns=cols)
    src["espn_proj"] = pd.to_numeric(src["espn_proj"], errors="coerce")
    src = src[src["espn_proj"] > 0]
    if src.empty:
        return pd.DataFrame(columns=cols)
    team_id = pd.to_numeric(src.get("pro_team_id"), errors="coerce")
    out = pd.DataFrame({
        "name": np.where(src["position"] == "DST",
                         team_id.map(_ESPN_PRO_TEAMS).fillna("") + " D/ST",
                         src["name"]),
        "position": src["position"].to_numpy(),
        "team": team_id.map(_ESPN_TEAM_ABBR).to_numpy(),
        "adp": pd.to_numeric(src["adp"], errors="coerce").to_numpy(),
        "espn_id": src["espn_id"].to_numpy() if "espn_id" in src.columns else None,
        "espn_rank": (pd.to_numeric(src["espn_rank"], errors="coerce").to_numpy()
                      if "espn_rank" in src.columns else np.nan),
        "espn_proj": src["espn_proj"].to_numpy(),
        "espn_injury": (src["espn_injury"].to_numpy()
                        if "espn_injury" in src.columns else None),
    })
    # A defense with no proTeamId cannot be named or given a bye week.
    out = out[out["name"].str.strip() != "D/ST"].reset_index(drop=True)
    # ESPN's projection is the projection. adj_ppg is it spread over a full
    # season, which is what `explain` prints; there is no games-played model
    # for a defense.
    out["proj_points"] = out["espn_proj"]
    out["adj_ppg"] = out["espn_proj"] / 17.0
    out["pos_rank"] = out.groupby("position")["proj_points"].rank(ascending=False,
                                                                 method="min")
    out["adp_source"] = "espn"
    out["adp_match"] = EXACT_JOIN
    # Facts, and they keep the board's boolean columns boolean: a team defense
    # is not a rookie and is not a player who has fallen off a depth chart.
    # Left as NaN they widen `is_rookie` and `off_roster` to object, and pandas
    # refuses to mask with an object column holding None, so `b[b["is_rookie"]]`
    # raises on the whole board.
    out["is_rookie"] = False
    out["off_roster"] = False
    out["_key"] = out["name"].map(norm_name)
    return out.drop_duplicates("_key").reset_index(drop=True)


def espn_league_rules(league_id: str, season: int = CURRENT_SEASON,
                      swid: str | None = None, espn_s2: str | None = None) -> dict:
    """The league's rules as ESPN states them: roster, scoring, schedule and
    playoffs, waivers, trades, locks, tiebreakers, plus the bye-week topology of
    the season. First-party, so nothing here is assumed from a default template."""
    from . import features

    swid = swid or os.environ.get("ESPN_SWID")
    espn_s2 = espn_s2 or os.environ.get("ESPN_S2")
    cookies = {}
    if swid and espn_s2:
        cookies = {"SWID": swid if swid.startswith("{") else f"{{{swid}}}",
                   "espn_s2": espn_s2}
    url = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}"
           f"/segments/0/leagues/{league_id}")
    resp = requests.get(url, params={"view": ["mSettings", "mTeam"]}, cookies=cookies,
                        timeout=20, headers={"User-Agent": "ffdraft-mcp/1.0"})
    resp.raise_for_status()
    data = resp.json()
    s = data.get("settings") or {}
    sched, acq, trade = s.get("scheduleSettings") or {}, s.get("acquisitionSettings") or {}, \
        s.get("tradeSettings") or {}
    roster, scoring, draft = s.get("rosterSettings") or {}, s.get("scoringSettings") or {}, \
        s.get("draftSettings") or {}

    slots = {_ESPN_SLOT_NAMES.get(k, f"slot_{k}"): v
             for k, v in (roster.get("lineupSlotCounts") or {}).items() if v}
    limits = {_ESPN_POSITION_NAMES.get(k, f"pos_{k}"): v
              for k, v in (roster.get("positionLimits") or {}).items() if v and v > 0}
    items = {}
    other = {}
    by_slot: dict[str, dict[str, float]] = {}
    for it in scoring.get("scoringItems") or []:
        sid = it.get("statId")
        name = _ESPN_STAT_NAMES.get(sid)
        if name:
            items[name] = it.get("points")
        elif it.get("points"):
            other[_ESPN_KDST_NAMES.get(sid, str(sid))] = it.get("points")
        # pointsOverrides: values that apply only when the stat is scored in one
        # lineup slot (16 = D/ST). Yards-allowed and points-allowed bands live here
        # with points 0 at the top level, so reading `points` alone hides them.
        for slot, pts in (it.get("pointsOverrides") or {}).items():
            if pts:
                by_slot.setdefault(_ESPN_SLOT_NAMES.get(slot, f"slot_{slot}"), {})[
                    _ESPN_KDST_NAMES.get(sid, str(sid))] = pts
    periods = sched.get("matchupPeriods") or {}
    reg = int(sched.get("matchupPeriodCount") or 0)
    playoff_periods = sorted(int(k) for k in periods if int(k) > reg)
    playoff_weeks = sorted(w for k in playoff_periods for w in periods[str(k)])
    byes = features.team_bye_weeks(season)
    per_week: dict[int, int] = {}
    for w in byes.values():
        per_week[w] = per_week.get(w, 0) + 1
    deadline = trade.get("deadlineDate")
    return {
        "league": s.get("name"), "teams": s.get("size"), "season": season,
        "draft": {"type": draft.get("type"), "rounds": sum(v for k, v in slots.items() if k != "IR"),
                  "seconds_per_pick": draft.get("timePerSelection"),
                  "keepers": draft.get("keeperCount"), "pick_trading": draft.get("isTradingEnabled")},
        "roster": {"starters": {k: v for k, v in slots.items() if k not in ("BENCH", "IR")},
                   "bench": slots.get("BENCH", 0), "ir": slots.get("IR", 0),
                   "position_limits": limits,
                   "lineup_lock": roster.get("lineupLocktimeType"),
                   "move_limit": roster.get("moveLimit")},
        "scoring": {"type": scoring.get("scoringType"), "items": items,
                    "kicker_and_dst_items": other, "slot_overrides": by_slot,
                    "matchup_tie": scoring.get("matchupTieRule"),
                    "playoff_tie": scoring.get("playoffMatchupTieRule"),
                    "home_bonus": scoring.get("homeTeamBonus")},
        "schedule": {"regular_season_weeks": reg,
                     "playoff_teams": sched.get("playoffTeamCount"),
                     "playoff_weeks": playoff_weeks,
                     "playoff_round_length": sched.get("playoffMatchupPeriodLength"),
                     "playoff_seeding": sched.get("playoffSeedingRule"),
                     "playoff_reseed": sched.get("playoffReseed"),
                     "divisions": len(sched.get("divisions") or [])},
        "waivers": {"type": acq.get("acquisitionType"), "hours": acq.get("waiverHours"),
                    "process_days": acq.get("waiverProcessDays"),
                    "process_hour": acq.get("waiverProcessHour"),
                    "faab": acq.get("isUsingAcquisitionBudget"),
                    "budget": acq.get("acquisitionBudget") if acq.get("isUsingAcquisitionBudget") else None,
                    "season_limit": acq.get("acquisitionLimit"),
                    "per_matchup_limit": acq.get("matchupAcquisitionLimit")},
        "trades": {"max": trade.get("max"), "review_hours": trade.get("revisionHours"),
                   "veto_votes": trade.get("vetoVotesRequired"),
                   "deadline_ms": deadline},
        "byes": {"teams_on_bye_by_week": dict(sorted(per_week.items())),
                 "last_bye_week": max(per_week) if per_week else None,
                 "byes_in_playoffs": [w for w in per_week if w in playoff_weeks]},
    }


def lineup_value(board: pd.DataFrame, picks: list[dict],
                 league: LeagueSettings) -> dict:
    """One team's picks scored as projected starter points: the best lineup they
    fill under the league's starting slots, plus bench projection and the
    starting slots still empty.

    Picks the board cannot model (kickers, defenses, unmodelled players) count
    toward the position they were recorded under with 0 projected points, so the
    slot shows filled while the number stays honest. `picks` is any list of
    `{"name", "position"}` dicts, so a simulated roster scores the same way a
    recorded one does.

    A pick that carries its own `proj_points` is taken at its word, position and
    all, and must carry a `position` too -- both or neither. Recorded picks carry
    neither, so `team_strength` is unaffected; a caller that knows exactly which
    board row it took (`replay.counterfactual_draft`) carries both, which is the
    only way to score two board rows sharing one normalised name without both
    resolving to whichever came last. Half a pick is refused rather than
    half-resolved: falling back to the name for the position would reintroduce
    exactly the ambiguity the projection was passed to close.

    A missing projection is worth 0, and so is a NaN one. Those are not the same
    statement in Python: NaN is truthy, so the obvious `proj.get(key) or 0.0`
    hands back the NaN, and one unprojected player turns a whole team's
    `starters_proj` into NaN instead of under-counting it by that player."""
    proj = dict(zip(board["_key"], board["proj_points"])) if "_key" in board.columns else {}
    pos_of = dict(zip(board["_key"], board["position"])) if "_key" in board.columns else {}
    starters = {p: n for p, n in league.starters.items() if n and p in ("QB", "RB", "WR", "TE")}
    flex = league.starters.get("FLEX", 0)

    def points(raw) -> float:
        if raw is None:
            return 0.0
        value = float(raw)
        return value if np.isfinite(value) else 0.0

    have: dict[str, list[float]] = {}
    for p in picks:
        key = norm_name(p["name"])
        given = p.get("proj_points")
        if given is None:
            pos, value = pos_of.get(key) or p.get("position"), points(proj.get(key))
        elif not p.get("position"):
            raise ValueError(
                f"lineup_value: pick {p.get('name')!r} carries proj_points but no position; "
                "a caller that knows which board row it took must give both")
        else:
            pos, value = p["position"], points(given)
        if not pos:
            continue
        have.setdefault(str(pos), []).append(value)
    have = {pos: sorted(v, reverse=True) for pos, v in have.items()}
    start = sum(sum(have.get(pos, [])[:n]) for pos, n in starters.items())
    leftovers = sorted((v for pos, n in starters.items() for v in have.get(pos, [])[n:]
                        if pos in league.flex_eligible), reverse=True)
    start += sum(leftovers[:flex])
    bench = sum(leftovers[flex:])
    empty = sum(max(0, n - len(have.get(pos, []))) for pos, n in starters.items())
    empty += max(0, flex - len(leftovers))
    return {"starters_proj": round(start), "bench_proj": round(bench),
            "open_starter_slots": empty, "picks": sum(len(v) for v in have.values())}


def team_strength(board: pd.DataFrame, state: DraftState,
                  labels: dict[int, str] | None = None) -> pd.DataFrame:
    """Every team's draft so far, scored as projected starter points by
    `lineup_value`, sorted strongest first."""
    by_slot: dict[int, list[dict]] = {}
    for p in state.picks:
        by_slot.setdefault(p["slot"], []).append(p)
    rows = []
    for slot in range(1, state.league.teams + 1):
        value = lineup_value(board, by_slot.get(slot, []), state.league)
        rows.append({"slot": slot, "team": (labels or {}).get(slot, f"slot {slot}"),
                     **value, "mine": slot == state.my_slot})
    out = pd.DataFrame(rows).sort_values("starters_proj", ascending=False).reset_index(drop=True)
    out.insert(0, "rank", range(1, len(out) + 1))
    return out


def espn_league_directory(league_id: str, season: int = CURRENT_SEASON,
                          swid: str | None = None, espn_s2: str | None = None) -> dict[int, dict]:
    """ESPN team id -> team name and owner display names, for labelling room events."""
    swid = swid or os.environ.get("ESPN_SWID")
    espn_s2 = espn_s2 or os.environ.get("ESPN_S2")
    cookies = {}
    if swid and espn_s2:
        cookies = {"SWID": swid if swid.startswith("{") else f"{{{swid}}}",
                   "espn_s2": espn_s2}
    url = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}"
           f"/segments/0/leagues/{league_id}")
    resp = requests.get(url, params={"view": ["mTeam"]}, cookies=cookies, timeout=20,
                        headers={"User-Agent": "ffdraft-mcp/1.0"})
    resp.raise_for_status()
    return league_directory_from_mteam(resp.json())


# Shown wherever an owner cannot be named. Never the SWID: it identifies a
# person, and a display-name fallback puts it into channel messages and reports.
UNKNOWN_OWNER = "unknown member"


def mteam_member_names(data: dict) -> dict[str, str]:
    """Owner SWID (upper case, no braces) -> display name, from an `mTeam` payload.

    The key is the caller's; the value is all that may be shown. Keep the two
    apart: the SWID identifies a person and does not belong in any report.
    """
    members = {}
    for m in data.get("members") or []:
        full = " ".join(filter(None, [m.get("firstName"), m.get("lastName")])).strip()
        members[str(m["id"]).strip("{}").upper()] = full or m.get("displayName") or ""
    return members


def league_directory_from_mteam(data: dict) -> dict[int, dict]:
    """ESPN team id -> team name, owner display names and owner SWIDs, from an
    `mTeam` payload.

    Split out from `espn_league_directory` so a saved `read_api/mTeam.json`
    from `espn_dump` reads the same way as the live view.

    An owner the payload's member list does not name reads `UNKNOWN_OWNER`, not
    his SWID. A SWID identifies a person and is not a display name: the moment
    it is used as one it ends up in a channel message or a report. `owner_ids`
    keeps the SWIDs available for joining, separately and deliberately.
    """
    members = mteam_member_names(data)
    out = {}
    for t in data.get("teams") or []:
        name = (t.get("name") or " ".join(filter(None, [t.get("location"), t.get("nickname")]))).strip()
        ids = [str(o).strip("{}").upper() for o in t.get("owners", [])]
        out[int(t["id"])] = {"name": name,
                             "owners": [members.get(i) or UNKNOWN_OWNER for i in ids],
                             "owner_ids": ids}
    return out


def parse_pasted_board(text: str) -> list[str]:
    """Best-effort parse of a pasted list of drafted players.

    Handles the shapes people actually paste: numbered lists, 'Round 3, Pick 7 - Name',
    comma-separated runs, and raw one-per-line names.
    """
    names = []
    # 'Round 3, Pick 7' contains a comma, so it must go before the comma split.
    text = re.sub(r"round\s*\d+\s*,?\s*pick\s*\d+\s*[-–—:]?", "", text, flags=re.I)
    for chunk in re.split(r"[\n,;]+", text):
        s = chunk.strip()
        if not s:
            continue
        s = re.sub(r"^\s*(?:R?\d+[.):]|\d+\.\d+)\s*[-–—:]?\s*", "", s)
        s = re.sub(r"\s*[-–—(]\s*(QB|RB|WR|TE|K|D/?ST|DEF)\b.*$", "", s, flags=re.I)
        s = re.sub(r"\s+[A-Z]{2,3}$", "", s).strip()
        # First token may be dotted initials: A.J. Brown, T.J. Hockenson, J.K. Dobbins.
        if len(s) > 2 and re.search(r"[A-Za-z.']{2,}\s+[A-Za-z]{2,}", s):
            names.append(s)
    return names
