"""`set_lineup` end to end: what the tool emits, not what a helper returns.

The invariant the task specifies is about the payload, so it is asserted at the
tool's exit. The network is replaced at the two functions that touch it, which
is why they were split out.
"""
import json
from typing import Any

import pandas as pd
import pytest

from ffdraft import board as bd
from ffdraft import lineup, rosters, server
from ffdraft.config import LeagueSettings


def _league(**kw: Any) -> LeagueSettings:
    base: dict[str, Any] = {
        "name": "t", "teams": 12, "draft_slot": 1, "rounds": 14,
        "starters": {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 0,
                     "K": 1, "DST": 1},
    }
    base.update(kw)
    return LeagueSettings(**base)


ROSTER = [
    ("QB One", "QB", 1, 240.0), ("RB One", "RB", 2, 260.0),
    ("RB Two", "RB", 2, 250.0), ("WR One", "WR", 3, 300.0),
    ("WR Two", "WR", 3, 290.0), ("WR Three", "WR", 3, 280.0),
    ("TE One", "TE", 4, 220.0), ("Kicker One", "K", 5, 120.0),
    ("DST One", "DST", 16, 110.0),
]


def _board():
    return pd.DataFrame([{
        "name": n, "_key": bd.norm_name(n), "position": p, "proj_points": v,
        "espn_id": str(1000 + i), "replacement_points": 100.0, "vor": v - 100.0,
        "draft_score": v - 100.0, "adj_ppg": v / 17.0, "exp_games": 17.0,
        "bye_week": 5, "espn_injury": None, "off_roster": False,
        "is_rookie": False,
    } for i, (n, p, _pid, v) in enumerate(ROSTER)])


def _entries(started=("WR One", "WR Two", "RB One", "RB Two", "QB One",
                      "TE One", "Kicker One", "DST One")):
    out = []
    for i, (name, _pos, pid, _v) in enumerate(ROSTER):
        out.append({"playerId": 1000 + i, "lineupSlotId":
                    2 if name in started else rosters.BENCH_SLOT,
                    "playerPoolEntry": {"player": {
                        "id": 1000 + i, "fullName": name,
                        "defaultPositionId": pid, "injuryStatus": None}}})
    return out


def _wire(monkeypatch, *, weekly=None, entries=None, board=None, league=None):
    monkeypatch.setattr(server, "_settings", lambda: (league or _league(), None))
    monkeypatch.setattr(server, "_build_board", lambda: board if board is not None
                        else _board())
    monkeypatch.setattr(rosters, "fetch_roster_teams",
                        lambda *a, **k: [{"id": 4, "owners": ["{ME}"],
                                          "roster": {"entries":
                                                     _entries() if entries is None
                                                     else entries}}])
    monkeypatch.setattr(rosters, "fetch_weekly_projections",
                        lambda *a, **k: weekly or {})
    monkeypatch.setenv("ESPN_SWID", "{ME}")


def _call(week=9):
    return json.loads(server.set_lineup("123", week))


class TestThePayload:
    def test_every_slot_the_league_starts_is_filled_and_named(self, monkeypatch):
        _wire(monkeypatch)
        out = _call()
        assert sorted(s["slot"] for s in out["lineup"]) == [
            "DST", "K", "QB", "RB", "RB", "TE", "WR", "WR"]
        assert out["unfilled_slots"] == {}

    def test_the_lowest_projected_players_still_start_when_only_they_can(
            self, monkeypatch):
        # The waiver defect's sibling: rank order would bench the kicker and the
        # defense, and they are the only ones who can fill their slots.
        _wire(monkeypatch)
        started = {s["player"] for s in _call()["lineup"]}
        assert {"Kicker One", "DST One", "TE One"} <= started

    def test_every_starter_carries_its_basis_and_a_why(self, monkeypatch):
        _wire(monkeypatch)
        for slot in _call()["lineup"]:
            assert slot["basis"]
            assert str(slot["week_points"]) in slot["why"] or "expected in" in slot["why"]
            assert slot["slot"] in slot["why"]

    def test_a_bye_benches_a_starter_and_the_why_says_so(self, monkeypatch):
        board = _board()
        board.loc[board["name"] == "WR One", "bye_week"] = 9
        _wire(monkeypatch, board=board)
        out = _call(week=9)
        assert "WR One" in {b["player"] for b in out["bench"]}
        assert "WR Three" in {s["player"] for s in out["lineup"]}
        benched = next(b for b in out["bench"] if b["player"] == "WR One")
        assert benched["basis"] == lineup.BASIS_BYE
        assert benched["week_points"] == 0.0

    def test_an_out_status_benches_a_starter(self, monkeypatch):
        entries = _entries()
        entries[3]["playerPoolEntry"]["player"]["injuryStatus"] = "OUT"
        _wire(monkeypatch, entries=entries)
        out = _call()
        assert "WR One" in {b["player"] for b in out["bench"]}

    def test_espn_weekly_projections_are_used_and_counted(self, monkeypatch):
        # WR Three is the worst receiver by season projection and the best this
        # week, so the weekly number has to be what decides the slot.
        _wire(monkeypatch, weekly={"1005": 40.0})
        out = _call()
        started = {s["player"] for s in out["lineup"]}
        assert "WR Three" in started
        assert out["espn_weekly_projections_seen"] == 1
        row = next(s for s in out["lineup"] if s["player"] == "WR Three")
        assert row["basis"] == lineup.BASIS_ESPN
        assert row["week_points"] == 40.0

    def test_alternatives_are_slot_eligible_and_never_positive(self, monkeypatch):
        _wire(monkeypatch)
        for slot in _call()["lineup"]:
            for alt in slot["alternatives"]:
                assert alt["costs"] <= 0, (slot["slot"], alt)

    def test_the_payload_is_strict_json_with_no_bare_nan(self, monkeypatch):
        board = _board()
        board.loc[board["name"] == "WR One", "adj_ppg"] = float("nan")
        _wire(monkeypatch, board=board)
        raw = server.set_lineup("123", 9)
        assert "NaN" not in raw
        json.loads(raw)


