"""A roster player the board cannot price still occupies his slot.

The live fault: MarShawn Lloyd at pick 93 has no board row, so `my_rows` returned
seven players for an eight-man roster and the lineup model counted two running
backs where the roster holds three. `draft_status` reported his position as null
in the same response whose `roster_counts` already counted him.
"""
import numpy as np
import pandas as pd

from ffdraft import board as bd
from ffdraft import roles
from ffdraft.board import DraftState
from ffdraft.config import LeagueSettings

# Two starting slots at RB is what makes the count decide anything: with one man
# ahead of a candidate the slot is genuinely open and he starts; with two it is
# not. The whole defect lives in which side of that line an unpriced man falls.
RB_REPLACEMENT = 150.0


def _league():
    return LeagueSettings(name="t", teams=12, draft_slot=1, rounds=14,
                          starters={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 0,
                                    "K": 1, "DST": 1})


def _board():
    """Two priced running backs on my roster, plus candidates.

    RB_A at 200 and RB_B at 100 straddle the replacement level of 150, so a
    candidate at 120 sits below the unpriced man and above the priced RB_B.
    That is the only arrangement where dropping the unpriced man changes the
    answer, and it is the arrangement the live roster is in.
    """
    rows = [
        ("RB_A", "RB", 200.0), ("RB_B", "RB", 100.0),
        ("WR_A", "WR", 250.0), ("CAND_RB", "RB", 120.0),
        ("CAND_RB_HIGH", "RB", 180.0), ("CAND_WR", "WR", 190.0),
    ]
    return pd.DataFrame([{
        # The real normaliser, not `lower()`: it folds punctuation, so a board
        # keyed by hand would silently match nothing and every assertion below
        # would be testing an empty frame.
        "name": n, "_key": bd.norm_name(n), "position": p, "proj_points": pts,
        "replacement_points": (RB_REPLACEMENT if p == "RB" else 120.0),
        "vor": pts - (RB_REPLACEMENT if p == "RB" else 120.0),
        "draft_score": pts - (RB_REPLACEMENT if p == "RB" else 120.0),
        "consistency": 0.5, "bye_week": 8, "exp_games": 17.0,
        "adp": 50.0, "drafted": False, "off_roster": False, "is_rookie": False,
    } for n, p, pts in rows])


def _state(tmp_path, monkeypatch, name="unpriced"):
    """Two priced backs and one the board does not carry, all mine."""
    monkeypatch.setattr(bd, "STATE_DIR", tmp_path)
    st = DraftState(_league(), name=name)
    st.record("RB_A", overall=1, team_slot=1)
    st.record("RB_B", overall=2, team_slot=1)
    # The unpriced man: on the roster, not on the board, position known from the
    # draft record because the ESPN sync files one.
    st.record("Ghost Back", overall=3, team_slot=1, position="RB")
    return st


