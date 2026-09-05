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
                            lambda _season, _superflex=False: pd.DataFrame())
        monkeypatch.setattr(board.requests, "get", lambda *_a, **_k: Resp())

        out = board.load_adp("ppr")
        assert out["source"].iloc[0] == "fantasypros_html"
        assert list(out["name"]) == ["Ja'Marr Chase", "Bijan Robinson"]
        assert list(out["adp"]) == [1.5, 2.2]


class TestRepriceCachedBoard:
    def test_consensus_board_is_repriced_with_espn_adp(self, monkeypatch, tmp_path):
        from ffdraft import server

        monkeypatch.setenv("ESPN_LEAGUE_ID", "1")
        monkeypatch.setenv("ESPN_SWID", "{A}")
        monkeypatch.setenv("ESPN_S2", "s")
        league, _ = server._settings()
        path = tmp_path / "board.parquet"
        stale = pd.DataFrame({"name": ["Jakobi Meyers"], "position": ["WR"], "team": ["JAX"],
                              "pos_rank": [20], "overall_rank": [60], "bye_week": [7],
                              "adp": [118.3], "adp_source": ["consensus"],
                              "adp_delta": [58.3], "adp_format": ["ppr"]})
        stale.to_parquet(path, index=False)
        monkeypatch.setattr(server, "_board_path", lambda _l: path)
        monkeypatch.setattr(board, "load_adp", lambda **_k: pd.DataFrame(
            {"name": ["Jakobi Meyers"], "adp": [104.5], "_key": ["jakobi meyers"],
             "source": ["espn_adp"]}))
        server._BOARDS.pop(league.cache_key(), None)

        b = server._build_board()
        row = b[b["name"] == "Jakobi Meyers"].iloc[0]
        assert row["adp_source"] == "espn" and row["adp"] == 104.5
        assert pd.read_parquet(path)["adp_source"].iloc[0] == "espn"
        server._BOARDS.pop(league.cache_key(), None)


class TestLeagueRules:
    def test_surfaces_first_party_settings_and_bye_topology(self, monkeypatch):
        from ffdraft import features

        settings = {
            "name": "TITLE LEAUGE ", "size": 16,
            "draftSettings": {"type": "SNAKE", "timePerSelection": 86400, "keeperCount": 0,
                              "isTradingEnabled": False},
            "rosterSettings": {"lineupSlotCounts": {"0": 1, "2": 2, "4": 2, "6": 1, "16": 1,
                                                    "17": 1, "20": 6, "21": 1, "23": 0},
                               "positionLimits": {"1": 4, "2": 8, "3": 8, "4": 3, "5": 3, "16": 3},
                               "lineupLocktimeType": "INDIVIDUAL_GAME", "moveLimit": -1},
            "scoringSettings": {"scoringType": "H2H_POINTS", "matchupTieRule": "NONE",
                                "playoffMatchupTieRule": "NONE", "homeTeamBonus": 0,
                                "scoringItems": [{"statId": 53, "points": 1.0},
                                                 {"statId": 4, "points": 4.0},
                                                 {"statId": 198, "points": 5.0},
                                                 {"statId": 99, "points": 0.0}]},
            "scheduleSettings": {"matchupPeriodCount": 14, "playoffTeamCount": 6,
                                 "playoffMatchupPeriodLength": 1, "playoffReseed": False,
                                 "playoffSeedingRule": "TOTAL_POINTS_SCORED",
                                 "divisions": [{"id": 0}],
                                 "matchupPeriods": {str(i): [i] for i in range(1, 18)}},
            "acquisitionSettings": {"acquisitionType": "WAIVERS_TRADITIONAL", "waiverHours": 24,
                                    "waiverProcessDays": ["MONDAY"], "waiverProcessHour": 11,
                                    "isUsingAcquisitionBudget": False, "acquisitionBudget": 100,
                                    "acquisitionLimit": -1, "matchupAcquisitionLimit": -1.0},
            "tradeSettings": {"max": -1, "revisionHours": 24, "vetoVotesRequired": 7,
                              "deadlineDate": 1796230800000},
        }

        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"settings": settings, "teams": []}

        monkeypatch.setattr(board.requests, "get", lambda *_a, **_k: Resp())
        monkeypatch.setattr(features, "team_bye_weeks", lambda _s: {
            "GB": 11, "NE": 11, "MIN": 6, "CIN": 6, "JAX": 7, "ARI": 14})

        r = board.espn_league_rules("1", 2026, swid="{A}", espn_s2="s")
        assert r["teams"] == 16 and r["draft"]["rounds"] == 14
        assert r["roster"]["starters"] == {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "DST": 1, "K": 1}
        assert r["roster"]["bench"] == 6 and r["roster"]["ir"] == 1
        assert r["roster"]["position_limits"]["QB"] == 4
        assert r["scoring"]["items"] == {"receptions": 1.0, "passing_tds": 4.0}
        assert r["scoring"]["other_items_by_stat_id"] == {"198": 5.0}
        assert r["schedule"]["playoff_weeks"] == [15, 16, 17]
        assert r["waivers"]["type"] == "WAIVERS_TRADITIONAL" and r["waivers"]["budget"] is None
        assert r["byes"]["teams_on_bye_by_week"] == {6: 2, 7: 1, 11: 2, 14: 1}
        assert r["byes"]["byes_in_playoffs"] == []


