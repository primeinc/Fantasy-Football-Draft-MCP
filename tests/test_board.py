"""ESPN id crosswalk: tested offline with synthetic weekly_rosters data."""
import pandas as pd

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

        x = board._id_crosswalk()
        assert len(x) == 1

    def test_drops_players_with_no_gsis_id(self, monkeypatch):
        rosters = pd.DataFrame([
            {"gsis_id": None, "espn_id": "123", "sleeper_id": None,
             "full_name": "No Gsis Guy", "position": "WR"},
        ])
        monkeypatch.setattr(sources, "weekly_rosters", lambda: rosters)

        assert board._id_crosswalk().empty


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
