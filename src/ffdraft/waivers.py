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
from typing import Any

import numpy as np
import pandas as pd
import requests

from . import roles
from .config import CURRENT_SEASON
from .config import OUT_STATUSES as _OUT_STATUSES
from .names import normalize as norm_name

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

# Role change is no longer unmeasured, and what it measured is negative. Carried
# verbatim in every row for the same reason the entropy result is: a score whose
# backtest went against it must say so where it is read, not in a changelog.
ROLE_CHANGE_EVIDENCE = (
    "MEASURED AND NEGATIVE: over 2022-2025 the top 10 by role change scored 6.4 to "
    "10.1 fewer PPR points over the following four weeks than the top 10 by recent "
    "points per game, from the same undrafted pool. Both blocks agree in all four "
    "seasons (spreads 0.48-1.06) and the sign holds at all eight (recent, prior) "
    "windows tried, effects -7.1 to -8.6. Ranking claims by this score is worse "
    "than ranking them by what the player just scored")

# Weeks either side of the split when measuring a role change. Two recent weeks
# against the three before them: one week is a game script, and a window longer
# than three weeks stops being "this changed" and becomes "this is who he is".
RECENT_WEEKS = 2
PRIOR_WEEKS = 3

# What the claim list is ordered by, and what it is NOT ordered by.
#
# `role_change` had weight 1 here until its backtest ran. It lost to recent
# points per game in 64 of 64 blocks -- four seasons, both blocks, all eight
# (recent, prior) windows -- by 6.4 to 10.1 PPR points over the following four
# weeks, with block spreads of 0.48 to 1.06. So the ordering is the alternative
# that beat it, and the weight on role change is 0.
#
# Zero, not negative. The sign is consistent and we do not know why, and a term
# nobody can explain is not a feature just because it points somewhere reliably;
# inverting it would be fitting the direction of a result rather than acting on
# an understood mechanism. `just rolechange` is the licence for this weight
# being 0, the way the backtest was the licence for it being 1.
RANK_BY = "recent_points_per_game"
ROLE_CHANGE_RANK_WEIGHT = 0.0
# Below this many recent appearances there is no week-over-week anything.
MIN_RECENT_GAMES = 1

