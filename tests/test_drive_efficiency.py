"""Drive counts are per game and drive number, not per drive number.

team_context reported 29 drives for HOU's 2025 season and 28 for KC's: the
groupby lacked game_id, so every game's drive 1 collapsed into one row and the
touchdown, field-goal and punt rates were computed on that.
"""
from __future__ import annotations

import pandas as pd

from ffdraft import features


def _pbp():
    rows = []
    for game, results in (("g1", ["Touchdown", "Punt", "Field goal"]),
                          ("g2", ["Punt", "Punt", "Touchdown"])):
        for drive, result in enumerate(results, start=1):
            for play in range(2):
                rows.append({"season": 2025, "week": 1, "game_id": game, "posteam": "HOU",
                             "drive": drive, "fixed_drive_result": result, "play": play})
    return pd.DataFrame(rows)


def test_drives_count_every_game_and_rates_follow():
    out = features._team_drive_efficiency(_pbp())
    row = out[out["team"] == "HOU"].iloc[0]
    assert row["drives"] == 6
    assert round(float(row["pct_td"]), 1) == round(100 * 2 / 6, 1)
    assert round(float(row["pct_punt"]), 1) == 50.0
