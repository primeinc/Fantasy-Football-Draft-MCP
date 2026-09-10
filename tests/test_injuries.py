"""ESPN's dated injury feed, parsed and joined to a roster, and the tool over it.

2026-09-09: a web search's synthesized answer said Christian Watson was
questionable for week 1, citing the Packers' site; the team's report dated
that day did not list him. A status without a date moved nothing after that,
and this is the surface that carries the date.
"""
import json

import pandas as pd

from ffdraft import injuries, server
from ffdraft.board import norm_name

FEED = {
    "timestamp": "2026-09-10T00:36:49Z",
    "injuries": [
        {"id": "9", "displayName": "Green Bay Packers", "injuries": [
            {"status": "Active", "date": "2026-09-03T17:41Z",
             "athlete": {"displayName": "Christian Watson"},
             "shortComment": "Watson is one of six receivers on the 53-man roster.",
             "details": {"type": None, "fantasyStatus": None}},
            {"status": "Active", "date": "2026-08-01T00:00Z",
             "athlete": {"displayName": "Christian Watson"},
             "shortComment": "older note", "details": {}},
        ]},
        {"id": "30", "displayName": "Jacksonville Jaguars", "injuries": [
            {"status": "Questionable", "date": "2026-09-09T16:28Z",
             "athlete": {"displayName": "Jakobi Meyers"},
             "shortComment": "Meyers (hand) practised in a non-contact jersey.",
             "details": {"type": "Thumb", "returnDate": "2026-09-13",
                         "fantasyStatus": {"abbreviation": "QUESTIONABLE"}}},
        ]},
        {"id": "17", "displayName": "New England Patriots", "injuries": [
            {"status": "Out", "date": "2026-09-09T22:54Z",
             "athlete": {"displayName": "TreVeyon Henderson"},
             "shortComment": "inactive",
             "details": {"type": "Ankle", "fantasyStatus": {"abbreviation": "INACTIVE"}}},
        ]},
    ],
}


def _roster():
    rows = [("Christian Watson", "GB", "ACTIVE"), ("Jakobi Meyers", "JAX", "QUESTIONABLE"),
            ("TreVeyon Henderson", "NE", "ACTIVE"), ("Trey McBride", "ARI", "ACTIVE"),
            ("MarShawn Lloyd", None, "ACTIVE")]
    return pd.DataFrame([{"name": n, "_key": norm_name(n), "team": t, "espn_injury": s}
                         for n, t, s in rows])


class TestParse:
    def test_every_entry_is_a_dated_row_on_the_boards_team_abbreviation(self):
        feed, stamp = injuries.parse_injuries(FEED)
        assert stamp == "2026-09-10T00:36:49Z"
        assert list(feed.columns) == list(injuries.COLUMNS)
        assert sorted(feed["team"].unique()) == ["GB", "JAX", "NE"]
        meyers = feed[feed["name"] == "Jakobi Meyers"].iloc[0]
        assert (meyers["fantasy_status"], meyers["injury"], meyers["date"]) == (
            "QUESTIONABLE", "Thumb", "2026-09-09T16:28Z")

    def test_an_empty_feed_is_an_empty_frame_with_the_columns(self):
        feed, stamp = injuries.parse_injuries({})
        assert feed.empty and list(feed.columns) == list(injuries.COLUMNS) and stamp is None


class TestJoin:
    def test_each_player_gets_his_newest_entry_or_an_explicit_absence(self):
        feed, _ = injuries.parse_injuries(FEED)
        rows = {r["player"]: r for r in injuries.for_roster(feed, _roster())}
        # Watson: two entries, the newer one wins, and it carries no fantasy
        # status, so agreement is undecidable rather than false.
        assert rows["Christian Watson"]["as_of"] == "2026-09-03T17:41Z"
        assert rows["Christian Watson"]["agrees_with_roster"] is None
        assert rows["Jakobi Meyers"]["agrees_with_roster"] is True
        # Official inactive in the feed, roster still says ACTIVE: the row to act on.
        assert rows["TreVeyon Henderson"]["feed_fantasy_status"] == "INACTIVE"
        assert rows["TreVeyon Henderson"]["agrees_with_roster"] is False
        assert rows["Trey McBride"] == {"player": "Trey McBride", "team": "ARI",
                                        "roster_status": "ACTIVE", "in_feed": False}

    def test_a_player_without_a_team_still_joins_on_his_name(self):
        feed, _ = injuries.parse_injuries({"injuries": [
            {"id": "9", "injuries": [{"status": "Active", "date": "2026-09-08T17:50Z",
                                      "athlete": {"displayName": "MarShawn Lloyd"},
                                      "shortComment": "x", "details": {}}]}]})
        rows = {r["player"]: r for r in injuries.for_roster(feed, _roster())}
        assert rows["MarShawn Lloyd"]["in_feed"] is True


class TestTheTool:
    def wire(self, monkeypatch, payload=FEED):
        monkeypatch.setattr(server, "_build_board", lambda: pd.DataFrame())
        monkeypatch.setattr(server, "_state", lambda: object())
        monkeypatch.setattr(server, "_my_roster",
                            lambda *a, **k: (_roster(), server.ROSTER_LIVE))
        monkeypatch.setattr(injuries, "fetch_injuries", lambda *a, **k: payload)

    def test_the_payload_names_the_date_the_basis_and_the_disagreements(self, monkeypatch):
        self.wire(monkeypatch)
        out = json.loads(server.injury_report("1", 1))
        assert out["feed_timestamp"] == "2026-09-10T00:36:49Z"
        assert out["basis"] == injuries.FEED_BASIS
        assert out["disagreements"] == ["TreVeyon Henderson"]
        assert len(out["players"]) == 5

    def test_an_unreadable_feed_is_an_error_not_the_roster_dressed_up(self, monkeypatch):
        self.wire(monkeypatch)
        monkeypatch.setattr(injuries, "fetch_injuries",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("403")))
        out = json.loads(server.injury_report("1", 1))
        assert "could not read ESPN's injury feed" in out["error"]
        assert "players" not in out
