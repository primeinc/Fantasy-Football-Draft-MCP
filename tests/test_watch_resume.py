"""A watch survives the server process that started it.

Every `/mcp` reconnect is a new process. The socket, the watch and the merged
queue die with the old one, and before this the user had to ask for both again.
"""
import asyncio

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


@pytest.fixture(autouse=True)
def no_leaked_watches():
    """`_WATCHES` is module state and a resumed watch stays in it.

    Left behind, it makes the next test look like a double join -- which is how
    the guard against that found this in the first place.
    """
    before = dict(server._WATCHES)
    yield
    server._WATCHES.clear()
    server._WATCHES.update(before)


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
        assert watchstore.load_all() == ([], ["L.json"])
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
    def _stub(self, monkeypatch, *, drafted=False, sent=None, never_ready=False,
              echo=True):
        """A watch that joins instantly, and a queue merge that records its call.

        `echo` is whether ESPN sends a `DRAFT_LIST` on this connection. It is
        modelled on the watch rather than in the return value because that is
        where the real thing puts it: `merge_queue_ids` waits for the echo, which
        sets `w.queue`, and only then decides what to send. A stub that left
        `w.queue` at None while reporting a successful merge described a state the
        socket never produces.
        """
        calls: dict = {}

        class FakeState:
            def summary(self):
                return {"picks_made": 122, "my_next_pick": 125}

        class FakeWatch:
            def __init__(self, *a, **kw):
                self.state = FakeState()
                self.espn_map = {}
                self.queue = None
                self.queue_echoes = []
                self.ready = asyncio.Event()
                if not never_ready:
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
        monkeypatch.setenv("ESPN_SWID", "{A}")
        monkeypatch.setenv("ESPN_S2", "s2")

        async def fake_merge(w, ids, replace=False, league_id=""):
            calls["ids"] = list(ids)
            calls["replace"] = replace
            calls.setdefault("sends", []).append(
                {"ids": list(ids), "replace": replace})
            if echo or replace:
                w.queue = list(ids)
            elif not replace:
                return {"error": "no queue echo on this connection yet; pass "
                                 "replace=True to send yours"}
            return sent if sent is not None else {
                "mode": "replace" if replace else "merge",
                "accepted": [{"espn_id": pid} for pid in ids],
                "kept_from_the_users_queue": [] if replace else [{"espn_id": 22}]}

        monkeypatch.setattr(server, "merge_queue_ids", fake_merge)
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

    def test_the_queue_goes_through_the_merge_path_as_ids(self, watch_dir, monkeypatch):
        calls = self._stub(monkeypatch)
        watchstore.save(_record(queue=[11, 22]))
        monkeypatch.setattr(server, "_channel", lambda content, meta: _done())

        asyncio.run(server.resume_watches())

        # Ids straight through, and without replace: that is what keeps the
        # user's app-side entries. Rendering them into names and re-resolving
        # would drop the whole queue over one player the crosswalk lacks -- and
        # the entries a merge preserves are exactly the ones that never went
        # through the crosswalk.
        assert calls["ids"] == [11, 22]
        assert calls["replace"] is False

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

        assert "ids" not in calls, "no queue means no send"
        assert "no queue to re-send" in said[-1]

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

    def test_no_echo_re_sends_the_recorded_queue_rather_than_nothing(
            self, watch_dir, monkeypatch):
        """The state a restart actually lands in.

        ESPN drops the queue when the client session ends and sends no
        `DRAFT_LIST` while it holds none, so the echo a merge waits for never
        comes on the one path resume exists for. First live use, 2026-09-05
        14:35: the watch came back, the message said the queue was NOT re-sent,
        and the user's queue stayed empty until it was sent by hand.
        """
        calls = self._stub(monkeypatch, echo=False)
        watchstore.save(_record(queue=[11, 22]))
        said: list = []

        def capture(content, meta):
            said.append(content)
            return _done()

        monkeypatch.setattr(server, "_channel", capture)

        asyncio.run(server.resume_watches())

        # The merge is tried first every time: a replace is what is left when
        # ESPN has said, by silence, that there is nothing to merge into.
        assert calls["sends"] == [{"ids": [11, 22], "replace": False},
                                  {"ids": [11, 22], "replace": True}]
        assert "queue re-sent from the record, 2 entries" in said[-1]
        # Not "0 of them yours", which would read as a loss. Nothing of the
        # user's was kept because ESPN was holding nothing to keep.
        assert "nothing of yours could be kept" in said[-1]
        # The cost of sending anyway, in the same sentence as the send.
        assert "would have been overwritten" in said[-1]

    def test_an_echo_is_merged_and_never_replaced(self, watch_dir, monkeypatch):
        """The guard the fallback must not swallow: once ESPN has said what it
        holds, the user's app-side entries are known and a replace would drop
        them."""
        calls = self._stub(monkeypatch, echo=True)
        watchstore.save(_record(queue=[11, 22]))
        said: list = []

        def capture(content, meta):
            said.append(content)
            return _done()

        monkeypatch.setattr(server, "_channel", capture)

        asyncio.run(server.resume_watches())

        assert calls["sends"] == [{"ids": [11, 22], "replace": False}]
        assert "queue re-sent, 2 entries, 1 of them yours" in said[-1]
        assert "overwritten" not in said[-1]

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


