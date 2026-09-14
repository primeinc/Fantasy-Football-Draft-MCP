"""`waivers.role_change` on one fixture week: a breakout, a handcuff, a noise mover.

The breakout's role moved and stayed moved; the handcuff's role has not moved;
the noise mover put up one loud week on unchanged usage.
"""
import pandas as pd
import pytest

from ffdraft import waivers

SEASON = 2026
WEEK = 6


def weekly():
    """Weeks 1-6 for three players and the team totals they are shares of.

    Breakout: 2 targets a week through week 4, then 9. Handcuff: 3 carries a
    week throughout. Noise: 5 targets a week throughout, but 28 points in week 6.
    """
    rows = []
    for wk in range(1, WEEK + 1):
        late = wk >= WEEK - waivers.RECENT_WEEKS + 1
        rows.append({"player_id": "00-breakout", "player_display_name": "Breakout Guy",
                     "recent_team": "AAA", "week": wk, "targets": 9 if late else 2,
                     "carries": 0, "fantasy_points_ppr": 14.0 if late else 4.0})
        rows.append({"player_id": "00-handcuff", "player_display_name": "Handcuff Guy",
                     "recent_team": "BBB", "week": wk, "targets": 0, "carries": 3,
                     "fantasy_points_ppr": 3.0})
        rows.append({"player_id": "00-noise", "player_display_name": "Noise Guy",
                     "recent_team": "CCC", "week": wk, "targets": 5, "carries": 0,
                     "fantasy_points_ppr": 28.0 if wk == WEEK else 6.0})
        # Team-mates, so every share has a denominator that does not move.
        for team, tg, ca in (("AAA", 30, 20), ("BBB", 30, 20), ("CCC", 30, 20)):
            rows.append({"player_id": f"00-rest-{team}", "player_display_name": f"Rest {team}",
                         "recent_team": team, "week": wk, "targets": tg, "carries": ca,
                         "fantasy_points_ppr": 10.0})
    out = pd.DataFrame(rows)
    out["season"] = SEASON
    out["season_type"] = "REG"
    return out


def snaps():
    rows = []
    for wk in range(1, WEEK + 1):
        late = wk >= WEEK - waivers.RECENT_WEEKS + 1
        rows.append({"player": "Breakout Guy", "week": wk,
                     "offense_pct": 0.75 if late else 0.25})
        rows.append({"player": "Handcuff Guy", "week": wk, "offense_pct": 0.20})
        rows.append({"player": "Noise Guy", "week": wk, "offense_pct": 0.55})
    out = pd.DataFrame(rows)
    out["season"] = SEASON
    out["game_type"] = "REG"
    return out


def changes():
    return waivers.role_change(weekly(), snaps(), SEASON, WEEK).set_index("name")


class TestRoleChange:
    def test_the_breakout_moved_and_the_noise_mover_did_not(self):
        c = changes()
        assert c.loc["Breakout Guy", "role_change"] > 0.3
        # 28 points on unchanged usage is the case a points-based tool buys.
        assert c.loc["Noise Guy", "role_change"] == pytest.approx(0.0, abs=1e-9)
        assert c.loc["Handcuff Guy", "role_change"] == pytest.approx(0.0, abs=1e-9)

    def test_the_breakout_moved_on_every_component(self):
        c = changes().loc["Breakout Guy"]
        assert c["target_share_change"] > 0.15
        assert c["snap_share_change"] == pytest.approx(0.50)
        assert c["carry_share_change"] == pytest.approx(0.0)

    def test_the_noise_mover_scored_without_the_role_moving(self):
        c = changes().loc["Noise Guy"]
        # Points went up, shares did not. Both facts are reported.
        assert c["recent_points"] > c["prior_points"] / waivers.PRIOR_WEEKS
        assert c["target_share_change"] == pytest.approx(0.0)
        assert c["snap_share_change"] == pytest.approx(0.0)

    def test_every_role_change_row_carries_the_measured_result(self):
        ev = changes()["role_change_evidence"]
        assert (ev == waivers.ROLE_CHANGE_EVIDENCE).all()
        assert "NEGATIVE" in waivers.ROLE_CHANGE_EVIDENCE

    def test_a_player_with_no_prior_window_is_a_new_role_not_a_changed_one(self):
        w = weekly()
        w = w[~((w["player_id"] == "00-breakout") & (w["week"] < WEEK))]
        c = waivers.role_change(w, snaps(), SEASON, WEEK).set_index("name")
        assert c.loc["Breakout Guy", "prior_games"] == 0

    def test_a_player_who_has_not_played_recently_is_absent(self):
        w = weekly()
        w = w[~((w["player_id"] == "00-noise") & (w["week"] >= WEEK - 1))]
        assert "Noise Guy" not in set(waivers.role_change(w, snaps(), SEASON, WEEK)["name"])


class TestATradedPlayerIsOnePlayer:
    """A player traded mid-window is one row, not one per team (Cam Akers,
    2024; six players in week 10 alone)."""

    def weekly_with_a_trade(self):
        w = weekly()
        # The move must land INSIDE a window, not between two. A trade in the gap
        # gives him one team per window and no duplicate at all: the first
        # version of this fixture moved him at WEEK-1, which is the start of the
        # recent window, and both tests passed against the defect. The control
        # run is what found that, not the tests.
        moved = (w["player_display_name"] == "Noise Guy") & (w["week"] >= WEEK)
        w.loc[moved, "recent_team"] = "DDD"
        # The new team needs team-mates, or his share of it is 1.0 by default.
        extra = []
        for wk in range(1, WEEK + 1):
            extra.append({"player_id": "00-rest-DDD", "player_display_name": "Rest DDD",
                          "recent_team": "DDD", "week": wk, "targets": 30, "carries": 20,
                          "fantasy_points_ppr": 10.0, "season": SEASON,
                          "season_type": "REG"})
        return pd.concat([w, pd.DataFrame(extra)], ignore_index=True)

    def test_a_trade_does_not_split_him_in_two(self):
        c = waivers.role_change(self.weekly_with_a_trade(), snaps(), SEASON, WEEK)
        assert len(c) == c["player_id"].nunique(), "one row per player"
        assert (c["name"] == "Noise Guy").sum() == 1

    def test_a_week_he_missed_does_not_shrink_his_role(self):
        """The other half of the same fix: the denominator is now the team's
        totals in the weeks he PLAYED, not the whole window. Counting the weeks
        he was out put availability inside a measure of role, which is the one
        thing it must not contain."""
        w = weekly()
        out_week = w[(w["player_display_name"] == "Breakout Guy")
                     & (w["week"] == WEEK - 1)].index
        played = waivers.role_change(w, snaps(), SEASON, WEEK).set_index("name")
        missed = waivers.role_change(w.drop(index=out_week), snaps(), SEASON,
                                     WEEK).set_index("name")
        assert missed.loc["Breakout Guy", "recent_target_share"] == pytest.approx(
            played.loc["Breakout Guy", "recent_target_share"], abs=1e-9)
