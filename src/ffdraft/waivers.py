"""Role change: snap and target share moving week over week, and its backtest.

`role_change` measures it from the nflverse weekly pipeline;
`role_change_backtest` and `window_sweep` measure whether it predicts the next
four weeks (`just rolechange`). The answer is no: `ROLE_CHANGE_EVIDENCE`.
`POOL_FILTER` is ESPN's player-pool filter, shared by `pool` and `rosters`.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .names import normalize as norm_name

# The same filter `espn_dump` uses, so the pool here is the pool that dump
# captured and the field shapes in docs/data-sources.md describe both.
POOL_FILTER = {"players": {"filterStatus": {"value": ["FREEAGENT", "WAIVERS", "ONTEAM"]},
                           "limit": 2000,
                           "sortDraftRanks": {"sortPriority": 100, "sortAsc": True,
                                              "value": "PPR"}}}


# Carried verbatim in every `role_change` row: a score whose backtest went
# against it says so where it is read.
ROLE_CHANGE_EVIDENCE = (
    "MEASURED AND NEGATIVE: over 2022-2025 the top 10 by role change scored 6.4 to "
    "10.1 fewer PPR points over the following four weeks than the top 10 by recent "
    "points per game, from the same undrafted pool. Negative in every season, every "
    "block, and all eight (recent, prior) windows tried, and still negative in all "
    "16 blocks when a full prior window is required. The DIRECTION is the result; "
    "the block spread is not a noise estimate here, because neighbouring weeks "
    "share three of their four outcome weeks (marge). Ranking claims by this score "
    "is worse than ranking them by what the player just scored")

# Weeks either side of the split when measuring a role change. Two recent weeks
# against the three before them: one week is a game script, and a window longer
# than three weeks stops being "this changed" and becomes "this is who he is".
RECENT_WEEKS = 2
PRIOR_WEEKS = 3

# Below this many recent appearances there is no week-over-week anything.
MIN_RECENT_GAMES = 1


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

    Measured against this score: ranking the undrafted pool by it picks players
    who score fewer points over the next four weeks than ranking the same pool
    by recent points per game. `ROLE_CHANGE_EVIDENCE` carries the numbers.
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
        # summed over the window per player, not per (player, team): a player
        # traded mid-window is one row (six in 2024 week 10). The denominator is
        # the team's totals in the weeks he played, so a missed game is not a
        # smaller role.
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
        # The distance between blocks of the same configuration. NOT this
        # harness's noise, unlike its namesake in the draft backtests: these
        # blocks are alternating weeks whose outcome windows overlap by three
        # weeks in four, so this is a lower bound on sampling variability and
        # cannot be read as the number `effect` must clear. See
        # `role_change_backtest`.
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

    Blocks are ALTERNATING weeks, not early-season against late. The season's
    halves are not the same process -- a role change in week 5 is news and the
    same change in week 13 is a fact everyone already has -- so an early/late
    split would measure the calendar and report it as harness noise.

    READ `block_spread` HERE AS A LOWER BOUND, NOT AS THIS HARNESS'S NOISE.
    Alternating weeks buys calendar-neutrality at the cost of independence, and
    the cost is large: each cohort's outcome window is w+1..w+4, so week 5's
    outcome (weeks 6-9) and week 6's (weeks 7-10) share three of four weeks.
    Every cohort in one block overlaps its neighbours in the other in
    three-quarters of the data being averaged, so the two blocks are two
    overlapping views of one sample rather than two samples, and they agree far
    more than independent halves would. The draft backtests block on disjoint
    seeds over independent trials, where the spread does carry that contract;
    this one does not, and the numbers must not be read across as though it did.
    Found by marge on review, after the first version of this docstring claimed
    the tight spreads were evidence of stability.

    So what this measures is the SIGN, which is robust: negative in every season,
    every block, every window, and in all 16 blocks when a full prior window is
    required. It does not measure how large the effect is relative to noise,
    because the split cannot estimate the noise. A season-pair split would share
    no outcome weeks, but four seasons gives two blocks of two.

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