class TestMyRows:
    def test_the_unpriced_player_is_on_the_roster_the_model_sees(self, tmp_path,
                                                                 monkeypatch):
        rows = _state(tmp_path, monkeypatch).my_rows(_board())
        assert sorted(rows["name"]) == ["Ghost Back", "RB_A", "RB_B"]
        ghost = rows.set_index("name").loc["Ghost Back"]
        assert ghost["position"] == "RB"
        assert float(ghost["proj_points"]) == RB_REPLACEMENT
        assert bool(ghost[bd.UNPRICED]) is True

    def test_he_is_priced_at_replacement_and_no_higher(self, tmp_path, monkeypatch):
        # Not a guess at what he is really worth: vor 0 says the board has no
        # opinion, and a number invented here would be one nothing supports.
        rows = _state(tmp_path, monkeypatch).my_rows(_board())
        ghost = rows.set_index("name").loc["Ghost Back"]
        assert float(ghost["vor"]) == 0.0
        assert float(ghost["draft_score"]) == 0.0

    def test_the_priced_players_are_not_marked_unpriced(self, tmp_path, monkeypatch):
        rows = _state(tmp_path, monkeypatch).my_rows(_board()).set_index("name")
        assert bool(rows.loc["RB_A", bd.UNPRICED]) is False
        assert bool(rows.loc["RB_B", bd.UNPRICED]) is False

    def test_boolean_columns_stay_boolean(self, tmp_path, monkeypatch):
        # Appending a row that has no opinion about a bool column turns it to
        # object and breaks every mask downstream. The K/DST rows did exactly
        # this once, so it is pinned rather than assumed.
        rows = _state(tmp_path, monkeypatch).my_rows(_board())
        for col in ("off_roster", "is_rookie", bd.UNPRICED):
            assert rows[col].dtype == bool, (col, rows[col].dtype)

    def test_he_carries_no_bye_week_rather_than_a_borrowed_one(self, tmp_path,
                                                              monkeypatch):
        # The board does not know his bye, so the bye term must skip him instead
        # of scoring a conflict against a fabricated week.
        rows = _state(tmp_path, monkeypatch).my_rows(_board())
        assert pd.isna(rows.set_index("name").loc["Ghost Back", "bye_week"])
        assert len(rows.dropna(subset=["bye_week"])) == 2

    def test_a_board_without_the_flag_columns_still_works(self, tmp_path,
                                                          monkeypatch):
        # Boards in this repo do not all carry the same flags: the live one has
        # off_roster and is_rookie, and a minimal fixture has neither. Naming
        # them raised KeyError on a board that had neither, which my own fixture
        # could not catch because it happened to carry both. The coercion reads
        # the frame's dtypes now, so a board with no bool columns is fine.
        monkeypatch.setattr(bd, "STATE_DIR", tmp_path)
        bare = _board().drop(columns=["off_roster", "is_rookie"])
        st = DraftState(_league(), name="bare")
        st.record("RB_A", overall=1, team_slot=1)
        st.record("Ghost Back", overall=2, team_slot=1, position="RB")
        rows = st.my_rows(bare)
        assert sorted(rows["name"]) == ["Ghost Back", "RB_A"]
        assert rows[bd.UNPRICED].dtype == bool
        ghost = rows.set_index("name").loc["Ghost Back"]
        assert float(ghost["proj_points"]) == RB_REPLACEMENT

    def test_a_bool_column_the_stand_in_never_sets_defaults_to_false(self, tmp_path,
                                                                    monkeypatch):
        # Any bool the board carries has to survive the concat, not just the two
        # this module happens to know about.
        monkeypatch.setattr(bd, "STATE_DIR", tmp_path)
        extra = _board()
        extra["keeper"] = True
        st = DraftState(_league(), name="extra")
        st.record("RB_A", overall=1, team_slot=1)
        st.record("Ghost Back", overall=2, team_slot=1, position="RB")
        rows = st.my_rows(extra).set_index("name")
        assert rows["keeper"].dtype == bool
        assert bool(rows.loc["Ghost Back", "keeper"]) is False
        assert bool(rows.loc["RB_A", "keeper"]) is True

    def test_a_player_with_no_position_anywhere_is_still_dropped(self, tmp_path,
                                                                 monkeypatch):
        # Nothing can place him, so he cannot occupy a slot. my_roster drops him
        # for the same reason, and the two must not disagree.
        monkeypatch.setattr(bd, "STATE_DIR", tmp_path)
        st = DraftState(_league(), name="nameless")
        st.record("RB_A", overall=1, team_slot=1)
        st.record("Nobody At All", overall=2, team_slot=1)
        assert sorted(st.my_rows(_board())["name"]) == ["RB_A"]
        assert st.my_roster(_board()) == {"RB": 1}

    def test_a_board_row_with_no_position_falls_back_to_the_recorded_one(
            self, tmp_path, monkeypatch):
        """NaN is truthy, so the `or` written to reach the fallback returned it.

        The count then went under a float key. `who_should_i_pick` passes these
        counts out as `your_roster`, where json renders that key as a position
        named "NaN", and its roster note sorts them -- so a NaN key beside any
        other thin position raised TypeError and took the whole tool down.
        Only a malformed board row reaches this, which is why nothing had.
        """
        monkeypatch.setattr(bd, "STATE_DIR", tmp_path)
        st = DraftState(_league(), name="blankpos")
        st.record("RB_A", overall=1, team_slot=1)
        st.record("RB_B", overall=2, team_slot=1, position="RB")

        nan_pos = _board()
        nan_pos.loc[nan_pos["name"] == "RB_B", "position"] = np.nan
        counts = st.my_roster(nan_pos)
        assert counts == {"RB": 2}
        # The type the signature promises, and what makes the note's sort safe.
        assert all(isinstance(key, str) for key in counts)

        # Its twin. An empty string is falsy, so this one always reached the
        # fallback; the two malformed shapes must not answer differently.
        empty_pos = _board()
        empty_pos.loc[empty_pos["name"] == "RB_B", "position"] = ""
        assert st.my_roster(empty_pos) == {"RB": 2}

        # And the split the roster note now exists to report: the board keeps
        # the row as it stands, so the man is counted at RB and priced at none.
        priced = st.my_rows(nan_pos)["position"].astype(str).value_counts().to_dict()
        assert priced.get("RB", 0) == 1

    def test_a_stand_in_is_never_built_at_a_position_called_nan(self, tmp_path,
                                                                monkeypatch):
        # The fourth copy of the same rule, found by marge on review. `my_rows`
        # filtered on `p.get("position")` and stringified whatever passed, and
        # NaN is truthy -- so a record carrying NaN built a stand-in at the
        # position "nan", priced at that position's replacement level, which
        # does not exist. It reads the same rule as the counts now.
        monkeypatch.setattr(bd, "STATE_DIR", tmp_path)
        st = DraftState(_league(), name="standin")
        st.record("RB_A", overall=1, team_slot=1)
        st.record("Ghost Back", overall=2, team_slot=1, position="RB")
        st.picks[-1]["position"] = np.nan

        rows = st.my_rows(_board())
        assert sorted(rows["name"]) == ["RB_A"]
        assert "nan" not in set(rows["position"].astype(str))
        # And the two halves still agree about him: neither counts him.
        assert st.my_roster(_board()) == {"RB": 1}

    def test_the_room_wide_count_invents_no_phantom_position(self, tmp_path,
                                                             monkeypatch):
        # Same cause, second symptom. `picks_by_position` stringified the NaN
        # rather than reaching the fallback, so `plan_my_draft` compared its
        # remaining supply against a position called "nan".
        monkeypatch.setattr(bd, "STATE_DIR", tmp_path)
        st = DraftState(_league(), name="phantom")
        st.record("RB_A", overall=1, team_slot=1)
        st.record("RB_B", overall=2, team_slot=2, position="RB")
        nan_pos = _board()
        nan_pos.loc[nan_pos["name"] == "RB_B", "position"] = np.nan
        assert st.picks_by_position(nan_pos) == {"RB": 2}

    def test_a_kicker_the_board_forgot_to_classify_still_fills_his_slot(
            self, tmp_path, monkeypatch):
        # Third symptom. `held_by_slot` tests membership in SPECIAL_POSITIONS,
        # and NaN is in nothing, so the pick was dropped and the team read as
        # still needing a kicker -- the exact deferral #26 decides on.
        monkeypatch.setattr(bd, "STATE_DIR", tmp_path)
        b = _board()
        b.loc[len(b)] = {**b.iloc[0].to_dict(), "name": "Boot",
                         "_key": bd.norm_name("Boot"), "position": np.nan}
        st = DraftState(_league(), name="kicker")
        st.record("Boot", overall=1, team_slot=3, position="K")
        assert st.held_by_slot(b) == {3: {"K": 1}}

    def test_an_all_priced_roster_is_unchanged_apart_from_the_marker(self, tmp_path,
                                                                    monkeypatch):
        monkeypatch.setattr(bd, "STATE_DIR", tmp_path)
        st = DraftState(_league(), name="allpriced")
        st.record("RB_A", overall=1, team_slot=1)
        st.record("WR_A", overall=2, team_slot=1)
        rows = st.my_rows(_board())
        assert len(rows) == 2
        assert not rows[bd.UNPRICED].any()


