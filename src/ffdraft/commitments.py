"""Approved roster actions that have not happened yet: the ledger the send tools
and the governor read.

A commitment is one action the user approved, written down when it was
approved: what comes in, what goes out, by when, the fallbacks named with it,
and the user's words. It is `open` until ESPN confirms it, the user cancels it,
it is blocked with a reason, or it becomes impossible. A passed deadline makes
it OVERDUE; nothing here drops a record because time went by, which is how a
decision point behaves and why a decision point is not a commitment.

`STATE_DIR/commitments.json` is a JSON list of records:

    {id, league_id, week, kind: claim | trade | lineup,
     add: {name, espn_id} | null, drop: {name, espn_id} | null,
     fallbacks: [{name, espn_id}, ...],          alternative adds, in order
     give: [{name, espn_id}, ...], get: [...], partner_team_id,   a trade
     deadline: ISO 8601 with a zone, approved_text, opened_at,
     status: open | confirmed | blocked | cancelled | impossible,
     confirmed: {espn_transaction_id, at} | blocked: {reason, at}
     | cancelled: {by, at} | impossible: {why, at}}

A missing file is no commitments. An unreadable one is an error, named, never
an empty list. `python -m ffdraft.commitments gate` exits 2 while any record is
open and prints each one: the Stop hook runs it, so a turn cannot end with an
approved action neither done nor accounted for.
"""
from __future__ import annotations

import json
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

import pandas as pd

from .config import STATE_DIR
from .governor import commitment_text, when

COMMITMENTS = STATE_DIR / "commitments.json"
KINDS = ("claim", "trade", "lineup")
OPEN, CONFIRMED, BLOCKED, CANCELLED, IMPOSSIBLE = (
    "open", "confirmed", "blocked", "cancelled", "impossible")
GATE_OPEN = 2


def player(name: str, espn_id: int | str) -> dict:
    """One player as a commitment names him: the name ESPN's pool carries and his id."""
    name = str(name).strip()
    if not name:
        raise ValueError("a commitment names a player by name")
    try:
        pid = int(str(espn_id))
    except ValueError:
        raise ValueError(f"{name}: ESPN player id {espn_id!r} is not an integer") from None
    return {"name": name, "espn_id": pid}


def load(path: Path | None = None) -> tuple[list[dict], str | None]:
    """The commitments on disk, and why they could not be read."""
    path = path or COMMITMENTS
    if not path.exists():
        return [], None
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [], f"{type(exc).__name__}: {exc}"
    if not isinstance(rows, list):
        return [], f"{path.name} is not a JSON list"
    return [r for r in rows if isinstance(r, dict)], None


def _load_or_raise(path: Path) -> list[dict]:
    rows, err = load(path)
    if err:
        raise ValueError(f"{path} is unreadable: {err}")
    return rows


def save(rows: list[dict], path: Path | None = None) -> None:
    path = path or COMMITMENTS
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=1), encoding="utf-8")


def _now(now) -> pd.Timestamp:
    ts = pd.Timestamp.now(tz="UTC") if now is None else when(now)
    if ts is None:
        raise ValueError(f"unreadable time {now!r}")
    return ts


def open_commitment(league_id: str, week: int, kind: str, deadline: str, *,
                    add: dict | None = None, drop: dict | None = None,
                    fallbacks: Sequence[dict] = (), give: Sequence[dict] = (),
                    get: Sequence[dict] = (),
                    partner_team_id: int | None = None, approved_text: str = "",
                    now=None, path: Path | None = None) -> dict:
    """Append one open commitment and return it. `deadline` must carry a zone;
    a claim names an add; a trade names a partner and at least one player."""
    path = path or COMMITMENTS
    if kind not in KINDS:
        raise ValueError(f"kind {kind!r} is not one of {', '.join(KINDS)}")
    due = when(deadline)
    if due is None or pd.Timestamp(deadline).tzinfo is None:
        raise ValueError(f"deadline {deadline!r} must be ISO 8601 with a UTC offset")
    if kind == "claim" and add is None:
        raise ValueError("a claim commitment names the player to add")
    if kind == "trade" and (partner_team_id is None or not (give or get)):
        raise ValueError("a trade commitment names the partner team and at least one player")
    rows = _load_or_raise(path)
    row = {
        "id": uuid.uuid4().hex[:8],
        "league_id": str(league_id),
        "week": int(week),
        "kind": kind,
        "add": add,
        "drop": drop,
        "fallbacks": list(fallbacks),
        "give": list(give),
        "get": list(get),
        "partner_team_id": None if partner_team_id is None else int(partner_team_id),
        "deadline": due.isoformat(),
        "approved_text": str(approved_text),
        "opened_at": _now(now).isoformat(),
        "status": OPEN,
    }
    rows.append(row)
    save(rows, path)
    return row


