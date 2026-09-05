"""Draft-room watch: wire handling tested offline against the captured INIT payload."""
import asyncio
from pathlib import Path

import pytest

from ffdraft import board, watch
from ffdraft.config import LeagueSettings

FIXTURE = Path(__file__).parent / "fixtures" / "espn_draft_init.b64"


def _watch(tmp_path, monkeypatch):
    monkeypatch.setattr(board, "STATE_DIR", tmp_path)
    monkeypatch.setattr(watch, "_espn_name_map", lambda: {
        "4429795": "Jahmyr Gibbs", "4362628": "Ja'Marr Chase"})
    league = LeagueSettings(name="t", teams=16, draft_slot=4, rounds=14)
    events = []

    async def notify(content, meta):
        events.append((content, meta))

    # No board: the recommendation path is only entered within RECOMMEND_WITHIN
    # picks of the user's turn, which these snapshots never reach.
    w = watch.DraftWatch("1734659820", 2026, 3, "{ABC}", "s2", league, None, notify)
    return w, events


def test_init_seeds_state_with_real_slots(tmp_path, monkeypatch):
    w, events = _watch(tmp_path, monkeypatch)
    asyncio.run(w.handle_line("INIT " + FIXTURE.read_text().strip()))

    assert w.picks_seen == 114
    assert w.state.on_the_clock == 115
    mine = [p for p in w.state.picks if p["slot"] == 4]
    assert [p["overall"] for p in mine] == [4, 29, 36, 61, 68, 93, 100]
    assert mine[0]["name"] == "Ja'Marr Chase"
    assert w.state.picks[0] == {"overall": 1, "slot": 1, "name": "Jahmyr Gibbs", "player_id": None}
    assert events[-1][1]["event"] == "snapshot"
    assert "114 picks made" in events[-1][0]


def test_selected_appends_and_announces(tmp_path, monkeypatch):
    w, events = _watch(tmp_path, monkeypatch)
    asyncio.run(w.handle_line("INIT " + FIXTURE.read_text().strip()))
    asyncio.run(w.handle_line("SELECTED 10 -16001 16"))

    last = w.state.picks[-1]
    assert last == {"overall": 115, "slot": 16, "name": "Atlanta Falcons D/ST", "player_id": None}
    content, meta = events[-1]
    assert meta["event"] == "pick" and meta["pick"] == "115"
    assert "team 10 took Atlanta Falcons D/ST" in content
    # 114 picks in the snapshot, this is 115, the user picks at 125.
    assert "Pick 116 on the clock; your turn in 9." in content


def test_undone_rolls_back(tmp_path, monkeypatch):
    w, events = _watch(tmp_path, monkeypatch)
    asyncio.run(w.handle_line("INIT " + FIXTURE.read_text().strip()))
    asyncio.run(w.handle_line("UNDONE 112"))
    assert w.state.on_the_clock == 113
    assert events[-1][1]["event"] == "undone"


def test_error_line_raises(tmp_path, monkeypatch):
    w, _ = _watch(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="No\\+team"):
        asyncio.run(w.handle_line("ERROR 1 No+team"))
