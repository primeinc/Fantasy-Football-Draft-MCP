"""Who starts and who sits.

The defect this exists to stop: `waivers` took the bench as a positional slice
of board-ordered rows, so on a receiver-heavy roster it called five receivers
starters and offered the only kicker, defense and tight end as droppable. The
drop then went to the lowest bench value, which is a kicker or a defense on any
roster, and the tool could recommend emptying a starting slot.

The unbalanced roster below is the ordinary shape of a team that drafted best
available. A balanced fixture cannot see this, which is why nothing caught it.
"""
from typing import Any

import pandas as pd

from ffdraft import lineup
from ffdraft.board import UNPRICED, norm_name
from ffdraft.config import LeagueSettings


def _league(**kw: Any) -> LeagueSettings:
    base: dict[str, Any] = {
        "name": "t", "teams": 12, "draft_slot": 1, "rounds": 14,
        "starters": {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 0,
                     "K": 1, "DST": 1},
    }
    base.update(kw)
    return LeagueSettings(**base)


def _rows(players):
    """(name, position, projection) in board order, which is rank order."""
    return pd.DataFrame([{
        "name": n, "_key": norm_name(n), "position": p, "proj_points": v,
        UNPRICED: False,
    } for n, p, v in players])


# Best-available drafting: four receivers before the tight end, and the kicker
# and defense last because they always project lowest.
UNBALANCED = [
    ("WR1", "WR", 300.0), ("WR2", "WR", 290.0), ("WR3", "WR", 280.0),
    ("WR4", "WR", 270.0), ("RB1", "RB", 260.0), ("RB2", "RB", 250.0),
    ("QB1", "QB", 240.0), ("WR5", "WR", 230.0), ("TE1", "TE", 220.0),
    ("K1", "K", 120.0), ("DST1", "DST", 110.0),
]