def open_ones(rows: list[dict]) -> list[dict]:
    """Every record still open. Time plays no part: an overdue one is open."""
    return [r for r in rows if r.get("status") == OPEN]


def find(rows: list[dict], commitment_id: str) -> dict | None:
    return next((r for r in rows if r.get("id") == commitment_id), None)


def allowed_ids(commitment: dict) -> set[int]:
    """Every ESPN player id a send under this commitment may move: the add, the
    drop, each named fallback, and both sides of a trade. Empty for a lineup,
    whose moves are the roster's."""
    people = [commitment.get("add"), commitment.get("drop")]
    people += list(commitment.get("fallbacks") or [])
    people += list(commitment.get("give") or []) + list(commitment.get("get") or [])
    return {int(p["espn_id"]) for p in people if isinstance(p, dict) and "espn_id" in p}


def _close(commitment_id: str, status: str, fields: dict, now, path: Path | None) -> dict:
    path = path or COMMITMENTS
    rows = _load_or_raise(path)
    row = find(rows, commitment_id)
    if row is None:
        raise ValueError(f"no commitment {commitment_id!r} in {path.name}")
    if row.get("status") != OPEN:
        raise ValueError(f"commitment {commitment_id} is {row.get('status')}, not open")
    row["status"] = status
    row[status] = {**fields, "at": _now(now).isoformat()}
    save(rows, path)
    return row


def confirm(commitment_id: str, espn_transaction_id: str, now=None,
            path: Path | None = None) -> dict:
    """ESPN holds the transaction: the commitment is done."""
    return _close(commitment_id, CONFIRMED,
                  {"espn_transaction_id": str(espn_transaction_id)}, now, path)


def block(commitment_id: str, reason: str, now=None, path: Path | None = None) -> dict:
    """The action could not be sent, and this is why. The record leaves the gate;
    the reason is what the next reader sees first."""
    if not str(reason).strip():
        raise ValueError("a block names its reason")
    return _close(commitment_id, BLOCKED, {"reason": str(reason)}, now, path)


def cancel(commitment_id: str, by: str, now=None, path: Path | None = None) -> dict:
    """The user withdrew or superseded the action."""
    return _close(commitment_id, CANCELLED, {"by": str(by)}, now, path)


def mark_impossible(commitment_id: str, why: str, now=None, path: Path | None = None) -> dict:
    """The action can no longer happen: the player is on another team, the
    deadline was a lock ESPN enforces, and so on."""
    if not str(why).strip():
        raise ValueError("impossible names why")
    return _close(commitment_id, IMPOSSIBLE, {"why": str(why)}, now, path)


def gate(rows: list[dict], now=None) -> tuple[int, list[str]]:
    """The exit code and the lines the Stop hook prints: GATE_OPEN with one line
    per open commitment, 0 with none."""
    at = _now(now)
    lines = [f"BLOCKED-UNTIL-RESOLVED: {commitment_text(r, at)}" for r in open_ones(rows)]
    return (GATE_OPEN if lines else 0), lines


EXITS = ("each stays open until a send under it is CONFIRMED, block_commitment records why it "
         "cannot be sent, or the user cancels it")


def main(argv: list[str], stdin: str = "") -> int:
    """The Stop hook. `stdin` is the hook's JSON; `stop_hook_active` true means
    this gate already blocked once this turn, and a hook that keeps blocking
    loops the session until it is killed (claude-hooks-mk2 data-contracts.md,
    `exit 2`), so the retry is allowed with the open commitments still printed."""
    if argv[:1] != ["gate"]:
        print("usage: python -m ffdraft.commitments gate", file=sys.stderr)
        return 1
    active = False
    if stdin.strip():
        try:
            active = bool(json.loads(stdin).get("stop_hook_active"))
        except (ValueError, AttributeError):
            active = False
    rows, err = load()
    if err:
        print(f"BLOCKED-UNTIL-RESOLVED: {COMMITMENTS} is unreadable: {err}", file=sys.stderr)
        return 0 if active else GATE_OPEN
    code, lines = gate(rows)
    for line in lines:
        print(line, file=sys.stderr)
    if lines:
        print(EXITS, file=sys.stderr)
    if code and active:
        print("stop allowed: this gate already blocked once this turn; the commitments above "
              "are still open", file=sys.stderr)
        return 0
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], "" if sys.stdin.isatty() else sys.stdin.read()))
