"""The Tuesday the commitment store exists because of, as a regression test.

2026-09-15 15:17 ET: the user put the session into EMERGENCY ROSTER PROTOCOL;
claim Carson Wentz and drop Tyrone Tracy Jr. before waivers ran at 03:00 ET,
fallbacks Drew Lock, then Jacoby Brissett. 21:13 ET the last decision point
passed. 02:36 ET the session said "there's no send path from here". 03:06 ET
waivers ran without the claim. 16:30 ET team 12 added Wentz. 2026-09-16 a
Jeudy-for-Wentz offer was built on "i want wentz" and nearly sent.

Each assertion names the expected or forbidden result for that moment. The
clock is the fixture's: every time below is given, never read.
"""
import json

import pandas as pd
import pytest

from ffdraft import (
    claims,
    commitments,
    governor,
    lineup_write,
    live,
    pool,
    rosters,
    server,
    transactions,
)

SWID = "AAAA-1111"
LEAGUE = "1734659820"
WENTZ, TRACY, LOCK, BRISSETT, LLOYD, JEUDY = 2573079, 4696981, 3924327, 2578570, 5000, 4241463
BENCH = 20
DEADLINE = "2026-09-16T03:00:00-04:00"
CLEARS_MS = 1789542000000                     # 2026-09-16 03:00 ET as ESPN files it
APPROVED_15_17 = "2026-09-15T19:17:00Z"
POINT_21_13 = "2026-09-16T01:13:00Z"
AT_02_36 = "2026-09-16T06:36:00Z"
AT_03_06 = "2026-09-16T07:06:00Z"
FALLBACK_CHAIN = "claim Carson Wentz and drop Tyrone Tracy Jr. before 03:00 ET; else Drew Lock; else Jacoby Brissett"


def at(value: str) -> pd.Timestamp:
    ts = governor.when(value)
    assert ts is not None
    return ts


def _pool_entry(pid, name, status, team=0):
    return {"id": pid, "status": status, "onTeamId": team,
            "waiverProcessDate": CLEARS_MS if status == "WAIVERS" else 0,
            "player": {"id": pid, "fullName": name, "defaultPositionId": 1, "proTeamId": 16,
                       "injuryStatus": "ACTIVE", "droppable": True, "ownership": {}, "stats": []}}


def _roster_entry(pid, name, slot=BENCH):
    return {"playerId": pid, "lineupSlotId": slot,
            "playerPoolEntry": {"player": {"id": pid, "fullName": name, "defaultPositionId": 1}}}


SETTINGS = {"rosterSettings": {"lineupSlotCounts": {
    "0": 1, "2": 2, "4": 2, "6": 1, "16": 1, "17": 1, "20": 6, "21": 1}}}


@pytest.fixture
def espn(monkeypatch, tmp_path):
    """ESPN as a dict. `wentz` is his status: WAIVERS Tuesday night, FREEAGENT
    after the 03:06 run, ONTEAM 12 from 16:30. `filed` is mTransactions2."""
    monkeypatch.setenv("ESPN_SWID", SWID)
    monkeypatch.setenv("ESPN_S2", "S2-TEST")
    state: dict = {"wentz": ("WAIVERS", 0), "filed": {2: []}, "sent": [],
                   "ledger": tmp_path / "commitments.json"}
    monkeypatch.setattr(commitments, "COMMITMENTS", state["ledger"])

    def fetch_pool(*_a, **_k):
        status, team = state["wentz"]
        return [_pool_entry(WENTZ, "Carson Wentz", status, team),
                _pool_entry(LOCK, "Drew Lock", "FREEAGENT"),
                _pool_entry(BRISSETT, "Jacoby Brissett", "WAIVERS"),
                _pool_entry(TRACY, "Tyrone Tracy Jr.", "ONTEAM", 3),
                _pool_entry(LLOYD, "MarShawn Lloyd", "ONTEAM", 3),
                _pool_entry(JEUDY, "Jerry Jeudy", "ONTEAM", 3)]
    monkeypatch.setattr(pool, "fetch_pool", fetch_pool)

    def payload(*_a, **_k):
        _status, team = state["wentz"]
        theirs = [_roster_entry(WENTZ, "Carson Wentz")] if team == 12 else []
        return {"members": [{"id": "{AAAA-1111}", "firstName": "Will", "lastName": "P"},
                            {"id": "{DDDD-4444}", "firstName": "Dean", "lastName": "H"}],
                "teams": [{"id": 3, "name": "adverse possession", "owners": ["{AAAA-1111}"],
                           "roster": {"entries": [_roster_entry(TRACY, "Tyrone Tracy Jr."),
                                                  _roster_entry(LLOYD, "MarShawn Lloyd"),
                                                  _roster_entry(JEUDY, "Jerry Jeudy")]
                                      + [_roster_entry(100 + i, f"Filler {i}") for i in range(11)]}},
                          {"id": 12, "name": "Post Closing King", "owners": ["{DDDD-4444}"],
                           "roster": {"entries": theirs}}]}
    monkeypatch.setattr(rosters, "fetch_roster_payload", payload)
    monkeypatch.setattr(claims, "fetch_settings", lambda *_a, **_k: SETTINGS)
    monkeypatch.setattr(live, "fetch_league_status", lambda *_a, **_k: {
        "scoringPeriodId": 2, "status": {"latestScoringPeriod": 2}})

    def fake_send(_league_id, _season, body, _swid=None, _espn_s2=None, _post=None):
        state["sent"].append(body)
        executed = body["type"] == "FREEAGENT"
        state["filed"][2].append({"id": f"espn-{len(state['sent'])}", "type": body["type"],
                                  "status": "EXECUTED" if executed else "PENDING", "teamId": 3,
                                  "scoringPeriodId": 2, "processDate": CLEARS_MS,
                                  "items": [dict(i) for i in body["items"]]})
        return {"status": 200, "body": {"memberId": "{AAAA-1111}"}}
    monkeypatch.setattr(lineup_write, "send", fake_send)
    def fetch(_league_id, _season, week=None, _swid=None, _espn_s2=None):
        return {"transactions": list(state["filed"].get(week, []))}
    monkeypatch.setattr(transactions, "fetch_transactions", fetch)
    return state


