"""#46. Weekly K and D/ST streaming, on a fixture with a clear mismatch.

Everything here is synthetic: `sources.weekly_stats` and `sources.schedules` are
replaced, so no test touches the network or the shared cache. The league's own
bands are passed in, which is also how the real call works.
"""
import numpy as np
import pandas as pd
import pytest

from ffdraft import sources, stream

BANDS = {
    "points_allowed_0": 5.0, "points_allowed_1_6": 4.0, "points_allowed_7_13": 3.0,
    "points_allowed_14_17": 1.0, "points_allowed_28_34": -1.0,
    "yards_allowed_under_100": 5.0, "yards_allowed_100_199": 3.0,
    "yards_allowed_200_299": 2.0, "yards_allowed_400_449": -3.0,
    "sack": 1.0, "interception": 2.0, "fumble_recovery": 2.0, "safety": 2.0,
    "blocked_kick": 2.0, "int_return_td": 6.0,
}
ITEMS = {"fg_made_under_40": 3.0, "fg_made_40_49": 4.0, "fg_made_50_59": 5.0,
         "fg_made_60_plus": 6.0, "fg_missed": -1.0, "pat_made": 1.0}


def _weekly(rows):
    base = {"passing_yards": 0.0, "rushing_yards": 0.0, "def_sacks": 0.0,
            "def_interceptions": 0.0, "def_fumbles": 0.0, "def_safeties": 0.0,
            "def_tds": 0.0, "def_fg_blocks": 0.0, "def_pat_blocks": 0.0,
            "def_punt_blocks": 0.0, "pt_return_tds": 0.0, "position": "WR",
            "player_display_name": "", "opponent_team": "", "season_type": "REG",
            "fg_made_0_19": 0.0, "fg_made_20_29": 0.0, "fg_made_30_39": 0.0,
            "fg_made_40_49": 0.0, "fg_made_50_59": 0.0, "fg_made_60_": 0.0,
            "fg_missed": 0.0, "pat_made": 0.0}
    return pd.DataFrame([{**base, **r} for r in rows])


def _sched(rows):
    base = {"season": 2025, "roof": "outdoors", "total_line": np.nan,
            "spread_line": np.nan, "home_score": np.nan, "away_score": np.nan}
    return pd.DataFrame([{**base, **r} for r in rows])


class TestDstScoring:
    def test_a_shutout_scores_every_band_the_league_defines(self, monkeypatch):
        # HOME held AWAY to 0 points on 150 yards, with 3 sacks and 2 picks:
        # 5 (shutout) + 3 (100-199 yards) + 3x1 (sacks) + 2x2 (picks) = 15.
        monkeypatch.setattr(sources, "weekly_stats", lambda *_a, **_k: _weekly([
            {"season": 2025, "week": 1, "recent_team": "HOME", "def_sacks": 3.0,
             "def_interceptions": 2.0, "passing_yards": 200.0, "rushing_yards": 100.0},
            {"season": 2025, "week": 1, "recent_team": "AWAY",
             "passing_yards": 100.0, "rushing_yards": 50.0},
        ]))
        monkeypatch.setattr(sources, "schedules", lambda: _sched([
            {"week": 1, "home_team": "HOME", "away_team": "AWAY",
             "home_score": 24.0, "away_score": 0.0}]))
        out = stream.dst_weekly_points([2025], BANDS)
        home = out[out["team"] == "HOME"].iloc[0]
        assert home["points_allowed"] == 0.0
        assert home["yards_allowed"] == 150.0
        assert home["points"] == pytest.approx(15.0)

    def test_a_band_the_league_omits_scores_nothing(self, monkeypatch):
        # 18-27 points allowed is not in BANDS, so it contributes 0 rather than
        # falling into the 14-17 or 28-34 band next to it.
        monkeypatch.setattr(sources, "weekly_stats", lambda *_a, **_k: _weekly([
            {"season": 2025, "week": 1, "recent_team": "HOME"},
            {"season": 2025, "week": 1, "recent_team": "AWAY",
             "passing_yards": 150.0, "rushing_yards": 100.0},
        ]))
        monkeypatch.setattr(sources, "schedules", lambda: _sched([
            {"week": 1, "home_team": "HOME", "away_team": "AWAY",
             "home_score": 10.0, "away_score": 21.0}]))
        out = stream.dst_weekly_points([2025], BANDS)
        home = out[out["team"] == "HOME"].iloc[0]
        # 21 allowed -> no band; 250 yards -> 2.
        assert home["points"] == pytest.approx(2.0)


