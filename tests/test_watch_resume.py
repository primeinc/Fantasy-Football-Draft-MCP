"""A watch survives the server process that started it.

Every `/mcp` reconnect is a new process. The socket, the watch and the merged
queue die with the old one, and before this the user had to ask for both again.
"""
import asyncio
import json

import pytest

from ffdraft import config, server, watchstore


@pytest.fixture(autouse=True)
def watch_dir(tmp_path, monkeypatch):
    """Records go to a temporary directory, never the user's real one."""
    d = tmp_path / "watch"
    d.mkdir()
    monkeypatch.setattr(config, "WATCH_DIR", d)
    monkeypatch.setattr(watchstore, "WATCH_DIR", d)
    return d


def _record(league_id: str = "L", team_id: int = 3, season: int = 2026,
            queue: list[int] | None = None, queue_from_user: int = 0,
            started_at_ms: int = 0) -> watchstore.WatchRecord:
    # Explicit parameters rather than a **kwargs splat: ty cannot narrow a dict
    # of mixed value types back onto the dataclass's fields, and the error it
    # gives is about the splat rather than about anything wrong.
    return watchstore.WatchRecord(
        league_id=league_id, team_id=team_id, season=season,
        queue=list(queue or []), queue_from_user=queue_from_user,
        started_at_ms=started_at_ms)


async def _done() -> None:
    """A stand-in for `_channel`, which is awaited by its callers."""
    return None


class TestTheStore:
    def test_a_saved_record_reads_back(self, watch_dir):
        watchstore.save(_record(queue=[1, 2, 3], queue_from_user=2))

        back = watchstore.load("L")
        assert back is not None
        assert (back.league_id, back.team_id, back.season) == ("L", 3, 2026)
        assert back.queue == [1, 2, 3] and back.queue_from_user == 2
        assert back.resume is True and back.started_at_ms > 0

    def test_stop_watch_clears_the_flag_and_keeps_the_record(self, watch_dir):
        watchstore.save(_record())

        watchstore.mark_stopped("L")

        back = watchstore.load("L")
        # The record survives on purpose: "the user stopped it" and "we have
        # never seen this league" are different answers to the next start.
        assert back is not None and back.resume is False
        ok, why = watchstore.resumable(back)
        assert ok is False and "stop_watch" in why

    def test_the_queue_is_updated_in_place(self, watch_dir):
        watchstore.save(_record(queue=[1]))

        watchstore.update_queue("L", [9, 8, 7], from_user=1)

        back = watchstore.load("L")
        assert back is not None and back.queue == [9, 8, 7] and back.queue_from_user == 1

    def test_a_record_for_an_unknown_league_updates_nothing(self, watch_dir):
        assert watchstore.update_queue("nobody", [1]) is None
        assert watchstore.mark_stopped("nobody") is None

    def test_an_unreadable_record_is_absent_not_an_error(self, watch_dir):
        # A half-written file must not stop the server coming up.
        (watch_dir / "L.json").write_text("{not json", encoding="utf-8")

        assert watchstore.load("L") is None
        assert watchstore.load_all() == []
        assert (watch_dir / "L.json").exists(), "the bad file is left to be looked at"

    def test_a_league_id_that_is_not_a_filename_is_refused(self):
        for bad in ("../escape", "a/b", ""):
            with pytest.raises(ValueError):
                watchstore.path_for(bad)


class TestWhenNotToResume:
    def test_a_stale_record_does_not_resume(self, watch_dir):
        old = _record(started_at_ms=1)
        ok, why = watchstore.resumable(old, now_ms=int(25 * 3600 * 1000))
        assert ok is False and "past the 24h limit" in why

    def test_a_record_inside_the_window_resumes(self, watch_dir):
        fresh = _record(started_at_ms=int(20 * 3600 * 1000))
        assert watchstore.resumable(fresh, now_ms=int(25 * 3600 * 1000))[0] is True

    def test_a_complete_draft_does_not_resume(self, watch_dir):
        assert watchstore.resumable(_record(), draft_complete=True) == (
            False, "the draft is complete")


