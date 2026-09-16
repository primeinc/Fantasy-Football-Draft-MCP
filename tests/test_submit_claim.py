"""`submit_claim`: the claim ESPN's client sends, checked and then reconciled.

A dry run and a refusal send nothing. A send is settled against the claims read
before it -- CONFIRMED, REJECTED or UNKNOWN_AFTER_SEND -- and never against a
local clock, so a claim ESPN filed under another scoring period is still found.

Record shape is ESPN's mTransactions2 for this league (2026-09-13): a WAIVER
claim is PENDING until the league's waiver time, an outright FREEAGENT add is
EXECUTED when it is made.
"""
import json

import pytest

from ffdraft import (
    claims,
    commitments,
    lineup_write,
    live,
    pool,
    rosters,
    server,
    trade_write,
    transactions,
)

SWID = "AAAA-1111"
BENCH, WR_SLOT = 20, 4
DEADLINE = "2026-12-01T03:00:00-05:00"
LEDGER: dict = {}   # the fixture's ledger path and the id of the commitment it opened


def _row(pid, name, status="ONTEAM", on_team=3, injury="ACTIVE", droppable=True):
    return {"espn_id": pid, "player": name, "position": "QB", "pro_team": "ARI",
            "status": status, "on_team_id": on_team,
            "waiver_clears": "2026-09-16 03:00 ET" if status == "WAIVERS" else None,
            "injury_status": injury, "droppable": droppable, "percent_owned": 5.0,
            "percent_change": 0.0, "week_points": None, "week_proj": None,
            "period_injury_status": None}


ROWS = [_row(2578570, "Jacoby Brissett", status="WAIVERS", on_team=0),
        _row(9, "Free Guy", status="FREEAGENT", on_team=0),
        _row(4241463, "Jerry Jeudy"), _row(4248528, "Christian Watson")]
SLOTS = {4241463: BENCH, 4248528: WR_SLOT}
SETTINGS = {"rosterSettings": {"lineupSlotCounts": {
    "0": 1, "2": 2, "4": 2, "6": 1, "16": 1, "17": 1, "20": 6, "21": 1}}}
ADD_ITEM = {"playerId": 2578570, "type": "ADD", "fromTeamId": 0, "toTeamId": 3,
            "fromLineupSlotId": -1, "toLineupSlotId": BENCH}
DROP_ITEM = {"playerId": 4241463, "type": "DROP", "fromTeamId": 3, "toTeamId": 0,
             "fromLineupSlotId": BENCH, "toLineupSlotId": -1}


def _pool_entry(row):
    return {"id": row["espn_id"], "status": row["status"], "onTeamId": row["on_team_id"],
            "waiverProcessDate": 1789542000000 if row["status"] == "WAIVERS" else 0,
            "player": {"id": row["espn_id"], "fullName": row["player"], "defaultPositionId": 1,
                       "proTeamId": 22, "injuryStatus": row["injury_status"],
                       "droppable": row["droppable"], "ownership": {}, "stats": []}}


def _claim(tid, items, status="PENDING", kind="WAIVER", period=2, when=1789542000000):
    return {"id": tid, "type": kind, "status": status, "teamId": 3,
            "scoringPeriodId": period, "processDate": when,
            "items": [dict(i) for i in items]}