class TestStartingLineup:
    def test_the_only_kicker_and_defense_start_however_low_they_project(self):
        # The whole point. They are the two lowest-projected rows on the roster
        # and they are not droppable, because nobody else can fill their slots.
        starters, bench = lineup.starting_lineup(_rows(UNBALANCED), _league())
        assert {"K1", "DST1", "TE1"} <= set(starters["name"])
        assert not {"K1", "DST1", "TE1"} & set(bench["name"])

    def test_the_surplus_receivers_are_the_bench(self):
        starters, bench = lineup.starting_lineup(_rows(UNBALANCED), _league())
        assert sorted(bench["name"]) == ["WR3", "WR4", "WR5"]
        assert len(starters) == 8

    def test_each_starter_names_the_slot_he_fills(self):
        starters, _ = lineup.starting_lineup(_rows(UNBALANCED), _league())
        by_name = starters.set_index("name")[lineup.SLOT_COLUMN].to_dict()
        assert by_name["WR1"] == "WR" and by_name["K1"] == "K"
        assert sorted(starters[lineup.SLOT_COLUMN]) == [
            "DST", "K", "QB", "RB", "RB", "TE", "WR", "WR"]

    def test_the_best_player_at_a_position_gets_the_slot(self):
        starters, _ = lineup.starting_lineup(_rows(UNBALANCED), _league())
        wrs = starters[starters["position"] == "WR"]
        assert sorted(wrs["name"]) == ["WR1", "WR2"]

    def test_a_flex_takes_the_leftovers_after_the_base_slots(self):
        # Base slots first is what stops a fourth receiver taking the flex while
        # the tight end slot sits empty: the base slot has no alternative.
        league = _league(starters={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1,
                                   "K": 1, "DST": 1})
        starters, bench = lineup.starting_lineup(_rows(UNBALANCED), league)
        flex = starters[starters[lineup.SLOT_COLUMN] == lineup.FLEX_SLOT]
        assert list(flex["name"]) == ["WR3"]
        assert "TE1" in set(starters["name"])
        assert sorted(bench["name"]) == ["WR4", "WR5"]

    # A superflex needs its own roster rather than UNBALANCED, and I got this
    # wrong twice before building one. The base QB slot takes the BEST
    # quarterback, so the one competing for the superflex is always the second
    # best; raising a single QB's projection just moves him into the base slot
    # and changes nothing about the superflex. Two quarterbacks who both
    # outproject the spare receivers is the only shape that tests the claim.
    SUPERFLEX_ROSTER = [
        ("QB_A", "QB", 300.0), ("QB_B", "QB", 290.0), ("WR_A", "WR", 280.0),
        ("WR_B", "WR", 270.0), ("RB_A", "RB", 260.0), ("RB_B", "RB", 250.0),
        ("TE_A", "TE", 240.0), ("K_A", "K", 120.0), ("DST_A", "DST", 110.0),
    ]

    def test_a_superflex_may_take_a_quarterback(self):
        league = _league(superflex=1)
        starters, _ = lineup.starting_lineup(_rows(self.SUPERFLEX_ROSTER), league)
        by_slot = starters.set_index("name")[lineup.SLOT_COLUMN].to_dict()
        assert by_slot["QB_A"] == "QB"
        assert by_slot["QB_B"] == lineup.SUPERFLEX_SLOT

    def test_a_superflex_goes_to_the_better_player_not_to_the_quarterback(self):
        # Eligibility is not priority: a spare receiver who outprojects the
        # backup quarterback takes the slot instead.
        #
        # Note what this roster has to look like, because I built it wrong twice.
        # The contest is between the SECOND quarterback and the THIRD receiver,
        # since the base slots take the best one and the best two. Simply giving
        # a receiver a big number promotes him into a base WR slot and changes
        # nothing here. Three receivers above the backup QB is the shape.
        league = _league(superflex=1)
        rows = _rows([
            ("QB_A", "QB", 300.0), ("QB_B", "QB", 290.0),
            ("WR_A", "WR", 320.0), ("WR_B", "WR", 310.0), ("WR_C", "WR", 300.0),
            ("RB_A", "RB", 260.0), ("RB_B", "RB", 250.0), ("TE_A", "TE", 240.0),
            ("K_A", "K", 120.0), ("DST_A", "DST", 110.0),
        ])
        starters, _ = lineup.starting_lineup(rows, league)
        sflex = starters[starters[lineup.SLOT_COLUMN] == lineup.SUPERFLEX_SLOT]
        assert list(sflex["name"]) == ["WR_C"]

    def test_a_slot_with_nobody_eligible_is_left_empty_not_filled_wrongly(self):
        # An empty starting slot is a real problem for the user. Promoting an
        # ineligible player would hide it, which is worse than reporting it.
        rows = _rows([("WR1", "WR", 300.0), ("WR2", "WR", 290.0),
                      ("RB1", "RB", 260.0), ("RB2", "RB", 250.0),
                      ("QB1", "QB", 240.0), ("TE1", "TE", 220.0)])
        starters, _ = lineup.starting_lineup(rows, _league())
        assert "K" not in set(starters[lineup.SLOT_COLUMN])
        assert lineup.unfilled_slots(starters, _league()) == {"K": 1, "DST": 1}

    def test_a_full_roster_leaves_no_slot_unfilled(self):
        starters, _ = lineup.starting_lineup(_rows(UNBALANCED), _league())
        assert lineup.unfilled_slots(starters, _league()) == {}

    def test_the_value_column_is_the_caller_s_choice(self):
        # set_lineup maximises a weekly number rather than the season one. Same
        # shape, different column, so the two questions share this function.
        rows = _rows(UNBALANCED)
        rows["week_points"] = [1.0] * len(rows)
        rows.loc[rows["name"] == "WR5", "week_points"] = 99.0
        starters, _ = lineup.starting_lineup(rows, _league(), value="week_points")
        assert "WR5" in set(starters[starters["position"] == "WR"]["name"])

    def test_a_missing_value_column_does_not_raise(self):
        rows = _rows(UNBALANCED).drop(columns=["proj_points"])
        starters, bench = lineup.starting_lineup(rows, _league())
        assert len(starters) + len(bench) == len(rows)

    def test_an_empty_roster_starts_nobody(self):
        starters, bench = lineup.starting_lineup(_rows([]), _league())
        assert starters.empty and bench.empty


