"""`runner.tick`: the governor decides, fantasy preempts, one leased item runs in worktrees."""
import json
import subprocess

import pytest

from ffdraft import improve, runner

AT = "2026-09-13T22:00:00+00:00"
ITEM = {"id": "q-a", "title": "t", "evidence": "e", "scope": ["tests/test_pool.py"], "defect": "d",
        "estimated_minutes": 20, "risk_class": "A", "acceptance_test": "a", "dependencies": [],
        "discovered_by": "x", "last_attempt": None, "status": "open"}


def state(mode, allowed=False, actionable=()):
    return json.dumps({"mode": mode, "week": 1, "as_of": "t", "fantasy_actionable": list(actionable),
                       "degraded_capabilities": {"scoreboard": "503"} if mode == "DEGRADED" else {},
                       "engineering": {"allowed": allowed, "max_minutes": 35 if allowed else 0,
                                       "max_risk_class": "A" if allowed else None}})


class FakeRun:
    def __init__(self, statuses=("",), verdicts=None):
        self.calls, self.statuses = [], list(statuses)
        self.verdicts = verdicts or {}

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd[0] == "git":
            out = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
        tree = cmd[cmd.index("--worktree") + 1] if "--worktree" in cmd else "fantasy"
        role = tree.split("-")[0]
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"result": "done\n" + self.verdicts.get(role, "")}), stderr="")

    def models(self):
        return [c for c in self.calls if c[0] != "git"]


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "LOCK", tmp_path / "lock")
    monkeypatch.setattr(improve, "LEASE", tmp_path / "lease.json")
    monkeypatch.setattr(improve, "RUNS", tmp_path / "runs.jsonl")
    monkeypatch.setattr(improve, "load", lambda path=None: ([dict(ITEM)], []))
    return tmp_path


def tick(mode_states, game=None, run=None, dry_run=False):
    seq = list(mode_states)

    def controller(_league):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def game_tick(_league, _week):
        return json.dumps(game or {"changes_since_last_tick": []})
    return runner.tick("123", controller, game_tick, run or FakeRun(), at=AT, dry_run=dry_run)


class TestCommand:
    def test_the_command_limits_tools_and_mcp(self):
        cmd = runner.claude_cmd("p", ("Read",), "oracle", worktree="review-q", claude="claude")
        assert cmd[:3] == ["claude", "-p", "p"]
        assert cmd[cmd.index("--allowedTools") + 1] == "Read"
        assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
        assert cmd[cmd.index("--mcp-config") + 1] == '{"mcpServers": {}}'
        assert "--strict-mcp-config" in cmd and cmd[-2:] == ["--worktree", "review-q"]
        with_mcp = runner.claude_cmd("p", runner.FANTASY_TOOLS, "fantasy", mcp=True, claude="claude")
        assert with_mcp[with_mcp.index("--mcp-config") + 1].endswith(".mcp.json")
        assert "--worktree" not in with_mcp

    def test_the_fixer_cannot_reach_espn_or_the_network(self):
        assert not any(t.startswith("mcp__") for t in runner.FIXER_TOOLS + runner.VERIFIER_TOOLS
                       + runner.ORACLE_TOOLS)
        assert "Edit" not in runner.VERIFIER_TOOLS + runner.ORACLE_TOOLS

    def test_last_json_line(self):
        assert runner.last_json_line('text\n{"full_suite": true}\n') == {"full_suite": True}
        assert runner.last_json_line("no json") is None


class TestModes:
    def test_degraded_starts_no_model(self, isolated):
        run = FakeRun()
        out = tick([state("DEGRADED")], run=run)
        assert out["outcome"].startswith("degraded") and run.calls == []

    def test_watch_with_no_change_starts_no_model(self, isolated):
        run = FakeRun()
        assert tick([state("WATCH")], run=run)["outcome"] == "no change" and run.calls == []

    def test_a_change_starts_the_fantasy_model_with_the_fantasy_tools(self, isolated):
        run = FakeRun()
        out = tick([state("HOT")], game={"changes_since_last_tick": ["Murray: QUESTIONABLE -> OUT"]},
                   run=run)
        (cmd,) = run.models()
        assert cmd[cmd.index("--allowedTools") + 1] == ",".join(runner.FANTASY_TOOLS)
        assert "Murray: QUESTIONABLE -> OUT" in cmd[2]
        assert out["changes"] == ["Murray: QUESTIONABLE -> OUT"]

    def test_a_held_lock_skips_the_tick(self, isolated):
        (isolated / "lock").write_text(json.dumps({"at": AT}))
        assert tick([state("WATCH")])["outcome"].startswith("skipped")


class TestEngineering:
    def test_an_idle_tick_runs_fixer_verifier_oracle_and_parks(self, isolated):
        run = FakeRun(verdicts={"verify": '{"targeted_tests": true, "full_suite": true}',
                                "review": '{"oracle_review": true, "findings": 0}'})
        out = tick([state("IDLE", allowed=True), state("IDLE", allowed=True)], run=run)
        trees = [c[c.index("--worktree") + 1] for c in run.models()]
        assert trees == ["fix-q-a", "verify-q-a", "review-q-a"]
        assert out["gates"] == {"targeted_tests": True, "full_suite": True, "oracle_review": True,
                                "still_idle": True}
        assert out["promotion"]["promote"] is True
        assert out["outcome"] == "parked: branch worktree-fix-q-a; promotion not performed"
        assert not (isolated / "lease.json").exists()
        assert improve.attempted(isolated / "runs.jsonl") == {"q-a"}
        # Attempted once, the item is not picked again.
        assert tick([state("IDLE", allowed=True)])["outcome"] == "idle: nothing to take"

    def test_fantasy_returning_mid_run_fails_the_still_idle_gate(self, isolated):
        run = FakeRun(verdicts={"verify": '{"targeted_tests": true, "full_suite": true}',
                                "review": '{"oracle_review": true}'})
        out = tick([state("IDLE", allowed=True), state("WATCH")], run=run)
        assert out["gates"]["still_idle"] is False and out["promotion"]["promote"] is False

    def test_a_changed_main_checkout_aborts_before_the_verifier(self, isolated):
        run = FakeRun(statuses=["", " M src/ffdraft/server.py"])
        out = tick([state("IDLE", allowed=True)], run=run)
        assert out["outcome"] == "failed: the main checkout changed while the fixer ran"
        assert len(run.models()) == 1 and not (isolated / "lease.json").exists()

    def test_a_dry_run_leases_nothing_and_runs_nothing(self, isolated):
        run = FakeRun()
        out = tick([state("DEEP_IDLE", allowed=True)], run=run, dry_run=True)
        assert out["outcome"] == "would lease q-a for 35 min" and run.calls == []
        assert len(out["commands"]) == 3 and not (isolated / "lease.json").exists()

    def test_the_record_is_written(self, isolated):
        path = runner.write_record({"started_utc": AT, "dry_run": True}, isolated / "runs")
        assert path.name == "20260913T220000Z-dry.json"
