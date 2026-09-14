"""The improvement queue: engineering work an idle tick may take, and the rules for taking it.

Queue: `.agent/improvement-queue.jsonl`, one item per line, in priority order.
Every item carries `FIELDS`; `evidence` names what established the defect.

Lease: `STATE_DIR/improvement-lease.json`, at most one at a time. A lease is
granted only when `controller-state.json` allows engineering, the item fits the
minutes left, and its risk class is within the allowance. It expires at the
governor's `lease_expires`; every tick starts from a fresh controller state, so
fantasy preempts any lease.

Risk classes:

  A  local and reversible with the external contract unchanged: tests, docs,
     diagnostics, read-only tools, pure parsing, fixtures. Promoted after every
     gate in `PROMOTION_GATES` passes.
  B  behavior change: rankings, heuristics, new data sources, refactors, new
     dependencies. Built and verified in a worktree; a human promotes it.
  C  never autonomous: ESPN writes, credentials, scheduling, permissions, agent
     and governor policy, the live .venv, runtime upgrades. Proposal only.

An item whose scope touches a `PROTECTED` path is class C whatever it declares,
and a class the queue does not know is C.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .config import STATE_DIR

REPO = Path(__file__).resolve().parents[2]
QUEUE = REPO / ".agent" / "improvement-queue.jsonl"
LEASE = STATE_DIR / "improvement-lease.json"
RUNS = STATE_DIR / "improvement-runs.jsonl"
FIELDS = ("id", "title", "evidence", "scope", "defect", "estimated_minutes", "risk_class",
          "acceptance_test", "dependencies", "discovered_by", "last_attempt", "status")
STATUSES = ("open", "leased", "done", "parked", "blocked")
RISK_RANK = {"A": 0, "B": 1, "C": 2}
PROTECTED = ("src/ffdraft/governor.py", "src/ffdraft/improve.py", "src/ffdraft/lineup_write.py",
             "src/ffdraft/claim_write.py", "runner.just", ".agent/", ".claude/", "CLAUDE.md",
             ".mcp.json", "pyproject.toml", "uv.lock", ".venv/")
PROMOTION_GATES = ("targeted_tests", "full_suite", "oracle_review", "still_idle")


class LeaseRefused(RuntimeError):
    """A lease the rules do not allow."""


def load(path: Path | None = None) -> tuple[list[dict], list[str]]:
    """Queue items in order, and one message per line that is not a valid item."""
    path = path or QUEUE
    if not path.exists():
        return [], [f"{path} does not exist"]
    items, errors = [], []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError as exc:
            errors.append(f"line {n}: {exc}")
            continue
        missing = [f for f in FIELDS if not isinstance(item, dict) or f not in item]
        if missing:
            errors.append(f"line {n}: missing {', '.join(missing)}")
            continue
        if item["status"] not in STATUSES:
            errors.append(f"line {n}: status {item['status']!r} is not one of {STATUSES}")
            continue
        items.append(item)
    return items, errors


def save(items: list[dict], path: Path | None = None) -> None:
    path = path or QUEUE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items),
                    encoding="utf-8")


def effective_risk(item: dict) -> str:
    """The declared class, raised to C when the scope touches a protected path."""
    declared = str(item.get("risk_class"))
    if declared not in RISK_RANK:
        return "C"
    for scope in item.get("scope") or []:
        path = str(scope).replace("\\", "/")
        while path.startswith("./"):
            path = path[2:]
        if any(path == p.rstrip("/") or path.startswith(p) for p in PROTECTED):
            return "C"
    return declared


def pick(items: list[dict], max_minutes: int, max_risk: str | None) -> dict | None:
    """The first open item whose dependencies are done, that fits the minutes,
    and whose effective class is within `max_risk` and is not C."""
    if max_risk not in ("A", "B") or max_minutes <= 0:
        return None
    done = {i["id"] for i in items if i["status"] == "done"}
    for item in items:
        risk = effective_risk(item)
        if (item["status"] == "open" and set(item.get("dependencies") or []) <= done
                and risk != "C" and RISK_RANK[risk] <= RISK_RANK[max_risk]
                and int(item["estimated_minutes"]) <= max_minutes):
            return item
    return None


def read_lease(path: Path | None = None) -> dict | None:
    path = path or LEASE
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def acquire(item: dict, state: dict, at: str, path: Path | None = None) -> dict:
    """Write a lease for `item` under the controller `state` read at `at`."""
    path = path or LEASE
    now = pd.Timestamp(at)
    held = read_lease(path)
    if held is not None and pd.Timestamp(held["expires_utc"]) > now:
        raise LeaseRefused(f"lease for {held['item_id']} is held until {held['expires_utc']}")
    eng = state.get("engineering") or {}
    if not eng.get("allowed"):
        raise LeaseRefused(f"controller mode {state.get('mode')} allows no engineering")
    chosen = pick([item], int(eng.get("max_minutes") or 0), eng.get("max_risk_class"))
    if chosen is None:
        raise LeaseRefused(
            f"{item['id']} ({effective_risk(item)}, {item['estimated_minutes']} min, "
            f"{item['status']}) does not fit {eng.get('max_minutes')} min at class "
            f"{eng.get('max_risk_class')}")
    expires = now + pd.Timedelta(minutes=int(eng["max_minutes"]))
    lease = {"item_id": item["id"], "risk_class": effective_risk(item),
             "acquired_utc": now.isoformat(), "expires_utc": expires.isoformat(),
             "mode": state.get("mode")}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(lease, indent=1), encoding="utf-8")
    return lease


def release(outcome: str, at: str, path: Path | None = None, runs: Path | None = None) -> dict:
    """Remove the lease and append the run record: the lease with its outcome."""
    path, runs = path or LEASE, runs or RUNS
    held = read_lease(path)
    if held is None:
        raise LeaseRefused("no lease is held")
    record = {**held, "released_utc": pd.Timestamp(at).isoformat(), "outcome": outcome}
    runs.parent.mkdir(parents=True, exist_ok=True)
    with runs.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    path.unlink()
    return record


def promotion(item: dict, gates: dict[str, bool]) -> dict:
    """Whether a verified candidate may be merged without a human."""
    risk = effective_risk(item)
    failed = [g for g in PROMOTION_GATES if gates.get(g) is not True]
    if risk != "A":
        return {"promote": False, "why": f"class {risk} is promoted by a human"}
    if failed:
        return {"promote": False, "why": f"gates not passed: {', '.join(failed)}"}
    return {"promote": True, "why": "class A with every gate passed"}