class TestEspnAdp:
    def test_parses_ownership_adp(self, monkeypatch):
        payload = {"players": [
            {"player": {"id": 4429795, "fullName": "Jahmyr Gibbs",
                        "ownership": {"averageDraftPosition": 1.32, "percentOwned": 99.9},
                        "draftRanksByRankType": {"PPR": {"rank": 1}}}},
            {"player": {"id": 3916433, "fullName": "Jakobi Meyers",
                        "ownership": {"averageDraftPosition": 118.4, "percentOwned": 80.0},
                        "draftRanksByRankType": {"PPR": {"rank": 101}}}},
            {"player": {"id": 1, "fullName": "No Adp", "ownership": {}}},
        ]}

        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return payload

        monkeypatch.setattr(board.requests, "get", lambda *_a, **_k: Resp())
        out = board.load_espn_adp("1", 2026, swid="{ABC}", espn_s2="s2")
        assert list(out["name"]) == ["Jahmyr Gibbs", "Jakobi Meyers"]
        assert list(out["adp"]) == [1.32, 118.4]
        assert list(out["espn_rank"]) == [1, 101]
        assert set(out["source"]) == {"espn_adp"}

    def test_attach_labels_espn_source(self):
        b = pd.DataFrame({"name": ["Jakobi Meyers", "Nobody"], "position": ["WR", "WR"],
                          "pos_rank": [20, 90], "overall_rank": [60, 300]})
        adp = pd.DataFrame({"name": ["Jakobi Meyers"], "adp": [118.4], "source": ["espn_adp"]})
        adp["_key"] = adp["name"].map(board.norm_name)
        out = board.attach_adp(b, adp)
        assert list(out["adp_source"]) == ["espn", "modelled"]
        assert out["adp"].iloc[0] == 118.4


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

        monkeypatch.setattr(board.requests, "get", lambda *_a, **_k: Resp())
        monkeypatch.setattr(board, "_id_crosswalk", lambda: pd.DataFrame(
            [{"gsis_id": "g1", "espn_id": "4429795", "full_name": "Jahmyr Gibbs", "position": "RB"},
             {"gsis_id": "g2", "espn_id": "4362628", "full_name": "Ja'Marr Chase", "position": "WR"}]))

        init = object()
        calls = {}

        def fetch_init(_league_id, _season, team_id, _swid, _espn_s2):
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
            {"overall": 1, "slot": 1, "name": "Jahmyr Gibbs", "position": "RB", "player_id": None},
            {"overall": 2, "slot": 4, "name": "Ja'Marr Chase", "position": "WR", "player_id": None},
            {"overall": 3, "slot": 4, "name": "Atlanta Falcons D/ST", "position": "DST",
             "player_id": None},
        ]

    def test_completed_draft_keeps_read_api_path(self, monkeypatch):
        league_json = {"draftDetail": {"drafted": True, "inProgress": False,
                                       "picks": [{"overallPickNumber": 1, "playerId": 4429795}]}}

        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return league_json

        monkeypatch.setattr(board.requests, "get", lambda *_a, **_k: Resp())
        monkeypatch.setattr(board, "_id_crosswalk", lambda: pd.DataFrame(
            [{"gsis_id": "g1", "espn_id": "4429795", "full_name": "Jahmyr Gibbs", "position": "RB"}]))
        picks = board.sync_espn("1", 2026, swid="{ABC}", espn_s2="s2")
        assert picks == [{"overall": 1, "slot": None, "name": "Jahmyr Gibbs", "position": "RB",
                          "player_id": None}]


