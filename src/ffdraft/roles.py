"""Role features: who actually starts, who backs whom, where the touches are,
and how sure any of it is.

Four ideas from the strategy brief, kept out of `model.py` so each can be
measured before it is allowed to move a `pick_value`. The brief's objective is

    DraftValue = VORP + VONA + Expected Starting Utility + Contingent Upside
                 + Roster Optionality - Bye Cost - Role Risk - Injury Risk
                 - Replaceability

and `model.recommend` already prices the first two: `draft_score` is value over
a 16-team replacement level, `marginal_value` is value over what should still be
there at the next pick. This module supplies the next two and scores the fifth.

  start probability   "With no FLEX, RB3 does not compete with WR3 for a starting
                      job. An RB3 only starts when one of your two RBs has a bye,
                      gets injured, loses his job, or has a horrific matchup."
                      Two of those four are modelled here: the bye is known and
                      the injury risk is already on the board. A losing-the-job
                      or benched-for-matchup event is not, so this is a floor on
                      start probability, not an estimate of it.
  handcuff value      "EV_handcuff = P(role change) x delta value after role
                      change + standalone value", plus the brief's separate point
                      that the two numbers must not be collapsed into "he is a
                      good handcuff". Held separately here, and the contingency
                      counts double when you hold the starter, because then it
                      also covers a slot the roster is depending on.
  opportunity shares  "Raw receiving yards can lie spectacularly." Volume named
                      and decomposed: target share, carry share, red zone share,
                      snap share, each of his own team's total that season.
  role entropy        "Low entropy: WR plays 95% snaps, 90% routes, 25% targets.
                      High entropy: could lead this three-man committee." Scored
                      from how far ESPN's projection sits from the model's and
                      how much the player's share of his team's snaps moved week
                      to week, and split the way the brief splits it: uncertainty
                      because upside has not resolved is not the same thing as
                      uncertainty because the player barely has a job.

Every weight defaults to 0: `pick_value_multiplier` returns all ones and
`pick_value_bonus` all zeros, so nothing here changes a recommendation until an
evidence run says otherwise. `weight_backtest` is that run — the same paired
Monte Carlo `adp.bye_backtest` uses — and the numbers behind any weight, moved
or not, belong in CHANGELOG.md.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import features, sources
from .config import CURRENT_SEASON, FANTASY_POSITIONS, LeagueSettings

# Fantasy regular season: the weeks a lineup is actually set for.
FANTASY_WEEKS = 14
# Smallest start probability the model will claim. `start_probability` counts
# only the two paths it can see -- a man ahead injured, or on his bye -- and its
# own docstring calls the result a floor rather than an estimate. A lineup slot
# also opens through a trade, a cut, a benching or a mid-season role loss, none
# of which are modelled, so exactly 0 asserts a certainty this cannot have.
# Policy, not fitted.
#
# This is NOT a fix for the ordering problem below. It is a fix for the claim.
START_PROB_FLOOR = 0.05
# `model.project` maps injury risk to expected games out of a 17-game season;
# the same mapping is the per-week availability here, so the two cannot drift.
SEASON_GAMES = 17

# Weights, both 0 until an evidence run earns otherwise (CHANGELOG.md).
START_PROB_WEIGHT = 0.0
HANDCUFF_WEIGHT = 0.0
# Holding the starter doubles the contingency: the backup then covers a slot the
# roster is already depending on, not merely a chance at points.
HANDCUFF_HELD_BONUS = 1.0

OPPORTUNITY_COLUMNS = ("target_share", "carry_share", "redzone_share", "snap_share")
ENTROPY_COLUMNS = ("role_entropy", "proj_disagreement", "role_churn", "entropy_kind",
                   "entropy_basis")
# What `role_entropy` was actually computed from, per row. The two halves do not
# have the same evidential standing — churn is monotonic against real projection
# error in two seasons across 700 players, disagreement has no test of its own
# here — so a blended number must say which halves went into it rather than
# letting the untested one borrow the tested one's credibility.
ENTROPY_BASIS_BOTH = "disagreement+churn"
ENTROPY_BASIS_DISAGREEMENT = "disagreement only"
ENTROPY_BASIS_CHURN = "churn only"
HANDCUFF_COLUMNS = ("starter", "depth_rank", "starter_injury_risk",
                    "starter_games_missed", "standalone_points", "contingent_points",
                    "ev_handcuff")

# Policy scales, not fitted, and picked to mean something rather than to land
# the median anywhere: full disagreement is ESPN projecting double the model or
# half of it (ln 2), and full churn is a snap share whose week-to-week standard
# deviation equals its own mean. On the live 2026 board those put the median
# entropy at 0.47 rather than the 0.79 a tighter scale produced, which is the
# difference between a score that separates players and one that saturates.
ENTROPY_DISAGREEMENT_SCALE = 0.6931471805599453  # ln 2
ENTROPY_CHURN_SCALE = 1.0
# Below this many games a snap-share series says nothing about stability.
MIN_CHURN_GAMES = 6
# Below this the two projections agree closely enough that neither direction is
# worth naming: a quarter apart either way, ln 1.25. At a tenth apart the label
# fired on ordinary disagreement — Jakobi Meyers at ESPN 182 against a model 205
# read "role in doubt", which is not what an 11% gap means.
ENTROPY_KIND_FLOOR = 0.22314355131420976  # ln 1.25


# ---------------------------------------------------------------- start probability


def weekly_availability(exp_games: float) -> float:
    """Chance a player is available in a given week, from his expected games."""
    if not np.isfinite(exp_games):
        return 1.0
    return float(np.clip(exp_games / SEASON_GAMES, 0.0, 1.0))


def _fewer_than(available: list[float], slots: int) -> float:
    """P(fewer than `slots` of these independent players are available).

    Exact Poisson-binomial by dynamic programming over the count. The lists are
    a handful of roster-mates long, so exactness costs nothing.
    """
    if slots <= 0:
        return 0.0
    if not available:
        return 1.0
    # dist[k] = P(exactly k available so far), truncated at `slots`: once the
    # count reaches the number of slots, the player is out whatever follows.
    dist = [1.0] + [0.0] * slots
    for p in available:
        nxt = [0.0] * (slots + 1)
        for k, mass in enumerate(dist):
            if mass == 0.0:
                continue
            nxt[k] += mass * (1 - p)
            if k + 1 <= slots:
                nxt[k + 1] += mass * p
        dist = nxt
    return float(sum(dist[:slots]))


def start_probability(ahead: list[tuple[float, float | None]], slots: int,
                      weeks: int = FANTASY_WEEKS) -> float:
    """Per-week chance a player is in the starting lineup.

    `ahead` is one (expected games, bye week) pair per roster-mate ranked above
    him at his position; `slots` is how many of them the lineup starts. He is in
    the lineup in a week when fewer than `slots` of them are available that week,
    and a man on his bye is not available. Averaged over the fantasy weeks.

    With no FLEX slot this is exact. With a FLEX slot it is a lower bound: a
    player can also start through the flex, which this does not count.

    This is whether the lineup has room for him, not whether he plays. His own
    bye and his own injury risk are already in `proj_points` through
    `exp_games`, and `recommend`'s bye term prices the stack; counting them
    again here would charge him twice.
    """
    if slots <= 0:
        return 0.0
    if len(ahead) < slots:
        return 1.0
    weekly = []
    for week in range(1, weeks + 1):
        avail = [0.0 if (bye is not None and np.isfinite(bye) and int(bye) == week)
                 else weekly_availability(games) for games, bye in ahead]
        weekly.append(_fewer_than(avail, slots))
    return float(np.mean(weekly))


def _slots_for(league: LeagueSettings, position: str) -> int:
    """Starting slots the position fills on its own, plus the superflex opening
    for a quarterback. FLEX is deliberately excluded — see `start_probability`."""
    slots = league.starters.get(position, 0)
    if position == "QB":
        slots += getattr(league, "superflex", 0) or 0
    return slots


def position_slots(league: LeagueSettings) -> dict[str, int]:
    """Starting slots per fantasy position, as `start_probability` counts them."""
    return {pos: _slots_for(league, pos) for pos in FANTASY_POSITIONS}


def _held_by_position(mine: pd.DataFrame | None) -> dict[str, list[tuple[float, float, float | None]]]:
    """(projected points, expected games, bye week) per held player, by position,
    best first."""
    held: dict[str, list[tuple[float, float, float | None]]] = {}
    if mine is None or mine.empty:
        return held
    for _, r in mine.iterrows():
        bye = r.get("bye_week")
        held.setdefault(str(r.get("position")), []).append((
            float(r.get("proj_points") or 0.0),
            float(r.get("exp_games") or SEASON_GAMES),
            float(bye) if bye is not None and pd.notna(bye) else None))
    for rows in held.values():
        rows.sort(reverse=True)
    return held


def start_probabilities(avail: pd.DataFrame, league: LeagueSettings,
                        mine: pd.DataFrame | None) -> pd.Series:
    """Per-week start probability for every candidate, given the roster I hold.

    The players "ahead" of a candidate are the ones I already hold at his
    position who project for more points than he does: drafting him does not
    demote anybody.
    """
    out = pd.Series(1.0, index=avail.index)
    if avail.empty:
        return out
    held = _held_by_position(mine)
    for idx, row in avail.iterrows():
        pos = str(row.get("position"))
        points = float(row.get("proj_points") or 0.0)
        ahead = [(games, bye) for pts, games, bye in held.get(pos, []) if pts > points]
        out.at[idx] = start_probability(ahead, _slots_for(league, pos))
    # Never exactly 0: the two paths this models are not the only ways a lineup
    # slot opens, and the docstring above already calls the result a floor.
    return out.clip(lower=START_PROB_FLOOR)


def bench_values(avail: pd.DataFrame, league: LeagueSettings,
                 mine: pd.DataFrame | None) -> pd.DataFrame:
    """`p_start` and `bench_value` (p_start x projected points) per candidate.

    The brief's "Expected Starting Utility": what the player is worth to *this*
    roster rather than to an average one. A starter scores his whole projection;
    a fourth running back scores the weeks the two ahead of him are both out.
    """
    out = pd.DataFrame(index=avail.index)
    for col in ("name", "position"):
        if col in avail.columns:
            out[col] = avail[col]
    out["p_start"] = start_probabilities(avail, league, mine)
    out["bench_value"] = out["p_start"] * pd.to_numeric(
        avail.get("proj_points"), errors="coerce").fillna(0.0)
    return out


# ---------------------------------------------------------------- handcuffs


def handcuff_table(board: pd.DataFrame) -> pd.DataFrame:
    """The two numbers the brief insists on keeping apart, per player.

    `standalone_points` is what he produces while everyone is healthy — his own
    projection. `contingent_points` is what he becomes if the man ahead of him
    disappears: the per-game upgrade he inherits, times the games that man is
    expected to miss. `ev_handcuff` is their sum, the brief's
    `P(role change) x delta value + standalone value`.

    Only the direct backup — `depth_rank` 2 at his own (NFL team, position), by
    the model's own projection — carries contingent value. Third and fourth on a
    depth chart do not inherit a vacated role in any way this can defend, and
    giving them the same number is how a handcuff term ends up rewarding
    whoever is worst.

    The starter has no handcuff value of his own: he is the one being insured
    against. His fragility is `injury_risk`, which is already built from
    availability history, injury-report frequency and workload burden. This is a
    first-order model of a workload transferring, not a claim about how a coach
    would split it.

    Computed over a whole board, not an available pool: once the starter is
    drafted, a pool would promote his backup to "starter" and silently zero the
    very value the term exists to price.
    """
    empty = pd.DataFrame(
        {"starter": pd.Series(dtype="object"),
         "depth_rank": pd.Series(dtype="float64"),
         "starter_injury_risk": pd.Series(dtype="float64"),
         "starter_games_missed": pd.Series(dtype="float64"),
         "standalone_points": pd.Series(dtype="float64"),
         "contingent_points": pd.Series(dtype="float64"),
         "ev_handcuff": pd.Series(dtype="float64")}, index=board.index)
    if board.empty or not {"team", "position", "proj_points"} <= set(board.columns):
        return empty
    b = board.copy()
    b["_pts"] = pd.to_numeric(b["proj_points"], errors="coerce").fillna(0.0)
    b["_ppg"] = pd.to_numeric(b.get("adj_ppg"), errors="coerce").fillna(0.0)
    b["_games"] = pd.to_numeric(b.get("exp_games"), errors="coerce").fillna(float(SEASON_GAMES))
    b["_risk"] = pd.to_numeric(b.get("injury_risk"), errors="coerce").fillna(0.0)

    rows: dict = {}
    for _key, chunk in b.groupby(["team", "position"], dropna=True, observed=True):
        chunk = chunk.sort_values("_pts", ascending=False)
        top = chunk.iloc[0]
        missed = max(0.0, SEASON_GAMES - float(top["_games"]))
        for rank, (idx, r) in enumerate(chunk.iterrows(), start=1):
            standalone = float(r["_pts"])
            contingent = (missed * max(0.0, float(top["_ppg"]) - float(r["_ppg"]))
                          if rank == 2 else 0.0)
            rows[idx] = {
                "starter": str(top.get("name")) if rank > 1 else None,
                "depth_rank": rank,
                "starter_injury_risk": round(float(top["_risk"]), 3) if rank > 1 else np.nan,
                "starter_games_missed": round(missed, 2) if rank > 1 else np.nan,
                "standalone_points": round(standalone, 2),
                "contingent_points": round(contingent, 2),
                "ev_handcuff": round(standalone + contingent, 2),
            }
    return pd.DataFrame.from_dict(rows, orient="index").reindex(board.index)


def attach_handcuffs(board: pd.DataFrame) -> pd.DataFrame:
    """Put the handcuff columns on a board, computed over the whole board."""
    out = board.drop(columns=[c for c in HANDCUFF_COLUMNS if c in board.columns])
    return out.join(handcuff_table(board))


# ---------------------------------------------------------------- the model hook


def pick_value_multiplier(avail: pd.DataFrame, league: LeagueSettings,
                          mine: pd.DataFrame | None = None,
                          start_prob_weight: float = START_PROB_WEIGHT) -> pd.Series:
    """What `model.recommend` scales a pick_value by: the start-probability term.

    Reported, and used to derive the additive term in `pick_value_bonus`, but
    never applied to `pick_value` by multiplication — see that function for why.
    At weight 0 it is all ones.
    """
    ones = pd.Series(1.0, index=avail.index)
    if avail.empty or not start_prob_weight:
        return ones
    p_start = start_probabilities(avail, league, mine)
    return ones * (1 - start_prob_weight + start_prob_weight * p_start)


def start_prob_adjustment(avail: pd.DataFrame, league: LeagueSettings,
                          mine: pd.DataFrame | None = None,
                          start_prob_weight: float = START_PROB_WEIGHT) -> pd.Series:
    """What the start-probability term adds to a pick_value. Never a multiplier.

    Scaling `pick_value` itself does not work, and no choice of factor fixes it.
    `pick_value` is not a ratio scale — its zero means "exactly as good as
    waiting", not "worthless" — and 566 of 577 available rows at a live
    mid-draft pick sit below that zero. Multiplying a positive `pick_value`
    toward zero therefore lands it *above* the entire negative field: a
    candidate this term says can hardly ever start came out around rank 12 of
    577. Dividing never touched that half either, which is why the trap survived
    three successive fixes to the sign rule on the negative half. Found by marge.

    So the term is applied where it is actually true. A player who is in the
    lineup a fraction `m` of the time is worth `m` of his own projection, and
    `draft_score` is built from that projection, so the honest statement is
    "scale his draft_score by m", *before* the fallback subtraction rather than
    after it. Writing out what `recommend` computes:

        pick_value      = (0.80 * (ds - fallback) + 0.20 * ds) * need
        pick_value(m*ds) = (0.80 * (m*ds - fallback) + 0.20 * m*ds) * need

    and the difference between them is exactly `(m - 1) * ds * need`. So the
    points-side scaling is available as an addition, with no restructuring of
    `recommend` and no multiplier anywhere. At m = 1 it is 0 to the bit.

    This also gives exclusion for free and without a sentinel: a candidate who
    can never start loses his whole `draft_score`, which for a good player is a
    large negative number, so he drops below the field instead of landing on
    zero at rank 12. `START_PROB_FLOOR` is then a statement about what the model
    can claim, not a workaround for arithmetic.

    Only value *above* replacement is scaled. `draft_score` is value over
    replacement, not points, so `m * draft_score` is the right statement only
    while it is positive: a player already below replacement is not made better
    by playing less, and scaling his negative score toward zero would lift him.
    Left unclipped it did exactly that, and a receiver whose own value had not
    changed at all fell from rank 1 to rank 13 because players below replacement
    rose past him. Below replacement the term is silent — his value already says
    he is not a starter.
    """
    zero = pd.Series(0.0, index=avail.index)
    if avail.empty or not start_prob_weight:
        return zero
    mult = pick_value_multiplier(avail, league, mine, start_prob_weight)
    ds = pd.to_numeric(avail.get("draft_score"), errors="coerce").fillna(0.0).clip(lower=0.0)
    need = pd.to_numeric(avail.get("need_mult"), errors="coerce").fillna(1.0) \
        if "need_mult" in avail.columns else pd.Series(1.0, index=avail.index)
    return (mult - 1.0) * ds * need


def pick_value_bonus(avail: pd.DataFrame, league: LeagueSettings,
                     mine: pd.DataFrame | None = None,
                     handcuff_weight: float = HANDCUFF_WEIGHT) -> pd.Series:
    """What the contingent-upside term adds to a pick_value.

    Added, not multiplied. A handcuff's contingent points are points, in the same
    units as the projection `draft_score` is built from; scaling by them instead
    would make a deep bench player's already negative value more negative the
    better his handcuff case is, which is the opposite of the intent.

    Gated by the chance he would be in *my* lineup when the promotion comes.
    Contingent points a roster can never start are not points: without the gate
    the term is at its largest for backup quarterbacks, whose starter has the
    highest per-game output on the board, and it arrives after `need_mult` has
    already discounted them, so it walks straight past the rule that stops the
    model rostering a second quarterback.

    The gate does not apply when the starter is mine. His absence is the event
    that pays the contingency AND the event that opens the lineup slot — the
    same event, not two — so multiplying by the slot's probability squares a
    probability `contingent_points` has already applied once. Left in, it made
    holding the starter *lower* his handcuff's value than not holding him: Brian
    Robinson behind a held Bijan Robinson came out at 11.7 against 17.9, which
    is the exact reverse of the brief's "higher when you hold that starter".

    Doubled when the starter is on my roster, on top of that: the contingency
    then also covers a slot the roster is already depending on, which is the
    brief's roster optionality rather than a second helping of the same points.
    """
    zero = pd.Series(0.0, index=avail.index)
    if avail.empty or not handcuff_weight:
        return zero
    hc = avail if "contingent_points" in avail.columns else handcuff_table(avail)
    contingent = pd.to_numeric(hc["contingent_points"], errors="coerce").fillna(0.0)
    held = set(mine["name"].astype(str)) if mine is not None and not mine.empty \
        and "name" in mine.columns else set()
    holds = (hc["starter"].isin(held) if "starter" in hc.columns
             else pd.Series(False, index=avail.index)).fillna(False)
    gate = start_probabilities(avail, league, mine).where(~holds, 1.0)
    return (handcuff_weight * contingent * gate
            * (1 + HANDCUFF_HELD_BONUS * holds.astype(float)))


# ---------------------------------------------------------------- opportunity shares


def opportunity_shares(sc, seasons: list[int] | None = None,
                       te_bonus: float = 0.0) -> pd.DataFrame:
    """Recency-weighted target, carry, red zone and snap share per player.

    Each share is of his own team's total in that season, so a player who moved
    teams is measured against whoever he was playing for at the time. Weighted
    across seasons exactly the way `model.build_player_table` weights production,
    so the columns line up with the projection they sit beside.
    """
    seasons = list(seasons or list(range(CURRENT_SEASON - 5, CURRENT_SEASON)))
    weekly = sources.weekly_stats(seasons)
    weekly = weekly[weekly["season_type"] == "REG"].copy()
    for col in ("targets", "carries"):
        weekly[col] = pd.to_numeric(weekly.get(col), errors="coerce").fillna(0.0)
    team = weekly.groupby(["season", "recent_team"], observed=True).agg(
        team_targets=("targets", "sum"), team_carries=("carries", "sum")).reset_index()
    by_team = weekly.groupby(["season", "player_id", "recent_team"], observed=True).agg(
        targets=("targets", "sum"), carries=("carries", "sum"),
        weeks=("week", "nunique")).reset_index()
    by_team = by_team.merge(team, on=["season", "recent_team"], how="left")
    # A player who changed teams mid-season has a row per team. Collapse to one
    # row per season against a denominator weighted by the weeks he spent at each
    # club, so a trade neither double-counts the season nor throws away the half
    # of his production that happened at the other one.
    weeks_total = by_team.groupby(["season", "player_id"], observed=True)["weeks"].transform("sum")
    frac = by_team["weeks"] / weeks_total.replace(0, np.nan)
    by_team["_eff_targets"] = by_team["team_targets"] * frac
    by_team["_eff_carries"] = by_team["team_carries"] * frac
    player = by_team.groupby(["season", "player_id"], observed=True).agg(
        targets=("targets", "sum"), carries=("carries", "sum"),
        team_targets=("_eff_targets", "sum"), team_carries=("_eff_carries", "sum")).reset_index()
    player["target_share"] = player["targets"] / player["team_targets"].replace(0, np.nan)
    player["carry_share"] = player["carries"] / player["team_carries"].replace(0, np.nan)

    player = player.merge(_redzone_shares(seasons), on=["season", "player_id"], how="left")
    prof = features.player_season_profiles(sc, te_bonus, seasons=seasons)
    player = player.merge(prof[["season", "player_id", "snap_share"]],
                          on=["season", "player_id"], how="left")

    wmap = features._season_weights(sorted(player["season"].unique()))
    player["_w"] = player["season"].map(wmap).fillna(0.0)
    # A NaN key on both sides of a merge is a row explosion, and the board now
    # carries rows with no player_id at all (kickers and defenses priced from
    # ESPN alone), so the key has to be clean before it leaves this function.
    player = player[player["player_id"].notna()]
    out = pd.DataFrame({"player_id": sorted(player["player_id"].unique())})
    for col in OPPORTUNITY_COLUMNS:
        p = player[["player_id", "_w", col]].copy()
        p["_wv"] = p[col].fillna(0.0) * p["_w"]
        # A season the player has no number for contributes no weight either,
        # rather than being counted as a zero share.
        p["_wd"] = p["_w"].where(p[col].notna(), 0.0)
        agg = p.groupby("player_id").agg(num=("_wv", "sum"), den=("_wd", "sum"))
        out = out.merge((agg["num"] / agg["den"].replace(0, np.nan)).rename(col).reset_index(),
                        on="player_id", how="left")
    return out


def _redzone_shares(seasons: list[int]) -> pd.DataFrame:
    """A player's share of his team's red zone touches, per season."""
    pbp = sources.play_by_play(seasons=seasons)
    rz = pbp[pbp["yardline_100"].notna() & (pbp["yardline_100"] <= 20)]
    rz = rz[rz["posteam"].notna() & rz["play_type"].isin(["pass", "run"])]
    team = rz.groupby(["season", "posteam"], observed=True).size().rename(
        "team_rz_touches").reset_index()
    player = features.player_redzone_role(pbp)[["season", "player_id", "rz_touches"]]
    # Which team he took those touches for is in the play rows themselves, so
    # read it back rather than assuming his season team.
    runs = rz[(rz["play_type"] == "run") & rz["rusher_player_id"].notna()][
        ["season", "rusher_player_id", "posteam"]].rename(
        columns={"rusher_player_id": "player_id"})
    passes = rz[(rz["play_type"] == "pass") & rz["receiver_player_id"].notna()][
        ["season", "receiver_player_id", "posteam"]].rename(
        columns={"receiver_player_id": "player_id"})
    both = pd.concat([runs, passes], ignore_index=True)
    counts = both.groupby(["season", "player_id", "posteam"], observed=True).size().rename(
        "n").reset_index()
    where = counts.sort_values("n").drop_duplicates(["season", "player_id"], keep="last")
    player = player.merge(where[["season", "player_id", "posteam"]],
                          on=["season", "player_id"], how="left")
    player = player.merge(team, on=["season", "posteam"], how="left")
    player["redzone_share"] = player["rz_touches"] / player["team_rz_touches"].replace(0, np.nan)
    return player[["season", "player_id", "redzone_share"]]


def attach_opportunity(board: pd.DataFrame, sc, seasons: list[int] | None = None,
                       te_bonus: float = 0.0) -> pd.DataFrame:
    """Put the four named shares on a board.

    `target_share` and `snap_share` already exist on the board as recency-weighted
    season means; these replace them with the share-of-team form, so all four
    columns mean the same kind of thing and can be read against each other.
    """
    if "player_id" not in board.columns:
        return board
    shares = opportunity_shares(sc, seasons, te_bonus).set_index("player_id")
    out = board.drop(columns=[c for c in OPPORTUNITY_COLUMNS if c in board.columns])
    # Mapped rather than merged: a merge would hand back a fresh RangeIndex, and
    # the entropy and handcuff columns that follow are joined on the index.
    for col in OPPORTUNITY_COLUMNS:
        out[col] = out["player_id"].map(shares[col])
    return out


# ---------------------------------------------------------------- role entropy


def snap_share_churn(seasons: list[int] | None = None) -> pd.DataFrame:
    """Week-to-week instability of a player's share of his team's offensive snaps
    in his most recent season: the coefficient of variation of `offense_pct`.

    The brief's low-entropy player "plays 95% snaps" every week and scores near
    zero. The high-entropy one is in a committee whose split moves, and scores
    high. Under `MIN_CHURN_GAMES` appearances the series says nothing, and the
    player is left out rather than given a number from three games.
    """
    seasons = list(seasons or list(range(CURRENT_SEASON - 5, CURRENT_SEASON)))
    snaps = sources.snap_counts(seasons)
    snaps = snaps[(snaps["game_type"] == "REG") & snaps["offense_pct"].notna()]
    snaps = snaps[snaps["offense_pct"] > 0]
    if snaps.empty:
        return pd.DataFrame({"player": [], "role_churn": []})
    latest = snaps.groupby("player", observed=True)["season"].transform("max")
    recent = snaps[snaps["season"] == latest]
    agg = recent.groupby("player", observed=True)["offense_pct"].agg(["size", "mean", "std"])
    agg = agg[agg["size"] >= MIN_CHURN_GAMES]
    churn = (agg["std"] / agg["mean"].replace(0, np.nan)).rename("role_churn")
    return churn.reset_index()


def role_entropy(board: pd.DataFrame, churn: pd.DataFrame | None = None) -> pd.DataFrame:
    """An uncertainty score in [0, 1] per player, its two parts, and its sign.

    `proj_disagreement` is |ln(ESPN projection / model projection)|: zero when
    the two agree, and symmetric, so ESPN at half the model and at twice it score
    alike. `role_churn` is the snap-share instability above. Each is divided by
    its own policy scale, clipped at 1, and the two are averaged; a player with
    neither input scores NaN rather than a confident zero.

    `entropy_kind` is the brief's split. ESPN projecting *above* a model built
    from past production is upside that has not resolved yet; ESPN projecting
    *below* it is a role that has shrunk — "uncertain because the player barely
    has a job". The two are not the same bet and should not read the same.
    """
    idx = board.index
    espn = pd.to_numeric(board["espn_proj"], errors="coerce") if "espn_proj" in board.columns \
        else pd.Series(np.nan, index=idx)
    ours = pd.to_numeric(board["proj_points"], errors="coerce") \
        if "proj_points" in board.columns else pd.Series(np.nan, index=idx)
    # Two projections that are bit-identical are one number, not two that agree.
    # A kicker or a team defense is priced from ESPN's own projection, so its
    # ratio is exactly 1 by construction; scored as agreement it would read as
    # the most certain role on the board, which is the opposite of what an
    # untested projection deserves.
    ratio = (espn / ours.replace(0, np.nan)).where((espn > 0) & (ours > 0) & (espn != ours))
    log_ratio = np.log(ratio)
    disagreement = log_ratio.abs()

    if churn is None:
        churn = snap_share_churn()
    lookup = dict(zip(churn["player"], churn["role_churn"])) if not churn.empty else {}
    role_churn = pd.to_numeric(
        board["name"].map(lookup) if "name" in board.columns else pd.Series(np.nan, index=idx),
        errors="coerce")

    parts = pd.concat([(disagreement / ENTROPY_DISAGREEMENT_SCALE).clip(upper=1.0),
                       (role_churn / ENTROPY_CHURN_SCALE).clip(upper=1.0)], axis=1)
    entropy = parts.mean(axis=1, skipna=True).clip(0.0, 1.0)

    kind = pd.Series("", index=idx, dtype="object")
    kind = kind.where(~(log_ratio > ENTROPY_KIND_FLOOR), "unresolved upside")
    kind = kind.where(~(log_ratio < -ENTROPY_KIND_FLOOR), "role in doubt")
    kind = kind.where(entropy.notna(), "")

    # Which halves the score actually rests on. A row scored from disagreement
    # alone is not the same claim as one scored from churn, and only the churn
    # half has been tested against real projection error.
    basis = pd.Series("", index=idx, dtype="object")
    basis = basis.where(~(disagreement.notna() & role_churn.notna()), ENTROPY_BASIS_BOTH)
    basis = basis.where(~(disagreement.notna() & role_churn.isna()),
                        ENTROPY_BASIS_DISAGREEMENT)
    basis = basis.where(~(disagreement.isna() & role_churn.notna()), ENTROPY_BASIS_CHURN)
    return pd.DataFrame({"role_entropy": entropy, "proj_disagreement": disagreement,
                         "role_churn": role_churn, "entropy_kind": kind,
                         "entropy_basis": basis}, index=idx)


def attach_role_entropy(board: pd.DataFrame, churn: pd.DataFrame | None = None) -> pd.DataFrame:
    out = board.drop(columns=[c for c in ENTROPY_COLUMNS if c in board.columns])
    return out.join(role_entropy(board, churn))


def entropy_error_backtest(board: pd.DataFrame, season: int, league,
                           bins: int = 3, min_points: float = 40.0) -> dict:
    """Does role entropy actually mark the projections that miss?

    Scores a leak-free board for `season` against what really happened: per
    player the absolute error between the projection and his real fantasy points,
    as a fraction of the projection, grouped into entropy bins. If entropy means
    anything, the top bin misses by more than the bottom one.
    """
    real = _actual_points(season, league)
    b = board.copy()
    b["_proj"] = pd.to_numeric(b["proj_points"], errors="coerce")
    b["_actual"] = b["name"].map(real)
    b = b[b["_actual"].notna() & (b["_proj"] >= min_points) & b["role_entropy"].notna()]
    if b.empty:
        return {"season": season, "n": 0, "bins": [], "spread": None}
    b["_err"] = (b["_actual"] - b["_proj"]).abs() / b["_proj"]
    labels = pd.qcut(b["role_entropy"], bins, labels=False, duplicates="drop")
    rows = []
    for label, chunk in b.groupby(labels, observed=True):
        rows.append({"bin": int(label), "n": int(len(chunk)),
                     "entropy_mean": round(float(chunk["role_entropy"].mean()), 3),
                     "abs_pct_error": round(float(chunk["_err"].mean()), 3)})
    rows.sort(key=lambda r: r["bin"])
    spread = round(rows[-1]["abs_pct_error"] - rows[0]["abs_pct_error"], 3) if len(rows) > 1 \
        else None
    return {"season": season, "n": int(len(b)), "bins": rows, "spread": spread,
            "interpretation": ("spread is the top entropy bin's mean absolute percentage "
                               "error minus the bottom bin's; positive means entropy marks "
                               "the projections that miss")}


def weight_backtest(league, weights, seasons: list[int], role_weights: dict[str, float],
                    n_trials: int = 12, top_n: int = 5, seed: int = 0,
                    blocks: int | None = None, progress=None) -> dict:
    """Do the start-probability and handcuff terms win weekly lineup points?

    The same paired Monte Carlo `adp.bye_backtest` uses, and for the same reason:
    a season total cannot see a bench, so the drafts are scored on real box
    scores as the best legal lineup each regular-season week. For each season and
    seed, one mock draft with the weights off and one with them on, identical
    bots and identical noise, so the pair differs in exactly the weights.

    Run in `blocks` disjoint blocks (`adp.DEFAULT_BLOCKS`), and every block's own
    improvement is reported beside `block_spread` and `blocks_agree`. An
    `improvement` > 0 whose blocks disagree in sign is a measurement of the
    harness rather than of the term, and the weight stays 0. The numbers go in
    CHANGELOG.md either way.
    """
    from . import adp as adp_mod
    from . import board as bd
    from . import model

    def say(msg: str) -> None:
        if progress is not None:
            progress(msg)

    blocks = adp_mod.DEFAULT_BLOCKS if blocks is None else blocks
    sc_label = "ppr" if float(league.scoring.rec) >= 0.9 else \
               "half_ppr" if float(league.scoring.rec) >= 0.35 else "standard"
    per_season = []
    for season in seasons:
        say(f"{season}: building leak-free board")
        tbl = model.build_player_table(league, weights, season=season)
        proj = model.project(tbl, league, weights)
        adp = bd.load_adp(season=season, superflex=bool(getattr(league, "superflex", 0)))
        board = bd.convert_adp_format(bd.attach_adp(proj, adp), sc_label)
        board["drafted"] = False
        board["bye_week"] = board["team"].map(features.team_bye_weeks(season))
        board = attach_handcuffs(board)
        try:
            sources.weekly_stats([season])
        except RuntimeError:
            per_season.append({"season": season, "error": "no box scores for this season"})
            continue

        def pair(trial_seed: int, board=board) -> tuple[list[str], list[str]]:
            a = [n for _, n in adp_mod._draft_trial(
                board, league, np.random.default_rng(trial_seed), top_n)]
            b = [n for _, n in adp_mod._draft_trial(
                board, league, np.random.default_rng(trial_seed), top_n,
                role_weights=role_weights)]
            return a, b

        def score(names: list[str], season=season) -> dict:
            return adp_mod.weekly_lineup_points(names, season, league)

        summary = adp_mod._paired_blocks(pair, score, n_trials, blocks, seed, say, str(season))
        per_season.append({"season": season, **summary})
        say(f"{season} done: improvement {summary['improvement']:+.1f} weekly pts across "
            f"{blocks} blocks {summary['block_improvements']}, spread "
            f"{summary['block_spread']}, blocks agree {summary['blocks_agree']}")

    valid: list[dict] = [s for s in per_season if "error" not in s]
    gains: list[float] = [float(s["improvement"]) for s in valid
                          if s["improvement"] is not None]
    spreads: list[float] = [float(s["block_spread"]) for s in valid
                            if s["block_spread"] is not None]
    return {
        "role_weights": dict(role_weights), "n_trials": n_trials, "n_blocks": blocks,
        "seasons": per_season,
        "overall_improvement": round(float(np.mean(gains)), 1) if gains else None,
        "blocks_agree": bool(valid) and all(s["blocks_agree"] for s in valid),
        "worst_block_spread": round(max(spreads), 1) if spreads else None,
        "players_swapped": int(sum(int(s["players_swapped"]) for s in valid)),
        "interpretation": ("improvement is mean weekly-lineup points gained per season by "
                           "turning the weights on over identical drafts without them. "
                           "Read it against block_spread, the distance between two "
                           "disjoint seed blocks of the same configuration: when "
                           "blocks_agree is false the improvement is inside the harness's "
                           "own noise and supports nothing. When it is true, read "
                           "blocks_agree_p_null first — at two blocks agreement is one "
                           "coin flip and is not a pass. trials_improved_of_changed is a "
                           "win rate over the trials the weights fired on, a different "
                           "denominator from improvement's; the two are not views of one "
                           "quantity. players_swapped is how many picks actually changed, "
                           "which is what makes a zero readable."),
    }


def _actual_points(season: int, league) -> dict[str, float]:
    """Real regular-season fantasy points that season, by player name."""
    w = sources.weekly_stats([season])
    w = w[w["season_type"] == "REG"].copy()
    w["fp"] = features.fantasy_points(w, league.scoring,
                                      getattr(league, "te_premium_bonus", 0.0))
    total = w.groupby("player_display_name", observed=True)["fp"].sum()
    return {str(k): float(v) for k, v in total.items()}
