"""In-season waiver claims: who to add, at what priority, and who to drop.

The draft is over; the tool now answers a different question every Tuesday. Four
signals, and only one of them has ever been backtested — every score carries how
it was measured, and `UNMEASURED` is the honest answer for three of them until
the role-change backtest lands (epic #45 milestone 3).

  role change        Snap and target share moving week over week, from the
                     nflverse weekly pipeline. UNMEASURED.
  projection lag     ESPN's own weekly projection against what the player's
                     recent usage produces. A pickup is a player whose usage has
                     moved and whose projection has not caught up. UNMEASURED.
  contingent value   `roles.handcuff_table`, made live by the starter's current
                     injury status. UNMEASURED.
  roster need        `roles.start_probabilities` against my own roster: a player
                     who cannot enter my lineup is not a claim. UNMEASURED.

Two facts about this league that the output has to carry, because both are
settings that read the other way at a glance (docs/data-sources.md):

  * `isUsingAcquisitionBudget` is false. It is a rolling waiver order, not FAAB,
    and `acquisitionBudget: 100` sits there populated and inert beside it.
  * `isBenchUnlimited` is true while there are six bench slots, so every claim
    has to name a drop.

Nothing here touches the network. The caller supplies the ESPN payloads; the
tool in `server.py` fetches them.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import requests

from . import roles
from .config import CURRENT_SEASON

READS_HOST = "https://lm-api-reads.fantasy.espn.com"
# The same filter `espn_dump` uses, so the pool here is the pool that dump
# captured and the field shapes in docs/data-sources.md describe both.
POOL_FILTER = {"players": {"filterStatus": {"value": ["FREEAGENT", "WAIVERS", "ONTEAM"]},
                           "limit": 2000,
                           "sortDraftRanks": {"sortPriority": 100, "sortAsc": True,
                                              "value": "PPR"}}}


def fetch_pool_and_settings(league_id: str, season: int = CURRENT_SEASON,
                            swid: str | None = None,
                            espn_s2: str | None = None) -> tuple[list[dict], dict]:
    """The free-agent pool and the league's settings, in one place.

    Two views, because a claim needs both: `kona_player_info` for who is
    available and `mSettings` for what a claim priority even means here. Split
    out from the tool so the tool is composition and the network is one function
    a test can replace.
    """
    swid = swid or os.environ.get("ESPN_SWID") or ""
    espn_s2 = espn_s2 or os.environ.get("ESPN_S2") or ""
    cookies = {"SWID": swid if swid.startswith("{") else f"{{{swid}}}",
               "espn_s2": espn_s2} if swid and espn_s2 else {}
    url = f"{READS_HOST}/apis/v3/games/ffl/seasons/{season}/segments/0/leagues/{league_id}"
    headers = {"User-Agent": "ffdraft-mcp/1.0", "X-Fantasy-Source": "kona"}
    pool = requests.get(url, params={"view": "kona_player_info"}, cookies=cookies,
                        timeout=30,
                        headers={**headers, "X-Fantasy-Filter": json.dumps(POOL_FILTER)})
    pool.raise_for_status()
    settings = requests.get(url, params={"view": "mSettings"}, cookies=cookies,
                            timeout=30, headers=headers)
    settings.raise_for_status()
    return (pool.json().get("players") or []), (settings.json().get("settings") or {})

# What is known about a score's accuracy. A score with no backtest says so in
# every row it appears in rather than in a docstring nobody reads at 11am on a
# Tuesday.
UNMEASURED = "unmeasured"
# The shape of a field this dump could not exercise: the capture was taken
# mid-draft, so the free-agent split and the ownership move are documented but
# unverified until the first in-season pull.
UNVERIFIED_SHAPE = "unverified-shape"

# Role entropy is the one score with a real predictive result, and it is carried
# verbatim rather than summarised: mean absolute percentage error against real
# season points, by entropy tercile, on leak-free boards.
ROLE_ENTROPY_EVIDENCE = ("monotonic in two seasons: 0.381/0.529/0.707 mean absolute "
                         "percentage error by entropy tercile in 2024 (n 356) and "
                         "0.366/0.510/0.704 in 2025 (n 347)")

# Weeks either side of the split when measuring a role change. Two recent weeks
# against the three before them: one week is a game script, and a window longer
# than three weeks stops being "this changed" and becomes "this is who he is".
RECENT_WEEKS = 2
PRIOR_WEEKS = 3
# Below this many recent appearances there is no week-over-week anything.
MIN_RECENT_GAMES = 1

# ESPN injury states that vacate a role. QUESTIONABLE does not: it is the
# default state of half the league by Friday.
OUT_STATUSES = ("OUT", "INJURY_RESERVE", "DOUBTFUL", "SUSPENSION", "NA")


@dataclass
class LeagueRules:
    """The acquisition and roster settings a claim has to respect."""

    uses_faab: bool = False
    acquisition_type: str = "WAIVERS_TRADITIONAL"
    budget: int = 0
    minimum_bid: int = 0
    bench_slots: int = 0
    uses_undroppable_list: bool = False
    position_limits: dict[str, int] = field(default_factory=dict)

    @property
    def priority_basis(self) -> str:
        """What a claim priority means here, in the league's own terms."""
        if self.uses_faab:
            return f"FAAB bid out of {self.budget}, minimum {self.minimum_bid}"
        return f"waiver order ({self.acquisition_type}); FAAB is off in this league"