# ESPN injury states that vacate a role. QUESTIONABLE does not: it is the
# default state of half the league by Friday. Defined in `config` because
# `lineup` prices a week with the same rule; re-exported here so every existing
# `waivers.OUT_STATUSES` reference keeps working and there is still one list.
OUT_STATUSES = _OUT_STATUSES


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
                week: int, recent: int = RECENT_WEEKS,
                prior: int = PRIOR_WEEKS) -> pd.DataFrame:
    """How far each player's role moved in the last `recent` weeks.

    Share of his own team's targets and carries, and his share of its offensive
    snaps, in weeks `[week - recent + 1, week]` against the `prior` weeks before
    them. A player with no prior window is a new role rather than a changed one
    and carries `prior_games` 0, which the caller can tell apart from a flat one.

    The two windows are arguments rather than only constants so the backtest can
    sweep them. A window is a choice about what counts as "changed", and one
    measured at a single setting is a result about that setting; see
    `role_change_backtest`.

    MEASURED, and it went against this score. The docstring here used to say
    nothing showed a role change through week w predicts points in w+1..w+4, and
    that milestone 3 would decide. It has: ranking the undrafted pool by this
    number picks players who go on to score 6.4 to 10.1 fewer points over the
    next four weeks than ranking the same pool by recent points per game, in
    every season and at every window tried. `ROLE_CHANGE_EVIDENCE` carries the
    numbers, and the ordering that rests on them is `rank_claims`'s to answer
    for.
    """
    w = weekly[(weekly["season"] == season) & (weekly["season_type"] == "REG")].copy()
    for col in ("targets", "carries"):
        w[col] = pd.to_numeric(w.get(col), errors="coerce").fillna(0.0)
    recent_lo = week - int(recent) + 1
    prior_lo = recent_lo - int(prior)
    windows = {"recent": (recent_lo, week), "prior": (prior_lo, recent_lo - 1)}

    frames = {}
    for label, (lo, hi) in windows.items():
        chunk = w[(w["week"] >= max(1, lo)) & (w["week"] <= hi)]
        # Team totals per (team, WEEK), joined to the player's own week, then
        # summed over the window. Two defects fall out of the per-week join and
        # both were found by the milestone-3 backtest rather than by reading:
        #
        # A player traded mid-window used to produce one row per team, because
        # `recent_team` was in the grouping key. Six players in 2024 week 10
        # alone, and it was not a cosmetic duplicate: `rank_claims` does
        # `by_name.loc[name]`, which returns a FRAME for a duplicated label, and
        # `float()` of a two-row Series raises. `waiver_targets` would have
        # crashed with a traceback the first time a traded player sat on waivers,
        # which is a common way to end up there.
        #
        # And the denominator used to be the team's whole window even in weeks
        # the player did not play, so a man who missed a game looked like his
        # role had shrunk. That mixes availability into a measure of role, which
        # is the one thing it must not contain -- the tool's whole claim is about
        # the snaps he takes when he is out there.
        totals = chunk.groupby(["recent_team", "week"], observed=True).agg(
            team_targets=("targets", "sum"), team_carries=("carries", "sum"))
        per_week = chunk.join(totals, on=["recent_team", "week"])
        per = per_week.groupby("player_id", observed=True).agg(
            player_display_name=("player_display_name", "last"),
            targets=("targets", "sum"), carries=("carries", "sum"),
            team_targets=("team_targets", "sum"),
            team_carries=("team_carries", "sum"),
            games=("week", "nunique"),
            points=("fantasy_points_ppr", "sum")).reset_index()
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
    # One row per player, asserted rather than assumed: everything downstream
    # indexes this frame by name, and a duplicate there is a raise rather than a
    # wrong number.
    if out.index.has_duplicates:
        raise AssertionError("role_change produced more than one row for a player")
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
    out["role_change_evidence"] = ROLE_CHANGE_EVIDENCE
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

    Ordering is by `recent_points_per_game`, over both reasons together, because
    that is the alternative that beat `role_change` in the backtest. Role change
    still decides who is ON the list -- a role that moved is one of the two
    reasons a player is a claim at all -- but it no longer decides the order of
    it, and `ROLE_CHANGE_RANK_WEIGHT` is 0.

    There is no weighted blend, and that is deliberate — but it is not the
    absence of a choice. It is weight 1 on `role_change` and 0 on the rest for
    ordering, which is a maximally strong choice; marge's point, and the
    docstring says it that way so the next reader does not think a decision was
    avoided. The four numbers stay in the row where a human can override them,
    where a soft blend would hide four invented weights inside one score nobody
    can decompose.

    THE BACKTEST THAT WAS SUPPOSED TO LICENSE THAT WEIGHT HAS RUN, AND IT
    REFUSED IT. `role_change_backtest` found ordering by role change worse than
    ordering the same pool by recent points per game -- by 6.4 to 10.1 PPR points
    over the following four weeks, in all four seasons, both blocks, and all
    eight `(recent, prior)` windows tried: 64 blocks, every one negative, block
    spreads 0.48 to 1.06. `just rolechange` reproduces it and
    `just rolechange names` prints the rows behind one Tuesday.

    So the weight is now 0 and the order is the alternative that beat it. Role
    change stays in every row as a labelled observation carrying
    `ROLE_CHANGE_EVIDENCE`, and it is never a rank input. `just rolechange` is
    the licence for the weight being 0, exactly as the backtest was named as the
    licence for it being 1.

    Zero rather than negative, deliberately. The sign is consistent and nobody
    can say why, and a term we cannot explain is not a feature because it points
    somewhere reliably -- inverting it would be fitting the direction of a
    result rather than acting on an understood mechanism.

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
            # What the list is ordered by. Measured to beat role change over the
            # next four weeks in every season and at every window tried.
            "recent_points_per_game": round(
                float(c["recent_points"]) / max(1, int(c["recent_games"])), 2),
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

    # One ordering, by the measured-better number, over both reasons together.
    # It used to be two tiers -- role movers by `role_change`, then live
    # contingencies by `contingent_value` -- and the tiers were themselves a
    # role-change ordering, since tier membership was `role_change > 0`. Weight 0
    # means neither the key nor the tier.
    #
    # A consequence worth stating rather than discovering: a handcuff whose
    # starter is out has low recent points BY CONSTRUCTION -- he has not been
    # playing, which is what makes him a handcuff -- so he now sorts down the
    # list. `contingent_value` is still in his row, unmeasured and visible, for a
    # human to override on. Nothing measured says where he belongs; what is
    # measured is only that role change is the wrong key.
    #
    # mergesort is stable and `player` breaks the remaining ties, so the answer
    # does not depend on the order of rows in the pool. Most of a real Tuesday
    # pool ties, and quicksort's ordering inside a tie is an implementation
    # detail that decided which claims the user saw at all.
    # The two reasons are still the FILTER -- a player with neither is not a
    # claim and is not listed. Only the order changed. The first version of this
    # edit replaced the two tiers with a bare sort and silently dropped the
    # filter with them, because the filter had been living inside the tier
    # membership rather than anywhere it could be seen; eight tests caught it.
    has_reason = (frame["role_change"] > 0) | (frame["contingent_value"] > 0)
    out = frame[has_reason].sort_values([RANK_BY, "player"], ascending=[False, True],
                                        kind="mergesort").head(limit)

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
                "role_change": ROLE_CHANGE_EVIDENCE,
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
        "ranked_by": (f"{RANK_BY}; role change carries weight "
                      f"{ROLE_CHANGE_RANK_WEIGHT:.0f} and is reported, not ranked on "
                      f"-- {ROLE_CHANGE_EVIDENCE}"),
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