class TestStartProbability:
    """What the count is for: whether a candidate queues behind him."""

    def test_a_candidate_below_him_is_no_longer_a_certain_starter(self, tmp_path,
                                                                  monkeypatch):
        board, league = _board(), _league()
        cand = board[board["name"] == "CAND_RB"]

        # Dropping the unpriced man leaves one RB above the candidate against
        # two starting slots, so the model calls him a certain starter.
        priced_only = board[board["_key"].isin([bd.norm_name("RB_A"), bd.norm_name("RB_B")])]
        assert float(roles.start_probabilities(cand, league, priced_only).iloc[0]) == 1.0

        # Counting him puts two men above the candidate and the certainty goes.
        full = _state(tmp_path, monkeypatch).my_rows(board)
        assert float(roles.start_probabilities(cand, league, full).iloc[0]) < 1.0

    def test_a_candidate_above_him_is_still_a_certain_starter(self, tmp_path,
                                                             monkeypatch):
        # The fix must not manufacture competition. A candidate who outprojects
        # him has the same one man ahead either way, and a slot really is open.
        # This is why the live headline at 132 does not move: Woody Marks
        # outprojects the roster's RB2 by 21 points, so his 1.00 was correct.
        board, league = _board(), _league()
        cand = board[board["name"] == "CAND_RB_HIGH"]
        full = _state(tmp_path, monkeypatch).my_rows(board)
        assert float(roles.start_probabilities(cand, league, full).iloc[0]) == 1.0

    def test_another_position_is_untouched(self, tmp_path, monkeypatch):
        board, league = _board(), _league()
        cand = board[board["name"] == "CAND_WR"]
        priced_only = board[board["_key"].isin([bd.norm_name("RB_A"), bd.norm_name("RB_B")])]
        full = _state(tmp_path, monkeypatch).my_rows(board)
        assert (float(roles.start_probabilities(cand, league, priced_only).iloc[0])
                == float(roles.start_probabilities(cand, league, full).iloc[0]))


class TestReplacementPoints:
    def test_reads_the_column_the_board_already_carries(self):
        assert bd.replacement_points(_board(), "RB") == RB_REPLACEMENT

    def test_a_position_the_board_does_not_carry_is_worth_nothing(self):
        # The same thing vor already means: no value over a free alternative.
        assert bd.replacement_points(_board(), "DST") == 0.0

    def test_a_board_without_the_column_is_worth_nothing(self):
        assert bd.replacement_points(pd.DataFrame({"position": ["RB"]}), "RB") == 0.0
