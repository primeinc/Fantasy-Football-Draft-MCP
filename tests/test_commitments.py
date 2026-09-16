"""`commitments`: an approved action stays open until it is confirmed, blocked,
cancelled or impossible, and the Stop gate refuses to close a turn over one.

Times are the Tuesday the store exists because of: 2026-09-15 15:17 ET the
Wentz claim was approved, due 2026-09-16 03:00 ET.
"""
import json

import pandas as pd
import pytest

from ffdraft import commitments, governor

OPENED = "2026-09-15T19:17:00Z"      # 15:17 ET
DEADLINE = "2026-09-16T03:00:00-04:00"
WENTZ = commitments.player("Carson Wentz", 2573079)
TRACY = commitments.player("Tyrone Tracy Jr.", "4696981")
LOCK = commitments.player("Drew Lock", 3924327)
BRISSETT = commitments.player("Jacoby Brissett", 2578570)


@pytest.fixture
def store(tmp_path):
    return tmp_path / "commitments.json"


def wentz(store):
    return commitments.open_commitment(
        "1734659820", 2, "claim", DEADLINE, add=WENTZ, drop=TRACY, fallbacks=[LOCK, BRISSETT],
        approved_text="claim Carson Wentz and drop Tyrone Tracy Jr. before 03:00 ET",
        now=OPENED, path=store)


def at(value: str) -> pd.Timestamp:
    ts = governor.when(value)
    assert ts is not None
    return ts


class TestOpening:
    def test_a_claim_is_written_open_with_its_ids_and_the_users_words(self, store):
        row = wentz(store)
        on_disk = json.loads(store.read_text(encoding="utf-8"))
        assert on_disk == [row]
        assert row["status"] == "open" and len(row["id"]) == 8
        assert row["add"] == {"name": "Carson Wentz", "espn_id": 2573079}
        assert row["drop"]["espn_id"] == 4696981
        assert [f["name"] for f in row["fallbacks"]] == ["Drew Lock", "Jacoby Brissett"]
        assert row["deadline"] == "2026-09-16T07:00:00+00:00"
        assert row["opened_at"] == "2026-09-15T19:17:00+00:00"
        assert row["approved_text"].startswith("claim Carson Wentz")

    def test_a_deadline_without_a_zone_is_refused(self, store):
        with pytest.raises(ValueError, match="UTC offset"):
            commitments.open_commitment("L", 2, "claim", "2026-09-16T03:00:00",
                                        add=WENTZ, path=store)

    def test_the_kind_and_its_required_parts_are_checked(self, store):
        with pytest.raises(ValueError, match="not one of"):
            commitments.open_commitment("L", 2, "waiver", DEADLINE, add=WENTZ, path=store)
        with pytest.raises(ValueError, match="names the player to add"):
            commitments.open_commitment("L", 2, "claim", DEADLINE, path=store)
        with pytest.raises(ValueError, match="partner team"):
            commitments.open_commitment("L", 2, "trade", DEADLINE, give=[TRACY], path=store)
        with pytest.raises(ValueError, match="not an integer"):
            commitments.player("Nobody", "abc")

    def test_a_lineup_commitment_needs_no_players(self, store):
        row = commitments.open_commitment("L", 2, "lineup", DEADLINE, path=store)
        assert row["status"] == "open" and commitments.allowed_ids(row) == set()


class TestReading:
    def test_a_missing_file_is_no_commitments_and_garbage_is_an_error(self, store):
        assert commitments.load(store) == ([], None)
        store.write_text("{not json", encoding="utf-8")
        rows, err = commitments.load(store)
        assert rows == [] and err is not None and err.startswith("JSONDecodeError")
        store.write_text("{}", encoding="utf-8")
        assert commitments.load(store)[1] == "commitments.json is not a JSON list"

    def test_an_unreadable_file_refuses_a_write_rather_than_replacing_it(self, store):
        store.write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError, match="unreadable"):
            wentz(store)
        assert store.read_text(encoding="utf-8") == "{not json"

    def test_a_passed_deadline_leaves_the_commitment_open(self, store):
        # 03:06 ET Wednesday: waivers ran, nothing was sent. The record is
        # overdue, not gone -- the opposite of governor.py's decision points.
        row = wentz(store)
        rows, _ = commitments.load(store)
        assert commitments.open_ones(rows) == [row]
        text = governor.commitment_text(row, at("2026-09-16T07:06:00Z"))
        assert text == (f"commitment {row['id']}: claim Carson Wentz for Tyrone Tracy Jr. "
                        f"(fallbacks: Drew Lock, Jacoby Brissett), due 2026-09-16 03:00 ET OVERDUE")
        before = governor.commitment_text(row, at("2026-09-16T06:36:00Z"))
        assert before.endswith("due 2026-09-16 03:00 ET")

    def test_allowed_ids_are_the_add_the_drop_and_every_fallback(self, store):
        assert commitments.allowed_ids(wentz(store)) == {2573079, 4696981, 3924327, 2578570}
        trade = commitments.open_commitment("L", 2, "trade", DEADLINE, give=[TRACY], get=[WENTZ],
                                            partner_team_id=12, path=store)
        assert commitments.allowed_ids(trade) == {4696981, 2573079}
        assert governor.commitment_text(trade, at(OPENED)) == (
            f"commitment {trade['id']}: trade Tyrone Tracy Jr. for Carson Wentz with team 12, "
            f"due 2026-09-16 03:00 ET")