# ------------------------------------------------------ does role change work

# The horizon a claim is made for. You claim a man off waivers to start him over
# the next month, not to hold him for a season, so this is what the score has to
# predict if it is worth anything.
OUTCOME_WEEKS = 4
# A claim list is about this long, so this is the set a user actually acts on.
# The question is not "does role_change correlate with anything" but "are the
# ten it puts in front of you better than the ten something else would".
TOP_K = 10
# Positions a role change is defined for at all. A kicker has no target or carry
# share, and a defense is not a player.
SCORED_POSITIONS = ("RB", "WR", "TE")
# The waiver pool, as a PROXY: historical ownership is in no source here, so
# "unrostered" stands in as "nobody drafted him". `adp.preseason_ecr` is the last
# August consensus before the season, which is leak-free by construction, and a
# 16-team 14-round league drafts this many players. Anyone ranked worse than
# that, or absent from the list, went undrafted.
#
# THE PROXY THIS REPLACED WAS THE MEASUREMENT'S BIGGEST DEFECT, and it is worth
# the paragraph because it produced a large, consistent, wrong answer. The first
# version defined the pool as "outside the top N at his position by points scored
# SO FAR THIS SEASON", which sounds equivalent and is not: a star who misses
# five weeks has few points to date and lands in the pool. Reading one cohort's
# rows found Puka Nacua, Christian McCaffrey and T.J. Hockenson in a 2024 week-10
# "waiver pool" -- all rostered in every league in the country, all returning
# from injury, and so all carrying a high recent points per game with a huge four
# weeks ahead of them. That handed the points-ranked baseline a population of
# returning stars, which is not a comparison but a definition, and it did it
# silently: every number was internally consistent and the effect was stable
# across four seasons and eight windows.
#
# Preseason rank cannot go wrong that way, because missing games is not what puts
# a player on it.
DRAFTED_THROUGH = 16 * 14
# Regular-season weeks. Week 18 exists and is scored, but a claim made for
# weeks 15-18 is a playoff decision with different rules, so the last cohort
# whose whole outcome window is regular season is the last one measured.
LAST_REGULAR_WEEK = 18
# Below this many players a top-10 comparison is not a comparison.
MIN_POOL = 2 * TOP_K


def _undrafted_keys(season: int) -> set[str]:
    """Normalised names nobody drafted, by the August consensus before `season`.

    The ownership proxy. Leak-free: an August snapshot cannot know what happens
    in October, and unlike points-to-date it does not select for players who
    missed games -- which is the trap this replaced.

    A name absent from the consensus entirely is undrafted, which is the common
    case and the point: the waiver pool is mostly people nobody ranked.
    """
    from . import adp as adp_mod
    from .names import normalize as norm_name

    ecr = adp_mod.preseason_ecr(season)
    if ecr.empty:
        return set()
    del norm_name
    return set(ecr[ecr["ecr"] <= DRAFTED_THROUGH]["_key"])


def _positions(weekly: pd.DataFrame, season: int, week: int) -> pd.DataFrame:
    """Each player's position as of `week`, from the box scores themselves."""
    w = weekly[(weekly["season"] == season) & (weekly["season_type"] == "REG")
               & (weekly["week"] <= week)]
    return w.groupby("player_id", observed=True).agg(
        position=("position", "last")).reset_index()


