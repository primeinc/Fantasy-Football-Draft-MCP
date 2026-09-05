"""#39. The headline and the per-row numbers behind it.

`who_should_i_pick` reported `survives_to_next_pick` 0.55 for Woody Marks under
the headline "Take Woody Marks", and that was narrated back as "he does not come
back" — the reverse of what 0.55 means. Two things were missing: the odds were
never said in words next to the pick, and "Take" was printed whatever the
numbers said, including at a later pick where the same player's marginal was
negative and the model's own arithmetic preferred waiting.

These exercise the tool, not just the helpers, because the invariant is about
what the tool emits.
"""
import json

import pandas as pd
import pytest

from ffdraft import board as bd
from ffdraft import server
from ffdraft.config import LeagueSettings, ModelWeights


def _board(rows):
    b = pd.DataFrame(rows)
    b["overall_rank"] = b["draft_score"].rank(ascending=False, method="min").astype(int)
    b["_key"] = b["name"].map(bd.norm_name)
    return b


def _row(name, pos, score, adp):
    return {"name": name, "position": pos, "team": "A", "proj_points": score,
            "draft_score": score, "adp": adp, "pos_rank": 1, "consistency": 0.5,
            "adj_ppg": score / 17.0, "bye_week": 7}


@pytest.fixture
def league():
    return LeagueSettings(name="t", teams=12, rounds=14, draft_slot=4,
                          starters={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 0,
                                    "K": 0, "DST": 0})


def _wire(monkeypatch, tmp_path, league, b, picks=()):
    monkeypatch.setattr(bd, "STATE_DIR", tmp_path)
    state = bd.DraftState(league)
    for name, overall, slot in picks:
        row = bd.match_player(name, b)
        state.record(row["name"] if row is not None else name, overall, slot,
                     position=(str(row["position"]) if row is not None else None))
    monkeypatch.setattr(server, "_settings", lambda: (league, ModelWeights()))
    monkeypatch.setattr(server, "_state", lambda: state)
    monkeypatch.setattr(server, "_build_board", lambda force=False: b)
    return state


class TestHeadlineUrgency:
    def _deep_position(self):
        # Two near-identical backs nobody is going to draft, so the best one
        # survives and the position still offers almost as much next turn:
        # likely available, and taking now buys almost nothing.
        return _board([
            _row("Safe Back", "RB", 100.0, 400.0),
            _row("Almost As Good", "RB", 99.0, 401.0),
            _row("Third Back", "RB", 98.0, 402.0),
            _row("A Receiver", "WR", 40.0, 403.0),
            _row("Another Receiver", "WR", 39.0, 404.0),
            _row("A Passer", "QB", 30.0, 405.0),
            _row("An End", "TE", 20.0, 406.0),
        ])

    def test_likely_available_with_nothing_to_gain_is_not_a_take(
            self, monkeypatch, tmp_path, league):
        b = self._deep_position()
        _wire(monkeypatch, tmp_path, league, b)
        out = json.loads(server.who_should_i_pick(limit=3))
        top = out["recommendations"][0]

        assert top["survival"] > 0.5
        assert top["marginal_now_vs_wait"] < server.model.NO_URGENCY_MARGINAL
        assert out["headline"].startswith("No urgency; best available is ")
        assert not out["headline"].startswith("Take ")
        # And the odds are stated in words, on the row, in the direction 0.55
        # actually means.
        assert "likely still there at" in top["why_now"]
        assert not top["why_now"].startswith("only ")

    def _boundary_position(self):
        """Backs whose ADPs sit just past the next turn, so the best one's
        survival lands between the gate's 0.5 and the comfortable 0.9+ of the
        fixture above. marge mutated the gate from `> 0.5` to `> 0.9` and this
        class did not notice, because its other fixture survives at ~1.0 either
        way — the boundary was resting on the model-level tests alone.
        """
        return _board([
            _row("Marginal Back", "RB", 100.0, 24.0),
            _row("Close Behind", "RB", 99.0, 25.0),
            _row("Also Close", "RB", 98.0, 26.0),
            _row("A Receiver", "WR", 40.0, 300.0),
            _row("Another Receiver", "WR", 39.0, 301.0),
            _row("A Passer", "QB", 30.0, 302.0),
            _row("An End", "TE", 20.0, 303.0),
        ])

    def test_the_gate_holds_at_the_boundary_not_just_far_above_it(
            self, monkeypatch, tmp_path, league):
        b = self._boundary_position()
        _wire(monkeypatch, tmp_path, league, b)
        out = json.loads(server.who_should_i_pick(limit=3))
        top = out["recommendations"][0]

        # The point of the fixture: strictly inside the band a 0.9 gate would
        # exclude, so moving the threshold breaks this test.
        assert 0.5 < top["survival"] < 0.9, top["survival"]
        assert top["marginal_now_vs_wait"] < server.model.NO_URGENCY_MARGINAL
        assert out["headline"].startswith("No urgency; best available is ")
        assert "likely still there at" in top["why_now"]

    def test_a_candidate_who_will_not_last_is_still_a_take(
            self, monkeypatch, tmp_path, league):
        # Same board, except the best back goes at the very top of the draft, so
        # he is gone long before the next turn.
        b = self._deep_position()
        b.loc[b["name"] == "Safe Back", "adp"] = 1.0
        _wire(monkeypatch, tmp_path, league, b)
        out = json.loads(server.who_should_i_pick(limit=3))
        top = out["recommendations"][0]

        assert top["survival"] < 0.5
        assert out["headline"].startswith("Take ")
        assert top["why_now"].startswith("only ")

    def test_every_row_carries_the_four_numbers_the_headline_rests_on(
            self, monkeypatch, tmp_path, league):
        b = self._deep_position()
        _wire(monkeypatch, tmp_path, league, b)
        out = json.loads(server.who_should_i_pick(limit=4))

        for row in out["recommendations"]:
            for key in ("value_now", "expected_best_at_next_pick",
                        "marginal_now_vs_wait", "survival", "why_now"):
                assert key in row, key
            # The decomposition has to add up: what taking now is worth, minus
            # what the position still offers, is what taking now buys. The
            # tolerance is one rounding step and is deliberate -- all three are
            # rounded independently, and `marginal_now_vs_wait` is kept faithful
            # to the unrounded number the gate compares rather than derived from
            # the two rounded components, so that the printed value can never
            # drift from the one that decided the headline.
            assert row["marginal_now_vs_wait"] == pytest.approx(
                row["value_now"] - row["expected_best_at_next_pick"], abs=0.11)