def league_rules_from_settings(settings: dict) -> LeagueRules:
    """Read the acquisition and roster settings that bind a claim.

    `isUsingAcquisitionBudget` decides which recommendation is even meaningful.
    `acquisitionBudget` and `minimumBid` are populated whether or not FAAB is on,
    so reading them first is how a tool ends up recommending bids to a league
    that runs a waiver order.

    `isBenchUnlimited` is likewise not the bench size: this league reports it
    true while `lineupSlotCounts["20"]` is 6. The slot count is the fact.
    """
    acq = (settings or {}).get("acquisitionSettings") or {}
    roster = (settings or {}).get("rosterSettings") or {}
    slots = roster.get("lineupSlotCounts") or {}
    limits = {str(k): int(v) for k, v in (roster.get("positionLimits") or {}).items()}
    return LeagueRules(
        uses_faab=bool(acq.get("isUsingAcquisitionBudget")),
        acquisition_type=str(acq.get("acquisitionType") or "WAIVERS_TRADITIONAL"),
        budget=int(acq.get("acquisitionBudget") or 0),
        minimum_bid=int(acq.get("minimumBid") or 0),
        bench_slots=int(slots.get("20") or 0),
        uses_undroppable_list=bool(roster.get("isUsingUndroppableList")),
        position_limits=limits,
    )


def free_agents(players: list[dict]) -> pd.DataFrame:
    """The unrostered pool from a `kona_player_info` payload.

    `status` is FREEAGENT / WAIVERS / ONTEAM and `onTeamId` is 0 when nobody
    holds him. The shape is documented but **unverified**: the capture this was
    written against was taken mid-draft, when ESPN reports every player as a
    free agent, so the split has never been exercised. Verified at the first
    in-season pull, not before.
    """
    rows = []
    for entry in players or []:
        p = entry.get("player") or {}
        own = p.get("ownership") or {}
        rows.append({
            "espn_id": p.get("id"),
            "name": p.get("fullName"),
            "position_id": p.get("defaultPositionId"),
            "status": entry.get("status"),
            "on_team_id": entry.get("onTeamId") or 0,
            "injury_status": p.get("injuryStatus"),
            # None means the pull did not carry this player, not "droppable".
            "droppable": p.get("droppable"),
            "percent_owned": own.get("percentOwned"),
            "percent_change": own.get("percentChange"),
            "percent_started": own.get("percentStarted"),
        })
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    available = (out["status"].isin(["FREEAGENT", "WAIVERS"])) & (out["on_team_id"] == 0)
    return out[available].reset_index(drop=True)