class TestARefusalIsSaidOutLoud:
    """The module's docstring promised this and the code did not keep it.

    `resume_watches` runs as a task and nobody reads its return value, so a
    reason returned to that caller is a reason nobody sees. A watch that
    silently does not come back is the same problem as one that silently dies.
    """

    def _said(self, monkeypatch) -> list:
        said: list = []

        def capture(content, meta):
            said.append((content, meta))
            return _done()

        monkeypatch.setattr(server, "_channel", capture)
        return said

    def test_a_complete_draft_says_why_on_the_channel(self, watch_dir, monkeypatch):
        TestResumingOnStart()._stub(monkeypatch, drafted=True)
        watchstore.save(_record())
        said = self._said(monkeypatch)

        asyncio.run(server.resume_watches())

        content, meta = said[-1]
        assert "watch NOT resumed for league L" in content
        assert "the draft is complete" in content
        assert meta["event"] == "not_resumed"

    def test_a_stale_record_says_why_on_the_channel(self, watch_dir, monkeypatch):
        TestResumingOnStart()._stub(monkeypatch)
        watchstore.save(_record(started_at_ms=1))
        said = self._said(monkeypatch)

        asyncio.run(server.resume_watches())

        assert "past the 24h limit" in said[-1][0]

    def test_a_record_nobody_can_read_says_so(self, watch_dir, monkeypatch):
        (watch_dir / "1734659820.json").write_text("{truncated", encoding="utf-8")
        said = self._said(monkeypatch)

        out = asyncio.run(server.resume_watches())

        # It has no league id to name, so the file is named instead. Silence here
        # would be the worst case: not even a refusal, because there is no record.
        assert out[0]["why"] == "its record could not be read"
        assert "1734659820.json" in said[-1][0]

    def test_the_users_own_stop_is_not_announced(self, watch_dir, monkeypatch):
        TestResumingOnStart()._stub(monkeypatch)
        watchstore.save(_record())
        watchstore.mark_stopped("L")
        said = self._said(monkeypatch)

        out = asyncio.run(server.resume_watches())

        # They asked for it. Saying it on every start would be noise forever, for
        # every league they have ever stopped.
        assert out[0]["resumed"] is False and "stop_watch" in out[0]["why"]
        assert said == []

    def test_a_league_already_watched_is_refused_not_joined_twice(
            self, watch_dir, monkeypatch):
        """The resume task starts before the transport, so a client can call
        watch_draft for the same league mid-join. Overwriting `_WATCHES` leaks a
        socket, and ESPN answers two connections on one team with a LEFT the
        watch reads as a pause."""
        TestResumingOnStart()._stub(monkeypatch)
        watchstore.save(_record())
        server._WATCHES["L"] = ("already", None)
        said = self._said(monkeypatch)

        out = asyncio.run(server.resume_watches())

        assert out[0]["resumed"] is False
        assert "already running" in out[0]["why"]
        assert "already running" in said[-1][0]
        assert server._WATCHES["L"] == ("already", None), "the live watch is untouched"

    def test_a_slow_init_says_still_joining_and_the_state_agrees(
            self, watch_dir, monkeypatch):
        """The message and `_WATCHES` must not disagree.

        `draft_room` and `draft_status` answer from `_WATCHES`, so a watch the
        user was told does not exist would still be answering questions -- and if
        INIT lands a second after the timeout it is fully live. The socket is up,
        so the message says so.
        """
        TestResumingOnStart()._stub(monkeypatch, never_ready=True)
        monkeypatch.setattr(server, "RESUME_READY_SECONDS", 0.01)
        watchstore.save(_record())
        said = self._said(monkeypatch)

        out = asyncio.run(server.resume_watches())

        # Not False: the watch is up and only the draft state is outstanding. A
        # boolean saying "no" beside a `why` saying "joined" is the same
        # field-against-field contradiction this branch removes.
        assert out[0]["resumed"] == "joining"
        assert "still to come" in out[0]["why"]
        content, meta = said[-1]
        assert "has not sent the draft state yet" in content
        assert "picks are being recorded" in content
        assert meta["event"] == "resuming"
        # The state agrees with the sentence: the watch is there because it is.
        assert "L" in server._WATCHES

    def test_a_watch_that_dies_before_init_is_refused_and_removed(
            self, watch_dir, monkeypatch):
        """The other half: when the watch really is gone, the refusal is true and
        nothing is left in `_WATCHES` to answer from."""
        calls = TestResumingOnStart()._stub(monkeypatch, never_ready=True)
        assert calls is not None

        from ffdraft import watch as watch_mod

        class DyingWatch(watch_mod.DraftWatch):
            async def run(self):
                raise RuntimeError("ESPN refused the join")

        monkeypatch.setattr(watch_mod, "DraftWatch", DyingWatch)
        watchstore.save(_record())
        said = self._said(monkeypatch)

        out = asyncio.run(server.resume_watches())

        assert out[0]["resumed"] is False
        assert "stopped before ESPN sent INIT" in out[0]["why"]
        assert "L" not in server._WATCHES, "a refusal must leave nothing answering"
        assert "watch NOT resumed" in said[-1][0]

    def test_the_queue_is_still_re_sent_when_init_arrives_late(
            self, watch_dir, monkeypatch):
        """A live watch and a queue never restored is half the feature silently
        missing, which is the shape of the defect this task is about."""
        calls = TestResumingOnStart()._stub(monkeypatch, never_ready=True)
        monkeypatch.setattr(server, "RESUME_READY_SECONDS", 0.01)
        watchstore.save(_record(queue=[11, 22]))
        said = self._said(monkeypatch)

        async def go():
            await server.resume_watches()
            assert "ids" not in calls, "nothing sent while INIT is outstanding"
            w, _task = server._WATCHES["L"]
            w.ready.set()                      # ESPN finally sends INIT
            # Wait on the finisher's own handle rather than on a duration.
            finish = next(t for t in asyncio.all_tasks()
                          if t.get_name().startswith("ffdraft-finish-resume"))
            await finish

        asyncio.run(go())

        assert calls["ids"] == [11, 22]
        assert "queue re-sent, 2 entries, 1 of them yours" in said[-1][0]

    def test_a_late_init_with_no_echo_also_re_sends_from_the_record(
            self, watch_dir, monkeypatch):
        """The two resume paths must not disagree. A slow join is the common one
        after a restart, and it is the one where ESPN is least likely to have
        echoed anything."""
        calls = TestResumingOnStart()._stub(monkeypatch, never_ready=True, echo=False)
        monkeypatch.setattr(server, "RESUME_READY_SECONDS", 0.01)
        watchstore.save(_record(queue=[11, 22]))
        said = self._said(monkeypatch)

        async def go():
            await server.resume_watches()
            w, _task = server._WATCHES["L"]
            w.ready.set()
            finish = next(t for t in asyncio.all_tasks()
                          if t.get_name().startswith("ffdraft-finish-resume"))
            await finish

        asyncio.run(go())

        assert calls["sends"] == [{"ids": [11, 22], "replace": False},
                                  {"ids": [11, 22], "replace": True}]
        assert "queue re-sent from the record, 2 entries" in said[-1][0]

    def test_the_first_ready_future_is_not_left_pending(self, watch_dir, monkeypatch):
        """`_finish_resume` builds its own. Left pending, the first one outlives
        the call and a room that never sends INIT ends the process with
        "Task was destroyed but it is pending"."""
        TestResumingOnStart()._stub(monkeypatch, never_ready=True)
        monkeypatch.setattr(server, "RESUME_READY_SECONDS", 0.01)
        watchstore.save(_record())
        monkeypatch.setattr(server, "_channel", lambda content, meta: _done())

        async def go():
            await server.resume_watches()
            leftovers = [t for t in asyncio.all_tasks()
                         if t is not asyncio.current_task()
                         and not t.get_name().startswith("ffdraft-finish-resume")
                         and "draft-watch" not in t.get_name()]
            # Await the handles rather than counting scheduler turns: a task
            # cancelled a moment ago is not `done()` until the loop comes back
            # to it, so "still pending" and "already cancelled" look identical
            # at this instant.
            outcomes = await asyncio.gather(*leftovers, return_exceptions=True)
            return [(t.get_name(), o) for t, o in zip(leftovers, outcomes)]

        left = asyncio.run(go())

        still_running = [name for name, outcome in left
                         if not isinstance(outcome, asyncio.CancelledError)]
        assert still_running == [], f"left running: {still_running}"