class TestKickerScoring:
    def test_distance_buckets_and_the_miss_penalty(self, monkeypatch):
        monkeypatch.setattr(sources, "weekly_stats", lambda *_a, **_k: _weekly([
            {"season": 2025, "week": 1, "recent_team": "HOME", "position": "K",
             "player_display_name": "A Kicker", "fg_made_30_39": 2.0,
             "fg_made_50_59": 1.0, "fg_missed": 1.0, "pat_made": 3.0},
        ]))
        out = stream.k_weekly_points([2025], ITEMS)
        # 2x3 + 5 - 1 + 3 = 13
        assert out.iloc[0]["points"] == pytest.approx(13.0)
        assert out.iloc[0]["player"] == "A Kicker"
        # One column named `team`, not two: weekly_stats has its own.
        assert list(out.columns).count("team") == 1
        assert isinstance(out.iloc[0]["team"], str)


class TestCalibrationUnits:
    def _frame(self, slope_odd, slope_even, noise=0.0, n=120):
        rng = np.random.default_rng(0)
        rows = []
        for i in range(n):
            week = 1 + (i % 12)
            x = float(rng.uniform(15, 30))
            slope = slope_odd if week % 2 == 1 else slope_even
            rows.append({"week": week, "feature": x,
                         "points": slope * x + rng.normal(0, noise)})
        return pd.DataFrame(rows)

    def test_a_sign_flip_between_blocks_ships_ordinal(self):
        out = stream.calibration_blocks(self._frame(1.0, -1.0), ("feature",))
        assert out["usable"] and out["all_signs_agree"] is False
        assert out["margin_units"] == "ordinal"

    def test_agreeing_signs_that_do_not_beat_the_mean_still_ship_ordinal(self):
        # The failure the sign rule alone does not catch, and the one kickers
        # actually exhibit: both blocks agree on the sign and neither predicts
        # the other any better than its own average does.
        rng = np.random.default_rng(1)
        rows = [{"week": 1 + (i % 12), "feature": float(rng.uniform(15, 30)),
                 "points": float(rng.normal(8, 5))} for i in range(200)]
        out = stream.calibration_blocks(pd.DataFrame(rows), ("feature",))
        assert out["usable"]
        assert out["beats_its_own_mean_out_of_sample"] is False
        assert out["margin_units"] == "ordinal"

    def test_a_real_signal_ships_points(self):
        out = stream.calibration_blocks(self._frame(1.0, 1.0, noise=1.0), ("feature",))
        assert out["all_signs_agree"] and out["beats_its_own_mean_out_of_sample"]
        assert out["margin_units"] == "points"
        assert all(b["variance_explained"] > 0 for b in out["blocks"])

    def test_too_little_data_is_refused_rather_than_fitted(self):
        out = stream.calibration_blocks(
            pd.DataFrame({"week": [1, 2], "feature": [1.0, 2.0], "points": [1.0, 2.0]}),
            ("feature",))
        assert out["usable"] is False and out["blocks"] == []


