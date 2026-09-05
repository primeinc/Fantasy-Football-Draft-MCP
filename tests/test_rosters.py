"""The in-season ESPN roster reader.

Every fixture here is written from ESPN's documented entry shape, because no
populated `mRoster` exists to write one from: the capture has
`roster.entries: []` for all 16 teams while the draft is in progress. So these
tests pin the mapping and the fallbacks, and they are NOT evidence that the live
parse works -- the fixture and the parser come from the same reading of the
shape, so they agree by construction. That is what `UNVERIFIED_SHAPE` says on
the output, and it is why the label is asserted here rather than assumed.
"""
import pandas as pd

from ffdraft import rosters
from ffdraft.board import _ESPN_POSITION_NAMES, UNPRICED, norm_name

POSITIONS = _ESPN_POSITION_NAMES


def _entry(pid, name, pos_id, slot=rosters.BENCH_SLOT, injury=None):
    return {"playerId": pid, "lineupSlotId": slot,
            "playerPoolEntry": {"player": {
                "id": pid, "fullName": name, "defaultPositionId": pos_id,
                "injuryStatus": injury, "injured": injury not in (None, "ACTIVE")}}}


def _board():
    rows = [
        # name,           pos,  proj,  espn_id
        ("Real Back", "RB", 200.0, "3001"),
        ("Other Back", "RB", 100.0, "3002"),
        ("A Receiver", "WR", 250.0, "3003"),
        ("Renamed Man", "RB", 150.0, None),
    ]
    return pd.DataFrame([{
        "name": n, "_key": norm_name(n), "position": p, "proj_points": pts,
        "espn_id": eid, "replacement_points": 120.0 if p == "RB" else 140.0,
        "vor": pts - 120.0, "draft_score": pts - 120.0,
        "adj_ppg": pts / 17.0, "exp_games": 17.0, "bye_week": 8,
        "espn_injury": None, "off_roster": False, "is_rookie": False,
    } for n, p, pts, eid in rows])


class TestEntryFacts:
    def test_reads_the_fields_the_board_shape_needs(self):
        f = rosters.entry_facts(_entry(3001, "Real Back", 2, slot=2), POSITIONS)
        assert f == {"espn_id": "3001", "name": "Real Back", "position": "RB",
                     "lineup_slot": 2, "espn_injury": None, "injured": False}

    def test_an_injury_status_is_carried_as_espn_files_it(self):
        f = rosters.entry_facts(
            _entry(3001, "Real Back", 2, injury="QUESTIONABLE"), POSITIONS)
        assert f["espn_injury"] == "QUESTIONABLE"
        assert f["injured"] is True

    def test_an_unknown_position_id_is_no_position_rather_than_a_guess(self):
        # 99 is not a position ESPN publishes. A guessed position would place a
        # player in a lineup slot he may not be eligible for.
        f = rosters.entry_facts(_entry(9001, "Mystery Man", 99), POSITIONS)
        assert f["position"] is None

    def test_a_player_id_only_at_the_entry_level_is_still_read(self):
        entry = {"playerId": 4001, "lineupSlotId": 20, "playerPoolEntry": {}}
        assert rosters.entry_facts(entry, POSITIONS)["espn_id"] == "4001"