def role_change(weekly: pd.DataFrame, snaps: pd.DataFrame, season: int,
                week: int) -> pd.DataFrame:
    """How far each player's role moved in the last `RECENT_WEEKS` weeks.

    Share of his own team's targets and carries, and his share of its offensive
    snaps, in weeks `[week - RECENT_WEEKS + 1, week]` against the
    `PRIOR_WEEKS` before them. A player with no prior window is a new role
    rather than a changed one and carries `prior_games` 0, which the caller can
    tell apart from a flat one.

    UNMEASURED: nothing yet shows a role change through week w predicts points
    in w+1..w+4. That is milestone 3, and until it lands this ranks players by a
    quantity whose predictive value is unknown.
    """
    w = weekly[(weekly["season"] == season) & (weekly["season_type"] == "REG")].copy()
    for col in ("targets", "carries"):
        w[col] = pd.to_numeric(w.get(col), errors="coerce").fillna(0.0)
    recent_lo = week - RECENT_WEEKS + 1
    prior_lo = recent_lo - PRIOR_WEEKS
    windows = {"recent": (recent_lo, week), "prior": (prior_lo, recent_lo - 1)}

    frames = {}
    for label, (lo, hi) in windows.items():
        chunk = w[(w["week"] >= max(1, lo)) & (w["week"] <= hi)]
        team_totals = chunk.groupby(["recent_team"], observed=True).agg(
            team_targets=("targets", "sum"), team_carries=("carries", "sum"))
        per = chunk.groupby(["player_id", "player_display_name", "recent_team"],
                            observed=True).agg(
            targets=("targets", "sum"), carries=("carries", "sum"),
            games=("week", "nunique"),
            points=("fantasy_points_ppr", "sum")).reset_index()
        per = per.join(team_totals, on="recent_team")
        per[f"{label}_target_share"] = per["targets"] / per["team_targets"].replace(0, np.nan)
        per[f"{label}_carry_share"] = per["carries"] / per["team_carries"].replace(0, np.nan)
        per[f"{label}_games"] = per["games"]
        per[f"{label}_points"] = per["points"]
        frames[label] = per.set_index("player_id")[[
            "player_display_name", f"{label}_target_share", f"{label}_carry_share",
            f"{label}_games", f"{label}_points"]]

    out = frames["recent"].join(frames["prior"].drop(columns=["player_display_name"]),
                                how="left")
    out = out.rename(columns={"player_display_name": "name"})
    for col in ("prior_target_share", "prior_carry_share"):
        out[col] = out[col].fillna(0.0)
    out["prior_games"] = out["prior_games"].fillna(0)
    # Snap counts are keyed by name and the frame above by player_id, so this
    # maps rather than joins. Getting that wrong is silent: the join succeeds,
    # every snap share is NaN, and the sum below quietly drops a third of the
    # signal instead of failing.
    shares = _snap_shares(snaps, season, windows)
    for col in shares.columns:
        out[col] = out["name"].map(shares[col])
    out["target_share_change"] = out["recent_target_share"] - out["prior_target_share"]
    out["carry_share_change"] = out["recent_carry_share"] - out["prior_carry_share"]
    out["snap_share_change"] = out["recent_snap_share"] - out["prior_snap_share"]
    # One number to rank on, and it is a sum of shares of the same kind rather
    # than a weighted blend: no weight here has been measured, so inventing one
    # would be a claim.
    out["role_change"] = (out["target_share_change"].fillna(0.0)
                          + out["carry_share_change"].fillna(0.0)
                          + out["snap_share_change"].fillna(0.0))
    out["role_change_evidence"] = UNMEASURED
    return out[out["recent_games"] >= MIN_RECENT_GAMES].reset_index()


def _snap_shares(snaps: pd.DataFrame, season: int, windows: dict) -> pd.DataFrame:
    """Mean offensive snap share per window, keyed by player name.

    Snap counts are keyed by name, not by `player_id`, the same join
    `features.player_season_profiles` makes.
    """
    s = snaps[(snaps["season"] == season) & (snaps["game_type"] == "REG")]
    out = {}
    for label, (lo, hi) in windows.items():
        chunk = s[(s["week"] >= max(1, lo)) & (s["week"] <= hi)]
        out[f"{label}_snap_share"] = chunk.groupby("player", observed=True)[
            "offense_pct"].mean()
    return pd.DataFrame(out)


def projection_lag(espn_weekly_proj: pd.Series, recent_points_per_game: pd.Series
                   ) -> pd.DataFrame:
    """Where ESPN's weekly projection sits against what the player is producing.

    Positive means his recent per-game output is ahead of what ESPN projects for
    him next week: the market has not repriced the role. That is the buy case,
    and it is the reason ESPN's projection is worth carrying at all — it is the
    thing usage gets measured against, not a second opinion to average with.

    UNMEASURED.
    """
    proj = pd.to_numeric(espn_weekly_proj, errors="coerce")
    actual = pd.to_numeric(recent_points_per_game, errors="coerce")
    out = pd.DataFrame({"espn_weekly_proj": proj, "recent_ppg": actual})
    out["projection_lag"] = out["recent_ppg"] - out["espn_weekly_proj"]
    out["projection_lag_evidence"] = UNMEASURED
    return out