class TestRankWeek:
    @pytest.fixture(autouse=True)
    def _data(self, monkeypatch):
        """Two seasons: 2025 to fit on, 2026 week 1 to rank. The mismatch is
        deliberate and large -- SHUT faces an offence implied at 13 points,
        BOMB faces one implied at 31."""
        rng = np.random.default_rng(7)
        weekly, sched = [], []
        for week in range(1, 13):
            for home, away in (("SHUT", "WEAK"), ("BOMB", "STRONG")):
                for team, yards in ((home, 300.0), (away, 380.0 if away == "STRONG" else 200.0)):
                    weekly.append({"season": 2025, "week": week, "recent_team": team,
                                   "passing_yards": yards * 0.6, "rushing_yards": yards * 0.4,
                                   "def_sacks": float(rng.integers(1, 4)),
                                   "def_interceptions": float(rng.integers(0, 2))})
                low = home == "SHUT"
                sched.append({"season": 2025, "week": week, "home_team": home,
                              "away_team": away, "home_score": 24.0,
                              "away_score": 6.0 if low else 34.0,
                              "total_line": 38.0 if low else 54.0,
                              "spread_line": 7.0, "roof": "outdoors"})
        for week in (1, 2):
            sched.append({"season": 2026, "week": week, "home_team": "SHUT",
                          "away_team": "WEAK", "total_line": 36.0, "spread_line": 7.0,
                          "roof": "dome"})
            sched.append({"season": 2026, "week": week, "home_team": "BOMB",
                          "away_team": "STRONG", "total_line": 55.0,
                          "spread_line": -7.0, "roof": "outdoors"})
        monkeypatch.setattr(sources, "weekly_stats", lambda *_a, **_k: _weekly(weekly))
        monkeypatch.setattr(sources, "schedules", lambda: _sched(sched))

    def test_the_good_matchup_outranks_the_bad_one(self):
        out = stream.rank_week(2026, 1, BANDS, ITEMS,
                               {"DST": ["SHUT", "BOMB"]}, [2025])
        ranked = out["positions"]["DST"]["ranked"]
        assert [r["name"] for r in ranked] == ["SHUT", "BOMB"]
        assert ranked[0]["opponent_implied_points"] < ranked[1]["opponent_implied_points"]

    def test_every_row_says_whether_it_had_a_line(self):
        out = stream.rank_week(2026, 1, BANDS, ITEMS, {"DST": ["SHUT", "BOMB"]}, [2025])
        assert all(r["line_basis"] == "implied total"
                   for r in out["positions"]["DST"]["ranked"])
        assert out["line_coverage"]["rows_without"] == 0

    def test_a_team_on_bye_is_unrankable_not_ranked_last(self):
        out = stream.rank_week(2026, 1, BANDS, ITEMS,
                               {"DST": ["SHUT", "NOTPLAYING"]}, [2025])
        pos = out["positions"]["DST"]
        assert [r["name"] for r in pos["ranked"]] == ["SHUT"]
        assert [r["name"] for r in pos["unrankable"]] == ["NOTPLAYING"]
        assert pos["unrankable"][0]["line_basis"] == "no game this week"

    def test_the_margin_is_against_your_own_starter(self):
        out = stream.rank_week(2026, 1, BANDS, ITEMS, {"DST": ["SHUT", "BOMB"]},
                               [2025], starters={"DST": "BOMB"})
        ranked = {r["name"]: r for r in out["positions"]["DST"]["ranked"]}
        if out["positions"]["DST"]["margin_units"] == "points":
            assert ranked["BOMB"]["margin_over_your_starter"] == 0.0
            assert ranked["SHUT"]["margin_over_your_starter"] > 0

    def test_an_ordinal_position_reports_no_points_margin(self, monkeypatch):
        monkeypatch.setattr(stream, "calibration_blocks",
                            lambda *_a, **_k: {"margin_units": "ordinal", "blocks": []})
        out = stream.rank_week(2026, 1, BANDS, ITEMS, {"DST": ["SHUT", "BOMB"]},
                               [2025], starters={"DST": "BOMB"})
        pos = out["positions"]["DST"]
        assert pos["margin_units"] == "ordinal"
        assert all(r["margin_over_your_starter"] is None for r in pos["ranked"])
        assert "ordinal only" in pos["note"]

    def test_the_look_ahead_covers_the_next_weeks(self):
        out = stream.stream_kdst(2026, 1, BANDS, ITEMS, {"DST": ["SHUT", "BOMB"]},
                                 [2025], look_ahead=2)
        assert out["weeks_covered"] == [1, 2]
        assert out["this_week"]["week"] == 1
        assert [w["week"] for w in out["look_ahead"]] == [2]
        assert "counting model" in out["not_the_draft_model"]
