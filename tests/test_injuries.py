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


def _athlete(name, espn_id=None):
    a = {"displayName": name}
    if espn_id:
        a["links"] = [{"rel": ["playercard"],
                       "href": f"https://www.espn.com/nfl/player/_/id/{espn_id}/x"}]
    return a


FEED = {
    "timestamp": "2026-09-10T00:36:49Z",
    "injuries": [
        {"id": "9", "displayName": "Green Bay Packers", "injuries": [
            {"status": "Active", "date": "2026-09-03T17:41Z",
             "athlete": _athlete("Christian Watson", "4569000"),
             "shortComment": "Watson is one of six receivers on the 53-man roster.",
             "details": {"type": None, "fantasyStatus": None}},
            {"status": "Active", "date": "2026-08-01T00:00Z",
             "athlete": _athlete("Christian Watson", "4569000"),
             "shortComment": "older note", "details": {}},
        ]},
        {"id": "30", "displayName": "Jacksonville Jaguars", "injuries": [
            {"status": "Questionable", "date": "2026-09-09T16:28Z",
             # ESPN spells him differently from the roster; the id joins him.
             "athlete": _athlete("Jakobi Meyers Jr.", "3116000"),
             "shortComment": "Meyers (hand) practised in a non-contact jersey.",
             "details": {"type": "Thumb", "returnDate": "2026-09-13",
                         "fantasyStatus": {"abbreviation": "QUESTIONABLE"}}},
        ]},
        {"id": "17", "displayName": "New England Patriots", "injuries": [
            {"status": "Out", "date": "2026-09-09T22:54Z",
             "athlete": _athlete("TreVeyon Henderson"),   # no link, no id
             "shortComment": "inactive",
             "details": {"type": "Ankle", "fantasyStatus": {"abbreviation": "INACTIVE"}}},
        ]},
    ],
}


def _roster():
    rows = [("Christian Watson", "GB", "ACTIVE", "4569000"),
            ("Jakobi Meyers", "JAX", "QUESTIONABLE", "3116000"),
            ("TreVeyon Henderson", "NE", "ACTIVE", "4430000"),
            ("Trey McBride", "ARI", "ACTIVE", "4361000"),
            ("MarShawn Lloyd", None, "ACTIVE", "4685000"),
            ("Baltimore Ravens D/ST", "BAL", None, "-16033")]
    return pd.DataFrame([{"name": n, "_key": norm_name(n), "team": t, "espn_injury": s,
                          "espn_id": i} for n, t, s, i in rows])


class TestParse:
    def test_every_entry_is_a_dated_row_on_the_boards_team_abbreviation(self):
        feed, stamp = injuries.parse_injuries(FEED)
        assert stamp == "2026-09-10T00:36:49Z"
        assert list(feed.columns) == list(injuries.COLUMNS)
        assert sorted(feed["team"].unique()) == ["GB", "JAX", "NE"]
        meyers = feed[feed["espn_id"] == "3116000"].iloc[0]
        assert (meyers["fantasy_status"], meyers["injury"], meyers["date"]) == (
            "QUESTIONABLE", "Thumb", "2026-09-09T16:28Z")
        assert feed[feed["name"] == "TreVeyon Henderson"]["espn_id"].isna().all()

    def test_the_athlete_id_is_the_path_segment_after_id(self):
        assert injuries.athlete_id(_athlete("x", "4569173")) == "4569173"
        assert injuries.athlete_id({"links": [{"href": "https://www.espn.com/nfl/player/_/x"}]}) is None
        assert injuries.athlete_id({"links": [{"href": "sportscenter://x?uid=a:1"}]}) is None
        assert injuries.athlete_id({}) is None

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
        assert rows["Christian Watson"]["joined_by"] == injuries.JOINED_BY_ID
        # Spelled "Jakobi Meyers Jr." in the feed: the id joins him, the name would not.
        assert rows["Jakobi Meyers"]["joined_by"] == injuries.JOINED_BY_ID
        assert rows["Jakobi Meyers"]["agrees_with_roster"] is True
        # Official inactive in the feed, roster still says ACTIVE: the row to act
        # on -- and joined by name, because the feed entry carried no id, and
        # the row says so.
        assert rows["TreVeyon Henderson"]["feed_fantasy_status"] == "INACTIVE"
        assert rows["TreVeyon Henderson"]["agrees_with_roster"] is False
        assert rows["TreVeyon Henderson"]["joined_by"] == injuries.JOINED_BY_NAME
        assert rows["Trey McBride"] == {"player": "Trey McBride", "team": "ARI",
                                        "roster_status": "ACTIVE", "in_feed": False,
                                        "joined_by": injuries.NOT_JOINED}

    def test_no_status_is_not_active(self):
        # A defense carries no injuryStatus. Against a feed entry that says
        # INACTIVE the comparison is undecidable, not "ACTIVE vs INACTIVE".
        feed, _ = injuries.parse_injuries({"injuries": [
            {"id": "33", "injuries": [{"status": "Out", "date": "2026-09-13T16:00Z",
                                       "athlete": _athlete("Baltimore Ravens D/ST"),
                                       "shortComment": "x",
                                       "details": {"fantasyStatus": {"abbreviation": "INACTIVE"}}}]}]})
        rows = {r["player"]: r for r in injuries.for_roster(feed, _roster())}
        assert rows["Baltimore Ravens D/ST"]["roster_status"] is None
        assert rows["Baltimore Ravens D/ST"]["agrees_with_roster"] is None

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
        assert out["name_joined"] == ["TreVeyon Henderson"]
        assert out["status_unknown"] == ["Baltimore Ravens D/ST"]
        assert len(out["players"]) == 6

    def test_an_unreadable_feed_is_an_error_not_the_roster_dressed_up(self, monkeypatch):
        self.wire(monkeypatch)
        monkeypatch.setattr(injuries, "fetch_injuries",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("403")))
        out = json.loads(server.injury_report("1", 1))
        assert "could not read ESPN's injury feed" in out["error"]
        assert "players" not in out
