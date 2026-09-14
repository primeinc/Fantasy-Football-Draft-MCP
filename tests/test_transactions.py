"""`league_transactions`: adds, drops and waiver results from mTransactions2.

The fixture is the live week 1 shape of 2026-09-13 (league 1734659820): a
DRAFT pick, a ROSTER transaction of LINEUP items, a WAIVER processed by
`NightlyLeagueUpdateTaskProcessor` with `processDate`, and a FREEAGENT move
with only `proposedDate`; `memberId` a SWID on manager moves.
"""
import json

from ffdraft import board, pool, server, transactions

SWID = "{D7B05DE8-D939-4D8F-9B31-D122B2CFDD5A}"


def _item(kind, pid, from_team=0, to_team=0):
    return {"type": kind, "playerId": pid, "fromTeamId": from_team, "toTeamId": to_team,
            "fromLineupSlotId": -1, "toLineupSlotId": 20, "isKeeper": False,
            "overallPickNumber": 0}


def _payload():
    return {"scoringPeriodId": 1, "transactions": [
        {"type": "DRAFT", "status": "EXECUTED", "teamId": 12, "bidAmount": 0,
         "executionType": "EXECUTE", "proposedDate": 1788899110699,
         "items": [_item("DRAFT", 4569987, 0, 12)]},
        {"type": "ROSTER", "status": "EXECUTED", "teamId": 8, "bidAmount": 0,
         "executionType": "EXECUTE", "memberId": SWID, "proposedDate": 1789051217096,
         "items": [_item("LINEUP", 4432665), _item("LINEUP", 4429086)]},
        {"type": "WAIVER", "status": "EXECUTED", "teamId": 8, "bidAmount": 0,
         "executionType": "PROCESS", "memberId": "NightlyLeagueUpdateTaskProcessor",
         "processDate": 1789025361004, "proposedDate": 1789025360983,
         "relatedTransactionId": "1650bb58-d178-4e5c-bccf-7015c60a072f",
         "items": [_item("ADD", 4243331, 0, 8), _item("DROP", 4682648, 8, 0)]},
        {"type": "FREEAGENT", "status": "EXECUTED", "teamId": 8, "bidAmount": 0,
         "executionType": "EXECUTE", "memberId": SWID, "proposedDate": 1789051020796,
         "items": [_item("ADD", 4429086, 0, 8), _item("DROP", 4243331, 8, 0)]},
    ]}


class _Resp:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


TEAMS = {8: "Andrea's Closing Team", 12: "Post Closing King"}
PLAYERS = {4243331: "Waiver Back", 4682648: "Cut Receiver", 4429086: "Free Agent End"}
# What the tool's name pull can answer: the lineup and draft movers too, so a wrong
# include flag leaves them as ids and the rows differ.
POOL_NAMES = PLAYERS | {4432665: "Lineup Mover", 4569987: "Draft Pick"}


class TestRows:
    def test_newest_first_with_process_time_for_a_waiver_run(self):
        rows = transactions.transaction_rows(_payload(), TEAMS, PLAYERS)
        assert [r["type"] for r in rows] == ["FREEAGENT", "WAIVER"]
        assert rows[0]["when"] == "2026-09-10 10:37 ET"
        assert rows[1]["when"] == "2026-09-10 03:29 ET"

    def test_moves_name_player_and_teams_with_none_for_free_agency(self):
        waiver = transactions.transaction_rows(_payload(), TEAMS, PLAYERS)[1]
        assert waiver["team"] == "Andrea's Closing Team"
        assert waiver["moves"] == [["ADD", "Waiver Back", None, "Andrea's Closing Team"],
                                   ["DROP", "Cut Receiver", "Andrea's Closing Team", None]]

    def test_lineup_only_and_draft_are_hidden_unless_asked(self):
        rows = transactions.transaction_rows(_payload(), TEAMS, PLAYERS,
                                             include_lineup=True, include_draft=True)
        assert sorted(r["type"] for r in rows) == ["DRAFT", "FREEAGENT", "ROSTER", "WAIVER"]

    def test_a_roster_transaction_with_a_drop_is_never_hidden(self):
        payload = {"transactions": [
            {"type": "ROSTER", "status": "EXECUTED", "teamId": 8, "proposedDate": 1,
             "items": [_item("LINEUP", 1), _item("DROP", 4682648, 8, 0)]}]}
        assert len(transactions.transaction_rows(payload, TEAMS, PLAYERS)) == 1

    def test_an_unknown_id_prints_as_its_id(self):
        row = transactions.transaction_rows(_payload(), {}, {})[1]
        assert row["team"] == "team 8" and row["moves"][0][1] == "player 4243331"

    def test_the_swid_is_never_in_a_row(self):
        rows = transactions.transaction_rows(_payload(), TEAMS, PLAYERS, True, True)
        assert "D7B05DE8" not in json.dumps(rows)

    def test_player_ids_follow_what_is_shown(self):
        assert transactions.player_ids(_payload()) == [4243331, 4429086, 4682648]
        assert transactions.player_ids(_payload(), True, True) == [
            4243331, 4429086, 4432665, 4569987, 4682648]

    def test_no_ids_no_request(self, monkeypatch):
        def get(*a, **k):
            raise AssertionError("no request expected")
        monkeypatch.setattr(board.requests, "get", get)
        assert transactions.fetch_player_names("123", []) == {}

    def test_counts_cover_hidden_types(self):
        assert transactions.type_counts(_payload()) == {
            "DRAFT": 1, "FREEAGENT": 1, "ROSTER": 1, "WAIVER": 1}


