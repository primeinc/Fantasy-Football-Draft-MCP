"""`propose_trade`: the TRADE_PROPOSAL ESPN's web client sends, checked against
the rosters. A dry run and a refusal send nothing; a send is reconciled against
the PENDING proposals read before it into CONFIRMED, REJECTED or
UNKNOWN_AFTER_SEND.

Body and item shape are ESPN's web client (kona 5a90d30cd38d-1.490, 2026-09-15)
and match the league's pending TRADE_PROPOSAL records in mTransactions2.
"""
import json
from datetime import datetime, timezone

import pytest

from ffdraft import (
    claims,
    commitments,
    lineup_write,
    live,
    rosters,
    server,
    trade_write,
    transactions,
)

SWID = "BBBB-2222"
LEDGER: dict = {}   # the fixture's ledger path and the id of the commitment it opened
TEAMS = [{"team_id": 3, "team": "Home Office", "owners": ["Pat Example"]},
         {"team_id": 7, "team": "Gravel Kings", "owners": ["Sam Sample"]},
         {"team_id": 9, "team": "Gravel Queens", "owners": ["Lee Test"]}]
MINE = [{"player": "Arlo Runner", "espn_id": "101", "position": "RB", "slot": "BENCH"},
        {"player": "Bo Passer", "espn_id": "102", "position": "QB", "slot": "QB"},
        {"player": "Bo Catcher", "espn_id": "103", "position": "WR", "slot": "WR"},
        {"player": "Nobody Id", "espn_id": None, "position": "TE", "slot": "BENCH"}]


def _proposal(tid, items, proposer=3, status="PENDING", period=2):
    return {"id": tid, "type": "TRADE_PROPOSAL", "status": status, "teamId": proposer,
            "scoringPeriodId": period, "items": items, "proposedDate": 1789497494107,
            "expirationDate": 1789670292378, "teamActions": {str(proposer): "ACCEPTED"}}


class TestParts:
    def test_partner_by_id_name_or_owner_and_every_refusal(self):
        for key, team_id in (("7", 7), ("kings", 7), ("lee", 9)):
            team, why = trade_write.resolve_team(TEAMS, key, 3)
            assert team is not None and why is None
            assert team["team_id"] == team_id
        assert trade_write.resolve_team(TEAMS, "gravel", 3) == (
            None, "'gravel' matches 2 teams (Gravel Kings, Gravel Queens); name one")
        assert trade_write.resolve_team(TEAMS, "home", 3) == (None, "that is your own team")
        assert trade_write.resolve_team(TEAMS, "zzz", 3) == (None, "no team named 'zzz' in this league")

    def test_players_resolve_on_the_named_roster_only(self):
        rows, why = trade_write.resolve_players(MINE, ["arlo", "Arlo Runner", "Cy", "bo", "Nobody"],
                                                "your roster")
        assert [r["espn_id"] for r in rows] == ["101"]
        assert why == ["Arlo Runner is named twice", "no player named 'Cy' on your roster",
                       "'bo' matches 3 players (Bo Catcher, Bo Passer, Nobody Id); name one",
                       "Nobody Id: ESPN gave no player id"]

    def test_exact_refuses_a_substring_a_send_would_act_on(self):
        rows, why = trade_write.resolve_players(MINE, ["arlo runner", "Passer"], "your roster",
                                                exact=True)
        assert [r["espn_id"] for r in rows] == ["101"]
        assert why == ["'Passer' is not a full name on your roster; a send needs 'Bo Passer'"]

    def test_check_names_capacity_days_and_an_empty_trade(self):
        assert trade_write.check(MINE[:1], MINE[1:2], 14, 14, 2) == []
        assert trade_write.check([], [], 14, 14, 0) == [
            "the trade names no players", "days is 0; ESPN offers 1 through 7"]
        assert trade_write.check(MINE[:1], MINE[1:3], 14, 14, 7) == [
            "your roster would hold 15 of 14; the trade needs a drop, which this tool does not send"]

    def test_expiration_is_moment_json_days_later(self):
        now = datetime(2026, 9, 15, 19, 24, 54, 107000, tzinfo=timezone.utc)
        assert trade_write.expiration(now, 2) == "2026-09-17T19:24:54.107Z"

    def test_the_transaction_is_field_for_field_what_espn_sends(self):
        body = trade_write.trade_transaction(3, 7, SWID, 2, MINE[:1], [MINE[1]],
                                             "2026-09-17T19:24:54.107Z", "hi")
        assert body == {
            "isLeagueManager": False, "teamId": 3, "type": "TRADE_PROPOSAL",
            "memberId": "{BBBB-2222}", "scoringPeriodId": 2, "executionType": "EXECUTE",
            "items": [{"playerId": 101, "type": "TRADE", "fromTeamId": 3, "toTeamId": 7},
                      {"playerId": 102, "type": "TRADE", "fromTeamId": 7, "toTeamId": 3}],
            "expirationDate": "2026-09-17T19:24:54.107Z", "comment": "hi"}

    def test_matching_needs_pending_the_proposer_and_the_exact_moves_across_periods(self):
        body = trade_write.trade_transaction(3, 7, SWID, 2, MINE[:1], [MINE[1]], "x", "")
        moves = [{"playerId": 102, "fromTeamId": 7, "toTeamId": 3, "fromLineupSlotId": 0},
                 {"playerId": 101, "fromTeamId": 3, "toTeamId": 7, "fromLineupSlotId": 20}]
        period2 = {"transactions": [_proposal("a", moves), _proposal("b", moves, status="CANCELED"),
                                    _proposal("c", moves, proposer=7), _proposal("d", moves[:1])]}
        period3 = {"transactions": [_proposal("a", moves), _proposal("e", moves, period=3)]}
        assert [t["id"] for t in trade_write.matching_proposals([period2, period3], body)] == ["a", "e"]
        assert trade_write.matching_proposals([{"transactions": []}], body) == []

    def test_reconcile_confirms_exactly_one_new_id_and_nothing_else(self):
        old, new, other = {"id": "old"}, {"id": "new"}, {"id": "other"}
        assert trade_write.reconcile([old], [old, new]) == (trade_write.CONFIRMED, new)
        assert trade_write.reconcile([old], [old]) == (trade_write.UNKNOWN_AFTER_SEND, None)
        assert trade_write.reconcile([], [new, other]) == (trade_write.UNKNOWN_AFTER_SEND, None)
        assert trade_write.reconcile([], [{"id": None}]) == (trade_write.UNKNOWN_AFTER_SEND, None)

    def test_redact_reaches_every_nested_string(self):
        assert trade_write.redact({"a": ["{bbbb-2222}", 1], "b": "ok"}, SWID) == {
            "a": ["<SWID>", 1], "b": "ok"}