def approve_wentz():
    """15:17 ET: the approval, written the moment it was given."""
    out = json.loads(server.open_commitment(
        LEAGUE, 2, "claim", DEADLINE, add="Carson Wentz", drop="Tyrone Tracy Jr.",
        fallbacks="Drew Lock, Jacoby Brissett", approved_text=FALLBACK_CHAIN))
    assert out["opened"] is True
    return out["commitment"]["id"]


def controller(now: str, espn, points=()):
    """The governor at `now` with the ledger as it stands: Tracy on the bench,
    no game in progress, waivers at 03:00 ET."""
    rows, err = commitments.load(espn["ledger"])
    assert err is None
    return governor.controller_state(
        now, [], [{"player": "Tyrone Tracy Jr.", "pro_team": "NYG", "started": False,
                   "locked": False, "injury_status": "ACTIVE"}],
        [], CLEARS_MS, list(points), {}, set(), commitments=commitments.open_ones(rows))


def claim(add, drop, cid, dry_run=False):
    return json.loads(server.submit_claim(LEAGUE, 2, add, drop=drop, dry_run=dry_run,
                                          commitment_id=cid))


class TestTuesdayNight:
    def test_15_17_the_approval_makes_the_tick_hot_and_holds_the_gate(self, espn):
        cid = approve_wentz()
        state = controller(APPROVED_15_17, espn)
        # Expected: the open Wentz commitment is live. Forbidden: DEEP_IDLE with
        # engineering allowed, which is what the controller read on 09-16 16:50.
        assert state["mode"] == "HOT" and state["engineering"]["allowed"] is False
        assert state["fantasy_actionable"] == [
            f"commitment {cid}: claim Carson Wentz for Tyrone Tracy Jr. "
            f"(fallbacks: Drew Lock, Jacoby Brissett), due 2026-09-16 03:00 ET"]
        code, lines = commitments.gate(commitments.load(espn["ledger"])[0], now=APPROVED_15_17)
        assert code == commitments.GATE_OPEN and len(lines) == 1

    def test_21_13_the_decision_point_passes_and_the_commitment_does_not(self, espn):
        approve_wentz()
        point = {"at": POINT_21_13, "what": "last check before waivers: final claim order and drop",
                 "teams": ["MIN"]}
        before = controller("2026-09-16T01:12:00Z", espn, points=[point])
        assert before["next_required_attention"]["why"].startswith("decision point")
        after = controller("2026-09-16T01:14:00Z", espn, points=[point])
        # The decision point is gone; the commitment is the attention now.
        assert after["mode"] == "HOT" and after["engineering"]["allowed"] is False
        assert after["fantasy_actionable"][0].startswith("commitment ")
        assert after["next_required_attention"]["at"] == "2026-09-16 03:00 ET"

    def test_02_36_the_send_path_exists_and_the_claim_goes_without_a_second_go(self, espn):
        cid = approve_wentz()
        assert controller(AT_02_36, espn)["mode"] == "HOT"
        # Forbidden result: "there's no send path from here".
        out = claim("Carson Wentz", "Tyrone Tracy Jr.", cid)
        assert out["sent"] is True and out["outcome"] == lineup_write.CONFIRMED
        assert espn["sent"][0]["type"] == "WAIVER"
        assert [i["playerId"] for i in espn["sent"][0]["items"]] == [WENTZ, TRACY]
        assert out["commitment"] == {"id": cid, "status": "confirmed",
                                     "espn_transaction_id": "espn-1"}
        after = controller("2026-09-16T06:40:00Z", espn)
        assert after["fantasy_actionable"] == []
        assert commitments.gate(commitments.load(espn["ledger"])[0])[0] == 0