class TestUnpricedStandIn:
    """#40's stand-ins occupy their slots here too."""

    def test_an_unpriced_kicker_still_starts_because_he_is_the_only_one(self):
        rows = _rows(UNBALANCED[:-2] + [("DST1", "DST", 110.0)])
        ghost = pd.DataFrame([{
            "name": "Ghost Kicker", "_key": norm_name("Ghost Kicker"),
            "position": "K", "proj_points": 0.0, UNPRICED: True}])
        rows = pd.concat([rows, ghost], ignore_index=True)
        starters, bench = lineup.starting_lineup(rows, _league())
        assert "Ghost Kicker" in set(starters["name"])
        assert "Ghost Kicker" not in set(bench["name"])
        assert lineup.unfilled_slots(starters, _league()) == {}

    def test_a_stand_in_loses_the_slot_to_a_priced_player_who_is_better(self):
        # He is not privileged, only counted. A real kicker with a projection
        # outranks a stand-in priced at replacement.
        rows = _rows(UNBALANCED)
        ghost = pd.DataFrame([{
            "name": "Ghost Kicker", "_key": norm_name("Ghost Kicker"),
            "position": "K", "proj_points": 0.0, UNPRICED: True}])
        rows = pd.concat([rows, ghost], ignore_index=True)
        starters, bench = lineup.starting_lineup(rows, _league())
        assert "K1" in set(starters["name"])
        assert "Ghost Kicker" in set(bench["name"])


class TestNoStarterIsEverDroppable:
    """The general form of the waiver defect, stated so it fails on any shape.

    lena's sharper version: the tool offered a drop whose own
    `starts_in_a_given_week` was 1.0 — two numbers in one payload contradicting
    each other, neither wrong on its own, computed over the wrong set. Pinned
    here as a property rather than as "K and DST are lowest", because that
    phrasing only catches this one roster shape.
    """

    def test_no_row_is_both_a_starter_and_on_the_bench(self):
        for league in (_league(),
                       _league(starters={"QB": 1, "RB": 2, "WR": 3, "TE": 1,
                                         "FLEX": 1, "K": 1, "DST": 1}),
                       _league(superflex=1)):
            starters, bench = lineup.starting_lineup(_rows(UNBALANCED), league)
            assert not set(starters["name"]) & set(bench["name"])
            assert len(starters) + len(bench) == len(UNBALANCED)

    def test_every_position_with_exactly_one_holder_keeps_him(self):
        # Whatever the shape: if the roster holds exactly as many of a position
        # as the league starts, none of them can be spare.
        drop = set(lineup.droppable(_rows(UNBALANCED), _league())["name"])
        for only in ("QB1", "TE1", "K1", "DST1"):
            assert only not in drop, only


class TestUnplaceableRows:
    """A row the board forgot to classify must not become the cut."""

    def test_a_row_with_no_position_is_not_offered_as_droppable(self):
        rows = pd.concat([_rows(UNBALANCED), pd.DataFrame([{
            "name": "Broken Row", "_key": norm_name("Broken Row"),
            "position": float("nan"), "proj_points": 5.0, UNPRICED: False}])],
            ignore_index=True)
        assert "Broken Row" not in set(lineup.droppable(rows, _league())["name"])
        assert list(lineup.unplaceable(rows)["name"]) == ["Broken Row"]

    def test_he_is_not_a_starter_either(self):
        rows = _rows([("Broken Row", "", 300.0), ("K1", "K", 120.0)])
        starters, _ = lineup.starting_lineup(rows, _league())
        assert "Broken Row" not in set(starters["name"])

    def test_a_clean_roster_has_nobody_unplaceable(self):
        assert lineup.unplaceable(_rows(UNBALANCED)).empty


class TestDuplicateIndexLabels:
    def test_a_frame_with_repeated_labels_is_split_correctly(self):
        # `.loc` on a duplicated label returns more rows than it was asked for.
        # my_rows is clean today by construction, which is a property of its
        # inputs rather than of this function's. Flagged by lena.
        rows = _rows(UNBALANCED)
        rows.index = [0] * len(rows)
        starters, bench = lineup.starting_lineup(rows, _league())
        assert len(starters) == 8
        assert len(bench) == 3
        assert sorted(bench["name"]) == ["WR3", "WR4", "WR5"]


class TestDroppable:
    def test_never_offers_a_player_whose_slot_nobody_else_can_fill(self):
        # The waiver defect, stated as the property that stops it.
        drop = lineup.droppable(_rows(UNBALANCED), _league())
        assert not {"K1", "DST1", "TE1", "QB1"} & set(drop["name"])
        assert sorted(drop["name"]) == ["WR3", "WR4", "WR5"]

    def test_a_roster_with_nothing_spare_offers_nobody(self):
        rows = _rows([("WR1", "WR", 300.0), ("WR2", "WR", 290.0),
                      ("RB1", "RB", 260.0), ("RB2", "RB", 250.0),
                      ("QB1", "QB", 240.0), ("TE1", "TE", 220.0),
                      ("K1", "K", 120.0), ("DST1", "DST", 110.0)])
        assert lineup.droppable(rows, _league()).empty
