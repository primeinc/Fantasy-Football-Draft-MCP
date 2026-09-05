"""ESPN id crosswalk: tested offline with synthetic weekly_rosters data."""
import pandas as pd

import ffdraft
from ffdraft import board, sources


class TestIdCrosswalk:
    def test_prefers_row_with_espn_id_over_earlier_null_row(self, monkeypatch):
        # weekly_rosters has one row per player per week; espn_id/sleeper_id are
        # only populated in some of those snapshots. A player whose earliest row
        # happens to lack espn_id must still resolve to the ID a later row has --
        # this is what silently dropped Bijan Robinson, Jahmyr Gibbs and De'Von
        # Achane (~23% of a real draft) before the fix.
        rosters = pd.DataFrame([
            {"gsis_id": "00-0038542", "espn_id": None, "sleeper_id": "9999",
             "full_name": "Bijan Robinson", "position": "RB"},
            {"gsis_id": "00-0038542", "espn_id": "4430807", "sleeper_id": None,
             "full_name": "Bijan Robinson", "position": "RB"},
        ])
        monkeypatch.setattr(sources, "weekly_rosters", lambda: rosters)
        monkeypatch.setattr(sources, "players", lambda: pd.DataFrame())

        x = board._id_crosswalk().set_index("gsis_id")
        assert x.loc["00-0038542", "espn_id"] == "4430807"
        assert x.loc["00-0038542", "sleeper_id"] == "9999"

    def test_one_row_per_gsis_id(self, monkeypatch):
        rosters = pd.DataFrame([
            {"gsis_id": "00-0038542", "espn_id": None, "sleeper_id": None,
             "full_name": "Bijan Robinson", "position": "RB"},
            {"gsis_id": "00-0038542", "espn_id": "4430807", "sleeper_id": None,
             "full_name": "Bijan Robinson", "position": "RB"},
            {"gsis_id": "00-0038542", "espn_id": None, "sleeper_id": "9999",
             "full_name": "Bijan Robinson", "position": "RB"},
        ])
        monkeypatch.setattr(sources, "weekly_rosters", lambda: rosters)
        monkeypatch.setattr(sources, "players", lambda: pd.DataFrame())

        x = board._id_crosswalk()
        assert len(x) == 1

    def test_drops_players_with_no_gsis_id(self, monkeypatch):
        rosters = pd.DataFrame([
            {"gsis_id": None, "espn_id": "123", "sleeper_id": None,
             "full_name": "No Gsis Guy", "position": "WR"},
        ])
        monkeypatch.setattr(sources, "weekly_rosters", lambda: rosters)
        monkeypatch.setattr(sources, "players", lambda: pd.DataFrame())

        assert board._id_crosswalk().empty

    def test_rookies_come_from_the_players_table(self, monkeypatch):
        # 2026 draft picks Jeremiyah Love, Carnell Tate, Jadarian Price and KC
        # Concepcion resolved as ESPN#<id> in a live draft: weekly_rosters only
        # spans the lookback seasons, so the current rookie class has no row.
        rosters = pd.DataFrame([
            {"gsis_id": "00-0038542", "espn_id": "4430807", "sleeper_id": None,
             "full_name": "Bijan Robinson", "position": "RB"},
        ])
        players = pd.DataFrame([
            {"gsis_id": "00-0038542", "espn_id": 4430807.0, "display_name": "Bijan Robinson",
             "position": "RB"},
            {"gsis_id": "00-0041027", "espn_id": 4870808.0, "display_name": "Jeremiyah Love",
             "position": "RB"},
            {"gsis_id": None, "espn_id": 1.0, "display_name": "No Gsis", "position": "WR"},
        ])
        monkeypatch.setattr(sources, "weekly_rosters", lambda: rosters)
        monkeypatch.setattr(sources, "players", lambda: players)

        x = board._id_crosswalk().set_index("gsis_id")
        assert len(x) == 2
        assert x.loc["00-0041027", "full_name"] == "Jeremiyah Love"
        assert x.loc["00-0041027", "espn_id"] == "4870808"
        assert x.loc["00-0038542", "espn_id"] == "4430807"


class TestLoadAdpHtmlFallback:
    def test_parses_fantasypros_page_from_string(self, monkeypatch):
        # pandas 3 removed literal-HTML input to read_html; the page body must be
        # wrapped in a text buffer or lxml treats it as a file path and raises.
        from ffdraft import adp as adp_mod

        html = """<html><body><table id="data">
        <thead><tr><th>Rank</th><th>Player Team (Bye)</th><th>ESPN</th><th>AVG</th></tr></thead>
        <tbody>
        <tr><td>1</td><td>Ja'Marr Chase CIN (10)</td><td>1</td><td>1.5</td></tr>
        <tr><td>2</td><td>Bijan Robinson ATL (5)</td><td>2</td><td>2.2</td></tr>
        </tbody></table></body></html>"""

        class Resp:
            text = html

            def raise_for_status(self):
                pass

        monkeypatch.setattr(adp_mod, "preseason_ecr",
                            lambda season, superflex=False: pd.DataFrame())
        monkeypatch.setattr(board.requests, "get", lambda *a, **k: Resp())

        out = board.load_adp("ppr")
        assert out["source"].iloc[0] == "fantasypros_html"
        assert list(out["name"]) == ["Ja'Marr Chase", "Bijan Robinson"]
        assert list(out["adp"]) == [1.5, 2.2]


