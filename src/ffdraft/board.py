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
from .config import CURRENT_SEASON, STATE_DIR, LeagueSettings

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
                     "position": _ESPN_POSITION_NAMES.get(str(p.get("defaultPositionId"))),
                     "espn_rank": ((p.get("draftRanksByRankType") or {}).get("PPR") or {}).get("rank"),
                     "percent_owned": (p.get("ownership") or {}).get("percentOwned"),
                     "espn_proj": espn_season_projection(p, season),
                     "espn_injury": p.get("injuryStatus")})
    out = pd.DataFrame(rows, columns=["name", "adp", "espn_id", "position", "espn_rank",
                                      "percent_owned", "espn_proj", "espn_injury"])
    out["_key"] = out["name"].map(norm_name)
    out["source"] = "espn_adp"
    return out.dropna(subset=["adp"]).drop_duplicates("_key").reset_index(drop=True)


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
            from .model import ROLE_DISAGREEMENT

            r = recommendations
            ratio = pd.to_numeric(r["espn_proj"], errors="coerce") / r["proj_points"]
            low = r[ratio.notna() & (ratio < ROLE_DISAGREEMENT)]
            if not low.empty:
                warnings.append("ESPN projects far below the model (role changed?): "
                                + "; ".join(f"{n} {e:.0f} vs {p:.0f}" for n, e, p in
                                            zip(low["name"], low["espn_proj"], low["proj_points"])))
            unknown = r[pd.to_numeric(r["espn_proj"], errors="coerce").isna()]
            if not unknown.empty:
                warnings.append(f"no ESPN projection for: {unknown['name'].tolist()}")

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
MARKET_JOIN_VERSION = 2


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
    return {"unjoined": unjoined, "unjoined_total": int(len(un)),
            "alias_joined": alias, "alias_joined_total": alias_total,
            "key_only": key_only, "key_only_total": key_only_total}


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
        extra = [c for c in ("espn_proj", "espn_injury", "espn_rank") if c in adp.columns]
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
        """Board rows for the players you have drafted, in pick order."""
        keys = [norm_name(p["name"]) for p in self.picks if p["slot"] == self.my_slot]
        if "_key" not in board.columns or not keys:
            return board.iloc[0:0]
        return board[board["_key"].isin(keys)]

    def my_roster(self, board: pd.DataFrame) -> dict[str, int]:
        mine = [p for p in self.picks if p["slot"] == self.my_slot]
        counts: dict[str, int] = {}
        idx = board.set_index("_key")["position"].to_dict() if "_key" in board.columns else {}
        for p in mine:
            pos = idx.get(norm_name(p["name"])) or p.get("position")
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


def team_strength(board: pd.DataFrame, state: DraftState,
                  labels: dict[int, str] | None = None) -> pd.DataFrame:
    """Every team's draft so far, scored as projected starter points: the best
    lineup its picks can fill under the league's starting slots, plus bench
    projection and the starting slots still empty. Sorted strongest first.

    Picks the board cannot model (kickers, defenses, unmodelled players) count
    toward the position they were recorded under with 0 projected points, so
    the slot shows filled while the number stays honest."""
    proj = dict(zip(board["_key"], board["proj_points"])) if "_key" in board.columns else {}
    pos_of = dict(zip(board["_key"], board["position"])) if "_key" in board.columns else {}
    starters = {p: n for p, n in state.league.starters.items()
                if n and p in ("QB", "RB", "WR", "TE")}
    flex = state.league.starters.get("FLEX", 0)
    by_slot: dict[int, dict[str, list[float]]] = {}
    for p in state.picks:
        key = norm_name(p["name"])
        pos = pos_of.get(key) or p.get("position")
        if not pos:
            continue
        by_slot.setdefault(p["slot"], {}).setdefault(pos, []).append(float(proj.get(key) or 0.0))
    rows = []
    for slot in range(1, state.league.teams + 1):
        have = {pos: sorted(v, reverse=True) for pos, v in by_slot.get(slot, {}).items()}
        start = sum(sum(have.get(pos, [])[:n]) for pos, n in starters.items())
        leftovers = sorted((v for pos, n in starters.items() for v in have.get(pos, [])[n:]
                            if pos in state.league.flex_eligible), reverse=True)
        start += sum(leftovers[:flex])
        bench = sum(leftovers[flex:])
        empty = sum(max(0, n - len(have.get(pos, []))) for pos, n in starters.items())
        empty += max(0, flex - len(leftovers))
        rows.append({"slot": slot, "team": (labels or {}).get(slot, f"slot {slot}"),
                     "starters_proj": round(start), "bench_proj": round(bench),
                     "open_starter_slots": empty,
                     "picks": sum(len(v) for v in have.values()),
                     "mine": slot == state.my_slot})
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
    data = resp.json()
    members = {}
    for m in data.get("members") or []:
        full = " ".join(filter(None, [m.get("firstName"), m.get("lastName")])).strip()
        members[m["id"].strip("{}").upper()] = full or m.get("displayName") or ""
    out = {}
    for t in data.get("teams") or []:
        name = (t.get("name") or " ".join(filter(None, [t.get("location"), t.get("nickname")]))).strip()
        owners = [members.get(o.strip("{}").upper(), o) for o in t.get("owners", [])]
        out[int(t["id"])] = {"name": name, "owners": owners}
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