# Twelve bench fillers plus the two slotted players fill the 14-spot roster, so
# a claim with no drop is refused and one with a bench drop is not.
PAYLOAD = {"teams": [{"id": 3, "name": "adverse possession", "owners": ["{AAAA-1111}"],
                      "roster": {"entries": [{"playerId": p, "lineupSlotId": s}
                                             for p, s in SLOTS.items()]
                                 + [{"playerId": 100 + i, "lineupSlotId": BENCH}
                                    for i in range(12)]}}]}


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """ESPN as a dict: `filed` is what mTransactions2 holds per period, `files`
    the period a 2xx send lands in (None for nowhere), `status` the answer.
    The ledger holds one open claim commitment: Brissett for Jeudy, Free Guy
    as the named fallback."""
    monkeypatch.setenv("ESPN_SWID", SWID)
    monkeypatch.setenv("ESPN_S2", "S2-TEST")
    LEDGER["path"] = tmp_path / "commitments.json"
    monkeypatch.setattr(commitments, "COMMITMENTS", LEDGER["path"])
    LEDGER["id"] = commitments.open_commitment(
        "L", 2, "claim", DEADLINE, add=commitments.player("Jacoby Brissett", 2578570),
        drop=commitments.player("Jerry Jeudy", 4241463),
        fallbacks=[commitments.player("Free Guy", 9)], path=LEDGER["path"])["id"]
    monkeypatch.setattr(rosters, "fetch_roster_payload", lambda *_a, **_k: PAYLOAD)
    monkeypatch.setattr(pool, "fetch_pool", lambda *_a, **_k: [_pool_entry(r) for r in ROWS])
    monkeypatch.setattr(claims, "fetch_settings", lambda *_a, **_k: SETTINGS)
    state: dict = {"sent": [], "status": 200, "files": 2, "filed": {2: [], 3: []},
                   "reads": [], "fail_read_after": None, "raises": None,
                   "league": {"scoringPeriodId": 2, "status": {"latestScoringPeriod": 3}}}
    monkeypatch.setattr(live, "fetch_league_status", lambda *_a, **_k: state["league"])

    def fake_send(_league_id, _season, payload, _swid=None, _espn_s2=None, _post=None):
        state["sent"].append(payload)
        if not 400 <= state["status"] < 500 and state["files"] is not None:
            executed = payload["type"] == "FREEAGENT"
            state["filed"][state["files"]].append(_claim(
                "new", payload["items"], kind=payload["type"], period=state["files"],
                status="EXECUTED" if executed else "PENDING"))
        if state["raises"]:
            raise TimeoutError(state["raises"])
        return {"status": state["status"], "body": [{"memberId": "{AAAA-1111}"}]}
    monkeypatch.setattr(lineup_write, "send", fake_send)

    def fetch(_league_id, _season, week=None, _swid=None, _espn_s2=None):
        state["reads"].append(week)
        if state["sent"] and state["fail_read_after"]:
            raise RuntimeError(state["fail_read_after"])
        return {"transactions": list(state["filed"].get(week, []))}
    monkeypatch.setattr(transactions, "fetch_transactions", fetch)
    return state


def run(add="Jacoby Brissett", drop="Jerry Jeudy", dry_run=True, week=2, commitment_id=None):
    cid = LEDGER["id"] if commitment_id is None else commitment_id
    return json.loads(server.submit_claim("L", week, add, drop=drop, dry_run=dry_run,
                                          commitment_id=cid))


def ledger():
    return commitments.load(LEDGER["path"])[0]


def test_a_dry_run_builds_the_claim_reads_what_espn_holds_and_sends_nothing(wired):
    out = run(add="Brissett", drop="Jeudy")
    assert out["sent"] is False and "dry run" in out["why_not_sent"]
    assert out["refusals"] == [] and out["warnings"] == []
    assert out["transaction"]["type"] == "WAIVER"
    assert out["transaction"]["items"] == [ADD_ITEM, DROP_ITEM]
    assert out["transaction"]["memberId"] == "<SWID>"
    assert out["waiver_clears"] == "2026-09-16 03:00 ET"
    # Every period the league names, plus the week asked for.
    assert out["claim_periods"] == [2, 3] and sorted(wired["reads"]) == [2, 3]
    assert wired["sent"] == []
    assert "AAAA" not in json.dumps(out)


def test_a_refusal_sends_nothing(wired):
    out = run(drop="Christian Watson", dry_run=False)
    assert out["refusals"] == ["Christian Watson is in ESPN lineup slot 4, not the bench"]
    assert out["sent"] is False and wired["sent"] == []


def test_a_full_roster_with_no_drop_is_refused(wired):
    out = run(drop="", dry_run=False)
    assert out["refusals"] == ["your roster is full (14 of 14); the claim must name a drop"]
    assert wired["sent"] == []


def test_a_send_by_substring_is_refused(wired):
    out = run(add="Brissett", dry_run=False)
    assert out["refusals"] == ["add 'Brissett' is not a full name; a send needs 'Jacoby Brissett'"]
    assert wired["sent"] == []


def test_an_identical_pending_claim_refuses_the_send(wired):
    wired["filed"][3].append(_claim("old", [ADD_ITEM, DROP_ITEM], period=3))
    out = run(dry_run=False)
    assert out["refusals"] == ["an identical claim is already PENDING on ESPN"]
    assert out["espn_already_holds"] == [{"status": "PENDING", "scoring_period": 3,
                                          "when": "2026-09-16 03:00 ET"}]
    assert wired["sent"] == []