def starters_out(board: pd.DataFrame, injury_by_name: dict[str, str | None]) -> set[str]:
    """Players whose ESPN status has taken them out of a lineup this week.

    QUESTIONABLE is excluded deliberately: by Friday it describes half the
    league and would make every backup a handcuff.

    This is "is out now", not "changed this week". Detecting a change needs last
    week's statuses, and nothing stores them yet — so the claim this supports is
    the weaker one, and it is labelled as such rather than dressed up.
    """
    del board
    return {name for name, status in (injury_by_name or {}).items()
            if status and str(status).upper() in OUT_STATUSES}


def contingent_value(board: pd.DataFrame, out_now: set[str]) -> pd.DataFrame:
    """Each player's handcuff value, live only when his starter is out.

    `roles.handcuff_table` already prices the contingency; this is the switch
    that says the contingency has actually happened.

    UNMEASURED. The handcuff term's own paired-draft numbers were measured on a
    pre-#26 harness and retired, and they were about drafting rather than
    claiming in any case.
    """
    hc = roles.handcuff_table(board)
    out = hc[["starter", "contingent_points"]].copy()
    if "name" in board.columns:
        out.insert(0, "name", board["name"])
    out["starter_is_out"] = out["starter"].isin(out_now).fillna(False)
    out["contingent_value"] = out["contingent_points"].where(out["starter_is_out"], 0.0)
    out["contingent_value_evidence"] = UNMEASURED
    return out


def drop_candidate(bench: pd.DataFrame, league, mine: pd.DataFrame | None = None) -> dict:
    """The bench player who costs least to lose, and why he is droppable.

    Ranked by `roles.bench_values` — what he is worth to *this* roster once the
    weeks he would actually start are priced — not by projection, because a
    fourth running back projecting well is still a fourth running back.

    ESPN's undroppable list is `player.droppable` on the `kona_player_info` row,
    already in the view we capture. A player it forbids is not offered. A player
    the pull did not carry has no value there, which is not the same as
    droppable, so he is offered with `undroppable_checked` false.

    `projection_basis` says what the recommendation rests on, because it does not
    always rest on a real projection. #40 (`board.UNPRICED`, not merged at the
    time of writing) makes `DraftState.my_rows` return a stand-in for a roster
    player the board cannot price, at the position's replacement level with `vor`
    0 and no `bye_week`. Once a `my_rows` frame is wired in here — the tool
    milestone, not this one — some bench rows carry a synthetic number the board
    has no opinion about, and a drop recommendation resting on one should say so
    rather than read like a measurement. Flagged by marge.

    Measured rather than assumed, because her prediction was that the stand-in
    would tend to look cheapest and that is only conditionally true. Replacement
    level is by construction *above* a genuinely deep bench player, so the
    stand-in is chosen only when every other bench player projects above
    replacement. With a real player at 90 against a stand-in at 120 the real one
    is still the cheaper drop (bench values 4.5 against 6.0); with the rest of
    the bench at 180 and 200 the stand-in wins it at 6.0. Both are plausible
    weeks. The flag matters in the first case too, since what changes is not
    who is picked but whether the number behind him is real.
    """
    if bench is None or bench.empty:
        return {"player": None, "reason": "no bench player to drop"}
    values = roles.bench_values(bench, league, mine)
    ranked = bench.assign(bench_value=values["bench_value"].to_numpy(),
                          p_start=values["p_start"].to_numpy())
    allowed = ranked[ranked.get("droppable").ne(False)] if "droppable" in ranked.columns \
        else ranked
    if allowed.empty:
        return {"player": None,
                "reason": "every bench player is on the league's undroppable list"}
    worst = allowed.nsmallest(1, "bench_value").iloc[0]
    checked = bool("droppable" in allowed.columns and pd.notna(worst.get("droppable")))
    # Read by name rather than through `board.UNPRICED`, which does not exist on
    # every head this runs against yet. A frame without the column simply has no
    # stand-ins in it, which is the honest answer for a board that prices
    # everyone it holds.
    #
    # Reading by name buys four states where an imported constant had two:
    # absent, np.False_, np.True_, and NaN. A bench assembled from two sources —
    # one frame carrying the flag, one not — concatenates to object dtype with
    # NaN in the rows that lacked it, and NaN is truthy, so a bare `bool()`
    # labels a real board-priced player a stand-in. That inverts what this note
    # is for: it would tell the user the figure is not a projection of his own
    # when it is, and a note that fires wrongly teaches the reader to discount
    # the honest ones. marge, who also caught that it becomes reachable at
    # exactly the wiring milestone.
    #
    # `is True` does not work here either: a real bool column yields `np.True_`,
    # which is not the Python singleton, so it would silently stop firing on the
    # path that matters. notna first, then truth.
    unpriced = bool(pd.notna(worst.get("unpriced")) and worst.get("unpriced"))
    return {
        "player": str(worst["name"]),
        "position": str(worst.get("position")),
        "bench_value": round(float(worst["bench_value"]), 1),
        "starts_in_a_given_week": round(float(worst["p_start"]), 2),
        "undroppable_checked": checked,
        "projection_basis": "replacement-level stand-in" if unpriced else "board projection",
        "reason": ("lowest bench value on the roster: worth "
                   f"{float(worst['bench_value']):.0f} points once the weeks he would "
                   f"actually start are priced"
                   + ("" if not unpriced else
                      "; the board cannot price this player, so that figure is the "
                      "position's replacement level standing in for him rather than a "
                      "projection of his own")
                   + ("" if checked else
                      "; ESPN's droppable flag was not in the pull for this player, so "
                      "the league's undroppable list is unchecked for him")),
    }


