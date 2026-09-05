"""Draft-room watch: wire handling tested offline against the captured INIT payload."""
import asyncio
from pathlib import Path

import pandas as pd
import pytest

from ffdraft import board, watch
from ffdraft.config import LeagueSettings

FIXTURE = Path(__file__).parent / "fixtures" / "espn_draft_init.b64"


def _watch(tmp_path, monkeypatch, board_df=None):
    monkeypatch.setattr(board, "STATE_DIR", tmp_path)
    monkeypatch.setattr(watch, "STATE_DIR", tmp_path)
    monkeypatch.setattr(board, "espn_maps", lambda: (
        {"4429795": "Jahmyr Gibbs", "4362628": "Ja'Marr Chase", "3000001": "Bench Guy"},
        {"4429795": "RB", "4362628": "WR", "3000001": "WR"}))
    league = LeagueSettings(name="t", teams=16, draft_slot=4, rounds=14)
    events = []

    async def notify(content, meta):
        events.append((content, meta))

    # No board by default: the recommendation path is only entered within
    # RECOMMEND_WITHIN picks of the user's turn, which these snapshots never reach.
    w = watch.DraftWatch("1734659820", 2026, 3, "{ABC}", "s2", league, board_df, notify)
    return w, events


def _market_board() -> pd.DataFrame:
    """A board with the three market columns an as-of snapshot keeps."""
    names = ["Jahmyr Gibbs", "Ja'Marr Chase", "Bench Guy", "Deep Guy"]
    b = pd.DataFrame({
        "name": names, "position": ["RB", "WR", "WR", "TE"], "team": ["A"] * 4,
        "player_id": ["00-1", "00-2", "00-3", "00-4"],
        "proj_points": [300.0, 290.0, 120.0, 90.0],
        "adp": [1.0, 2.0, 90.0, 150.0], "espn_rank": [1, 2, 88, 140],
        "espn_proj": [280.0, 275.0, 110.0, 80.0],
    })
    b["_key"] = b["name"].map(board.norm_name)
    return b


def test_init_seeds_state_with_real_slots(tmp_path, monkeypatch):
    w, events = _watch(tmp_path, monkeypatch)
    asyncio.run(w.handle_line("INIT " + FIXTURE.read_text().strip()))

    assert w.picks_seen == 114
    assert w.state.on_the_clock == 115
    mine = [p for p in w.state.picks if p["slot"] == 4]
    assert [p["overall"] for p in mine] == [4, 29, 36, 61, 68, 93, 100]
    assert mine[0]["name"] == "Ja'Marr Chase"
    assert w.state.picks[0] == {"overall": 1, "slot": 1, "name": "Jahmyr Gibbs", "player_id": None,
                                "position": "RB"}
    assert events[-1][1]["event"] == "snapshot"
    assert "114 picks made" in events[-1][0]


def test_selected_appends_and_announces(tmp_path, monkeypatch):
    w, events = _watch(tmp_path, monkeypatch)
    asyncio.run(w.handle_line("INIT " + FIXTURE.read_text().strip()))
    asyncio.run(w.handle_line("SELECTED 10 -16001 16"))

    last = w.state.picks[-1]
    assert last == {"overall": 115, "slot": 16, "name": "Atlanta Falcons D/ST", "player_id": None,
                    "position": "DST"}
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
    assert [(r["team"], r["event"]) for r in room["recent"]] == [
        ("The Spreadsheet Squad (Cool Breeze)", "joined"),
        ("Sydney Sideline Stars (Sydney Tiller)", "left"),
    ]
    assert all(r["at_ms"] > 0 for r in room["recent"])


def test_room_names_the_next_pickers(tmp_path, monkeypatch):
    w, _events = _watch(tmp_path, monkeypatch)
    asyncio.run(w.handle_line("INIT " + FIXTURE.read_text().strip()))
    team_of_slot = {slot: team for team, slot in w.slot_of.items()}
    w.directory = {team_of_slot[14]: {"name": "Fourteen", "owners": ["Ada"]},
                   3: {"name": "adverse possession", "owners": ["Will Peters"]}}
    asyncio.run(w.handle_line(f"JOINED {team_of_slot[14]} {{X}}"))
    asyncio.run(w.handle_line(f"LEFT {team_of_slot[13]} {{Y}} 1"))

    up = w.room()["upcoming"]
    # 114 picks made: 115 is on the clock in round 8 (reverse), slot 14; the
    # user's slot 4 comes at 125.
    assert [u["pick"] for u in up] == [115, 116, 117, 118, 119]
    assert up[0]["slot"] == 14 and up[0]["team"] == "Fourteen (Ada)" and up[0]["online"] is True
    assert up[1]["slot"] == 13 and up[1]["online"] is False and up[1]["mine"] is False
    assert [u["pick"] for u in w.upcoming(11) if u["mine"]] == [125]
    assert w.upcoming(11)[-1]["team"] == "adverse possession (Will Peters)"