class TestClosing:
    def test_confirm_records_espn_id_and_closes_once(self, store):
        row = wentz(store)
        done = commitments.confirm(row["id"], "3ee7b612", now="2026-09-16T20:43:00Z", path=store)
        assert done["status"] == "confirmed"
        assert done["confirmed"] == {"espn_transaction_id": "3ee7b612",
                                     "at": "2026-09-16T20:43:00+00:00"}
        assert commitments.open_ones(commitments.load(store)[0]) == []
        with pytest.raises(ValueError, match="is confirmed, not open"):
            commitments.confirm(row["id"], "again", path=store)

    def test_block_cancel_and_impossible_each_name_their_reason(self, store):
        a, b, c = wentz(store), wentz(store), wentz(store)
        assert commitments.block(a["id"], "ESPN 503 on send", path=store)["blocked"]["reason"] == \
            "ESPN 503 on send"
        assert commitments.cancel(b["id"], "user", path=store)["cancelled"]["by"] == "user"
        assert commitments.mark_impossible(c["id"], "Wentz is ONTEAM 12", path=store)[
            "impossible"]["why"] == "Wentz is ONTEAM 12"
        with pytest.raises(ValueError, match="names its reason"):
            commitments.block(a["id"], "  ", path=store)
        with pytest.raises(ValueError, match="no commitment 'zzz'"):
            commitments.cancel("zzz", "user", path=store)


class TestGate:
    def test_open_commitments_hold_the_gate_and_terminal_ones_release_it(self, store):
        assert commitments.gate([]) == (0, [])
        row = wentz(store)
        code, lines = commitments.gate(commitments.load(store)[0], now="2026-09-16T06:36:00Z")
        assert code == commitments.GATE_OPEN
        assert lines == [f"BLOCKED-UNTIL-RESOLVED: commitment {row['id']}: claim Carson Wentz "
                         f"for Tyrone Tracy Jr. (fallbacks: Drew Lock, Jacoby Brissett), "
                         f"due 2026-09-16 03:00 ET"]
        commitments.block(row["id"], "ESPN 503", path=store)
        assert commitments.gate(commitments.load(store)[0]) == (0, [])

    def test_the_module_entry_point_is_the_hook_command(self, store, monkeypatch, capsys):
        monkeypatch.setattr(commitments, "COMMITMENTS", store)
        assert commitments.main(["gate"]) == 0
        wentz(store)
        assert commitments.main(["gate"], '{"stop_hook_active": false}') == commitments.GATE_OPEN
        err = capsys.readouterr().err
        assert err.startswith("BLOCKED-UNTIL-RESOLVED: commitment ")
        assert commitments.EXITS in err
        store.write_text("{not json", encoding="utf-8")
        assert commitments.main(["gate"]) == commitments.GATE_OPEN
        assert "unreadable" in capsys.readouterr().err
        assert commitments.main([]) == 1

    def test_the_second_stop_of_a_turn_is_allowed_with_the_commitments_still_printed(
            self, store, monkeypatch, capsys):
        # A hook that keeps exiting 2 loops the session until it is killed.
        monkeypatch.setattr(commitments, "COMMITMENTS", store)
        wentz(store)
        assert commitments.main(["gate"], '{"stop_hook_active": true}') == 0
        err = capsys.readouterr().err
        assert err.startswith("BLOCKED-UNTIL-RESOLVED: commitment ")
        assert "stop allowed: this gate already blocked once this turn" in err
        assert commitments.main(["gate"], "not json") == commitments.GATE_OPEN
