"""ESPN id crosswalk: tested offline with synthetic weekly_rosters data."""
import pandas as pd
import pytest

import ffdraft
from ffdraft import board, names, sources


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
        saved = pd.read_parquet(path)
        assert saved["adp_source"].iloc[0] == "espn"
        assert int(saved["key_version"].iloc[0]) == names.KEY_VERSION
        server._BOARDS.pop(league.cache_key(), None)

    def test_board_keyed_by_an_older_normaliser_is_rejoined(self, monkeypatch, tmp_path):
        from ffdraft import server

        monkeypatch.setenv("ESPN_LEAGUE_ID", "1")
        monkeypatch.setenv("ESPN_SWID", "{A}")
        monkeypatch.setenv("ESPN_S2", "s")
        league, _ = server._settings()
        path = tmp_path / "board.parquet"
        # Priced under the old key ("audric estim"), so ESPN never joined and
        # the synthetic fallback filled adp; espn_rank exists, adp_source is a
        # mix, and key_version is old.
        stale = pd.DataFrame({"name": ["Audric Estimé", "Jakobi Meyers"],
                              "position": ["RB", "WR"], "team": ["NO", "JAX"],
                              "pos_rank": [36, 20], "overall_rank": [125, 60],
                              "bye_week": [8, 7], "adp": [110.7, 122.4],
                              "adp_source": ["modelled", "espn"], "adp_delta": [0.0, 0.0],
                              "adp_format": ["ppr", "ppr"], "espn_rank": [None, 112],
                              "key_version": [names.KEY_VERSION - 1] * 2})
        stale.to_parquet(path, index=False)
        monkeypatch.setattr(server, "_board_path", lambda _l: path)
        monkeypatch.setattr(board, "load_adp", lambda **_k: pd.DataFrame(
            {"name": ["Audric Estime", "Jakobi Meyers"], "adp": [169.99, 122.4],
             "_key": ["audric estime", "jakobi meyers"], "espn_rank": [1392, 112],
             "source": ["espn_adp", "espn_adp"]}))
        server._BOARDS.pop(league.cache_key(), None)

        b = server._build_board().set_index("name")
        assert b.loc["Audric Estimé", "adp"] == 169.99
        assert b.loc["Audric Estimé", "adp_source"] == "espn"
        assert int(b.loc["Audric Estimé", "espn_rank"]) == 1392
        assert int(pd.read_parquet(path)["key_version"].iloc[0]) == names.KEY_VERSION
        server._BOARDS.pop(league.cache_key(), None)

    def test_board_joined_by_an_older_market_join_is_repriced(self, monkeypatch, tmp_path):
        from ffdraft import server

        monkeypatch.setenv("ESPN_LEAGUE_ID", "1")
        monkeypatch.setenv("ESPN_SWID", "{A}")
        monkeypatch.setenv("ESPN_S2", "s")
        league, _ = server._settings()
        path = tmp_path / "board.parquet"
        # Nothing else in the cache gate can fire: the key version is current,
        # adp_source is espn, espn_rank and adp_match are both present. Only
        # the market-join version is old, and what it left behind is the
        # collision -- a tight end wearing the ADP and rank of the running back
        # who shares his name.
        stale = pd.DataFrame({"name": ["Terry Case", "Terry Case"],
                              "position": ["TE", "RB"], "team": ["GB", "MIN"],
                              "pos_rank": [30, 40], "overall_rank": [200, 210],
                              "bye_week": [5, 6], "adp": [88.0, 88.0],
                              "adp_source": ["espn", "espn"],
                              "adp_match": ["exact", "exact"],
                              "adp_delta": [0.0, 0.0], "adp_format": ["ppr", "ppr"],
                              "espn_rank": [70, 70],
                              "key_version": [names.KEY_VERSION] * 2,
                              "market_join_version": [board.MARKET_JOIN_VERSION - 1] * 2})
        stale.to_parquet(path, index=False)
        monkeypatch.setattr(server, "_board_path", lambda _l: path)
        monkeypatch.setattr(board, "load_adp", lambda **_k: pd.DataFrame(
            {"name": ["Terry Case", "Terry Case"], "position": ["TE", "RB"],
             "adp": [140.0, 88.0], "_key": ["terry case", "terry case"],
             "espn_rank": [150, 70], "source": ["espn_adp", "espn_adp"]}))
        server._BOARDS.pop(league.cache_key(), None)

        b = server._build_board().set_index("position")
        assert b.loc["TE", "adp"] == 140.0 and b.loc["RB", "adp"] == 88.0
        assert int(b.loc["TE", "espn_rank"]) == 150
        saved = pd.read_parquet(path)
        assert int(saved["market_join_version"].iloc[0]) == board.MARKET_JOIN_VERSION
        server._BOARDS.pop(league.cache_key(), None)


