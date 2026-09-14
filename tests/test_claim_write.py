"""`preview_waiver_claim`: the claim body, checked, with no send path.

Item shape is ESPN's record of this league's processed claims in
mTransactions2 (2026-09-13): ADD fromTeamId 0 toTeamId <team> slot -1 -> 20;
DROP the reverse.
"""
import json

from ffdraft import claim_write, claims, pool, rosters, server

SWID = "AAAA-1111"


def _row(pid, name, status="ONTEAM", on_team=3, injury="ACTIVE", droppable=True):
    return {"espn_id": pid, "player": name, "position": "QB", "pro_team": "ARI",
            "status": status, "on_team_id": on_team,
            "waiver_clears": "2026-09-16 03:00 ET" if status == "WAIVERS" else None,
            "injury_status": injury, "droppable": droppable, "percent_owned": 5.0,
            "percent_change": 0.0, "week_points": None, "week_proj": None,
            "period_injury_status": None}


ROWS = [_row(2578570, "Jacoby Brissett", status="WAIVERS", on_team=0),
        _row(9, "Free Guy", status="FREEAGENT", on_team=0),
        _row(4241463, "Jerry Jeudy"), _row(4248528, "Christian Watson"),
        _row(5, "Held Elsewhere", on_team=7), _row(6, "Josh Allen", on_team=12),
        _row(7, "Josh Jacobs", on_team=14)]
SLOTS = {4241463: 20, 4248528: 4}


class TestParts:
    def test_resolve_exact_substring_ambiguous_missing(self):
        for name in ("jacoby brissett", "brissett"):
            row, why = claim_write.resolve(ROWS, name)
            assert row is not None and why is None
            assert row["espn_id"] == 2578570
        row, why = claim_write.resolve(ROWS, "josh")
        assert row is None and why is not None and "matches 2 players" in why
        assert claim_write.resolve(ROWS, "nobody")[1] == "no player named 'nobody' in ESPN's pool"

    def test_capacity_excludes_injured_reserve(self):
        settings = {"rosterSettings": {"lineupSlotCounts": {
            "0": 1, "2": 2, "4": 2, "6": 1, "16": 1, "17": 1, "20": 6, "21": 1}}}
        assert claim_write.roster_capacity(settings) == 14

    def test_a_bench_drop_for_a_waiver_player_passes(self):
        assert claim_write.check(ROWS[0], ROWS[2], 3, SLOTS, 14, 14) == []

    def test_every_refusal_is_named(self):
        refusals = claim_write.check(ROWS[4], ROWS[3], 3, SLOTS, 14, 14)
        assert refusals == ["Held Elsewhere is ONTEAM on team 7, not claimable",
                            "Christian Watson is in ESPN lineup slot 4, not the bench"]
        assert claim_write.check(ROWS[0], None, 3, SLOTS, 14, 14) == [
            "your roster is full (14 of 14); the claim must name a drop"]
        undroppable = {**ROWS[2], "droppable": False}
        assert claim_write.check(ROWS[0], undroppable, 3, SLOTS, 14, 14) == [
            "Jerry Jeudy is on ESPN's undroppable list"]
        assert claim_write.check(ROWS[0], ROWS[4], 3, SLOTS, 14, 14) == [
            "Held Elsewhere is not on your roster"]

    def test_transaction_mirrors_the_processed_record(self):
        body = claim_write.claim_transaction(3, SWID, 2, ROWS[0], ROWS[2])
        assert body["type"] == "WAIVER" and body["memberId"] == "{AAAA-1111}"
        assert body["items"] == [
            {"playerId": 2578570, "type": "ADD", "fromTeamId": 0, "toTeamId": 3,
             "fromLineupSlotId": -1, "toLineupSlotId": 20},
            {"playerId": 4241463, "type": "DROP", "fromTeamId": 3, "toTeamId": 0,
             "fromLineupSlotId": 20, "toLineupSlotId": -1}]
        assert claim_write.claim_transaction(3, SWID, 2, ROWS[1], None)["type"] == "FREEAGENT"


def _entry(row):
    return {"id": row["espn_id"], "status": row["status"], "onTeamId": row["on_team_id"],
            "waiverProcessDate": 1789542000000 if row["status"] == "WAIVERS" else 0,
            "player": {"id": row["espn_id"], "fullName": row["player"], "defaultPositionId": 1,
                       "proTeamId": 22, "injuryStatus": row["injury_status"],
                       "droppable": row["droppable"], "ownership": {}, "stats": []}}


class TestTool:
    def run(self, monkeypatch, **kw):
        monkeypatch.setenv("ESPN_SWID", SWID)
        payload = {"teams": [{"id": 3, "name": "adverse possession", "owners": ["{AAAA-1111}"],
                              "roster": {"entries": [{"playerId": p, "lineupSlotId": s}
                                                     for p, s in SLOTS.items()]
                                         + [{"playerId": 100 + i, "lineupSlotId": 20}
                                            for i in range(12)]}}]}
        monkeypatch.setattr(rosters, "fetch_roster_payload", lambda *a, **k: payload)
        monkeypatch.setattr(pool, "fetch_pool", lambda *a, **k: [_entry(r) for r in ROWS])
        monkeypatch.setattr(claims, "fetch_settings", lambda *a, **k: {
            "rosterSettings": {"lineupSlotCounts": {"0": 1, "2": 2, "4": 2, "6": 1, "16": 1,
                                                    "17": 1, "20": 6, "21": 1}}})
        return json.loads(server.preview_waiver_claim("123", 2, **kw))

    def test_a_valid_claim_previews_with_the_swid_redacted(self, monkeypatch):
        out = self.run(monkeypatch, add="Brissett", drop="Jeudy")
        assert out["sends"] == "nothing" and out["refusals"] == []
        assert out["transaction"]["memberId"] == "redacted"
        assert out["waiver_clears"] == "2026-09-16 03:00 ET"
        assert "AAAA" not in json.dumps(out)
        assert out["contract_basis"].startswith("UNVERIFIED")

    def test_dropping_a_starter_is_refused(self, monkeypatch):
        out = self.run(monkeypatch, add="Brissett", drop="Christian Watson")
        assert out["refusals"] == ["Christian Watson is in ESPN lineup slot 4, not the bench"]

    def test_no_drop_on_a_full_roster_is_refused(self, monkeypatch):
        out = self.run(monkeypatch, add="Brissett")
        assert out["refusals"] == ["your roster is full (14 of 14); the claim must name a drop"]
