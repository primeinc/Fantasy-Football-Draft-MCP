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


def test_duplicate_connection_pauses_instead_of_reconnecting(tmp_path, monkeypatch):
    w, events = _watch(tmp_path, monkeypatch)
    # ESPN sends LEFT for our own team with reason 2 right before closing the socket.
    asyncio.run(w.handle_line("LEFT 3 {abc} 2"))
    assert w.bumped is True

    async def session():
        raise ConnectionError("no close frame received or sent")

    monkeypatch.setattr(w, "_session", session)
    asyncio.run(w.run())
    assert events[-1][1]["event"] == "paused"
    assert "watch_draft" in events[-1][0]


def test_other_teams_leaving_does_not_pause(tmp_path, monkeypatch):
    w, _events = _watch(tmp_path, monkeypatch)
    asyncio.run(w.handle_line("LEFT 13 {C8E45485} 1"))
    asyncio.run(w.handle_line("LEFT 3 {ABC} 1"))
    assert w.bumped is False


def test_select_sends_and_resolves_on_own_selected(tmp_path, monkeypatch):
    w, _events = _watch(tmp_path, monkeypatch)
    asyncio.run(w.handle_line("INIT " + FIXTURE.read_text().strip()))
    sent = []

    class Ws:
        async def send(self, text):
            sent.append(text)
            # ESPN answers on the same socket; feed it back as the server would.
            await w.handle_line("SELECTED 3 4429795 2")

    async def go():
        w.ws = Ws()
        return await w.select(4429795, timeout=2)

    result = asyncio.run(go())
    assert sent == ["SELECT 4429795\n"]
    assert result == {"overall": 115, "player_id": 4429795, "name": "Jahmyr Gibbs"}
    assert w.state.picks[-1]["slot"] == 4 and w.own_pick is None


def test_select_surfaces_server_error(tmp_path, monkeypatch):
    w, _ = _watch(tmp_path, monkeypatch)
    asyncio.run(w.handle_line("INIT " + FIXTURE.read_text().strip()))

    class Ws:
        async def send(self, _text):
            await w.handle_line("ERROR 1 Not+your+turn")

    async def go():
        w.ws = Ws()
        return await w.select(4429795, timeout=2)

    with pytest.raises(RuntimeError, match="Not\\+your\\+turn"):
        asyncio.run(go())


def test_select_without_connection_raises(tmp_path, monkeypatch):
    w, _ = _watch(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="not connected"):
        asyncio.run(w.select(1))


def test_session_pings_on_a_timer_despite_constant_inbound_traffic(tmp_path, monkeypatch):
    # ESPN drops a client that never pings; CLOCK ticks every few seconds do not
    # count. The first PING must go out ~1 s after connect, then every 15 s, even
    # though recv() never times out.
    w, _ = _watch(tmp_path, monkeypatch)
    monkeypatch.setattr(watch.espn_live, "draft_security_token", lambda *_a: "tok")
    sent, ticks = [], {"n": 0}

    class Ws:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def recv(self):
            ticks["n"] += 1
            if ticks["n"] > 60:
                raise ConnectionError("done")
            return "CLOCK 6 1000 2\n"

        async def send(self, text):
            sent.append(text)

    monkeypatch.setattr(watch, "connect", lambda *_a, **_k: Ws())
    # Compress time: each recv advances the loop clock by 0.5 s.
    base = [0.0]

    class Loop:
        def time(self):
            base[0] += 0.5
            return base[0]

    monkeypatch.setattr(watch.asyncio, "get_running_loop", lambda: Loop())

    with pytest.raises(ConnectionError):
        asyncio.run(w._session())
    pings = [s for s in sent if s.startswith("PING ")]
    # 60 ticks x 0.5 s = ~30 s of traffic: first ping at ~1 s, then at ~16 s.
    assert len(pings) >= 2


def test_room_tracks_presence_and_chat(tmp_path, monkeypatch):
    w, _events = _watch(tmp_path, monkeypatch)
    w.directory = {3: {"name": "adverse possession", "owners": ["Will Peters"]},
                   9: {"name": "The Spreadsheet Squad", "owners": ["Cool Breeze"]},
                   15: {"name": "Sydney Sideline Stars", "owners": ["Sydney Tiller"]}}
    asyncio.run(w.handle_line("INIT " + FIXTURE.read_text().strip()))
    online_from_init = {r["team_id"] for r in w.room()["online"]}
    assert 3 in online_from_init

    asyncio.run(w.handle_line("JOINED 9 {ABC}"))
    asyncio.run(w.handle_line("LEFT 15 {DEF} 1"))
    asyncio.run(w.handle_line("CHAT 15 {DEF} 1788469125122 Kate+Pick+your+player+man"))
    room = w.room()
    online = {r["team_id"]: r["team"] for r in room["online"]}
    assert online[9] == "The Spreadsheet Squad (Cool Breeze)"
    assert 15 not in online
    assert room["chat"][-1]["text"] == "Kate Pick your player man"
    assert room["chat"][-1]["team"] == "Sydney Sideline Stars (Sydney Tiller)"
    assert room["on_the_clock"] == 115


def test_error_line_raises(tmp_path, monkeypatch):
    w, _ = _watch(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="No\\+team"):
        asyncio.run(w.handle_line("ERROR 1 No+team"))
