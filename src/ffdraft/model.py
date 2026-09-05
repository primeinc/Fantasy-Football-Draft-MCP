"""The draft model.

Pipeline: baseline projection -> environment adjustments -> consistency blend ->
value over replacement -> pick-aware urgency.

The last step is the one that actually wins drafts. Knowing a player is good is easy;
knowing whether he'll still be there when you pick again is what decides who to take now.
"""
from __future__ import annotations

import math
from collections.abc import Mapping

import numpy as np
import pandas as pd

from . import features, sources
from .config import (
    CURRENT_SEASON,
    FANTASY_POSITIONS,
    SPECIAL_POSITIONS,
    LeagueSettings,
    ModelWeights,
)


def touchdown_luck_multiplier(rz_touches: pd.Series, rz_td: pd.Series,
                              baseline_rate: pd.Series, weight: float,
                              min_touches: int = 8) -> pd.Series:
    """How far a player's red zone conversion rate sits from his position's baseline,
    expressed as a bounded multiplier on projected production.

    A player who scored on far more of his red zone touches than his position
    converts on average gets pulled down; one who scored on far fewer gets pulled up.
    Below min_touches a rate is mostly noise, so those players sit neutral (1.0)
    rather than swinging on a two- or three-touch sample.

    weight is the maximum fractional move in either direction, same convention as
    every other environment multiplier in project() -- z-scored, clipped to +/-2.5,
    then scaled by weight so this factor can't dominate the projection on its own.
    """
    touches = rz_touches.fillna(0.0)
    td = rz_td.fillna(0.0)
    qualifies = touches >= min_touches
    expected = touches * baseline_rate.fillna(0.0)
    surplus = td - expected

    # Z-score over the qualifying population only, so the non-qualifying players
    # pinned to neutral below don't drag the scale everyone else is measured on.
    pool = surplus.where(qualifies)
    sd = pool.std(ddof=0)
    if sd and sd > 0:
        z = (surplus - pool.mean()) / sd
    else:
        z = pd.Series(0.0, index=surplus.index)
    # Sign-flipped: a positive surplus (overperformed) should push the multiplier
    # below 1, a negative surplus (underperformed) should push it above 1.
    z = (-z).clip(-2.5, 2.5) / 2.5
    mult = 1 + z * weight
    return mult.where(qualifies, 1.0)


def apply_current_team(tbl: pd.DataFrame, depth_chart: pd.DataFrame | None) -> pd.DataFrame:
    """Override each player's team with the current depth chart, when available.

    `tbl["team"]` (built from weekly box scores) reflects whichever team a player
    last actually played a game for, which can be a full offseason stale by draft
    day -- trades, cuts and free-agent signings don't show up until Week 1 snaps
    get recorded. Depth charts are filed by the teams themselves, so they catch a
    move as soon as it's reported. A player missing from the depth chart (e.g. a
    rookie before training camp, or if this season's chart isn't out yet) just
    keeps his last known team -- this is a correction, not a hard requirement.

    Also flags `off_roster`: True for a player absent from a *populated* depth
    chart, meaning every team has filed one and this player is on none of them --
    released, retired mid-cycle, or otherwise not currently rosterable, as
    opposed to a rookie or a player whose team simply hasn't reported yet (which
    an empty depth_chart already covers by skipping the flag entirely below). A
    player like this can still carry a strong projection from last season's real
    production even though he has no path to touches this season; project()
    discounts him for it the same way it discounts a stale season.

    Must run before the O-line / pace / schedule merges below, which key off
    `team`: otherwise a traded player would get graded on his old team's offense.
    """
    if depth_chart is None or depth_chart.empty or "player_id" not in tbl.columns:
        tbl = tbl.copy()
        tbl["off_roster"] = False
        return tbl
    out = tbl.merge(
        depth_chart[["player_id", "team"]].rename(columns={"team": "_current_team"}),
        on="player_id", how="left",
    )
    has_current = out["_current_team"].notna()
    out.loc[has_current, "team"] = out.loc[has_current, "_current_team"]
    out["off_roster"] = ~has_current
    return out.drop(columns=["_current_team"])


def _season_weighted(profiles: pd.DataFrame, col: str) -> pd.Series:
    wmap = features._season_weights(sorted(profiles["season"].unique()))
    p = profiles.copy()
    p["w"] = p["season"].map(wmap).fillna(0)
    p["wv"] = p[col].fillna(0) * p["w"]
    agg = p.groupby("player_id").agg(num=("wv", "sum"), den=("w", "sum"))
    return (agg["num"] / agg["den"].replace(0, np.nan)).rename(col)