class TestResumingOnStart:
    def _stub(self, monkeypatch, *, drafted=False, sent=None):
        """A watch that joins instantly, and a queue send that records its call."""
        calls: dict = {}

        class FakeState:
            def summary(self):
                return {"picks_made": 122, "my_next_pick": 125}

        class FakeWatch:
            def __init__(self, *a, **kw):
                self.state = FakeState()
                self.espn_map = {}
                self.ready = asyncio.Event()
                self.ready.set()

            async def run(self):
                await asyncio.Event().wait()

        from ffdraft import watch as watch_mod
        monkeypatch.setattr(watch_mod, "DraftWatch", FakeWatch)
        monkeypatch.setattr(server.bd, "espn_league_context",
                            lambda *a, **k: {"my_team_id": 3, "draft_slot": 4,
                                             "league_name": "L", "drafted": drafted})
        monkeypatch.setattr(server.bd, "espn_league_directory", lambda *a, **k: {})
        monkeypatch.setattr(server, "_build_board", lambda force=False: None)
        monkeypatch.setattr(server.bd, "_espn_player_name",
                            lambda pid, m: {11: "Player Eleven",
                                            22: "Player Twenty-Two"}[pid])
        monkeypatch.setenv("ESPN_SWID", "{A}")
        monkeypatch.setenv("ESPN_S2", "s2")

        async def fake_set_queue(league_id, player_names, replace=False):
            calls["names"] = player_names
            calls["replace"] = replace
            return json.dumps(sent if sent is not None else
                              {"mode": "merge",
                               "accepted": [{"espn_id": 11}, {"espn_id": 22}],
                               "kept_from_the_users_queue": [{"espn_id": 22}]})

        monkeypatch.setattr(server, "set_draft_queue", fake_set_queue)
        return calls

    def test_a_persisted_record_resumes_and_announces(self, watch_dir, monkeypatch):
        self._stub(monkeypatch)
        watchstore.save(_record(queue=[11, 22], queue_from_user=1))
        said: list = []

        def capture(content, meta):
            said.append((content, meta))
            return _done()

        monkeypatch.setattr(server, "_channel", capture)

        out = asyncio.run(server.resume_watches())

        assert len(out) == 1 and out[0]["resumed"] is True
        assert out[0]["picks_made"] == 122
        assert "L" in server._WATCHES
        content, meta = said[-1]
        assert "watch resumed after restart: 122 picks made" in content
        assert "your next pick is 125" in content
        assert "queue re-sent, 2 entries, 1 of them yours" in content
        assert meta["event"] == "resumed"
        server._WATCHES.pop("L", None)

    def test_the_queue_goes_through_the_merge_path(self, watch_dir, monkeypatch):
        calls = self._stub(monkeypatch)
        watchstore.save(_record(queue=[11, 22]))
        monkeypatch.setattr(server, "_channel", lambda content, meta: _done())

        asyncio.run(server.resume_watches())

        # By name through set_draft_queue, not raw ids down the socket, and
        # without replace: that is what keeps the user's app-side entries.
        assert calls["names"] == "Player Eleven, Player Twenty-Two"
        assert calls["replace"] is False
        server._WATCHES.pop("L", None)

    def test_a_watch_with_no_queue_says_so_rather_than_sending_nothing(
            self, watch_dir, monkeypatch):
        calls = self._stub(monkeypatch)
        watchstore.save(_record())
        said: list = []

        def capture(content, meta):
            said.append(content)
            return _done()

        monkeypatch.setattr(server, "_channel", capture)

        asyncio.run(server.resume_watches())

        assert "names" not in calls, "no queue means no send"
        assert "no queue to re-send" in said[-1]
        server._WATCHES.pop("L", None)

    def test_a_failed_queue_send_is_named_not_swallowed(self, watch_dir, monkeypatch):
        self._stub(monkeypatch, sent={"error": "ESPN did not echo the queue within 10s"})
        watchstore.save(_record(queue=[11]))
        said: list = []

        def capture(content, meta):
            said.append(content)
            return _done()

        monkeypatch.setattr(server, "_channel", capture)

        asyncio.run(server.resume_watches())

        assert "the queue was NOT re-sent" in said[-1]
        assert "did not echo" in said[-1]
        server._WATCHES.pop("L", None)

    def test_a_complete_draft_is_not_rejoined(self, watch_dir, monkeypatch):
        self._stub(monkeypatch, drafted=True)
        watchstore.save(_record())
        monkeypatch.setattr(server, "_channel", lambda content, meta: _done())

        out = asyncio.run(server.resume_watches())

        assert out[0]["resumed"] is False
        assert out[0]["why"] == "the draft is complete"
        assert "L" not in server._WATCHES

    def test_a_stopped_record_is_not_rejoined(self, watch_dir, monkeypatch):
        self._stub(monkeypatch)
        watchstore.save(_record())
        watchstore.mark_stopped("L")
        monkeypatch.setattr(server, "_channel", lambda content, meta: _done())

        out = asyncio.run(server.resume_watches())

        assert out[0]["resumed"] is False and "stop_watch" in out[0]["why"]
        assert "L" not in server._WATCHES

    def test_one_league_failing_does_not_stop_another(self, watch_dir, monkeypatch):
        self._stub(monkeypatch)
        watchstore.save(_record(league_id="good"))
        watchstore.save(_record(league_id="bad"))
        monkeypatch.setattr(server, "_channel", lambda content, meta: _done())

        def context(league_id, *a, **k):
            if league_id == "bad":
                raise RuntimeError("ESPN said no")
            return {"my_team_id": 3, "draft_slot": 4, "league_name": "L", "drafted": False}

        monkeypatch.setattr(server.bd, "espn_league_context", context)

        out = {r["league_id"]: r for r in asyncio.run(server.resume_watches())}

        assert out["bad"]["resumed"] is False and "ESPN said no" in out["bad"]["why"]
        assert out["good"]["resumed"] is True
        server._WATCHES.pop("good", None)