def test_an_executed_claim_for_the_same_players_does_not_refuse(wired):
    # Last week's processed claim is not this week's duplicate.
    wired["filed"][2].append(_claim("done", [ADD_ITEM, DROP_ITEM], status="EXECUTED"))
    out = run(dry_run=False)
    assert out["refusals"] == [] and out["sent"] is True
    assert out["outcome"] == lineup_write.CONFIRMED


def test_a_failed_read_refuses_a_send_and_says_so_in_a_dry_run(wired, monkeypatch):
    def broken(*_a, **_k):
        raise RuntimeError("503")
    monkeypatch.setattr(transactions, "fetch_transactions", broken)
    out = run(dry_run=False)
    assert out["sent"] is False and wired["sent"] == []
    assert out["claim_read_error"] == "RuntimeError: 503"
    assert "could not be reconciled" in out["why_not_sent"]
    dry = run()
    assert "the duplicate check did not run" in dry["why_not_sent"]


def test_an_unreadable_scoring_period_refuses_a_send(wired, monkeypatch):
    def broken(*_a, **_k):
        raise RuntimeError("mStatus 500")
    monkeypatch.setattr(live, "fetch_league_status", broken)
    out = run(dry_run=False)
    assert out["sent"] is False and wired["sent"] == []
    assert "could not read the league's scoring periods" in out["why_not_sent"]


def test_a_sent_claim_espn_holds_is_confirmed(wired):
    out = run(dry_run=False)
    assert out["sent"] is True and len(wired["sent"]) == 1
    assert wired["sent"][0]["memberId"] == "{AAAA-1111}"
    assert out["outcome"] == lineup_write.CONFIRMED and "retry" not in out
    assert out["espn_holds"] == {"status": "PENDING", "scoring_period": 2,
                                 "when": "2026-09-16 03:00 ET", "bid": None}
    assert "AAAA" not in json.dumps(out)


def test_an_outright_add_is_a_freeagent_claim_espn_executes(wired):
    out = run(add="Free Guy", dry_run=False)
    assert wired["sent"][0]["type"] == "FREEAGENT"
    assert out["waiver_clears"] is None
    assert out["outcome"] == lineup_write.CONFIRMED
    assert out["espn_holds"]["status"] == "EXECUTED"


def test_a_claim_filed_under_another_period_is_still_confirmed(wired):
    wired["files"] = 3
    out = run(dry_run=False)
    assert out["outcome"] == lineup_write.CONFIRMED
    assert out["espn_holds"]["scoring_period"] == 3


def test_an_espn_error_is_rejected_and_not_read_back(wired):
    wired["status"] = 409
    out = run(dry_run=False)
    assert out["outcome"] == lineup_write.REJECTED and out["espn_holds"] is None
    assert sorted(wired["reads"]) == [2, 3], "only the pre-send read"


@pytest.mark.parametrize("files, outcome", [(2, "CONFIRMED"), (None, "UNKNOWN_AFTER_SEND")])
def test_a_server_error_is_reconciled_not_rejected(wired, files, outcome):
    wired["status"], wired["files"] = 502, files
    out = run(dry_run=False)
    assert out["outcome"] == outcome
    assert ("retry" in out) is (outcome == "UNKNOWN_AFTER_SEND")


@pytest.mark.parametrize("files, outcome", [(3, "CONFIRMED"), (None, "UNKNOWN_AFTER_SEND")])
def test_a_send_that_raises_is_reconciled(wired, files, outcome):
    wired["raises"], wired["files"] = "read timed out {AAAA-1111}", files
    out = run(dry_run=False)
    assert out["sent"] == "unknown" and out["outcome"] == outcome
    assert out["send_error"] == "<SWID>"
    assert ("retry" in out) is (outcome == "UNKNOWN_AFTER_SEND")


def test_a_2xx_nothing_holds_afterwards_is_unknown_and_says_not_to_retry(wired):
    wired["files"] = None
    out = run(dry_run=False)
    assert out["sent"] is True and out["outcome"] == lineup_write.UNKNOWN_AFTER_SEND
    assert out["espn_holds"] is None and out["retry"] == lineup_write.NO_RETRY