def _entry(pid, name, pos_id, slot):
    return {"playerId": pid, "lineupSlotId": slot,
            "playerPoolEntry": {"player": {"id": pid, "fullName": name,
                                           "defaultPositionId": pos_id}}}


PAYLOAD = {
    "members": [{"id": "{BBBB-2222}", "firstName": "Pat", "lastName": "Example"},
                {"id": "{CCCC-3333}", "firstName": "Sam", "lastName": "Sample"}],
    "teams": [
        {"id": 3, "name": "Home Office", "owners": ["{BBBB-2222}"],
         "roster": {"entries": [_entry(101, "Arlo Runner", 2, 20), _entry(102, "Bo Passer", 1, 0)]
                    + [_entry(200 + i, f"Filler {i}", 3, 20) for i in range(12)]
                    + [_entry(250, "Hurt Reserve", 3, 21)]}},
        {"id": 7, "name": "Gravel Kings", "owners": ["{CCCC-3333}"],
         "roster": {"entries": [_entry(301, "Cy Thrower", 1, 20)]}}]}
SETTINGS = {"rosterSettings": {"lineupSlotCounts": {
    "0": 1, "2": 2, "4": 2, "6": 1, "16": 1, "17": 1, "20": 6, "21": 1}}}
MOVES = [{"playerId": 101, "type": "TRADE", "fromTeamId": 3, "toTeamId": 7},
         {"playerId": 301, "type": "TRADE", "fromTeamId": 7, "toTeamId": 3}]


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """ESPN as a dict: `pending` is what mTransactions2 holds per period, `files`
    whether a 2xx send lands in it and under which period, `status` the answer.
    The ledger holds one open trade commitment: Arlo Runner for Cy Thrower
    with team 7."""
    monkeypatch.setenv("ESPN_SWID", SWID)
    monkeypatch.setenv("ESPN_S2", "S2-TEST")
    LEDGER["path"] = tmp_path / "commitments.json"
    monkeypatch.setattr(commitments, "COMMITMENTS", LEDGER["path"])
    LEDGER["id"] = commitments.open_commitment(
        "L", 2, "trade", "2026-12-01T03:00:00-05:00",
        give=[commitments.player("Arlo Runner", 101)], get=[commitments.player("Cy Thrower", 301)],
        partner_team_id=7, path=LEDGER["path"])["id"]
    monkeypatch.setattr(rosters, "fetch_roster_payload", lambda *a, **k: PAYLOAD)
    monkeypatch.setattr(claims, "fetch_settings", lambda *a, **k: SETTINGS)
    state: dict = {"sent": [], "status": 200, "files": 2, "pending": {2: [], 3: []},
                   "reads": [], "fail_read_after": None, "raises": None,
                   "league": {"scoringPeriodId": 3, "status": {"latestScoringPeriod": 2}}}
    monkeypatch.setattr(live, "fetch_league_status", lambda *a, **k: state["league"])

    def fake_send(league_id, season, payload, swid=None, espn_s2=None, post=None):
        state["sent"].append(payload)
        if not 400 <= state["status"] < 500 and state["files"] is not None:
            state["pending"][state["files"]].append(
                {**_proposal("new", [dict(i) for i in payload["items"]], period=state["files"]),
                 "memberId": "{BBBB-2222}"})
        if state["raises"]:
            raise TimeoutError(state["raises"])
        return {"status": state["status"],
                "body": [{"memberId": "{BBBB-2222}", "status": "PENDING"}]}
    monkeypatch.setattr(lineup_write, "send", fake_send)

    def fetch(league_id, season, week=None, swid=None, espn_s2=None):
        state["reads"].append(week)
        if state["sent"] and state["fail_read_after"]:
            raise RuntimeError(state["fail_read_after"])
        return {"transactions": list(state["pending"].get(week, []))}
    monkeypatch.setattr(transactions, "fetch_transactions", fetch)
    return state


