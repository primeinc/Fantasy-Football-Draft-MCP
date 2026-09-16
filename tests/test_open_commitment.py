"""`open_commitment`, `block_commitment`, `cancel_commitment`: an approval is
written with ESPN's ids the moment it is given, or not at all."""
import json

import pytest

from ffdraft import commitments, pool, rosters, server

SWID = "AAAA-1111"
DEADLINE = "2026-09-16T03:00:00-04:00"


def _entry(pid, name, status, team=0):
    return {"id": pid, "status": status, "onTeamId": team, "waiverProcessDate": 0,
            "player": {"id": pid, "fullName": name, "defaultPositionId": 1, "proTeamId": 16,
                       "injuryStatus": "ACTIVE", "droppable": True, "ownership": {}, "stats": []}}


POOL = [_entry(2573079, "Carson Wentz", "FREEAGENT"), _entry(3924327, "Drew Lock", "FREEAGENT"),
        _entry(2578570, "Jacoby Brissett", "WAIVERS"), _entry(4696981, "Tyrone Tracy Jr.", "ONTEAM", 3),
        _entry(4241463, "Jerry Jeudy", "ONTEAM", 3), _entry(1, "Josh Allen", "ONTEAM", 12),
        _entry(2, "Josh Jacobs", "ONTEAM", 14)]


def _roster_entry(pid, name):
    return {"playerId": pid, "lineupSlotId": 20,
            "playerPoolEntry": {"player": {"id": pid, "fullName": name, "defaultPositionId": 1}}}


PAYLOAD = {"members": [{"id": "{AAAA-1111}", "firstName": "Will", "lastName": "P"},
                       {"id": "{DDDD-4444}", "firstName": "Dean", "lastName": "H"}],
           "teams": [{"id": 3, "name": "adverse possession", "owners": ["{AAAA-1111}"],
                      "roster": {"entries": [_roster_entry(4241463, "Jerry Jeudy")]}},
                     {"id": 12, "name": "Post Closing King", "owners": ["{DDDD-4444}"],
                      "roster": {"entries": [_roster_entry(2573079, "Carson Wentz")]}}]}


@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setenv("ESPN_SWID", SWID)
    monkeypatch.setenv("ESPN_S2", "S2-TEST")
    path = tmp_path / "commitments.json"
    monkeypatch.setattr(commitments, "COMMITMENTS", path)
    reads: list[str] = []
    monkeypatch.setattr(pool, "fetch_pool", lambda *_a, **_k: reads.append("pool") or POOL)
    monkeypatch.setattr(rosters, "fetch_roster_payload",
                        lambda *_a, **_k: reads.append("rosters") or PAYLOAD)
    return {"path": path, "reads": reads}


def ledger(wired):
    return commitments.load(wired["path"])[0]


def test_a_claim_is_written_with_espn_ids_and_the_users_words(wired):
    out = json.loads(server.open_commitment(
        "1734659820", 2, "claim", DEADLINE, add="Carson Wentz", drop="Tyrone Tracy Jr.",
        fallbacks="Drew Lock, Jacoby Brissett",
        approved_text="claim Carson Wentz and drop Tyrone Tracy Jr. before 03:00 ET"))
    assert out["opened"] is True
    row = out["commitment"]
    assert row["add"] == {"name": "Carson Wentz", "espn_id": 2573079}
    assert row["drop"] == {"name": "Tyrone Tracy Jr.", "espn_id": 4696981}
    assert [f["espn_id"] for f in row["fallbacks"]] == [3924327, 2578570]
    assert row["deadline"] == "2026-09-16T07:00:00+00:00" and row["status"] == "open"
    assert row["approved_text"].startswith("claim Carson Wentz")
    assert ledger(wired) == [row]
    assert "HOT" in out["note"]


@pytest.mark.parametrize("add, refusal", [
    ("Wentz", "add 'Wentz' is not a full name; an approval needs 'Carson Wentz'"),
    ("Josh", "'Josh' matches 2 players (Josh Allen, Josh Jacobs); name one"),
    ("Nobody", "no player named 'Nobody' in ESPN's pool"),
])
def test_a_name_that_is_not_one_full_player_writes_nothing(wired, add, refusal):
    out = json.loads(server.open_commitment("L", 2, "claim", DEADLINE, add=add))
    assert out == {"opened": False, "refusals": [refusal]}
    assert ledger(wired) == []


def test_a_deadline_without_a_zone_and_an_unknown_kind_write_nothing(wired):
    out = json.loads(server.open_commitment("L", 2, "claim", "2026-09-16T03:00:00",
                                            add="Carson Wentz"))
    assert out["opened"] is False and "UTC offset" in out["refusals"][0]
    out = json.loads(server.open_commitment("L", 2, "waiver", DEADLINE, add="Carson Wentz"))
    assert out["error"].startswith("kind 'waiver' is not one of")
    assert ledger(wired) == []


def test_a_trade_resolves_the_partner_and_both_sides(wired):
    out = json.loads(server.open_commitment("L", 2, "trade", DEADLINE, give="Jerry Jeudy",
                                            get="Carson Wentz", partner="king"))
    assert out["opened"] is True
    row = out["commitment"]
    assert row["partner_team_id"] == 12
    assert row["give"] == [{"name": "Jerry Jeudy", "espn_id": 4241463}]
    assert row["get"] == [{"name": "Carson Wentz", "espn_id": 2573079}]
    out = json.loads(server.open_commitment("L", 2, "trade", DEADLINE, give="Jerry Jeudy",
                                            get="Carson Wentz", partner="nobody"))
    assert out == {"opened": False, "refusals": ["no team named 'nobody' in this league"]}


def test_a_lineup_commitment_reads_nothing_from_espn(wired):
    out = json.loads(server.open_commitment("L", 3, "lineup", DEADLINE))
    assert out["opened"] is True and out["commitment"]["kind"] == "lineup"
    assert wired["reads"] == []


def test_block_and_cancel_close_once_and_say_why(wired):
    a = json.loads(server.open_commitment("L", 2, "claim", DEADLINE, add="Carson Wentz"))
    b = json.loads(server.open_commitment("L", 2, "claim", DEADLINE, add="Drew Lock"))
    c = json.loads(server.open_commitment("L", 2, "claim", DEADLINE, add="Jacoby Brissett"))
    out = json.loads(server.block_commitment(a["commitment"]["id"], "ESPN 503 on send"))
    assert out["closed"] is True and out["commitment"]["blocked"]["reason"] == "ESPN 503 on send"
    out = json.loads(server.block_commitment(b["commitment"]["id"], "Wentz is on team 12",
                                             impossible=True))
    assert out["commitment"]["status"] == "impossible"
    out = json.loads(server.cancel_commitment(c["commitment"]["id"]))
    assert out["commitment"]["cancelled"]["by"] == "user"
    out = json.loads(server.cancel_commitment(c["commitment"]["id"]))
    assert out == {"closed": False, "error": f"commitment {c['commitment']['id']} is cancelled, "
                                              f"not open"}
    assert commitments.open_ones(ledger(wired)) == []


def test_no_credentials_is_an_error(monkeypatch):
    monkeypatch.delenv("ESPN_SWID", raising=False)
    monkeypatch.delenv("ESPN_S2", raising=False)
    out = json.loads(server.open_commitment("L", 2, "claim", DEADLINE, add="Carson Wentz"))
    assert out == {"error": "open_commitment needs ESPN_SWID and ESPN_S2"}
