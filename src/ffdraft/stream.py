"""Weekly kicker and defence streaming: who to pick up for *this* week.

A different question from the draft. The draft asks whether a required position
can still be filled at all (`server._plan_pool`, #26/#32, a counting model over
league supply); this asks which of the available options has the best matchup on
one particular Sunday. Nothing here consults that counting model and nothing here
uses a season projection to rank -- both answer "is this player good", where the
question is "is this week good for this player".

What the data supports, measured before this was designed:

  * implied totals and spreads are in `sources.schedules()` and cover the whole
    board six weeks out and nothing beyond about week seven, filling in as each
    week approaches. Every row says whether it had one; a season number is never
    substituted for a missing line.
  * weather does not exist pre-game at all. `temp` and `wind` are populated
    after kickoff and only outdoors -- 0 of 272 rows for the current season. The
    only weather-shaped fact available in advance is `roof`, so that is what is
    used, and live forecasts would need a source this project does not have.
  * kicker production is a position in `weekly_stats` with the full distance
    buckets. Team defence is not a position there, but every component the
    league's own D/ST bands need is: `def_sacks`, `def_interceptions`,
    `def_fumbles`, `def_safeties`, `def_tds`, the block columns, and team
    offensive yards to give the opponent's yards allowed. Points allowed comes
    from the final scores in `schedules`. So no play-by-play is required, which
    is what the task assumed would be.

Scoring uses the league's own bands out of `mSettings`, not a generic template,
so the calibration target is what this league would actually have paid.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import sources

# Weeks of history used to describe how a defence and an offence have been
# playing. Short on purpose: this is a streaming call, and a unit that changed
# in September is not the unit that played in Week 1.
FORM_WEEKS = 5
# How many weeks ahead the answer looks, so a waiver claim can cover a bye.
LOOK_AHEAD = 2


def _team_week(seasons: list[int]) -> pd.DataFrame:
    """One row per team per week: what its offence did and what its defence did.

    Everything the league's D/ST bands need except points allowed, which is a
    final score and comes from the schedule.
    """
    w = sources.weekly_stats(seasons)
    if w.empty:
        return pd.DataFrame()
    cols = {
        "off_yards": ["passing_yards", "rushing_yards"],
        "def_sacks": ["def_sacks"],
        "def_interceptions": ["def_interceptions"],
        "def_fumbles": ["def_fumbles"],
        "def_safeties": ["def_safeties"],
        "def_tds": ["def_tds"],
        "def_blocks": ["def_fg_blocks", "def_pat_blocks", "def_punt_blocks"],
        "return_tds": ["pt_return_tds"],
    }
    have = {out: [c for c in src if c in w.columns] for out, src in cols.items()}
    frame = w.groupby(["season", "week", "recent_team"], observed=True).agg(
        **{out: pd.NamedAgg(column=src[0], aggfunc="sum") for out, src in have.items() if src}
    ).reset_index()
    # Multi-column sums the aggregation above cannot express in one pass.
    for out, src in have.items():
        if len(src) > 1:
            extra = w.groupby(["season", "week", "recent_team"], observed=True)[src].sum().sum(axis=1)
            frame[out] = frame.set_index(["season", "week", "recent_team"]).index.map(extra)
    frame = frame.rename(columns={"recent_team": "team"})
    for out in cols:
        if out not in frame.columns:
            frame[out] = 0.0
    return frame.fillna(0.0)


def _band(value: float, bands: dict, prefix: str, edges: list[tuple[float, float, str]]) -> float:
    """The one banded value that applies, or 0 when the league defines no band
    covering it. Bands the league leaves out score nothing rather than falling
    into a neighbour."""
    for lo, hi, name in edges:
        if lo <= value <= hi:
            return float(bands.get(f"{prefix}{name}", 0.0))
    return 0.0


POINTS_ALLOWED_BANDS: list[tuple[float, float, str]] = [
    (0, 0, "0"), (1, 6, "1_6"), (7, 13, "7_13"), (14, 17, "14_17"),
    (18, 27, "18_27"), (28, 34, "28_34"), (35, 45, "35_45"), (46, 10_000, "46_plus"),
]
YARDS_ALLOWED_BANDS: list[tuple[float, float, str]] = [
    (0, 99, "under_100"), (100, 199, "100_199"), (200, 299, "200_299"),
    (300, 349, "300_349"), (350, 399, "350_399"), (400, 449, "400_449"),
    (450, 499, "450_499"), (500, 549, "500_549"), (550, 100_000, "550_plus"),
]


def dst_weekly_points(seasons: list[int], bands: dict) -> pd.DataFrame:
    """What each team's defence would have scored, week by week, under this
    league's own D/ST bands.

    The calibration target for the streamer. Built from `weekly_stats` (the
    counting stats and the opponent's offensive yards) and `schedules` (the
    final score, which is the points-allowed band), so it needs no play-by-play.

    Points allowed is the opponent's final score. ESPN excludes points the
    opposing defence and special teams scored themselves; that distinction is
    not in the final score and is not made here, which slightly understates a
    defence whose offence gave up a pick six. Stated rather than silently
    absorbed.
    """
    tw = _team_week(seasons)
    if tw.empty:
        return pd.DataFrame()
    sch = sources.schedules()
    sch = sch[sch["season"].isin(seasons)]
    rows = []
    for _, g in sch.iterrows():
        for team, opp, allowed in ((g["home_team"], g["away_team"], g["away_score"]),
                                   (g["away_team"], g["home_team"], g["home_score"])):
            if pd.isna(allowed):
                continue
            rows.append({"season": int(g["season"]), "week": int(g["week"]),
                         "team": str(team), "opponent": str(opp),
                         "points_allowed": float(allowed)})
    games = pd.DataFrame(rows)
    if games.empty:
        return pd.DataFrame()
    own = tw.rename(columns={c: c for c in tw.columns})
    opp_yards = tw[["season", "week", "team", "off_yards"]].rename(
        columns={"team": "opponent", "off_yards": "yards_allowed"})
    out = games.merge(own, on=["season", "week", "team"], how="left")
    out = out.merge(opp_yards, on=["season", "week", "opponent"], how="left")
    out = out.fillna({c: 0.0 for c in ("off_yards", "def_sacks", "def_interceptions",
                                       "def_fumbles", "def_safeties", "def_tds",
                                       "def_blocks", "return_tds", "yards_allowed")})
    td_points = float(bands.get("int_return_td", 6.0))
    out["points"] = (
        out["def_sacks"] * float(bands.get("sack", 0.0))
        + out["def_interceptions"] * float(bands.get("interception", 0.0))
        + out["def_fumbles"] * float(bands.get("fumble_recovery", 0.0))
        + out["def_safeties"] * float(bands.get("safety", 0.0))
        + out["def_blocks"] * float(bands.get("blocked_kick", 0.0))
        + (out["def_tds"] + out["return_tds"]) * td_points
        + out["points_allowed"].map(
            lambda v: _band(v, bands, "points_allowed_", POINTS_ALLOWED_BANDS))
        + out["yards_allowed"].map(
            lambda v: _band(v, bands, "yards_allowed_", YARDS_ALLOWED_BANDS))
    )
    return out[["season", "week", "team", "opponent", "points", "points_allowed",
                "yards_allowed", "def_sacks", "def_interceptions", "def_fumbles"]]


FG_BUCKETS = [
    ("fg_made_0_19", "fg_made_under_40"), ("fg_made_20_29", "fg_made_under_40"),
    ("fg_made_30_39", "fg_made_under_40"), ("fg_made_40_49", "fg_made_40_49"),
    ("fg_made_50_59", "fg_made_50_59"), ("fg_made_60_", "fg_made_60_plus"),
]


def k_weekly_points(seasons: list[int], items: dict) -> pd.DataFrame:
    """What each kicker actually scored, week by week, under this league's own
    kicker items -- distance buckets, missed-kick penalty and extra points."""
    w = sources.weekly_stats(seasons)
    if w.empty or "position" not in w.columns:
        return pd.DataFrame()
    k = w[w["position"] == "K"].copy()
    if k.empty:
        return pd.DataFrame()
    points = pd.Series(0.0, index=k.index)
    for column, item in FG_BUCKETS:
        if column in k.columns:
            points = points + k[column].fillna(0.0) * float(items.get(item, 0.0))
    if "fg_missed" in k.columns:
        points = points + k["fg_missed"].fillna(0.0) * float(items.get("fg_missed", 0.0))
    if "pat_made" in k.columns:
        points = points + k["pat_made"].fillna(0.0) * float(items.get("pat_made", 0.0))
    # Built column by column rather than renamed in place: `weekly_stats`
    # already carries a `team` column of its own, so renaming `recent_team` to
    # `team` leaves two columns of that name and every later row lookup returns
    # a two-element Series instead of a string. It fails silently -- the join
    # key becomes unhashable-looking text and every merge misses.
    return pd.DataFrame({
        "season": k["season"].to_numpy(),
        "week": k["week"].to_numpy(),
        "player": k["player_display_name"].astype(str).to_numpy(),
        "team": k["recent_team"].astype(str).to_numpy(),
        "opponent": (k["opponent_team"].astype(str).to_numpy()
                     if "opponent_team" in k.columns else ""),
        "points": points.to_numpy(),
    })


def _form(history: pd.DataFrame, value: str, season: int, week: int,
          weeks: int = FORM_WEEKS) -> dict[str, float]:
    """Per-team mean of `value` over the `weeks` games before (season, week),
    reaching back into the previous season when the current one is young. A
    streaming call in Week 2 has one game of this season and needs more."""
    if history.empty:
        return {}
    prior = history[(history["season"] < season)
                    | ((history["season"] == season) & (history["week"] < week))]
    if prior.empty:
        return {}
    prior = prior.sort_values(["season", "week"])
    return {str(team): float(g[value].tail(weeks).mean())
            for team, g in prior.groupby("team", observed=True)}


def matchup_table(season: int, week: int, bands: dict, items: dict,
                  history_seasons: list[int] | None = None) -> dict:
    """Every team's matchup for one week, with the facts a streamer ranks on.

    `line_basis` per row is the point of it: `implied total` when the book has
    posted one for that game, `no line` when it has not. A missing line is never
    replaced by a season number.
    """
    history_seasons = history_seasons or [season - 1, season]
    sch = sources.schedules()
    wk = sch[(sch["season"] == season) & (sch["week"] == week)]
    dst_hist = dst_weekly_points(history_seasons, bands)
    tw = _team_week(history_seasons)
    dst_form = _form(dst_hist, "points", season, week)
    off_form = _form(tw.rename(columns={"off_yards": "value"}).assign(points=lambda d: d["value"]),
                     "points", season, week) if not tw.empty else {}
    rows = []
    for _, g in wk.iterrows():
        total = g.get("total_line")
        spread = g.get("spread_line")
        has_line = pd.notna(total)
        for team, opp, home in ((g["home_team"], g["away_team"], True),
                                (g["away_team"], g["home_team"], False)):
            # A defence wants a low-scoring game its side is favoured in; the
            # implied total for THIS team's opponent is total/2 shaded by the
            # spread, which is the closest thing to "how many will they score".
            opp_implied = (None if not has_line
                           else float(total) / 2 - (float(spread) / 2 if pd.notna(spread) else 0.0)
                           * (1 if home else -1))
            rows.append({
                "team": str(team), "opponent": str(opp), "home": home,
                "roof": (str(g["roof"]) if pd.notna(g.get("roof")) else None),
                "total_line": (float(total) if has_line else None),
                "spread_line": (float(spread) if pd.notna(spread) else None),
                "opponent_implied_points": (None if opp_implied is None
                                            else round(opp_implied, 1)),
                "line_basis": "implied total" if has_line else "no line",
                "dst_form": round(dst_form.get(str(team), float("nan")), 2)
                if str(team) in dst_form else None,
                "opponent_offense_yards_form": (round(off_form[str(opp)], 1)
                                                if str(opp) in off_form else None),
            })
    covered = sum(1 for r in rows if r["line_basis"] == "implied total")
    return {
        "season": season, "week": week,
        "games": int(len(wk)),
        "line_coverage": {"rows_with_a_line": covered, "rows_without": len(rows) - covered,
                          "note": "books post lines a few weeks out; a row without one is "
                                  "ranked on form and venue alone and says so, and a season "
                                  "projection is never substituted"},
        "weather": "roof only — temp and wind are recorded after kickoff and only "
                   "outdoors, so no future game has them from this source",
        "teams": rows,
    }


# The features a week's matchup offers, in the order the fitted coefficients
# hold. Deliberately few: this is fitted on a few hundred team-weeks and every
# extra column is another chance to fit noise.
DST_FEATURES = ("opponent_implied_points",)
K_FEATURES = ("team_implied_points",)
# Everything tried and dropped, with why, so nobody re-adds them expecting a
# gain. Measured on 2025, two disjoint blocks of weeks:
#
#   feature                      D/ST outcome
#   opponent_implied_points      signs agree, spread 0.13, kept
#   home                         signs FLIP (+0.71 / -0.66), and held-out RMSE
#                                gets worse (5.29 -> 5.32); dropped
#   opponent_offense_yards_form  signs FLIP (-0.010 / +0.006), RMSE worse;
#                                dropped
#
# The single-feature fit is not merely the one that agrees, it is also the most
# accurate out of sample. That is the usual shape when a feature is noise: it
# buys in-sample fit and costs out-of-sample error.
DROPPED_FEATURES = ("home", "opponent_offense_yards_form", "roof")


def _fit(x: np.ndarray, y: np.ndarray, ridge: float = 1.0) -> np.ndarray:
    """Least squares with a small ridge, intercept last. The ridge is there so a
    block with a near-collinear column cannot return a wild coefficient that the
    block comparison would then read as disagreement."""
    design = np.column_stack([x, np.ones(len(x))])
    penalty = ridge * np.eye(design.shape[1])
    penalty[-1, -1] = 0.0            # never penalise the intercept
    return np.linalg.solve(design.T @ design + penalty, design.T @ y)


def calibration_blocks(frame: pd.DataFrame, features: tuple[str, ...],
                       target: str = "points") -> dict:
    """Fit on two disjoint halves of the weeks and report both, never their mean.

    `adp.DEFAULT_BLOCKS`' rule applied to a calibration rather than a paired
    draft: the question is whether the fit says the same thing on two samples of
    the same data, and if it does not, the units it produces are not real. Odd
    against even weeks rather than early against late, so a block is not also a
    slice of the season.

    Each block is fitted on its own weeks and scored on the other's, so
    `held_out_rmse` is out of sample. Read it against `held_out_sd_of_target`:
    a fit whose error equals the target's own spread has learned nothing, and
    the margin it produces is not in points however much it looks like points.
    """
    needed = [*features, target, "week"]
    if frame.empty or any(c not in frame.columns for c in needed):
        # A position with no history at all -- an empty fixture, a season the
        # source has not published. Ordinal by default, because "no evidence"
        # and "evidence that disagrees" both mean the margin is not in points.
        return {"blocks": [], "usable": False, "margin_units": "ordinal",
                "note": "no rows to fit on for this position"}
    frame = frame.dropna(subset=[*features, target])
    if len(frame) < 4 * len(features):
        return {"blocks": [], "usable": False, "margin_units": "ordinal",
                "note": f"only {len(frame)} complete rows for {len(features)} features"}
    y = frame[target].to_numpy(dtype=float)
    x = frame[list(features)].to_numpy(dtype=float)
    odd = (frame["week"].to_numpy() % 2 == 1)
    out: list[dict] = []
    for name, mask in (("odd weeks", odd), ("even weeks", ~odd)):
        if mask.sum() < 2 * len(features):
            continue
        coef = _fit(x[mask], y[mask])
        held = ~mask
        pred = x[held] @ coef[:-1] + coef[-1]
        resid = y[held] - pred
        out.append({
            "block": name, "n_fit": int(mask.sum()), "n_held_out": int(held.sum()),
            "coefficients": {f: round(float(c), 4) for f, c in zip(features, coef[:-1])},
            "intercept": round(float(coef[-1]), 3),
            "held_out_rmse": round(float(np.sqrt((resid ** 2).mean())), 2),
            "held_out_sd_of_target": round(float(y[held].std(ddof=1)), 2),
            # 1 - (rmse/sd)^2 on the held-out block. Negative means the fit
            # predicts the other half of the data worse than its own mean does,
            # which no amount of sign agreement rescues.
            "variance_explained": round(
                float(1.0 - (resid ** 2).mean() / max(y[held].var(ddof=1), 1e-9)), 3),
        })
    signs = [{f: float(np.sign(b["coefficients"][f])) for f in features} for b in out]
    agree = ({f: len({s[f] for s in signs}) == 1 for f in features}
             if len(signs) == 2 else {})
    all_agree = bool(agree) and all(agree.values())
    beats_mean = bool(out) and all(b["variance_explained"] > 0 for b in out)
    return {
        "blocks": out, "usable": len(out) == 2,
        "coefficient_signs_agree": agree,
        "all_signs_agree": all_agree,
        "beats_its_own_mean_out_of_sample": beats_mean,
        # The one field a caller should branch on. Two ways to fail, and both
        # mean the same thing about the answer: the number is an order, not a
        # quantity of points.
        "margin_units": "points" if (all_agree and beats_mean) else "ordinal",
        "spread": ({f: round(abs(out[0]["coefficients"][f] - out[1]["coefficients"][f]), 4)
                    for f in features} if len(out) == 2 else {}),
        "note": "two disjoint blocks of the same data. A margin ships in points "
                "only if every coefficient keeps its sign across both blocks AND "
                "each block beats its own mean on the other's data. Sign "
                "agreement alone is not enough: a fit can agree with itself and "
                "still predict worse than guessing the average, which is what "
                "kickers do here.",
    }


def dst_calibration_frame(seasons: list[int], bands: dict) -> pd.DataFrame:
    """Actual D/ST points beside the matchup facts that were knowable before the
    game, which is what a calibration needs on both sides."""
    actual = dst_weekly_points(seasons, bands)
    if actual.empty:
        return pd.DataFrame()
    tw = _team_week(seasons)
    sch = sources.schedules()
    sch = sch[sch["season"].isin(seasons)]
    line = {}
    for _, g in sch.iterrows():
        total, spread = g.get("total_line"), g.get("spread_line")
        if pd.isna(total):
            continue
        half, edge = float(total) / 2, (float(spread) / 2 if pd.notna(spread) else 0.0)
        key = (int(g["season"]), int(g["week"]))
        line[(*key, str(g["home_team"]))] = (half - edge, True)
        line[(*key, str(g["away_team"]))] = (half + edge, False)
    rows = []
    for _, r in actual.iterrows():
        key = (int(r["season"]), int(r["week"]), str(r["team"]))
        implied, home = line.get(key, (None, None))
        rows.append({**r.to_dict(),
                     "opponent_implied_points": implied,
                     "home": (1.0 if home else 0.0) if home is not None else None})
    frame = pd.DataFrame(rows)
    # Opponent offensive form as it stood before that week, never including it.
    form_by_week: dict[tuple[int, int], dict[str, float]] = {}
    # The distinct weeks, not the groups: only the key is wanted, and a
    # groupby key is typed as an opaque Hashable.
    for season, week in sorted({(int(s), int(w))
                               for s, w in zip(frame["season"], frame["week"])}):
        form_by_week[(season, week)] = _form(
            tw.rename(columns={"off_yards": "points"}), "points", season, week)
    frame["opponent_offense_yards_form"] = [
        form_by_week[(int(s), int(w))].get(str(o))
        for s, w, o in zip(frame["season"], frame["week"], frame["opponent"])]
    return frame


def _implied_and_roof(seasons: list[int]) -> dict[tuple[int, int, str], tuple[float, float, str]]:
    """(season, week, team) -> (that team's implied points, home flag, roof)."""
    sch = sources.schedules()
    sch = sch[sch["season"].isin(seasons)]
    out: dict[tuple[int, int, str], tuple[float, float, str]] = {}
    for _, g in sch.iterrows():
        total, spread = g.get("total_line"), g.get("spread_line")
        if pd.isna(total):
            continue
        half, edge = float(total) / 2, (float(spread) / 2 if pd.notna(spread) else 0.0)
        roof = str(g["roof"]) if pd.notna(g.get("roof")) else ""
        key = (int(g["season"]), int(g["week"]))
        out[(*key, str(g["home_team"]))] = (half + edge, 1.0, roof)
        out[(*key, str(g["away_team"]))] = (half - edge, 0.0, roof)
    return out


def k_calibration_frame(seasons: list[int], items: dict) -> pd.DataFrame:
    """Actual kicker points beside what was knowable before kickoff. A kicker
    scores when his own offence moves the ball and then stops, so the feature is
    his team's implied points rather than the opponent's."""
    actual = k_weekly_points(seasons, items)
    if actual.empty:
        return pd.DataFrame()
    lines = _implied_and_roof(seasons)
    tw = _team_week(seasons)
    rows = []
    for _, r in actual.iterrows():
        key = (int(r["season"]), int(r["week"]), str(r["team"]))
        implied, home, roof = lines.get(key, (None, None, ""))
        rows.append({**r.to_dict(), "team_implied_points": implied, "home": home,
                     "dome": 1.0 if roof in ("dome", "closed") else 0.0})
    frame = pd.DataFrame(rows)
    form_by_week: dict[tuple[int, int], dict[str, float]] = {}
    # The distinct weeks, not the groups: only the key is wanted, and a
    # groupby key is typed as an opaque Hashable.
    for season, week in sorted({(int(s), int(w))
                               for s, w in zip(frame["season"], frame["week"])}):
        form_by_week[(season, week)] = _form(
            tw.rename(columns={"off_yards": "points"}), "points", season, week)
    frame["own_offense_yards_form"] = [
        form_by_week[(int(s), int(w))].get(str(t))
        for s, w, t in zip(frame["season"], frame["week"], frame["team"])]
    return frame


def fit_position(frame, features, target="points"):
    """Coefficients fitted on all the history, and the block report that says
    whether the number they produce is points or an order."""
    report = calibration_blocks(frame, features, target)
    clean = frame.dropna(subset=[*features, target]) if len(frame) else frame
    if len(clean) == 0:
        return None, report
    coef = _fit(clean[list(features)].to_numpy(dtype=float),
                clean[target].to_numpy(dtype=float))
    return coef, report


def _predict(row: dict, features: tuple[str, ...], coef) -> float | None:
    if coef is None:
        return None
    values = [row.get(f) for f in features]
    if any(v is None or (isinstance(v, float) and np.isnan(v)) for v in values):
        return None
    return float(np.dot(values, coef[:-1]) + coef[-1])


def rank_week(season: int, week: int, bands: dict, items: dict,
              available: dict[str, list[str]], history_seasons: list[int],
              starters: dict[str, str] | None = None,
              team_of: dict[str, str] | None = None) -> dict:
    """Rank the available defences and kickers for one week by that week's
    matchup, and say in what units the margin is expressed.

    `available` maps "DST"/"K" to what could be picked up -- team abbreviations
    for defences, player names for kickers, with `team_of` giving each kicker's
    team. `starters` names what is already started at each position, so the
    margin is against the real alternative rather than against the field.
    """
    starters = starters or {}
    team_of = team_of or {}
    table = matchup_table(season, week, bands, items, history_seasons)
    by_team = {r["team"]: r for r in table["teams"]}

    dst_coef, dst_report = fit_position(dst_calibration_frame(history_seasons, bands),
                                        DST_FEATURES)
    k_coef, k_report = fit_position(k_calibration_frame(history_seasons, items),
                                    K_FEATURES)

    out: dict = {"season": season, "week": week,
                 "line_coverage": table["line_coverage"],
                 "weather": table["weather"], "positions": {}}
    for pos, features, coef, report in (("DST", DST_FEATURES, dst_coef, dst_report),
                                        ("K", K_FEATURES, k_coef, k_report)):
        rows: list[dict] = []
        for name in available.get(pos, []):
            team = name if pos == "DST" else team_of.get(name, name)
            m = by_team.get(team)
            if m is None:
                rows.append({"name": name, "team": team, "playing": False,
                             "score": None, "line_basis": "no game this week"})
                continue
            feed = dict(m)
            feed["team_implied_points"] = (
                None if m["total_line"] is None
                else round(float(m["total_line"]) / 2
                           + (float(m["spread_line"]) / 2
                              if m["spread_line"] is not None else 0.0)
                           * (1 if m["home"] else -1), 1))
            rows.append({
                "name": name, "team": team, "opponent": m["opponent"],
                "home": m["home"], "roof": m["roof"], "playing": True,
                "opponent_implied_points": m["opponent_implied_points"],
                "team_implied_points": feed["team_implied_points"],
                "line_basis": m["line_basis"],
                "score": _predict(feed, features, coef),
            })
        scored: list[dict] = [r for r in rows if r["score"] is not None]
        scored.sort(key=lambda r: float(r["score"]), reverse=True)
        for i, r in enumerate(scored, start=1):
            r["rank"] = i
        units = report.get("margin_units", "ordinal")
        held = starters.get(pos)
        base = next((r["score"] for r in scored if r["name"] == held), None)
        for r in scored:
            if base is None or units != "points":
                r["margin_over_your_starter"] = None
            else:
                r["margin_over_your_starter"] = round(float(r["score"]) - float(base), 2)
        out["positions"][pos] = {
            "margin_units": units,
            "your_starter": held,
            "calibration": report,
            "unrankable": [r for r in rows if r["score"] is None],
            "ranked": scored,
            "note": ("margins are fantasy points under this league's own bands"
                     if units == "points" else
                     "ordinal only: the fit for this position does not beat its own "
                     "mean out of sample, so the order is usable and a points "
                     "margin would not be"),
        }
    return out


def stream_kdst(season: int, week: int, bands: dict, items: dict,
                available: dict[str, list[str]], history_seasons: list[int],
                starters: dict[str, str] | None = None,
                team_of: dict[str, str] | None = None,
                look_ahead: int = LOOK_AHEAD) -> dict:
    """This week's kicker and defence pickups, plus the following weeks, so a
    waiver claim can be judged against the bye it has to cover."""
    weeks = [rank_week(season, week + i, bands, items, available, history_seasons,
                       starters, team_of) for i in range(look_ahead)]
    return {
        "season": season, "week": week, "weeks_covered": [w["week"] for w in weeks],
        "this_week": weeks[0],
        "look_ahead": weeks[1:],
        "not_the_draft_model": "ranked by this week's matchup only. The draft-time "
                               "counting model answers whether a required position "
                               "can still be filled at all and is not consulted here; "
                               "neither is any season projection.",
    }