def test_a_failed_read_after_a_2xx_is_unknown_not_absent(wired):
    wired["fail_read_after"] = "timeout"
    out = run(dry_run=False)
    assert out["outcome"] == lineup_write.UNKNOWN_AFTER_SEND
    assert out["retry"] == lineup_write.NO_RETRY
    assert out["read_back_error"] == "RuntimeError: timeout"


def test_a_send_without_a_commitment_is_refused(wired):
    # 2026-09-16: a Jeudy-for-Wentz offer was nearly sent on an inferred
    # approval. Nothing is sent under no commitment.
    out = run(dry_run=False, commitment_id="")
    assert out["sent"] is False and wired["sent"] == []
    assert out["refusals"] == ["a send needs an open commitment: open_commitment records the "
                               "approved action, then pass its id as commitment_id"]
    assert out["commitment"] is None
    dry = run(commitment_id="")
    assert dry["refusals"] == [] and "dry run" in dry["why_not_sent"]


def test_a_player_the_commitment_does_not_name_is_refused(wired):
    other = commitments.open_commitment(
        "L", 2, "claim", DEADLINE, add=commitments.player("Free Guy", 9),
        drop=commitments.player("Jerry Jeudy", 4241463), path=LEDGER["path"])
    out = run(dry_run=False, commitment_id=other["id"])
    assert out["sent"] is False and wired["sent"] == []
    assert out["refusals"] == [
        f"commitment {other['id']} does not cover player id(s) [2578570]; it names Free Guy, "
        f"Jerry Jeudy. A different player is a different action: open a new commitment for it"]


def test_a_named_fallback_sends_without_a_new_approval(wired):
    # Tuesday's chain was Wentz, then Lock, then Brissett. The second name in
    # the chain is the same approval, not a new question.
    out = run(add="Free Guy", dry_run=False)
    assert out["sent"] is True and out["outcome"] == lineup_write.CONFIRMED
    assert out["commitment"] == {"id": LEDGER["id"], "status": "confirmed",
                                 "espn_transaction_id": "new"}


def test_a_confirmed_send_closes_the_commitment_once(wired):
    out = run(dry_run=False)
    assert out["commitment"]["status"] == "confirmed"
    assert ledger()[0]["confirmed"]["espn_transaction_id"] == "new"
    # The fallback name, so the duplicate check stays quiet and the closed
    # commitment is what refuses.
    again = run(add="Free Guy", dry_run=False)
    assert again["sent"] is False and len(wired["sent"]) == 1
    assert again["refusals"] == [f"commitment {LEDGER['id']} is confirmed, not open"]


@pytest.mark.parametrize("status, files", [(409, 2), (200, None)])
def test_an_unconfirmed_send_leaves_the_commitment_open(wired, status, files):
    wired["status"], wired["files"] = status, files
    out = run(dry_run=False)
    assert out["outcome"] in (lineup_write.REJECTED, lineup_write.UNKNOWN_AFTER_SEND)
    assert out["commitment"]["status"] == "open"
    assert ledger()[0]["status"] == "open"


def test_a_commitment_for_another_week_league_or_kind_is_refused(wired):
    out = run(dry_run=False, week=3)
    assert out["refusals"] == [f"commitment {LEDGER['id']} is for week 2, not 3"]
    out = json.loads(server.submit_claim("M", 2, "Jacoby Brissett", drop="Jerry Jeudy",
                                         dry_run=False, commitment_id=LEDGER["id"]))
    assert out["refusals"] == [f"commitment {LEDGER['id']} is for league L, not M"]
    out = run(dry_run=False, commitment_id="nope")
    assert out["refusals"] == ["no commitment 'nope' in commitments.json"]


def test_the_trade_write_outcome_words_are_the_shared_ones():
    # One vocabulary for every write, not a copy per module.
    assert trade_write.CONFIRMED is lineup_write.CONFIRMED
    assert trade_write.reconcile is lineup_write.reconcile_new_id


def test_no_credentials_is_an_error(monkeypatch):
    monkeypatch.delenv("ESPN_SWID", raising=False)
    monkeypatch.delenv("ESPN_S2", raising=False)
    out = json.loads(server.submit_claim("L", 2, "Jacoby Brissett"))
    assert out == {"error": "submit_claim needs ESPN_SWID and ESPN_S2"}