def _send(give="Arlo Runner", get="Cy Thrower", commitment_id=None, **kw):
    cid = LEDGER["id"] if commitment_id is None else commitment_id
    return json.loads(server.propose_trade("L", "7", give=give, get=get, dry_run=False,
                                           commitment_id=cid, **kw))


def test_a_send_without_a_commitment_is_refused(wired):
    out = _send(commitment_id="")
    assert out["sent"] is False and wired["sent"] == []
    assert out["refusals"][0].startswith("a send needs an open commitment")


def test_a_player_the_commitment_does_not_name_is_refused(wired):
    # 2026-09-16: Jeudy for Wentz was built on "i want wentz". The approval
    # named nobody to give, so the offer is refused, not guessed.
    out = _send(give="Bo Passer")
    assert out["sent"] is False and wired["sent"] == []
    assert out["refusals"] == [
        f"commitment {LEDGER['id']} does not cover player id(s) [102]; it names Arlo Runner, "
        f"Cy Thrower. A different player is a different action: open a new commitment for it"]


def test_a_confirmed_send_closes_the_commitment(wired):
    out = _send()
    assert out["outcome"] == "CONFIRMED"
    assert out["commitment"] == {"id": LEDGER["id"], "status": "confirmed",
                                 "espn_transaction_id": "new"}
    assert commitments.load(LEDGER["path"])[0][0]["status"] == "confirmed"


def test_dry_run_builds_the_proposal_reads_pending_and_sends_nothing(wired):
    out = json.loads(server.propose_trade("L", "kings", give="Arlo", get="Cy Thrower"))
    assert out["sent"] is False and "dry run" in out["why_not_sent"]
    assert out["refusals"] == [], "injured reserve does not count against capacity"
    assert out["partner"] == {"team_id": 7, "team": "Gravel Kings", "owners": ["Sam Sample"]}
    assert [r["player"] for r in out["give"]] == ["Arlo Runner"]
    assert out["transaction"]["items"] == MOVES
    assert out["transaction"]["scoringPeriodId"] == 2
    assert out["pending_periods"] == [2, 3] and sorted(wired["reads"]) == [2, 3]
    assert wired["sent"] == []
    assert "BBBB" not in json.dumps(out)


def test_a_refusal_sends_nothing(wired):
    out = json.loads(server.propose_trade("L", "kings", give="Arlo Runner", get="Nobody",
                                          dry_run=False))
    assert out["sent"] is False
    assert out["refusals"] == ["no player named 'Nobody' on Gravel Kings's roster"]
    assert wired["sent"] == []