def _points_ahead(weekly: pd.DataFrame, season: int, lo: int, hi: int) -> pd.Series:
    """PPR points per player over weeks [lo, hi]. A player with no row scored
    nothing available to a lineup, which is the honest number for a claim."""
    w = weekly[(weekly["season"] == season) & (weekly["season_type"] == "REG")
               & (weekly["week"] >= lo) & (weekly["week"] <= hi)]
    return w.groupby("player_id", observed=True)["fantasy_points_ppr"].sum()


def role_change_cohort(weekly: pd.DataFrame, snaps: pd.DataFrame, season: int,
                       week: int, recent: int = RECENT_WEEKS,
                       prior: int = PRIOR_WEEKS, weeks_ahead: int = OUTCOME_WEEKS,
                       top_k: int = TOP_K, min_prior_games: int = 0,
                       drafted_keys: set[str] | None = None) -> dict | None:
    """One Tuesday: rank the pool by role change, and by what they just scored.

    The comparison is against recent points per game rather than against nothing,
    because "rank the free agents by what they just scored" is what a waiver tool
    without this feature does, and it is the case the module claims to get right.
    Beating an empty pool would be no evidence at all -- almost any ranking beats
    a random draw from a pool that contains a few real players.

    Returns None when the week cannot be scored: no prior window, no outcome
    window, or a pool too small for a top-ten comparison to mean anything.
    """
    if week - recent + 1 - prior < 1:
        return None
    if week + weeks_ahead > LAST_REGULAR_WEEK:
        return None
    changes = role_change(weekly, snaps, season, week, recent, prior)
    if changes.empty:
        return None
    frame = changes.merge(_positions(weekly, season, week), on="player_id", how="left")
    frame = frame[frame["position"].isin(SCORED_POSITIONS)]
    drafted = drafted_keys if drafted_keys is not None else _undrafted_keys(season)
    keys = frame["name"].map(norm_name)
    pool = frame[~keys.isin(drafted)].copy()
    pool = pool[pool["recent_games"] > 0]
    if min_prior_games:
        pool = pool[pool["prior_games"] >= min_prior_games]
    if len(pool) < MIN_POOL:
        return None

    ahead = _points_ahead(weekly, season, week + 1, week + weeks_ahead)
    pool["points_ahead"] = pool["player_id"].map(ahead).fillna(0.0)
    pool["recent_ppg"] = pool["recent_points"] / pool["recent_games"]

    # mergesort and a name tiebreak, so a pool full of ties does not hand the
    # answer to the order of the input frame -- the same defect marge found in
    # `rank_claims`, and it would be worse here because it would move a measured
    # number rather than one row of a list.
    def top(column: str) -> pd.DataFrame:
        return pool.sort_values([column, "name"], ascending=[False, True],
                                kind="mergesort").head(top_k)

    by_role, by_points = top("role_change"), top("recent_ppg")
    keep = ["name", "position", "role_change", "recent_ppg", "recent_games",
            "prior_games", "points_ahead"]
    return {
        # The two lists themselves, so a result about them can be read rather
        # than only summarised. A backtest that reports a single number and
        # cannot show its rows is a backtest nobody can find the defect in.
        "top_by_role_change": by_role[keep].to_dict("records"),
        "top_by_recent_points": by_points[keep].to_dict("records"),
        "season": int(season), "week": int(week), "pool": int(len(pool)),
        "by_role_change": round(float(by_role["points_ahead"].mean()), 2),
        "by_recent_points": round(float(by_points["points_ahead"].mean()), 2),
        "pool_mean": round(float(pool["points_ahead"].mean()), 2),
        "effect": round(float(by_role["points_ahead"].mean()
                              - by_points["points_ahead"].mean()), 2),
        # How much of the answer the two rankings already share. An effect near
        # zero on a nine-of-ten overlap says the two disagree about one player,
        # not that the score is worthless.
        "overlap": int(len(set(by_role["name"]) & set(by_points["name"]))),
        # The diagnostic that turned out to carry the result. `role_change` is a
        # difference of shares and the prior share is filled with 0.0 for a
        # player who has no prior window, so a man who did not play at all in
        # those weeks scores his ENTIRE recent share as a change. `role_change`'s
        # own docstring says the caller can tell that apart by `prior_games` 0 --
        # and `rank_claims` does not.
        "top_with_no_prior_window": int((by_role["prior_games"] <= 0).sum()),
        "top_mean_prior_games": round(float(by_role["prior_games"].mean()), 2),
    }


