"""`improve`: the queue, the lease, the risk classes, and promotion."""
import json

import pytest

from ffdraft import improve


def item(id_="q1", risk="A", minutes=20, status="open", scope=("tests/test_pool.py",), deps=()):
    return {"id": id_, "title": id_, "evidence": "e", "scope": list(scope), "defect": "d",
            "estimated_minutes": minutes, "risk_class": risk, "acceptance_test": "t",
            "dependencies": list(deps), "discovered_by": "oracle", "last_attempt": None,
            "status": status}


IDLE = {"mode": "IDLE", "engineering": {"allowed": True, "max_minutes": 35,
                                        "max_risk_class": "A"}}
AT = "2026-09-13T22:00:00+00:00"


class TestQueue:
    def test_load_names_bad_lines_and_keeps_good_ones(self, tmp_path):
        path = tmp_path / "q.jsonl"
        path.write_text(json.dumps(item()) + "\n{not json\n" + json.dumps({"id": "x"}) + "\n"
                        + json.dumps(item("q2", status="weird")) + "\n"
                        + json.dumps(item("../q3")) + "\n", encoding="utf-8")
        items, errors = improve.load(path)
        assert [i["id"] for i in items] == ["q1"]
        assert len(errors) == 4 and errors[1].startswith("line 3: missing")
        assert errors[3] == "line 5: id '../q3' is not [a-z0-9-_]"

    def test_a_missing_queue_is_an_error_not_an_empty_queue(self, tmp_path):
        assert improve.load(tmp_path / "none.jsonl")[1]

    def test_save_round_trips(self, tmp_path):
        path = tmp_path / "q.jsonl"
        improve.save([item(), item("q2")], path)
        assert [i["id"] for i in improve.load(path)[0]] == ["q1", "q2"]


class TestRisk:
    def test_a_protected_path_makes_any_item_class_c(self):
        assert improve.effective_risk(item(scope=["src/ffdraft/governor.py"])) == "C"
        assert improve.effective_risk(item(scope=["./.claude/agents/fixer.md"])) == "C"
        assert improve.effective_risk(item(scope=[".venv/Lib/x.py"])) == "C"
        # Code `just check` runs: a fixer editing these runs anything as the user.
        assert improve.effective_risk(item(scope=["tests/conftest.py"])) == "C"
        assert improve.effective_risk(item(scope=["justfile"])) == "C"
        # The write tools' dry-run defaults and send gates live here.
        assert improve.effective_risk(item(risk="A", scope=["src/ffdraft/server.py"])) == "C"
        assert improve.effective_risk(item(risk="B", scope=["src/ffdraft/pool.py"])) == "B"

    def test_an_unknown_class_is_c(self):
        assert improve.effective_risk(item(risk="Z")) == "C"

    def test_pick_respects_minutes_class_dependencies_and_order(self):
        items = [item("big", minutes=90), item("b", risk="B"), item("blocked", deps=["b"]),
                 item("c", scope=["CLAUDE.md"]), item("ok"), item("later")]
        picks = [improve.pick(items, 35, "A"), improve.pick(items, 35, "B"),
                 improve.pick(items, 120, "A")]
        assert [p and p["id"] for p in picks] == ["ok", "b", "big"]
        assert improve.pick(items, 35, None) is None
        assert improve.pick(items, 0, "A") is None
        assert improve.pick([item("c", scope=["CLAUDE.md"])], 120, "B") is None


class TestLease:
    def test_acquire_and_release_write_the_lease_and_the_run(self, tmp_path):
        lease, runs = tmp_path / "lease.json", tmp_path / "runs.jsonl"
        got = improve.acquire(item(), IDLE, AT, lease)
        assert got["expires_utc"] == "2026-09-13T22:35:00+00:00"
        with pytest.raises(improve.LeaseRefused, match="held until"):
            improve.acquire(item("q2"), IDLE, AT, lease)
        record = improve.release("parked: tests green", "2026-09-13T22:30:00+00:00", lease, runs)
        assert record["outcome"] == "parked: tests green" and not lease.exists()
        assert json.loads(runs.read_text().splitlines()[0])["item_id"] == "q1"

    def test_an_expired_lease_does_not_block(self, tmp_path):
        lease = tmp_path / "lease.json"
        improve.acquire(item(), IDLE, AT, lease)
        assert improve.acquire(item("q2"), IDLE, "2026-09-13T23:00:00+00:00", lease)["item_id"] == "q2"

    def test_no_lease_outside_idle_or_beyond_the_allowance(self, tmp_path):
        lease = tmp_path / "lease.json"
        watch = {"mode": "WATCH", "engineering": {"allowed": False, "max_minutes": 0,
                                                  "max_risk_class": None}}
        with pytest.raises(improve.LeaseRefused, match="WATCH"):
            improve.acquire(item(), watch, AT, lease)
        with pytest.raises(improve.LeaseRefused, match="does not fit"):
            improve.acquire(item(minutes=60), IDLE, AT, lease)
        with pytest.raises(improve.LeaseRefused, match="does not fit"):
            improve.acquire(item(risk="B"), IDLE, AT, lease)
        with pytest.raises(improve.LeaseRefused, match="no lease"):
            improve.release("x", AT, lease, tmp_path / "runs.jsonl")


class TestPromotion:
    def test_only_class_a_with_every_gate_promotes(self):
        gates = {g: True for g in improve.PROMOTION_GATES}
        assert improve.promotion(item(), gates)["promote"] is True
        assert improve.promotion(item(), {**gates, "still_idle": False}) == {
            "promote": False, "why": "gates not passed: still_idle"}
        assert improve.promotion(item(risk="B"), gates)["promote"] is False
        assert improve.promotion(item(scope=["src/ffdraft/claim_write.py"]), gates)["promote"] is False


def test_the_committed_queue_is_valid():
    items, errors = improve.load()
    assert errors == [] and items
    assert len({i["id"] for i in items}) == len(items)