def build_player_table(league: LeagueSettings, _weights: ModelWeights,
                       season: int = CURRENT_SEASON) -> pd.DataFrame:
    """Assemble every modelled feature into one row per player.

    Every history-derived input (production, O-line, pace, defense, separation) is
    bounded to the five seasons strictly before `season` -- the same lookback the
    live board uses by construction, since weekly_stats/play_by_play don't yet have
    a season that hasn't happened. Passing a past `season` (e.g. 2025 to backtest a
    2025 draft) makes that bound explicit: nothing from `season` or later leaks in,
    so this is what draft_backtest uses to see only what a drafter could have known
    on that draft day. The live depth-chart team override is skipped for a past
    season too -- it reflects today's rosters, not that season's.
    """
    sc = league.scoring
    lookback = list(range(season - 5, season))
    profiles = features.player_season_profiles(sc, league.te_premium_bonus, seasons=lookback)

    # --- recency-weighted production and reliability
    agg = pd.concat([
        _season_weighted(profiles, "fp_mean"),
        _season_weighted(profiles, "startable_rate"),
        _season_weighted(profiles, "spike_rate"),
        _season_weighted(profiles, "floor"),
        _season_weighted(profiles, "ceiling"),
        _season_weighted(profiles, "fp_cv"),
        _season_weighted(profiles, "snap_share"),
        _season_weighted(profiles, "touches"),
        _season_weighted(profiles, "target_share"),
        _season_weighted(profiles, "rec_per_game"),
    ], axis=1).reset_index()

    latest = profiles.sort_values("season").groupby("player_id").agg(
        name=("player_display_name", "last"),
        position=("position", "last"),
        team=("team", "last"),
        last_season=("season", "max"),
        seasons_played=("season", "nunique"),
        games_last=("games", "last"),
    ).reset_index()
    tbl = latest.merge(agg, on="player_id", how="left")

    # Keep players who were active recently AND held a real offensive role. Without
    # the role filter the board fills with special-teamers and emergency starters
    # whose tiny samples produce noisy, flattering rates.
    tbl = tbl[tbl["last_season"] >= profiles["season"].max() - 1]
    role = (
        (tbl["games_last"].fillna(0) >= 6)
        | (tbl["snap_share"].fillna(0) >= 0.40)
        | (tbl["touches"].fillna(0) >= 45)
    )
    tbl = tbl[role].reset_index(drop=True)

    # --- injury & age
    risk = features.injury_risk(profiles)
    tbl = tbl.merge(risk.drop(columns=["position"]), on="player_id", how="left")
    tbl["injury_risk"] = tbl["injury_risk"].fillna(0.25)
    tbl["age"] = tbl["age"].fillna(26.0) + (season - profiles["season"].max())

    # --- team environment
    if season >= CURRENT_SEASON:
        tbl = apply_current_team(tbl, sources.depth_charts())
    pbp = sources.play_by_play(seasons=lookback)
    ol = features.oline_ratings(pbp)
    pace = features.team_pace_and_split(pbp)
    dfn = features.defense_ratings(pbp, sources.weekly_stats(lookback), sc=sc)
    sos = features.strength_of_schedule(season, dfn)

    recent = int(pace["season"].max())
    ol_r = ol[ol["season"] == recent][["team", "run_block_z", "pass_block_z",
                                       "run_block_rank", "pass_block_rank"]]
    pace_r = pace[pace["season"] == recent][["team", "plays_per_game", "pass_rate",
                                             "rush_rate", "neutral_pass_rate"]]
    tbl = tbl.merge(ol_r, on="team", how="left").merge(pace_r, on="team", how="left")
    sos_cols = ["team", "divisional_games"] + [c for c in sos.columns if c.endswith("_z")]
    tbl = tbl.merge(sos[sos_cols], on="team", how="left")

    # --- red zone role, for the touchdown-luck regression in project()
    # Recency-weighted the same way as touches/target_share: every (player, season)
    # the player had *any* offensive role gets a row, filling zero red zone touches
    # rather than dropping the season, so a player's weighting window matches his
    # other features exactly instead of narrowing to just his red-zone seasons.
    rz = profiles[["player_id", "season"]].drop_duplicates().merge(
        features.player_redzone_role(pbp), on=["player_id", "season"], how="left")
    rz["rz_touches"] = rz["rz_touches"].fillna(0.0)
    rz["rz_td"] = rz["rz_td"].fillna(0.0)
    rz_w = pd.concat([
        _season_weighted(rz, "rz_touches"), _season_weighted(rz, "rz_td"),
    ], axis=1).reset_index()
    tbl = tbl.merge(rz_w, on="player_id", how="left")

    # --- separation and route efficiency (pass catchers only)
    try:
        from . import separation as sep_mod
        sep = sep_mod.separation_summary(seasons=lookback)
        if not sep.empty:
            tbl = tbl.merge(
                sep[["player_id", "sep_score", "avg_separation", "avg_cushion",
                     "yprr", "tprr", "yac_oe", "adot", "seasons_qualified"]],
                on="player_id", how="left",
            )
    except Exception as exc:
        print(f"separation data unavailable ({type(exc).__name__})")
    for c in ("sep_score", "yprr", "tprr", "avg_separation", "adot"):
        if c not in tbl.columns:
            tbl[c] = np.nan
    tbl["is_rookie"] = False

    # --- rookies, projected from draft capital rather than history
    try:
        from . import rookies as rk
        curves = rk.fit_draft_curves(sc, seasons=list(range(season - rk.FIT_SEASONS, season)))
        rb = rk.rookie_board(season, sc, curves)
        if not rb.empty:
            rb["rookie_consistency"] = [
                rk.rookie_consistency_prior(p, k, curves)
                for p, k in zip(rb["position"], rb["pick"])
            ]
            # Rookies get the same landing-spot context veterans do.
            rb = rb.merge(ol_r, on="team", how="left").merge(pace_r, on="team", how="left")
            rb = rb.merge(sos[sos_cols], on="team", how="left")
            # Drop anyone already modelled from real production (returning players
            # occasionally appear in a later draft class through supplemental entry).
            rb = rb[~rb["player_id"].isin(tbl["player_id"])]
            tbl = pd.concat([tbl, rb], ignore_index=True)
            tbl["is_rookie"] = tbl["is_rookie"].fillna(True)
    except Exception as exc:
        print(f"rookie projections unavailable ({type(exc).__name__}: {exc})")

    # Rookies join after the veteran fills, so backfill anything the scorer needs.
    # A single NaN here propagates through every multiplier and voids the whole rank.
    defaults = {
        "injury_risk": 0.22, "games_missed_rate": 0.10, "report_rate": 0.12,
        "heavy_seasons": 0.0, "recent_burden": 0.5, "seasons_played": 0,
        "games_last": 0, "age": 22.0, "startable_rate": np.nan, "fp_mean": np.nan,
        "divisional_games": 6.0, "rz_touches": 0.0, "rz_td": 0.0,
        "off_roster": False,
    }
    for col, val in defaults.items():
        if col in tbl.columns:
            tbl[col] = tbl[col].fillna(val)
        else:
            tbl[col] = val
    return tbl