class TestTheStoreSurvivesACrashMidWrite:
    def test_the_write_is_atomic(self, watch_dir, monkeypatch):
        """`save` runs from `set_draft_queue` on every accepted queue, which is
        mid-draft. A truncating write plus `load`'s tolerance turns a crash into
        a record that reads as absent -- the silent no-resume this module is
        about, reachable through its own write path."""
        watchstore.save(_record(queue=[1]))
        real_replace = watchstore.os.replace

        def crash(src, dst):
            raise OSError("power cut between write and rename")

        monkeypatch.setattr(watchstore.os, "replace", crash)
        with pytest.raises(OSError):
            watchstore.save(_record(queue=[2, 3]))
        monkeypatch.setattr(watchstore.os, "replace", real_replace)

        # The old record is intact: the failed write never touched it.
        back = watchstore.load("L")
        assert back is not None and back.queue == [1]


class TestTheChannelWaitsForASession:
    """There is no session at server start: sessions are per request, and what
    outlives them is the connection's standalone channel. So a resume that runs
    before any client request has nowhere to speak and holds its message."""

    def test_a_message_with_no_session_is_held_not_lost(self, monkeypatch):
        monkeypatch.setattr(server, "_SESSION", None)
        monkeypatch.setattr(server, "_PENDING_CHANNEL", [])

        asyncio.run(server._channel("resumed", {"event": "resumed"}))

        assert len(server._PENDING_CHANNEL) == 1
        content, meta, _held_at = server._PENDING_CHANNEL[0]
        assert (content, meta) == ("resumed", {"event": "resumed"})

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
        monkeypatch.setattr(server, "_PENDING_CHANNEL", [("held", {}, 0.0)])

        assert server._attach_session(object()) is None

        assert server._PENDING_CHANNEL == [("held", {}, 0.0)], "held for the next attach"

    def test_a_send_that_fails_is_held_rather_than_raising(self, monkeypatch):
        """This runs inside the watch's socket loop; a failed notification must
        not take the socket down with it."""
        class Broken:
            async def send_notification(self, note):
                raise RuntimeError("the client went away")

        monkeypatch.setattr(server, "_SESSION", Broken())
        monkeypatch.setattr(server, "_PENDING_CHANNEL", [])

        asyncio.run(server._channel("pick 130", {"event": "pick"}))

        assert len(server._PENDING_CHANNEL) == 1
        content, meta, _held_at = server._PENDING_CHANNEL[0]
        assert (content, meta) == ("pick 130", {"event": "pick"})