class TestWednesday:
    def test_03_06_nothing_sent_is_overdue_and_still_hot(self, espn):
        cid = approve_wentz()
        espn["wentz"] = ("FREEAGENT", 0)
        state = controller(AT_03_06, espn)
        # Forbidden: thirteen hours of engineering while the Add button worked.
        assert state["mode"] == "HOT" and state["engineering"]["allowed"] is False
        assert state["fantasy_actionable"] == [
            f"commitment {cid}: claim Carson Wentz for Tyrone Tracy Jr. "
            f"(fallbacks: Drew Lock, Jacoby Brissett), due 2026-09-16 03:00 ET OVERDUE"]
        assert state["next_required_attention"] is None
        assert commitments.gate(commitments.load(espn["ledger"])[0], now=AT_03_06)[0] == 2

    def test_16_30_wentz_gone_the_named_fallback_sends_without_a_new_approval(self, espn):
        cid = approve_wentz()
        espn["wentz"] = ("ONTEAM", 12)
        gone = claim("Carson Wentz", "Tyrone Tracy Jr.", cid)
        assert gone["sent"] is False
        assert gone["refusals"] == ["Carson Wentz is ONTEAM on team 12, not claimable"]
        # Expected: Lock is the second name in Tuesday's chain, so the same
        # approval sends him. Forbidden: asking for another "go".
        out = claim("Drew Lock", "Tyrone Tracy Jr.", cid)
        assert out["sent"] is True and out["outcome"] == lineup_write.CONFIRMED
        assert espn["sent"][-1]["type"] == "FREEAGENT"
        assert out["commitment"]["status"] == "confirmed"
        assert len(commitments.load(espn["ledger"])[0]) == 1, "one approval, no second record"
        assert commitments.gate(commitments.load(espn["ledger"])[0])[0] == 0

    def test_a_drop_the_approval_did_not_name_is_refused(self, espn):
        cid = approve_wentz()
        espn["wentz"] = ("ONTEAM", 12)
        # The stale-read recommendation of 09-16 was Brissett for Lloyd.
        # Brissett is in the chain; Lloyd is not.
        out = claim("Jacoby Brissett", "MarShawn Lloyd", cid)
        assert out["sent"] is False and espn["sent"] == []
        assert out["refusals"] == [
            f"commitment {cid} does not cover player id(s) [{LLOYD}]; it names Carson Wentz, "
            f"Tyrone Tracy Jr., Drew Lock, Jacoby Brissett. A different player is a different "
            f"action: open a new commitment for it"]
        assert commitments.load(espn["ledger"])[0][0]["status"] == "open"

    def test_i_want_wentz_does_not_send_a_trade(self, espn):
        espn["wentz"] = ("ONTEAM", 12)
        out = json.loads(server.propose_trade(LEAGUE, "12", give="Jerry Jeudy",
                                              get="Carson Wentz", dry_run=False))
        assert out["sent"] is False and espn["sent"] == []
        assert out["refusals"] == ["a send needs an open commitment: open_commitment records the "
                                   "approved action, then pass its id as commitment_id"]
        assert out["transaction"]["items"][0]["playerId"] == JEUDY, "the offer was built, not sent"

    def test_a_block_with_a_reason_is_the_other_way_out(self, espn):
        cid = approve_wentz()
        espn["wentz"] = ("ONTEAM", 12)
        out = json.loads(server.block_commitment(cid, "Wentz is on team 12 since 16:30 ET; "
                                                      "Lock sent under 3ee7b612", impossible=True))
        assert out["closed"] is True
        rows = commitments.load(espn["ledger"])[0]
        assert commitments.gate(rows)[0] == 0
        assert controller(AT_03_06, espn)["mode"] != "HOT"