class TestTeamStrength:
    def test_ranks_best_lineups_and_counts_open_slots(self, tmp_path, monkeypatch):
        from ffdraft.config import LeagueSettings

        monkeypatch.setattr(board, "STATE_DIR", tmp_path)
        league = LeagueSettings(name="t", teams=2, rounds=4, draft_slot=2,
                                starters={"QB": 1, "RB": 1, "WR": 1, "TE": 0, "FLEX": 0,
                                          "K": 0, "DST": 0})
        b = pd.DataFrame({"name": ["QB One", "RB One", "RB Two", "WR One", "WR Two"],
                          "position": ["QB", "RB", "RB", "WR", "WR"],
                          "proj_points": [300.0, 200.0, 150.0, 180.0, 120.0]})
        b["_key"] = b["name"].map(board.norm_name)
        st = board.DraftState(league)
        st.record("RB One", 1, 1)
        st.record("QB One", 2, 2)
        st.record("Some Kicker", 3, 2, position="K")
        st.record("RB Two", 4, 1)

        out = board.team_strength(b, st, {1: "Alpha", 2: "Beta"})
        assert out["team"].tolist() == ["Beta", "Alpha"]
        beta, alpha = out.iloc[0], out.iloc[1]
        assert beta["starters_proj"] == 300 and beta["open_starter_slots"] == 2
        assert beta["picks"] == 2 and bool(beta["mine"])
        assert alpha["starters_proj"] == 200 and alpha["bench_proj"] == 150
        assert alpha["open_starter_slots"] == 2 and not alpha["mine"]
        assert out["rank"].tolist() == [1, 2]


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
                                                 {"statId": 99, "points": 0.0,
                                                  "pointsOverrides": {"16": 1.0}},
                                                 {"statId": 128, "points": 0.0,
                                                  "pointsOverrides": {"16": 5.0}},
                                                 {"statId": 136, "points": 0.0,
                                                  "pointsOverrides": {"16": -7.0}},
                                                 {"statId": 131, "points": 0.0,
                                                  "pointsOverrides": {"16": 0.0}}]},
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
        assert r["scoring"]["kicker_and_dst_items"] == {"fg_made_50_59": 5.0}
        assert r["scoring"]["slot_overrides"] == {"DST": {
            "sack": 1.0, "yards_allowed_under_100": 5.0, "yards_allowed_550_plus": -7.0}}
        assert r["schedule"]["playoff_weeks"] == [15, 16, 17]
        assert r["waivers"]["type"] == "WAIVERS_TRADITIONAL" and r["waivers"]["budget"] is None
        assert r["byes"]["teams_on_bye_by_week"] == {6: 2, 7: 1, 11: 2, 14: 1}
        assert r["byes"]["byes_in_playoffs"] == []