class TestRosterRows:
    def test_matches_on_espn_id_and_says_so(self):
        out = rosters.roster_rows([_entry(3001, "Real Back", 2)], _board(), POSITIONS)
        assert list(out["name"]) == ["Real Back"]
        assert out["matched_by"].iloc[0] == rosters.MATCHED_BY_ID
        assert not out[UNPRICED].iloc[0]

    def test_falls_back_to_the_normalised_name_and_says_so(self):
        # The board row carries no espn_id, which is the state of any board built
        # before the ADP join ran. Name is the only join left.
        out = rosters.roster_rows([_entry(9999, "Renamed Man", 2)], _board(), POSITIONS)
        assert list(out["name"]) == ["Renamed Man"]
        assert out["matched_by"].iloc[0] == rosters.MATCHED_BY_NAME

    def test_the_id_wins_when_the_name_would_match_a_different_row(self):
        # ESPN's id is authoritative. If a board row happened to share a
        # normalised name with another player, the id must decide -- this is the
        # join lena's #45 bug was the other half of.
        b = _board()
        b.loc[b["name"] == "Other Back", "_key"] = norm_name("Real Back")
        out = rosters.roster_rows([_entry(3001, "Real Back", 2)], b, POSITIONS)
        assert out["proj_points"].iloc[0] == 200.0
        assert out["matched_by"].iloc[0] == rosters.MATCHED_BY_ID

    def test_a_player_the_board_does_not_carry_becomes_a_stand_in(self):
        out = rosters.roster_rows(
            [_entry(3001, "Real Back", 2), _entry(7777, "Ghost Back", 2)],
            _board(), POSITIONS)
        ghost = out.set_index("name").loc["Ghost Back"]
        assert bool(ghost[UNPRICED]) is True
        assert ghost["matched_by"] == rosters.UNMATCHED
        # Priced at the board's replacement for RB, through the same helper the
        # draft record uses, so the two roster sources agree on an unknown man.
        assert float(ghost["proj_points"]) == 120.0
        assert float(ghost["vor"]) == 0.0
        assert pd.isna(ghost["bye_week"])

    def test_the_stand_in_keeps_the_position_espn_gave_him(self):
        # The whole reason this closes #40's remaining gap: `record_pick` files
        # no position for a player off the board, and ESPN does.
        out = rosters.roster_rows([_entry(7777, "Ghost End", 4)], _board(), POSITIONS)
        assert out.set_index("name").loc["Ghost End", "position"] == "TE"

    def test_a_player_with_no_usable_position_is_dropped_not_guessed(self):
        out = rosters.roster_rows([_entry(7777, "Mystery Man", 99)], _board(),
                                  POSITIONS)
        assert "Mystery Man" not in list(out["name"])

    def test_the_columns_freddy_reads_survive(self):
        # #47 prices a week as adj_ppg paid when available, with availability
        # drawn from exp_games. Handing it only proj_points would force it to
        # divide the two back out, which is the same number and stops being
        # checkable.
        out = rosters.roster_rows([_entry(3001, "Real Back", 2)], _board(), POSITIONS)
        assert float(out["adj_ppg"].iloc[0]) > 0
        assert float(out["exp_games"].iloc[0]) == 17.0

    def test_espn_slotting_and_injury_come_from_the_entry(self):
        out = rosters.roster_rows(
            [_entry(3001, "Real Back", 2, slot=2, injury="QUESTIONABLE")],
            _board(), POSITIONS)
        assert out["lineup_slot"].iloc[0] == 2
        assert out["espn_injury"].iloc[0] == "QUESTIONABLE"

    def test_every_frame_says_its_shape_is_unverified(self):
        out = rosters.roster_rows([_entry(3001, "Real Back", 2)], _board(), POSITIONS)
        assert out["shape"].iloc[0] == rosters.UNVERIFIED_SHAPE

    def test_boolean_columns_survive_a_stand_in(self):
        out = rosters.roster_rows(
            [_entry(3001, "Real Back", 2), _entry(7777, "Ghost Back", 2)],
            _board(), POSITIONS)
        for col in ("off_roster", "is_rookie", UNPRICED):
            assert out[col].dtype == bool, col

    def test_an_empty_roster_is_an_empty_frame_not_an_error(self):
        # The live state today: every team's entries are []. The reader has to
        # return a frame a caller can use, not raise.
        out = rosters.roster_rows([], _board(), POSITIONS)
        assert out.empty
        assert UNPRICED in out.columns


class TestStarted:
    def test_bench_and_ir_are_not_starts(self):
        out = rosters.roster_rows([
            _entry(3001, "Real Back", 2, slot=2),
            _entry(3002, "Other Back", 2, slot=rosters.BENCH_SLOT),
            _entry(3003, "A Receiver", 3, slot=rosters.IR_SLOT),
        ], _board(), POSITIONS)
        assert sorted(rosters.started(out)["name"]) == ["Real Back"]

    def test_a_flex_slot_is_a_start_without_knowing_the_slot_table(self):
        # 23 is FLEX. Anything that is not bench or IR is a start, which is the
        # question ESPN has already answered by putting him there.
        out = rosters.roster_rows([_entry(3001, "Real Back", 2, slot=23)],
                                  _board(), POSITIONS)
        assert list(rosters.started(out)["name"]) == ["Real Back"]

    def test_an_empty_roster_starts_nobody(self):
        assert rosters.started(rosters.roster_rows([], _board(), POSITIONS)).empty