def test_set_queue_sends_full_list_and_returns_echo(tmp_path, monkeypatch):
    w, _events = _watch(tmp_path, monkeypatch)
    sent = []

    class Ws:
        async def send(self, text):
            sent.append(text)
            # ESPN echoes the accepted list, dropping ids it rejects.
            await w.handle_line("DRAFT_LIST 4429795 4362628")

    async def go():
        w.ws = Ws()
        return await w.set_queue([4429795, 4362628, 999999], timeout=2)

    assert asyncio.run(go()) == [4429795, 4362628]
    assert sent == ["DRAFT_LIST 4429795 4362628 999999\n"]
    assert w.queue == [4429795, 4362628]


def test_empty_queue_clears(tmp_path, monkeypatch):
    w, _events = _watch(tmp_path, monkeypatch)
    sent = []

    class Ws:
        async def send(self, text):
            sent.append(text)
            await w.handle_line("DRAFT_LIST")

    async def go():
        w.ws = Ws()
        return await w.set_queue([], timeout=2)

    assert asyncio.run(go()) == []
    assert sent == ["DRAFT_LIST\n"]


def test_error_line_raises(tmp_path, monkeypatch):
    w, _ = _watch(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="No\\+team"):
        asyncio.run(w.handle_line("ERROR 1 No+team"))


class TestAsOfSnapshots:
    def test_init_and_each_selected_file_the_market_for_the_pick_on_the_clock(
            self, tmp_path, monkeypatch):
        w, _events = _watch(tmp_path, monkeypatch, board_df=_market_board())
        asyncio.run(w.handle_line("INIT " + FIXTURE.read_text().strip()))
        # 114 picks in the snapshot, so the pick on the clock is 115.
        assert w.snapshots == [115]
        snap = watch.read_snapshot("1734659820", 115)
        assert snap is not None
        assert list(snap.columns) == ["_key", "player_id", "adp", "espn_rank", "espn_proj"]
        # Cheapest ADP first, and the fixture already has Jahmyr Gibbs (pick 1)
        # and Ja'Marr Chase (pick 4), so the snapshot records only what is left.
        assert snap["_key"].tolist() == ["bench guy", "deep guy"]
        assert snap["adp"].tolist() == [90.0, 150.0]
        assert snap["espn_proj"].tolist() == [110.0, 80.0]

        asyncio.run(w.handle_line("SELECTED 10 3000001 4"))
        assert w.snapshots == [115, 116]
        after = watch.read_snapshot("1734659820", 116)
        # Bench Guy was just taken, so pick 116's snapshot no longer carries him.
        assert after is not None and after["_key"].tolist() == ["deep guy"]

    def test_a_snapshot_is_bounded_and_never_breaks_the_socket_loop(self, tmp_path, monkeypatch):
        w, events = _watch(tmp_path, monkeypatch, board_df=_market_board())
        monkeypatch.setattr(watch, "SNAPSHOT_ROWS", 1)
        asyncio.run(w.handle_line("INIT " + FIXTURE.read_text().strip()))
        bounded = watch.read_snapshot("1734659820", 115)
        assert bounded is not None and len(bounded) == 1

        # A board that cannot be written costs the snapshot, not the pick.
        w.board = _market_board().drop(columns=["_key"])
        asyncio.run(w.handle_line("SELECTED 10 4362628 4"))
        assert w.snapshots == [115]
        assert events[-1][1]["event"] == "pick"

    def test_no_board_writes_nothing(self, tmp_path, monkeypatch):
        w, _events = _watch(tmp_path, monkeypatch)
        asyncio.run(w.handle_line("INIT " + FIXTURE.read_text().strip()))
        assert w.snapshots == []
        assert watch.read_snapshot("1734659820", 115) is None
        assert not watch.snapshot_dir("1734659820").exists()