def _effect_summary(rows: list[dict]) -> dict:
    """Pool block rows, keeping the spread and the agreement visible.

    Deliberately not `adp._block_summary`: that one reports `trials_changed` and
    `players_swapped`, which are facts about paired mock drafts and have no
    meaning here. Borrowing it would have filled those fields with something,
    and a number nobody can interpret is worse than a field that is absent. What
    carries over is the discipline and the arithmetic, `2 ** -(k - 1)` included.
    """
    gains = [r["effect"] for r in rows]
    agree = bool(gains) and (all(g > 0 for g in gains) or all(g < 0 for g in gains))
    return {
        "blocks": rows,
        "effect": round(float(np.mean(gains)), 2) if gains else None,
        "block_effects": gains,
        # The distance between blocks of the same configuration: this harness's
        # own noise for this term, and the number to read `effect` against.
        "block_spread": round(float(max(gains) - min(gains)), 2) if gains else None,
        # No agreement, no finding. A block at exactly 0 agrees with nothing.
        "blocks_agree": agree,
        # k blocks of a term that does nothing agree in sign with probability
        # 2^-(k-1). At two blocks that is one coin flip, so `blocks_agree: true`
        # is not a pass, and this sits beside it saying so.
        "blocks_agree_p_null": round(0.5 ** (len(rows) - 1), 4) if rows else None,
        "cohorts": sum(r["cohorts"] for r in rows),
    }


def role_change_backtest(seasons: list[int], recent: int = RECENT_WEEKS,
                         prior: int = PRIOR_WEEKS, weeks_ahead: int = OUTCOME_WEEKS,
                         top_k: int = TOP_K, blocks: int = 2,
                         min_prior_games: int = 0, progress=None) -> dict:
    """Does a role change through week w predict points in w+1..w+`weeks_ahead`?

    The claim `role_change`'s docstring has carried as UNMEASURED since it was
    written. Every Tuesday of every season is one cohort: take the pool, rank it
    by role change and by recent points per game, and compare what the top ten of
    each actually went on to score.

    Blocks are ALTERNATING weeks, not early-season against late. Two disjoint
    samples of the same process is the point, and the season's halves are not the
    same process -- a role change in week 5 is news and the same change in week
    13 is a fact everyone already has. Splitting early/late would measure the
    calendar and report it as harness noise.

    An effect whose blocks disagree in sign is a measurement of this harness and
    not of the score, and `verdict` says so in words rather than leaving it to be
    read off `blocks_agree`.
    """
    from . import sources

    def say(msg: str) -> None:
        if progress is not None:
            progress(msg)

    out_seasons = []
    for season in seasons:
        weekly = sources.weekly_stats([season])
        snaps = sources.snap_counts([season])
        # Once per season: the consensus does not move during one.
        drafted = _undrafted_keys(season)
        cohorts = []
        for week in range(1, LAST_REGULAR_WEEK + 1):
            row = role_change_cohort(weekly, snaps, season, week, recent, prior,
                                     weeks_ahead, top_k, min_prior_games, drafted)
            if row is not None:
                cohorts.append(row)
                say(f"{season} week {week}: pool {row['pool']}, "
                    f"role {row['by_role_change']:.1f} vs points "
                    f"{row['by_recent_points']:.1f} "
                    f"(overlap {row['overlap']}/{top_k}, "
                    f"{row['top_with_no_prior_window']}/{top_k} of the role top "
                    f"never played in the prior window)")
        if len(cohorts) < blocks:
            out_seasons.append({"season": int(season),
                                "error": f"only {len(cohorts)} scorable weeks"})
            continue
        rows = []
        for block in range(blocks):
            chunk = cohorts[block::blocks]
            rows.append({
                "block": block + 1,
                "weeks": [c["week"] for c in chunk],
                "cohorts": len(chunk),
                "by_role_change": round(float(np.mean(
                    [c["by_role_change"] for c in chunk])), 2),
                "by_recent_points": round(float(np.mean(
                    [c["by_recent_points"] for c in chunk])), 2),
                "pool_mean": round(float(np.mean([c["pool_mean"] for c in chunk])), 2),
                "effect": round(float(np.mean([c["effect"] for c in chunk])), 2),
                "mean_overlap": round(float(np.mean([c["overlap"] for c in chunk])), 1),
                "mean_top_with_no_prior_window": round(float(np.mean(
                    [c["top_with_no_prior_window"] for c in chunk])), 1),
            })
        out_seasons.append({"season": int(season), **_effect_summary(rows)})

    scored = [s for s in out_seasons if "error" not in s]
    agree = bool(scored) and all(s["blocks_agree"] for s in scored)
    return {
        "question": (f"do the top {top_k} by role change through week w outscore the "
                     f"top {top_k} by recent points per game over weeks "
                     f"w+1..w+{weeks_ahead}?"),
        "windows": {"recent_weeks": int(recent), "prior_weeks": int(prior),
                    "min_prior_games": int(min_prior_games)},
        "seasons": out_seasons,
        "blocks_agree": agree,
        "pool_definition": (f"undrafted by the August consensus before the season "
                            f"(preseason ECR worse than {DRAFTED_THROUGH}, or absent "
                            "from it) -- a leak-free PROXY for unrostered, because "
                            "historical ownership is in no source here"),
        "verdict": effect_verdict({"seasons": out_seasons, "blocks_agree": agree}),
    }