def claim_priority(rank: int, rules: LeagueRules) -> dict:
    """What to spend on a claim, in the units this league actually uses."""
    return {"order": rank, "basis": rules.priority_basis,
            "faab_bid": None if not rules.uses_faab else max(rules.minimum_bid, 1)}


def rank_claims(pool: pd.DataFrame, changes: pd.DataFrame, contingency: pd.DataFrame,
                league, rules: LeagueRules, mine: pd.DataFrame | None,
                bench: pd.DataFrame | None, limit: int = 8) -> list[dict]:
    """The ranked claim list: who to add, at what priority, dropping whom.

    Two reasons a player is here, and each row says which: his role moved, or
    his starter is out. They are listed rather than traded off, because trading
    them off needs a rate — how much contingent value equals how much role
    change — and no such rate has been measured.

    Ordering inside the list is role-movers first by `role_change`, then live
    contingencies by `contingent_value`. That is a stated policy about which
    question to read first, not a claim that one is worth more.

    There is no weighted blend, and that is deliberate — but it is not the
    absence of a choice. It is weight 1 on `role_change` and 0 on the rest for
    ordering, which is a maximally strong UNMEASURED choice; marge's point, and
    the docstring says it that way so the next reader does not think a decision
    was avoided. It is still the better call: the four numbers stay in the row
    where a human can override them, where a soft blend would hide four invented
    weights inside one score nobody can decompose. What licenses changing it is
    milestone 3's backtest, and until that lands no weight from anyone.

    The contingency is resolved for the **whole pool before truncation**. It used
    to be read after `.head(limit)`, which meant it was only ever consulted for
    rows that had already survived a cut made on `role_change` — and a handcuff's
    role_change is 0.000 by construction, because a role that has not moved is
    what makes him a handcuff. In any pool larger than `limit` he sat in a block
    of ties with no breaker, so whether the user saw a live 30-point contingency
    depended on the order of rows in the input frame. Found by marge.
    """
    if pool is None or pool.empty or changes.empty:
        return []
    by_name = changes.set_index("name")
    rows = []
    for _, p in pool.iterrows():
        name = str(p.get("name"))
        if name not in by_name.index:
            continue
        c = by_name.loc[name]
        rows.append({
            "player": name,
            "position": str(p.get("position") or ""),
            "percent_owned": p.get("percent_owned"),
            "percent_change": p.get("percent_change"),
            "role_change": round(float(c["role_change"]), 3),
            "target_share_change": round(float(c["target_share_change"] or 0.0), 3),
            "snap_share_change": (round(float(c["snap_share_change"]), 3)
                                  if pd.notna(c["snap_share_change"]) else None),
            "recent_games": int(c["recent_games"]),
            "prior_games": int(c["prior_games"]),
        })
    if not rows:
        return []
    frame = pd.DataFrame(rows)

    # Resolved over the whole pool, before anything is cut.
    live: dict[str, float] = {}
    starter_of: dict[str, str] = {}
    if contingency is not None and not contingency.empty and "name" in contingency.columns:
        for _, c in contingency.iterrows():
            if c.get("starter_is_out"):
                live[str(c["name"])] = float(c["contingent_value"])
                starter_of[str(c["name"])] = str(c["starter"])
    frame["contingent_value"] = frame["player"].map(live).fillna(0.0)
    frame["handcuff_for"] = frame["player"].map(starter_of)
    frame["reason"] = np.where(
        frame["contingent_value"] > 0,
        np.where(frame["role_change"] > 0, "role moved; starter out", "starter out"),
        "role moved")

    # mergesort is stable and name breaks the remaining ties, so the answer does
    # not depend on the order of rows in the pool. Most of a real Tuesday pool
    # ties at role_change 0.000, and quicksort's ordering inside a tie is an
    # implementation detail — it decided which claims the user saw at all.
    movers = frame[frame["role_change"] > 0].sort_values(
        ["role_change", "player"], ascending=[False, True], kind="mergesort")
    contingents = frame[(frame["contingent_value"] > 0) & (frame["role_change"] <= 0)] \
        .sort_values(["contingent_value", "player"], ascending=[False, True],
                     kind="mergesort")
    out = pd.concat([movers, contingents]).head(limit)

    drop = drop_candidate(bench, league, mine) if bench is not None else {
        "player": None, "reason": "no bench supplied"}
    claims = []
    for rank, (_, r) in enumerate(out.iterrows(), start=1):
        claims.append({
            **r.to_dict(),
            # +0.0 normalises the negative zero the share arithmetic leaves
            # behind: -0.0 renders as "-0.0" in JSON and reads like a signal.
            "role_change": round(float(r["role_change"]), 3) + 0.0,
            "contingent_value": round(float(r["contingent_value"]), 1),
            "claim_priority": claim_priority(rank, rules),
            "drop": drop,
            "evidence": {
                "role_change": UNMEASURED,
                "projection_lag": UNMEASURED,
                "contingent_value": UNMEASURED,
                "roster_need": UNMEASURED,
                "role_entropy": ROLE_ENTROPY_EVIDENCE,
            },
            "shape": {
                "free_agent_pool": UNVERIFIED_SHAPE,
                "ownership_move": UNVERIFIED_SHAPE,
            },
        })
    return claims