class TestVersusEspn:
    def test_an_identical_lineup_reports_no_gain(self, monkeypatch):
        _wire(monkeypatch)
        out = _call()["versus_espn"]
        assert out["espn_lineup_known"] is True
        assert out["start"] == [] and out["bench"] == []
        assert out["gain"] == 0.0

    def test_a_difference_is_reported_as_swaps_and_a_gain(self, monkeypatch):
        # ESPN starts the worst receiver; the tool starts the best.
        _wire(monkeypatch, entries=_entries(
            started=("WR Three", "WR Two", "RB One", "RB Two", "QB One",
                     "TE One", "Kicker One", "DST One")))
        out = _call()["versus_espn"]
        assert [s["player"] for s in out["start"]] == ["WR One"]
        assert [b["player"] for b in out["bench"]] == ["WR Three"]
        assert out["gain"] > 0

    def test_no_espn_lineup_says_so_rather_than_scoring_zero(self, monkeypatch):
        _wire(monkeypatch, entries=_entries(started=()))
        out = _call()["versus_espn"]
        assert out["espn_lineup_known"] is False
        assert out["gain"] is None


class TestFailures:
    def test_owning_no_team_is_an_error_payload_not_a_traceback(self, monkeypatch):
        _wire(monkeypatch)
        monkeypatch.setenv("ESPN_SWID", "{SOMEONE-ELSE}")
        assert "error" in _call()

    def test_an_empty_roster_is_a_named_refusal_not_a_zero_point_lineup(
            self, monkeypatch):
        # What running this against the live league actually produced before the
        # fix: lineup [], projected_points 0.0, every slot unfilled. None of it
        # false, and "0.0" is a number a reader can take for an answer. Rosters
        # are withheld until a draft completes, so this is the ordinary state
        # before the season rather than an edge case.
        _wire(monkeypatch, entries=[])
        out = _call()
        assert "error" in out
        assert "no roster" in out["error"]
        assert "projected_points" not in out

    def test_the_payload_says_how_the_roster_was_priced(self, monkeypatch):
        # projected_points sums several bases including zeros, so the total is
        # not readable without the mix. A slot filled by a man nothing could
        # price is invisible in unfilled_slots, because the slot is filled.
        _wire(monkeypatch, weekly={"1005": 40.0})
        by_basis = _call()["priced_by"]
        assert by_basis[lineup.BASIS_ESPN] == 1
        assert sum(by_basis.values()) == len(ROSTER)

    def test_a_failed_pull_is_an_error_payload(self, monkeypatch):
        _wire(monkeypatch)

        def boom(*a, **k):
            raise RuntimeError("espn said no")
        monkeypatch.setattr(rosters, "fetch_roster_teams", boom)
        out = _call()
        assert "espn said no" in out["error"]

    def test_a_row_the_board_cannot_place_is_named_not_started(self, monkeypatch):
        board = _board()
        board.loc[board["name"] == "TE One", "position"] = float("nan")
        _wire(monkeypatch, board=board)
        out = _call()
        assert "TE One" in out["unplaceable"]
        assert "TE One" not in {s["player"] for s in out["lineup"]}
        assert out["unfilled_slots"] == {"TE": 1}


@pytest.mark.parametrize("week", [1, 9, 14])
def test_the_week_is_carried_through_to_the_payload(monkeypatch, week):
    _wire(monkeypatch)
    assert _call(week)["week"] == week