class TestUnpricedRosterSlot:
    """#40's second cause, surfaced where it applies. A pick the board cannot
    price still fills its slot in `my_roster`'s count, so `need_mult` sees the
    position as fuller than `roles.bench_values` does — the two halves of the
    model disagree about the same roster."""

    def test_a_pick_the_board_cannot_price_no_longer_splits_the_two_counts(
            self, monkeypatch, tmp_path, league):
        # This case USED to be the note's trigger and #40 closed it. `my_rows`
        # now gives a pick with no board row a stand-in at replacement level, so
        # the counted roster and the priced one agree and there is nothing to
        # report. The note going quiet here is the fix landing, not a regression.
        b = _board([
            _row("Real Back", "RB", 100.0, 400.0),
            _row("Other Back", "RB", 99.0, 401.0),
            _row("A Receiver", "WR", 40.0, 402.0),
            _row("Another Receiver", "WR", 39.0, 403.0),
            _row("A Passer", "QB", 30.0, 404.0),
            _row("An End", "TE", 20.0, 405.0),
        ])
        state = _wire(monkeypatch, tmp_path, league, b)
        state.record("Ghost Back", 4, league.draft_slot, position="RB")

        out = json.loads(server.who_should_i_pick(limit=4))
        assert out["roster_note"] is None
        assert all(r["roster_slot_note"] is None for r in out["recommendations"])

    def test_a_board_row_with_no_position_still_splits_them(
            self, monkeypatch, tmp_path, league):
        # What the note now catches, and the only thing that can still split the
        # two counts. `my_roster` reads the board's position first and falls back
        # to the recorded one when it is blank, while `my_rows` keeps the board
        # row as it stands. So a row the board carries WITHOUT a position is
        # counted at RB and priced at "", and the halves disagree again.
        #
        # CORRECTION. An earlier version of this comment said the NaN case was
        # "its twin" and that the two behave alike. They do not, and I had not
        # measured it: NaN is truthy, so `idx.get(...) or p.get("position")`
        # returns the NaN and never reaches the fallback. `my_roster` then
        # returns a float key in a dict[str, int], which is not a split -- it is
        # a crash in anything that sorts those keys, which the note does. otto
        # found it by re-running the enumeration I had written down instead of
        # reading it, and it is fixed separately at the three sites that shared
        # the idiom.
        #
        # The empty string is genuinely falsy and genuinely reaches the fallback,
        # so this case stands on its own and is the one pinned here.
        b = _board([
            _row("Real Back", "RB", 100.0, 400.0),
            _row("Other Back", "RB", 99.0, 401.0),
            _row("A Receiver", "WR", 40.0, 402.0),
            _row("Another Receiver", "WR", 39.0, 403.0),
            _row("A Passer", "QB", 30.0, 404.0),
            _row("An End", "TE", 20.0, 405.0),
            _row("Blank Back", "", 95.0, 406.0),
        ])
        state = _wire(monkeypatch, tmp_path, league, b)
        state.record("Blank Back", 4, league.draft_slot, position="RB")

        out = json.loads(server.who_should_i_pick(limit=4))
        assert out["roster_note"] is not None
        assert "RB: 1 counted, 0 priced" in out["roster_note"]
        # And it says the thing that is now true. The note used to blame a pick
        # the board cannot model, which #40 closed; what is left is a board that
        # carries the man and forgot to say what he plays.
        assert "records no position" in out["roster_note"]
        assert "cannot model" not in out["roster_note"]

    def test_a_fully_priced_roster_says_nothing(self, monkeypatch, tmp_path, league):
        b = _board([
            _row("Real Back", "RB", 100.0, 400.0),
            _row("Other Back", "RB", 99.0, 401.0),
            _row("A Receiver", "WR", 40.0, 402.0),
            _row("Another Receiver", "WR", 39.0, 403.0),
            _row("A Passer", "QB", 30.0, 404.0),
            _row("An End", "TE", 20.0, 405.0),
        ])
        state = _wire(monkeypatch, tmp_path, league, b)
        state.record("Real Back", 4, league.draft_slot, position="RB")

        out = json.loads(server.who_should_i_pick(limit=3))
        assert out["roster_note"] is None
        assert all(r["roster_slot_note"] is None for r in out["recommendations"])