class TestTheChannelWaitsForASession:
    """There is no session at server start: sessions are per request, and what
    outlives them is the connection's standalone channel. So a resume that runs
    before any client request has nowhere to speak and holds its message."""

    def test_a_message_with_no_session_is_held_not_lost(self, monkeypatch):
        monkeypatch.setattr(server, "_SESSION", None)
        monkeypatch.setattr(server, "_PENDING_CHANNEL", [])

        asyncio.run(server._channel("resumed", {"event": "resumed"}))

        assert server._PENDING_CHANNEL == [("resumed", {"event": "resumed"})]

    def test_attaching_a_session_flushes_what_was_held(self, monkeypatch):
        sent: list = []

        class Session:
            async def send_notification(self, note):
                sent.append(note.params["content"])

        monkeypatch.setattr(server, "_SESSION", None)
        monkeypatch.setattr(server, "_PENDING_CHANNEL", [])

        async def go():
            await server._channel("held", {"event": "resumed"})
            assert sent == []
            # Await the flush's own handle rather than yielding a guessed number
            # of scheduler turns.
            flush = server._attach_session(Session())
            assert flush is not None
            await flush

        asyncio.run(go())
        assert sent == ["held"]
        assert server._PENDING_CHANNEL == []

    def test_attaching_without_a_running_loop_does_not_raise(self, monkeypatch):
        """`draft_status` is a sync tool and FastMCP may run it off the loop."""
        monkeypatch.setattr(server, "_SESSION", None)
        monkeypatch.setattr(server, "_PENDING_CHANNEL", [("held", {})])

        assert server._attach_session(object()) is None

        assert server._PENDING_CHANNEL == [("held", {})], "held for the next attach"

    def test_a_send_that_fails_is_held_rather_than_raising(self, monkeypatch):
        """This runs inside the watch's socket loop; a failed notification must
        not take the socket down with it."""
        class Broken:
            async def send_notification(self, note):
                raise RuntimeError("the client went away")

        monkeypatch.setattr(server, "_SESSION", Broken())
        monkeypatch.setattr(server, "_PENDING_CHANNEL", [])

        asyncio.run(server._channel("pick 130", {"event": "pick"}))

        assert server._PENDING_CHANNEL == [("pick 130", {"event": "pick"})]