class TestSyncEspnLive:
    def test_in_progress_draft_uses_socket_snapshot(self, monkeypatch):
        import sys
        import types

        # What lm-api-reads returns mid-draft: inProgress, every pick playerId -1.
        league_json = {
            "teams": [{"id": 3, "owners": ["{ABC}"]}, {"id": 15, "owners": ["{DEF}"]}],
            "draftDetail": {"drafted": False, "inProgress": True,
                            "picks": [{"overallPickNumber": i, "playerId": -1} for i in range(1, 5)]},
        }

        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return league_json

        monkeypatch.setattr(board.requests, "get", lambda *a, **k: Resp())
        monkeypatch.setattr(board, "_id_crosswalk", lambda: pd.DataFrame(
            [{"gsis_id": "g1", "espn_id": "4429795", "full_name": "Jahmyr Gibbs", "position": "RB"},
             {"gsis_id": "g2", "espn_id": "4362628", "full_name": "Ja'Marr Chase", "position": "WR"}]))

        init = object()
        calls = {}

        def fetch_init(league_id, season, team_id, swid, espn_s2):
            calls["team_id"] = team_id
            return init, ["TOKEN x", "SELECTED 3 -16001 16"]

        fake = types.SimpleNamespace(
            fetch_init=fetch_init,
            slot_by_team=lambda _init: {15: 1, 3: 4},
            picks_from_init=lambda _init: [
                {"overall": 1, "team_id": 15, "player_id": 4429795, "slot_id": 2, "keeper": False},
                {"overall": 2, "team_id": 3, "player_id": 4362628, "slot_id": 4, "keeper": False},
            ])
        # `from . import espn_live` resolves the package attribute first and only
        # falls back to sys.modules when the submodule has never been imported.
        # Patching both keeps the fake in place regardless of collection order.
        monkeypatch.setitem(sys.modules, "ffdraft.espn_live", fake)
        monkeypatch.setattr(ffdraft, "espn_live", fake, raising=False)

        picks = board.sync_espn("1", 2026, swid="{ABC}", espn_s2="s2")
        assert calls["team_id"] == 3
        assert picks == [
            {"overall": 1, "slot": 1, "name": "Jahmyr Gibbs", "player_id": None},
            {"overall": 2, "slot": 4, "name": "Ja'Marr Chase", "player_id": None},
            {"overall": 3, "slot": 4, "name": "Atlanta Falcons D/ST", "player_id": None},
        ]

    def test_completed_draft_keeps_read_api_path(self, monkeypatch):
        league_json = {"draftDetail": {"drafted": True, "inProgress": False,
                                       "picks": [{"overallPickNumber": 1, "playerId": 4429795}]}}

        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return league_json

        monkeypatch.setattr(board.requests, "get", lambda *a, **k: Resp())
        monkeypatch.setattr(board, "_id_crosswalk", lambda: pd.DataFrame(
            [{"gsis_id": "g1", "espn_id": "4429795", "full_name": "Jahmyr Gibbs", "position": "RB"}]))
        picks = board.sync_espn("1", 2026, swid="{ABC}", espn_s2="s2")
        assert picks == [{"overall": 1, "slot": None, "name": "Jahmyr Gibbs", "player_id": None}]


class TestRekey:
    def test_stale_cached_keys_are_recomputed(self):
        # A board cached before the initials fix stored "a j brown"; live draft
        # state keys the same name "aj brown", so the pick never marked him taken.
        stale = pd.DataFrame({"name": ["A.J. Brown", "DJ Moore"], "_key": ["a j brown", "d j moore"]})
        fresh = board.rekey(stale)
        assert list(fresh["_key"]) == ["aj brown", "dj moore"]
        assert list(stale["_key"]) == ["a j brown", "d j moore"]


class TestParsePastedBoard:
    def test_keeps_dotted_initials(self):
        # A real 110-pick ESPN draft room paste lost A.J. Brown, T.J. Hockenson and
        # J.K. Dobbins, which shifted every later pick's overall number and
        # attributed the wrong players to the user's slot.
        text = "Drake London\nA.J. Brown\nT.J. Hockenson\nJ.K. Dobbins\nKyle Monangai"
        assert board.parse_pasted_board(text) == [
            "Drake London", "A.J. Brown", "T.J. Hockenson", "J.K. Dobbins", "Kyle Monangai",
        ]

    def test_strips_numbering_and_position_tags(self):
        text = "1. Jahmyr Gibbs - RB\n2) Bijan Robinson (RB)\nRound 1, Pick 3 - Jonathan Taylor"
        assert board.parse_pasted_board(text) == [
            "Jahmyr Gibbs", "Bijan Robinson", "Jonathan Taylor",
        ]