def project(tbl: pd.DataFrame, league: LeagueSettings, weights: ModelWeights) -> pd.DataFrame:
    """Turn features into a projection, a reliability score, and a draft value."""
    t = tbl.copy()
    w = weights

    # ---- baseline: recency-weighted PPG, regressed toward the mean for small
    # samples so a two-game hot streak doesn't outrank a proven starter.
    #
    # The regression target has to be the mean of *starter-caliber* players at the
    # position, not the mean of everyone who logged a snap. Including third-string
    # backs drags the target down so far that regressing toward it would cut a
    # genuine RB1 by a third. We take the top N by weighted PPG as the comparison set.
    starter_n = {"QB": 24, "RB": 36, "WR": 48, "TE": 20}
    pos_target = {}
    for pos, chunk in t.groupby("position"):
        ranked = chunk.sort_values("fp_mean", ascending=False)
        n = min(starter_n.get(pos, 30), max(3, len(ranked)))
        pos_target[pos] = float(ranked["fp_mean"].head(n).mean())
    t["pos_target"] = t["position"].map(pos_target)

    # Shrink on games played, not seasons: 17 healthy games is a real sample,
    # three cameo appearances across two years is not.
    # Rookies arrive with a baseline already fitted from draft capital, so stash it
    # before the veteran regression runs. Letting that regression touch them would
    # replace the fitted value with a positional average and erase the whole
    # distinction between a top-five pick and a sixth-rounder.
    is_rook = t.get("is_rookie", pd.Series(False, index=t.index)).fillna(False).astype(bool)
    rookie_baseline = t["baseline_ppg"].copy() if "baseline_ppg" in t.columns else None

    games = t["games_last"].fillna(0) + 8 * (t["seasons_played"].fillna(1) - 1).clip(lower=0)
    shrink = (games / (games + 10)).clip(0.15, 0.93)
    t["baseline_ppg"] = t["fp_mean"].fillna(t["pos_target"]) * shrink + t["pos_target"] * (1 - shrink)

    if rookie_baseline is not None and is_rook.any():
        t.loc[is_rook, "baseline_ppg"] = rookie_baseline[is_rook]
        # Rookie availability is already capital-scaled; don't shrink them like vets.
        shrink = shrink.mask(is_rook, 0.60)

    # A veteran who hasn't played as recently as the board's freshest players is a
    # real unknown -- retired, hurt long-term, or just out of the league -- and last
    # season's box score can't tell us which. Discounted hard (60% per season stale,
    # compounding) rather than left at face value: without this, a two-seasons-
    # retired running back's still-strong 2020 form outprojected most of a real
    # board in a 2022 backtest and became the model's runaway top recommendation.
    # Rookies are unaffected (NaN last_season -> stale 0, and they're already
    # capital-scaled above, not derived from fp_mean).
    stale = (t["last_season"].max() - t["last_season"]).clip(lower=0).fillna(0)
    t["baseline_ppg"] = t["baseline_ppg"] * (0.4 ** stale)

    # A player absent from every team's current depth chart produced last season's
    # numbers for a roster he's no longer on -- cut, retired mid-cycle, or simply
    # without a team right now. last_season alone won't catch this (he may well
    # have played a full slate as recently as anyone else on the board), so it's a
    # separate check from the staleness above. Same 0.4x the model already uses for
    # one stale season: severe enough that a genuine value signal (real ADP already
    # crashed for the same reason) doesn't get doubled by an inflated projection on
    # top of it, without fully zeroing a talent who could still land a role.
    off_roster = t.get("off_roster", pd.Series(False, index=t.index)).fillna(False).astype(bool)
    t.loc[off_roster & ~is_rook, "baseline_ppg"] *= 0.4

    # ---- environment multipliers, each bounded by its configured weight
    def bounded(series: pd.Series, weight: float) -> pd.Series:
        z = series.fillna(0).clip(-2.5, 2.5) / 2.5
        return 1 + z * weight

    is_rb = t["position"].eq("RB")
    block_z = np.where(is_rb, t["run_block_z"].fillna(0), t["pass_block_z"].fillna(0))
    t["m_oline"] = bounded(pd.Series(block_z, index=t.index), w.oline)

    # Volume: pace helps everyone; run/pass split helps the side of the ball you're on.
    pace_z = features._zscore(t["plays_per_game"].fillna(t["plays_per_game"].mean()))
    split = np.where(is_rb, t["rush_rate"].fillna(0.43), t["neutral_pass_rate"].fillna(0.57))
    split_z = features._zscore(pd.Series(split, index=t.index))
    t["m_volume"] = bounded((pace_z + split_z) / 2, w.pace_volume)

    # Schedule: positive z = opponents allow more points to this position.
    sos_z = pd.Series(0.0, index=t.index)
    for pos in FANTASY_POSITIONS:
        col = f"sos_{pos}_z"
        if col in t.columns:
            sos_z = sos_z.where(t["position"] != pos, t[col].fillna(0))
    t["m_schedule"] = bounded(sos_z, w.schedule)

    # Divisional games: six games against defenses that game-plan for you specifically.
    # Modelled as a small drag that scales with how tough those division defenses are.
    div = t["divisional_games"].fillna(6)
    t["m_divisional"] = 1 - w.divisional * ((div - 6) / 6 + 0.5) * (-sos_z.fillna(0).clip(-2, 2) / 2)

    # Injury: expected games available out of 17. The risk score is a relative
    # ranking, not a literal miss probability, so it's scaled before converting.
    t["exp_games"] = (17 * (1 - t["injury_risk"] * 0.62)).clip(7, 17)
    if is_rook.any() and "exp_games" in tbl.columns:
        # Rookie availability comes from draft capital, not injury history they
        # don't have yet.
        t.loc[is_rook, "exp_games"] = tbl["exp_games"][is_rook].values
        t.loc[is_rook, "m_injury"] = 1.0
    t["m_injury"] = 1 - (t["injury_risk"] - 0.22).clip(-0.2, 0.6) * w.injury

    t["m_age"] = [features.age_adjustment(p, a) for p, a in zip(t["position"], t["age"])]

    # Separation and route efficiency, for pass catchers only. This is the signal
    # that distinguishes a receiver whose production came from genuine route-winning
    # from one riding target volume he may not keep. It only applies to players who
    # qualified on route count — everyone else sits at neutral.
    sep_z = pd.Series(0.0, index=t.index)
    if "sep_score" in t.columns:
        catchers = t["position"].isin(["WR", "TE"]) & t["sep_score"].notna()
        if catchers.any():
            sep_z.loc[catchers] = t.loc[catchers, "sep_score"].clip(-2.5, 2.5)
    t["m_separation"] = bounded(sep_z, w.separation)

    # Coverage-scheme trend, opt-in via w.coverage_trend (defaults to 0 -- see
    # ModelWeights.coverage_trend docstring for why this stays off by default).
    # For WR/TE: reward a short-area profile (high TPRR, low aDOT) over a
    # boundary/vertical one, since that's the archetype that beats zone coverage
    # and draws linebackers rather than nickel corners. For RB: reward receiving
    # role (target_share) directly, since backs already schemed into the passing
    # game are the ones positioned to exploit the same trend.
    ct_z = pd.Series(0.0, index=t.index)
    if w.coverage_trend:
        catchers = t["position"].isin(["WR", "TE"]) & t.get(
            "tprr", pd.Series(np.nan, index=t.index)).notna()
        if catchers.any():
            tprr_z = features._zscore(t.loc[catchers, "tprr"])
            adot = t.loc[catchers, "adot"]
            adot_z = features._zscore(adot.fillna(adot.mean()))
            ct_z.loc[catchers] = ((tprr_z - adot_z) / 2).clip(-2.5, 2.5)
        is_rb = t["position"].eq("RB") & t.get(
            "target_share", pd.Series(np.nan, index=t.index)).notna()
        if is_rb.any():
            ts = t.loc[is_rb, "target_share"]
            ct_z.loc[is_rb] = features._zscore(ts).clip(-2.5, 2.5)
    t["m_coverage_trend"] = bounded(ct_z, w.coverage_trend)

    # Touchdown luck: a player's own red zone conversion rate, regressed toward what
    # his position converts on average. Touchdowns dominate fantasy scoring and are
    # much noisier than yardage — a back who scored on 40% of his red zone carries
    # one season is not a repeatable event, he's a running back who is about to score
    # on a lot fewer of them next season. Uses the same "starter-caliber cohort" the
    # baseline regression above uses for pos_target, not the league as a whole,
    # because a bench-caliber player's red zone rate is exactly the kind of small,
    # unrepresentative sample that shouldn't set the bar a real starter is judged against.
    rz_baseline = {}
    for pos, chunk in t.groupby("position"):
        ranked = chunk.sort_values("fp_mean", ascending=False)
        n = min(starter_n.get(pos, 30), max(3, len(ranked)))
        top = ranked.head(n)
        touch_sum = top["rz_touches"].sum()
        rz_baseline[pos] = float(top["rz_td"].sum() / touch_sum) if touch_sum > 0 else 0.18
    t["rz_baseline_rate"] = t["position"].map(rz_baseline)
    t["rz_td_rate"] = t["rz_td"] / t["rz_touches"].replace(0, np.nan)
    t["m_td_luck"] = touchdown_luck_multiplier(
        t["rz_touches"], t["rz_td"], t["rz_baseline_rate"], w.td_luck)

    t["adj_ppg"] = (t["baseline_ppg"] * t["m_oline"] * t["m_volume"] * t["m_schedule"]
                    * t["m_divisional"] * t["m_injury"] * t["m_age"] * t["m_separation"]
                    * t["m_td_luck"] * t["m_coverage_trend"])
    t["proj_points"] = t["adj_ppg"] * t["exp_games"]

    # Full-PPR equivalent of the same projection. Published consensus rankings are
    # PPR, so converting the market's opinion into this league's format needs each
    # player's reception volume. The conversion itself is exact arithmetic — half
    # PPR is simply PPR minus half a point per catch — so the only estimate involved
    # is the reception count, which the projection already implies.
    rec_pg = t["rec_per_game"] if "rec_per_game" in t.columns else pd.Series(
        np.nan, index=t.index)
    # Rookies and anyone without a reception history fall back to positional norms.
    pos_rec = {"WR": 3.4, "TE": 2.8, "RB": 2.2, "QB": 0.0}
    rec_pg = rec_pg.fillna(t["position"].map(pos_rec)).fillna(0.0).clip(lower=0)
    t["proj_receptions"] = rec_pg * t["exp_games"]
    ppr_gap = 1.0 - float(league.scoring.rec)
    t["proj_points_ppr"] = t["proj_points"] + ppr_gap * t["proj_receptions"]

    # ---- consistency: the "will he give me a usable week" score.
    # Startable rate is the backbone; low variance and a high floor reinforce it;
    # injury risk cuts it, because an absent player is a zero.
    cv = t["fp_cv"].fillna(t["fp_cv"].median())
    cv_score = 1 - features._zscore(cv).clip(-2, 2) / 4
    floor_ratio = (t["floor"] / t["fp_mean"].replace(0, np.nan)).fillna(0.4).clip(0, 1)
    raw_consistency = (
        0.45 * t["startable_rate"].fillna(0.35)
        + 0.25 * floor_ratio
        + 0.15 * cv_score.clip(0, 1.5) / 1.5
        + 0.15 * (1 - t["injury_risk"])
    ).clip(0, 1)

    # Consistency needs the same small-sample regression the projection gets.
    # A backup who caught two touchdowns in his only three appearances will show a
    # perfect startable rate and near-zero variance; without this he outranks
    # established starters on "reliability" he has never actually demonstrated.
    cons_target = t["position"].map(
        t.assign(_c=raw_consistency).groupby("position").apply(
            lambda g: g.nlargest(40, "fp_mean")["_c"].mean(), include_groups=False
        ).to_dict()
    ).fillna(raw_consistency.mean())
    t["consistency"] = (raw_consistency * shrink + cons_target * (1 - shrink)).clip(0, 1)
    if is_rook.any() and "rookie_consistency" in t.columns:
        # Rookies get an explicit prior instead. They are the most volatile group in
        # fantasy: roles move mid-season and the floor is a healthy scratch.
        t.loc[is_rook, "consistency"] = t.loc[is_rook, "rookie_consistency"].fillna(0.35)
    t["consistency_sample_games"] = games

    # ---- value over replacement, then a consistency-weighted blend.
    repl = league.replacement_ranks()
    t["pos_rank"] = t.groupby("position")["proj_points"].rank(ascending=False, method="min")
    baselines = {}
    for pos in FANTASY_POSITIONS:
        chunk = t[t["position"] == pos].sort_values("proj_points", ascending=False)
        n = min(repl.get(pos, 24), max(1, len(chunk)))
        baselines[pos] = float(chunk["proj_points"].iloc[n - 1]) if len(chunk) else 0.0
    t["replacement_points"] = t["position"].map(baselines)
    t["vor"] = t["proj_points"] - t["replacement_points"]

    # Scale consistency onto the VOR distribution so the blend is apples to apples.
    vor_sd = t["vor"].std(ddof=0) or 1.0
    consistency_pts = (t["consistency"] - t["consistency"].mean()) / (
        t["consistency"].std(ddof=0) or 1.0) * vor_sd
    cw = weights.consistency_weight
    t["draft_score"] = (1 - cw) * t["vor"] + cw * consistency_pts
    if weights.qb_boost:
        t.loc[t["position"] == "QB", "draft_score"] *= (1 + weights.qb_boost)
    # Any player whose score is undefined would silently sort to the bottom rather
    # than announcing itself, so fail loudly instead.
    if t["draft_score"].isna().any():
        bad = t.loc[t["draft_score"].isna(), ["name", "position", "is_rookie"]]
        raise ValueError(f"{len(bad)} players scored NaN, e.g. "
                         f"{bad.head(3).to_dict('records')}")
    t["overall_rank"] = t["draft_score"].rank(ascending=False, method="min").astype(int)
    return t.sort_values("draft_score", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------- pick-aware layer

# The spread of a player's realised draft slot around his ADP: at least
# ADP_SD_FLOOR picks, growing with ADP_SD_RATE of the ADP. replay.market_z reads
# the same spread so a reach is measured in these units.
ADP_SD_FLOOR = 5.0
ADP_SD_RATE = 0.22


_erfc_vec = np.vectorize(math.erfc, otypes=[float])

# Shape of the distribution of a player's realised draft slot around his ADP.
# "normal" was the original; "logistic" keeps the same spread but replaces the
# Gaussian right tail with an exponential one. See survival_probability.
SURVIVAL_TAIL = "logistic"
# Logistic scale that matches a normal of standard deviation 1.
_LOGISTIC_SCALE = math.sqrt(3.0) / math.pi


def _log_sf(z: np.ndarray, tail: str) -> np.ndarray:
    """log P(the realised slot lands more than z standard deviations past ADP).

    Computed in logs because the useful range is the far tail, where the
    survival function underflows to zero in linear space and the conditional
    probability below would come out 0/0.
    """
    if tail == "normal":
        # erfc, not 1 - cdf: the complementary error function keeps its accuracy
        # into the far tail, where `1 - Phi(z)` is catastrophic cancellation and
        # underflows to exactly 0 past about z = 8. That underflow made the
        # numerator and the denominator of the conditional probability below
        # equal, and the survival of a hopelessly gone player came out as 1.0.
        with np.errstate(divide="ignore"):
            return np.log(np.clip(0.5 * _erfc_vec(z / math.sqrt(2)), 1e-300, 1.0))
    # Logistic: P(X > m + z*sd) = 1 / (1 + exp(z / _LOGISTIC_SCALE)), so the log
    # survival is -logaddexp(0, z / scale) -- stable for any z.
    return -np.logaddexp(0.0, z / _LOGISTIC_SCALE)


def _conditional_survival(adp: np.ndarray, current_pick: float, next_pick: float,
                          sd_floor: float, tail: str) -> np.ndarray:
    """P(still available at next_pick | still available now). NaN where no ADP."""
    adp = np.asarray(adp, dtype=float)
    # A row with no ADP is carried through as NaN rather than pushed into the
    # tail functions, which would warn on every board that has one.
    known = np.isfinite(adp)
    safe = np.where(known, adp, 1.0)
    sd = np.maximum(sd_floor, ADP_SD_RATE * safe)
    log_p = (_log_sf((next_pick - safe) / sd, tail)
             - _log_sf((current_pick - safe) / sd, tail))
    out = np.clip(np.exp(np.minimum(log_p, 0.0)), 0.0, 1.0)
    return np.where(known, out, np.nan)


def survival_probability(adp: float, current_pick: int, next_pick: int,
                         sd_floor: float = ADP_SD_FLOOR,
                         tail: str | None = None) -> float:
    """Chance a player is still on the board at your next pick, given that he is
    on it now.

    ADP is the centre of a distribution whose spread widens later in the draft,
    which matches how real draft variance behaves: pick 3 goes where pick 3
    goes, pick 90 is a coin flip across twenty names.

    The shape of that distribution is the part that matters most, and it is not
    normal. A Gaussian right tail says a player 3.5 standard deviations past his
    ADP is certainly gone -- and the board is full of players who are not. The
    live 2026 record had the second-best defense sitting undrafted at pick 123
    with an ADP of 93, in a room that had taken one defense in 122 picks; the
    normal tail put his survival at 0.00 and did the same for every other
    defense, which then told `expected_best_at_next_pick` that waiting at the
    position was worth its *worst* player. A whole position's marginal value was
    computed off that.

    A logistic of matching spread keeps the middle of the distribution and gives
    the tail an exponential decay instead, so the conditional survival of a
    player well past his ADP tends to exp(-(next - current) / scale): a constant
    hazard per pick rather than a cliff. "He has slid this far already, so the
    chance he goes in the next seven picks is roughly what it was for the last
    seven" is the right statement about a player the market has stopped pricing
    at his ADP.
    """
    if not np.isfinite(adp):
        return 0.5
    out = _conditional_survival(np.asarray([adp], dtype=float), current_pick, next_pick,
                                sd_floor, tail or SURVIVAL_TAIL)
    return float(out[0])


_erf_vec = np.vectorize(math.erf, otypes=[float])


def _norm_cdf_vec(z: np.ndarray) -> np.ndarray:
    return 0.5 * (1 + _erf_vec(z / math.sqrt(2)))


def survival_probability_vec(adp: np.ndarray, current_pick: int, next_pick: int,
                             sd_floor: float = ADP_SD_FLOOR,
                             tail: str | None = None) -> np.ndarray:
    """Vectorised form of survival_probability. plan_my_draft evaluates the whole
    board once per round, so the scalar version ran thousands of times per call."""
    adp = np.asarray(adp, dtype=float)
    out = _conditional_survival(adp, current_pick, next_pick, sd_floor,
                                tail or SURVIVAL_TAIL)
    return np.clip(np.nan_to_num(out, nan=0.5), 0.0, 1.0)


# ESPN's projection reads the current depth chart; the model's reads last
# season's box scores. Below ROLE_DISAGREEMENT of the model's number the
# player's role has shrunk (a backup now, a new team, an injury the model
# cannot see); above ROLE_GROWTH it has grown (a rookie or a new starter the
# box scores have not caught up with). Replaying the live draft showed both:
# Tyrone Tracy at 41 vs 185, KC Concepcion at 156 vs 102.
ROLE_DISAGREEMENT = 0.70
ROLE_FLOOR = 0.20
ROLE_GROWTH = 1.30
ROLE_CEILING = 1.30
# ESPN draft rank past which the list has stopped expressing an opinion. ESPN
# ranks far more players than any room drafts (2394 on the live 2026 list, for a
# 12-team 16-round draft that takes 192), so a rank this deep carries no
# information beyond "not a fantasy asset". 400 is comfortably past the last
# pick and comfortably inside the gap the live board shows: of the 169 rows ESPN
# declines to project, the best-ranked sits at 456 and all but two are past 1300.
ROLE_UNKNOWN_RANK = 400.0


def role_multiplier(tbl: pd.DataFrame) -> pd.Series:
    """Scale for pick_value from the ESPN/model projection ratio, continuous
    in the ratio: 1 inside [ROLE_DISAGREEMENT, ROLE_GROWTH]; below it
    ratio / ROLE_DISAGREEMENT (so 1 at the edge, ROLE_FLOOR at worst); above
    it ratio / ROLE_GROWTH capped at ROLE_CEILING. No step at either edge: a
    ratio of 0.699 and 0.701 land within a hair of each other.

    A player ESPN neither projects *nor* ranks inside ROLE_UNKNOWN_RANK is
    role-unknown rather than neutral, and takes ROLE_FLOOR. ESPN publishes a
    season projection for everyone it treats as rosterable, so no projection
    plus no meaningful rank is the list saying the player has no 2026 role at
    all -- while the model, reading five years of box scores, still has him at
    whatever his last real season was. That is how a receiver ESPN has at rank
    1401 with no projection reached the top five of a live recommendation. The
    factor is not a new number: it is what the continuous scale above already
    gives a player ESPN projects at zero, since a ratio of 0 clips to
    ROLE_FLOOR.

    A row ESPN declines to project but still ranks inside ROLE_UNKNOWN_RANK
    keeps 1: the list does rate him, it just has no projection row for him."""
    ones = pd.Series(1.0, index=tbl.index)
    if "espn_proj" not in tbl.columns or "proj_points" not in tbl.columns:
        return ones
    espn = pd.to_numeric(tbl["espn_proj"], errors="coerce")
    ours = pd.to_numeric(tbl["proj_points"], errors="coerce")
    rank = (pd.to_numeric(tbl["espn_rank"], errors="coerce") if "espn_rank" in tbl.columns
            else pd.Series(np.nan, index=tbl.index))
    ratio = espn / ours
    known = espn.notna() & ours.notna() & (ours > 0)
    unknown = espn.isna() & (rank.isna() | (rank > ROLE_UNKNOWN_RANK))
    low = known & (ratio < ROLE_DISAGREEMENT)
    high = known & (ratio > ROLE_GROWTH)
    out = ones.where(~low, (ratio / ROLE_DISAGREEMENT).clip(lower=ROLE_FLOOR, upper=1.0))
    out = out.where(~high, (ratio / ROLE_GROWTH).clip(lower=1.0, upper=ROLE_CEILING))
    return out.where(~unknown, ROLE_FLOOR)


def score_special_teams(special: pd.DataFrame, board: pd.DataFrame,
                        league: LeagueSettings, weights: ModelWeights) -> pd.DataFrame:
    """Put ESPN's K and D/ST projections on the board's own draft_score scale.

    Everything else on the board scores (1 - cw) * VOR + cw * consistency_pts,
    where consistency is centred across the board and so contributes nothing at
    the mean. This codebase has no week-to-week history for a kicker or a
    defense, so they are given exactly the board's mean consistency: no
    reliability claim in either direction, and a draft_score of (1 - cw) * VOR,
    which is what any average-consistency player on the board already gets.
    Inventing a consistency for them would be the only made-up number here.

    Replacement level is `league.replacement_ranks()`'s K/DST entry -- the last
    one a team would start -- so a defense's value over replacement is measured
    the same way a running back's is, and the two are comparable by
    construction rather than by assertion.
    """
    out = special.copy()
    if out.empty:
        return out
    repl = league.replacement_ranks()
    baselines = {}
    for pos, chunk in out.groupby("position"):
        ranked = chunk.sort_values("proj_points", ascending=False)
        n = min(int(repl.get(str(pos), len(ranked))), len(ranked))
        baselines[str(pos)] = float(ranked["proj_points"].iloc[n - 1]) if n else 0.0
    out["replacement_points"] = out["position"].map(baselines)
    out["vor"] = out["proj_points"] - out["replacement_points"]
    mean_consistency = (float(board["consistency"].mean())
                        if "consistency" in board.columns and len(board) else 0.5)
    out["consistency"] = mean_consistency
    out["draft_score"] = (1 - weights.consistency_weight) * out["vor"]
    return out


# A multiplier above this would flip the sign of a negative pick_value rather
# than push it further down. Nothing in `recommend` comes close -- role caps at
# ROLE_CEILING 1.3, need at 1.18 -- but the reflection below is only monotone
# while it holds.
DISCOUNT_CEILING = 2.0


def recommend(board: pd.DataFrame, league: LeagueSettings, current_pick: int,
              next_pick: int | None, roster: dict[str, int] | None = None,
              top_n: int = 8, mine: pd.DataFrame | None = None,
              bye_weight: float = 0.0,
              adp_shift: float | Mapping[str, float] = 0.0,
              role_weights: Mapping[str, float] | None = None) -> pd.DataFrame:
    """Rank available players for the pick that's on the clock.

    Two ideas drive the ordering beyond raw value:
      * Opportunity cost — a player you're confident survives to your next pick is
        worth less right now than an equally good player who certainly won't.
      * Roster need — value is discounted once a position is full and the player
        would only be a bench body, and boosted when a starting slot is still open.

    `mine` is the board rows you already hold. With bye_weight > 0 a candidate whose
    bye_week matches theirs loses bye_weight of pick_value per same-position player
    and half that per other player; `bye_conflicts` names them either way.

    `adp_shift` is how many picks earlier than ADP this room has been taking
    players (replay.room_drift), one number or one per position; survival odds
    are computed against ADP minus it.

    `role_weights` opens the two `roles.py` terms — `start_prob` (what a bench
    player is worth once you price the weeks he actually starts) and `handcuff`
    (what a backup is worth conditional on the man ahead of him). Both default
    to 0, and at 0 the result is bit-identical to one computed without them; see
    CHANGELOG.md for the evidence behind any non-zero value.
    """
    avail = board[~board["drafted"]].copy() if "drafted" in board.columns else board.copy()
    if avail.empty:
        return avail

    roster = roster or {}
    if isinstance(adp_shift, Mapping):
        shift = avail["position"].map(adp_shift).fillna(0.0).to_numpy(dtype=float)
    else:
        shift = float(adp_shift)
    if next_pick:
        avail["p_available_next"] = survival_probability_vec(
            avail["adp"].to_numpy() - shift, current_pick, next_pick)
    else:
        avail["p_available_next"] = 0.0

    if role_weights and float(role_weights.get("start_prob", 0.0)):
        from . import roles

        # Applied to draft_score itself, before anything downstream reads it,
        # rather than reconstructed as a correction afterwards. A player in the
        # lineup a fraction m of the time is worth m of his projection, and
        # draft_score is built from that projection, so this is the one place
        # the statement is true. Doing it later means undoing need_mult and
        # role_mult by algebra, and that algebra is wrong the moment either is
        # applied by anything other than plain multiplication -- which is
        # exactly what `_discount` does on the negative half of the board.
        avail["p_start"] = roles.start_probabilities(avail, league, mine)
        avail["draft_score_full"] = avail["draft_score"]
        avail["draft_score"] = roles.scaled_draft_score(
            avail, league, mine, float(role_weights["start_prob"]))

    # The heart of it: what a position is expected to still offer at your next turn.
    # Raw value says take the best player; that's wrong in a snake draft, because
    # passing on an elite QB costs you almost nothing (the QB you get two rounds
    # later is nearly as good) while passing on an elite RB costs a great deal.
    # Only the *marginal* gain over the wait matters.
    fallback = expected_best_at_next_pick(avail)
    avail["fallback_value"] = avail["position"].map(fallback).fillna(0.0)
    avail["marginal_value"] = avail["draft_score"] - avail["fallback_value"]
    avail["urgency"] = 1 - avail["p_available_next"]

    need = _positional_need(league, roster)
    avail["need_mult"] = avail["position"].map(need).fillna(0.7)

    # A small share of raw value is retained so a truly generational player still
    # rises even when his position is deep behind him.
    #
    # Not for kickers and defenses. That share is an escape hatch for scarcity,
    # and neither position is ever scarce: ESPN lists 32 of each for a league
    # that needs one apiece, so the supply cannot run out before the draft does
    # and there is no such thing as missing out on the position. Keeping the raw
    # term for them prices a top defense as if passing on it cost you the slot,
    # which put one second on the board in round 8 of a 14-round draft. Priced on
    # marginal value alone -- how much better than waiting -- a defense stays an
    # option from the middle rounds and wins the last pick, which is where the
    # league actually forces the slot.
    raw_share = np.where(avail["position"].isin(SPECIAL_POSITIONS), 0.0, 0.20)
    avail["pick_value"] = _discount(
        (1 - raw_share) * avail["marginal_value"] + raw_share * avail["draft_score"],
        avail["need_mult"])

    avail["role_mult"] = role_multiplier(avail)
    avail["pick_value"] = _discount(avail["pick_value"], avail["role_mult"])

    if role_weights and float(role_weights.get("handcuff", 0.0)):
        from . import roles

        # Contingent points are points, in the same units draft_score is built
        # from, so they are added rather than multiplied -- scaling a deep bench
        # player's negative pick_value by his handcuff case pushes him further
        # down, which is backwards. The start-probability term is not here: it
        # was applied to draft_score above.
        avail["roles_bonus"] = roles.pick_value_bonus(
            avail, league, mine, handcuff_weight=float(role_weights["handcuff"]))
        avail["pick_value"] = avail["pick_value"] + avail["roles_bonus"]

    avail["bye_conflicts"] = ""
    avail["bye_mult"] = 1.0
    if mine is not None and not mine.empty and "bye_week" in avail.columns \
            and "bye_week" in mine.columns:
        held = mine.dropna(subset=["bye_week"])
        names, mults = [], []
        for _, r in avail.iterrows():
            same = held[held["bye_week"] == r["bye_week"]]
            same_pos = same[same["position"] == r["position"]]
            other = same[same["position"] != r["position"]]
            names.append(", ".join(same["name"].tolist()))
            mults.append(max(0.5, 1 - bye_weight * (len(same_pos) + 0.5 * len(other))))
        avail["bye_conflicts"] = names
        avail["bye_mult"] = mults
        avail["pick_value"] = _discount(avail["pick_value"], avail["bye_mult"])
    return avail.sort_values("pick_value", ascending=False).head(top_n)


def expected_best_at_next_pick(avail: pd.DataFrame) -> dict[str, float]:
    """Expected draft_score of the best player at each position who survives to your
    next pick.

    Walks each position from the top down, accumulating the chance every better
    player is already gone. That product is the probability this player is the best
    one left, and the sum over players is the expected value of waiting.
    """
    out: dict[str, float] = {}
    for pos, chunk in avail.groupby("position"):
        chunk = chunk.sort_values("draft_score", ascending=False)
        expected, p_all_gone = 0.0, 1.0
        for _, r in chunk.iterrows():
            p = float(r.get("p_available_next", 0.0))
            expected += float(r["draft_score"]) * p * p_all_gone
            p_all_gone *= (1 - p)
            if p_all_gone < 0.005:
                break
        # If the position empties out entirely, waiting is worth the worst on the board.
        out[str(pos)] = expected + p_all_gone * float(chunk["draft_score"].min() if len(chunk) else 0)
    return out


# How much a bench player at each position is worth relative to the one ahead of him.
# RB and WR depth holds real value because injuries and byes force them into lineups
# constantly. A second QB, in a non-superflex league, never starts at all outside
# this league's own FLEX/superflex rules -- QB isn't flex-eligible like RB/WR/TE
# are, so 0.20 wasn't steep enough: a mock_draft check (30 trials, 1-QB league)
# had the model rostering a real backup QB (Mahomes-plus tier, not a late dart
# throw) in 27 of 30 trials, because QB draft_score is structurally so much
# larger than every other position's at that draft slot -- passing yards/TDs
# outscore the field even for a QB1-caliber name nobody would actually roster
# twice -- that even an 80% discount left him ahead of real bench RB/WR value.
# 0.04 pushes a true backup below that bench value in the same test.
BACKUP_DECAY = {"QB": 0.04, "TE": 0.28, "RB": 0.72, "WR": 0.70}
# Past these counts a player cannot realistically help you, whatever his projection.
ROSTER_CAP = {"QB": 2, "TE": 2, "RB": 6, "WR": 7}


def _positional_need(league: LeagueSettings, roster: dict[str, int]) -> dict[str, float]:
    """Multiplier per position, driven by the chance the player ever starts for you.

    An empty starting slot is worth a premium. A bench spot is worth only as much as
    the odds it gets pressed into the lineup, which decays fast at positions with one
    starting slot — the reason a great backup quarterback wins you nothing.
    """
    need = {}
    superflex = getattr(league, "superflex", 0) or 0
    flex_slots = league.starters.get("FLEX", 0)
    flex_filled = sum(max(0, roster.get(p, 0) - league.starters.get(p, 0))
                      for p in league.flex_eligible)

    # In superflex the second quarterback is a starter, not a bench body, so every
    # rule that assumes one QB slot has to shift: more of them are required, they
    # stay valuable deeper, and the early-round dampener must not apply.
    required_qb = league.starters.get("QB", 0) + superflex
    caps = dict(ROSTER_CAP)
    decay = dict(BACKUP_DECAY)
    if superflex:
        caps["QB"] = league.starters.get("QB", 0) + superflex + 1
        decay["QB"] = 0.55

    for pos in FANTASY_POSITIONS:
        required = required_qb if pos == "QB" else league.starters.get(pos, 0)
        have = roster.get(pos, 0)
        cap = caps.get(pos, 6)
        if have >= cap:
            need[pos] = 0.02
        elif have < required:
            need[pos] = 1.18
        elif pos in league.flex_eligible and flex_filled < flex_slots:
            need[pos] = 1.05
        else:
            depth = have - required + (1 if pos in league.flex_eligible
                                       and flex_filled >= flex_slots else 0)
            need[pos] = max(0.03, decay.get(pos, 0.5) ** max(1, depth))

    # Kickers and defenses have exactly one starting slot each and no bench value
    # at all: a second one never plays, and the position's whole supply survives
    # to the end of the draft, which the marginal-value term already prices.
    # They are given their own need so that filling the slot registers, without
    # appearing in the need of any other position -- FANTASY_POSITIONS above is
    # untouched, so nothing about QB/RB/WR/TE need changes.
    for pos in SPECIAL_POSITIONS:
        required = league.starters.get(pos, 0)
        if not required:
            continue
        need[pos] = 1.18 if roster.get(pos, 0) < required else 0.02

    # Don't let a lone QB/TE slot pull you into reaching in the early rounds, where
    # the elite RB and WR you'd be passing on are the scarcer resource. This is a
    # 1-QB argument only — in superflex, quarterbacks genuinely are the scarce
    # resource and reaching for one early is correct.
    filled = sum(roster.values())
    if not superflex and roster.get("QB", 0) == 0 and filled < 5:
        need["QB"] = min(need["QB"], 0.80)
    if roster.get("TE", 0) == 0 and filled < 3:
        need["TE"] = min(need["TE"], 0.88)
    return need


def explain(row: pd.Series) -> str:
    """Plain-language reasoning for a recommendation."""
    bits = []
    if row.get("pos_rank"):
        bits.append(f"{row['position']}{int(row['pos_rank'])} by projection "
                    f"({row['proj_points']:.0f} pts, {row['adj_ppg']:.1f}/gm)")
    c = row.get("consistency")
    # A kicker or a defense carries the board's mean consistency, which is a
    # deliberate absence of a claim (score_special_teams), not a measurement.
    # Printing it would read as one.
    if c is not None and np.isfinite(c) and row.get("position") not in SPECIAL_POSITIONS:
        sr = row.get("startable_rate")
        bits.append(f"consistency {c:.2f}" + (f", startable in {sr:.0%} of weeks" if np.isfinite(sr or np.nan) else ""))
    for label, key in [
        ("O-line", "m_oline"), ("volume/pace", "m_volume"),
        ("schedule", "m_schedule"), ("age curve", "m_age"),
        ("separation", "m_separation"), ("touchdown regression", "m_td_luck"),
        ("coverage trend", "m_coverage_trend"),
    ]:
        v = row.get(key)
        if v is not None and np.isfinite(v) and abs(v - 1) > 0.02:
            bits.append(f"{label} {'+' if v > 1 else ''}{(v - 1) * 100:.1f}%")
    ir = row.get("injury_risk")
    if ir is not None and np.isfinite(ir):
        bits.append(f"injury risk {ir:.0%} (~{row.get('exp_games', 17):.0f} games)")
    ep = row.get("espn_proj")
    rm = row.get("role_mult")
    if ep is not None and pd.notna(ep):
        tag = ""
        if rm is not None and pd.notna(rm) and rm < 1:
            tag = f" ({ep / row['proj_points']:.0%} of model: role shrank, value scaled)"
        elif rm is not None and pd.notna(rm) and rm > 1:
            tag = f" ({ep / row['proj_points']:.0%} of model: role grew, value scaled)"
        bits.append(f"ESPN projects {ep:.0f}{tag}")
    elif rm is not None and pd.notna(rm) and rm < 1:
        er = row.get("espn_rank")
        where = ("does not rank him" if er is None or pd.isna(er)
                 else f"ranks him {int(er)}")
        bits.append(f"ESPN does not project him and {where}: role unknown, "
                    f"value scaled to {rm:.0%}")
    inj = row.get("espn_injury")
    # NaN is truthy, and ESPN files no injury status at all for a team defense,
    # so an unguarded check printed "ESPN status nan" on every D/ST.
    if inj is not None and pd.notna(inj) and inj != "ACTIVE":
        bits.append(f"ESPN status {inj}")
    p = row.get("p_available_next")
    if p is not None and np.isfinite(p):
        bits.append(f"{p:.0%} chance he lasts to your next pick")
    bye = row.get("bye_week")
    if bye is not None and pd.notna(bye):
        conflicts = row.get("bye_conflicts") or ""
        bits.append(f"bye week {int(bye)}" + (f" stacks with {conflicts}" if conflicts else ""))
    ent = row.get("role_entropy")
    if ent is not None and pd.notna(ent):
        # Uncertainty is not automatically bad: late in a draft an unresolved
        # ceiling is often underpriced. What it must not do is read the same as
        # a player who barely has a job, so the direction is named.
        from . import roles

        # NaN is truthy, so a missing label would print as "nan" — the same trap
        # that had `explain` announcing "ESPN status nan" on every defense.
        kind = row.get("entropy_kind")
        churn = row.get("role_churn")
        why = []
        if kind is not None and pd.notna(kind) and str(kind):
            why.append(str(kind))
        if churn is not None and pd.notna(churn) and churn > roles.ENTROPY_CHURN_SCALE / 2:
            why.append("snap share moved week to week")
        # Say which half the score rests on when it rests on only one: the two
        # do not have the same evidential standing.
        basis = row.get("entropy_basis")
        if basis is not None and pd.notna(basis) and str(basis) \
                and str(basis) != roles.ENTROPY_BASIS_BOTH:
            why.append(str(basis))
        bits.append(f"role uncertainty {float(ent):.2f}"
                    + (f" ({'; '.join(why)})" if why else ""))
    shares = [(label, row.get(key)) for label, key in
              (("targets", "target_share"), ("carries", "carry_share"),
               ("red zone", "redzone_share"), ("snaps", "snap_share"))]
    named = [f"{label} {float(v):.0%}" for label, v in shares if v is not None and pd.notna(v)]
    if named:
        bits.append("share of team: " + ", ".join(named))
    return "; ".join(bits)
