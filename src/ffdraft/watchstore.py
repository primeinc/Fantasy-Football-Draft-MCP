"""What a draft watch was doing, so a new server process can pick it up.

Every `/mcp` reconnect starts a fresh server. The old process dies with its
socket, its watch and the queue it had merged, and the user has to ask for both
again. This records the intent -- which league, which team, the queue as ESPN
last accepted it -- so the next process can resume without being told.

Intent, not state. The picks belong to the draft and live in `STATE_DIR`; this
says only that a watch was wanted and has not been stopped. `stop_watch` clears
the flag rather than deleting the record, because "the user stopped it" and "we
never saw this league" are different answers to the next process's question.

Two refusals to resume, both in `resumable`:

  - a record older than `MAX_AGE_HOURS`. A draft is a few hours; a day-old record
    is last week's draft, and rejoining its room would take a connection slot
    for nothing.
  - a draft the caller has found to be complete. Whoever resumes must decide
    that -- this module does no network -- so it is passed in.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import WATCH_DIR

# Beyond this a record describes a draft that is over, whatever it claims.
MAX_AGE_HOURS = 24.0
_MS = 1000.0


@dataclass
class WatchRecord:
    """One league's watch intent. `queue` is what ESPN last accepted, which after
    a merge already holds the entries the user built in the app."""

    league_id: str
    team_id: int
    season: int
    resume: bool = True
    started_at_ms: int = 0
    queue: list[int] = field(default_factory=list)
    # How many of `queue` came from the user rather than from us, as the merge
    # reported it. Carried so the resume message can say it without recomputing
    # from a queue that has since changed.
    queue_from_user: int = 0

    def age_hours(self, now_ms: int | None = None) -> float:
        now = int(time.time() * _MS) if now_ms is None else now_ms
        return max(0.0, (now - self.started_at_ms) / _MS / 3600.0)


def path_for(league_id: str) -> Path:
    """Where one league's record lives. The id is used as a filename, so anything
    that is not a plain identifier is refused rather than escaped: a league id is
    digits in every payload seen, and a path separator arriving here would be a
    bug worth stopping on."""
    text = str(league_id)
    if not text or not all(c.isalnum() or c in "-_" for c in text):
        raise ValueError(f"league id {league_id!r} is not usable as a filename")
    return WATCH_DIR / f"{text}.json"


def save(record: WatchRecord) -> Path:
    """Write the record atomically, stamping `started_at_ms` on first write.

    Temp file then `os.replace`, which is atomic on the same filesystem. A plain
    write truncates first, so a crash mid-write leaves a truncated file that
    `load` reads as absent -- and this runs from `set_draft_queue` on every
    accepted queue, which is mid-draft. An absent record is not even a refusal to
    report: it is the silent no-resume this module exists to prevent, reachable
    through its own write path.
    """
    if not record.started_at_ms:
        record.started_at_ms = int(time.time() * _MS)
    path = path_for(record.league_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(asdict(record), indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return path


def load(league_id: str) -> WatchRecord | None:
    """One record, or None if it is absent or unreadable.

    A record this cannot parse is treated as absent rather than raised on: it is
    read at server start, and a half-written file must not stop the server from
    coming up. The file is left alone so it can be looked at.
    """
    path = path_for(league_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    known = {f for f in WatchRecord.__dataclass_fields__}
    try:
        return WatchRecord(**{k: v for k, v in raw.items() if k in known})
    except TypeError:
        return None


def load_all() -> tuple[list[WatchRecord], list[str]]:
    """Every readable record oldest first, and the names of the files skipped.

    The skipped list is returned rather than swallowed. A record that cannot be
    read is a watch that will not come back, and dropping it silently is the
    failure this module is about; the caller turns it into a refusal a user can
    see.
    """
    if not WATCH_DIR.is_dir():
        return [], []
    out, skipped = [], []
    for path in sorted(WATCH_DIR.glob("*.json")):
        record = load(path.stem)
        if record is None:
            skipped.append(path.name)
        else:
            out.append(record)
    return sorted(out, key=lambda r: r.started_at_ms), skipped


def update_queue(league_id: str, queue: list[int], from_user: int = 0) -> WatchRecord | None:
    """Record the queue ESPN accepted, so a resume re-sends what is live now
    rather than what was sent when the watch started."""
    record = load(league_id)
    if record is None:
        return None
    record.queue = list(queue)
    record.queue_from_user = from_user
    save(record)
    return record


def mark_stopped(league_id: str) -> WatchRecord | None:
    """Clear the resume flag. The record stays: a stopped watch and a league
    nobody has watched are different facts, and only one of them should be
    silent on the next start."""
    record = load(league_id)
    if record is None:
        return None
    record.resume = False
    save(record)
    return record


def resumable(record: WatchRecord, draft_complete: bool = False,
              now_ms: int | None = None) -> tuple[bool, str]:
    """Whether to bring this watch back, and why not when the answer is no.

    The reason is returned rather than logged because the caller reports it: a
    watch that silently does not come back is the same problem as a watch that
    silently dies, which is what this whole module is about.
    """
    if not record.resume:
        return False, "stopped by stop_watch"
    if draft_complete:
        return False, "the draft is complete"
    age = record.age_hours(now_ms)
    if age > MAX_AGE_HOURS:
        return False, f"the record is {age:.1f}h old, past the {MAX_AGE_HOURS:.0f}h limit"
    return True, ""
