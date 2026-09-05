"""Bye-week awareness: schedule -> team byes, and the stacking penalty in recommend()."""
from ffdraft import features, model, sources
from ffdraft.config import LeagueSettings

DataFrame = model.pd.DataFrame


def test_team_bye_weeks_from_schedule(monkeypatch):
    sched = DataFrame([
        {"season": 2026, "game_type": "REG", "week": 1, "home_team": "A", "away_team": "B"},
        {"season": 2026, "game_type": "REG", "week": 2, "home_team": "A", "away_team": "C"},
        {"season": 2026, "game_type": "REG", "week": 3, "home_team": "B", "away_team": "C"},
        {"season": 2026, "game_type": "POST", "week": 19, "home_team": "A", "away_team": "B"},
    ])
    monkeypatch.setattr(sources, "schedules", lambda: sched)
    assert features.team_bye_weeks(2026) == {"A": 3, "B": 2, "C": 1}


def _league():
    return LeagueSettings(name="t", teams=16, draft_slot=4, rounds=14,
                          starters={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 0,
                                    "K": 1, "DST": 1})


def _board():
    # Two receivers identical except for bye week; two players already held on bye 11.
    return DataFrame([
        {"name": "Stacked WR", "_key": "stacked wr", "position": "WR", "team": "GB",
         "adp": 110.0, "draft_score": 100.0, "bye_week": 11, "drafted": False},
        {"name": "Clear WR", "_key": "clear wr", "position": "WR", "team": "JAX",
         "adp": 110.0, "draft_score": 100.0, "bye_week": 7, "drafted": False},
        {"name": "Mine WR", "_key": "mine wr", "position": "WR", "team": "GB",
         "adp": 61.0, "draft_score": 120.0, "bye_week": 11, "drafted": True},
        {"name": "Mine RB", "_key": "mine rb", "position": "RB", "team": "NE",
         "adp": 68.0, "draft_score": 110.0, "bye_week": 11, "drafted": True},
    ])


def test_conflicts_are_named_even_at_zero_weight():
    board = _board()
    mine = board[board["drafted"]]
    recs = model.recommend(board, _league(), 125, 132, {"WR": 1, "RB": 1}, top_n=2,
                           mine=mine, bye_weight=0.0)
    by_name = recs.set_index("name")
    assert by_name.loc["Stacked WR", "bye_conflicts"] == "Mine WR, Mine RB"
    assert by_name.loc["Clear WR", "bye_conflicts"] == ""
    assert by_name.loc["Stacked WR", "pick_value"] == by_name.loc["Clear WR", "pick_value"]


def test_weight_penalises_stack_and_explains_it():
    board = _board()
    mine = board[board["drafted"]]
    recs = model.recommend(board, _league(), 125, 132, {"WR": 1, "RB": 1}, top_n=2,
                           mine=mine, bye_weight=0.08)
    assert recs["name"].iloc[0] == "Clear WR"
    stacked = recs.set_index("name").loc["Stacked WR"]
    # one same-position player (0.08) plus one other-position player (0.04)
    assert abs(stacked["bye_mult"] - 0.88) < 1e-9
    assert "bye week 11 stacks with Mine WR, Mine RB" in model.explain(stacked)


def test_no_bye_column_is_a_no_op():
    board = _board().drop(columns=["bye_week"])
    mine = board[board["drafted"]]
    recs = model.recommend(board, _league(), 125, 132, {"WR": 1, "RB": 1}, top_n=2,
                           mine=mine, bye_weight=0.08)
    assert list(recs["bye_mult"]) == [1.0, 1.0]
