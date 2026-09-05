"""Team-level context features: drive efficiency and red zone play-calling identity."""
import pandas as pd

from ffdraft.features import _redzone_identity_shift, _team_drive_efficiency


def _play(season, team, play_type, yardline_100, drive, fixed_drive_result, is_pass):
    return {
        "season": season, "posteam": team, "game_id": "g1", "play_type": play_type,
        "pass": 1 if is_pass else 0, "rush": 0 if is_pass else 1,
        "yardline_100": yardline_100, "drive": drive,
        "fixed_drive_result": fixed_drive_result,
    }


class TestTeamDriveEfficiency:
    def test_counts_drive_outcomes_once_per_drive_not_per_play(self):
        pbp = pd.DataFrame([
            _play(2025, "BUF", "pass", 50, 1, "Touchdown", True),
            _play(2025, "BUF", "run", 5, 1, "Touchdown", False),   # same drive, same result
            _play(2025, "BUF", "run", 60, 2, "Punt", False),
            _play(2025, "BUF", "pass", 40, 3, "Field goal", True),
        ])
        out = _team_drive_efficiency(pbp)
        row = out[(out["team"] == "BUF") & (out["season"] == 2025)].iloc[0]
        assert row["drives"] == 3
        assert row["pct_td"] == pytest_approx(100 / 3)
        assert row["pct_punt"] == pytest_approx(100 / 3)
        assert row["pct_fg"] == pytest_approx(100 / 3)

    def test_missing_column_returns_empty_frame_not_a_crash(self):
        pbp = pd.DataFrame([{"season": 2025, "posteam": "BUF", "drive": 1}])
        out = _team_drive_efficiency(pbp)
        assert out.empty
        assert "pct_td" in out.columns


class TestRedzoneIdentityShift:
    def test_run_heavy_redzone_team_shows_positive_shift(self):
        pbp = pd.DataFrame([
            _play(2025, "PHI", "pass", 50, 1, "Touchdown", True),
            _play(2025, "PHI", "pass", 45, 1, "Touchdown", True),
            _play(2025, "PHI", "run", 10, 1, "Touchdown", False),
            _play(2025, "PHI", "run", 5, 1, "Touchdown", False),
        ])
        out = _redzone_identity_shift(pbp)
        row = out[(out["team"] == "PHI") & (out["season"] == 2025)].iloc[0]
        assert row["neutral_pass_rate"] == 100.0
        assert row["rz_pass_rate"] == 0.0
        assert row["shift"] == pytest_approx(100.0)

    def test_flat_shift_team_shows_near_zero(self):
        pbp = pd.DataFrame([
            _play(2025, "ARI", "pass", 50, 1, "Touchdown", True),
            _play(2025, "ARI", "run", 45, 1, "Touchdown", False),
            _play(2025, "ARI", "pass", 10, 2, "Touchdown", True),
            _play(2025, "ARI", "run", 5, 2, "Touchdown", False),
        ])
        out = _redzone_identity_shift(pbp)
        row = out[(out["team"] == "ARI") & (out["season"] == 2025)].iloc[0]
        assert row["shift"] == pytest_approx(0.0)


def pytest_approx(x):
    import pytest
    return pytest.approx(x, abs=0.5)
