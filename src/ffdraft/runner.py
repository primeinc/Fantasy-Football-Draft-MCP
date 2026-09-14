"""One unattended tick: the governor decides, fantasy preempts, idle time goes to one leased item.

The tick is Python; a model is started only where judgment is needed.

  DEGRADED        the unreadable sources are logged; no model.
  HOT, WATCH      `game_tick` runs; a model runs only when
                  `changes_since_last_tick` or `fantasy_actionable` is non-empty,
                  with the heartbeat's lineup rule and no waiver claims.
  IDLE, DEEP_IDLE one queue item is picked and leased; fixer, verifier and
                  oracle-static each run as a separate `claude -p` in their own
                  worktree, bounded by the lease. The main checkout's git status
                  is compared before and after the fixer, and a change aborts
                  the run. The lease is released with the outcome and the
                  branch is parked. Nothing is merged: `improve.promotion` is
                  recorded, not performed.

Every tick writes `STATE_DIR/runner/<stamp>.json`; `LOCK` stops overlapping
ticks. `python -m ffdraft.runner tick [--dry-run]` is the entry point Task
Scheduler runs; it loads the fantasy-draft server env from `.mcp.json`.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pandas as pd

from . import improve
from .config import STATE_DIR
from .governor import when

REPO = improve.REPO
LOCK = STATE_DIR / "runner.lock"
RUN_DIR = STATE_DIR / "runner"
LOCK_STALE_MINUTES = 150
LEAGUE_ENV = "ESPN_LEAGUE_ID"
# Spend caps per `claude -p`. A one-word reply cost $0.28-0.40 here on
# 2026-09-13: this machine's plugins, skills and CLAUDE.md are ~40k tokens of
# cache creation before any work.
BUDGET_USD = {"fantasy": 3.0, "fixer": 8.0, "verifier": 3.0, "oracle": 3.0}
MAX_TURNS = {"fantasy": 20, "fixer": 60, "verifier": 30, "oracle": 30}
FANTASY_TOOLS = ("mcp__fantasy-draft__game_tick", "mcp__fantasy-draft__controller_state",
                 "mcp__fantasy-draft__injury_report", "mcp__fantasy-draft__live_scores",
                 "mcp__fantasy-draft__weekly_lineup", "mcp__fantasy-draft__submit_lineup",
                 "mcp__fantasy-draft__player_week")
FIXER_TOOLS = ("Read", "Grep", "Glob", "Edit", "Write", "Bash(just check)",
               "Bash(git status *)", "Bash(git diff *)", "Bash(git add *)", "Bash(git commit *)")
VERIFIER_TOOLS = ("Read", "Grep", "Glob", "Bash(just check)", "Bash(just runner candidate-risk *)",
                  "Bash(git checkout *)", "Bash(git diff *)", "Bash(git log *)")
ORACLE_TOOLS = ("Read", "Grep", "Glob", "Bash(git diff *)", "Bash(git show *)")

FANTASY_PROMPT = """Unattended fantasy tick for ESPN league {league_id}, week {week}. No human is \
present: never ask questions.
controller_state: {state}
game_tick changes since the last tick: {changes}
Report each change in one line. If a starter not yet locked is OUT, INACTIVE or on bye and \
submit_lineup with dry_run shows only benching him for an ACTIVE player with no refusals, send it \
and report espn_holds. Never submit waiver claims. End with one summary line."""
FIXER_PROMPT = """You are the fixer for improvement-queue item {id} in this espn-ffd-mcp worktree. \
Read and follow .claude/agents/fixer.md exactly. The lease expires {expires}; stop before it.
Item: {item}"""
VERIFIER_PROMPT = """You are the verifier for improvement-queue item {id} in this espn-ffd-mcp \
worktree. Read and follow .claude/agents/verifier.md exactly. Branch to verify: {branch}.
Item: {item}
End with one line of JSON: {{"targeted_tests": bool, "full_suite": bool, "candidate_risk": str}}"""
ORACLE_PROMPT = """You are oracle-static reviewing improvement-queue item {id} in this espn-ffd-mcp \
worktree. Read and follow .claude/agents/oracle-static.md. Review `git diff HEAD...{branch}` \
against the item.
Item: {item}
End with one line of JSON: {{"oracle_review": bool, "findings": int}}"""

Runner = Callable[..., subprocess.CompletedProcess]


def claude_cmd(prompt: str, tools: tuple[str, ...], role: str, worktree: str | None = None,
               mcp: bool = False, claude: str | None = None) -> list[str]:
    """A headless `claude -p` command limited to `tools`, with no MCP server
    unless `mcp`, in its own worktree when `worktree` is given."""
    cmd = [claude or shutil.which("claude") or "claude", "-p", prompt,
           "--permission-mode", "dontAsk", "--allowedTools", ",".join(tools),
           "--max-turns", str(MAX_TURNS[role]), "--max-budget-usd", str(BUDGET_USD[role]),
           "--output-format", "json", "--no-session-persistence", "--strict-mcp-config",
           "--mcp-config", str(REPO / ".mcp.json") if mcp else '{"mcpServers": {}}']
    if worktree:
        cmd += ["--worktree", worktree]
    return cmd


def last_json_line(text: str) -> dict | None:
    """The last line of `text` that parses as a JSON object."""
    for line in reversed((text or "").splitlines()):
        line = line.strip().strip("`")
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return None


def result_text(proc: subprocess.CompletedProcess) -> str:
    """The model's final text from `--output-format json`, else stdout. Claude
    Code 2.1.270 prints a JSON array of events whose last `result` event
    carries it (probed 2026-09-13)."""
    try:
        doc = json.loads(proc.stdout)
    except (ValueError, TypeError):
        return str(proc.stdout or "")
    events = doc if isinstance(doc, list) else [doc]
    results = [e for e in events if isinstance(e, dict) and e.get("type", "result") == "result"
               and "result" in e]
    return str(results[-1]["result"] or "") if results else str(proc.stdout or "")


def remove_worktree(run: Runner, name: str, keep_branch: bool) -> str:
    """Remove `.claude/worktrees/<name>`, which a headless `--worktree` run leaves
    behind locked (probed 2026-09-13), and its branch unless `keep_branch`."""
    path = str(REPO / ".claude" / "worktrees" / name)
    done = run(["git", "-C", str(REPO), "worktree", "remove", "-f", "-f", path],
               capture_output=True, text=True)
    out = f"worktree {name}: rc {done.returncode}"
    if not keep_branch:
        # -d, not -D: a verify or review branch has no commits of its own, so a
        # refusal here means it gained some and a human should look.
        gone = run(["git", "-C", str(REPO), "branch", "-d", f"worktree-{name}"],
                   capture_output=True, text=True)
        out += f", branch rc {gone.returncode}"
    return out


def acquire_lock(now: pd.Timestamp, path: Path | None = None) -> bool:
    path = path or LOCK
    if path.exists():
        try:
            held = pd.Timestamp(json.loads(path.read_text(encoding="utf-8"))["at"])
        except (ValueError, KeyError, OSError):
            held = None
        if held is not None and now - held < pd.Timedelta(minutes=LOCK_STALE_MINUTES):
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"at": now.isoformat(), "pid": os.getpid()}), encoding="utf-8")
    return True


def release_lock(path: Path | None = None) -> None:
    (path or LOCK).unlink(missing_ok=True)


def git_status(run: Runner) -> str:
    return run(["git", "-C", str(REPO), "status", "--porcelain"], capture_output=True,
               text=True, check=True).stdout


def tick(league_id: str, controller: Callable[[str], str], game_tick: Callable[[str, int], str],
         run: Runner = subprocess.run, at: str | None = None, dry_run: bool = False) -> dict:
    """One tick. `controller` and `game_tick` are the server tools; `run` runs commands."""
    now = when(at or pd.Timestamp.now(tz="UTC").isoformat())
    if now is None:
        raise ValueError(f"unreadable tick time {at!r}")
    record: dict = {"started_utc": now.isoformat(), "dry_run": dry_run, "steps": []}
    if not dry_run and not acquire_lock(now):
        record["outcome"] = "skipped: another tick holds the lock"
        return record
    try:
        state = json.loads(controller(league_id))
        record["mode"] = state.get("mode")
        record["state"] = {k: state.get(k) for k in ("as_of", "next_required_attention",
                                                     "idle_budget_minutes", "engineering",
                                                     "degraded_capabilities")}
        if state.get("mode") == "DEGRADED":
            record["outcome"] = f"degraded: {state.get('degraded_capabilities')}"
        elif state.get("mode") in ("HOT", "WATCH"):
            _fantasy(league_id, state, game_tick, run, record, dry_run)
        else:
            _engineering(state, controller, league_id, run, now, record, dry_run)
    finally:
        if not dry_run:
            release_lock()
    return record


def _fantasy(league_id: str, state: dict, game_tick: Callable[[str, int], str], run: Runner,
             record: dict, dry_run: bool) -> None:
    week = int(state.get("week") or 0)
    if dry_run:
        record["outcome"] = f"would run game_tick({league_id}, {week}) and a model on its changes"
        return
    tick_out = json.loads(game_tick(league_id, week))
    changes = tick_out.get("changes_since_last_tick") or []
    record["changes"] = changes
    if not changes and not state.get("fantasy_actionable"):
        record["outcome"] = "no change"
        return
    prompt = FANTASY_PROMPT.format(league_id=league_id, week=week, changes=json.dumps(changes),
                                   state=json.dumps({k: state.get(k) for k in (
                                       "mode", "fantasy_actionable", "observing",
                                       "next_required_attention")}))
    proc = run(claude_cmd(prompt, FANTASY_TOOLS, "fantasy", mcp=True), capture_output=True,
               text=True, cwd=str(REPO), timeout=20 * 60)
    record["steps"].append({"role": "fantasy", "rc": proc.returncode,
                            "result": result_text(proc), "stderr": proc.stderr})
    record["outcome"] = f"fantasy model rc {proc.returncode}"


def _engineering(state: dict, controller: Callable[[str], str], league_id: str, run: Runner,
                 now: pd.Timestamp, record: dict, dry_run: bool) -> None:
    eng = state.get("engineering") or {}
    items, errors = improve.load()
    if errors:
        record["outcome"] = f"queue invalid: {errors}"
        return
    item = (improve.pick(items, int(eng.get("max_minutes") or 0), eng.get("max_risk_class"),
                         improve.attempted()) if eng.get("allowed") else None)
    if item is None:
        record["outcome"] = "idle: nothing to take"
        return
    record["item"] = item["id"]
    fixer_tree, branch = f"fix-{item['id']}", f"worktree-fix-{item['id']}"
    item_json = json.dumps(item)
    if dry_run:
        record["outcome"] = f"would lease {item['id']} for {eng.get('max_minutes')} min"
        record["commands"] = [claude_cmd("<fixer prompt>", FIXER_TOOLS, "fixer", fixer_tree),
                              claude_cmd("<verifier prompt>", VERIFIER_TOOLS, "verifier",
                                         f"verify-{item['id']}"),
                              claude_cmd("<oracle prompt>", ORACLE_TOOLS, "oracle",
                                         f"review-{item['id']}")]
        return
    lease = improve.acquire(item, state, now.isoformat())
    deadline = pd.Timestamp(lease["expires_utc"])

    def remaining() -> float:
        return max(0.0, (deadline - pd.Timestamp.now(tz="UTC")).total_seconds())

    def step(role: str, prompt: str, tools: tuple[str, ...], tree: str) -> dict:
        proc = run(claude_cmd(prompt, tools, role, tree), capture_output=True, text=True,
                   cwd=str(REPO), timeout=remaining())
        text = result_text(proc)
        entry = {"role": role, "rc": proc.returncode, "result": text, "stderr": proc.stderr,
                 "verdict": last_json_line(text)}
        record["steps"].append(entry)
        return entry

    current = "fixer"
    record["cleanup"] = []
    try:
        before = git_status(run)
        step("fixer", FIXER_PROMPT.format(id=item["id"], expires=lease["expires_utc"],
                                          item=item_json), FIXER_TOOLS, fixer_tree)
        # The branch stays; the worktree goes so the verifier can check it out.
        record["cleanup"].append(remove_worktree(run, fixer_tree, keep_branch=True))
        if git_status(run) != before:
            outcome = "failed: the main checkout changed while the fixer ran"
        else:
            current = "verifier"
            verify = step("verifier", VERIFIER_PROMPT.format(id=item["id"], branch=branch,
                                                             item=item_json),
                          VERIFIER_TOOLS, f"verify-{item['id']}")
            record["cleanup"].append(remove_worktree(run, f"verify-{item['id']}", False))
            current = "oracle"
            review = step("oracle", ORACLE_PROMPT.format(id=item["id"], branch=branch,
                                                         item=item_json),
                          ORACLE_TOOLS, f"review-{item['id']}")
            record["cleanup"].append(remove_worktree(run, f"review-{item['id']}", False))
            still = json.loads(controller(league_id)).get("mode") in ("IDLE", "DEEP_IDLE")
            gates = {"targeted_tests": bool((verify["verdict"] or {}).get("targeted_tests")),
                     "full_suite": bool((verify["verdict"] or {}).get("full_suite")),
                     "oracle_review": bool((review["verdict"] or {}).get("oracle_review")),
                     "still_idle": still}
            record["gates"] = gates
            record["promotion"] = improve.promotion(item, gates)
            outcome = f"parked: branch {branch}; promotion not performed"
    except subprocess.TimeoutExpired:
        outcome = f"failed: lease expired during the {current}"
    except Exception as exc:
        outcome = f"failed: {current}: {type(exc).__name__}: {exc}"
    record["outcome"] = outcome
    improve.release(outcome, pd.Timestamp.now(tz="UTC").isoformat())


def write_record(record: dict, root: Path | None = None) -> Path:
    root = root or RUN_DIR
    root.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp(record["started_utc"]).strftime("%Y%m%dT%H%M%SZ")
    path = root / f"{stamp}{'-dry' if record.get('dry_run') else ''}.json"
    path.write_text(json.dumps(record, indent=1), encoding="utf-8")
    return path


def main(argv: list[str]) -> int:
    if not argv or argv[0] != "tick":
        print("usage: python -m ffdraft.runner tick [--dry-run]", file=sys.stderr)
        return 2
    env = json.loads((REPO / ".mcp.json").read_text(encoding="utf-8"))
    os.environ.update(env["mcpServers"]["fantasy-draft"]["env"])
    league_id = os.environ.get(LEAGUE_ENV)
    if not league_id:
        print(f"{LEAGUE_ENV} is not set in .mcp.json", file=sys.stderr)
        return 2
    from . import server

    record = tick(league_id, server.controller_state, server.game_tick,
                  dry_run="--dry-run" in argv)
    path = write_record(record)
    print(json.dumps({"record": str(path), "mode": record.get("mode"),
                      "outcome": record.get("outcome")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