class TestTool:
    def run(self, monkeypatch, names_fail=False, unanswered=(), nameless=(), **kw):
        """`unanswered` ids the name pull leaves out; `nameless` ids it returns
        with no fullName."""
        captured: dict = {}

        def fetch(league_id, season, week):
            captured.update(week=week)
            return _payload()

        def directory(*a, **k):
            if names_fail:
                raise RuntimeError("401")
            return {tid: {"name": n, "owners": [], "owner_ids": []} for tid, n in TEAMS.items()}

        def get(url, *, params: dict, headers: dict, **k):
            flt = json.loads(headers["X-Fantasy-Filter"])
            captured.setdefault("name_pulls", []).append({"params": params, "filter": flt})
            wanted = set(flt["players"]["filterIds"]["value"])
            return _Resp({"players": [
                {"id": pid, "player": {} if pid in nameless else {"fullName": n}}
                for pid, n in POOL_NAMES.items() if pid in wanted and pid not in unanswered]})

        def whole_pool(*a, **k):
            raise AssertionError("the whole pool is not pulled for names")

        monkeypatch.setattr(transactions, "fetch_transactions", fetch)
        monkeypatch.setattr(board, "espn_league_directory", directory)
        monkeypatch.setattr(board.requests, "get", get)
        monkeypatch.setattr(pool, "fetch_pool", whole_pool)
        return json.loads(server.league_transactions("123", 1, **kw)), captured

    def test_names_come_from_one_pull_filtered_to_the_moved_ids(self, monkeypatch):
        out, captured = self.run(monkeypatch)
        assert len(captured["name_pulls"]) == 1
        pull = captured["name_pulls"][0]
        assert pull["params"] == {"view": "kona_player_info"}
        # No limit: ESPN rejects a limit without a sort (HTTP 400, probed 2026-09-14).
        assert pull["filter"] == {"players": {"filterIds": {"value": [4243331, 4429086, 4682648]}}}
        assert out["unread"] == {}

    def test_same_rows_as_names_from_the_whole_pool(self, monkeypatch):
        out, _ = self.run(monkeypatch, include_lineup=True, include_draft=True)
        expected = transactions.transaction_rows(_payload(), TEAMS, POOL_NAMES, True, True)
        assert out["transactions"] == json.loads(json.dumps(expected))
        assert "player 4432665" not in json.dumps(out) and out["unread"] == {}

    def test_players_the_pull_leaves_out_are_counted_in_unread(self, monkeypatch):
        out, _ = self.run(monkeypatch, unanswered={4243331})
        assert out["unread"]["player_names"] == (
            "1 of 3 moved players came back without a name; their ids stand in")
        assert out["transactions"][1]["moves"][0][1] == "player 4243331"

    def test_an_entry_without_a_name_stands_in_as_its_id(self, monkeypatch):
        out, _ = self.run(monkeypatch, nameless={4682648})
        assert out["transactions"][1]["moves"][1][1] == "player 4682648"
        assert "None" not in json.dumps(out["transactions"])
        assert out["unread"]["player_names"].startswith("1 of 3 ")

    def test_a_failed_player_name_pull_is_named(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("500")
        self.run(monkeypatch)
        monkeypatch.setattr(board.requests, "get", boom)
        out = json.loads(server.league_transactions("123", 1))
        assert out["unread"]["player_names"] == "RuntimeError: 500"
        assert out["transactions"][0]["moves"][0][1] == "player 4429086"

    def test_lists_the_weeks_moves_with_counts_and_what_is_hidden(self, monkeypatch):
        out, captured = self.run(monkeypatch)
        assert captured["week"] == 1 and out["scoring_period"] == 1
        assert [t["type"] for t in out["transactions"]] == ["FREEAGENT", "WAIVER"]
        assert out["counts"]["DRAFT"] == 1
        assert out["hidden"] == ["lineup-only moves", "draft picks"]
        assert out["unread"] == {} and out["shape"] == transactions.TXN_SHAPE

    def test_team_narrows(self, monkeypatch):
        out, _ = self.run(monkeypatch, team="post closing", include_draft=True)
        assert [t["type"] for t in out["transactions"]] == ["DRAFT"]

    def test_a_failed_name_lookup_is_named_and_ids_stand_in(self, monkeypatch):
        out, _ = self.run(monkeypatch, names_fail=True)
        assert "team_names" in out["unread"]
        assert out["transactions"][0]["team"] == "team 8"

    def test_an_unreadable_transaction_view_is_an_error(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("503")
        monkeypatch.setattr(transactions, "fetch_transactions", boom)
        out = json.loads(server.league_transactions("123", 1))
        assert "transactions" not in out
        assert out["error"] == "could not read ESPN's transactions: RuntimeError: 503"