def waiver_report(pool: pd.DataFrame, changes: pd.DataFrame, contingency: pd.DataFrame,
                  league, rules: LeagueRules, mine: pd.DataFrame | None = None,
                  bench: pd.DataFrame | None = None, limit: int = 8) -> dict:
    """The claim list with the census that makes an empty one readable.

    An empty `claims` has two completely different causes and they were the same
    value: a quiet week where nobody's role moved and nobody's starter is out,
    and a free-agent pull that returned nothing usable. Those are the two most
    different answers this tool has.

    It matters here rather than in general because the pool's shape is
    `UNVERIFIED_SHAPE` — the capture this was built against was taken mid-draft
    and reported every player as a free agent, so a malformed pull is a live
    possibility rather than a hypothetical, and a quiet week is exactly when it
    would be silent. Found by marge, who tested it rather than asserting it:
    twelve quiet free agents and a broken pull both returned `[]`.

    The census also keeps the filter's own work visible. With players who have no
    reason excluded from the list, "412 considered, 2 claimed" is the only place
    left that shows the filter ran at all.

    This is the entry point the tool calls, so the census cannot be forgotten by
    the caller that matters.
    """
    claims = rank_claims(pool, changes, contingency, league, rules, mine, bench, limit)
    empty_pool = pool is None or pool.empty or "name" not in pool.columns
    considered = 0 if empty_pool else len(pool)
    names: set[str] = set() if empty_pool else set(pool["name"].astype(str))
    with_usage, moved, out_now = 0, 0, 0
    if changes is not None and not changes.empty and "name" in changes.columns:
        seen = changes[changes["name"].astype(str).isin(names)]
        with_usage = len(seen)
        moved = int((seen["role_change"] > 0).sum())
    if contingency is not None and not contingency.empty and "name" in contingency.columns:
        live = contingency[contingency["name"].astype(str).isin(names)]
        out_now = int(live["starter_is_out"].fillna(False).sum())
    return {
        "census": {
            "considered": considered,
            "with_weekly_usage": with_usage,
            "role_moved": moved,
            "starter_out": out_now,
            "claimed": len(claims),
            # Named rather than inferred from a zero: the caller should never
            # have to tell a quiet week from a broken pull by reading counts.
            "status": "ok" if considered else "no free agents in the pool",
        },
        "claims": claims,
    }
