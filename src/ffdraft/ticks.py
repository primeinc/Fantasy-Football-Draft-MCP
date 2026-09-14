"""What changed between two `game_tick` reads, so a monitor reports deltas from state, not memory.

A change is one of: a roster player's status appearing, changing or clearing;
a roster/feed disagreement not seen last tick; a lineup gain while a lock is
still ahead that differs from last tick's; a next lock that differs from last
tick's; a source that became unreadable. The first tick of a week reports
everything non-empty. The previous tick is `STATE_DIR/ticks/game_tick_<league>_<week>.json`.
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import STATE_DIR

TICKS = STATE_DIR / "ticks"


def path_for(league_id: str, week: int, root: Path | None = None) -> Path:
    return (root or TICKS) / f"game_tick_{league_id}_{int(week)}.json"


def load(path: Path) -> tuple[dict | None, str | None]:
    """The previous tick, and why it could not be read. A missing file is no
    previous tick; a corrupt one is an error, not a first tick."""
    if not path.exists():
        return None, None
    try:
        tick = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(tick, dict):
        return None, f"{path.name} is not a JSON object"
    return tick, None


def save(tick: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tick, indent=1), encoding="utf-8")


def delta(prev: dict | None, cur: dict) -> list[str]:
    """One line per change from `prev` to `cur`, in a fixed order."""
    before = prev or {}
    out: list[str] = []
    old, new = before.get("statuses") or {}, cur.get("statuses") or {}
    newest = cur.get("newest_feed_entry") or {}
    for name in sorted(set(old) | set(new)):
        if old.get(name) == new.get(name):
            continue
        as_of = f" (feed as_of {newest['as_of']})" if newest.get("player") == name else ""
        if prev is None:
            # No read saw a transition; say what is, not a change from ACTIVE.
            out.append(f"{name}: {new.get(name)} (no prior tick this week){as_of}")
            continue
        out.append(f"{name}: {old.get(name) or 'ACTIVE'} -> {new.get(name) or 'ACTIVE'}{as_of}")

    def key(d: dict) -> tuple:
        return d.get("player"), d.get("roster"), d.get("feed")

    seen = {key(d) for d in before.get("disagreements") or []}
    for d in cur.get("disagreements") or []:
        if key(d) not in seen:
            out.append(f"disagreement: {d.get('player')} roster {d.get('roster')}, feed "
                       f"{d.get('feed')} as_of {d.get('as_of')}")

    versus = cur.get("versus_espn") or {}
    if (versus.get("gain") or 0) > 0 and cur.get("next_lock") is not None \
            and versus != before.get("versus_espn"):
        out.append(f"lineup gain {versus.get('gain')}: start {versus.get('start')}, bench "
                   f"{versus.get('bench')}")

    if cur.get("next_lock") is not None and cur.get("next_lock") != before.get("next_lock"):
        lock = cur["next_lock"]
        out.append(f"next lock {lock.get('kickoff')}: {', '.join(lock.get('players') or [])}")

    for source in sorted(set(cur.get("unread") or {}) - set(before.get("unread") or {})):
        out.append(f"unreadable: {source}: {cur['unread'][source]}")
    return out