def test_an_unknown_partner_sends_nothing_and_builds_no_body(wired):
    out = json.loads(server.propose_trade("L", "nobody", give="Arlo Runner", dry_run=False))
    assert out["refusals"] == ["no team named 'nobody' in this league"]
    assert "transaction" not in out and wired["sent"] == []


def test_a_send_by_substring_is_refused(wired):
    out = json.loads(server.propose_trade("L", "7", give="Arlo", get="Cy Thrower", dry_run=False))
    assert out["refusals"] == ["'Arlo' is not a full name on your roster; a send needs 'Arlo Runner'"]
    assert wired["sent"] == []


def test_an_identical_pending_offer_refuses_the_send(wired):
    wired["pending"][3].append(_proposal("old", [dict(i) for i in MOVES], period=3))
    out = _send()
    assert out["sent"] is False and wired["sent"] == []
    assert out["refusals"] == ["an identical offer is already PENDING on ESPN, proposed "
                               "2026-09-15 14:38 ET"]


def test_a_failed_pending_read_refuses_a_send(wired, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("503")
    monkeypatch.setattr(transactions, "fetch_transactions", broken)
    out = _send()
    assert out["sent"] is False and wired["sent"] == []
    assert out["pending_read_error"] == "RuntimeError: 503"
    dry = json.loads(server.propose_trade("L", "7", give="Arlo Runner", get="Cy Thrower"))
    assert "the duplicate check did not run" in dry["why_not_sent"]


@pytest.mark.parametrize("files, outcome", [(2, "CONFIRMED"), (None, "UNKNOWN_AFTER_SEND")])
def test_a_server_error_is_reconciled_not_rejected(wired, files, outcome):
    wired["status"], wired["files"] = 502, files
    out = _send()
    assert out["outcome"] == outcome
    assert ("retry" in out) is (outcome == "UNKNOWN_AFTER_SEND")


@pytest.mark.parametrize("files, outcome", [(3, "CONFIRMED"), (None, "UNKNOWN_AFTER_SEND")])
def test_a_send_that_raises_is_reconciled(wired, files, outcome):
    wired["raises"], wired["files"] = "read timed out {BBBB-2222}", files
    out = _send()
    assert out["sent"] == "unknown" and out["outcome"] == outcome
    assert out["send_error"] == "<SWID>"
    assert ("retry" in out) is (outcome == "UNKNOWN_AFTER_SEND")


def test_a_send_filed_under_another_period_is_confirmed_with_the_swid_redacted(wired):
    wired["files"] = 3
    out = _send(days=3, comment="for you")
    assert out["sent"] is True and len(wired["sent"]) == 1
    body = wired["sent"][0]
    assert body["memberId"] == "{BBBB-2222}"
    assert body["type"] == "TRADE_PROPOSAL" and body["comment"] == "for you"
    assert out["outcome"] == "CONFIRMED" and "retry" not in out
    assert out["espn_holds"] == {"status": "PENDING", "scoring_period": 3,
                                 "proposed": "2026-09-15 14:38 ET",
                                 "expires": "2026-09-17 14:38 ET",
                                 "team_actions": {"3": "ACCEPTED"}}
    assert "BBBB" not in json.dumps(out)


def test_an_espn_error_is_rejected_and_not_read_back(wired):
    wired["status"] = 409
    out = _send()
    assert out["outcome"] == "REJECTED" and out["espn_holds"] is None
    assert sorted(wired["reads"]) == [2, 3], "only the pre-send read"
    assert "BBBB" not in json.dumps(out)


def test_a_2xx_not_found_afterwards_is_unknown_and_says_not_to_retry(wired):
    wired["files"] = None
    out = _send()
    assert out["sent"] is True and out["outcome"] == "UNKNOWN_AFTER_SEND"
    assert out["espn_holds"] is None and out["retry"] == trade_write.NO_RETRY


def test_a_failed_read_after_a_2xx_is_unknown_not_absent(wired):
    wired["fail_read_after"] = "timeout"
    out = _send()
    assert out["outcome"] == "UNKNOWN_AFTER_SEND" and out["retry"] == trade_write.NO_RETRY
    assert out["read_back_error"] == "RuntimeError: timeout"


def test_no_credentials_is_an_error(monkeypatch):
    monkeypatch.delenv("ESPN_SWID", raising=False)
    out = json.loads(server.propose_trade("L", "7", give="x"))
    assert out == {"error": "propose_trade needs ESPN_SWID and ESPN_S2"}