class TestEspnAdp:
    def test_parses_ownership_adp(self, monkeypatch):
        payload = {"players": [
            {"player": {"id": 4429795, "fullName": "Jahmyr Gibbs",
                        "ownership": {"averageDraftPosition": 1.32, "percentOwned": 99.9},
                        "draftRanksByRankType": {"PPR": {"rank": 1}},
                        "injuryStatus": "ACTIVE",
                        "stats": [{"seasonId": 2025, "statSourceId": 0, "scoringPeriodId": 0,
                                   "appliedTotal": 350.0},
                                  {"seasonId": 2026, "statSourceId": 1, "scoringPeriodId": 1,
                                   "appliedTotal": 20.0},
                                  {"seasonId": 2026, "statSourceId": 1, "scoringPeriodId": 0,
                                   "appliedTotal": 301.5}]}},
            {"player": {"id": 3916433, "fullName": "Jakobi Meyers",
                        "ownership": {"averageDraftPosition": 118.4, "percentOwned": 80.0},
                        "draftRanksByRankType": {"PPR": {"rank": 101}},
                        "injuryStatus": "QUESTIONABLE"}},
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
        assert out["espn_proj"].iloc[0] == 301.5 and pd.isna(out["espn_proj"].iloc[1])
        assert list(out["espn_injury"]) == ["ACTIVE", "QUESTIONABLE"]

    def test_attach_labels_espn_source(self):
        b = pd.DataFrame({"name": ["Jakobi Meyers", "Nobody"], "position": ["WR", "WR"],
                          "pos_rank": [20, 90], "overall_rank": [60, 300],
                          "espn_proj": [1.0, 1.0]})
        adp = pd.DataFrame({"name": ["Jakobi Meyers"], "adp": [118.4], "source": ["espn_adp"],
                            "espn_proj": [181.9], "espn_injury": ["QUESTIONABLE"]})
        adp["_key"] = adp["name"].map(board.norm_name)
        out = board.attach_adp(b, adp)
        assert list(out["adp_source"]) == ["espn", "modelled"]
        assert out["adp"].iloc[0] == 118.4
        assert out["espn_proj"].iloc[0] == 181.9 and pd.isna(out["espn_proj"].iloc[1])
        assert out["espn_injury"].iloc[0] == "QUESTIONABLE"


class TestMarketJoin:
    def _adp(self):
        adp = pd.DataFrame({
            "name": ["Joshua Palmer", "Audric Estime", "Josh Allen", "Josh Allen", "Trey Palmer"],
            "position": ["WR", "RB", "QB", "LB", "WR"],
            "adp": [150.0, 169.99, 20.0, 400.0, 300.0],
            "espn_rank": [140, 1392, 15, 900, 500], "espn_proj": [120.0, None, 380.0, 5.0, 40.0],
            "espn_injury": ["ACTIVE"] * 5, "source": ["espn_adp"] * 5})
        adp["_key"] = adp["name"].map(board.norm_name)
        return adp

    def test_alias_and_accent_rows_join_at_the_same_position(self):
        b = pd.DataFrame({"name": ["Josh Palmer", "Audric Estimé", "Josh Allen", "Josh Palmr",
                                   "Tyler Palmer"],
                          "position": ["WR", "RB", "QB", "WR", "TE"],
                          "pos_rank": [50, 36, 1, 60, 30], "overall_rank": [200, 125, 10, 260, 250],
                          "proj_points": [110.0, 151.5, 400.0, 90.0, 60.0]})
        out = board.attach_adp(b, self._adp()).set_index("name")
        assert out.loc["Josh Palmer", "adp"] == 150.0
        assert out.loc["Josh Palmer", "adp_match"] == "alias"
        assert out.loc["Josh Palmer", "espn_rank"] == 140
        assert out.loc["Audric Estimé", "adp"] == 169.99
        assert out.loc["Audric Estimé", "adp_match"] == "exact"
        assert out.loc["Josh Allen", "adp"] == 20.0
        # A typo would only match fuzzily: never joined, stays synthetic.
        assert out.loc["Josh Palmr", "adp_source"] == "modelled"
        assert out.loc["Josh Palmr", "adp_match"] == "none"
        # Last name plus initial at another position ("T. Palmer" TE vs Trey
        # Palmer WR): not joined.
        assert out.loc["Tyler Palmer", "adp_source"] == "modelled"
        assert list(out["adp_source"]) == ["espn", "espn", "espn", "modelled", "modelled"]

    def test_same_name_at_two_positions_prices_each_from_its_own_row(self):
        # The market frame carries a QB and a linebacker called Josh Allen. A join
        # on the name key alone gives both board rows whichever came first.
        b = pd.DataFrame({"name": ["Josh Allen", "Josh Allen"],
                          "position": ["QB", "LB"],
                          "pos_rank": [1, 40], "overall_rank": [10, 500],
                          "proj_points": [400.0, 20.0]})
        out = board.attach_adp(b, self._adp()).set_index("position")
        assert out.loc["QB", "adp"] == 20.0
        assert out.loc["QB", "espn_proj"] == 380.0
        assert out.loc["LB", "adp"] == 400.0
        assert out.loc["LB", "espn_proj"] == 5.0
        assert list(out["adp_match"]) == ["exact", "exact"]

    def test_lone_market_row_still_prices_a_position_disagreement(self):
        # One "Trey Palmer" in the market, listed WR; the board calls him RB.
        # Nobody else can be picked by mistake, so he keeps his market price and
        # the report says the join crossed a position label.
        b = pd.DataFrame({"name": ["Trey Palmer"], "position": ["RB"],
                          "pos_rank": [40], "overall_rank": [300],
                          "proj_points": [80.0]})
        out = board.attach_adp(b, self._adp())
        assert out["adp"].iloc[0] == 300.0
        assert out["adp_match"].iloc[0] == "key_only"
        assert out["adp_source"].iloc[0] == "espn"
        rep = board.market_join_report(out)
        assert rep["key_only"] == [{"name": "Trey Palmer", "position": "RB", "adp": 300.0}]

    def test_alias_pass_survives_an_exact_pass_that_matched_nothing(self):
        # Only the alias index can price this board, so the exact merge leaves
        # espn_injury behind as an all-NaN float column. Writing a string into
        # it has to go through a whole-column assignment, not a cell at a time.
        b = pd.DataFrame({"name": ["Josh Palmer"], "position": ["WR"],
                          "pos_rank": [50], "overall_rank": [200],
                          "proj_points": [110.0]})
        adp = pd.DataFrame({"name": ["Joshua Palmer"], "position": ["WR"],
                            "adp": [150.0], "espn_rank": [140], "espn_proj": [120.0],
                            "espn_injury": ["QUESTIONABLE"], "source": ["espn_adp"]})
        adp["_key"] = adp["name"].map(board.norm_name)
        out = board.attach_adp(b, adp)
        assert out["adp_match"].iloc[0] == "alias"
        assert out["adp"].iloc[0] == 150.0
        assert out["espn_injury"].iloc[0] == "QUESTIONABLE"
        assert out["espn_proj"].iloc[0] == 120.0

    def test_ambiguous_key_at_an_unlisted_position_is_not_priced(self):
        # Josh Allen the tight end is neither of the two the market knows, and
        # guessing between them is exactly the collision this join avoids.
        b = pd.DataFrame({"name": ["Josh Allen"], "position": ["TE"],
                          "pos_rank": [30], "overall_rank": [250],
                          "proj_points": [60.0]})
        out = board.attach_adp(b, self._adp())
        assert out["adp_source"].iloc[0] == "modelled"
        assert out["adp_match"].iloc[0] == "none"

    def test_report_lists_unjoined_by_projection_and_alias_joins(self):
        b = pd.DataFrame({"name": ["Josh Palmer", "Deep Bench", "Star Guy"],
                          "position": ["WR", "WR", "RB"], "team": ["LAC", "X", "Y"],
                          "pos_rank": [50, 90, 1], "overall_rank": [200, 400, 1],
                          "proj_points": [110.0, 30.0, 300.0]})
        out = board.attach_adp(b, self._adp())
        rep = board.market_join_report(out)
        assert [u["name"] for u in rep["unjoined"]] == ["Star Guy", "Deep Bench"]
        assert rep["unjoined_total"] == 2
        assert "synthetic_adp" in rep["unjoined"][0] and "adp" not in rep["unjoined"][0]
        assert rep["alias_joined"] == [{"name": "Josh Palmer", "position": "WR",
                                        "how": "alias", "adp": 150.0}]


class TestRoleMultiplier:
    def test_scales_only_large_disagreements(self):
        from ffdraft import model

        tbl = pd.DataFrame({"proj_points": [185.0, 204.7, 200.0, 100.0, 0.0, 102.5, 120.0,
                                            100.0, 100.0],
                            "espn_proj": [40.9, 181.9, None, 10.0, 50.0, 156.0, 150.0,
                                          69.9, 70.1],
                            # The unprojected row is ranked well inside the cutoff,
                            # so it is not role-unknown; it just has no projection.
                            "espn_rank": [350, 20, 60, 90, 40, 30, 55, 80, 85]})
        m = model.role_multiplier(tbl)
        assert m.tolist() == pytest.approx([40.9 / 185.0 / 0.7, 1.0, 1.0, 0.2, 1.0,
                                            156.0 / 102.5 / 1.3, 1.0, 0.699 / 0.7, 1.0])
        # Continuous at both edges: no step across the thresholds.
        assert abs(m.iloc[7] - m.iloc[8]) < 0.01
        assert model.role_multiplier(pd.DataFrame(
            {"proj_points": [100.0, 100.0], "espn_proj": [129.9, 130.1]})).tolist() == \
            pytest.approx([1.0, 130.1 / 100 / 1.3])

    def test_no_espn_column_is_neutral(self):
        from ffdraft import model

        assert model.role_multiplier(pd.DataFrame({"proj_points": [1.0]})).tolist() == [1.0]

    def test_recommend_demotes_a_lost_role(self):
        from ffdraft import model
        from ffdraft.config import LeagueSettings

        league = LeagueSettings(name="t", teams=16)
        b = pd.DataFrame({
            "name": ["Tyrone Tracy Jr.", "Woody Marks"], "position": ["RB", "RB"],
            "team": ["NYG", "HOU"], "proj_points": [185.0, 176.7], "draft_score": [185.0, 176.7],
            "adp": [170.4, 151.9], "pos_rank": [22, 26], "overall_rank": [75, 84],
            "consistency": [0.5, 0.5], "espn_proj": [40.9, 131.8], "drafted": [False, False],
            "adj_ppg": [12.3, 11.7],
        })
        b["_key"] = b["name"].map(board.norm_name)
        out = model.recommend(b, league, current_pick=125, next_pick=132, roster={"RB": 3})
        assert out["name"].tolist() == ["Woody Marks", "Tyrone Tracy Jr."]
        assert out.set_index("name")["role_mult"]["Tyrone Tracy Jr."] == pytest.approx(40.9 / 185.0 / 0.7)
        assert "role shrank" in model.explain(out.iloc[1])


class TestRoleUnknown:
    def test_no_projection_and_no_meaningful_rank_takes_the_floor(self):
        from ffdraft import model

        tbl = pd.DataFrame({
            "proj_points": [178.8, 178.8, 178.8],
            "espn_proj": [None, None, 170.0],
            # deep in the list; ranked as a real asset; projected, so not unknown.
            "espn_rank": [1401.0, 120.0, 1401.0]})
        assert model.role_multiplier(tbl).tolist() == pytest.approx(
            [model.ROLE_FLOOR, 1.0, 1.0])

    def test_absent_rank_is_unknown_too(self):
        from ffdraft import model

        tbl = pd.DataFrame({"proj_points": [168.6], "espn_proj": [None],
                            "espn_rank": [None]})
        assert model.role_multiplier(tbl).tolist() == [model.ROLE_FLOOR]

    def test_recommend_demotes_a_player_espn_has_no_opinion_on(self):
        from ffdraft import model
        from ffdraft.config import LeagueSettings

        league = LeagueSettings(name="t", teams=12)
        b = pd.DataFrame({
            "name": ["Jared Wayne", "Woody Marks"], "position": ["WR", "RB"],
            "team": ["HOU", "HOU"], "proj_points": [178.8, 176.7],
            "draft_score": [38.9, 46.6], "adp": [170.0, 151.9],
            "pos_rank": [60, 26], "overall_rank": [98, 84],
            "consistency": [0.5, 0.5], "espn_proj": [None, 131.8],
            "espn_rank": [1401.0, 165.0], "drafted": [False, False],
            "adj_ppg": [12.3, 11.7],
        })
        b["_key"] = b["name"].map(board.norm_name)
        out = model.recommend(b, league, current_pick=164, next_pick=189,
                              roster={"WR": 2, "RB": 3, "TE": 1, "QB": 1})
        assert out.set_index("name")["role_mult"]["Jared Wayne"] == model.ROLE_FLOOR
        assert out["name"].tolist() == ["Woody Marks", "Jared Wayne"]
        why = model.explain(out.set_index("name").loc["Jared Wayne"])
        assert "role unknown" in why and "ranks him 1401" in why

    def test_a_discount_never_promotes_a_negative_pick_value(self):
        from ffdraft import model
        from ffdraft.config import LeagueSettings

        # Both candidates are worth less than waiting, so pick_value is negative
        # for both. Multiplying the discounted one by 0.2 would move it toward
        # zero and put it first; it has to stay last.
        league = LeagueSettings(name="t", teams=12)
        b = pd.DataFrame({
            "name": ["Ghost Back", "Real Back"], "position": ["RB", "RB"],
            "team": ["X", "Y"], "proj_points": [120.0, 118.0],
            "draft_score": [-90.0, -95.0], "adp": [400.0, 380.0],
            "pos_rank": [70, 72], "overall_rank": [500, 520],
            "consistency": [0.4, 0.4], "espn_proj": [None, 110.0],
            "espn_rank": [2100.0, 300.0], "drafted": [False, False],
            "adj_ppg": [8.0, 7.9],
        })
        b["_key"] = b["name"].map(board.norm_name)
        out = model.recommend(b, league, current_pick=210, next_pick=None,
                              roster={"RB": 3}).set_index("name")
        assert (out["pick_value"] < 0).all()
        assert out.loc["Ghost Back", "pick_value"] < out.loc["Real Back", "pick_value"]

    def test_audit_names_the_role_unknown_recommendations(self):
        from ffdraft.config import LeagueSettings
        from ffdraft.model import ROLE_UNKNOWN_RANK

        league = LeagueSettings(name="t", teams=12)
        state = board.DraftState(league)
        recs = pd.DataFrame({
            "name": ["Jared Wayne", "Anthony Richardson"], "proj_points": [178.8, 181.6],
            "espn_proj": [None, None], "espn_rank": [1401.0, 300.0],
            "_key": ["jared wayne", "anthony richardson"]})
        out = board.audit_state(pd.DataFrame({"name": [], "position": []}), state, recs)
        joined = " ".join(out["warnings"])
        assert f"no ESPN rank inside {ROLE_UNKNOWN_RANK:.0f}" in joined
        assert "Jared Wayne" in joined
        assert "left unscaled" in joined and "Anthony Richardson" in joined


class TestSpecialTeams:
    def _adp(self):
        adp = pd.DataFrame({
            "name": ["Broncos D/ST", "Texans D/ST", "Rams D/ST", "Brandon Aubrey",
                     "Cameron Dicker", "Jobless Kicker", "Jakobi Meyers"],
            "position": ["DST", "DST", "DST", "K", "K", "K", "WR"],
            "pro_team_id": [7, 34, 14, 6, 24, 9, 30],
            "adp": [99.5, 93.0, 101.8, 84.1, 112.4, 170.0, 122.4],
            "espn_id": ["-16007", "-16034", "-16014", "1", "2", "3", "4"],
            "espn_rank": [179, 176, 243, 119, 159, 800, 112],
            "espn_proj": [130.8, 129.1, 124.4, 171.5, 161.9, 0.0, 181.9],
            "espn_injury": [None, None, None, "ACTIVE", "ACTIVE", "ACTIVE", "ACTIVE"],
            "source": ["espn_adp"] * 7})
        adp["_key"] = adp["name"].map(board.norm_name)
        return adp

    def test_defenses_are_named_the_way_a_drafted_one_is_recorded(self):
        out = board.espn_special_teams(self._adp())
        # ESPN's list says "Broncos D/ST"; _espn_player_name records
        # "Denver Broncos D/ST". Keyed differently, a drafted defense would
        # stay on the board as available.
        assert "Denver Broncos D/ST" in out["name"].tolist()
        assert board._espn_player_name(-16007, {}) in out["name"].tolist()
        assert out.set_index("name").loc["Denver Broncos D/ST", "team"] == "DEN"
        assert out.set_index("name").loc["Los Angeles Rams D/ST", "team"] == "LA"

    def test_only_kickers_and_defenses_espn_actually_projects(self):
        out = board.espn_special_teams(self._adp())
        assert set(out["position"]) == {"DST", "K"}
        # A kicker projected at exactly 0 is ESPN saying he has no job.
        assert "Jobless Kicker" not in out["name"].tolist()
        assert "Jakobi Meyers" not in out["name"].tolist()
        assert out["proj_points"].tolist() == out["espn_proj"].tolist()

    def test_no_market_frame_gives_an_empty_frame_not_a_crash(self):
        assert board.espn_special_teams(None).empty
        assert board.espn_special_teams(pd.DataFrame()).empty

    def test_scored_against_the_last_starter_at_the_position(self):
        from ffdraft import model
        from ffdraft.config import LeagueSettings, ModelWeights

        league = LeagueSettings(name="t", teams=2)
        rows = board.espn_special_teams(self._adp())
        scored = model.score_special_teams(
            rows, pd.DataFrame({"consistency": [0.4, 0.6]}), league, ModelWeights())
        dst = scored[scored["position"] == "DST"].set_index("name")
        # 2 teams, one D/ST slot: the replacement is the 2nd best, 129.1.
        assert dst.loc["Denver Broncos D/ST", "replacement_points"] == 129.1
        assert dst.loc["Denver Broncos D/ST", "vor"] == pytest.approx(1.7)
        # draft_score is (1 - consistency_weight) * VOR: the board's mean
        # consistency contributes nothing, which is what an average-consistency
        # player on the board already gets.
        cw = ModelWeights().consistency_weight
        assert dst.loc["Denver Broncos D/ST", "draft_score"] == pytest.approx((1 - cw) * 1.7)
        assert scored["consistency"].unique().tolist() == [0.5]

    def test_recommend_prices_a_defense_without_letting_it_lead_early(self):
        from ffdraft import model
        from ffdraft.config import LeagueSettings

        league = LeagueSettings(name="t", teams=16)
        # Two defenses, so waiting has a real value to be marginal against: a
        # position with one row can never have a marginal value at all.
        b = pd.DataFrame({
            "name": ["Jakobi Meyers", "Houston Texans D/ST", "Los Angeles Rams D/ST"],
            "position": ["WR", "DST", "DST"],
            "team": ["JAX", "HOU", "LA"], "proj_points": [204.7, 129.1, 124.4],
            "draft_score": [59.1, 29.8, 26.8], "adp": [122.4, 93.0, 101.8],
            "pos_rank": [21, 2, 3], "overall_rank": [98, 119, 125],
            "consistency": [0.61, 0.42, 0.42], "espn_proj": [181.9, 129.1, 124.4],
            "espn_rank": [112.0, 176.0, 243.0], "drafted": [False, False, False],
            "adj_ppg": [13.5, 7.6, 7.3],
        })
        b["_key"] = b["name"].map(board.norm_name)
        roster = {"QB": 1, "RB": 3, "WR": 2, "TE": 1}
        out = model.recommend(b, league, current_pick=125, next_pick=132,
                              roster=roster).set_index("name")
        # The defense is priced -- it is on the list with a real number, where it
        # used to be off the board entirely -- but does not outrank a starter.
        assert out.loc["Houston Texans D/ST", "pick_value"] > 0
        assert out.index[0] == "Jakobi Meyers"
        # Priced on marginal value alone: no share of raw draft_score.
        row = out.loc["Houston Texans D/ST"]
        assert row["pick_value"] == pytest.approx(row["marginal_value"] * row["need_mult"])
        assert row["need_mult"] == 1.18
        why = model.explain(row)
        assert "DST2 by projection" in why
        # A defense has no week-to-week history and ESPN files no injury status.
        assert "consistency" not in why and "nan" not in why

    def test_a_filled_defense_slot_stops_wanting_another(self):
        from ffdraft import model
        from ffdraft.config import LeagueSettings

        league = LeagueSettings(name="t", teams=16)
        need = model._positional_need(league, {"DST": 1, "K": 0})
        assert need["DST"] == 0.02
        assert need["K"] == 1.18
        # Nothing about the modelled positions moved.
        assert need["QB"] == model._positional_need(
            LeagueSettings(name="t", teams=16,
                           starters={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1}),
            {"DST": 1, "K": 0})["QB"]


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