class TestAuditState:
    def _state(self, tmp_path, monkeypatch, picks):
        from ffdraft.config import LeagueSettings

        monkeypatch.setattr(board, "STATE_DIR", tmp_path)
        st = board.DraftState(LeagueSettings(name="t", teams=16, draft_slot=4, rounds=14))
        for i, (name, slot) in enumerate(picks, start=1):
            st.record(name, i, slot)
        return st

    def _board(self):
        b = pd.DataFrame({"name": ["Jahmyr Gibbs", "Bijan Robinson", "A.J. Brown", "Ja'Marr Chase",
                                   "Kenny Gainwell"],
                          "position": ["RB", "RB", "WR", "WR", "RB"]})
        return board.rekey(b)

    def test_unmodelled_own_pick_still_counts_by_recorded_position(self, tmp_path, monkeypatch):
        # MarShawn Lloyd: on the user's roster, no modelled season, so no board row.
        # ESPN's crosswalk still knows he is a running back.
        from ffdraft.config import LeagueSettings

        monkeypatch.setattr(board, "STATE_DIR", tmp_path)
        st = board.DraftState(LeagueSettings(name="t", teams=16, draft_slot=4, rounds=14))
        st.record("Ja'Marr Chase", 4, 4, position="WR")
        st.record("MarShawn Lloyd", 29, 4, position="RB")
        assert st.my_roster(self._board()) == {"WR": 1, "RB": 1}

    def test_clean_state_passes(self, tmp_path, monkeypatch):
        st = self._state(tmp_path, monkeypatch, [("Jahmyr Gibbs", 1), ("Bijan Robinson", 2),
                                                 ("A.J. Brown", 3), ("Ja'Marr Chase", 4),
                                                 ("Atlanta Falcons D/ST", 5), ("Brandon Aubrey", 6)])
        out = board.audit_state(self._board(), st)
        assert out["ok"] and out["failures"] == []
        assert out["unresolved"] == 2 and "D/ST" in out["warnings"][0]

    def test_board_spelling_mismatch_fails(self, tmp_path, monkeypatch):
        # ESPN and nflverse say Kenneth; the board says Kenny. Recorded raw, the
        # pick never marks him taken. The audit must call that out by name.
        st = self._state(tmp_path, monkeypatch, [("Kenneth Gainwell", 1)])
        out = board.audit_state(self._board(), st)
        assert not out["ok"]
        assert "Kenneth Gainwell -> board has 'Kenny Gainwell'" in out["failures"][0]

    def test_stale_board_keys_fail(self, tmp_path, monkeypatch):
        st = self._state(tmp_path, monkeypatch, [("A.J. Brown", 1)])
        b = self._board()
        b.loc[b["name"] == "A.J. Brown", "_key"] = "a j brown"
        out = board.audit_state(b, st)
        assert not out["ok"] and "normaliser" in out["failures"][0]

    def test_my_picks_off_schedule_and_duplicates_fail(self, tmp_path, monkeypatch):
        st = self._state(tmp_path, monkeypatch, [("Jahmyr Gibbs", 1), ("Jahmyr Gibbs", 2),
                                                 ("A.J. Brown", 3), ("Ja'Marr Chase", 9)])
        out = board.audit_state(self._board(), st)
        joined = " ".join(out["failures"])
        assert "recorded twice" in joined and "scheduled picks" in joined

    def test_drafted_player_in_recommendations_fails(self, tmp_path, monkeypatch):
        st = self._state(tmp_path, monkeypatch, [("A.J. Brown", 1)])
        recs = board.rekey(pd.DataFrame({"name": ["A.J. Brown", "Jahmyr Gibbs"]}))
        out = board.audit_state(self._board(), st, recs)
        assert "drafted players in recommendations: ['A.J. Brown']" in out["failures"]


class TestResolveEspnId:
    def _board(self):
        return board.rekey(pd.DataFrame({"name": ["Jakobi Meyers", "DJ Moore"],
                                         "position": ["WR", "WR"]}))

    def test_board_player_via_crosswalk(self):
        espn_map = {"3916433": "Jakobi Meyers", "3953687": "Brandon Aubrey"}
        assert board.resolve_espn_id("Meyers", self._board(), espn_map) == (3916433, "Jakobi Meyers")

    def test_kicker_off_board_via_crosswalk(self):
        espn_map = {"3916433": "Jakobi Meyers", "3953687": "Brandon Aubrey"}
        assert board.resolve_espn_id("brandon aubrey", self._board(), espn_map) == (3953687, "Brandon Aubrey")

    def test_defense_by_city_nickname_or_dst(self):
        for q in ("Denver Broncos D/ST", "Broncos", "denver", "Denver DST"):
            assert board.resolve_espn_id(q, self._board(), {}) == (-16007, "Denver Broncos D/ST"), q

    def test_unknown_is_a_reason_not_a_crash(self):
        pid, why = board.resolve_espn_id("Nobody Real", self._board(), {})
        assert pid is None and "Nobody Real" in why


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
