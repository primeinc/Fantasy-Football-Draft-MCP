"""#43c. One team's draft against what the model would have taken.

The two things worth pinning are the ones the design measurement said the brief
assumed and the data does not support: as-of pricing exists only from the pick a
watch first connected at, so most rows are priced with today's board and every
row has to say which; and before week 1 there are no box scores, so the delta is
a projection and `your_pick_edge_actual` is null rather than zero.
"""
import json

import pandas as pd
import pytest

from ffdraft import board as bd
from ffdraft import replay, watch
from ffdraft.config import LeagueSettings


def _board():
    rows = []
    for pos, n, top in (("QB", 6, 300.0), ("RB", 12, 260.0),
                        ("WR", 12, 250.0), ("TE", 6, 180.0)):
        for i in range(n):
            rows.append({"name": f"{pos}{i}", "position": pos, "team": "A",
                         "proj_points": top - 5.0 * i, "draft_score": 90.0 - 5.0 * i,
                         "adp": 3.0 + 4.0 * i, "pos_rank": i + 1,
                         "consistency": 0.5, "adj_ppg": 10.0})
    b = pd.DataFrame(rows)
    b["overall_rank"] = b["draft_score"].rank(ascending=False, method="min").astype(int)
    b["_key"] = b["name"].map(bd.norm_name)
    return b


@pytest.fixture
def league():
    return LeagueSettings(name="t", teams=4, rounds=3, draft_slot=2,
                          starters={"QB": 1, "RB": 1, "WR": 1, "TE": 0, "FLEX": 0,
                                    "K": 0, "DST": 0})


@pytest.fixture
def state(monkeypatch, tmp_path, league):
    monkeypatch.setattr(bd, "STATE_DIR", tmp_path)
    monkeypatch.setattr(watch, "STATE_DIR", tmp_path)
    st = bd.DraftState(league)
    b = _board()
    # A full first two rounds; slot 2 picks at 2 and 7.
    order = ["RB0", "WR0", "RB1", "QB0", "WR1", "TE0", "RB2", "QB1"]
    for i, name in enumerate(order, start=1):
        row = bd.match_player(name, b)
        assert row is not None, name
        st.record(str(row["name"]), i, st.slot_for_pick(i),
                  position=str(row["position"]))
    return st


class TestRetrospective:
    def test_it_reviews_only_that_slots_picks_in_order(self, state, league):
        out = replay.draft_retrospective(_board(), state, league)
        assert out["slot"] == 2 and out["mine"] is True
        assert [r["pick"] for r in out["picks"]] == [2, 7]
        assert out["picks_reviewed"] == 2
        assert out["picks_in_the_draft"] == len(league.picks_for_slot(2))
        assert [r["took"] for r in out["picks"]] == ["WR0", "RB2"]

    def test_without_snapshots_every_row_is_priced_from_todays_board(self, state, league):
        out = replay.draft_retrospective(_board(), state, league)
        assert all(r["basis"] == "today's board" for r in out["picks"])
        assert all(r["model_pick_as_of"] is None for r in out["picks"])
        cov = out["as_of_coverage"]
        assert cov["rows_priced_from_a_snapshot"] == 0
        assert cov["rows_priced_from_todays_board"] == 2

    def test_a_snapshot_for_one_pick_prices_only_that_row_as_of(self, state, league):
        b = _board()
        # A watch that connected at pick 7 and no earlier, which is the live
        # shape: 2 of 9 of my picks covered, the rest impossible to price as of.
        watch.write_snapshot(b, set(), "lg", 7)
        out = replay.draft_retrospective(b, state, league,
                                         snapshots=watch.snapshot_dir("lg"))
        by_pick = {r["pick"]: r for r in out["picks"]}
        assert by_pick[7]["basis"] == "as-of snapshot"
        assert by_pick[2]["basis"] == "today's board"
        assert by_pick[7]["model_pick_as_of"] is not None
        assert by_pick[2]["model_pick_as_of"] is None
        cov = out["as_of_coverage"]
        assert (cov["rows_priced_from_a_snapshot"],
                cov["rows_priced_from_todays_board"]) == (1, 1)
        # The agreement block counts only the covered rows.
        assert out["as_of_agreement"]["of"] == 1

    def test_before_week_one_the_delta_is_a_projection_and_says_so(self, state, league):
        out = replay.draft_retrospective(_board(), state, league)
        assert "projected full-season points" in out["delta_basis"]
        assert "no box scores yet" in out["delta_basis"]
        # Null, not zero: zero would read as "these players scored nothing".
        assert all(r["your_pick_edge_actual"] is None for r in out["picks"])
        assert any(r["your_pick_edge"] is not None for r in out["picks"])

    def test_your_pick_edge_is_signed_so_that_up_is_good(self, state, league):
        out = replay.draft_retrospective(_board(), state, league)
        seen = 0
        for r in out["picks"]:
            if r["your_pick_edge"] is None:
                continue
            seen += 1
            # Your projection minus the model's: positive when your pick
            # projects more, which is the direction a reader assumes.
            assert r["your_pick_edge"] == pytest.approx(
                r["took_projection"] - r["model_pick_projection"], abs=0.11)
            if r["took_projection"] > r["model_pick_projection"]:
                assert r["your_pick_edge"] > 0
            elif r["took_projection"] < r["model_pick_projection"]:
                assert r["your_pick_edge"] < 0
        assert seen, "no priced row to check the sign on"

    def test_the_room_around_each_pick_flags_which_one_is_yours(self, state, league):
        out = replay.draft_retrospective(_board(), state, league, around=2)
        for r in out["picks"]:
            window = r["room_around"]
            mine = [q for q in window if q["yours"]]
            assert len(mine) == 1 and mine[0]["pick"] == r["pick"]
            assert [q["pick"] for q in window] == sorted(q["pick"] for q in window)
            # Never invents a pick that has not happened.
            assert all(q["pick"] <= len(state.picks) for q in window)

    def test_another_slot_can_be_reviewed_and_is_not_marked_yours(self, state, league):
        out = replay.draft_retrospective(_board(), state, league, slot=1)
        assert out["slot"] == 1 and out["mine"] is False
        assert [r["pick"] for r in out["picks"]] == [1, 8]


class TestSeasonPoints:
    def test_a_season_with_no_box_scores_returns_none(self, monkeypatch, league):
        from ffdraft import sources

        monkeypatch.setattr(sources, "weekly_stats",
                            lambda *_a, **_k: pd.DataFrame(columns=["season_type"]))
        assert replay.season_points(["Anyone"], 2026, league) is None

    def test_an_unavailable_source_is_none_rather_than_an_error(self, monkeypatch, league):
        from ffdraft import sources

        def boom(*_a, **_k):
            raise RuntimeError("404")

        monkeypatch.setattr(sources, "weekly_stats", boom)
        assert replay.season_points(["Anyone"], 2026, league) is None


class TestRetrospectiveTool:
    def test_the_tool_emits_json_and_names_its_basis(self, monkeypatch, tmp_path,
                                                     league, state):
        from ffdraft import server
        from ffdraft.config import ModelWeights

        monkeypatch.setattr(server, "_settings", lambda: (league, ModelWeights()))
        monkeypatch.setattr(server, "_state", lambda: state)
        monkeypatch.setattr(server, "_build_board", lambda force=False: _board())
        out = json.loads(server.draft_retrospective())
        assert out["picks_reviewed"] == 2
        assert "delta_basis" in out and "as_of_coverage" in out
        assert all("basis" in r for r in out["picks"])