def effect_verdict(out: dict) -> str:
    """One line saying what this backtest's numbers will and will not carry.

    Its own rather than `adp.block_verdict` because that one speaks of drafts and
    improvements; the rule it states is the same and is stated the same way, so
    that a role-change result and a bye result cannot be summed up differently by
    whichever recipe printed them.
    """
    seasons = [s for s in out.get("seasons", []) if "error" not in s]
    if not seasons:
        return "nothing scored: no verdict"
    p_null = next((s.get("blocks_agree_p_null") for s in seasons
                   if s.get("blocks_agree_p_null") is not None), None)
    if not out.get("blocks_agree"):
        return ("the blocks disagree in sign in at least one season: this effect is "
                "inside the harness's own noise and supports no weight")
    odds = f" (one season's blocks agree by chance with probability {p_null})" if p_null \
        else ""
    return (f"every season's blocks agree in sign{odds}, so the sign is consistent — "
            "which is an observation, not a pass, and says nothing about the magnitude")


def window_sweep(seasons: list[int], windows: list[tuple[int, int]],
                 weeks_ahead: int = OUTCOME_WEEKS, top_k: int = TOP_K,
                 blocks: int = 2, min_prior_games: int = 0, progress=None) -> dict:
    """The same measurement at several `(recent, prior)` settings.

    One window is a choice about what counts as "changed", and a result measured
    at a single setting is a result about that setting. Reporting the sweep is
    also the only way to see the shape that matters most: a term whose sign
    flips as the window moves by one week has not been measured, whatever any
    single row of it says.
    """
    # Annotated: the row dicts hold both floats and a per-season mapping, and
    # without this the inferred value type is a union that no comparison checks.
    rows: list[dict[str, Any]] = []
    for recent, prior in windows:
        out = role_change_backtest(seasons, recent, prior, weeks_ahead, top_k,
                                   blocks, min_prior_games, progress)
        scored = [s for s in out["seasons"] if "error" not in s]
        rows.append({
            "recent_weeks": int(recent), "prior_weeks": int(prior),
            "effect": round(float(np.mean([s["effect"] for s in scored])), 2)
            if scored else None,
            "worst_block_spread": round(float(max(s["block_spread"] for s in scored)), 2)
            if scored else None,
            "seasons_whose_blocks_agree": sum(1 for s in scored if s["blocks_agree"]),
            "seasons_scored": len(scored),
            "per_season": {s["season"]: s["effect"] for s in scored},
        })
    # float() rather than the raw value: the rows carry a per-season dict too, so
    # the inferred value type is a union and a bare comparison does not check.
    signs = [float(r["effect"]) for r in rows if r["effect"] is not None]
    stable = bool(signs) and (all(v > 0 for v in signs) or all(v < 0 for v in signs))
    return {
        "windows": rows,
        "sign_stable_across_windows": stable,
        "verdict": ("the sign holds across every window tried" if stable else
                    "the sign flips as the window moves, so no window here has "
                    "measured anything"),
    }
