"""The lineup write: moves computed from the recommended lineup, the
transaction ESPN's own client sends, and a send that goes nowhere on a dry run.
"""
from __future__ import annotations

import pandas as pd

from ffdraft import lineup_write as lw


def _roster(rows):
    return pd.DataFrame(rows)


def _starters(pairs):
    return pd.DataFrame([{"name": n, lw.SLOT_COLUMN: slot} for n, slot in pairs])


BASE = [
    {"name": "QB One", "espn_id": "1", "position": "QB", "lineup_slot": 0,
     "eligible_slots": [0, 7, 20], "lineup_locked": False},
    {"name": "RB One", "espn_id": "2", "position": "RB", "lineup_slot": 20,
     "eligible_slots": [2, 3, 23, 20], "lineup_locked": False},
    {"name": "RB Two", "espn_id": "3", "position": "RB", "lineup_slot": 2,
     "eligible_slots": [2, 3, 23, 20], "lineup_locked": False},
    {"name": "WR IR", "espn_id": "4", "position": "WR", "lineup_slot": 21,
     "eligible_slots": [4, 5, 23, 20, 21], "lineup_locked": False},
]


def test_moves_only_the_players_whose_slot_changes():
    plan = lw.plan_moves(_starters([("QB One", "QB"), ("RB One", "RB")]), _roster(BASE))
    assert plan["refusals"] == []
    assert plan["items"] == [
        {"playerId": 2, "type": "LINEUP", "fromLineupSlotId": 20, "toLineupSlotId": 2},
        {"playerId": 3, "type": "LINEUP", "fromLineupSlotId": 2, "toLineupSlotId": 20},
    ]
    assert plan["after"]["WR IR"] == 21, "injured reserve is never touched"
    assert plan["before"]["QB One"] == plan["after"]["QB One"] == 0


def test_a_player_without_an_espn_id_refuses_the_whole_plan():
    rows = [dict(BASE[1], espn_id=None)] + BASE[2:]
    plan = lw.plan_moves(_starters([("RB One", "RB")]), _roster(rows))
    assert any("no player id" in r for r in plan["refusals"])


def test_an_ineligible_slot_refuses():
    plan = lw.plan_moves(_starters([("QB One", "RB")]), _roster(BASE))
    assert any("not among his eligible slots" in r for r in plan["refusals"])


def test_a_locked_player_refuses_and_an_unknown_lock_is_named():
    rows = [dict(BASE[1], lineup_locked=True), dict(BASE[2], lineup_locked=None)]
    plan = lw.plan_moves(_starters([("RB One", "RB")]), _roster(rows))
    assert any("locked" in r for r in plan["refusals"])
    assert plan["lock_status_unknown_for"] == ["RB Two"]


def test_the_transaction_is_field_for_field_what_espn_sends():
    tx = lw.lineup_transaction(3, "ABC-123", 4, [{"playerId": 2, "type": "LINEUP",
                                                 "fromLineupSlotId": 20, "toLineupSlotId": 2}])
    assert tx == {"isLeagueManager": False, "teamId": 3, "type": "ROSTER",
                  "memberId": "{ABC-123}", "scoringPeriodId": 4, "executionType": "EXECUTE",
                  "items": [{"playerId": 2, "type": "LINEUP", "fromLineupSlotId": 20,
                             "toLineupSlotId": 2}]}
    assert lw.lineup_transaction(3, "{ABC-123}", 4, [])["memberId"] == "{ABC-123}"


def test_the_url_is_the_writes_host_transactions_path():
    assert lw.transaction_url("1734659820", 2026) == (
        "https://lm-api-writes.fantasy.espn.com/apis/v3/games/ffl/seasons/2026"
        "/segments/0/leagues/1734659820/transactions/")


def test_send_posts_the_payload_with_the_client_headers(monkeypatch):
    seen = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"ok": 1}

    def spy(url, json=None, cookies=None, headers=None, timeout=None):
        seen.update(url=url, json=json, headers=headers, cookies=cookies)
        return Resp()

    out = lw.send("L", 2026, {"type": "ROSTER"}, swid="X", espn_s2="Y", post=spy)
    assert out == {"status": 200, "body": {"ok": 1}}
    assert seen["url"] == lw.transaction_url("L", 2026)
    assert seen["json"] == {"type": "ROSTER"}
    assert seen["headers"]["X-Fantasy-Source"] == "kona"
    assert seen["headers"]["X-Fantasy-Platform"] == "espn-fantasy-web"
    assert seen["cookies"] == {"SWID": "{X}", "espn_s2": "Y"}
